# Python
from typing import Dict, List, Tuple

import pytest

import shootflow.handlers.flickr_score_refresher as refresher_mod
from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.shared.config import settings


def _wire_repo_with_fake_ddb(monkeypatch, fake_ddb, items: List[Tuple[str, str]]):
    """
    Create a real PhotoRepository backed by fake_ddb and patch the handler to use it.
    Also patch the iterator to yield provided items.
    """
    repo = PhotoRepository(ddb_client=fake_ddb)

    def _iter_ids(*, page_limit: int = 1000):
        for pair in items:
            yield pair

    # Replace factory in handler module to return our repo
    class _RepoFactory:
        def __call__(self):
            return repo

    monkeypatch.setattr(refresher_mod, "PhotoRepository", _RepoFactory(), raising=False)
    # Patch the iterator on this repo instance
    monkeypatch.setattr(repo, "iter_all_flickr_photo_ids", _iter_ids, raising=False)

    return repo


def _patch_client_scores(monkeypatch, scores: Dict[str, float], fail_ids: set[str] | None = None):
    """
    Patch FlickrAPIClient in the handler module to a minimal fake that returns scores.
    """
    class _Client:
        def get_photo_score(self, flickr_id: str) -> float:
            if fail_ids and flickr_id in fail_ids:
                raise RuntimeError("boom")
            return float(scores.get(flickr_id, 0.0))

    class _ClientFactory:
        def __call__(self):
            return _Client()

    monkeypatch.setattr(refresher_mod, "FlickrAPIClient", _ClientFactory(), raising=False)


def test_refresher_updates_scores(fake_ddb_factory, monkeypatch):
    fake_ddb = fake_ddb_factory("success")
    items = [("p1", "f1"), ("p2", "f2")]
    repo = _wire_repo_with_fake_ddb(monkeypatch, fake_ddb, items)
    _patch_client_scores(monkeypatch, {"f1": 1.23, "f2": 4.56})

    # Ensure not in dry-run mode
    settings.FLICKR_SCORE_REFRESH_DRY_RUN = False

    result = refresher_mod.handler({}, None)

    assert result["processed"] == 2
    assert result["updated"] == 2
    assert result["errors"] == 0
    # Verify writes went through the fake DDB client
    assert len(fake_ddb.update_calls) == 2
    # The calls are dicts; ensure the keys match expected ids
    keys = [c["Key"]["photoId"]["S"] for c in fake_ddb.update_calls]
    assert keys == ["p1", "p2"]


def test_refresher_dry_run_skips_updates(fake_ddb_factory, monkeypatch):
    fake_ddb = fake_ddb_factory("success")
    repo = _wire_repo_with_fake_ddb(monkeypatch, fake_ddb, [("p1", "f1")])
    _patch_client_scores(monkeypatch, {"f1": 10.0})

    # Enable dry-run via settings flag used by the handler
    settings.FLICKR_SCORE_REFRESH_DRY_RUN = True

    result = refresher_mod.handler({}, None)

    assert result["processed"] == 1
    assert result["updated"] == 1  # counts computed even in dry-run
    assert result["errors"] == 0
    assert fake_ddb.update_calls == []  # no DB writes


def test_refresher_respects_max_items(fake_ddb_factory, monkeypatch):
    fake_ddb = fake_ddb_factory("success")
    items = [("p1", "f1"), ("p2", "f2"), ("p3", "f3")]
    _wire_repo_with_fake_ddb(monkeypatch, fake_ddb, items)
    _patch_client_scores(monkeypatch, {"f1": 1.0, "f2": 2.0, "f3": 3.0})

    # Ensure not in dry-run mode
    settings.FLICKR_SCORE_REFRESH_DRY_RUN = False

    result = refresher_mod.handler({"max_items": 2}, None)

    assert result["processed"] == 2
    assert result["updated"] == 2
    assert result["errors"] == 0
    keys = [c["Key"]["photoId"]["S"] for c in fake_ddb.update_calls]
    assert keys == ["p1", "p2"]


def test_refresher_handles_score_errors(fake_ddb_factory, monkeypatch):
    fake_ddb = fake_ddb_factory("success")
    items = [("p1", "f1"), ("p2", "f2")]
    _wire_repo_with_fake_ddb(monkeypatch, fake_ddb, items)
    # Make one id fail
    _patch_client_scores(monkeypatch, {"f1": 1.0, "f2": 2.0}, fail_ids={"f2"})

    # Ensure not in dry-run mode
    settings.FLICKR_SCORE_REFRESH_DRY_RUN = False

    result = refresher_mod.handler({}, None)

    assert result["processed"] == 2
    assert result["updated"] == 1
    assert result["errors"] == 1
    keys = [c["Key"]["photoId"]["S"] for c in fake_ddb.update_calls]
    assert keys == ["p1"]
