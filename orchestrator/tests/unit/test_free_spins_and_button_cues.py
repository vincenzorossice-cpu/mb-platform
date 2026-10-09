import logging
import os
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import session
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.slot_map import Point, SlotMemory
from orchestrator.modules.slot_autopilot.slot_setup import SlotSetup
from orchestrator.modules.slot_autopilot.slot_warden import SlotWarden
from orchestrator.modules.slot_autopilot.spin_button import SpinButtonState
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import _candidate, _config, _context_result, _tap_outcome
from orchestrator.tests.unit.test_slot_map_learning import FakePhone, SIZE, _good_map


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


# ======================== FIX 1: indizi sul pulsante e zona dei rulli ===============================================

def _button(tmp_path, name, color, with_arrow=True, spinner=False, size=(1080, 2400), center=(540, 1776)):
    path = str(tmp_path / name)
    image = Image.new("RGB", size, (40, 30, 60))
    draw = ImageDraw.Draw(image)
    x, y = center
    draw.ellipse([x - 90, y - 90, x + 90, y + 90], fill=color)
    if with_arrow:
        draw.ellipse([x - 40, y - 40, x + 40, y + 40], outline=(255, 255, 255), width=14)
    if spinner:
        draw.rectangle([x - 85, y - 30, x + 85, y + 30], fill=(10, 10, 10))      # sbarra scura sopra il pulsante
    image.save(path)
    return path


class TestAlternativeCuesWhenTheArrowDoesNotDisappear:
    def _learn(self, tmp_path, idle, busy):
        state = SpinButtonState(memory_dir=str(tmp_path / "mem"), debug_dir=str(tmp_path / "dbg"))
        return state, state.learn("Piggy", SIZE_FULL, 540, 1776, idle, busy)

    def test_a_colour_change_is_learned_as_the_colour_method(self, tmp_path):
        idle = _button(tmp_path, "i.png", (200, 30, 30))
        busy = _button(tmp_path, "b.png", (30, 30, 200))                      # rosso -> blu, freccia sempre li'

        state, outcome = self._learn(tmp_path, idle, busy)

        assert outcome == "learned" and state.method("Piggy") == "color"
        probe = state.classify("Piggy", SIZE_FULL, idle, 540, 1776), state.classify("Piggy", SIZE_FULL, busy, 540, 1776)
        assert probe == (True, False)

    def test_only_the_brightness_changing_is_the_luminance_method(self, tmp_path):
        idle = _button(tmp_path, "i.png", (80, 80, 80))
        busy = _button(tmp_path, "b.png", (50, 50, 50))                       # stesso colore, piu' scuro

        state, outcome = self._learn(tmp_path, idle, busy)

        assert outcome == "learned" and state.method("Piggy") == "luminance"

    def test_a_spinner_over_the_button_is_learned_with_an_alternative_cue(self, tmp_path):
        idle = _button(tmp_path, "i.png", (30, 120, 60), with_arrow=False)
        busy = _button(tmp_path, "b.png", (30, 120, 60), with_arrow=False, spinner=True)

        state, outcome = self._learn(tmp_path, idle, busy)

        assert outcome == "learned" and state.method("Piggy") in ("patch", "luminance", "color")    # un indizio alternativo c'e'

    def test_nothing_changing_falls_back_and_after_three_attempts_gives_up_as_frame_zone(self, tmp_path):
        idle = _button(tmp_path, "i.png", (30, 200, 60))
        busy = _button(tmp_path, "b.png", (30, 200, 60))
        state = SpinButtonState(memory_dir=str(tmp_path / "mem"), debug_dir=str(tmp_path / "dbg"))

        outcomes = [state.learn("Piggy", SIZE_FULL, 540, 1776, idle, busy) for _ in range(3)]

        assert outcomes == ["retry", "retry", "unusable"] and state.method("Piggy") == "frame_zone"
        assert not state.needs_learning("Piggy", SIZE_FULL)

    def test_an_uncertain_frame_is_not_called_ready(self, tmp_path):
        idle = _button(tmp_path, "i.png", (200, 30, 30))
        busy = _button(tmp_path, "b.png", (30, 30, 200))
        middle = _button(tmp_path, "m.png", (90, 120, 90))                    # lontano da entrambi: ne' pronto ne' occupato
        state, _ = self._learn(tmp_path, idle, busy)

        assert state.classify("Piggy", SIZE_FULL, middle, 540, 1776) is None

    def test_mark_unusable_stops_the_learning_attempts_but_never_overwrites_a_learned_reference(self, tmp_path):
        state = SpinButtonState(memory_dir=str(tmp_path / "mem"), debug_dir=str(tmp_path / "dbg"))
        state.mark_unusable("Piggy", SIZE_FULL)
        assert not state.needs_learning("Piggy", SIZE_FULL) and state.method("Piggy") == "frame_zone"

        idle = _button(tmp_path, "i.png", (200, 30, 30))
        busy = _button(tmp_path, "b.png", (30, 30, 200))
        learned = SpinButtonState(memory_dir=str(tmp_path / "mem2"), debug_dir=str(tmp_path / "dbg"))
        learned.learn("Other", SIZE_FULL, 540, 1776, idle, busy)
        learned.mark_unusable("Other", SIZE_FULL)
        assert learned.method("Other") == "color"


