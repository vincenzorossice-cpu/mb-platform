import pytest
from unittest.mock import MagicMock, patch

import requests

from orchestrator.modules.decision_engine.client import DecisionEngineClient


def _mock_post_ok(json_data):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = json_data
    return resp


def _mock_http_error(status: int = 500):
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status
    return resp


@pytest.fixture
def client() -> DecisionEngineClient:
    return DecisionEngineClient(base_url="http://test:8001", timeout=5)


ANSWERS_RESPONSE = {
    "answers": {
        "action": {"choice": "tap", "confidence": 0.9, "probabilities": {"tap": 0.9}},
        "target": {"choice": "Accedi", "confidence": 0.9, "probabilities": {}},
    }
}


class TestAsk:

    def test_success(self, client: DecisionEngineClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok(ANSWERS_RESPONSE)) as mock_post:
            result = client.ask("state text", {"action": {"type": "choice"}})

        assert result.success
        assert result.data == ANSWERS_RESPONSE
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == "http://test:8001/v1/systemone"
        assert mock_post.call_args.kwargs["json"] == {
            "state": "state text",
            "questions": {"action": {"type": "choice"}},
        }

    def test_connection_error(self, client: DecisionEngineClient):
        with patch.object(client.session, "post", side_effect=requests.ConnectionError("down")):
            result = client.ask("state", {})

        assert not result.success
        assert result.error_code == "DECISION_ENGINE_UNREACHABLE"

    def test_timeout(self, client: DecisionEngineClient):
        with patch.object(client.session, "post", side_effect=requests.Timeout("slow")):
            result = client.ask("state", {})

        assert not result.success
        assert result.error_code == "DECISION_ENGINE_TIMEOUT"

    def test_http_error(self, client: DecisionEngineClient):
        with patch.object(client.session, "post", return_value=_mock_http_error(500)):
            result = client.ask("state", {})

        assert not result.success
        assert result.error_code == "DECISION_ENGINE_HTTP_ERROR"

    def test_invalid_json(self, client: DecisionEngineClient):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        with patch.object(client.session, "post", return_value=resp):
            result = client.ask("state", {})

        assert not result.success
        assert result.error_code == "DECISION_ENGINE_PARSE_ERROR"


class TestBaseUrl:

    def test_default_base_url_from_env(self, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE_URL", "http://envhost:9000")
        client = DecisionEngineClient()
        assert client.base_url == "http://envhost:9000"

    def test_default_base_url_fallback(self, monkeypatch):
        monkeypatch.delenv("DECISION_ENGINE_URL", raising=False)
        client = DecisionEngineClient()
        assert client.base_url == "http://localhost:8001"

    def test_explicit_base_url_overrides_env(self, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE_URL", "http://envhost:9000")
        client = DecisionEngineClient(base_url="http://explicit:1234/")
        assert client.base_url == "http://explicit:1234"
