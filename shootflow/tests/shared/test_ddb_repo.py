# Python
import json
from typing import Any, Dict
from datetime import datetime, timezone

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


def test_update_flickr_score_updates_number_attribute(fake_ddb_factory):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    repo.update_flickr_score("pid-1", 123.456)

    assert len(fake_ddb.update_calls) == 1
    call = fake_ddb.update_calls[0]
    assert call["TableName"] == "tbl"
    assert call["Key"] == {"photoId": {"S": "pid-1"}}
    assert call["UpdateExpression"] == "SET flickr_score = :flickr_score"
    assert call["ConditionExpression"] == "attribute_exists(photoId)"

    vals = call["ExpressionAttributeValues"]
    assert ":flickr_score" in vals
    # DynamoDB Number values must be strings
    nval = vals[":flickr_score"]["N"]
    assert isinstance(nval, str)
    assert pytest.approx(float(nval), rel=0, abs=1e-12) == 123.456


def test_iter_all_flickr_photo_ids_scans_with_pagination(fake_ddb_factory, monkeypatch):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    scan_calls = []
    responses = [
        {
            "Items": [
                {"photoId": {"S": "p1"}, "flickr_photo_id": {"S": "fl1"}},
                {"photoId": {"S": "p2"}, "flickr_photo_id": {"S": "fl2"}},
                {"other": {"S": "ignore"}},
            ],
            "LastEvaluatedKey": {"photoId": {"S": "p2"}},
        },
        {
            "Items": [
                {"photoId": {"S": "p3"}, "flickr_photo_id": {"S": "fl3"}},
            ]
        },
    ]
    state = {"i": 0}

    def scan(**kwargs):
        scan_calls.append(kwargs)
        idx = state["i"]
        state["i"] += 1
        return responses[idx]

    # Attach scan to fake ddb instance
    monkeypatch.setattr(fake_ddb, "scan", scan, raising=False)

    repo = PhotoRepository(ddb_client=fake_ddb)
    got = list(repo.iter_all_flickr_photo_ids())

    assert got == [("p1", "fl1"), ("p2", "fl2"), ("p3", "fl3")]
    assert len(scan_calls) == 2
    assert scan_calls[0]["TableName"] == "tbl"
    assert scan_calls[0]["ProjectionExpression"] == "photoId, flickr_photo_id"
    assert "ExclusiveStartKey" not in scan_calls[0]
    # Subsequent page should include ExclusiveStartKey
    assert "ExclusiveStartKey" in scan_calls[1]


def test_iter_all_flickr_photo_ids_empty(fake_ddb_factory, monkeypatch):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")

    def scan(**kwargs):
        return {"Items": []}

    monkeypatch.setattr(fake_ddb, "scan", scan, raising=False)

    repo = PhotoRepository(ddb_client=fake_ddb)
    got = list(repo.iter_all_flickr_photo_ids())
    assert got == []


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
    assert ":posted_time" in vals and "S" in vals[":posted_time"] and vals[":posted_time"]["S"]
    assert vals[":flickr_id"]["S"] == "fl-999"
    assert call["ConditionExpression"] == "attribute_exists(photoId)"


def test_put_photo_item_can_set_posted_and_optional_fields(fake_ddb_factory):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    ts = datetime(2021, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    ok, err = repo.put_photo_item(
        photoId="id-posted",
        s3_key="ready/p.jpg",
        filename="p.jpg",
        title=None,
        caption=None,
        tags=[],
        gps=None,
        exif={},
        status="POSTED",
        posted_time=ts,
        flickr_id="fl-123",
    )

    assert ok is True and err is None
    assert len(fake_ddb.put_item_calls) == 1
    _, item, _ = fake_ddb.put_item_calls[0]

    # Status should be POSTED when posted=True
    assert item["status"]["S"] == "POSTED"
    # Optional fields included when provided
    assert item["posted_at"]["S"] == ts.isoformat()
    assert item["flickr_photo_id"]["S"] == "fl-123"


def test_put_photo_item_omits_posted_fields_when_not_provided(fake_ddb_factory):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    settings.S3_INGEST_STATUS_READY = "READY"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    ok, err = repo.put_photo_item(
        photoId="id-no-post",
        s3_key="ready/x.jpg",
        filename="x.jpg",
        title=None,
        caption=None,
        tags=[],
        gps=None,
        exif={},
        # posted defaults to None -> status READY
    )

    assert ok is True and err is None
    _, item, _ = fake_ddb.put_item_calls[0]
    assert item["status"]["S"] == "READY"
    assert "posted_at" not in item
    assert "flickr_photo_id" not in item


def test_get_photo_with_highest_flickr_score_returns_best(fake_ddb_factory, monkeypatch):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    # Two-page scan: includes some items without scores and one with the max
    page1 = {
        "Items": [
            {"photoId": {"S": "p1"}, "flickr_photo_id": {"S": "fl1"}, "flickr_score": {"N": "10.5"}},
            {"photoId": {"S": "p2"}, "flickr_photo_id": {"S": "fl2"}},  # missing score
        ],
        "LastEvaluatedKey": {"photoId": {"S": "p2"}},
    }
    page2 = {
        "Items": [
            {"photoId": {"S": "p3"}, "flickr_photo_id": {"S": "fl3"}, "flickr_score": {"N": "99.9"}},
            {"photoId": {"S": "p4"}, "flickr_photo_id": {"S": "fl4"}, "flickr_score": {"N": "42"}},
        ]
    }
    scans = [page1, page2]
    state = {"i": 0}

    def scan(**kwargs):
        idx = state["i"]
        state["i"] += 1
        return scans[idx]

    monkeypatch.setattr(fake_ddb, "scan", scan, raising=False)

    got = repo.get_photo_with_highest_flickr_score()
    assert got == ("p3", "fl3")


def test_get_photo_with_highest_flickr_score_none_when_no_scores(fake_ddb_factory, monkeypatch):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    # Only items without proper scores or ids
    page = {"Items": [{"photoId": {"S": "p1"}}, {"flickr_photo_id": {"S": "fl2"}}]}
    monkeypatch.setattr(fake_ddb, "scan", lambda **kwargs: page, raising=False)

    got = repo.get_photo_with_highest_flickr_score()
    assert got is None


def test_get_photo_with_highest_flickr_score_skips_instagram_posted(fake_ddb_factory, monkeypatch):
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"

    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    # Highest score is p1 but it's marked instagram_posted=POSTED; should return p2
    page = {
        "Items": [
            {"photoId": {"S": "p1"}, "flickr_photo_id": {"S": "fl1"}, "flickr_score": {"N": "200.0"}, "instagram_posted": {"S": "POSTED"}},
            {"photoId": {"S": "p2"}, "flickr_photo_id": {"S": "fl2"}, "flickr_score": {"N": "150.0"}},
            {"photoId": {"S": "p3"}, "flickr_photo_id": {"S": "fl3"}, "flickr_score": {"N": "50.0"}, "instagram_posted": {"S": "DRAFT"}},
        ]
    }
    monkeypatch.setattr(fake_ddb, "scan", lambda **kwargs: page, raising=False)

    got = repo.get_photo_with_highest_flickr_score()
    assert got == ("p2", "fl2")
