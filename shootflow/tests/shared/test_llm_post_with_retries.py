# Python
import types
import time
import pytest

from shootflow.shared.llm import post_with_retries


class _FakeResp:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text
    def json(self):
        return {"ok": True}


def test_post_with_retries_eventual_success(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        # First two attempts fail with 500, third succeeds
        if calls["n"] < 3:
            return _FakeResp(status_code=500, text="server error")
        return _FakeResp(status_code=200)

    start = time.time()
    resp = post_with_retries(
        "https://example/api",
        headers={"A": "b"},
        body={"x": 1},
        connect_timeout_s=0.01,
        read_timeout_s=0.01,
        max_retries=3,
        backoff_base_s=0.0,
        post_fn=fake_post,
    )
    elapsed = time.time() - start
    assert resp.status_code == 200
    assert calls["n"] == 3
    # With backoff_base_s=0.0 it should be quick
    assert elapsed < 0.5


def test_post_with_retries_raises_after_exhaustion():
    def always_error(url, headers=None, json=None, timeout=None):
        raise Exception("boom")

    with pytest.raises(Exception):
        post_with_retries(
            "https://example/api",
            headers={},
            body={},
            connect_timeout_s=0.01,
            read_timeout_s=0.01,
            max_retries=1,
            backoff_base_s=0.0,
            post_fn=always_error,
        )
