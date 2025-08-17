# Python
import io
from typing import Any, Callable, Dict, Optional

import pytest


class StreamingBody:
    """Simple read()-compatible stream similar to S3 Body."""
    def __init__(self, data: bytes):
        self._bio = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._bio.read(size)


class FakeS3:
    def __init__(self, data_by_key: Optional[Dict[str, bytes]] = None):
        self.data_by_key = data_by_key or {}
        self.get_object_calls: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> Dict[str, Any]:
        self.get_object_calls.append((Bucket, Key))
        data = self.data_by_key.get(Key, b"")
        return {"Body": StreamingBody(data)}


class FakeDDB:
    def __init__(self, behavior: str = "success"):
        """
        behavior:
          - "success": put_item succeeds
          - "duplicate": put_item raises ConditionalCheckFailedException
          - "error": put_item raises a generic ClientError
        """
        self.behavior = behavior
        self.put_item_calls: list[tuple[str, Dict[str, Any], str]] = []

    def put_item(self, TableName, Item, ConditionExpression):
        from botocore.exceptions import ClientError
        self.put_item_calls.append((TableName, Item, ConditionExpression))
        if self.behavior == "success":
            return {}
        if self.behavior == "duplicate":
            raise ClientError(
                error_response={"Error": {"Code": "ConditionalCheckFailedException", "Message": "duplicate"}},
                operation_name="PutItem",
            )
        if self.behavior == "error":
            raise ClientError(
                error_response={"Error": {"Code": "InternalServerError", "Message": "boom"}},
                operation_name="PutItem",
            )
        return {}


class MetricsSpy:
    def __init__(self):
        self.calls: list[tuple[str, float]] = []

    def count(self, name: str, value: float):
        self.calls.append((name, value))


@pytest.fixture
def fake_s3_factory() -> Callable[[Optional[Dict[str, bytes]]], FakeS3]:
    """Factory to build a FakeS3 with optional initial content."""
    def _factory(data_by_key: Optional[Dict[str, bytes]] = None) -> FakeS3:
        return FakeS3(data_by_key)
    return _factory


@pytest.fixture
def fake_ddb_factory() -> Callable[[str], FakeDDB]:
    """Factory to build a FakeDDB with a given behavior."""
    def _factory(behavior: str = "success") -> FakeDDB:
        return FakeDDB(behavior)
    return _factory


@pytest.fixture
def wire_aws_clients(monkeypatch):
    """
    Returns a function to wire provided fake S3/DDB clients into a module under test.
    Usage:
        wire = wire_aws_clients
        wire(s3_ingest, s3=fake_s3, ddb=fake_ddb)
    """
    def _wire(module, *, s3=None, ddb=None):
        if s3 is not None:
            monkeypatch.setattr(module, "_s3", s3, raising=True)
        if ddb is not None:
            monkeypatch.setattr(module, "_ddb", ddb, raising=True)
    return _wire


@pytest.fixture
def metrics_spy(monkeypatch):
    """
    Monkey-patches module.metrics with a spy object that records count() calls.
    To use with a module: monkeypatch.setattr(module, "metrics", spy)
    """
    spy = MetricsSpy()
    return spy


@pytest.fixture
def exif_stub(monkeypatch):
    """
    Provides helpers to stub EXIF functions in a target module.
    Usage:
        exif_stub.apply(s3_ingest, title="T", caption="C", tags=["t1"], gps={"lat":1}, exif={"foo":"bar"})
    """
    class _Stub:
        def apply(
            self,
            module,
            *,
            title: str = "My Title",
            caption: str = "My Caption",
            tags: list[str] = None,
            gps: Optional[Dict[str, float]] = None,
            exif: Dict[str, Any] = None,
            raw_meta: Dict[str, Any] = None,
        ):
            tags = tags or ["tag1", "tag2"]
            gps = gps if gps is not None else {"lat": 1.23, "lon": 4.56}
            exif = exif or {"foo": "bar"}
            raw_meta = raw_meta or {"Some": "RawExifData"}

            def run_exiftool_json(path: str):
                return raw_meta

            def parse_exif_fields(meta, fallback_title: str):
                return (title, caption, tags, {"gps": gps, "exif": exif})

            monkeypatch.setattr(module, "run_exiftool_json", run_exiftool_json, raising=True)
            monkeypatch.setattr(module, "parse_exif_fields", parse_exif_fields, raising=True)

    return _Stub()


@pytest.fixture
def event_factory() -> Callable[[str, str], Dict[str, Any]]:
    def _make(bucket: str, key: str) -> Dict[str, Any]:
        return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}
    return _make


@pytest.fixture
def unlink_recorder(monkeypatch):
    """
    Records os.unlink calls. Use:
        path_calls = unlink_recorder.record_in(module_under_test)
        ... run test ...
        assert path_calls  # not empty
    """
    class _Rec:
        def record_in(self, module):
            called_paths: list[str] = []
            real_unlink = module.os.unlink

            def recorder(path):
                called_paths.append(path)
                try:
                    real_unlink(path)
                except FileNotFoundError:
                    pass

            monkeypatch.setattr(module.os, "unlink", recorder, raising=True)
            return called_paths

    return _Rec()


# ---- New: FakeRepo for PhotoRepository monkeypatching ----

class FakeRepo:
    """
    Minimal stub for PhotoRepository.
    behavior:
      - "success": put_photo_item returns (True, None)
      - "duplicate": returns (False, "duplicate")
      - "error": returns (False, "boom")
    """
    def __init__(self, behavior: str = "success"):
        self.behavior = behavior
        self.calls: list[dict] = []

    def put_photo_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.behavior == "success":
            return True, None
        if self.behavior == "duplicate":
            return False, "duplicate"
        return False, "boom"


@pytest.fixture
def repo_factory() -> Callable[[str], FakeRepo]:
    """Factory to build a FakeRepo with a given behavior."""
    def _factory(behavior: str = "success") -> FakeRepo:
        return FakeRepo(behavior)
    return _factory


@pytest.fixture
def fake_repo(repo_factory) -> FakeRepo:
    """
    Default FakeRepo instance (success behavior). Tests that need different
    behavior can use repo_factory to construct their own and monkeypatch it.
    """
    return repo_factory("success")
