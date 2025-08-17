# Python
import json
from typing import Any, Dict

import pytest
from botocore.exceptions import ClientError

from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.shared.config import settings


def test_put_photo_item_success_builds_item_and_calls_ddb(fake_ddb_factory):
    table_name = "test-table"
    region = "us-east-1"
    settings.DDB_TABLE = table_name
    settings.AWS_REGION = region
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

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
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("duplicate")
    repo = PhotoRepository(ddb_client=fake_ddb)

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
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("error")
    repo = PhotoRepository(ddb_client=fake_ddb)

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
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    ok, err = repo.put_photo_item(
        photoId="id3",
        s3_key="ready/c.jpg",
        filename="c.jpg",
        title=None,
        caption=None,
        tags=[],  # empty
        gps=None,  # None
        exif={},  # exif is always present as JSON, even if empty
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


# -------- Tests for get_random_ready_photo_via_gsi and mark_posted using shared FakeDDB --------

def _ddb_item(photoId: str, status: str = "READY") -> Dict[str, Any]:
    return {
        "photoId": {"S": photoId},
        "status": {"S": status},
        "s3_key": {"S": f"ready/{photoId}.jpg"},
        "filename": {"S": f"{photoId}.jpg"},
    }


def test_get_random_ready_photo_via_gsi_first_window(fake_ddb_factory):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    # First query returns one item
    fake_ddb.query_responses = [[_ddb_item("p1")]]
    repo = PhotoRepository(ddb_client=fake_ddb)

    got = repo.get_random_ready_photo_via_gsi()
    assert got is not None
    assert got["photoId"] == "p1"
    assert len(fake_ddb.query_calls) == 1
    qc = fake_ddb.query_calls[0]
    assert qc["IndexName"] == "status-photoId-gsi"
    assert "#st" in qc["ExpressionAttributeNames"] and qc["ExpressionAttributeNames"]["#st"] == "status"
    assert ":ready" in qc["ExpressionAttributeValues"] and qc["ExpressionAttributeValues"][":ready"]["S"] == "READY"


def test_get_random_ready_photo_via_gsi_wraparound(fake_ddb_factory):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    # First empty, second returns an item
    fake_ddb.query_responses = [[], [_ddb_item("p2")]]
    repo = PhotoRepository(ddb_client=fake_ddb)

    got = repo.get_random_ready_photo_via_gsi()
    assert got is not None and got["photoId"] == "p2"
    assert len(fake_ddb.query_calls) == 2
    second = fake_ddb.query_calls[1]
    assert second["ExpressionAttributeValues"][":min"]["S"] == "0"


def test_get_random_ready_photo_via_gsi_none_when_empty(fake_ddb_factory):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    fake_ddb.query_responses = [[], []]
    repo = PhotoRepository(ddb_client=fake_ddb)

    got = repo.get_random_ready_photo_via_gsi()
    assert got is None
    assert len(fake_ddb.query_calls) == 2


def test_get_random_ready_photo_via_gsi_bubbles_client_error(fake_ddb_factory):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    class ErrDdb(type(fake_ddb_factory("success"))):  # subclass the same FakeDDB type
        def query(self, **kwargs):
            raise ClientError(
                error_response={"Error": {"Code": "ValidationException", "Message": "bad index"}},
                operation_name="Query",
            )

    ddb = ErrDdb("success")
    repo = PhotoRepository(ddb_client=ddb)

    with pytest.raises(ClientError):
        repo.get_random_ready_photo_via_gsi()


def test_mark_posted_updates_status_and_sets_fields(fake_ddb_factory):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    repo.mark_posted("p123", flickr_photo_id="fl-999")

    assert len(fake_ddb.update_calls) == 1
    call = fake_ddb.update_calls[0]
    assert call["TableName"] == "tbl"
    assert call["Key"] == {"photoId": {"S": "p123"}}
    assert call["UpdateExpression"].startswith("SET ")
    names = call["ExpressionAttributeNames"]
    vals = call["ExpressionAttributeValues"]
    assert names == {"#st": "status"}
    assert vals[":posted"]["S"] == "POSTED"
    assert ":ts" in vals and "S" in vals[":ts"] and vals[":ts"]["S"]
    assert vals[":fid"]["S"] == "fl-999"
    assert call["ConditionExpression"] == "attribute_exists(photoId)"
