# Python
import base64
import json
import pytest

from shootflow.shared.llm import (
    load_openai_api_key_from_secrets,
    extract_json_object,
    strip_urls_and_html,
    truncate,
)
from shootflow.tests.conftest import FakeSecretsManager


def test_load_api_key_from_secrets_json_secretstring():
    sm = FakeSecretsManager({"api_key": "sk-json"})
    key = load_openai_api_key_from_secrets(sm, "OPENAI-SECRET")
    assert key == "sk-json"


def test_load_api_key_from_secrets_raw_secretstring():
    class RawClient:
        def get_secret_value(self, *, SecretId):
            return {"SecretString": "sk-raw"}
    key = load_openai_api_key_from_secrets(RawClient(), "OPENAI-SECRET")
    assert key == "sk-raw"


def test_load_api_key_from_secrets_secretbinary_json():
    class BinClient:
        def get_secret_value(self, *, SecretId):
            payload = base64.b64encode(json.dumps({"api_key": "sk-bin"}).encode("utf-8"))
            return {"SecretBinary": payload}
    key = load_openai_api_key_from_secrets(BinClient(), "OPENAI-SECRET")
    assert key == "sk-bin"


def test_extract_json_object_from_wrapped_text():
    wrapped = "Here you go: {\"k\": 1} thanks"
    obj = extract_json_object(wrapped)
    assert obj == {"k": 1}


def test_extract_json_object_raises_on_missing_json():
    with pytest.raises(ValueError):
        extract_json_object("no json here")


def test_strip_urls_and_html_and_repeating_chars():
    txt = (
        "<b>Hello</b> visit http://example.com and www.test.com ____ see <a href='https://x.com'>x</a> ~~~~~~"
    )
    out = strip_urls_and_html(txt)
    # No raw URLs or tags remain
    assert "http" not in out and "www." not in out and "<" not in out and ">" not in out
    # Repeating separators collapsed
    assert "____" not in out and "~~~~" not in out
    assert "Hello" in out


def test_truncate_adds_ellipsis():
    assert truncate("hello", 10) == "hello"
    assert truncate("abcdefghij", 10) == "abcdefghij"
    assert truncate("abcdefghijk", 10).endswith("…")
    assert len(truncate("abcdefghijk", 10)) == 10
