# Python
import pytest

import shootflow.handlers.flickr_poster as flickr_poster
from shootflow.shared.config import settings
from shootflow.shared.ddb_repo import PhotoRepository


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

    # Metric emitted
    assert ("FlickrPosted", 1.0) in metrics_spy.calls


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
