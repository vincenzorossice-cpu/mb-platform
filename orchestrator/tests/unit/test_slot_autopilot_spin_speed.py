import random
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import session as session_module
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.session import (
    SlotSessionResult, average_timings, play_session, progress_line, timing_summary_line,
)
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, CalmResult, TapOutcome
from orchestrator.modules.touch_injector.injector import TouchInjector


def _config(**kw):
    return AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"), **kw)


class TestConfigDefaults:
    def test_defaults_for_a_faster_spin(self):
        config = _config()
        assert config.tap_delay_seconds == (0.05, 0.15)
        assert config.frame_diff_poll_interval == 0.05
        assert config.full_read_every_spins == 30
        assert config.spin_skip_calm_after_loss and config.spin_calm_skip_max_age_seconds == 2.0

    @pytest.mark.parametrize("delay", [(0.5, 0.1), (-1, 0.2), (0.1,), (0.1, 0.2, 0.3)])
    def test_invalid_tap_delays_are_rejected(self, delay):
        assert not _config(tap_delay_seconds=delay).validate().success

    def test_invalid_skip_age_is_rejected(self):
        assert not _config(spin_calm_skip_max_age_seconds=0).validate().success


class TestTapDelay:
    def _tap(self, delay, monkeypatch):
        injector = TouchInjector(screen_reader=MagicMock(), tap_delay_seconds=delay) if delay else \
            TouchInjector(screen_reader=MagicMock())
        slept = []
        monkeypatch.setattr("orchestrator.modules.touch_injector.injector.time.sleep", slept.append)
        monkeypatch.setattr("orchestrator.modules.touch_injector.injector.subprocess.run",
                            lambda *a, **k: MagicMock(returncode=0, stderr=""))
        injector.tap_coordinates("d", 10, 10)
        return slept

    def test_the_historical_default_is_unchanged(self, monkeypatch):
        random.seed(1)
        slept = self._tap(None, monkeypatch)
        assert 0.3 <= slept[0] <= 1.5

    def test_a_configured_delay_is_used(self, monkeypatch):
        slept = self._tap((0.05, 0.15), monkeypatch)
        assert 0.05 <= slept[0] <= 0.15


def _spin_controller(**config_kw):
    locator = MagicMock()
    locator.locate.return_value = Result.ok(LocateCandidate(x=518, y=1775, confidence=1.0, strategy_name="profile"))
    locator.is_trusted_strategy.return_value = True
    verifier = MagicMock()
    verifier.wait_until_calm.return_value = Result.ok(CalmResult("p.png", calm=True, waited=False, seconds=0.1))
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    return SpinController(locator, verifier, step_logger, _config(**config_kw)), verifier


def _tap_outcome(balance_before, balance_after):
    return Result.ok(TapOutcome(
        tapped_coords=(518, 1775), verified=True, screenshot_before="a.png", screenshot_after="b.png", diff_score=0.4,
        balance_before=balance_before, balance_after=balance_after, bet_before=Decimal("0.2"), bet_after=Decimal("0.2"),
        screen_state_before="idle", screen_state_after="idle", outcome="success",
        tap_seconds=0.3, wait_seconds=6.0, wait_captures=6, read_seconds=0.7, state_after={"saldo": 1},
    ))


def _context():
    return SimpleNamespace(slot_ocr_state={"saldo": 10.0, "posta": 0.2}, screenshot_path="/x.png", screen_width=1080,
                           screen_height=2400, profile=None)


def _spin(controller, index=1):
    return controller.spin("d", "s", "ep", index, MagicMock(return_value=Result.ok(_context())), True, Decimal("0.2"))


class TestSpinTimings:
    def test_the_result_carries_the_time_of_each_phase(self):
        controller, verifier = _spin_controller()
        verifier.verified_tap.return_value = _tap_outcome(Decimal("10"), Decimal("9.8"))

        timings = _spin(controller).data.timings

        assert timings["tocco"] == 0.3 and timings["attesa"] == 6.0 and timings["scatti"] == 6
        assert timings["lettura"] == 0.7 and set(timings) >= {"contesto", "calma", "altro", "totale"}


