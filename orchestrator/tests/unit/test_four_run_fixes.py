from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_value, save_autopilot_value
from orchestrator.modules.slot_autopilot import session
from orchestrator.modules.slot_autopilot.bet_controller import BetController, BetResult
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.slot_map import SlotMemory
from orchestrator.modules.slot_autopilot.slot_setup import SlotSetup
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.supervisor import SlotSupervisor
from orchestrator.tests.unit.test_slot_autopilot_bet_menu import MAIN_BOXES, _box, _context, _provider, _tap
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import (
    _candidate, _config, _context_result, _tap_outcome,
)
from orchestrator.tests.unit.test_slot_map_learning import FakePhone, _good_map


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.bet_menu.time.sleep", lambda s: None)


# ---- P1: Sol da solo non basta per dire "persa" ----------------------------------------------------------------

class TestLostNeedsTheBalanceToBeUnreadable:
    def _supervisor(self, probe_answers, in_game=False):
        gpt = MagicMock()
        gpt._VISION_MAX_TOKENS = 300
        gpt._create_completion.return_value = Result.ok({"screen_state": "other", "in_game": in_game, "action": "none"})
        reader = MagicMock()
        reader._screenshot.return_value = Result.ok("/tmp/x.png")
        probe = MagicMock(side_effect=list(probe_answers))
        return SlotSupervisor(gpt, reader, MagicMock(), balance_probe=probe, probe_pause_seconds=0), probe

    @pytest.fixture(autouse=True)
    def _enc(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor._encode_reduced", lambda p: "x")
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor.time.sleep", lambda s: None)

    def test_sol_says_lost_but_the_balance_reads_so_the_slot_is_alive(self):
        supervisor, probe = self._supervisor([True])
        assert supervisor.check_in_game("d") == "in_game" and probe.call_count == 1

    def test_lost_needs_two_unreadable_probes(self):
        supervisor, probe = self._supervisor([False, False])
        assert supervisor.check_in_game("d") == "lost" and probe.call_count == 2

    def test_an_animation_that_hides_the_balance_for_a_moment_is_not_enough(self):
        supervisor, _ = self._supervisor([False, True])
        assert supervisor.check_in_game("d") == "in_game"

    def test_without_a_probe_sol_alone_decides_as_before(self):
        supervisor, _ = self._supervisor([])
        supervisor.balance_probe = None
        assert supervisor.check_in_game("d") == "lost"

    def test_check_ready_uses_the_same_guard(self):
        supervisor, _ = self._supervisor([True])
        assert supervisor.check_ready("d") != "lost"


# ---- P2: dopo il rientro nessuna navigazione per errori di puntata ------------------------------------------

def _bet(verified=True, final="0.2"):
    return Result.ok(BetResult(slot_name="s", starting_bet=Decimal("1.4"), final_bet=Decimal(final),
                               min_bet_verified=verified, attempts=1, aborted_reason=None if verified else "NO_DECREASE_OBSERVED"))


def _ok_spin(i, bet="0.20", before="100", after="99.8"):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal(bet), balance_before=Decimal(before),
                                balance_after=Decimal(after), win_amount=None, verified=True, attempts=1,
                                outcome="success"))


def _lost(i=1):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal("0.2"), balance_before=None,
                                balance_after=None, win_amount=None, verified=False, attempts=0, outcome="slot_lost"))


class _Hitl:
    def __init__(self, answers):
        self.answers, self.interventions, self.reasons = list(answers), 0, []

    def ask_for_help(self, device, reason, bet=None):
        self.reasons.append(reason)
        return self.answers.pop(0)


def _autopilot(spins, hitl=None):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = _bet()
    autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
    autopilot.spin.side_effect = list(spins)
    autopilot.hitl_strategy = hitl
    autopilot.slot_setup = None
    return autopilot


