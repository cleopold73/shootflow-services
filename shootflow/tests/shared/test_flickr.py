# Python
import json
import os
import tempfile
import types

import pytest

import shootflow.shared.flickr as flickr_mod
from shootflow.shared.flickr import FlickrAPIClient
from shootflow.shared.config import settings


class _FakeSecretsManager:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def get_secret_value(self, *, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": json.dumps(self.payload)}


class _FakeBoto3:
    def __init__(self, sm: _FakeSecretsManager):
        self.sm = sm

    def client(self, name, region_name=None):
        assert name == "secretsmanager"
        # region_name used but not enforced in tests
        return self.sm


class _FakeAuthHandler:
    def __init__(self):
        # access_token is set by the client
        self.access_token = None
        self.user_nsid = None
        self.username = None


class _FakeOAuthToken:
    def __init__(self, key, secret):
        self.key = key
        self.secret = secret


class _FakeFlickrAPI:
    def __init__(self):
        self.keys = None
        self.timeout = None
        self.auth_handler = None
        self.upload_calls = []
        self._upload_side_effects = []
        self.test = types.SimpleNamespace(login=self._test_login)
        self.auth = types.SimpleNamespace(AuthHandler=_FakeAuthHandler, OAuthToken=_FakeOAuthToken)

    def set_keys(self, *, api_key, api_secret):
        self.keys = (api_key, api_secret)

    def set_timeout(self, timeout_seconds):
        self.timeout = timeout_seconds

    def set_auth_handler(self, handler):
        self.auth_handler = handler

    def _test_login(self):
        # return some dict like real API
        return {"user": {"username": {"_content": "testuser"}}}

    def upload(self, **kwargs):
        self.upload_calls.append(kwargs)
        if self._upload_side_effects:
            eff = self._upload_side_effects.pop(0)
            if isinstance(eff, BaseException):
                raise eff
            return eff
        # Default: return object with id attr
        return types.SimpleNamespace(id="999")


class _ReadTimeout(Exception):
    pass


class _ConnectionError(Exception):
    pass


@pytest.fixture(autouse=True)
def patch_settings_and_deps(monkeypatch):
    # Minimal settings
    settings.AWS_REGION = "us-east-1"
    settings.FLICKR_SECRET_NAME = "FAKE-SECRET"
    settings.FLICKR_UPLOAD_TIMEOUT_SECONDS = 5
    settings.FLICKR_UPLOAD_MAX_RETRIES = 3
    settings.FLICKR_UPLOAD_BACKOFF_SECONDS = 0.0  # keep tests fast

    # Fake Secrets Manager content
    secret = {
        "api_key": "k",
        "api_secret": "s",
        "oauth_token": "tok",
        "oauth_token_secret": "toksec",
        "user_nsid": "123@N00",
        "username": "demo",
    }
    fake_sm = _FakeSecretsManager(secret)
    fake_boto3 = _FakeBoto3(fake_sm)
    monkeypatch.setattr(flickr_mod, "boto3", fake_boto3, raising=True)

    # Fake flickr_api module
    fake_api = _FakeFlickrAPI()
    monkeypatch.setattr(flickr_mod, "flickr_api", fake_api, raising=True)

    # Control retry exception type recognition
    # Provide a "requests" shim with exceptions carrying our fake classes
    shim = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(ReadTimeout=_ReadTimeout, ConnectionError=_ConnectionError)
    )
    monkeypatch.setattr(flickr_mod, "requests", shim, raising=True)

    return {"fake_api": fake_api, "fake_sm": fake_sm}


def test_init_configures_keys_auth_and_timeout(patch_settings_and_deps):
    fake_api = patch_settings_and_deps["fake_api"]

    c = FlickrAPIClient()

    # keys configured
    assert fake_api.keys == ("k", "s")
    # timeout wired from settings
    assert fake_api.timeout == settings.FLICKR_UPLOAD_TIMEOUT_SECONDS
    # auth handler set with OAuth token
    assert fake_api.auth_handler is not None
    assert getattr(fake_api.auth_handler, "access_token", None) is not None
    assert getattr(fake_api.auth_handler, "user_nsid", None) == "123@N00"
    assert getattr(fake_api.auth_handler, "username", None) == "demo"


def test_verify_auth_invokes_test_login(patch_settings_and_deps):
    fake_api = patch_settings_and_deps["fake_api"]
    c = FlickrAPIClient()

    out = c.verify_auth()
    assert out == {"user": {"username": {"_content": "testuser"}}}
    # Sanity: still has the handler
    assert fake_api.auth_handler is not None


@pytest.mark.parametrize(
    "inp,expected",
    [
        (None, None),
        ([], None),
        (["sunset", "sky"], "sunset sky"),
        (["new york", "golden hour"], '"new york" "golden hour"'),
        (["sunset, sky"], "sunset sky"),
        (["  spaced  ", " multiple words "], "spaced \"multiple words\""),
        (["alpha", "alpha", "beta"], "alpha beta"),  # dedupe
        (["he\"said", "two words"], "he\\\"said \"two words\""),
    ],
)
def test_tags_to_string_formatting(inp, expected):
    got = FlickrAPIClient._tags_to_string(inp)
    assert got == expected


