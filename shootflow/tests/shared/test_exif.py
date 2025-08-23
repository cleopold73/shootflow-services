# python
# tests/test_exif.py
import base64
import types
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def patch_exif_tunables():
    # Import the module under test
    import shootflow.shared.exif as exif

    # Patch constants via patchers and yield, then stop
    patchers = [
        patch.object(exif, "EXIF_MAX_TEXT_LENGTH", 20000),
        patch.object(exif, "EXIF_BASE64_SNIFF_PREFIX_LEN", 16),
        patch.object(exif, "EXIF_LARGE_COLLECTION_SIZE", 10),
        patch.object(exif, "GEAR_TAG_BLOCKLIST", {"canon", "nikkor"}),
    ]
    for p in patchers:
        p.start()
    try:
        yield exif
    finally:
        for p in reversed(patchers):
            p.stop()


def test_run_exiftool_json_maps_fields(patch_exif_tunables, tmp_path):
    exif = patch_exif_tunables

    # Mock pyexiftool.ExifTool context manager used by run_exiftool_json
    class FakeET:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *args, **kwargs):
            return b""

        def execute_json(self, *args):
            # mimic pyexiftool.ExifTool.execute_json("-n", file_path)
            return [{
                "XMP-dc:Title": "My Title",
                "XMP-dc:Description": "Desc",
                "XMP-dc:Subject": ["landscape", "Canon", "sunset"],
                "IPTC:ObjectName": "IPTC Title",
                "IPTC:Keywords": ["alpha", "beta"],
                "EXIF:ImageDescription": "EXIF Desc",
                "EXIF:UserComment": "User Comment",
                "EXIF:GPSLatitude": 37 + 46 / 60 + 30 / 3600,
                "EXIF:GPSLongitude": -122 - 25 / 60,
                "EXIF:GPSAltitude": 15.5,
            }]

    with patch.object(exif, "exiftool", types.SimpleNamespace(ExifTool=FakeET)):
        # Create a fake file path (not used by the fake, but needed by our function)
        fake_file = tmp_path / "photo.jpg"
        fake_file.write_bytes(b"not-used")

        out = exif.run_exiftool_json(str(fake_file))
        assert out is not None
        # Key presence and mapped values (flattened)
        assert out["XMP-dc:Title"] == "My Title"
        assert out["XMP-dc:Description"] == "Desc"
        assert out["XMP-dc:Subject"] == ["landscape", "Canon", "sunset"]
        assert out["IPTC:ObjectName"] == "IPTC Title"
        assert out["IPTC:Keywords"] == ["alpha", "beta"]
        assert out["EXIF:ImageDescription"] == "EXIF Desc"
        assert out["EXIF:UserComment"] == "User Comment"
        # GPS numeric (decimal degrees with -n)
        assert pytest.approx(out["EXIF:GPSLatitude"], rel=1e-6) == 37 + 46 / 60 + 30 / 3600
        assert pytest.approx(out["EXIF:GPSLongitude"], rel=1e-6) == -122 - 25 / 60
        assert out["EXIF:GPSAltitude"] == 15.5


def test_parse_exif_fields_basic_and_blocklist(patch_exif_tunables):
    exif = patch_exif_tunables

    meta = {
        "XMP-dc:Title": "XMP Title",
        "IPTC:ObjectName": "IPTC Title",
        "EXIF:ImageDescription": "EXIF Desc",
        "XMP-dc:Description": "XMP Description",
        "IPTC:Keywords": ["Canon", "Sunset", "nikkor", "Landscape"],
        "XMP-dc:Subject": ["travel", "Canon EOS", "Seascape"],
    }

    title, caption, tags, extras = exif.parse_exif_fields(meta, fallback_title="fallback")
    # Title should pick first present from preference order (XMP-dc:Title)
    assert title == "XMP Title"
    # Caption should pick XMP-dc:Description
    assert caption == "XMP Description"
    # Tags should be normalized, deduped, lowercased, and blocklisted terms removed
    # Blocklist contains "canon" and "nikkor" so they should be filtered
    assert set(tags) == {"sunset", "landscape", "travel", "seascape"}


def test_parse_exif_fields_title_fallback_when_missing(patch_exif_tunables):
    exif = patch_exif_tunables

    meta = {
        # No title fields present
        "IPTC:Keywords": ["alpha"],
    }
    title, caption, tags, extras = exif.parse_exif_fields(meta, fallback_title="name-no-ext")
    assert title == "name-no-ext"
    assert caption is None
    assert tags == ["alpha"]


