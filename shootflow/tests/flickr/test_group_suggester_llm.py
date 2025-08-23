import json
import types
import pytest

from shootflow.shared.config import settings
import shootflow.flickr.group_suggester_llm as gl


class FakeSecretsManager:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def get_secret_value(self, *, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": json.dumps(self.payload)}


class FakeBoto3SecretsOnly:
    def __init__(self, sm: FakeSecretsManager):
        self._sm = sm

    def client(self, name, region_name=None):
        assert name == "secretsmanager"
        return self._sm


class FakeFlickrClient:
    def __init__(self, groups):
        self._groups = groups

    def list_member_groups(self, *, include_descriptions=True, max_groups=200):
        return self._groups[: max_groups]


@pytest.fixture
def llm_test_env(monkeypatch):
    # Settings required by group_suggester_llm
    settings.AWS_REGION = "us-east-1"
    settings.OPENAI_SECRET_NAME = "OPENAI-SECRET"
    settings.OPENAI_BASE_URL = "https://api.openai.com/v1"
    settings.OPENAI_CHAT_MODEL = "gpt-any"
    settings.LLM_MAX_GROUPS = 2
    settings.LLM_MAX_CANDIDATE_GROUPS = 100
    settings.LLM_HTTP_CONNECT_TIMEOUT_SECONDS = 10.0
    settings.LLM_HTTP_READ_TIMEOUT_SECONDS = 5.0
    settings.LLM_HTTP_RETRIES = 0
    settings.LLM_HTTP_BACKOFF_BASE_SECONDS = 0.1

    # Secrets manager returns an API key
    sm = FakeSecretsManager({"api_key": "sk-test"})
    monkeypatch.setattr(gl, "boto3", FakeBoto3SecretsOnly(sm), raising=True)

    # Speed up retries/backoff if any
    monkeypatch.setattr(gl.time, "sleep", lambda s: None, raising=True)

    return {"sm": sm}


def test_suggest_groups_success_with_truncation(monkeypatch, llm_test_env):
    # Arrange: candidates and LLM response selecting 3, limited to 2
    flickr_client = FakeFlickrClient(
        [{"id": "g1", "name": "One"}, {"id": "g2", "name": "Two"}, {"id": "g3", "name": "Three"}]
    )

    class RespOK:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"selected_group_ids": ["g2", "g3", "g1", "g999"]}
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(gl.requests, "post", lambda *a, **k: RespOK(), raising=True)

    # Act
    result = gl.suggest_groups_via_llm(
        {"title": "T", "caption": "C", "tags": ["x"]}, flickr_client=flickr_client, settings=settings
    )

    # Assert: preserves order from LLM, filters to candidates, enforces max size=2
    assert result == ["g2", "g3"]


def test_suggest_groups_parses_wrapped_json(monkeypatch, llm_test_env):
    # Arrange
    flickr_client = FakeFlickrClient([{"id": "g1", "name": "One"}])

    class RespOK:
        status_code = 200

        def json(self):
            # Content includes extra text around the JSON; regex path should extract the object
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Here you go:\n{ \"selected_group_ids\": [\"g1\"] }\nThanks!"
                        }
                    }
                ]
            }

    monkeypatch.setattr(gl.requests, "post", lambda *a, **k: RespOK(), raising=True)

    # Act
    result = gl.suggest_groups_via_llm(
        {"title": "T", "caption": "C", "tags": []}, flickr_client=flickr_client, settings=settings
    )

    # Assert
    assert result == ["g1"]


def test_suggest_groups_http_400_returns_empty(monkeypatch, llm_test_env):
    # Arrange
    flickr_client = FakeFlickrClient([{"id": "g1", "name": "One"}])

    class RespBad:
        def __init__(self):
            self.status_code = 400
            self.text = "bad request"

        def json(self):
            return {"error": "bad"}

    monkeypatch.setattr(gl.requests, "post", lambda *a, **k: RespBad(), raising=True)

    # Act
    result = gl.suggest_groups_via_llm(
        {"title": "T", "caption": "C", "tags": []}, flickr_client=flickr_client, settings=settings
    )

    # Assert
    assert result == []


def test_suggest_groups_timeout_returns_empty(monkeypatch, llm_test_env):
    # Arrange: 0 retries to avoid loops; requests.post raises ReadTimeout
    settings.LLM_HTTP_RETRIES = 0
    flickr_client = FakeFlickrClient([{"id": "g1", "name": "One"}])

    def raise_timeout(*a, **k):
        raise gl.ReadTimeout("timeout")

    monkeypatch.setattr(gl.requests, "post", raise_timeout, raising=True)

    # Act
    result = gl.suggest_groups_via_llm(
        {"title": "T", "caption": "C", "tags": []}, flickr_client=flickr_client, settings=settings
    )

    # Assert: gracefully degrades to empty
    assert result == []
