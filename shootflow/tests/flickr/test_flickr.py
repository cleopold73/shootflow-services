import os
import sys
import types
from unittest.mock import patch

import pytest
from datetime import datetime, timezone

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

    with pytest.raises(ReadTimeout):
        c.upload_file(__file__, title=None, description=None, tags=None)


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


# ---- iter_public_photos_date_range tests ----

def _install_person_stub(monkeypatch, pages_data):
    """
    Install a Person class into the existing flickr_api module whose getPhotos()
    returns a paginated iterable based on pages_data: {page_number: [item_dict, ...]}.
    Each item_dict supports keys: id, title, description, dateuploaded, tags (list[str|bytes]).
    """
    mod = sys.modules["flickr_api"]

    class _Info:
        def __init__(self, page, pages):
            self.page = page
            self.pages = pages

    class _Resp:
        def __init__(self, items, page, pages):
            self._items = list(items)
            self.info = _Info(page, pages)

        def __iter__(self):
            return iter(self._items)

    class _Tag:
        def __init__(self, raw):
            self.raw = raw

    class _Item:
        def __init__(self, id, title, description, dateuploaded, tags):
            self.id = id
            self.title = title
            self.description = description
            self.dateuploaded = dateuploaded
            self._tags = [_Tag(t) for t in (tags or [])]

        def getTags(self):
            return list(self._tags)

    class Person:
        def __init__(self, id):
            self.id = id

        def getPhotos(self, **kwargs):
            page = int(kwargs.get("page", 1))
            pages = max(pages_data.keys()) if pages_data else 1
            raw_items = pages_data.get(page, [])
            items = [_Item(**it) for it in raw_items]
            return _Resp(items, page, pages)

    monkeypatch.setattr(mod, "Person", Person, raising=False)


def test_iter_public_no_user_nsid_yields_nothing(flickr_test_env, monkeypatch):
    # Install a person stub even though it shouldn't be called when NSID is missing
    _install_person_stub(monkeypatch, {1: []})

    c = FlickrAPIClient()
    # Force missing NSID for this test
    c._user_nsid = ""

    results = list(
        c.iter_public_photos_date_range(
            start_date=0, end_date=0, by="uploaded", per_page=10
        )
    )
    assert results == []


def test_iter_public_single_page_converts_datetime(flickr_test_env, monkeypatch):
    epoch = 1_620_000_000
    _install_person_stub(
        monkeypatch,
        {
            1: [
                {
                    "id": "123",
                    "title": "A Title",
                    "description": "Desc",
                    "dateuploaded": epoch,
                    "tags": [b"raw1", "raw two"],
                }
            ]
        },
    )

    c = FlickrAPIClient()
    out = list(
        c.iter_public_photos_date_range(
            start_date=0, end_date=0, by="uploaded", per_page=10
        )
    )
    assert len(out) == 1
    item = out[0]
    assert isinstance(item, flickr_mod.PhotoMetadata)
    assert item.id == "123"
    assert item.title == "A Title"
    assert item.description == "Desc"
    assert isinstance(item.tags, list)
    assert "raw1" in item.tags
    assert "raw two" in item.tags
    assert isinstance(item.date_uploaded, datetime)
    assert item.date_uploaded == datetime.fromtimestamp(epoch, tz=timezone.utc)


def test_iter_public_paginates_two_pages(flickr_test_env, monkeypatch):
    epoch1 = 1_620_000_000
    epoch2 = 1_630_000_000
    _install_person_stub(
        monkeypatch,
        {
            1: [
                {
                    "id": "p1",
                    "title": "First",
                    "description": "D1",
                    "dateuploaded": epoch1,
                    "tags": ["t1"],
                }
            ],
            2: [
                {
                    "id": "p2",
                    "title": "Second",
                    "description": "D2",
                    "dateuploaded": epoch2,
                    "tags": ["t2"],
                }
            ],
        },
    )

    c = FlickrAPIClient()
    got = list(
        c.iter_public_photos_date_range(
            start_date=0, end_date=0, by="uploaded", per_page=1
        )
    )
    assert [x.id for x in got] == ["p1", "p2"]
    assert got[0].date_uploaded == datetime.fromtimestamp(epoch1, tz=timezone.utc)
    assert got[1].date_uploaded == datetime.fromtimestamp(epoch2, tz=timezone.utc)


