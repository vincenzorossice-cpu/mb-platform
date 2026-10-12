import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import precise_hitl, session
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.slot_warden import SlotWarden, is_forbidden_target, parse_decision
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import _candidate, _config, _context_result, _tap_outcome

SPIN = (540, 1776)


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


class Phone:
    """Screenshot diversi a ogni tocco (lo schermo 'cambia'), o sempre uguali con change_on_tap=False."""
    def __init__(self, tmp_path, change_on_tap=True):
        self.paths = []
        for i, color in enumerate(("white", "gray", "black", "red", "blue", "green")):
            path = str(tmp_path / f"s{i}.png")
            Image.new("RGB", (100, 200), color).save(path)
            self.paths.append(path)
        self.i, self.taps, self.change = 0, [], change_on_tap

    def _screenshot(self, device):
        return Result.ok(self.paths[self.i])

    def tap_coordinates(self, device, x, y):
        self.taps.append((x, y))
        if self.change:
            self.i = min(self.i + 1, len(self.paths) - 1)
        return Result.ok(True)


def _sol(*answers):
    gpt = MagicMock()
    gpt._VISION_MAX_TOKENS = 300
    gpt._create_completion.side_effect = [Result.ok(a) for a in answers]
    return gpt


def _warden(tmp_path, gpt, phone=None, ask=None, **kw):
    phone = phone or Phone(tmp_path)
    notes = []
    warden = SlotWarden(gpt, phone, phone, spin_point=lambda slot: SPIN, describe=kw.pop("describe", None),
                        add_note=lambda slot, text: notes.append(text), ask_text=ask, sleep=lambda s: None, **kw)
    return warden, phone, notes


def _d(action, situation="unknown", confidence=0.9, **extra):
    data = {"situation": situation, "action": action, "confidence": confidence, "explanation": "x", "wait_seconds": 3}
    data.update(extra)
    return data


