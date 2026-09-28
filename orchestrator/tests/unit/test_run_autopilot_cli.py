import argparse
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.scripts.run_autopilot import build_autopilot, main


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        device="ZT322ZSJVS", slot="pirots5", target=30.0, max_steps=50,
        dry_run=False, live=False, no_gpt=False, no_hitl=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


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
