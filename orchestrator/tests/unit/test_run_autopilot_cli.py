import argparse
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from orchestrator.core.result import Result
from orchestrator.scripts.run_autopilot import _check_openai_key as real_check_openai_key
from orchestrator.scripts.run_autopilot import _configure_logging as real_configure_logging
from orchestrator.scripts.run_autopilot import _progress_line
from orchestrator.scripts.run_autopilot import build_autopilot, main


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        device="ZT322ZSJVS", slot="pirots5", target=30.0, max_steps=50,
        dry_run=False, live=False, no_gpt=False, no_hitl=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture(autouse=True)
def _no_log_files(monkeypatch):
    """main() non deve creare cartelle di log nel repo durante i test."""
    monkeypatch.setattr("orchestrator.scripts.run_autopilot._configure_logging", lambda log_dir, slot: "test.log")


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

    def test_hitl_gets_the_gpt_strategy_as_describer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        strategies = build_autopilot(_args()).data.locator.strategies
        assert strategies["hitl"].describer is strategies["gpt"]

    def test_hitl_without_gpt_has_no_describer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        strategies = build_autopilot(_args(no_gpt=True)).data.locator.strategies
        assert strategies["hitl"].describer is None

    def test_with_gpt_the_ocr_reader_gets_the_vision_safety_net(self, tmp_path, monkeypatch):
        from orchestrator.modules.slot_ocr.vision_reader import ResilientOCRReader
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        autopilot = build_autopilot(_args()).data

        assert isinstance(autopilot.ocr_reader, ResilientOCRReader) and autopilot.ocr_reader.vision is not None

    def test_without_gpt_the_ocr_reader_is_the_plain_one(self, tmp_path, monkeypatch):
        from orchestrator.modules.slot_ocr.reader import SlotOCRReader
        monkeypatch.chdir(tmp_path)

        autopilot = build_autopilot(_args(no_gpt=True)).data

        assert isinstance(autopilot.ocr_reader, SlotOCRReader)

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


class TestSpinLoopStopsWithoutEffect:
    def _autopilot(self, outcomes):
        from orchestrator.modules.slot_autopilot.bet_controller import BetResult
        from orchestrator.modules.slot_autopilot.spin_controller import SpinResult
        from decimal import Decimal

        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.return_value = Result.ok(BetResult(
            slot_name="s", starting_bet=Decimal("2"), final_bet=Decimal("0.1"), min_bet_verified=True, attempts=1,
        ))
        autopilot.get_balance_summary.return_value = (None, None)
        autopilot.spin.side_effect = [
            Result.ok(SpinResult(
                slot_name="s", spin_index=i + 1, bet_amount=Decimal("0.1"), balance_before=None,
                balance_after=None, win_amount=None, verified=outcome == "success", attempts=1, outcome=outcome,
            )) for i, outcome in enumerate(outcomes)
        ]
        return autopilot

    def test_three_consecutive_no_effect_spins_stop_the_session(self):
        from orchestrator.scripts.run_autopilot import _run_session
        autopilot = self._autopilot(["no_effect"] * 10)

        code = _run_session(autopilot, _args(target=30.0, max_steps=50))

        assert code == 1
        assert autopilot.spin.call_count == 3

    def test_a_repeatedly_understated_bet_stops_the_session_after_two_rereads(self):
        from orchestrator.scripts.run_autopilot import _run_session
        autopilot = self._autopilot(["success"] * 5)
        results = list(autopilot.spin.side_effect)
        for result in results[:3]:
            result.data.bet_understated = True
        autopilot.spin.side_effect = iter(results)

        code = _run_session(autopilot, _args(target=30.0, max_steps=50))

        # le prime due volte la puntata viene riletta e si prosegue; alla terza ci si ferma
        assert code == 1 and autopilot.spin.call_count == 3

    def test_a_successful_spin_resets_the_count(self):
        from orchestrator.scripts.run_autopilot import _run_session
        autopilot = self._autopilot(["no_effect", "no_effect", "success", "no_effect", "no_effect", "success"])

        code = _run_session(autopilot, _args(target=0.3, max_steps=6))

        assert code == 0
        assert autopilot.spin.call_count == 6