SIZE_FULL = (1080, 2400)


class TestReelsZoneWaitWithoutAReference:
    def test_without_the_arrow_the_wait_compares_only_the_reels_zone(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate(strategy_name="gpt")
        locator.is_trusted_strategy.return_value = False
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.90"))
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        controller = SpinController(locator, verifier, step_logger, _config())

        controller.spin("dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal("10.00"))), True,
                        Decimal("0.10"))

        region = verifier.verified_tap.call_args.kwargs["wait_region"]
        assert region == (54, 528, 1026, 1632)                                # 5-95% x 22-68% di 1080x2400

    def test_the_threshold_is_untouched_at_two_percent(self):
        assert AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1")).frame_diff_stable_threshold == 0.02


# ======================== FIX 2 + FIX 3: vigile e free spin ==========================================================

def _sol(*answers):
    gpt = MagicMock()
    gpt._VISION_MAX_TOKENS = 300
    gpt._create_completion.side_effect = [Result.ok(a) for a in answers]
    return gpt


def _d(situation, action="resume", confidence=0.9, **extra):
    data = {"situation": situation, "action": action, "confidence": confidence, "explanation": "x", "wait_seconds": 3}
    data.update(extra)
    return data


@pytest.fixture
def _enc(monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.slot_warden._encode_reduced", lambda p: "x")


class TestWardenBudgetsAndFreeSpinConfirmation:
    def test_budgets_are_25_per_incident_and_50_per_session(self, tmp_path, _enc):
        phone = FakePhone(tmp_path)
        warden = SlotWarden(MagicMock(), phone, phone, spin_point=lambda s: (1, 1))
        assert (warden.max_calls_per_incident, warden.max_calls) == (25, 50)

    def test_sol_confirming_free_spins_returns_free_spins_with_one_call(self, tmp_path, _enc):
        phone = FakePhone(tmp_path)
        gpt = _sol(_d("free_spins", "wait"))
        warden = SlotWarden(gpt, phone, phone, spin_point=lambda s: (1, 1), sleep=lambda s: None)

        assert warden.handle("d", "Piggy", "test") == "free_spins"
        assert gpt._create_completion.call_count == 1 and warden.free_spin_confirmations == 1

    def test_the_prompt_tells_sol_not_to_say_free_spins_without_indicators(self, tmp_path, _enc):
        phone = FakePhone(tmp_path)
        gpt = _sol(_d("normal"))
        SlotWarden(gpt, phone, phone, spin_point=lambda s: (1, 1), sleep=lambda s: None).handle("d", "Piggy", "t")

        system = gpt._create_completion.call_args.args[0][0]["content"]
        assert "ONLY if you SEE" in system and "win equals the bet" in system


class FakeWarden:
    def __init__(self, verdicts):
        self.verdicts, self.calls, self.incidents, self.triggers = list(verdicts), 0, 0, []

    def handle(self, device, slot, trigger, history=None):
        self.incidents += 1
        self.triggers.append(trigger)
        return self.verdicts.pop(0)

    def summary(self):
        return "Vigile: finto"


def _spin(i, outcome="success", before="100", after="99.8", anomaly=False, bet="0.20"):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal(bet),
                                balance_before=Decimal(before) if before is not None else None,
                                balance_after=Decimal(after) if after is not None else None, win_amount=None,
                                verified=outcome == "success", attempts=1, outcome=outcome, anomaly=anomaly))


def _free(i, after="100"):          # tocco senza addebito che cambia lo schermo
    return _spin(i, outcome="no_effect", before="100", after=after, anomaly=True)


def _autopilot(spins, warden, setup=None):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(BetResult(
        slot_name="s", starting_bet=Decimal("1"), final_bet=Decimal("0.2"), min_bet_verified=True, attempts=1))
    autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
    autopilot.spin.side_effect = list(spins)
    autopilot.hitl_strategy = None
    autopilot.slot_setup = setup
    autopilot.warden = warden
    return autopilot


