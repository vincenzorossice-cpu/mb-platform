import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import session
from orchestrator.modules.slot_autopilot.slot_map import SlotMemory
from orchestrator.modules.slot_autopilot.slot_setup import SlotSetup
from orchestrator.modules.slot_autopilot.spin_controller import SpinController
from orchestrator.modules.slot_autopilot.verifier import TapOutcome
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import _candidate, _config, _context_result
from orchestrator.tests.unit.test_slot_map_learning import FakePhone, SIZE, _good_map

BET = Decimal("0.10")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QUICK_STOP", raising=False)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


class QuickState:
    def __init__(self, value=None):
        self.value, self.calls = value, []

    def quick_stop(self, slot):
        return self.value

    def set_quick_stop(self, slot, enabled, reason):
        self.value = enabled
        self.calls.append((enabled, reason))
        return Result.ok(True)


class FakeVerifier:
    """verified_tap finto: registra tocchi e attese; chiama on_after_tap come fa il verificatore vero."""
    def __init__(self, tmp_path, balance_mid="9.90", balance_after="9.90", moving=False, ocr_ok=True):
        self.taps, self.kwargs = [], []
        self.touch_injector = SimpleNamespace(tap_coordinates=lambda d, x, y: self.taps.append((x, y)) or Result.ok(True))
        self.shots = []
        for name, color in (("s1", "white"), ("s2", "black" if moving else "white")):
            path = str(tmp_path / f"{name}.png")
            Image.new("RGB", (1080, 2400), color).save(path)
            self.shots.append(path)
        self._shot_iter = iter(self.shots)
        self.screen_reader = SimpleNamespace(_screenshot=lambda d: Result.ok(next(self._shot_iter)))
        mid = Decimal(balance_mid) if balance_mid is not None else None
        self.ocr_reader = SimpleNamespace(read_slot_state=lambda p: {"saldo": float(mid)} if (ocr_ok and mid is not None) else {})
        self.balance_after = Decimal(balance_after)

    def verified_tap(self, device_id, x, y, expectation, **kwargs):
        self.taps.append((x, y))                       # tocco 1
        self.kwargs.append(kwargs)
        callback = kwargs.get("on_after_tap")
        if callback is not None:
            callback()
        return Result.ok(TapOutcome(
            tapped_coords=(x, y), verified=True, screenshot_before="/tmp/a.png", screenshot_after="/tmp/b.png",
            diff_score=0.5, balance_before=Decimal("10.00"), balance_after=self.balance_after, bet_before=BET,
            bet_after=BET, screen_state_before="idle", screen_state_after="idle", outcome="success",
        ))


def _controller(tmp_path, state, **verifier_kw):
    locator = MagicMock()
    locator.locate.return_value = _candidate(strategy_name="gpt")
    locator.is_trusted_strategy.return_value = False
    verifier = FakeVerifier(tmp_path, **verifier_kw)
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    controller = SpinController(locator, verifier, step_logger, _config())
    controller.quick_stop_state = state
    return controller, verifier


def _spin(controller, index=1):
    provider = MagicMock(return_value=_context_result(Decimal("10.00")))
    return controller.spin("dev", "wrap", "ep", index, provider, True, BET).data


