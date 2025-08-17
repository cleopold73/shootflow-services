# python
# tests/test_config.py
import importlib
import os

import pytest


def test_settings_ini_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force Dynaconf to use the [default] environment for this test
    monkeypatch.setenv("ENV_FOR_DYNACONF", "default")

    # Remove env overrides that could shadow settings.toml values
    for env_key in (
            "SHOOTFLOW_AWS_REGION",
            "SHOOTFLOW_DDB_TABLE",
            "SHOOTFLOW_EXIF_BLOCKLIST",
            "SHOOTFLOW_EXIF_MAX_TEXT_LENGTH",
            "SHOOTFLOW_EXIF_BASE64_SNIFF_PREFIX_LEN",
            "SHOOTFLOW_EXIF_LARGE_COLLECTION_SIZE",
            "SHOOTFLOW_S3_INGEST_DRY_RUN",
    ):
        monkeypatch.delenv(env_key, raising=False)

    # Reload settings after adjusting env so Dynaconf re-reads files
    from shootflow.shared import config
    importlib.reload(config)
    settings = config.settings

    # Core keys from [default]
    assert settings.get("AWS_REGION") == "us-east-1"
    assert settings.get("DDB_TABLE") == "shootflow-db"

    # EXIF keys (flattened section access via EXIF_* is the most reliable with Dynaconf)
    # BLOCKLIST should be a list/tuple and contain brand terms from settings.toml
    bl = settings.get("EXIF_BLOCKLIST")
    assert isinstance(bl, (list, tuple))
    assert "canon" in {str(x).lower() for x in bl}

    # Integer tunables should parse from "@int ..." entries
    assert settings.get("EXIF_MAX_TEXT_LENGTH") == 20000
    assert settings.get("EXIF_BASE64_SNIFF_PREFIX_LEN") == 200
    assert settings.get("EXIF_LARGE_COLLECTION_SIZE") == 2000
    assert settings.get("S3_INGEST_DRY_RUN") == True