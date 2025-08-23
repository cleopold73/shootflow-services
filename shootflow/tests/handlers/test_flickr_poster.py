# Python
import pytest
from unittest.mock import patch

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


class _FakeFlickrOK:
    def __init__(self):
        self.upload_calls = []

    def upload_bytes(self, data: bytes, *, title=None, description=None, tags=None, **opts) -> str:
        self.upload_calls.append(
            {"len": len(data), "title": title, "description": description, "tags": list(tags or [])}
        )
        return "fl-123"


class _FakeFlickrBoom:
    def upload_bytes(self, data: bytes, *, title=None, description=None, tags=None, **opts) -> str:
        raise RuntimeError("upload failed")


class _S3DownloadAdapter:
    """
    Minimal adapter that provides download_bytes using FakeS3 from conftest.
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


def test_no_ready_items_returns_zero(fake_ddb_factory, metrics_spy):
    settings.S3_BUCKET = "test-bucket"

    fake_ddb = fake_ddb_factory("success")
    # No items in both primary and wrap-around windows
    fake_ddb.query_responses = [[], []]
    repo = PhotoRepository(ddb_client=fake_ddb)

    with patch("shootflow.handlers.flickr_poster._repo", repo), \
         patch("shootflow.handlers.flickr_poster.metrics", metrics_spy), \
         patch("shootflow.handlers.flickr_poster.FlickrAPIClient", return_value=_FakeFlickrOK()):
        got = flickr_poster.handler({}, None)

    assert got == {"posted": 0, "errors": 0}
    assert metrics_spy.calls == []


def test_success_flow_uses_fake_s3_and_ddb(
    fake_s3_factory, fake_ddb_factory, metrics_spy
):
    settings.S3_BUCKET = "photos"

    # DDB returns one READY item
    ddb_item = _ddb_item("p-001", title="Sunset", caption="Beautiful evening", tags=["sunset", "sky"])
    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[ddb_item]]  # first window returns the item
    repo = PhotoRepository(ddb_client=fake_ddb)

    # S3 content for the key
    key = "ready/p-001.jpg"
    data = b"\x01\x02imagebytes"
    fake_s3 = fake_s3_factory({key: data})

    # Flickr client stub
    fake_flickr = _FakeFlickrOK()

    with patch("shootflow.handlers.flickr_poster._repo", repo), \
         patch("shootflow.handlers.flickr_poster._s3_store", _S3DownloadAdapter(fake_s3, "photos")), \
         patch("shootflow.handlers.flickr_poster.FlickrAPIClient", return_value=fake_flickr), \
         patch("shootflow.handlers.flickr_poster.metrics", metrics_spy):
        got = flickr_poster.handler({}, None)
    assert got == {"posted": 1, "errors": 0}

    # S3 called with expected bucket/key
    assert getattr(fake_s3, "get_object_calls", []) == [("photos", key)]

    # Flickr uploaded with expected metadata
    assert len(fake_flickr.upload_calls) == 1
    up = fake_flickr.upload_calls[0]
    assert up["len"] == len(data)

    # DDB was updated to POSTED
    assert len(fake_ddb.update_calls) == 1
    upd = fake_ddb.update_calls[0]
    assert upd["Key"] == {"photoId": {"S": "p-001"}}
    assert ":fid" in upd["ExpressionAttributeValues"] and upd["ExpressionAttributeValues"][":fid"]["S"] == "fl-123"

    # Metric emitted
    assert ("FlickrPosted", 1.0) in metrics_spy.calls


def test_missing_bucket_counts_failed(fake_ddb_factory, metrics_spy, monkeypatch):
    settings.S3_BUCKET = None  # type: ignore

    # Provide one item so code reaches bucket check
    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[_ddb_item("p-002")]]
    repo = PhotoRepository(ddb_client=fake_ddb)
    monkeypatch.setattr(flickr_poster, "_repo", repo, raising=True)
    monkeypatch.setattr(flickr_poster, "metrics", metrics_spy, raising=True)
    # Avoid real Flickr init
    monkeypatch.setattr(flickr_poster, "FlickrAPIClient", lambda: _FakeFlickrOK(), raising=True)

    with pytest.raises(RuntimeError):
        flickr_poster.handler({}, None)

    assert ("FlickrPostFailed", 1.0) in metrics_spy.calls
    # No update if bucket missing
    assert getattr(fake_ddb, "update_calls", []) == []


def test_s3_download_failure_counts_failed(fake_s3_factory, fake_ddb_factory, metrics_spy):
    settings.S3_BUCKET = "photos"

    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[_ddb_item("p-003")]]
    repo = PhotoRepository(ddb_client=fake_ddb)

    class BoomStore:
        def download_bytes(self, bucket, key):
            raise RuntimeError("s3 read failed")

    with patch("shootflow.handlers.flickr_poster._repo", repo), \
         patch("shootflow.handlers.flickr_poster._s3_store", BoomStore()), \
         patch("shootflow.handlers.flickr_poster.FlickrAPIClient", return_value=_FakeFlickrOK()), \
         patch("shootflow.handlers.flickr_poster.metrics", metrics_spy):
        with pytest.raises(RuntimeError):
            flickr_poster.handler({}, None)

    assert ("FlickrPostFailed", 1.0) in metrics_spy.calls
    assert getattr(fake_ddb, "update_calls", []) == []


def test_upload_failure_counts_failed_and_no_mark_posted(
    fake_s3_factory, fake_ddb_factory, metrics_spy
):
    settings.S3_BUCKET = "photos"

    key = "ready/p-004.jpg"
    fake_s3 = fake_s3_factory({key: b"bytes"})

    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[_ddb_item("p-004")]]
    repo = PhotoRepository(ddb_client=fake_ddb)

    with patch("shootflow.handlers.flickr_poster._s3_store", _S3DownloadAdapter(fake_s3, "photos")), \
         patch("shootflow.handlers.flickr_poster._repo", repo), \
         patch("shootflow.handlers.flickr_poster.FlickrAPIClient", return_value=_FakeFlickrBoom()), \
         patch("shootflow.handlers.flickr_poster.metrics", metrics_spy):
        with pytest.raises(RuntimeError):
            flickr_poster.handler({}, None)

    assert ("FlickrPostFailed", 1.0) in metrics_spy.calls
    assert getattr(fake_ddb, "update_calls", []) == []


# --- New fakes and tests for LLM group add flow ---

class _FakeFlickrWithGroups:
    def __init__(self):
        self.upload_calls = []
        self.group_add_calls = []

    def upload_bytes(self, data: bytes, *, title=None, description=None, tags=None, **opts) -> str:
        self.upload_calls.append({"len": len(data), "title": title, "description": description, "tags": list(tags or [])})
        return "fl-xyz"

    def add_photo_to_group(self, photo_id: str, group_id: str):
        self.group_add_calls.append((photo_id, group_id))


def test_llm_groups_selected_and_added(
    fake_s3_factory, fake_ddb_factory, metrics_spy
):
    # Ensure non-dry-run and LLM enabled for this test
    settings.S3_BUCKET = "photos"
    settings.FLICKR_GROUPS_DRY_RUN_ENABLED = False
    settings.FLICKR_GROUPS_VIA_LLM_ENABLED = True

    # DDB returns one READY item
    ddb_item = _ddb_item("p-005", title="City", caption="Skyline", tags=["city"])
    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[ddb_item]]
    repo = PhotoRepository(ddb_client=fake_ddb)

    # S3 data
    key = "ready/p-005.jpg"
    data = b"\x10\x20img"
    fake_s3 = fake_s3_factory({key: data})

    # Flickr client with group add
    fake_flickr = _FakeFlickrWithGroups()

    with patch("shootflow.handlers.flickr_poster._repo", repo), \
         patch("shootflow.handlers.flickr_poster._s3_store", _S3DownloadAdapter(fake_s3, "photos")), \
         patch("shootflow.handlers.flickr_poster.FlickrAPIClient", return_value=fake_flickr), \
         patch("shootflow.handlers.flickr_poster.suggest_groups_via_llm", return_value=["g1", "g2"]), \
         patch("shootflow.handlers.flickr_poster.metrics", metrics_spy):
        got = flickr_poster.handler({}, None)
    assert got == {"posted": 1, "errors": 0}

    # Verify groups were added
    assert fake_flickr.group_add_calls == [("fl-xyz", "g1"), ("fl-xyz", "g2")]
    assert ("FlickrGroupsSuggested", 2.0) in metrics_spy.calls
    assert ("FlickrGroupsAdded", 2.0) in metrics_spy.calls


def test_llm_groups_error_does_not_fail_post(
    fake_s3_factory, fake_ddb_factory, metrics_spy
):
    settings.S3_BUCKET = "photos"
    settings.FLICKR_GROUPS_DRY_RUN_ENABLED = False
    settings.FLICKR_GROUPS_VIA_LLM_ENABLED = True

    # DDB item
    ddb_item = _ddb_item("p-006")
    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[ddb_item]]
    repo = PhotoRepository(ddb_client=fake_ddb)

    # S3 data
    key = "ready/p-006.jpg"
    data = b"\x33\x44img"
    fake_s3 = fake_s3_factory({key: data})

    # Flickr fake
    fake_flickr = _FakeFlickrWithGroups()

    def boom(*args, **kwargs):
        raise RuntimeError("llm down")

    with patch("shootflow.handlers.flickr_poster._repo", repo), \
         patch("shootflow.handlers.flickr_poster._s3_store", _S3DownloadAdapter(fake_s3, "photos")), \
         patch("shootflow.handlers.flickr_poster.FlickrAPIClient", return_value=fake_flickr), \
         patch("shootflow.handlers.flickr_poster.suggest_groups_via_llm", side_effect=boom), \
         patch("shootflow.handlers.flickr_poster.metrics", metrics_spy):
        got = flickr_poster.handler({}, None)
    assert got == {"posted": 1, "errors": 0}
    # No group adds on error
    assert fake_flickr.group_add_calls == []