class TestAfterTheReentry:
    def test_a_bet_problem_after_a_good_reentry_does_not_navigate_again(self):
        hitl = _Hitl(["hint"])
        autopilot = _autopilot([_ok_spin(1), _lost(2), _ok_spin(3), _ok_spin(4)], hitl)
        autopilot.set_min_bet.side_effect = [_bet(), _bet(verified=False), _bet()]
        reenter = MagicMock(return_value=Result.ok(True))

        result = session.play_session(autopilot, "d", "slot", Decimal("0.60"), 30, reenter=reenter)

        assert not result.failed and reenter.call_count == 1 and result.reentries == 1
        assert hitl.reasons == ["non riesco a impostare la puntata dopo il rientro nella slot."]

    def test_without_an_operator_a_bet_problem_after_the_reentry_stops_without_more_navigation(self):
        autopilot = _autopilot([_ok_spin(1), _lost(2)])
        autopilot.set_min_bet.side_effect = [_bet(), _bet(verified=False)]
        reenter = MagicMock(return_value=Result.ok(True))

        result = session.play_session(autopilot, "d", "slot", Decimal("5"), 30, reenter=reenter)

        assert result.stop_reason == "SLOT_LOST" and reenter.call_count == 1

    def test_the_map_is_reseeded_after_the_reentry(self, tmp_path):
        phone = FakePhone(tmp_path)
        setup = SlotSetup(SlotMemory(str(tmp_path / "mem")), MagicMock(), phone)
        setup.memory.save(_good_map())
        reseed = MagicMock(wraps=setup.reseed)
        setup.reseed = reseed
        autopilot = _autopilot([_ok_spin(1), _lost(2), _ok_spin(3)])
        autopilot.slot_setup = setup

        session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 30, reenter=MagicMock(return_value=Result.ok(True)))

        reseed.assert_called_once_with("Wrap")

    def test_known_minimum_and_a_bet_already_at_it_means_no_taps_after_the_reentry(self, tmp_path):
        config = AutoPilotConfig(device_id="d", slot_name="wrap", target_played_amount=Decimal("1"),
                                 slot_memory_dir=str(tmp_path), known_min_bet=Decimal("0.20"))
        controller = BetController(MagicMock(), MagicMock(), MagicMock(), config, menu_selector=MagicMock())
        controller.forget_session_min("wrap")
        context = SimpleNamespace(profile=None, slot_ocr_state={"posta": 0.2}, screen_width=1080, screen_height=2400,
                                  text_boxes=[], slot_name="wrap")

        result = controller._set_min_bet("d", "wrap", "ep", MagicMock(return_value=Result.ok(context)))

        assert result.data.outcome == "unchanged_already_min" and result.data.min_bet_verified
        controller.verifier.verified_tap.assert_not_called()


# ---- P3: eventi bonus ----------------------------------------------------------------------------------------------

class TestBonusEvents:
    def _controller(self, balance_after):
        locator = MagicMock()
        locator.locate.return_value = _candidate(strategy_name="gpt")
        locator.is_trusted_strategy.return_value = False
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal(balance_after))
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        return SpinController(locator, verifier, step_logger, _config())

    def _spin(self, controller):
        provider = MagicMock(return_value=_context_result(Decimal("10.00")))
        return controller.spin("dev", "pirots5", "ep", 1, provider, True, Decimal("0.10"))

    def test_a_rise_in_a_normal_sequence_is_a_winning_spin(self):
        controller = self._controller("11.00")              # +1.00 = vincita 1.10 - puntata 0.10

        result = self._spin(controller)

        assert result.data.outcome == "success" and result.data.win_amount == Decimal("1.10")

    def test_a_rise_right_after_an_anomaly_is_a_bonus_credit_not_a_spin(self):
        controller = self._controller("11.00")
        controller._anomaly["pirots5"] = True

        result = self._spin(controller)

        assert result.data.outcome == "bonus_event"

    def test_a_real_debit_clears_the_anomaly(self):
        controller = self._controller("9.90")
        controller._anomaly["pirots5"] = True

        result = self._spin(controller)

        assert result.data.outcome == "success" and "pirots5" not in controller._anomaly

    def test_session_does_not_count_bonus_events_and_goes_on(self):
        bonus = Result.ok(SpinResult(slot_name="s", spin_index=1, bet_amount=Decimal("0.2"), balance_before=Decimal("99"),
                                     balance_after=Decimal("100"), win_amount=None, verified=False, attempts=1,
                                     outcome="bonus_event"))
        autopilot = _autopilot([_ok_spin(1), bonus, _ok_spin(3)])

        result = session.play_session(autopilot, "d", "slot", Decimal("0.40"), 30)

        assert result.total_played == Decimal("0.40") and not result.failed

    def test_five_bonus_events_in_a_row_call_the_operator_or_stop(self):
        bonus = Result.ok(SpinResult(slot_name="s", spin_index=1, bet_amount=Decimal("0.2"), balance_before=Decimal("99"),
                                     balance_after=Decimal("100"), win_amount=None, verified=False, attempts=1,
                                     outcome="bonus_event"))
        result = session.play_session(_autopilot([bonus] * 6), "d", "slot", Decimal("5"), 30)

        assert result.failed and result.stop_reason == "BLOCKED"


