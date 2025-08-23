# Python
import io
import hashlib
import tempfile
import json
import types
import sys
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


class FakeS3Store:
    """
    S3Storage-compatible adapter over FakeS3 for tests that expect the shared S3 interface.
    """
    def __init__(self, fake_s3: FakeS3):
        self._fake_s3 = fake_s3

    def stream_to_temp_and_sha256(self, bucket: str, key: str):
        resp = self._fake_s3.get_object(Bucket=bucket, Key=key)
        body = resp["Body"]

        hasher = hashlib.sha256()
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp_path = tmp.name
        with tmp as f:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                f.write(chunk)
        return hasher.hexdigest(), tmp_path


class FakeDDB:
    def __init__(self, behavior: str = "success"):
        """
        behavior:
          - "success": put_item succeeds
          - "duplicate": put_item raises ConditionalCheckFailedException
          - "error": put_item raises a generic ClientError

        Additional capabilities for repository tests:
          - query_responses: list of "Items" lists returned per query() call
          - query_calls: recorded query() kwargs
          - update_calls: recorded update_item() kwargs
        """
        self.behavior = behavior
        self.put_item_calls: list[tuple[str, Dict[str, Any], str]] = []
        self.query_calls: list[Dict[str, Any]] = []
        self.update_calls: list[Dict[str, Any]] = []
        self.query_responses: list[list[Dict[str, Any]]] = []

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

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        items = self.query_responses.pop(0) if self.query_responses else []
        return {"Items": items}

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)
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
def fake_s3_store_factory() -> Callable[[FakeS3], FakeS3Store]:
    """Factory to wrap a FakeS3 into an S3Storage-compatible FakeS3Store."""
    def _factory(fake_s3: FakeS3) -> FakeS3Store:
        return FakeS3Store(fake_s3)
    return _factory


@pytest.fixture
def fake_ddb_factory() -> Callable[[str], FakeDDB]:
    """Factory to build a FakeDDB with a given behavior."""
    def _factory(behavior: str = "success") -> FakeDDB:
        return FakeDDB(behavior)
    return _factory


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


class FakeSecretsManager:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": json.dumps(self.payload)}


class FakeBoto3SecretsOnly:
    """
    Minimal boto3 shim that only returns a Secrets Manager client.
    """
    def __init__(self, sm: FakeSecretsManager):
        self._sm = sm

    def client(self, name, region_name=None):
        if name != "secretsmanager":
            raise AssertionError(f"Unexpected boto3 client requested in Flickr tests: {name}")
        return self._sm


class FakeFlickrAPI:
    """
    Minimal flickr_api shim for tests:
      - set_keys, set_timeout, set_auth_handler are recorded
      - upload() records calls and can return side effects
      - test.login returns a stable value
      - auth includes AuthHandler and OAuthToken classes
    """
    def __init__(self):
        self.keys = None
        self.timeout = None
        self.auth_handler = None
        self.upload_calls = []
        self._upload_side_effects = []
        self.test = types.SimpleNamespace(login=lambda: {"user": {"username": {"_content": "testuser"}}})

        class _AuthHandler:
            def __init__(self):
                self.access_token = None
                self.user_nsid = None
                self.username = None

        class _OAuthToken:
            def __init__(self, key, secret):
                self.key = key
                self.secret = secret

        self.auth = types.SimpleNamespace(AuthHandler=_AuthHandler, OAuthToken=_OAuthToken)

    def set_keys(self, *, api_key, api_secret):
        self.keys = (api_key, api_secret)

    def set_timeout(self, timeout_seconds):
        self.timeout = timeout_seconds

    def set_auth_handler(self, handler):
        self.auth_handler = handler

    def upload(self, **kwargs):
        self.upload_calls.append(kwargs)
        if self._upload_side_effects:
            eff = self._upload_side_effects.pop(0)
            if isinstance(eff, BaseException):
                raise eff
            return eff
        # Default object with .id to mimic flickr_api upload return
        return types.SimpleNamespace(id="999")


class ReadTimeoutFake(Exception):
    pass


class ConnectionErrorFake(Exception):
    pass


class FakeRawFlickr:
    """
    Minimal shim for flickr_api.api.flickr to support groups_pools_add.
    Records calls and can simulate failure via side_effect.
    """
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.side_effect: Optional[BaseException] = None

    def groups_pools_add(self, *, photo_id: str, group_id: str):
        self.calls.append((photo_id, group_id))
        if self.side_effect:
            raise self.side_effect
        return {"stat": "ok"}


@pytest.fixture
def flickr_test_env(monkeypatch):
    """
    Patches shootflow.flickr.flickr module to use:
      - FakeSecretsManager via a minimal boto3 shim (secrets only)
      - FakeFlickrAPI as the flickr_api module
      - requests.exceptions mapped to local fake classes for retry testing
      - A fake flickr_api.api.flickr module for groups.pools.add

    Returns a dict with handles to the fakes for assertions and control:
      {"fake_api": FakeFlickrAPI, "fake_sm": FakeSecretsManager, "raw_flickr": FakeRawFlickr}
    """
    import shootflow.flickr.flickr as flickr_mod
    from shootflow.shared.config import settings

    # Stable, fast tunables
    settings.AWS_REGION = "us-east-1"
    settings.FLICKR_SECRET_NAME = "FAKE-SECRET"
    settings.FLICKR_UPLOAD_TIMEOUT_SECONDS = 5
    settings.FLICKR_UPLOAD_MAX_RETRIES = 3
    settings.FLICKR_UPLOAD_BACKOFF_SECONDS = 0.0

    # Secrets payload used by FlickrAPIClient __init__
    secret = {
        "api_key": "k",
        "api_secret": "s",
        "oauth_token": "tok",
        "oauth_token_secret": "toksec",
        "user_nsid": "123@N00",
        "username": "demo",
    }
    fake_sm = FakeSecretsManager(secret)
    fake_boto3 = FakeBoto3SecretsOnly(fake_sm)
    monkeypatch.setattr(flickr_mod, "boto3", fake_boto3, raising=True)

    fake_api = FakeFlickrAPI()
    monkeypatch.setattr(flickr_mod, "flickr_api", fake_api, raising=True)

    # Shim for requests exceptions used for retry detection
    req_shim = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(ReadTimeout=ReadTimeoutFake, ConnectionError=ConnectionErrorFake)
    )
    monkeypatch.setattr(flickr_mod, "requests", req_shim, raising=True)

    # Provide flickr_api.api.flickr for add_photo_to_group()
    fake_raw = FakeRawFlickr()
    api_module = types.ModuleType("flickr_api.api")
    api_module.flickr = fake_raw
    monkeypatch.setitem(sys.modules, "flickr_api.api", api_module)

    return {
        "fake_api": fake_api,
        "fake_sm": fake_sm,
        "ReadTimeout": ReadTimeoutFake,
        "ConnectionError": ConnectionErrorFake,
        "raw_flickr": fake_raw,
    }
