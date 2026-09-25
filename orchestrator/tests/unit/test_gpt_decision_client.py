import json

import httpx2
import openai
import pytest
from unittest.mock import MagicMock, patch

from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient

VISION_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "...",
        "criteria": {"tap": "Touch a point on the screen", "stop": "Stop playing"},
    },
    "target": {
        "type": "choice",
        "instructions": "...",
        "criteria": {"PUNTATA €10.00": "...", "SPIN": "..."},
    },
}


QUESTIONS = {
    "action": {"type": "choice", "instructions": "...", "criteria": {"tap": "...", "done": "..."}},
    "target": {"type": "choice", "instructions": "...", "criteria": {"Accedi": "Accedi", "up": "up"}},
    "text_to_type": {"type": "noul", "instructions": "..."},
}

NAVIGATION_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "...",
        "criteria": {"tap": "...", "input_text": "...", "scroll": "...", "wait": "...", "done": "..."},
    },
    "target": {"type": "choice", "instructions": "...", "criteria": {"Accedi": "Accedi", "up": "up"}},
    "text_to_type": {"type": "noul", "instructions": "..."},
}

SLOT_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "...",
        "criteria": {"spin": "Spin again", "stop": "Stop playing"},
    },
    "balance_ok": {"type": "noul", "instructions": "..."},
}

SLOT_TAP_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "...",
        "criteria": {"tap": "Touch an element", "stop": "Stop playing"},
    },
    "target": {"type": "choice", "instructions": "...", "criteria": {"PUNTATA € 1.00": "...", "Xiter": "..."}},
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


