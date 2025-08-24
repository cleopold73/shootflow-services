# Python
from datetime import datetime, date, timezone
from typing import List

import pytest

from shootflow.shared.helpers import to_epoch_seconds, retry_with_backoff
import shootflow.shared.helpers as helpers_mod


def test_to_epoch_seconds_with_numeric():
    assert to_epoch_seconds(1700000000) == 1700000000
    assert to_epoch_seconds(1700000000.9) == 1700000000  # truncated


def test_to_epoch_seconds_with_datetime_aware():
    dt = datetime(2021, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert to_epoch_seconds(dt) == int(dt.timestamp())


def test_to_epoch_seconds_with_date_start_and_end_of_day():
    d = date(2023, 5, 6)
    # Compute expected using the same local-time interpretation helpers uses
    start_dt = datetime(d.year, d.month, d.day, 0, 0, 0)
    end_dt = datetime(d.year, d.month, d.day, 23, 59, 59)
    assert to_epoch_seconds(d, end_of_day=False) == int(start_dt.timestamp())
    assert to_epoch_seconds(d, end_of_day=True) == int(end_dt.timestamp())


def test_to_epoch_seconds_with_string():
    # Numeric string
    assert to_epoch_seconds("1700000000") == 1700000000
    # ISO 8601 with timezone to avoid local-time dependence
    s = "2021-01-02T03:04:05+00:00"
    dt = datetime.fromisoformat(s)
    assert to_epoch_seconds(s) == int(dt.timestamp())


def test_to_epoch_seconds_errors():
    with pytest.raises(ValueError):
        to_epoch_seconds(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        to_epoch_seconds("not-a-date")
    with pytest.raises(TypeError):
        to_epoch_seconds(object())  # type: ignore[arg-type]


def test_retry_with_backoff_success_no_retry(monkeypatch):
    sleeps: List[float] = []

    def fake_sleep(x):
        sleeps.append(x)

    monkeypatch.setattr(helpers_mod, "time", helpers_mod.time)
    monkeypatch.setattr(helpers_mod.time, "sleep", fake_sleep)

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    out = retry_with_backoff(fn, max_attempts=5, base_delay_s=0.1, multiplier=2.0, logger=None)
    assert out == "ok"
    assert calls["n"] == 1
    assert sleeps == []  # no retries


def test_retry_with_backoff_eventual_success(monkeypatch):
    sleeps: List[float] = []

    def fake_sleep(x):
        sleeps.append(x)

    monkeypatch.setattr(helpers_mod.time, "sleep", fake_sleep)

    class E(Exception):
        pass

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise E("fail")
        return 42

    out = retry_with_backoff(fn, max_attempts=3, base_delay_s=0.1, multiplier=2.0, logger=None)
    assert out == 42
    # Two retries -> two sleeps with geometric backoff: 0.1, 0.2
    assert sleeps == pytest.approx([0.1, 0.2], rel=0, abs=1e-9)
    assert calls["n"] == 3


def test_retry_with_backoff_non_retryable_raises_immediately(monkeypatch):
    sleeps: List[float] = []

    def fake_sleep(x):
        sleeps.append(x)

    monkeypatch.setattr(helpers_mod.time, "sleep", fake_sleep)

    class E(Exception):
        pass

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise E("no-retry")

    with pytest.raises(E):
        retry_with_backoff(fn, max_attempts=5, is_retryable=lambda e: False, logger=None)

    # Should not sleep when first exception is non-retryable
    assert sleeps == []
    assert calls["n"] == 1


def test_retry_with_backoff_exhausts_and_raises_last(monkeypatch):
    sleeps: List[float] = []

    def fake_sleep(x):
        sleeps.append(x)

    monkeypatch.setattr(helpers_mod.time, "sleep", fake_sleep)

    class E(Exception):
        pass

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise E(f"boom-{calls['n']}")

    with pytest.raises(E) as ei:
        retry_with_backoff(fn, max_attempts=3, base_delay_s=0.2, multiplier=3.0, logger=None)

    # Should have slept for attempts 1 and 2
    assert sleeps == pytest.approx([0.2, 0.6], rel=0, abs=1e-9)
    assert calls["n"] == 3
    # Last exception should be from the final attempt
    assert "boom-3" in str(ei.value)
