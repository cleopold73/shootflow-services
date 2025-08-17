# Python
import hashlib

import shootflow.handlers.s3_ingest as s3_ingest


def test_maybe_url_unquote():
    assert s3_ingest._maybe_url_unquote("ready/IMG%20123+test.jpg") == "ready/IMG 123 test.jpg"
    assert s3_ingest._maybe_url_unquote("ready/plain.jpg") == "ready/plain.jpg"


def test_handler_success_ingest_unlinks_temp_and_counts_metric(
    fake_s3_factory,
    fake_s3_store_factory,
    metrics_spy,
    exif_stub,
    unlink_recorder,
    repo_factory,
    event_factory,
    monkeypatch,
):
    bucket = "test-bucket"
    key = "ready/photo.jpg"

    payload = b"hello world"
    fake_s3 = fake_s3_factory({key: payload})
    s3_ingest._s3_store = fake_s3_store_factory(fake_s3)

    # Repo stub and metrics spy
    repo = repo_factory("success")
    monkeypatch.setattr(s3_ingest, "_repo", repo, raising=True)
    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)

    # Stub EXIF functions
    exif_stub.apply(s3_ingest)

    # Record temp cleanup
    called_paths = unlink_recorder.record_in(s3_ingest)

    event = event_factory(bucket, key)  # direct S3 event
    result = s3_ingest.handler(event, None)

    assert result == {"processed": 1, "errors": 0}

    # Verify repository was called once with expected data
    assert len(repo.calls) == 1
    call = repo.calls[0]
    expected_photo_id = hashlib.sha256(payload).hexdigest()
    assert call["photoId"] == expected_photo_id
    assert call["s3_key"] == key
    assert call["filename"] == "photo.jpg"

    # Metric and temp cleanup checks
    assert ("IngestSucceeded", 1.0) in metrics_spy.calls
    assert len(called_paths) >= 1


def test_handler_duplicate_ingest_counts_duplicate(
    fake_s3_factory, fake_s3_store_factory, metrics_spy, exif_stub, event_factory, repo_factory, monkeypatch
):
    bucket = "test-bucket"
    key = "ready/dup.jpg"

    fake_s3 = fake_s3_factory({key: b"dup"})
    s3_ingest._s3_store = fake_s3_store_factory(fake_s3)

    repo = repo_factory("duplicate")
    monkeypatch.setattr(s3_ingest, "_repo", repo, raising=True)
    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)
    exif_stub.apply(s3_ingest)

    result = s3_ingest.handler(event_factory(bucket, key), None)

    assert result == {"processed": 1, "errors": 0}
    assert ("IngestDuplicate", 1.0) in metrics_spy.calls
    assert len(repo.calls) == 1


def test_handler_ddb_error_counts_failed(
    fake_s3_factory, fake_s3_store_factory, metrics_spy, exif_stub, event_factory, repo_factory, monkeypatch
):
    bucket = "test-bucket"
    key = "ready/fail.jpg"

    fake_s3 = fake_s3_factory({key: b"data"})
    s3_ingest._s3_store = fake_s3_store_factory(fake_s3)

    repo = repo_factory("error")
    monkeypatch.setattr(s3_ingest, "_repo", repo, raising=True)
    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)
    exif_stub.apply(s3_ingest)

    result = s3_ingest.handler(event_factory(bucket, key), None)

    # Error in ingest should be counted, no processed increment
    assert result == {"processed": 0, "errors": 1}
    assert ("IngestFailed", 1.0) in metrics_spy.calls
    assert len(repo.calls) == 1


def test_handler_skips_non_ready_prefix_but_counts_as_processed(
    fake_s3_factory, fake_s3_store_factory, metrics_spy, exif_stub, event_factory, repo_factory, monkeypatch
):
    bucket = "test-bucket"
    key = "uploads/photo.jpg"  # not under ready/ prefix

    fake_s3 = fake_s3_factory({key: b"irrelevant"})
    s3_ingest._s3_store = fake_s3_store_factory(fake_s3)

    repo = repo_factory("success")
    monkeypatch.setattr(s3_ingest, "_repo", repo, raising=True)
    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)
    exif_stub.apply(s3_ingest)

    result = s3_ingest.handler(event_factory(bucket, key), None)

    # Considered processed (no retry), but nothing ingested
    assert result == {"processed": 1, "errors": 0}
    assert repo.calls == []
    assert not any(name in ("IngestSucceeded", "IngestDuplicate", "IngestFailed") for name, _ in metrics_spy.calls)


def test_handler_malformed_record_increments_errors(metrics_spy):
    # Direct S3 event with malformed record (missing bucket/name or object/key)
    event = {"Records": [{"s3": {"bucket": {}, "object": {}}}]}

    result = s3_ingest.handler(event, None)

    assert result == {"processed": 0, "errors": 1}
    assert metrics_spy.calls == []


def test_handler_ingest_exception_increments_errors_and_counts_failed(
    metrics_spy, event_factory, repo_factory, monkeypatch
):
    bucket = "test-bucket"
    key = "ready/will-crash.jpg"

    def boom(bucket_arg, key_arg):
        raise RuntimeError("read error")

    # Force the compute step to raise before repository is used
    monkeypatch.setattr(s3_ingest, "_compute_sha256_and_save_temp_streaming", boom, raising=True)
    monkeypatch.setattr(s3_ingest, "_repo", repo_factory("success"), raising=True)
    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)

    result = s3_ingest.handler(event_factory(bucket, key), None)

    assert result == {"processed": 0, "errors": 1}
    assert ("IngestFailed", 1.0) in metrics_spy.calls


def test_handler_url_decoding_before_ingest(
    fake_s3_factory, fake_s3_store_factory, metrics_spy, exif_stub, event_factory, repo_factory, monkeypatch
):
    bucket = "test-bucket"
    encoded_key = "ready/IMG%20123+Sunset.jpg"
    decoded_key = "ready/IMG 123 Sunset.jpg"

    fake_s3 = fake_s3_factory({decoded_key: b"bytes"})
    s3_ingest._s3_store = fake_s3_store_factory(fake_s3)
    monkeypatch.setattr(s3_ingest, "_repo", repo_factory("success"), raising=True)
    monkeypatch.setattr(s3_ingest, "metrics", metrics_spy, raising=True)
    exif_stub.apply(s3_ingest)

    result = s3_ingest.handler(event_factory(bucket, encoded_key), None)

    assert result == {"processed": 1, "errors": 0}
    # Ensure S3 was called with the decoded key via the underlying fake
    assert getattr(fake_s3, "get_object_calls", []) == [(bucket, decoded_key)]