class TestSkipCalmAfterALoss:
    def test_the_calm_check_is_skipped_right_after_a_losing_spin(self):
        controller, verifier = _spin_controller()
        verifier.verified_tap.return_value = _tap_outcome(Decimal("10"), Decimal("9.8"))   # perso

        _spin(controller, 1)
        _spin(controller, 2)

        assert verifier.wait_until_calm.call_count == 1       # solo il primo spin

    def test_it_is_not_skipped_after_a_win(self):
        controller, verifier = _spin_controller()
        verifier.verified_tap.return_value = _tap_outcome(Decimal("10"), Decimal("11.0"))  # vincita 1.2

        _spin(controller, 1)
        _spin(controller, 2)

        assert verifier.wait_until_calm.call_count == 2

    def test_it_is_not_skipped_when_the_win_is_unknown(self):
        controller, verifier = _spin_controller()
        verifier.verified_tap.return_value = _tap_outcome(Decimal("10"), None)

        _spin(controller, 1)
        _spin(controller, 2)

        assert verifier.wait_until_calm.call_count == 2

    def test_it_is_not_skipped_if_the_previous_frame_is_old(self, monkeypatch):
        controller, verifier = _spin_controller()
        verifier.verified_tap.return_value = _tap_outcome(Decimal("10"), Decimal("9.8"))
        clock = {"now": 100.0}
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.spin_controller.time.monotonic", lambda: clock["now"])

        _spin(controller, 1)
        clock["now"] += 5.0                                   # piu' dei 2 s ammessi
        _spin(controller, 2)

        assert verifier.wait_until_calm.call_count == 2

    def test_it_can_be_switched_off(self):
        controller, verifier = _spin_controller(spin_skip_calm_after_loss=False)
        verifier.verified_tap.return_value = _tap_outcome(Decimal("10"), Decimal("9.8"))

        _spin(controller, 1)
        _spin(controller, 2)

        assert verifier.wait_until_calm.call_count == 2

    def test_the_skip_is_consumed_by_one_spin(self):
        controller, verifier = _spin_controller()
        verifier.verified_tap.return_value = _tap_outcome(Decimal("10"), Decimal("9.8"))

        _spin(controller, 1)
        _spin(controller, 2)        # salta la calma, ma anche questo e' una perdita: arma il successivo
        _spin(controller, 3)         # salta di nuovo

        assert verifier.wait_until_calm.call_count == 1


class TestSessionTimings:
    def test_the_progress_line_shows_the_breakdown_only_when_available(self):
        plain = SimpleNamespace(bet_amount=Decimal("0.2"), balance_after=Decimal("9.8"), win_amount=None)
        timed = SimpleNamespace(bet_amount=Decimal("0.2"), balance_after=Decimal("9.8"), win_amount=None,
                                timings={"calma": 0.8, "tocco": 1.2, "attesa": 6.1, "scatti": 6, "lettura": 0.7,
                                         "altro": 0.4})

        assert "[" not in progress_line(1, plain, Decimal("0.2"), Decimal("30"), 10)
        line = progress_line(1, timed, Decimal("0.2"), Decimal("30"), 10)
        assert "calma 0.8" in line and "attesa 6.1 (6 scatti)" in line and "lettura 0.7" in line

    def test_a_non_dict_timings_value_is_ignored(self):
        result = SimpleNamespace(bet_amount=Decimal("0.2"), balance_after=None, win_amount=None, timings=MagicMock())

        assert "[" not in progress_line(1, result, Decimal("0.2"), Decimal("30"), 10)

    def test_averages_and_summary(self):
        rows = [{"contesto": 0, "calma": 1.0, "tocco": 1.0, "attesa": 6.0, "lettura": 1.0, "altro": 1.0, "totale": 10.0},
                {"contesto": 0, "calma": 0.0, "tocco": 3.0, "attesa": 8.0, "lettura": 1.0, "altro": 0.0, "totale": 12.0},
                None]

        averages = average_timings(rows)

        assert averages["totale"] == 11.0 and averages["attesa"] == 7.0 and averages["calma"] == 0.5
        assert "Tempo medio per spin (2 spin): totale 11.0 s" in timing_summary_line(averages, 2)
        assert average_timings([None, "x"]) is None

    def test_play_session_logs_the_average_and_returns_it(self, monkeypatch, caplog):
        timings = {"contesto": 0, "calma": 0.5, "tocco": 0.5, "attesa": 6.0, "scatti": 6, "lettura": 0.6,
                   "altro": 0.2, "totale": 7.8}
        spin = Result.ok(SpinResult("s", 1, Decimal("0.2"), Decimal("10"), Decimal("9.8"), None, True, 1, "success",
                                    timings=timings))
        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.return_value = Result.ok(SimpleNamespace(min_bet_verified=True, final_bet=Decimal("0.2"),
                                                                        outcome="x", attempts=1, aborted_reason=None))
        autopilot.spin.return_value = spin
        autopilot.get_balance_summary.return_value = (None, None)

        with caplog.at_level("INFO"):
            result = play_session(autopilot, "d", "s", Decimal("0.4"), 10)

        assert result.avg_timings["totale"] == 7.8
        assert "Tempo medio per spin (2 spin)" in caplog.text