def test_get_download_url_retries_then_returns(flickr_test_env, monkeypatch):
    # Use fixture-provided exception classes to drive retry logic
    ReadTimeout = flickr_test_env["ReadTimeout"]
    ConnectionError = flickr_test_env["ConnectionError"]

    # Patch the module's requests reference so isinstance checks work
    exc_ns = types.SimpleNamespace(ReadTimeout=ReadTimeout, ConnectionError=ConnectionError)
    req_obj = types.SimpleNamespace(exceptions=exc_ns)
    monkeypatch.setattr(flickr_mod, "requests", req_obj, raising=False)

    # Stub flickr_api.Photo.getPhotoFile to fail once, then return URL
    import sys

    calls = {"n": 0}
    url = "https://live.staticflickr.com/path/original.jpg"

    class _Photo:
        def __init__(self, user_id, id):
            self.user_id = user_id
            self.id = id

        def getPhotoFile(self, size_label="Original"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ReadTimeout("rt")
            return url

    monkeypatch.setattr(sys.modules["flickr_api"], "Photo", _Photo, raising=False)

    c = FlickrAPIClient()
    got = c.get_download_url("fid-123")
    assert got == url
    assert calls["n"] == 2  # one retry


def test_get_download_url_nonretryable_error_bubbles(flickr_test_env, monkeypatch):
    # Patch requests for completeness (won't matter for generic Exception)
    ReadTimeout = flickr_test_env["ReadTimeout"]
    ConnectionError = flickr_test_env["ConnectionError"]

    exc_ns = types.SimpleNamespace(ReadTimeout=ReadTimeout, ConnectionError=ConnectionError)
    req_obj = types.SimpleNamespace(exceptions=exc_ns)
    monkeypatch.setattr(flickr_mod, "requests", req_obj, raising=False)

    # Stub flickr_api.Photo.getPhotoFile to raise a generic error (non-retryable)
    import sys

    class _Photo:
        def __init__(self, user_id, id):
            self.user_id = user_id
            self.id = id

        def getPhotoFile(self, size_label="Original"):
            raise RuntimeError("boom")

    monkeypatch.setattr(sys.modules["flickr_api"], "Photo", _Photo, raising=False)

    c = FlickrAPIClient()
    with pytest.raises(RuntimeError):
        c.get_download_url("fid-err")


# ---- get_photo_score tests ----

def _install_photo_stub(monkeypatch, data_by_id):
    """
    Install a flickr_api.Photo stub that returns favorites/comments/info from data_by_id.
    data_by_id: {photo_id: {"favorites": int, "comments": int, "views": int, "dateuploaded": int}}
    """
    mod = sys.modules["flickr_api"]
    import types

    class Photo:
        def __init__(self, user_id, id):
            self._data = data_by_id[str(id)]

        def getFavorites(self):
            return types.SimpleNamespace(info=types.SimpleNamespace(total=int(self._data.get("favorites", 0))))

        def getComments(self):
            return [None] * int(self._data.get("comments", 0))

        def getInfo(self):
            return {
                "views": int(self._data.get("views", 0)),
                "dateuploaded": int(self._data.get("dateuploaded", 0)),
            }

    monkeypatch.setattr(mod, "Photo", Photo, raising=False)


def _freeze_now(monkeypatch, fixed_now):
    """
    Freeze flickr module's datetime.now to a fixed timezone-aware datetime.
    """
    # Keep original module-level datetime for fromtimestamp
    orig_datetime = flickr_mod.datetime

    class _DT:
        @staticmethod
        def fromtimestamp(ts, tz=None):
            return orig_datetime.fromtimestamp(ts, tz=tz)

        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(flickr_mod, "datetime", _DT, raising=False)


def test_get_photo_score_prefers_recency(flickr_test_env, monkeypatch):
    # Configure scoring hyperparameters explicitly for deterministic behavior
    settings.FLICKR_SCORE_W_FAV = 3.0
    settings.FLICKR_SCORE_W_COM = 4.0
    settings.FLICKR_SCORE_W_VIEW = 1.0
    settings.FLICKR_SCORE_BAYES_ALPHA = 5.0
    settings.FLICKR_SCORE_BAYES_BETA = 200.0
    settings.FLICKR_SCORE_HALF_LIFE_HOURS = 48.0

    # Freeze "now"
    fixed_now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, fixed_now)

    # Prepare two photos with identical counts, different ages
    p_new = "n1"
    p_old = "o1"
    one_hour_ago = int((fixed_now.timestamp()) - 1 * 3600)
    seventy_two_hours_ago = int((fixed_now.timestamp()) - 72 * 3600)

    _install_photo_stub(
        monkeypatch,
        {
            p_new: {"favorites": 10, "comments": 3, "views": 100, "dateuploaded": one_hour_ago},
            p_old: {"favorites": 10, "comments": 3, "views": 100, "dateuploaded": seventy_two_hours_ago},
        },
    )

    c = FlickrAPIClient()
    s_new = c.get_photo_score(p_new)
    s_old = c.get_photo_score(p_old)

    assert s_new > s_old  # newer should score higher due to decay