@pytest.fixture
def screenshot_path(tmp_path) -> str:
    from PIL import Image

    path = tmp_path / "screenshot.png"
    Image.new("RGB", (4, 4), color="white").save(path)
    return str(path)


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

    def test_invalid_action_falls_back_instead_of_failing(self, client: GPTDecisionClient):
        """QUESTIONS non ha 'wait'/'stop' tra le criteria: il fallback ricade
        sulla prima opzione valida in ordine alfabetico ('done')."""
        payload = {"action": "fly", "target": "Accedi", "confidence": 0.5}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask("state", QUESTIONS)

        assert result.success
        assert result.data["answers"]["action"]["choice"] == "done"

    def test_invalid_action_falls_back_to_wait_when_available(self, client: GPTDecisionClient):
        payload = {"action": "click_random_thing", "target": "Accedi", "confidence": 0.5}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask("state", NAVIGATION_QUESTIONS)

        assert result.success
        assert result.data["answers"]["action"]["choice"] == "wait"

    def test_slot_choices_accepted(self, client: GPTDecisionClient):
        payload = {"action": "spin", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask("state", SLOT_QUESTIONS)

        assert result.success
        assert result.data["answers"]["action"]["choice"] == "spin"

    def test_slot_invalid_action_falls_back_to_stop_not_wait(self, client: GPTDecisionClient):
        """GPT risponde con un'azione da bookmaker (input_text) mentre le uniche
        scelte valide sono spin/stop: niente 'wait' disponibile -> fallback 'stop'."""
        payload = {"action": "input_text", "confidence": 0.4}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask("state", SLOT_QUESTIONS)

        assert result.success
        assert result.data["answers"]["action"]["choice"] == "stop"

    def test_navigation_choices_accepted(self, client: GPTDecisionClient):
        payload = {"action": "scroll", "target": "down", "confidence": 0.7}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask("state", NAVIGATION_QUESTIONS)

        assert result.success
        assert result.data["answers"]["action"]["choice"] == "scroll"

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


class TestAskWithImage:

    def test_success_with_target_text(self, client: GPTDecisionClient, screenshot_path: str):
        payload = {
            "action": "tap", "screen_state": "idle", "target_text": "PUNTATA €10.00",
            "target_region": None, "confidence": 0.8, "reasoning": "Tap the bet display.",
        }
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as mock_create:
            result = client.ask_with_image(screenshot_path, "state text", VISION_QUESTIONS, 540, 1200)

        assert result.success
        assert result.data["answers"]["action"] == {"choice": "tap", "confidence": 0.8}
        assert result.data["answers"]["target"] == {"choice": {"text": "PUNTATA €10.00", "region": None}, "confidence": 0.8}
        assert result.data["answers"]["screen_state"] == {"choice": "idle", "confidence": 0.8}

        mock_create.assert_called_once()
        messages = mock_create.call_args.kwargs["messages"]
        assert mock_create.call_args.kwargs["max_tokens"] == 300
        user_content = messages[1]["content"]
        assert user_content[0]["type"] == "text"
        assert "state text" in user_content[0]["text"]
        assert "PUNTATA €10.00" in user_content[0]["text"]  # candidato elencato per il target
        assert user_content[1]["type"] == "image_url"
        assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert "540x1200" in messages[0]["content"]

    def test_success_with_target_region_when_no_label(self, client: GPTDecisionClient, screenshot_path: str):
        """Nessuna etichetta testuale disponibile per l'icona: GPT usa una
        regione grossolana invece di inventare coordinate precise."""
        payload = {
            "action": "tap", "screen_state": "idle", "target_text": None,
            "target_region": "bottom-right", "confidence": 0.7,
        }
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask_with_image(screenshot_path, "state text", VISION_QUESTIONS, 540, 1200)

        assert result.success
        assert result.data["answers"]["target"] == {"choice": {"text": None, "region": "bottom-right"}, "confidence": 0.7}

    def test_invalid_target_region_is_ignored(self, client: GPTDecisionClient, screenshot_path: str):
        """Una regione fuori dal vocabolario noto non deve mai propagarsi."""
        payload = {"action": "tap", "target_text": None, "target_region": "somewhere weird", "confidence": 0.5}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert result.success
        assert result.data["answers"]["target"]["choice"] is None

    def test_unknown_screen_state_falls_back_to_other(self, client: GPTDecisionClient, screenshot_path: str):
        payload = {"action": "stop", "screen_state": "something_unexpected", "confidence": 0.5}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert result.success
        assert result.data["answers"]["screen_state"]["choice"] == "other"

    def test_system_prompt_insists_on_lowering_bet_before_spinning(self, client: GPTDecisionClient, screenshot_path: str):
        """Regressione per il bug 'GPT non abbassa la puntata al minimo':
        il system prompt deve dire esplicitamente di impostare la posta al
        minimo PRIMA di cercare il tasto spin."""
        payload = {"action": "tap", "target_text": "PUNTATA €10.00", "confidence": 0.8}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as mock_create:
            client.ask_with_image(screenshot_path, "state text", VISION_QUESTIONS, 540, 1200)

        system_message = mock_create.call_args.kwargs["messages"][0]["content"]
        assert "CRITICAL" in system_message
        assert "minimum" in system_message.lower()
        assert "before" in system_message.lower()
        assert "must not spin" in system_message.lower()
        assert "close a bet menu" in system_message.lower()
        assert "do not just repeat the exact same" in system_message.lower()

    def test_system_prompt_warns_about_coin_icon_ambiguity(self, client: GPTDecisionClient, screenshot_path: str):
        """Regressione per il bug reale: un'icona a gettoni con '+' puo' essere
        'aggiungi crediti demo' invece del selettore posta su alcune slot."""
        payload = {"action": "stop", "confidence": 0.5}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as mock_create:
            client.ask_with_image(screenshot_path, "state text", VISION_QUESTIONS, 540, 1200)

        system_message = mock_create.call_args.kwargs["messages"][0]["content"].lower()
        assert "add demo credits" in system_message
        assert "bet amount text" in system_message

    def test_system_prompt_forbids_pixel_coordinates_and_lists_regions(self, client: GPTDecisionClient, screenshot_path: str):
        """gpt-4o-mini non e' affidabile nel grounding spaziale (slot_anatomy.md):
        il prompt non deve chiedere coordinate x/y dirette, solo target_text o
        una regione grossolana su griglia 3x3."""
        payload = {"action": "stop", "confidence": 0.5}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as mock_create:
            client.ask_with_image(screenshot_path, "state text", VISION_QUESTIONS, 540, 1200)

        system_message = mock_create.call_args.kwargs["messages"][0]["content"]
        assert '"x"' not in system_message
        assert '"y"' not in system_message
        assert "not reliable at that" in system_message.lower()
        assert "target_region" in system_message
        assert "bottom-right" in system_message
        assert "screen_state" in system_message

    def test_stop_has_no_coordinates(self, client: GPTDecisionClient, screenshot_path: str):
        payload = {"action": "stop", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert result.success
        assert result.data["answers"]["action"]["choice"] == "stop"
        assert result.data["answers"]["target"]["choice"] is None

    def test_invalid_action_falls_back_to_stop(self, client: GPTDecisionClient, screenshot_path: str):
        payload = {"action": "input_text", "target_text": "PUNTATA €10.00", "confidence": 0.5}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert result.success
        assert result.data["answers"]["action"]["choice"] == "stop"

    def test_missing_screenshot_file(self, client: GPTDecisionClient):
        result = client.ask_with_image("/nonexistent/path/screenshot.png", "state", VISION_QUESTIONS, 540, 1200)

        assert not result.success
        assert result.error_code == "GPT_DECISION_IMAGE_READ_ERROR"

    def test_malformed_json(self, client: GPTDecisionClient, screenshot_path: str):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion("not json")):
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert not result.success
        assert result.error_code == "GPT_DECISION_PARSE_ERROR"

    def test_connection_error(self, client: GPTDecisionClient, screenshot_path: str):
        with patch.object(client.client.chat.completions, "create", side_effect=_api_connection_error()):
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert not result.success
        assert result.error_code == "GPT_DECISION_UNREACHABLE"

    def test_timeout(self, client: GPTDecisionClient, screenshot_path: str):
        with patch.object(client.client.chat.completions, "create", side_effect=_api_timeout_error()):
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert not result.success
        assert result.error_code == "GPT_DECISION_TIMEOUT"

    def test_http_error(self, client: GPTDecisionClient, screenshot_path: str):
        with patch.object(client.client.chat.completions, "create", side_effect=_api_status_error(500)):
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert not result.success
        assert result.error_code == "GPT_DECISION_HTTP_ERROR"


class TestSystemPrompt:

    def test_slot_questions_mention_spin_stop(self, client: GPTDecisionClient):
        payload = {"action": "spin", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as mock_create:
            client.ask("state", SLOT_QUESTIONS)

        system_message = mock_create.call_args.kwargs["messages"][0]["content"]
        assert "slot machine" in system_message.lower()
        assert "bookmaker" not in system_message.lower()

    def test_navigation_questions_mention_bookmaker(self, client: GPTDecisionClient):
        payload = {"action": "tap", "target": "Accedi", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as mock_create:
            client.ask("state", NAVIGATION_QUESTIONS)

        system_message = mock_create.call_args.kwargs["messages"][0]["content"]
        assert "bookmaker" in system_message.lower()
        assert "slot machine" not in system_message.lower()

    def test_slot_tap_questions_mention_slot_not_bookmaker_or_spin(self, client: GPTDecisionClient):
        """criteria={'tap','stop'}: dominio nuovo (tap libero in slot), non deve
        confondersi con la navigazione bookmaker (che condivide 'tap') ne' menzionare
        'spin' (non e' un'azione valida qui)."""
        payload = {"action": "tap", "target": "Xiter", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as mock_create:
            client.ask("state", SLOT_TAP_QUESTIONS)

        system_message = mock_create.call_args.kwargs["messages"][0]["content"]
        assert "slot machine" in system_message.lower()
        assert "bookmaker" not in system_message.lower()
        assert "'spin'" not in system_message.lower()