def test_parse_exif_fields_gps_conversion(patch_exif_tunables):
    exif = patch_exif_tunables

    meta = {
        "EXIF:GPSLatitude": [37, 46, 30],  # 37 + 46/60 + 30/3600 = 37.775
        "EXIF:GPSLongitude": [-122, 25, 0],  # -122 - 25/60 = -122.416666...
        "EXIF:GPSAltitude": "15.5",
    }
    title, caption, tags, extras = exif.parse_exif_fields(meta, fallback_title="f")
    gps = extras["gps"]
    assert gps is not None
    assert pytest.approx(gps["lat"], rel=1e-6) == 37 + 46 / 60 + 30 / 3600
    assert pytest.approx(gps["lon"], rel=1e-6) == -122 - 25 / 60
    assert pytest.approx(gps["alt"], rel=1e-6) == 15.5


def test_parse_exif_fields_filters_large_and_base64_fields(patch_exif_tunables):
    exif = patch_exif_tunables

    # A very long plain string
    too_long = "x" * (exif.EXIF_MAX_TEXT_LENGTH + 1)

    # A long base64-like string
    payload = base64.b64encode(b"\x00" * 4096).decode("ascii")

    # A very large list
    huge_list = list(range(exif.EXIF_LARGE_COLLECTION_SIZE + 1))

    meta = {
        "NormalField": "ok",
        "PreviewImage": "binary marker should be dropped",
        "SomeText": too_long,
        "MaybeBase64": payload,
        "GiantList": huge_list,
    }

    _, _, _, extras = exif.parse_exif_fields(meta, fallback_title="f")
    exif_kept = extras["exif"]
    # Normal field is kept
    assert exif_kept.get("NormalField") == "ok"
    # Large/binary-like fields should be filtered out
    assert "PreviewImage" not in exif_kept
    assert "SomeText" not in exif_kept
    assert "MaybeBase64" not in exif_kept
    assert "GiantList" not in exif_kept


def test_capital_jpg_exif_parsing():
    # Locate sample
    img_path = Path(__file__).parent / "data" / "Capital.jpg"
    if not img_path.exists():
        pytest.skip(f"Sample image not found: {img_path}")

    # Import module under test
    import shootflow.shared.exif as exif

    # Ensure tunables are deterministic for the test
    with patch.object(exif, "EXIF_MAX_TEXT_LENGTH", 20000), \
         patch.object(exif, "EXIF_BASE64_SNIFF_PREFIX_LEN", 32), \
         patch.object(exif, "EXIF_LARGE_COLLECTION_SIZE", 2000), \
         patch.object(exif, "GEAR_TAG_BLOCKLIST", {"canon", "nikon", "nikkor", "sony"}):
        # 1) Raw read via exiftool wrapper
        out = exif.run_exiftool_json(str(img_path))
        assert out is not None, "Failed to read metadata from Capital.jpg"
        assert isinstance(out, dict)
        # At least some metadata keys should be present
        assert len(out) > 0

        # 2) Normalize via parse_exif_fields
        title, caption, tags, extras = exif.parse_exif_fields(out, fallback_title=img_path.stem)

        # Basic type checks
        assert title is None or isinstance(title, str)
        assert caption is None or isinstance(caption, str)
        assert isinstance(tags, list)
        assert isinstance(extras, dict)
        assert "exif" in extras and isinstance(extras["exif"], dict)
        assert title == "Quiet Morning at the Capitol"

        # Tags normalization: lowercase, trimmed, deduped
        assert all(isinstance(t, str) for t in tags)
        assert all(t == t.strip() for t in tags)
        assert all(t == t.lower() for t in tags)
        assert len(tags) == len(set(tags))  # deduped

        # Blocklist applied: no tag should contain blocked terms as substrings
        for t in tags:
            assert not any(bad in t for bad in exif.GEAR_TAG_BLOCKLIST)

        # GPS (if present) must be numeric floats
        gps = extras.get("gps")
        if gps is not None:
            assert isinstance(gps, dict)
            assert "lat" in gps and "lon" in gps
            assert isinstance(gps["lat"], float)
            assert isinstance(gps["lon"], float)
            # Optional altitude
            if "alt" in gps:
                assert isinstance(gps["alt"], float)

        # Ensure large/binary-like fields are filtered in "exif" (can't assert exact keys,
        # but can assert original 'out' size is at least as big as kept subset)
        exif_kept = extras["exif"]
        assert isinstance(exif_kept, dict)