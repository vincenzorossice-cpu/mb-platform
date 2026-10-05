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


class TestModelResolution:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for name in ("GPT_MODEL", "DECISION_ENGINE_PROVIDER", "GPT_READER_MODEL"):
            monkeypatch.delenv(name, raising=False)

    def _client(self, **kwargs):
        with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI"):
            return GPTDecisionClient(api_key="k", **kwargs)

    def test_default_is_gpt_4o_mini(self):
        assert self._client().model == "gpt-4o-mini"

    def test_provider_gpt6sol_selects_sol(self, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE_PROVIDER", "gpt6sol")
        assert self._client().model == "gpt-6.1-sol"

    def test_provider_openjev_or_unknown_keeps_the_default_model(self, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE_PROVIDER", "openjev")
        assert self._client().model == "gpt-4o-mini"

    def test_gpt_model_wins_over_the_provider(self, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE_PROVIDER", "gpt6sol")
        monkeypatch.setenv("GPT_MODEL", "gpt-6-luna")
        assert self._client().model == "gpt-6-luna"

    def test_the_constructor_argument_wins_over_everything(self, monkeypatch):
        monkeypatch.setenv("GPT_MODEL", "gpt-6-luna")
        assert self._client(model="gpt-4o").model == "gpt-4o"

    def test_the_reader_model_is_separate_and_cheap_by_default(self, monkeypatch):
        from orchestrator.modules.decision_engine.gpt_client import resolve_reader_model
        monkeypatch.setenv("GPT_MODEL", "gpt-6.1-sol")
        assert resolve_reader_model() == "gpt-4o-mini"
        monkeypatch.setenv("GPT_READER_MODEL", "gpt-6-luna")
        assert resolve_reader_model() == "gpt-6-luna"


class TestVisionTokensByModel:
    def test_compact_models_use_fewer_output_tokens(self, screenshot_path):
        with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI"):
            sol = GPTDecisionClient(api_key="k", model="gpt-6.1-sol")
        payload = {"action": "tap", "screen_state": "idle", "target_text": "SPIN", "confidence": 0.9}
        with patch.object(sol.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as create:
            sol.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)
        assert create.call_args.kwargs["max_tokens"] == 150

    def test_other_models_keep_300(self, client, screenshot_path):
        payload = {"action": "tap", "screen_state": "idle", "target_text": "SPIN", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as create:
            client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)
        assert create.call_args.kwargs["max_tokens"] == 300


class TestCache:
    PAYLOAD = {"action": "tap", "screen_state": "idle", "target_text": "SPIN", "confidence": 0.9}

    def test_identical_image_question_is_asked_once(self, client, screenshot_path):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.PAYLOAD))) as create:
            first = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)
            second = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert create.call_count == 1 and first.data == second.data

    def test_a_different_file_or_state_is_a_new_call(self, client, screenshot_path, tmp_path):
        from PIL import Image
        other = tmp_path / "other.png"
        Image.new("RGB", (4, 4), color="black").save(other)
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.PAYLOAD))) as create:
            client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)
            client.ask_with_image(str(other), "state", VISION_QUESTIONS, 540, 1200)
            client.ask_with_image(str(other), "another state", VISION_QUESTIONS, 540, 1200)

        assert create.call_count == 3

    def test_invalidate_cache_forces_a_new_call(self, client, screenshot_path):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.PAYLOAD))) as create:
            client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)
            client.invalidate_cache()
            client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert create.call_count == 2

    def test_text_questions_are_cached_too(self, client):
        payload = {"action": "tap", "target": "Accedi", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as create:
            client.ask("state", NAVIGATION_QUESTIONS)
            client.ask("state", NAVIGATION_QUESTIONS)
            client.invalidate_cache()
            client.ask("state", NAVIGATION_QUESTIONS)

        assert create.call_count == 2

    def test_failures_are_never_cached(self, client):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion("not json")) as create:
            client.ask("state", NAVIGATION_QUESTIONS)
            client.ask("state", NAVIGATION_QUESTIONS)

        assert create.call_count == 2

    def test_a_cached_answer_is_a_copy(self, client):
        payload = {"action": "tap", "target": "Accedi", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            first = client.ask("state", NAVIGATION_QUESTIONS)
            first.data["answers"]["action"]["choice"] = "MUTATED"
            second = client.ask("state", NAVIGATION_QUESTIONS)

        assert second.data["answers"]["action"]["choice"] == "tap"


class TestUsageLog:
    def _response(self, tokens_in, tokens_out):
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out))

    def test_known_price_gives_an_estimated_cost(self, client, caplog):
        with caplog.at_level("INFO"):
            client.record_usage(self._response(1_000_000, 1_000_000), "prova")

        assert "gpt-4o-mini" in caplog.text and "~$0.7500" in caplog.text
        assert "1 chiamate" in client.usage_summary()

    def test_unknown_price_is_declared_not_invented(self, caplog, monkeypatch):
        monkeypatch.delenv("GPT_PRICE_INPUT_PER_M", raising=False)
        with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI"):
            sol = GPTDecisionClient(api_key="k", model="gpt-x-unknown")
        with caplog.at_level("INFO"):
            sol.record_usage(self._response(100, 10))

        assert "n/d" in caplog.text and "costo n/d" in sol.usage_summary()

    def test_price_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GPT_PRICE_INPUT_PER_M", "2.0")
        monkeypatch.setenv("GPT_PRICE_OUTPUT_PER_M", "8.0")
        with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI"):
            sol = GPTDecisionClient(api_key="k", model="gpt-x-unknown")
        sol.record_usage(self._response(1_000_000, 500_000))

        assert "~$6.0000" in sol.usage_summary()

    def test_sol_has_a_price_in_the_price_file(self, monkeypatch):
        monkeypatch.delenv("GPT_PRICE_INPUT_PER_M", raising=False)
        with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI"):
            sol = GPTDecisionClient(api_key="k", model="gpt-6.1-sol")
        sol.record_usage(self._response(1_000_000, 1_000_000))

        assert "~$12.0000" in sol.usage_summary()      # $2 in + $10 out

    def test_seconds_are_logged_and_summarised_by_purpose(self, client, caplog):
        with caplog.at_level("INFO"):
            client.record_usage(self._response(10, 1), "visione", seconds=4.2)
            client.record_usage(self._response(10, 1), "visione", seconds=5.8)

        assert "tempo=4.2 s" in caplog.text and "visione 2 (~5.0 s)" in client.usage_summary()

    def test_cached_tokens_use_the_cached_price(self, monkeypatch):
        from types import SimpleNamespace
        with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI"):
            sol = GPTDecisionClient(api_key="k", model="gpt-6.1-sol")
        response = SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=1_000_000, completion_tokens=0, prompt_tokens_details=SimpleNamespace(cached_tokens=1_000_000),
        ))
        sol.record_usage(response)

        assert "~$0.1000" in sol.usage_summary()

    def test_a_response_without_usage_never_raises(self, client):
        client.record_usage(MagicMock())
        client.record_usage(object())

    def test_every_completion_is_recorded(self, client):
        payload = {"action": "tap", "target": "Accedi", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))):
            client.ask("state", NAVIGATION_QUESTIONS)

        assert "1 chiamate" in client.usage_summary()


