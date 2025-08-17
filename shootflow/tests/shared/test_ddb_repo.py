# Python
import json

import pytest

from shootflow.shared.ddb_repo import PhotoRepository


def test_put_photo_item_success_builds_item_and_calls_ddb(fake_ddb_factory):
    table_name = "test-table"
    region = "us-east-1"
    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(table_name=table_name, region_name=region, ddb_client=fake_ddb)

    photo_id = "abc123"
    s3_key = "ready/photo.jpg"
    filename = "photo.jpg"
    title = "My Title"
    caption = "My Caption"
    tags = ["t1", "t2"]
    gps = {"lat": 1.23, "lon": 4.56}
    exif = {"foo": "bar"}

    ok, err = repo.put_photo_item(
        photoId=photo_id,
        s3_key=s3_key,
        filename=filename,
        title=title,
        caption=caption,
        tags=tags,
        gps=gps,
        exif=exif,
    )

    assert ok is True and err is None
    assert len(fake_ddb.put_item_calls) == 1

    table, item, cond = fake_ddb.put_item_calls[0]
    assert table == table_name
    assert "attribute_not_exists(photoId)" in cond

    # Ensure old keys are gone
    assert "pk" not in item
    assert "sk" not in item

    # Static attributes
    assert item["photoId"]["S"] == photo_id
    assert item["status"]["S"] == "READY"
    assert item["s3_key"]["S"] == s3_key
    assert item["filename"]["S"] == filename
    assert "ingested_at" in item and "S" in item["ingested_at"] and item["ingested_at"]["S"]

    # Optional/complex attributes
    assert item["title"]["S"] == title
    assert item["caption"]["S"] == caption
    assert item["tags"]["L"] == [{"S": "t1"}, {"S": "t2"}]
    assert json.loads(item["gps"]["S"]) == gps
    assert json.loads(item["exif"]["S"]) == exif


def test_put_photo_item_duplicate_returns_flag(fake_ddb_factory):
    fake_ddb = fake_ddb_factory("duplicate")
    repo = PhotoRepository(table_name="tbl", region_name="us-east-1", ddb_client=fake_ddb)

    ok, err = repo.put_photo_item(
        photoId="id1",
        s3_key="ready/a.jpg",
        filename="a.jpg",
        title=None,
        caption=None,
        tags=[],
        gps=None,
        exif={},
    )

    assert ok is False
    assert err == "duplicate"


def test_put_photo_item_error_propagates_error_string(fake_ddb_factory):
    fake_ddb = fake_ddb_factory("error")
    repo = PhotoRepository(table_name="tbl", region_name="us-east-1", ddb_client=fake_ddb)

    ok, err = repo.put_photo_item(
        photoId="id2",
        s3_key="ready/b.jpg",
        filename="b.jpg",
        title=None,
        caption=None,
        tags=[],
        gps=None,
        exif={},
    )

    assert ok is False
    # Should include the original ClientError message
    assert isinstance(err, str) and ("InternalServerError" in err or "boom" in err)


def test_put_photo_item_omits_optional_fields_when_none_or_empty(fake_ddb_factory):
    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(table_name="tbl", region_name="us-east-1", ddb_client=fake_ddb)

    ok, err = repo.put_photo_item(
        photoId="id3",
        s3_key="ready/c.jpg",
        filename="c.jpg",
        title=None,
        caption=None,
        tags=[],        # empty
        gps=None,       # None
        exif={},        # exif is always present as JSON, even if empty
    )

    assert ok is True and err is None
    assert len(fake_ddb.put_item_calls) == 1
    _, item, _ = fake_ddb.put_item_calls[0]

    assert "title" not in item
    assert "caption" not in item
    assert "tags" not in item
    assert "gps" not in item
    # exif should still be present as serialized JSON
    assert "exif" in item and item["exif"]["S"] == json.dumps({})