# ---- P4: carosello: tocca, verifica, correggi ---------------------------------------------------------------------

class TestCarouselPick:
    def _selector(self, tmp_path):
        locator = MagicMock()
        locator.is_trusted_strategy.return_value = True
        locator.memory_dir = str(tmp_path)
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap(True)
        verifier.touch_injector.swipe.return_value = Result.ok(True)
        return BetMenuSelector(locator, verifier, scroll_probe=False), verifier

    A = [_box("0,20", 100, 1975), _box("0,40", 300, 1975), _box("1,00", 500, 1975), _box("1,40", 700, 1975)]
    SHIFTED = [_box("0,10", 100, 1975), _box("0,20", 500, 1975), _box("0,40", 300, 1975), _box("1,00", 700, 1975)]

    def test_a_row_that_recenters_after_the_tap_gets_a_second_tap_on_the_right_cell(self, tmp_path):
        selector, verifier = self._selector(tmp_path)
        provider = _provider(_context(self.SHIFTED, posta=0.40), _context(MAIN_BOXES, posta=0.20))

        result = selector.select_from_open_menu("d", "wrap", _context(self.A), provider, target=Decimal("0.20"))

        assert result.success and result.data.final_bet == Decimal("0.20")
        taps = [c.args[1:3] for c in verifier.verified_tap.call_args_list]
        assert taps == [(100, 1975), (500, 1975)]

    def test_the_declared_minimum_is_the_wanted_cell_not_just_the_lowest_one(self, tmp_path):
        selector, verifier = self._selector(tmp_path)
        provider = _provider(_context(MAIN_BOXES, posta=0.40))

        result = selector.select_from_open_menu("d", "wrap", _context(self.A), provider, target=Decimal("0.40"))

        assert result.success and verifier.verified_tap.call_args_list[0].args[1:3] == (300, 1975)

    def test_it_gives_up_after_three_taps(self, tmp_path):
        selector, verifier = self._selector(tmp_path)
        always_wrong = [_context(self.SHIFTED, posta=0.40) for _ in range(6)]

        selector.select_from_open_menu("d", "wrap", _context(self.A), _provider(*always_wrong), target=Decimal("0.20"))

        assert verifier.verified_tap.call_count <= 3 + 2        # 3 selezioni al massimo (+ eventuali chiusure)

    def test_the_scroll_direction_that_worked_is_remembered_and_tried_first(self, tmp_path):
        selector, verifier = self._selector(tmp_path)
        selector.scroll_probe = True
        cut = [_box("1,00", 300, 1975), _box("1,40", 500, 1975), _box("2,00", 700, 1975), _box("3,00", 900, 1975)]
        low = [_box("0,40", 300, 1975), _box("0,50", 500, 1975), _box("1,00", 700, 1975), _box("1,40", 900, 1975)]
        provider = _provider(_context(low), _context(low), _context(MAIN_BOXES, posta=0.40))

        selector.select_from_open_menu("d", "wrap", _context(cut), provider)

        assert load_autopilot_value("wrap", "bet_row_direction", str(tmp_path)) == "left"
        # la volta dopo, con "right" ricordato, il primo gesto va a destra
        save_autopilot_value("wrap", "bet_row_direction", "right", str(tmp_path))
        verifier.touch_injector.swipe.reset_mock()
        selector2, verifier2 = self._selector(tmp_path)
        selector2.scroll_probe = True
        selector2.select_from_open_menu("d", "wrap", _context(cut), _provider(
            _context(low), _context(low), _context(MAIN_BOXES, posta=0.40)))
        first = verifier2.touch_injector.swipe.call_args_list[0].args
        assert first[1] < first[3]


def test_the_scroll_direction_enters_the_map(tmp_path):
    phone = FakePhone(tmp_path)
    setup = SlotSetup(SlotMemory(str(tmp_path / "mem")), MagicMock(), phone)
    setup.memory.save(_good_map())
    setup.prepare("d", "Wrap")
    save_autopilot_value("Wrap", "bet_row_direction", "left", str(tmp_path / "mem"))

    assert setup.learn_bet_menu("d", "Wrap").data is True
    assert setup.memory.load("Wrap", (200, 400)).data.bet_scroll_direction == "left"