@pytest.fixture(autouse=True)
def _enc(monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.slot_warden._encode_reduced", lambda p: "x")


class TestDecisionParsing:
    def test_valid_and_invalid_answers(self):
        assert parse_decision(_d("wait", "bonus_running")).action == "wait"
        assert parse_decision({"action": "dance"}) is None and parse_decision("x") is None
        assert parse_decision(_d("wait", wait_seconds=500)).wait_seconds == 30       # tetto

    def test_forbidden_targets(self):
        for text in ("Acquista bonus", "the Buy feature button", "il pulsante Esci", "raddoppia", "cambia puntata"):
            assert is_forbidden_target(text)
        assert not is_forbidden_target("the Start button of the bonus intro")


class TestIncidents:
    def test_a_running_bonus_is_waited_out_without_touching_anything(self, tmp_path):
        gpt = _sol(_d("wait", "bonus_running"), _d("resume", "normal"))
        warden, phone, _ = _warden(tmp_path, gpt)

        assert warden.handle("d", "Wrap", "test") == "resume"
        assert phone.taps == [] and warden.bonus_events == 1 and warden.incidents == 1

    def test_a_bonus_intro_presses_the_known_spin_point_only(self, tmp_path):
        gpt = _sol(_d("tap_spin", "bonus_intro"), _d("wait", "bonus_running"), _d("resume", "normal"))
        warden, phone, _ = _warden(tmp_path, gpt)

        assert warden.handle("d", "Wrap", "test") == "resume"
        assert phone.taps == [SPIN]

    def test_a_forbidden_target_is_never_tapped(self, tmp_path):
        gpt = _sol(*[_d("tap_described", target_description="Acquista bonus")] * 3)
        warden, phone, _ = _warden(tmp_path, gpt, describe=MagicMock(return_value=(10, 10)), ask=lambda p: "stop")

        assert warden.handle("d", "Wrap", "test") == "stop" and phone.taps == []

    def test_three_actions_that_do_not_change_the_screen_go_to_the_operator(self, tmp_path):
        gpt = _sol(*[_d("tap_spin", "bonus_intro")] * 5)
        warden, phone, _ = _warden(tmp_path, gpt, phone=Phone(tmp_path, change_on_tap=False), ask=lambda p: "stop")

        assert warden.handle("d", "Wrap", "test") == "stop" and len(phone.taps) == 3

    def test_low_confidence_asks_the_operator_instead_of_acting(self, tmp_path):
        gpt = _sol(_d("tap_spin", confidence=0.1))
        warden, phone, _ = _warden(tmp_path, gpt, ask=lambda p: "skip")

        assert warden.handle("d", "Wrap", "test") == "resume" and phone.taps == []

    def test_budget_exhausted_without_an_operator_gives_up(self, tmp_path):
        gpt = _sol(*[_d("wait", "bonus_running")] * 5)
        warden, _, _ = _warden(tmp_path, gpt, max_calls_per_incident=2)

        assert warden.handle("d", "Wrap", "test") == "give_up" and gpt._create_completion.call_count == 2

    def test_the_wait_ends_early_when_the_spin_arrow_is_back(self, tmp_path):
        slept = []
        gpt = _sol(_d("wait", "bonus_running", wait_seconds=30), _d("resume", "normal"))
        phone = Phone(tmp_path)
        warden = SlotWarden(gpt, phone, phone, spin_point=lambda s: SPIN, ready_probe=lambda slot, path: True,
                            sleep=slept.append)

        warden.handle("d", "Wrap", "test")

        assert sum(slept) <= 6                       # non ha aspettato i 30 s


class TestTalkingToSol:
    def test_free_text_reaches_sol_and_what_it_learned_is_saved(self, tmp_path):
        gpt = _sol(_d("ask_operator", "unknown"), _d("tap_spin", "bonus_intro"),
                   _d("resume", "normal", learn="When a bonus starts press spin once; free spins then play by themselves."))
        answers = iter(["c'e' un bonus, premi spin una volta e aspetta che finisca"])
        warden, phone, notes = _warden(tmp_path, gpt, ask=lambda prompt: next(answers))

        assert warden.handle("d", "Wrap", "test") == "resume"

        second_prompt = gpt._create_completion.call_args_list[1].args[0][1]["content"][0]["text"]
        assert "INSTRUCTION FROM THE HUMAN OPERATOR" in second_prompt and "premi spin una volta" in second_prompt
        assert phone.taps == [SPIN] and notes == ["When a bonus starts press spin once; free spins then play by themselves."]

    def test_a_note_is_not_saved_when_the_operator_did_not_help(self, tmp_path):
        gpt = _sol(_d("resume", "normal", learn="Some rule."))
        warden, _, notes = _warden(tmp_path, gpt)

        warden.handle("d", "Wrap", "test")

        assert notes == []

    def test_coordinates_skip_and_stop(self, tmp_path):
        gpt = _sol(_d("ask_operator"), _d("resume"))
        warden, phone, _ = _warden(tmp_path, gpt, ask=(lambda it: (lambda prompt: next(it)))(iter(["540,1776"])))
        assert warden.handle("d", "Wrap", "t") == "resume" and phone.taps == [(540, 1776)]

        warden, _, _ = _warden(tmp_path, _sol(_d("ask_operator")), ask=lambda p: "stop")
        assert warden.handle("d", "Wrap", "t") == "stop"
        warden, _, _ = _warden(tmp_path, _sol(_d("ask_operator")), ask=lambda p: "skip")
        assert warden.handle("d", "Wrap", "t") == "resume"

    def test_at_most_six_exchanges_with_the_operator(self, tmp_path):
        gpt = _sol(*[_d("ask_operator")] * 10)
        warden, _, _ = _warden(tmp_path, gpt, ask=lambda p: "boh")

        assert warden.handle("d", "Wrap", "t") == "give_up"
        assert gpt._create_completion.call_count == 7           # la chiamata iniziale + una per ognuno dei 6 scambi

    def test_notes_already_learned_reach_the_prompt(self, tmp_path):
        gpt = _sol(_d("resume"))
        phone = Phone(tmp_path)
        warden = SlotWarden(gpt, phone, phone, spin_point=lambda s: SPIN, notes=lambda slot: ["Press spin once for bonus."],
                            sleep=lambda s: None)

        warden.handle("d", "Wrap", "t")

        assert "Press spin once for bonus." in gpt._create_completion.call_args.args[0][1]["content"][0]["text"]


# ---- P0: il punto di spin buono non si dimentica -----------------------------------------------------------------

class TestGoodSpinPointIsNeverForgotten:
    def _controller(self, tmp_path, protected=None):
        locator = MagicMock()
        locator.locate.return_value = _candidate(strategy_name="memory")
        locator.is_trusted_strategy.return_value = True
        unexpected = _tap_outcome(False)
        unexpected.data.outcome = "unexpected_change"
        verifier = MagicMock()
        verifier.verified_tap.return_value = unexpected
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        controller = SpinController(locator, verifier, step_logger, _config(spin_calm_check=True, slot_memory_dir=str(tmp_path)))
        controller._settle_after_unexpected = lambda *a, **k: None
        controller.verified_spin_point = protected
        return controller, locator

    def _spin(self, controller):
        provider = MagicMock(return_value=_context_result(Decimal("10.00")))
        return controller.spin("dev", "wrap", "ep", 1, provider, True, Decimal("0.10"))

    def test_the_point_verified_in_the_map_is_protected_and_the_result_flags_the_anomaly(self, tmp_path):
        controller, locator = self._controller(tmp_path, protected=lambda slot: (540, 2000))
        from unittest.mock import patch
        with patch("orchestrator.modules.slot_autopilot.spin_controller.forget_autopilot_element") as forget:
            first, second = self._spin(controller), self._spin(controller)

        forget.assert_not_called()
        locator.reject_position.assert_not_called()
        assert first.data.anomaly and second.data.anomaly

    def test_a_point_that_already_debited_is_protected(self, tmp_path):
        controller, locator = self._controller(tmp_path)
        controller._worked_points["wrap"] = {(540, 2000)}
        from unittest.mock import patch
        with patch("orchestrator.modules.slot_autopilot.spin_controller.forget_autopilot_element") as forget:
            self._spin(controller), self._spin(controller)
        forget.assert_not_called()

    def test_a_point_that_never_worked_is_still_forgotten(self, tmp_path):
        controller, locator = self._controller(tmp_path)
        from unittest.mock import patch
        with patch("orchestrator.modules.slot_autopilot.spin_controller.forget_autopilot_element") as forget:
            self._spin(controller), self._spin(controller)
        forget.assert_called_once()


class TestOperatorSpinIsConfirmedByAnyDebit:
    def test_a_description_without_the_word_spin_still_confirms_the_spin_and_does_not_ask_again(self, monkeypatch):
        monkeypatch.setattr(precise_hitl, "ask_human_help", MagicMock(side_effect=[("text", "e' il pulsante verde in basso"),
                                                                                     ("text", "non deve essere richiesto")]))
        monkeypatch.setattr(precise_hitl, "get_image_dimensions", lambda p: (1080, 2400))
        reader = MagicMock()
        reader._screenshot.return_value = Result.ok("/tmp/x.png")
        ocr = MagicMock()
        ocr.read_slot_state.return_value = {"saldo": 25036.8, "posta": 0.2, "raw_texts": [("a", (0, 0), 0.9)]}
        touch = MagicMock()
        monkeypatch.setattr(precise_hitl, "read_ocr_state",
                            lambda *a, **k: {"saldo": 25036.6, "posta": 0.2, "raw_texts": [("a", (0, 0), 0.9)]})

        result = precise_hitl.handle_precise_hitl_escalation(
            reader, ocr, touch, "d", 0.2, 0.2, lambda text, ctx: (562, 1775), element="spin")

        assert result[0] == "spin_confirmed" and touch.tap_coordinates.call_count == 1
        assert precise_hitl.ask_human_help.call_count == 1


# ---- sessione con il vigile ---------------------------------------------------------------------------------------

def _spin(i, anomaly=False, outcome="success"):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal("0.20"), balance_before=Decimal("100"),
                                balance_after=Decimal("99.8"), win_amount=None, verified=outcome == "success",
                                attempts=1, outcome=outcome, anomaly=anomaly))