class TestLogging:
    @pytest.fixture(autouse=True)
    def _restore_logging(self):
        import logging
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        yield
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)

    def test_full_detail_goes_to_the_file_and_only_progress_and_warnings_to_the_console(self, tmp_path, capsys):
        import logging
        path = real_configure_logging(str(tmp_path / "logs"), "Big Bass Bonanza")

        logging.getLogger("orchestrator.scripts.run_autopilot").info("Spin 1 | puntata 0.10")
        logging.getLogger("httpx2").info("HTTP Request: POST https://api.openai.com/...")
        logging.getLogger("orchestrator.modules.slot_autopilot.verifier").info("verified_tap: tap (1,2)")
        logging.getLogger("orchestrator.modules.slot_autopilot.locator").warning("attenzione: punto scartato")
        for handler in logging.getLogger().handlers:
            handler.flush()

        console = capsys.readouterr().err
        assert "Spin 1 | puntata 0.10" in console and "attenzione: punto scartato" in console
        assert "HTTP Request" not in console and "verified_tap" not in console
        detail = open(path, encoding="utf-8").read()
        assert "Spin 1" in detail and "HTTP Request" in detail and "verified_tap" in detail

    def test_log_file_name_carries_the_slot_and_the_folder_is_created(self, tmp_path):
        path = real_configure_logging(str(tmp_path / "nuova" / "cartella"), "Big Bass Bonanza")

        assert "autopilot_big_bass_bonanza_" in path and path.endswith(".log")
        assert (tmp_path / "nuova" / "cartella").is_dir()

    def test_harmless_torch_warnings_are_ignored(self, tmp_path):
        import warnings
        real_configure_logging(str(tmp_path), "s")

        with warnings.catch_warnings(record=True) as caught:
            warnings.warn("'pin_memory' argument is set as true but no accelerator is found", UserWarning)
            warnings.warn("torch.quantize_per_tensor is deprecated", UserWarning)
        assert caught == []


class TestProgressLine:
    def _result(self, **overrides):
        from decimal import Decimal
        from orchestrator.modules.slot_autopilot.spin_controller import SpinResult
        data = dict(
            slot_name="s", spin_index=9, bet_amount=Decimal("0.1"), balance_before=None,
            balance_after=Decimal("99999.15"), win_amount=Decimal("0.05"), verified=True, attempts=1, outcome="success",
        )
        data.update(overrides)
        return SpinResult(**data)

    def test_simple_one_line_summary(self):
        from decimal import Decimal
        line = _progress_line(9, self._result(), Decimal("0.90"), Decimal("30"), 13.6)

        assert line == "Spin 9 | puntata 0.10 | saldo 99999.15 | vincita 0.05 | giocato 0.90/30.00 | 14 s"

    def test_missing_values_are_shown_plainly(self):
        from decimal import Decimal
        line = _progress_line(1, self._result(bet_amount=None, balance_after=None, win_amount=None), Decimal("0"), Decimal("30"), 2)

        assert "puntata -" in line and "saldo -" in line and "vincita 0.00" in line


class TestBetFailureMessages:
    @pytest.mark.parametrize("reason,expected", [
        ("BET_UNREADABLE", "non tocco nulla"),
        ("MONEY_MOVED", "hanno giocato uno spin"),
        ("MIN_NOT_PROVEN", "non spinno"),
        ("NO_DECREASE_OBSERVED", "mi fermo senza spinnare"),
    ])
    def test_each_reason_explains_itself(self, reason, expected, caplog):
        from decimal import Decimal
        from orchestrator.modules.slot_autopilot.bet_controller import BetResult
        from orchestrator.scripts.run_autopilot import _run_session

        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.return_value = Result.ok(BetResult(
            slot_name="s", starting_bet=None, final_bet=None, min_bet_verified=False, attempts=0,
            aborted_reason=reason, outcome="aborted",
        ))

        with caplog.at_level("ERROR"):
            code = _run_session(autopilot, _args(target=30.0, max_steps=50))

        assert code == 1
        assert any(reason in r.getMessage() and expected in r.getMessage() for r in caplog.records)
        autopilot.spin.assert_not_called()