def test_extract_photo_id_variants():
    class Obj:
        def __init__(self, pid):
            self.id = pid

    assert FlickrAPIClient._extract_photo_id(Obj(123)) == "123"
    assert FlickrAPIClient._extract_photo_id({"id": 42}) == "42"
    assert FlickrAPIClient._extract_photo_id({"photoid": "77"}) == "77"
    assert FlickrAPIClient._extract_photo_id({"photo_id": 88}) == "88"
    assert FlickrAPIClient._extract_photo_id("321") == "321"
    assert FlickrAPIClient._extract_photo_id(654) == "654"

    with pytest.raises(RuntimeError):
        FlickrAPIClient._extract_photo_id(None)

    with pytest.raises(RuntimeError):
        FlickrAPIClient._extract_photo_id({"unexpected": "x"})


@pytest.mark.parametrize(
    "val,expected",
    [
        (None, None),
        (True, "1"),
        (False, "0"),
        (1, "1"),
        (0, "0"),
        ("1", "1"),
        ("0", "0"),
        ("not-int", "0"),
    ],
)
def test_to_flag(val, expected):
    assert FlickrAPIClient._to_flag(val) == expected


def test_upload_file_builds_kwargs_and_calls_upload(monkeypatch, patch_settings_and_deps, tmp_path):
    fake_api = patch_settings_and_deps["fake_api"]
    c = FlickrAPIClient()

    # Ensure file exists
    p = tmp_path / "img.jpg"
    p.write_bytes(b"abc")
    monkeypatch.setattr(os.path, "exists", lambda path: True, raising=True)

    # Make upload return object with id
    def make_resp():
        return types.SimpleNamespace(id="123")
    fake_api._upload_side_effects = [make_resp()]

    pid = c.upload_file(
        str(p),
        title="T",
        description="D",
        tags=["new york", "sunset, sky", "alpha", "alpha"],
        is_public=True,
        is_friend=0,
        is_family=False,
        safety_level=2,
        content_type=1,
        hidden=1,
    )

    assert pid == "123"
    assert len(fake_api.upload_calls) == 1
    kwargs = fake_api.upload_calls[0]

    # File path
    assert kwargs["photo_file"] == str(p)
    # Tag string: quotes for multi-word, comma split, deduped
    assert kwargs["tags"] == '"new york" sunset sky alpha'
    # Flags coerced to strings and preserved "0"
    assert kwargs["is_public"] == "1"
    assert kwargs["is_friend"] == "0"
    assert kwargs["is_family"] == "0"
    # Additional ints
    assert kwargs["safety_level"] == 2
    assert kwargs["content_type"] == 1
    assert kwargs["hidden"] == 1
    # Title/description
    assert kwargs["title"] == "T"
    assert kwargs["description"] == "D"


def test_upload_retries_on_timeout_then_succeeds(monkeypatch, patch_settings_and_deps):
    fake_api = patch_settings_and_deps["fake_api"]
    c = FlickrAPIClient()

    # Simulate two timeouts then success
    fake_api._upload_side_effects = [_ReadTimeout("t1"), _ReadTimeout("t2"), types.SimpleNamespace(id="777")]

    pid = c.upload_file(__file__, title=None, description=None, tags=None)
    assert pid == "777"
    assert len(fake_api.upload_calls) == 3  # 2 retries + 1 success


def test_upload_retries_exhaust_then_raises(monkeypatch, patch_settings_and_deps):
    fake_api = patch_settings_and_deps["fake_api"]
    c = FlickrAPIClient()

    # 3 attempts total -> all timeouts
    fake_api._upload_side_effects = [_ReadTimeout("a"), _ReadTimeout("b"), _ReadTimeout("c")]

    with pytest.raises(RuntimeError) as ei:
        c.upload_file(__file__, title=None, description=None, tags=None)

    # The message should indicate retries exhausted
    assert "failed after" in str(ei.value)


def test_upload_bytes_writes_temp_and_cleans(monkeypatch, patch_settings_and_deps):
    fake_api = patch_settings_and_deps["fake_api"]
    c = FlickrAPIClient()

    # Arrange upload to return id and capture path existence during call
    observed_exists = {"during_upload": False}
    real_upload = fake_api.upload

    def spy_upload(**kwargs):
        path = kwargs["photo_file"]
        observed_exists["during_upload"] = os.path.exists(path)
        return types.SimpleNamespace(id="9001")

    fake_api.upload = spy_upload

    # Spy unlink to ensure cleanup happens
    unlinked = {"path": None}
    real_unlink = os.unlink

    def spy_unlink(path):
        unlinked["path"] = path
        # actually unlink to avoid temp clutter if present
        try:
            return real_unlink(path)
        except FileNotFoundError:
            return None

    monkeypatch.setattr(os, "unlink", spy_unlink, raising=True)

    pid = c.upload_bytes(b"\x00\x01data", title="X")

    assert pid == "9001"
    assert observed_exists["during_upload"] is True
    assert unlinked["path"] is not None
    # After cleanup, the temp file should be gone
    assert not os.path.exists(unlinked["path"])

    # restore upload
    fake_api.upload = real_upload


def test_missing_secret_name_raises(monkeypatch):
    # Ensure missing secret name triggers configuration error
    settings.FLICKR_SECRET_NAME = None  # type: ignore

    with pytest.raises(RuntimeError):
        FlickrAPIClient()