class TestFreeSpinModeInTheSession:
    def test_confirmed_free_spins_do_not_count_and_sol_is_called_once(self, caplog):
        warden = FakeWarden(["free_spins"])
        spins = [_spin(1), _free(2), _free(3), _free(4, after="100.5"), _free(5), _spin(6), _spin(7)]
        autopilot = _autopilot(spins, warden)

        with caplog.at_level(logging.INFO):
            result = session.play_session(autopilot, "d", "slot", Decimal("0.60"), 30)

        assert result.total_played == Decimal("0.60") and result.free_spins == 4        # 3 spin pagati, 4 free
        assert len(warden.triggers) == 1                                                # una sola conferma
        assert "Free spin terminati: 4 spin gratis" in caplog.text and "riprendo gioco normale" in caplog.text

    def test_a_spin_with_a_win_equal_to_the_bet_and_no_free_spin_indicators_is_counted(self, caplog):
        warden = FakeWarden(["resume"])                       # Sol: gioco normale, nessun indizio di free spin
        autopilot = _autopilot([_spin(1), _free(2, after="100"), _spin(3)], warden)

        with caplog.at_level(logging.INFO):
            result = session.play_session(autopilot, "d", "slot", Decimal("0.60"), 30)

        assert result.total_played == Decimal("0.60") and result.free_spins == 0
        assert "vincita pari alla puntata" in caplog.text

    def test_the_case_b_spin_is_not_counted_twice(self):
        autopilot = _autopilot([_free(1, after="100"), _spin(2)], FakeWarden(["resume"]))

        result = session.play_session(autopilot, "d", "slot", Decimal("0.40"), 30)

        assert result.total_played == Decimal("0.40")                                    # 1 (B) + 1 normale

    def test_fifteen_consecutive_free_spins_call_sol_again(self):
        warden = FakeWarden(["free_spins", "free_spins"])
        spins = [_free(i) for i in range(1, 20)] + [_spin(20)]
        autopilot = _autopilot(spins, warden)

        session.play_session(autopilot, "d", "slot", Decimal("0.20"), 100)

        assert len(warden.triggers) == 2 and "15 free spin consecutivi" in warden.triggers[1]

    def test_twenty_five_seconds_without_changes_call_sol_and_it_can_end_the_free_spins(self, monkeypatch, caplog):
        clock = {"now": 1000.0}
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.monotonic", lambda: clock["now"])
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep",
                            lambda s: clock.__setitem__("now", clock["now"] + 10))
        warden = FakeWarden(["free_spins", "resume"])
        idle = _spin(0, outcome="no_effect", before="100", after="100")
        autopilot = _autopilot([_free(1), idle, idle, idle, idle, _spin(6)], warden)

        with caplog.at_level(logging.INFO):
            result = session.play_session(autopilot, "d", "slot", Decimal("0.20"), 100)

        assert any("senza cambiamenti durante i free spin" in t for t in warden.triggers)
        assert result.total_played == Decimal("0.20") and "Free spin terminati" in caplog.text

    def test_a_real_debit_ends_the_free_spins_and_counting_resumes(self):
        warden = FakeWarden(["free_spins"])
        autopilot = _autopilot([_free(1), _free(2), _spin(3), _spin(4)], warden)

        result = session.play_session(autopilot, "d", "slot", Decimal("0.40"), 30)

        assert result.total_played == Decimal("0.40") and result.free_spins == 2


class TestMapRemembersFreeSpinsAndTheSpinMethod:
    def _setup(self, tmp_path):
        phone = FakePhone(tmp_path)
        setup = SlotSetup(SlotMemory(str(tmp_path / "mem")), MagicMock(), phone)
        setup.memory.save(_good_map())
        setup.prepare("d", "Wrap")
        return setup

    def test_free_spins_are_stored_with_a_note_for_the_warden(self, tmp_path):
        setup = self._setup(tmp_path)

        assert setup.mark_free_spins("Wrap").data is True and setup.mark_free_spins("Wrap").data is False
        saved = setup.memory.load("Wrap", SIZE).data
        assert saved.has_free_spins and any("free spins" in note for note in saved.notes)

    def test_the_spin_method_is_stored_and_frame_zone_stops_future_learning(self, tmp_path):
        setup = self._setup(tmp_path)
        button = SpinButtonState(memory_dir=str(tmp_path / "mem"), debug_dir=str(tmp_path / "dbg"))
        setup.button = button
        setup.learn_spin_method("Wrap", "frame_zone")

        assert setup.memory.load("Wrap", SIZE).data.spin_ready_method == "frame_zone"

        next_run = SlotSetup(SlotMemory(str(tmp_path / "mem")), MagicMock(), FakePhone(tmp_path))
        fresh_button = SpinButtonState(memory_dir=str(tmp_path / "other"), debug_dir=str(tmp_path / "dbg"))
        next_run.button = fresh_button
        next_run.prepare("d", "Wrap")
        assert not fresh_button.needs_learning("Wrap", SIZE)                  # nessun nuovo tentativo di apprendimento

    def test_invalid_method_is_ignored(self, tmp_path):
        assert self._setup(tmp_path).learn_spin_method("Wrap", "magia").data is False
