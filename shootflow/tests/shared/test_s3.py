# Python
import hashlib
import os

import pytest

from shootflow.shared.s3 import S3Storage


def test_stream_to_temp_and_sha256_writes_file_and_returns_hash(fake_s3_factory, tmp_path):
    bucket = "test-bucket"
    key = "path/to/object.bin"
    data = b"some test bytes for hashing"
    fake_s3 = fake_s3_factory({key: data})

    s3 = S3Storage()
    # Replace underlying client with the fake
    s3._s3 = fake_s3  # type: ignore[attr-defined]

    digest, tmp_path_str = s3.stream_to_temp_and_sha256(bucket, key)

    # Verify expected SHA256
    expected = hashlib.sha256(data).hexdigest()
    assert digest == expected

    # Temp file exists and contains exact bytes
    assert os.path.exists(tmp_path_str)
    with open(tmp_path_str, "rb") as f:
        assert f.read() == data

    # Underlying get_object was called with correct args
    assert getattr(fake_s3, "get_object_calls", []) == [(bucket, key)]

    # Caller is responsible for cleanup
    os.unlink(tmp_path_str)
    assert not os.path.exists(tmp_path_str)


def test_stream_to_temp_and_sha256_handles_empty_object(fake_s3_factory):
    bucket = "bkt"
    key = "empty"
    data = b""
    fake_s3 = fake_s3_factory({key: data})

    s3 = S3Storage()
    s3._s3 = fake_s3  # type: ignore[attr-defined]

    digest, tmp_path_str = s3.stream_to_temp_and_sha256(bucket, key)

    # SHA256 of empty bytes
    assert digest == hashlib.sha256(b"").hexdigest()

    # File exists and is zero-length
    assert os.path.exists(tmp_path_str)
    assert os.path.getsize(tmp_path_str) == 0

    os.unlink(tmp_path_str)


def test_download_bytes_returns_full_payload(fake_s3_factory):
    bucket = "photos"
    key = "ready/image.jpg"
    data = b"\x00\x01\x02binary\xff\xfe"
    fake_s3 = fake_s3_factory({key: data})

    s3 = S3Storage()
    s3._s3 = fake_s3  # type: ignore[attr-defined]

    out = s3.download_bytes(bucket, key)
    assert out == data
    assert getattr(fake_s3, "get_object_calls", []) == [(bucket, key)]


def test_download_bytes_empty_object(fake_s3_factory):
    bucket = "photos"
    key = "missing-or-empty"
    data = b""
    fake_s3 = fake_s3_factory({key: data})

    s3 = S3Storage()
    s3._s3 = fake_s3  # type: ignore[attr-defined]

    out = s3.download_bytes(bucket, key)
    assert out == b""
