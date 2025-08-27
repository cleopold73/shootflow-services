# Python
import json
import os
import pytest

import shootflow.handlers.s3_ingest as s3_ingest
from shootflow.shared.config import settings


def test_success_single_record_streams_exif_puts_and_cleans(
    fake_s3_factory,
    fake_s3_store_factory,
    repo_factory,
    exif_stub,
    metrics_spy,
    monkeypatch,
    unlink_recorder,
):
    # Settings
    settings.S3_INGEST_STATUS_READY = "READY"

    # Fake S3 with content for the key
    bucket = "photos"
    key = "ready/a.jpg"
    data = b"\x00\x01image"
    fake_s3 = fake_s3_factory({key: data})
    # Wire S3Storage-compatible adapter into module (uses get_object underneath)
    monkeypatch.setattr(s3_ingest, "_s3_store", fake_s3_store_factory(fake_s3), raising=True)

    # Stub EXIF: deterministic output
    exif_stub.apply(
        s3_ingest,
        title="My Title",
        caption="My Caption",
        tags=["t1", "t2"],
        gps={"lat": 1.23, "lon": 4.56},
        exif={"foo": "bar"},
        raw_meta={"foo": "bar"},
    )

    # Fake repo to capture put_photo_item
    fake_repo = repo_factory("success")
    monkeypatch.setattr(s3_ingest, "_repo", fake_repo, raising=True)

    # Metrics spy
    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)

    # Record unlink calls to ensure temp cleanup
    unlinks = unlink_recorder.record_in(s3_ingest)

    event = {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}
    got = s3_ingest.handler(event, None)

    assert got == {"processed": 1, "errors": 0}
    # Repo put called with normalized values
    assert len(fake_repo.calls) == 1
    call = fake_repo.calls[0]
    assert call["s3_key"] == key
    assert call["filename"] == "a.jpg"
    assert call["title"] == "My Title"
    assert call["caption"] == "My Caption"
    assert call["tags"] == ["t1", "t2"]
    assert call["gps"] == {"lat": 1.23, "lon": 4.56}
    assert call["exif"] == {"foo": "bar"}

    # Metrics: success counted
    assert ("IngestSucceeded", 1.0) in metrics_spy.calls

    # Temp file was cleaned up
    assert unlinks  # at least one unlink call
    for p in unlinks:
        assert not os.path.exists(p)

    # S3 was read once through FakeS3Store -> FakeS3.get_object
    assert fake_s3.get_object_calls == [(bucket, key)]


def test_duplicate_item_counts_duplicate_not_error(
    fake_s3_factory,
    fake_s3_store_factory,
    repo_factory,
    exif_stub,
    metrics_spy,
    monkeypatch,
):
    bucket = "photos"
    key = "ready/dup.jpg"
    fake_s3 = fake_s3_factory({key: b"img"})
    monkeypatch.setattr(s3_ingest, "_s3_store", fake_s3_store_factory(fake_s3), raising=True)

    exif_stub.apply(s3_ingest, title=None, caption=None, tags=["t"])

    fake_repo = repo_factory("duplicate")
    monkeypatch.setattr(s3_ingest, "_repo", fake_repo, raising=True)

    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)

    event = {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}
    got = s3_ingest.handler(event, None)

    # Handler still counts as processed (no exception), errors remain 0
    assert got == {"processed": 1, "errors": 0}
    # Duplicate metric
    assert ("IngestDuplicate", 1.0) in metrics_spy.calls

    # Ensure FakeS3 was invoked
    assert fake_s3.get_object_calls == [(bucket, key)]


def test_put_item_error_bubbles_and_counts_failed(
    fake_s3_factory,
    fake_s3_store_factory,
    repo_factory,
    exif_stub,
    metrics_spy,
    monkeypatch,
):
    bucket = "photos"
    key = "ready/err.jpg"
    fake_s3 = fake_s3_factory({key: b"img"})
    monkeypatch.setattr(s3_ingest, "_s3_store", fake_s3_store_factory(fake_s3), raising=True)

    exif_stub.apply(s3_ingest, title="T", caption="C", tags=["t"])

    fake_repo = repo_factory("error")
    monkeypatch.setattr(s3_ingest, "_repo", fake_repo, raising=True)

    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)

    event = {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}
    got = s3_ingest.handler(event, None)

    # put_item error raises inside _ingest_single_record, handler catches and counts error
    assert got == {"processed": 0, "errors": 1}
    assert ("IngestFailed", 1.0) in metrics_spy.calls

    # Ensure FakeS3 was invoked
    assert fake_s3.get_object_calls == [(bucket, key)]


def test_skips_non_ready_prefix_counts_processed(
    fake_s3_factory,
    fake_s3_store_factory,
    repo_factory,
    metrics_spy,
    monkeypatch,
):
    bucket = "photos"
    key = "uploads/not_ready.jpg"  # does not start with READY_PREFIX
    fake_s3 = fake_s3_factory({key: b"img"})
    # Even if wired, stream_to_temp should not be called for non-ready prefix; using FakeS3Store is safe
    monkeypatch.setattr(s3_ingest, "_s3_store", fake_s3_store_factory(fake_s3), raising=True)

    # Repo should not be used
    fake_repo = repo_factory("success")
    monkeypatch.setattr(s3_ingest, "_repo", fake_repo, raising=True)

    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)

    event = {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}
    got = s3_ingest.handler(event, None)

    # Considered processed (record handled) with no errors
    assert got == {"processed": 1, "errors": 0}
    # No repo calls made
    assert fake_repo.calls == []
    # No S3 get_object since non-ready path short-circuits
    assert fake_s3.get_object_calls == []


def test_malformed_event_increments_errors(metrics_spy):
    event = {"Records": [{"s3": {"bucket": {"name": "b"}}}]}  # missing object.key
    got = s3_ingest.handler(event, None)
    assert got == {"processed": 0, "errors": 1}
    # No S3 or repo paths are exercised in this scenario


def test_url_unquote_applied_before_stream(
    fake_s3_factory,
    fake_s3_store_factory,
    repo_factory,
    exif_stub,
    metrics_spy,
    monkeypatch,
):
    bucket = "photos"
    unquoted_key = "ready/My File.jpg"
    quoted_key = "ready/My%20File.jpg"

    fake_s3 = fake_s3_factory({unquoted_key: b"img"})
    # Wire the store that uses get_object -> will record calls with the unquoted key if handler unquotes properly
    monkeypatch.setattr(s3_ingest, "_s3_store", fake_s3_store_factory(fake_s3), raising=True)

    exif_stub.apply(s3_ingest, title="T", caption="C", tags=["t"])
    fake_repo = repo_factory("success")
    monkeypatch.setattr(s3_ingest, "_repo", fake_repo, raising=True)
    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)

    event = {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": quoted_key}}}]}
    got = s3_ingest.handler(event, None)

    assert got == {"processed": 1, "errors": 0}
    # Verify unquoted key was used to stream from S3
    assert fake_s3.get_object_calls == [(bucket, unquoted_key)]