class TestTheTestOnTheFirstSpin:
    def test_a_second_tap_that_stops_the_reels_enables_quick_stop(self, tmp_path, monkeypatch, caplog):
        sleeps = []
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.spin_controller.time.sleep", sleeps.append)
        state = QuickState()
        controller, verifier = _controller(tmp_path, state, balance_mid="9.90", moving=False)

        with caplog.at_level(logging.INFO):
            result = _spin(controller)

        assert state.calls == [(True, "animazione saltata dal secondo tocco")]
        assert len(verifier.taps) == 2 and "skip_wait" not in verifier.kwargs[0]       # nel test l'attesa e' normale
        assert "Quick Stop test: ABILITATO (saldo sceso 1x, animazione saltata)" in caplog.text
        assert controller.quick_stats["state"] == "enabled" and not result.bet_understated

    def test_a_double_spin_disables_it_and_is_not_a_wrong_bet(self, tmp_path, caplog):
        state = QuickState()
        controller, verifier = _controller(tmp_path, state, balance_mid="9.80", balance_after="9.80")

        with caplog.at_level(logging.INFO):
            result = _spin(controller)

        assert state.calls == [(False, "doppio spin")] and "DISABILITATO (doppio spin" in caplog.text
        assert not result.bet_understated                       # spiegato dal Quick Stop: niente rilettura del minimo

    def test_an_ignored_tap_disables_it(self, tmp_path, caplog):
        state = QuickState()
        controller, _ = _controller(tmp_path, state, balance_mid="9.90", moving=True)

        with caplog.at_level(logging.INFO):
            _spin(controller)

        assert state.calls == [] and "riprovo al prossimo spin con 0.3 s" in caplog.text      # niente DISABILITATO al primo no

    def test_an_unreadable_balance_is_inconclusive_and_nothing_is_saved(self, tmp_path, caplog):
        state = QuickState()
        controller, _ = _controller(tmp_path, state, ocr_ok=False)

        with caplog.at_level(logging.INFO):
            _spin(controller)

        assert state.calls == [] and "NON CONCLUSIVO" in caplog.text and controller.quick_stats["state"] == "untested"

    def test_the_test_runs_only_on_the_first_spin_of_the_session(self, tmp_path):
        state = QuickState()
        controller, verifier = _controller(tmp_path, state, ocr_ok=False)         # inconclusivo: lo stato resta None
        _spin(controller, 1)
        taps_after_first = len(verifier.taps)

        _spin(controller, 2)

        assert len(verifier.taps) - taps_after_first == 1                            # un solo tocco: nessun secondo test


