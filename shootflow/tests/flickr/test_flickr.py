import os
import types
from unittest.mock import patch

import pytest

from shootflow.flickr import FlickrAPIClient
import shootflow.flickr.flickr as flickr_mod
from shootflow.shared.config import settings


@pytest.fixture(autouse=True)
def patch_settings_defaults():
    # Base defaults; flickr_test_env will refine as needed
    settings.AWS_REGION = "us-east-1"
    settings.FLICKR_SECRET_NAME = "FAKE-SECRET"
    settings.FLICKR_UPLOAD_TIMEOUT_SECONDS = 5
    settings.FLICKR_UPLOAD_MAX_RETRIES = 3
    settings.FLICKR_UPLOAD_BACKOFF_SECONDS = 0.0
    yield


def test_init_configures_keys_auth_and_timeout(flickr_test_env):
    fake_api = flickr_test_env["fake_api"]

    c = FlickrAPIClient()

    # keys configured
    assert fake_api.keys == ("k", "s")
    # timeout wired from settings
    assert fake_api.timeout == settings.FLICKR_UPLOAD_TIMEOUT_SECONDS
    # auth handler set with OAuth token + optional fields
    assert fake_api.auth_handler is not None
    assert getattr(fake_api.auth_handler, "access_token", None) is not None
    assert getattr(fake_api.auth_handler, "user_nsid", None) == "123@N00"
    assert getattr(fake_api.auth_handler, "username", None) == "demo"


def test_verify_auth_invokes_test_login(flickr_test_env):
    fake_api = flickr_test_env["fake_api"]
    c = FlickrAPIClient()

    out = c.verify_auth()
    assert out == {"user": {"username": {"_content": "testuser"}}}
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
        (['he"said', "two words"], 'he\\\"said "two words"'),
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


@patch("os.path.exists", return_value=True)
def test_upload_file_builds_kwargs_and_calls_upload(exists_mock, flickr_test_env, tmp_path):
    fake_api = flickr_test_env["fake_api"]
    c = FlickrAPIClient()

    # Ensure file exists
    p = tmp_path / "img.jpg"
    p.write_bytes(b"abc")

    # Make upload return object with id
    fake_api._upload_side_effects = [types.SimpleNamespace(id="123")]

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
    assert kwargs["photo_file"] == str(p)
    assert kwargs["tags"] == '"new york" sunset sky alpha'
    assert kwargs["is_public"] == "1"
    assert kwargs["is_friend"] == "0"
    assert kwargs["is_family"] == "0"
    assert kwargs["safety_level"] == 2
    assert kwargs["content_type"] == 1
    assert kwargs["hidden"] == 1
    assert kwargs["title"] == "T"
    assert kwargs["description"] == "D"


def test_upload_retries_on_timeout_then_succeeds(flickr_test_env):
    fake_api = flickr_test_env["fake_api"]
    ReadTimeout = flickr_test_env["ReadTimeout"]
    c = FlickrAPIClient()

    # Two timeouts then success
    fake_api._upload_side_effects = [ReadTimeout("t1"), ReadTimeout("t2"), types.SimpleNamespace(id="777")]

    pid = c.upload_file(__file__, title=None, description=None, tags=None)
    assert pid == "777"
    assert len(fake_api.upload_calls) == 3


def test_upload_retries_exhaust_then_raises(flickr_test_env):
    fake_api = flickr_test_env["fake_api"]
    ReadTimeout = flickr_test_env["ReadTimeout"]
    c = FlickrAPIClient()

    # All attempts timeout
    fake_api._upload_side_effects = [ReadTimeout("a"), ReadTimeout("b"), ReadTimeout("c")]

    with pytest.raises(RuntimeError) as ei:
        c.upload_file(__file__, title=None, description=None, tags=None)

    assert "failed after" in str(ei.value)


@patch("os.unlink")
def test_upload_bytes_writes_temp_and_cleans(unlink_mock, flickr_test_env):
    fake_api = flickr_test_env["fake_api"]
    c = FlickrAPIClient()

    observed_exists = {"during_upload": False}
    real_upload = fake_api.upload

    def spy_upload(**kwargs):
        path = kwargs["photo_file"]
        observed_exists["during_upload"] = os.path.exists(path)
        return types.SimpleNamespace(id="9001")

    fake_api.upload = spy_upload

    pid = c.upload_bytes(b"\x00\x01data", title="X")

    assert pid == "9001"
    assert observed_exists["during_upload"] is True
    # Ensure cleanup attempted
    assert unlink_mock.called

    fake_api.upload = real_upload


def test_missing_secret_name_raises(monkeypatch):
    settings.FLICKR_SECRET_NAME = None  # type: ignore
    with pytest.raises(RuntimeError):
        FlickrAPIClient()


def test_add_photo_to_group_success(flickr_test_env, monkeypatch):
    # Stub the objects API Group to capture addPhoto calls
    import sys
    import types

    calls = []

    class _Group:
        def __init__(self, id):
            self.id = id

        def getInfo(self):
            # Minimal shape; implementation only needs this to not fail if invoked
            return {"group": {"id": self.id}}

        def addPhoto(self, *, photo_id):
            calls.append((photo_id, self.id))

    # Inject our stub into the import system
    objects_mod = types.ModuleType("flickr_api.objects")
    objects_mod.Group = _Group  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flickr_api.objects", objects_mod)

    c = FlickrAPIClient()
    c.add_photo_to_group("p123", "g999")

    # Verify the instance method path was used
    assert calls == [("p123", "g999")]


def test_add_photo_to_group_pending_queue_treated_as_success(flickr_test_env, monkeypatch):
    import sys
    import types

    class _Group:
        def __init__(self, id):
            self.id = id

        def addPhoto(self, *, photo_id):
            # Simulate Flickr's moderation queue message
            raise Exception("6 : Your Photo has been added to the Pending Queue for this Pool")

    # Inject our stub into the import system
    objects_mod = types.ModuleType("flickr_api.objects")
    objects_mod.Group = _Group  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flickr_api.objects", objects_mod)

    c = FlickrAPIClient()
    # Should be treated as success (no exception raised)
    c.add_photo_to_group("p2", "g2")


def test_add_photo_to_group_invalid_args(flickr_test_env):
    c = FlickrAPIClient()
    with pytest.raises(ValueError):
        c.add_photo_to_group("", "g1")
    with pytest.raises(ValueError):
        c.add_photo_to_group("p1", "")


def test_add_photo_to_group_failure_logs_no_raise(flickr_test_env, monkeypatch):
    import sys
    import types

    class _Group:
        def __init__(self, id):
            self.id = id

        def addPhoto(self, *, photo_id):
            # Simulate a generic failure from the API
            raise Exception("boom")

    # Inject our stub into the import system
    objects_mod = types.ModuleType("flickr_api.objects")
    objects_mod.Group = _Group  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flickr_api.objects", objects_mod)

    c = FlickrAPIClient()
    # Should not raise; failure is logged and swallowed
    c.add_photo_to_group("p2", "g2")
