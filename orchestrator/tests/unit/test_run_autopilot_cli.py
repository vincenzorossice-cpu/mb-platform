import argparse
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from orchestrator.core.result import Result
from orchestrator.scripts.run_autopilot import _check_openai_key as real_check_openai_key
from orchestrator.scripts.run_autopilot import build_autopilot, main


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        device="ZT322ZSJVS", slot="pirots5", target=30.0, max_steps=50,
        dry_run=False, live=False, no_gpt=False, no_hitl=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture(autouse=True)
def _no_network_key_check(monkeypatch):
    """Nessun test deve chiamare OpenAI: il controllo della chiave e' finto
    (i suoi casi reali sono in TestOpenAIKeyCheck)."""
    monkeypatch.setattr("orchestrator.scripts.run_autopilot._check_openai_key", lambda client: Result.ok(True))


class TestBuildAutopilot:
    def test_wires_all_collaborators(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        result = build_autopilot(_args())
        assert result.success
        strategies = result.data.locator.strategies
        for name in ("profile", "memory", "ocr_anchor", "spatial_heuristic", "template", "gpt", "hitl"):
            assert name in strategies

    def test_no_gpt_omits_gpt_strategy(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        result = build_autopilot(_args(no_gpt=True))
        assert result.success
        assert "gpt" not in result.data.locator.strategies
        assert "hitl" in result.data.locator.strategies

    def test_no_hitl_omits_hitl_strategy(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = build_autopilot(_args(no_hitl=True))
        assert result.success
        assert "hitl" not in result.data.locator.strategies

    def test_missing_api_key_degrades_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        result = build_autopilot(_args())
        assert result.success
        assert "gpt" not in result.data.locator.strategies


class TestOpenAIKeyCheck:
    def test_rejected_key_blocks_the_start_with_a_clear_message(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "bad-key")
        monkeypatch.setattr(
            "orchestrator.scripts.run_autopilot._check_openai_key",
            lambda client: Result.fail("chiave rifiutata", "OPENAI_KEY_REJECTED"),
        )
        result = build_autopilot(_args())

        assert not result.success
        assert result.error_code == "OPENAI_KEY_REJECTED"

    def test_no_gpt_skips_the_check_entirely(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "bad-key")
        check = MagicMock(return_value=Result.fail("x", "OPENAI_KEY_REJECTED"))
        monkeypatch.setattr("orchestrator.scripts.run_autopilot._check_openai_key", check)

        assert build_autopilot(_args(no_gpt=True)).success
        check.assert_not_called()

    def test_401_is_reported_as_rejected_key(self):
        client = MagicMock()
        response = httpx.Response(401, request=httpx.Request("GET", "https://api.openai.com/v1/models"))
        client.client.models.list.side_effect = openai.AuthenticationError(
            "Incorrect API key", response=response, body=None,
        )

        result = real_check_openai_key(client)

        assert not result.success
        assert result.error_code == "OPENAI_KEY_REJECTED"
        assert "--no-gpt" in result.error and "OPENAI_API_KEY" in result.error

    def test_network_problem_does_not_block_the_start(self):
        client = MagicMock()
        client.client.models.list.side_effect = openai.APIConnectionError(request=MagicMock())

        assert real_check_openai_key(client).success

    def test_valid_key_passes(self):
        assert real_check_openai_key(MagicMock()).success


class TestMain:
    def test_shutdown_called_even_on_keyboard_interrupt(self, monkeypatch):
        autopilot = MagicMock()
        autopilot.shutdown.return_value = Result.ok(True)
        monkeypatch.setattr(
            "orchestrator.scripts.run_autopilot.build_autopilot",
            lambda args: Result.ok(autopilot),
        )
        monkeypatch.setattr("orchestrator.scripts.run_autopilot._parse_args", lambda: _args())
        monkeypatch.setattr(
            "orchestrator.scripts.run_autopilot._run_session",
            MagicMock(side_effect=KeyboardInterrupt),
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        autopilot.shutdown.assert_called_once_with("ZT322ZSJVS")

    def test_exit_code_1_when_build_fails(self, monkeypatch):
        monkeypatch.setattr(
            "orchestrator.scripts.run_autopilot.build_autopilot",
            lambda args: Result.fail("bad config", "CONFIG_INVALID"),
        )
        monkeypatch.setattr("orchestrator.scripts.run_autopilot._parse_args", lambda: _args())

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_exit_code_0_on_normal_completion(self, monkeypatch):
        autopilot = MagicMock()
        autopilot.shutdown.return_value = Result.ok(True)
        monkeypatch.setattr(
            "orchestrator.scripts.run_autopilot.build_autopilot",
            lambda args: Result.ok(autopilot),
        )
        monkeypatch.setattr("orchestrator.scripts.run_autopilot._parse_args", lambda: _args())
        monkeypatch.setattr(
            "orchestrator.scripts.run_autopilot._run_session", MagicMock(return_value=0),
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        autopilot.shutdown.assert_called_once()


class TestBuildAutopilotCollaboratorsPatched:
    def test_gpt_locator_uses_injected_client_not_a_new_one(self, tmp_path, monkeypatch):
        """La strategia GPT non deve istanziare il client da sola: usa quello
        gia' costruito da build_autopilot, iniettato al costruttore."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        with patch("orchestrator.scripts.run_autopilot.GPTDecisionClient") as mock_gpt_cls:
            mock_instance = MagicMock()
            mock_gpt_cls.return_value = mock_instance
            result = build_autopilot(_args())
        assert result.success
        gpt_strategy = result.data.locator.strategies["gpt"]
        assert gpt_strategy.gpt_client is mock_instance