class TestUsingQuickStop:
    def test_with_quick_stop_enabled_the_spin_taps_twice_and_skips_the_wait(self, tmp_path, monkeypatch):
        sleeps = []
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.spin_controller.time.sleep", sleeps.append)
        controller, verifier = _controller(tmp_path, QuickState(True))

        result = _spin(controller)

        assert len(verifier.taps) == 2 and verifier.kwargs[0]["skip_wait"] is True
        assert [round(s, 1) for s in sleeps if s > 0][-2:] == [1.5, 1.5] or sum(sleeps) <= 3.2     # ~3 s di attese
        assert result.verified and controller.quick_stats["spins"] == 1

    def test_with_quick_stop_disabled_there_is_a_single_tap_and_the_old_wait(self, tmp_path):
        controller, verifier = _controller(tmp_path, QuickState(False))

        _spin(controller)

        assert len(verifier.taps) == 1 and "skip_wait" not in verifier.kwargs[0]

    def test_a_double_charge_while_using_it_disables_it_immediately(self, tmp_path, caplog):
        state = QuickState(True)
        controller, _ = _controller(tmp_path, state, balance_after="9.80")           # calo 0.20 con puntata 0.10

        with caplog.at_level(logging.WARNING):
            result = _spin(controller, index=5)

        assert state.calls == [(False, "doppio addebito rilevato")]
        assert "Quick Stop disabilitato: doppio addebito rilevato" in caplog.text
        assert not result.bet_understated and controller.quick_stats["state"] == "disabled"
        controller_after = _spin(controller, index=6)                              # poi si torna al metodo normale
        assert controller_after.verified

    def test_the_env_variable_switches_it_off_completely(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUICK_STOP", "0")
        controller, verifier = _controller(tmp_path, QuickState(True))

        _spin(controller)

        assert len(verifier.taps) == 1

    def test_never_during_free_spins(self, tmp_path):
        controller, verifier = _controller(tmp_path, QuickState(True))
        controller.quick_stop_suspended = True

        _spin(controller)

        assert len(verifier.taps) == 1

    def test_never_without_a_state_object(self, tmp_path):
        controller, verifier = _controller(tmp_path, None)

        _spin(controller)

        assert len(verifier.taps) == 1


class TestMapAndDiagnosis:
    def test_a_map_with_quick_stop_loads_without_testing(self, tmp_path):
        phone = FakePhone(tmp_path)
        setup = SlotSetup(SlotMemory(str(tmp_path / "mem")), MagicMock(), phone)
        setup.memory.save(_good_map(quick_stop=True, quick_stop_tested=True))
        setup.prepare("d", "Wrap")

        assert setup.quick_stop("Wrap") is True
        controller, verifier = _controller(tmp_path, setup)
        controller.quick_stop_state = setup
        provider = MagicMock(return_value=_context_result(Decimal("10.00")))
        controller.spin("dev", "Wrap", "ep", 1, provider, True, BET)
        assert len(verifier.taps) == 2 and verifier.kwargs[0]["skip_wait"] is True      # usato, non provato

    def test_untested_slot_reports_none_and_the_result_is_saved(self, tmp_path):
        phone = FakePhone(tmp_path)
        setup = SlotSetup(SlotMemory(str(tmp_path / "mem")), MagicMock(), phone)
        setup.memory.save(_good_map())
        setup.prepare("d", "Wrap")
        assert setup.quick_stop("Wrap") is None

        setup.set_quick_stop("Wrap", False, "doppio spin")

        reloaded = setup.memory.load("Wrap", SIZE).data
        assert reloaded.quick_stop is False and reloaded.quick_stop_tested and reloaded.quick_stop_reason == "doppio spin"
        assert setup.quick_stop("Wrap") is False

    @pytest.mark.parametrize("stats,expected", [
        ({"state": "enabled", "reason": "", "spins": 10, "seconds": 52.0}, "ABILITATO (10 spin rapidi, ~5.2 s l'uno)"),
        ({"state": "disabled", "reason": "doppio spin", "spins": 0, "seconds": 0.0}, "DISABILITATO (doppio spin)"),
        ({"state": "untested", "reason": "", "spins": 0, "seconds": 0.0}, "NON TESTATO"),
    ])
    def test_the_diagnosis_line(self, stats, expected, caplog):
        autopilot = SimpleNamespace(spin_controller=SimpleNamespace(quick_stats=stats))

        with caplog.at_level(logging.INFO):
            session._log_quick_stats(autopilot, logging.getLogger("x"))

        assert f"Quick Stop: {expected}" in caplog.text

    def test_the_session_suspends_quick_stop_during_free_spins(self):
        from orchestrator.modules.slot_autopilot.bet_controller import BetResult
        from orchestrator.modules.slot_autopilot.spin_controller import SpinResult

        def spin(i, anomaly=False, outcome="success"):
            return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal("0.2"), balance_before=Decimal("100"),
                                        balance_after=Decimal("99.8") if outcome == "success" else Decimal("100"),
                                        win_amount=None, verified=outcome == "success", attempts=1, outcome=outcome,
                                        anomaly=anomaly))
        flags = []
        controller = SimpleNamespace(quick_stop_suspended=False)
        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.return_value = Result.ok(BetResult(
            slot_name="s", starting_bet=Decimal("1"), final_bet=Decimal("0.2"), min_bet_verified=True, attempts=1))
        autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
        spins = iter([spin(1, anomaly=True, outcome="no_effect"), spin(2, anomaly=True, outcome="no_effect"), spin(3), spin(4)])

        def fake_spin(*a, **k):
            flags.append(controller.quick_stop_suspended)
            return next(spins)
        autopilot.spin.side_effect = fake_spin
        autopilot.spin_controller = controller
        autopilot.hitl_strategy = None
        autopilot.slot_setup = None
        autopilot.warden = SimpleNamespace(calls=0, incidents=0, handle=lambda *a, **k: "free_spins", summary=lambda: "")

        session.play_session(autopilot, "d", "slot", Decimal("0.40"), 30)

        assert flags[0] is False and flags[1] is True                             # sospeso appena entrati nei free spin


# ---- secondo tocco back-to-back, delay configurabile, riprova a 0.3 s ------------------------------------------------

def _traced(controller, verifier, monkeypatch):
    """Registro ordinato di tocchi, attese e screenshot; il tempo finto avanza con le attese."""
    events, clock = [], [0.0]
    original_verified = verifier.verified_tap
    verifier.verified_tap = lambda *a, **k: (events.append(("tap", clock[0])), original_verified(*a, **k))[1]
    original_tap = verifier.touch_injector.tap_coordinates
    verifier.touch_injector.tap_coordinates = lambda d, x, y: (events.append(("tap", clock[0])), original_tap(d, x, y))[1]
    original_shot = verifier.screen_reader._screenshot
    verifier.screen_reader._screenshot = lambda d: (events.append(("shot", clock[0])), original_shot(d))[1]

    def fake_sleep(seconds):
        events.append(("sleep", seconds))
        clock[0] += seconds
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.spin_controller.time.sleep", fake_sleep)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.spin_controller.time.monotonic", lambda: clock[0])
    controller.button = MagicMock()
    controller.button.reference.return_value = None
    controller.button.needs_learning.return_value = True
    controller._sample_busy_button = lambda d, h: events.append(("busy_sample", clock[0]))
    return events


