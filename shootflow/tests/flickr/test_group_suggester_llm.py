import json
import types
from unittest.mock import patch
import pytest

from shootflow.shared.config import settings
import shootflow.flickr.group_suggester_llm as gl
from shootflow.tests.conftest import FakeSecretsManager


@pytest.fixture
def llm_test_env():
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

    return {}


@patch("shootflow.flickr.group_suggester_llm.boto3.client")
@patch("shootflow.flickr.group_suggester_llm.requests.post")
def test_suggest_groups_success_with_truncation(post_mock, boto3_client_mock, llm_test_env):
    # Arrange: candidates and LLM response selecting 3, limited to 2
    class FakeFlickrClient:
        def __init__(self, groups):
            self._groups = groups
        def list_member_groups(self, *, include_descriptions=True, max_groups=200):
            return self._groups[:max_groups]

    flickr_client = FakeFlickrClient(
        [{"id": "g1", "name": "One"}, {"id": "g2", "name": "Two"}, {"id": "g3", "name": "Three"}]
    )

    # Secrets Manager returns API key
    boto3_client_mock.return_value = FakeSecretsManager({"api_key": "sk-test"})

    class RespOK:
        status_code = 200
        def json(self):
            return {
                "choices": [
                    {"message": {"content": json.dumps({"selected_group_ids": ["g2", "g3", "g1", "g999"]})}}
                ]
            }

    post_mock.return_value = RespOK()

    # Act
    result = gl.suggest_groups_via_llm(
        {"title": "T", "caption": "C", "tags": ["x"]}, flickr_client=flickr_client, settings=settings
    )

    # Assert: preserves order from LLM, filters to candidates, enforces max size=2
    assert result == ["g2", "g3"]


@patch("shootflow.flickr.group_suggester_llm.boto3.client")
@patch("shootflow.flickr.group_suggester_llm.requests.post")
def test_suggest_groups_parses_wrapped_json(post_mock, boto3_client_mock, llm_test_env):
    # Arrange
    class FakeFlickrClient:
        def __init__(self, groups):
            self._groups = groups
        def list_member_groups(self, *, include_descriptions=True, max_groups=200):
            return self._groups[:max_groups]

    flickr_client = FakeFlickrClient([{"id": "g1", "name": "One"}])

    boto3_client_mock.return_value = FakeSecretsManager({"api_key": "sk-test"})

    class RespOK:
        status_code = 200
        def json(self):
            return {
                "choices": [
                    {"message": {"content": "Here you go:\n{ \"selected_group_ids\": [\"g1\"] }\nThanks!"}}
                ]
            }

    post_mock.return_value = RespOK()

    # Act
    result = gl.suggest_groups_via_llm(
        {"title": "T", "caption": "C", "tags": []}, flickr_client=flickr_client, settings=settings
    )

    # Assert
    assert result == ["g1"]


@patch("shootflow.flickr.group_suggester_llm.boto3.client")
@patch("shootflow.flickr.group_suggester_llm.requests.post")
def test_suggest_groups_http_400_returns_empty(post_mock, boto3_client_mock, llm_test_env):
    # Arrange
    class FakeFlickrClient:
        def __init__(self, groups):
            self._groups = groups
        def list_member_groups(self, *, include_descriptions=True, max_groups=200):
            return self._groups[:max_groups]

    flickr_client = FakeFlickrClient([{"id": "g1", "name": "One"}])

    boto3_client_mock.return_value = FakeSecretsManager({"api_key": "sk-test"})

    class RespBad:
        def __init__(self):
            self.status_code = 400
            self.text = "bad request"
        def json(self):
            return {"error": "bad"}

    post_mock.return_value = RespBad()

    # Act
    result = gl.suggest_groups_via_llm(
        {"title": "T", "caption": "C", "tags": []}, flickr_client=flickr_client, settings=settings
    )

    # Assert
    assert result == []


@patch("shootflow.flickr.group_suggester_llm.boto3.client")
@patch("shootflow.flickr.group_suggester_llm.requests.post")
def test_suggest_groups_timeout_returns_empty(post_mock, boto3_client_mock, llm_test_env):
    # Arrange: 0 retries to avoid loops; requests.post raises ReadTimeout
    settings.LLM_HTTP_RETRIES = 0

    class FakeFlickrClient:
        def __init__(self, groups):
            self._groups = groups
        def list_member_groups(self, *, include_descriptions=True, max_groups=200):
            return self._groups[:max_groups]

    flickr_client = FakeFlickrClient([{"id": "g1", "name": "One"}])

    boto3_client_mock.return_value = FakeSecretsManager({"api_key": "sk-test"})

    def raise_timeout(*a, **k):
        raise gl.ReadTimeout("timeout")

    post_mock.side_effect = raise_timeout

    # Act
    result = gl.suggest_groups_via_llm(
        {"title": "T", "caption": "C", "tags": []}, flickr_client=flickr_client, settings=settings
    )

    # Assert: gracefully degrades to empty
    assert result == []