def _autopilot(spins, warden):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(BetResult(
        slot_name="s", starting_bet=Decimal("1"), final_bet=Decimal("0.2"), min_bet_verified=True, attempts=1))
    autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
    autopilot.spin.side_effect = list(spins)
    autopilot.hitl_strategy = None
    autopilot.slot_setup = None
    autopilot.warden = warden
    return autopilot


class FakeWarden:
    def __init__(self, verdicts):
        self.verdicts, self.calls, self.incidents, self.triggers = list(verdicts), 0, 0, []

    def handle(self, device, slot, trigger, history=None):
        self.incidents += 1
        self.triggers.append(trigger)
        return self.verdicts.pop(0)

    def summary(self):
        return "Vigile: finto"


class TestSessionWithTheWarden:
    def test_an_anomaly_calls_the_warden_before_any_second_tap_and_the_session_resumes(self):
        warden = FakeWarden(["resume"])
        autopilot = _autopilot([_spin(1), _spin(2, anomaly=True, outcome="no_effect"), _spin(3), _spin(4)], warden)

        result = session.play_session(autopilot, "d", "slot", Decimal("0.60"), 30)

        assert not result.failed and result.total_played == Decimal("0.60")      # l'anomalia non conta
        assert len(warden.triggers) == 1 and "senza addebito" in warden.triggers[0]

    def test_the_operator_stop_through_the_warden_ends_the_session(self):
        autopilot = _autopilot([_spin(1, anomaly=True, outcome="no_effect")], FakeWarden(["stop"]))

        result = session.play_session(autopilot, "d", "slot", Decimal("5"), 30)

        assert result.failed and result.stop_reason == "OPERATOR_STOP"

    def test_without_a_warden_the_old_behaviour_is_unchanged(self):
        autopilot = _autopilot([_spin(1, anomaly=True, outcome="no_effect")] * 3, None)

        result = session.play_session(autopilot, "d", "slot", Decimal("5"), 30)

        assert result.stop_reason == "NO_EFFECT"

    def test_the_summary_line_is_written(self, caplog):
        autopilot = _autopilot([_spin(1, anomaly=True, outcome="no_effect"), _spin(2)], FakeWarden(["resume"]))

        with caplog.at_level(logging.INFO):
            session.play_session(autopilot, "d", "slot", Decimal("0.20"), 30)

        assert "Vigile: finto" in caplog.text


