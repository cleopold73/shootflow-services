# Python
import pytest
import sys
import types

import shootflow.handlers.flickr_poster as flickr_poster
from shootflow.shared.config import settings
from shootflow.shared.ddb_repo import PhotoRepository
import shootflow.flickr.group_suggester_llm as gl
from shootflow.tests.conftest import FakeBoto3SecretsOnly, FakeSecretsManager


@pytest.fixture(autouse=True, scope="function")
def _mock_openai_for_integration(monkeypatch, flickr_test_env):
    """
    Integration tests mock only external dependencies:
      - AWS Secrets Manager (for OpenAI API key) within group_suggester_llm
      - OpenAI HTTP endpoint via requests.post inside group_suggester_llm
      - flickr_api.objects.Group to provide candidate groups for filtering
    """
    # Enable LLM flow and ensure non-dry-run
    settings.FLICKR_GROUPS_VIA_LLM_ENABLED = True
    settings.FLICKR_GROUPS_DRY_RUN_ENABLED = False

    # Provide required LLM settings
    settings.OPENAI_SECRET_NAME = "OPENAI-SECRET"
    settings.OPENAI_BASE_URL = "https://api.openai.com/v1"
    settings.OPENAI_CHAT_MODEL = "gpt-any"
    settings.LLM_MAX_GROUPS = 5
    settings.LLM_MAX_CANDIDATE_GROUPS = 100
    settings.LLM_HTTP_CONNECT_TIMEOUT_SECONDS = 1.0
    settings.LLM_HTTP_READ_TIMEOUT_SECONDS = 1.0
    settings.LLM_HTTP_RETRIES = 0
    settings.LLM_HTTP_BACKOFF_BASE_SECONDS = 0.0
    settings.AWS_REGION = "us-east-1"

    # Use shared test fakes for Secrets Manager
    openai_sm = FakeSecretsManager({"api_key": "sk-test"})
    monkeypatch.setattr(gl, "boto3", FakeBoto3SecretsOnly(openai_sm), raising=True)

    # Mock OpenAI HTTP to return two groups; LLM logic will filter to candidate groups
    class _RespOK:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {"message": {"content": gl.json.dumps({"selected_group_ids": ["g1", "g2", "g999"]})}}
                ]
            }

    monkeypatch.setattr(gl.requests, "post", lambda *a, **k: _RespOK(), raising=True)

    # Provide a stub for flickr_api.objects.Group.getGroups() so list_member_groups has candidates
    objects_mod = types.ModuleType("flickr_api.objects")

    class _Group:
        def __init__(self, id):
            self.id = id

        def getGroups(self):
            # Minimal iterable of group-like objects
            return [
                types.SimpleNamespace(id="g1", name="One"),
                types.SimpleNamespace(id="g2", name="Two"),
                types.SimpleNamespace(id="gX", name="Other"),
            ]

    objects_mod.Group = _Group  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flickr_api.objects", objects_mod)

    yield


def _ddb_item(photoId: str, *, title=None, caption=None, tags=None):
    item = {
        "photoId": {"S": photoId},
        "status": {"S": "READY"},
        "s3_key": {"S": f"ready/{photoId}.jpg"},
        "filename": {"S": f"{photoId}.jpg"},
    }
    if title is not None:
        item["title"] = {"S": title}
    if caption is not None:
        item["caption"] = {"S": caption}
    if tags:
        item["tags"] = {"L": [{"S": t} for t in tags]}
    return item


class _S3DownloadAdapter:
    """
    Minimal adapter over FakeS3 to satisfy S3Storage.download_bytes used by the handler.
    """
    def __init__(self, fake_s3, expect_bucket: str):
        self._fake_s3 = fake_s3
        self._bucket = expect_bucket

    def download_bytes(self, bucket: str, key: str) -> bytes:
        assert bucket == self._bucket
        resp = self._fake_s3.get_object(Bucket=bucket, Key=key)
        body = resp["Body"]
        chunks = []
        while True:
            c = body.read(1024 * 1024)
            if not c:
                break
            chunks.append(c)
        return b"".join(chunks)


def test_no_ready_items_returns_zero(fake_ddb_factory, metrics_spy, monkeypatch, fake_s3_factory, flickr_test_env):
    settings.S3_BUCKET = "test-bucket"

    # DDB returns no items
    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[], []]
    repo = PhotoRepository(ddb_client=fake_ddb)
    monkeypatch.setattr(flickr_poster, "_repo", repo, raising=True)

    # S3 wired (not used in this case)
    fake_s3 = fake_s3_factory({})
    monkeypatch.setattr(flickr_poster, "_s3_store", _S3DownloadAdapter(fake_s3, "test-bucket"), raising=True)

    # Metrics
    monkeypatch.setattr(flickr_poster, "metrics", metrics_spy, raising=True)

    got = flickr_poster.handler({}, None)
    assert got == {"posted": 0, "errors": 0}
    assert metrics_spy.calls == []