def test_get_photo_score_engagement_influence(flickr_test_env, monkeypatch):
    # Keep decay stable by freezing time; both photos same age
    fixed_now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, fixed_now)

    # Hyperparameters
    settings.FLICKR_SCORE_W_FAV = 3.0
    settings.FLICKR_SCORE_W_COM = 4.0
    settings.FLICKR_SCORE_W_VIEW = 1.0
    settings.FLICKR_SCORE_BAYES_ALPHA = 5.0
    settings.FLICKR_SCORE_BAYES_BETA = 200.0
    settings.FLICKR_SCORE_HALF_LIFE_HOURS = 72.0

    epoch_same = int(fixed_now.timestamp() - 6 * 3600)
    pid_lo = "lo"
    pid_hi = "hi"

    # Same views, different engagement
    _install_photo_stub(
        monkeypatch,
        {
            pid_lo: {"favorites": 2, "comments": 0, "views": 200, "dateuploaded": epoch_same},
            pid_hi: {"favorites": 20, "comments": 10, "views": 200, "dateuploaded": epoch_same},
        },
    )

    c = FlickrAPIClient()
    score_lo = c.get_photo_score(pid_lo)
    score_hi = c.get_photo_score(pid_hi)

    assert score_hi > score_lo  # better engagement should result in a higher score


def test_get_photo_score_views_sublinear(flickr_test_env, monkeypatch):
    fixed_now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, fixed_now)

    # Hyperparameters
    settings.FLICKR_SCORE_W_FAV = 3.0
    settings.FLICKR_SCORE_W_COM = 4.0
    settings.FLICKR_SCORE_W_VIEW = 1.0
    settings.FLICKR_SCORE_BAYES_ALPHA = 5.0
    settings.FLICKR_SCORE_BAYES_BETA = 200.0
    settings.FLICKR_SCORE_HALF_LIFE_HOURS = 72.0

    epoch_same = int(fixed_now.timestamp() - 12 * 3600)
    pid_a = "a"
    pid_b = "b"

    # Same engagement, views differ by 10x
    _install_photo_stub(
        monkeypatch,
        {
            pid_a: {"favorites": 10, "comments": 5, "views": 100, "dateuploaded": epoch_same},
            pid_b: {"favorites": 10, "comments": 5, "views": 1000, "dateuploaded": epoch_same},
        },
    )

    c = FlickrAPIClient()
    score_a = c.get_photo_score(pid_a)
    score_b = c.get_photo_score(pid_b)

    # More views should increase score, but sublinearly (far less than 10x)
    assert score_b > score_a
    assert (score_b / max(score_a, 1e-9)) < 4.0