class TestNavigationVision:
    NAV_PAYLOAD = {
        "action": "tap", "screen_state": "other", "target_text": None, "target_region": "top-left",
        "target_description": "the hamburger menu icon", "text_to_type": "", "confidence": 0.8,
    }

    def test_navigation_actions_use_the_navigation_prompt(self, client, screenshot_path):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.NAV_PAYLOAD))) as create:
            result = client.ask_with_image(screenshot_path, "Goal: attiva la promo", NAVIGATION_QUESTIONS, 540, 1200)

        system = create.call_args.kwargs["messages"][0]["content"]
        assert "navigating a bookmaker" in system and "set the bet" not in system.lower()
        assert result.data["answers"]["target_description"]["choice"] == "the hamburger menu icon"
        assert "text_to_type" in result.data["answers"]

    def test_slot_actions_still_use_the_slot_prompt(self, client, screenshot_path):
        payload = {"action": "tap", "screen_state": "idle", "target_text": "SPIN", "confidence": 0.9}
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(payload))) as create:
            result = client.ask_with_image(screenshot_path, "state", VISION_QUESTIONS, 540, 1200)

        assert "slot machine" in create.call_args.kwargs["messages"][0]["content"]
        assert "target_description" not in result.data["answers"]

    def test_navigation_prompt_has_the_phase_done_and_demo_rules(self, client, screenshot_path):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.NAV_PAYLOAD))) as create:
            client.ask_with_image(screenshot_path, "Goal: g", NAVIGATION_QUESTIONS, 540, 1200)

        system = create.call_args.kwargs["messages"][0]["content"]
        assert "ONLY when the final condition" in system and "VISIBLE" in system
        assert "Previous actions" in system
        assert "'Prova' / 'For fun', never 'Gioca'" in system

    def test_navigation_prompt_covers_gentle_scroll_and_load_more(self, client, screenshot_path):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.NAV_PAYLOAD))) as create:
            client.ask_with_image(screenshot_path, "Goal: g", NAVIGATION_QUESTIONS, 540, 1200)

        system = create.call_args.kwargs["messages"][0]["content"]
        assert "Carica altro" in system and "ONE more step" in system and "'up'" in system

    def test_navigation_with_a_reasoning_model_gets_more_tokens(self, screenshot_path):
        sol = GPTDecisionClient(api_key="k", model="gpt-6.1-sol")
        with patch.object(sol.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.NAV_PAYLOAD))) as create:
            sol.ask_with_image(screenshot_path, "Goal: g", NAVIGATION_QUESTIONS, 540, 1200)

        kwargs = create.call_args.kwargs
        assert kwargs.get("max_tokens", kwargs.get("max_completion_tokens")) == 300

    def test_an_empty_answer_is_retried_once_with_double_tokens(self, screenshot_path):
        sol = GPTDecisionClient(api_key="k", model="gpt-6.1-sol")
        replies = [_mock_completion(""), _mock_completion(json.dumps(self.NAV_PAYLOAD))]
        with patch.object(sol.client.chat.completions, "create", side_effect=replies) as create:
            result = sol.ask_with_image(screenshot_path, "Goal: g", NAVIGATION_QUESTIONS, 540, 1200)

        assert result.success and create.call_count == 2
        last = create.call_args.kwargs
        assert last.get("max_tokens", last.get("max_completion_tokens")) == 600

    def test_two_empty_answers_fail_with_a_parse_error(self, screenshot_path):
        sol = GPTDecisionClient(api_key="k", model="gpt-6.1-sol")
        with patch.object(sol.client.chat.completions, "create", side_effect=[_mock_completion(""), _mock_completion("")]):
            result = sol.ask_with_image(screenshot_path, "Goal: g", NAVIGATION_QUESTIONS, 540, 1200)

        assert not result.success and result.error_code == GPTDecisionClient.GPT_DECISION_PARSE_ERROR

    def test_navigation_prompt_covers_notes_demo_tiles_and_notifications(self, client, screenshot_path):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.NAV_PAYLOAD))) as create:
            client.ask_with_image(screenshot_path, "Goal: g", NAVIGATION_QUESTIONS, 540, 1200)

        system = create.call_args.kwargs["messages"][0]["content"]
        assert "did not change" in system and "never tap a game's tile" in system and "Telegram" in system

    def test_usage_summary_counts_calls_by_purpose(self, client):
        client.record_usage(MagicMock(usage=MagicMock(prompt_tokens=1, completion_tokens=1)), "visione")
        client.record_usage(MagicMock(usage=MagicMock(prompt_tokens=1, completion_tokens=1)), "griglia icone")
        client.record_usage(MagicMock(usage=MagicMock(prompt_tokens=1, completion_tokens=1)), "griglia icone")

        assert "griglia icone 2" in client.usage_summary() and "visione 1" in client.usage_summary()

    def test_navigation_prompt_allows_one_retry_after_a_missed_point(self, client, screenshot_path):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.NAV_PAYLOAD))) as create:
            client.ask_with_image(screenshot_path, "Goal: g", NAVIGATION_QUESTIONS, 540, 1200)

        system = create.call_args.kwargs["messages"][0]["content"]
        assert "POINT (coordinates)" in system and "try ONCE more" in system

    def test_navigation_prompt_lets_a_goal_skip_a_verification_when_the_step_is_already_done(self, client, screenshot_path):
        with patch.object(client.client.chat.completions, "create", return_value=_mock_completion(json.dumps(self.NAV_PAYLOAD))) as create:
            client.ask_with_image(screenshot_path, "Goal: g", NAVIGATION_QUESTIONS, 540, 1200)

        system = create.call_args.kwargs["messages"][0]["content"]
        assert "EXCEPT when the goal says the step may already be done" in system