# ---- popup "CONGRATULAZIONI! HAI VINTO": tap al centro, senza cercare un elemento ---------------------------------------

POPUP = "Il pannello centrale «CONGRATULAZIONI! HAI VINTO €17,15», con la scritta «PREMI OVUNQUE PER CONTINUARE...»"


class TestWinPopup:
    def test_the_popup_from_the_log_is_closed_with_a_tap_in_the_center_and_remembered(self, tmp_path, caplog):
        gpt = _sol(_d("tap_described", "win_overlay", target_description=POPUP), _d("resume", "normal"))
        describe = MagicMock(return_value=None)                       # "non trovo con precisione"
        warden, phone, notes = _warden(tmp_path, gpt, describe=describe)

        with caplog.at_level(logging.INFO):
            verdict = warden.handle("d", "Wrap", "schermo occupato")

        assert verdict == "resume" and phone.taps == [(50, 100)]       # centro dello screenshot (100x200)
        describe.assert_not_called()
        assert notes == ["popup_win_close: tap_center"]
        assert "popup vincita rilevato, tap al centro per chiudere" in caplog.text

    @pytest.mark.parametrize("phrase", ["CONGRATULAZIONI", "hai vinto 5 euro", "PREMI OVUNQUE", "TAP ANYWHERE",
                                        "TAPPA OVUNQUE", "tap to continue"])
    def test_every_phrase_is_recognised(self, phrase):
        from orchestrator.modules.slot_autopilot.slot_warden import is_win_popup
        assert is_win_popup(phrase) and is_win_popup(None, f"the screen says {phrase}")

    def test_an_ordinary_button_still_goes_through_describe(self, tmp_path):
        gpt = _sol(_d("tap_described", target_description="il pulsante Continua"), _d("resume", "normal"))
        describe = MagicMock(return_value=(30, 40))
        warden, phone, notes = _warden(tmp_path, gpt, describe=describe)

        warden.handle("d", "Wrap", "t")

        describe.assert_called_once()
        assert phone.taps == [(30, 40)] and notes == []

    def test_a_center_tap_that_changes_nothing_saves_no_rule_and_warns_sol(self, tmp_path):
        gpt = _sol(_d("tap_described", "win_overlay", target_description=POPUP),
                   _d("tap_described", "win_overlay", target_description=POPUP), _d("resume", "normal"))
        warden, phone, notes = _warden(tmp_path, gpt, phone=Phone(tmp_path, change_on_tap=False),
                                       describe=MagicMock(return_value=None))

        warden.handle("d", "Wrap", "t")

        assert notes == [] and gpt._create_completion.call_count >= 2
        second_prompt = gpt._create_completion.call_args_list[1].args[0][1]["content"][0]["text"]
        assert "did not close this popup" in second_prompt

    def test_the_fast_lane_never_calls_sol_for_the_popup_itself(self, tmp_path):
        gpt = _sol(_d("resume", "normal"))
        probes = iter([True, False])
        warden, phone, _ = _warden(tmp_path, gpt, notes=lambda slot: ["popup_win_close: tap_center"],
                                   popup_probe=lambda path: next(probes))

        assert warden.handle("d", "Wrap", "t") == "resume"
        assert phone.taps == [(50, 100)] and gpt._create_completion.call_count == 1      # solo il "resume" finale

    def test_a_false_probe_goes_to_sol_as_before(self, tmp_path):
        gpt = _sol(_d("resume", "normal"))
        warden, phone, _ = _warden(tmp_path, gpt, notes=lambda slot: ["popup_win_close: tap_center"],
                                   popup_probe=lambda path: False)

        assert warden.handle("d", "Wrap", "t") == "resume" and phone.taps == []

    def test_the_fast_lane_is_capped_at_three_taps_then_sol_decides(self, tmp_path):
        gpt = _sol(_d("resume", "normal"))
        warden, phone, _ = _warden(tmp_path, gpt, notes=lambda slot: ["popup_win_close: tap_center"],
                                   popup_probe=lambda path: True)

        assert warden.handle("d", "Wrap", "t") == "resume" and len(phone.taps) == 3
        assert warden.calls == 1                                           # le tre corsie veloci non pesano sul budget

    def test_a_forbidden_description_is_never_tapped_even_with_tap_anywhere(self, tmp_path):
        gpt = _sol(*[_d("tap_described", target_description="Acquista bonus - tap anywhere")] * 3)
        warden, phone, notes = _warden(tmp_path, gpt, describe=MagicMock(return_value=(10, 10)), ask=lambda p: "stop")

        assert warden.handle("d", "Wrap", "t") == "stop" and phone.taps == [] and notes == []

    def test_an_unreadable_screenshot_size_falls_back_to_540_1200(self, tmp_path):
        warden, phone, _ = _warden(tmp_path, _sol())
        warden._size = lambda path: None

        assert warden._close_win_popup("d", "Wrap", phone.paths[0], remembered=True) is True
        assert phone.taps == [(540, 1200)]