class TestWaitForBusyScreen:
    @pytest.fixture
    def clock(self, monkeypatch):
        """Orologio finto: ogni sleep lo fa avanzare, nessuna attesa reale."""
        now = [1000.0]
        sleeps = []
        monkeypatch.setattr("orchestrator.scripts.run_autopilot.time.monotonic", lambda: now[0])

        def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        monkeypatch.setattr("orchestrator.scripts.run_autopilot.time.sleep", fake_sleep)
        return sleeps

    def _autopilot(self, outcomes):
        from decimal import Decimal
        from orchestrator.modules.slot_autopilot.bet_controller import BetResult
        from orchestrator.modules.slot_autopilot.spin_controller import SpinResult

        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.return_value = Result.ok(BetResult(
            slot_name="s", starting_bet=Decimal("2"), final_bet=Decimal("0.1"), min_bet_verified=True, attempts=1,
        ))
        autopilot.get_balance_summary.return_value = (None, None)
        results = []
        for i, outcome in enumerate(outcomes):
            violated = None
            if outcome.startswith("blocked:"):
                outcome, violated = "blocked", outcome.split(":")[1]
            results.append(Result.ok(SpinResult(
                slot_name="s", spin_index=i + 1, bet_amount=Decimal("0.1"), balance_before=None,
                balance_after=None, win_amount=None, verified=outcome == "success", attempts=1,
                outcome=outcome, violated_invariant=violated,
            )))
        autopilot.spin.side_effect = results
        return autopilot

    def test_a_busy_screen_is_waited_out_and_the_session_continues(self, clock):
        from orchestrator.scripts.run_autopilot import _run_session
        autopilot = self._autopilot(["blocked:UNSAFE_SCREEN_STATE"] * 2 + ["success", "success"])

        code = _run_session(autopilot, _args(target=0.2, max_steps=4))

        assert code == 0
        assert autopilot.spin.call_count == 4 and clock == [5, 5]

    def test_waiting_does_not_consume_max_steps(self, clock):
        from orchestrator.scripts.run_autopilot import _run_session
        autopilot = self._autopilot(["blocked:UNSAFE_SCREEN_STATE"] * 3 + ["success"])

        code = _run_session(autopilot, _args(target=0.1, max_steps=1))

        assert code == 0 and autopilot.spin.call_count == 4

    def test_a_screen_busy_for_too_long_stops_the_session_with_a_failure_code(self, clock):
        from orchestrator.scripts.run_autopilot import _run_session
        autopilot = self._autopilot(["blocked:UNSAFE_SCREEN_STATE"] * 40)

        code = _run_session(autopilot, _args(target=30.0, max_steps=200))

        assert code == 1
        assert sum(clock) >= 120 and autopilot.spin.call_count < 40

    def test_other_blocks_stop_immediately(self, clock):
        from orchestrator.scripts.run_autopilot import _run_session
        autopilot = self._autopilot(["blocked:INSUFFICIENT_BALANCE", "success"])

        _run_session(autopilot, _args(target=30.0, max_steps=200))

        assert autopilot.spin.call_count == 1 and clock == []

    def test_the_busy_clock_restarts_after_a_successful_spin(self, clock):
        from orchestrator.scripts.run_autopilot import _run_session
        busy = ["blocked:UNSAFE_SCREEN_STATE"] * 20
        autopilot = self._autopilot(busy + ["success"] + busy + ["success"])

        code = _run_session(autopilot, _args(target=0.2, max_steps=50))

        assert code == 0 and autopilot.spin.call_count == 42


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