class TestBackToBackTaps:
    def test_tap_sleep_tap_and_only_then_the_screenshots(self, tmp_path, monkeypatch):
        controller, verifier = _controller(tmp_path, QuickState(), balance_mid="9.90")
        events = _traced(controller, verifier, monkeypatch)

        _spin(controller)

        kinds = [e[0] for e in events]
        first_tap, second_tap = (i for i, k in enumerate(kinds) if k == "tap")
        assert kinds[first_tap + 1:second_tap] == ["sleep"] and events[first_tap + 1][1] == pytest.approx(0.5)
        assert kinds[second_tap + 1:].count("shot") == 2 and "shot" not in kinds[:second_tap]

    def test_nothing_runs_between_the_two_taps_even_while_learning_the_button(self, tmp_path, monkeypatch):
        controller, verifier = _controller(tmp_path, QuickState(), balance_mid="9.90")
        events = _traced(controller, verifier, monkeypatch)

        _spin(controller)

        kinds = [e[0] for e in events]
        first_tap = kinds.index("tap")
        second_tap = kinds.index("tap", first_tap + 1)
        assert set(kinds[first_tap + 1:second_tap]) == {"sleep"}           # niente shot, niente busy_sample

    def test_the_gap_between_the_taps_is_under_a_second(self, tmp_path, monkeypatch):
        controller, verifier = _controller(tmp_path, QuickState(), balance_mid="9.90")
        events = _traced(controller, verifier, monkeypatch)

        _spin(controller)

        taps = [e[1] for e in events if e[0] == "tap"]
        assert taps[1] - taps[0] < 1.0

    def test_a_late_second_tap_proves_nothing(self, tmp_path, monkeypatch, caplog):
        state = QuickState()
        controller, verifier = _controller(tmp_path, state, balance_mid="9.90")
        _traced(controller, verifier, monkeypatch)
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.spin_controller.quick_stop_delay", lambda: 1.4)

        with caplog.at_level(logging.INFO):
            _spin(controller)

        assert state.calls == [] and "NON CONCLUSIVO (secondo tocco arrivato dopo" in caplog.text


class TestDelayAndRetry:
    def test_the_delay_comes_from_the_environment(self, monkeypatch):
        from orchestrator.modules.slot_autopilot.config import quick_stop_delay
        assert quick_stop_delay() == 0.5
        monkeypatch.setenv("QUICK_STOP_DELAY", "0.8")
        assert quick_stop_delay() == 0.8
        for bad in ("abc", "0", "9"):
            monkeypatch.setenv("QUICK_STOP_DELAY", bad)
            assert quick_stop_delay() == 0.5

    def test_ignored_at_half_a_second_retries_at_0_3_then_disables(self, tmp_path, monkeypatch, caplog):
        state = QuickState()
        controller, verifier = _controller(tmp_path, state, balance_mid="9.90", moving=True)
        events = _traced(controller, verifier, monkeypatch)

        with caplog.at_level(logging.INFO):
            _spin(controller, 1)
            assert state.calls == []                                   # niente salvato dopo il primo "ignorato"
            verifier._shot_iter = iter(verifier.shots)
            events.clear()
            _spin(controller, 2)

        assert events[[e[0] for e in events].index("sleep")][1] == pytest.approx(0.3)
        assert state.calls == [(False, "tocco ignorato (0.5 s e 0.3 s)")]
        verifier._shot_iter = iter(verifier.shots)
        events.clear()
        _spin(controller, 3)
        assert [e[0] for e in events].count("tap") == 1          # disabilitato: un solo tocco, nessun secondo

    def test_a_double_spin_does_not_retry(self, tmp_path):
        state = QuickState()
        controller, _ = _controller(tmp_path, state, balance_mid="9.80", balance_after="9.80")

        _spin(controller)

        assert state.calls == [(False, "doppio spin")] and not controller._quick_retry
