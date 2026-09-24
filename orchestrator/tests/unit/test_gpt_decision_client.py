import json

import httpx2
import openai
import pytest
from unittest.mock import MagicMock, patch

from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient


QUESTIONS = {
    "action": {"type": "choice", "instructions": "...", "criteria": {"tap": "...", "done": "..."}},
    "target": {"type": "choice", "instructions": "...", "criteria": {"Accedi": "Accedi", "up": "up"}},
    "text_to_type": {"type": "noul", "instructions": "..."},
}


def _mock_completion(content: str):
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    completion = MagicMock()
    completion.choices = [choice]
    return completion


def _api_status_error(status_code: int = 500) -> openai.APIStatusError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx2.Response(status_code, request=request, json={"error": "boom"})
    return openai.APIStatusError("server error", response=response, body={"error": "boom"})


def _api_connection_error() -> openai.APIConnectionError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APIConnectionError(request=request)


def _api_timeout_error() -> openai.APITimeoutError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APITimeoutError(request=request)


@pytest.fixture
def client() -> GPTDecisionClient:
    with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI"):
        return GPTDecisionClient(api_key="test-key")


class TestConstruction:

    def test_uses_explicit_api_key(self):
        with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI") as mock_openai:
            GPTDecisionClient(api_key="explicit-key")
        assert mock_openai.call_args.kwargs["api_key"] == "explicit-key"

    def test_uses_env_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI") as mock_openai:
            GPTDecisionClient()
        assert mock_openai.call_args.kwargs["api_key"] == "env-key"

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError):
            GPTDecisionClient()


class TestAsk:

    def test_success(self, client: GPTDecisionClient):
        payload = {
            "action": "tap",
            "target": "Accedi",
            "text_to_type": "",
            "confidence": 0.85,
            "reasoning": "Il bottone Accedi porta al login.",
        }
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as mock_create:
            result = client.ask("state text", QUESTIONS)

        assert result.success
        assert result.data["answers"]["action"] == {"choice": "tap", "confidence": 0.85}
        assert result.data["answers"]["target"] == {"choice": "Accedi", "confidence": 0.85}
        assert result.data["answers"]["text_to_type"] == {"choice": "", "confidence": 0.85}
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["model"] == "gpt-4o-mini"
        assert mock_create.call_args.kwargs["response_format"] == {"type": "json_object"}

    def test_invalid_action(self, client: GPTDecisionClient):
        payload = {"action": "fly", "target": "Accedi", "confidence": 0.5}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask("state", QUESTIONS)

        assert not result.success
        assert result.error_code == "GPT_DECISION_INVALID_ACTION"

    def test_malformed_json(self, client: GPTDecisionClient):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion("not json")):
            result = client.ask("state", QUESTIONS)

        assert not result.success
        assert result.error_code == "GPT_DECISION_PARSE_ERROR"

    def test_connection_error(self, client: GPTDecisionClient):
        with patch.object(client.client.chat.completions, "create", side_effect=_api_connection_error()):
            result = client.ask("state", QUESTIONS)

        assert not result.success
        assert result.error_code == "GPT_DECISION_UNREACHABLE"

    def test_timeout(self, client: GPTDecisionClient):
        with patch.object(client.client.chat.completions, "create", side_effect=_api_timeout_error()):
            result = client.ask("state", QUESTIONS)

        assert not result.success
        assert result.error_code == "GPT_DECISION_TIMEOUT"

    def test_http_error(self, client: GPTDecisionClient):
        with patch.object(client.client.chat.completions, "create", side_effect=_api_status_error(500)):
            result = client.ask("state", QUESTIONS)

        assert not result.success
        assert result.error_code == "GPT_DECISION_HTTP_ERROR"
