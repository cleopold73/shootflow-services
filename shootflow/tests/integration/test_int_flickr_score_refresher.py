# Python
from typing import Dict, List, Tuple

import pytest

import shootflow.handlers.flickr_score_refresher as refresher_mod
from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.shared.config import settings


def test_integration_flickr_score_refresher_scans_and_updates(fake_ddb_factory, monkeypatch):
    # Configure settings relevant to the handler
    settings.DDB_TABLE = "tbl"
    settings.AWS_REGION = "us-east-1"
    # Ensure non-dry-run so DB writes occur
    settings.FLICKR_SCORE_REFRESH_DRY_RUN = False

    # Real repository backed by fake DynamoDB
    fake_ddb = fake_ddb_factory("success")
    repo = PhotoRepository(ddb_client=fake_ddb)

    # Prepare a two-page scan response with (photoId, flickr_photo_id)
    page1 = {
        "Items": [
            {"photoId": {"S": "p1"}, "flickr_photo_id": {"S": "f1"}},
            {"photoId": {"S": "p2"}, "flickr_photo_id": {"S": "f2"}},
        ],
        "LastEvaluatedKey": {"photoId": {"S": "p2"}},
    }
    page2 = {
        "Items": [
            {"photoId": {"S": "p3"}, "flickr_photo_id": {"S": "f3"}},
            {"photoId": {"S": "p4"}, "flickr_photo_id": {"S": "f4"}},
        ]
    }
    scans: List[dict] = [page1, page2]
    state = {"i": 0}

    def scan(**kwargs):
        idx = state["i"]
        state["i"] += 1
        return scans[idx]

    # Patch scan on the fake client
    monkeypatch.setattr(fake_ddb, "scan", scan, raising=False)

    # Make handler use our repo instance
    class _RepoFactory:
        def __call__(self):
            return repo

    monkeypatch.setattr(refresher_mod, "PhotoRepository", _RepoFactory(), raising=False)

    # Minimal fake Flickr client that returns scores, with one failing ID
    scores = {"f1": 1.0, "f2": 2.0, "f3": 3.0, "f4": 4.0}
    fail_ids = {"f3"}

    class _Client:
        def get_photo_score(self, flickr_id: str) -> float:
            if flickr_id in fail_ids:
                raise RuntimeError("boom")
            return float(scores.get(flickr_id, 0.0))

    class _ClientFactory:
        def __call__(self):
            return _Client()

    monkeypatch.setattr(refresher_mod, "FlickrAPIClient", _ClientFactory(), raising=False)

    # Execute
    result = refresher_mod.handler({}, None)

    # Validate results
    assert result["processed"] == 4
    assert result["updated"] == 3  # one failed
    assert result["errors"] == 1
    assert result["dry_run"] is False

    # Ensure three updates written to fake DDB for p1, p2, p4
    assert len(fake_ddb.update_calls) == 3
    keys = [c["Key"]["photoId"]["S"] for c in fake_ddb.update_calls]
    assert keys == ["p1", "p2", "p4"]
    # Validate numeric values were passed as DynamoDB Numbers (string form)
    vals = [c["ExpressionAttributeValues"][":flickr_score"]["N"] for c in fake_ddb.update_calls]
    assert vals == ["1.0", "2.0", "4.0"]