def test_success_end_to_end(fake_s3_factory, fake_ddb_factory, metrics_spy, monkeypatch, flickr_test_env):
    settings.S3_BUCKET = "photos"

    # DDB returns one READY item
    ddb_item = _ddb_item("p-001", title="Sunset", caption="Beautiful evening", tags=["sunset", "sky"])
    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[ddb_item]]
    repo = PhotoRepository(ddb_client=fake_ddb)
    monkeypatch.setattr(flickr_poster, "_repo", repo, raising=True)

    # S3 content for the key
    key = "ready/p-001.jpg"
    data = b"\x01\x02imagebytes"
    fake_s3 = fake_s3_factory({key: data})
    monkeypatch.setattr(flickr_poster, "_s3_store", _S3DownloadAdapter(fake_s3, "photos"), raising=True)

    # Metrics
    monkeypatch.setattr(flickr_poster, "metrics", metrics_spy, raising=True)

    # Capture group add calls by patching the Group instance methods used by the client
    objects_mod = sys.modules["flickr_api.objects"]
    GroupCls = getattr(objects_mod, "Group")
    group_add_calls = []

    def _getInfo(self):
        return {"group": {"id": self.id}}

    def _add(self, *, photo_id):
        group_add_calls.append((photo_id, self.id))

    # The stubbed Group class doesn't define these attributes yet; add them with raising=False
    monkeypatch.setattr(GroupCls, "addPhoto", _add, raising=False)

    got = flickr_poster.handler({}, None)
    assert got == {"posted": 1, "errors": 0}

    # S3 called as expected
    assert getattr(fake_s3, "get_object_calls", []) == [("photos", key)]

    # DDB mark_posted called with Flickr id coming from the FakeFlickrAPI (default "999")
    assert len(fake_ddb.update_calls) == 1
    upd = fake_ddb.update_calls[0]
    assert upd["Key"] == {"photoId": {"S": "p-001"}}
    # Flickr id recorded
    assert ":fid" in upd["ExpressionAttributeValues"]
    assert "S" in upd["ExpressionAttributeValues"][":fid"]

    # Metric emitted for post
    assert ("FlickrPosted", 1.0) in metrics_spy.calls

    # With LLM mocked, the handler should attempt to add the photo to suggested groups
    # Expect two adds with the uploaded photo id ("999") to g1 and g2
    assert set(group_add_calls) == {("999", "g1"), ("999", "g2")}
    # And corresponding metrics for suggestion and add
    assert ("FlickrGroupsSuggested", 2.0) in metrics_spy.calls
    assert ("FlickrGroupsAdded", 2.0) in metrics_spy.calls


def test_missing_bucket_bubbles_and_counts_failed(fake_ddb_factory, metrics_spy, monkeypatch, fake_s3_factory, flickr_test_env):
    settings.S3_BUCKET = None  # type: ignore

    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[_ddb_item("p-002")]]
    repo = PhotoRepository(ddb_client=fake_ddb)
    monkeypatch.setattr(flickr_poster, "_repo", repo, raising=True)

    fake_s3 = fake_s3_factory({})
    monkeypatch.setattr(flickr_poster, "_s3_store", _S3DownloadAdapter(fake_s3, "unused"), raising=True)

    monkeypatch.setattr(flickr_poster, "metrics", metrics_spy, raising=True)

    with pytest.raises(RuntimeError):
        flickr_poster.handler({}, None)

    assert ("FlickrPostFailed", 1.0) in metrics_spy.calls
    assert getattr(fake_ddb, "update_calls", []) == []


def test_s3_download_failure_bubbles_and_counts_failed(fake_s3_factory, fake_ddb_factory, metrics_spy, monkeypatch, flickr_test_env):
    settings.S3_BUCKET = "photos"

    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[_ddb_item("p-003")]]
    repo = PhotoRepository(ddb_client=fake_ddb)
    monkeypatch.setattr(flickr_poster, "_repo", repo, raising=True)

    class BoomStore:
        def download_bytes(self, bucket, key):
            raise RuntimeError("s3 read failed")

    # Force download failure
    monkeypatch.setattr(flickr_poster, "_s3_store", BoomStore(), raising=True)
    monkeypatch.setattr(flickr_poster, "metrics", metrics_spy, raising=True)

    with pytest.raises(RuntimeError):
        flickr_poster.handler({}, None)

    assert ("FlickrPostFailed", 1.0) in metrics_spy.calls
    assert getattr(fake_ddb, "update_calls", []) == []


def test_flickr_upload_failure_bubbles_and_no_mark_posted(fake_s3_factory, fake_ddb_factory, metrics_spy, monkeypatch, flickr_test_env):
    settings.S3_BUCKET = "photos"

    key = "ready/p-004.jpg"
    data = b"bytes"
    fake_s3 = fake_s3_factory({key: data})
    monkeypatch.setattr(flickr_poster, "_s3_store", _S3DownloadAdapter(fake_s3, "photos"), raising=True)

    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[_ddb_item("p-004")]]
    repo = PhotoRepository(ddb_client=fake_ddb)
    monkeypatch.setattr(flickr_poster, "_repo", repo, raising=True)

    # Make the underlying FakeFlickrAPI fail on upload
    env = flickr_test_env
    fake_api = env["fake_api"]
    fake_api._upload_side_effects = [RuntimeError("upload failed")]

    monkeypatch.setattr(flickr_poster, "metrics", metrics_spy, raising=True)

    with pytest.raises(RuntimeError):
        flickr_poster.handler({}, None)

    assert ("FlickrPostFailed", 1.0) in metrics_spy.calls
    assert getattr(fake_ddb, "update_calls", []) == []
