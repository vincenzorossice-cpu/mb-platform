from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector
from orchestrator.modules.slot_autopilot.session import play_session
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.tests.unit.test_slot_autopilot_bet_menu import MAIN_BOXES, _box, _context, _provider, _tap
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import (
    _candidate, _config, _context_result,
)


def _controller(action, balances):
    locator = MagicMock()
    locator.locate.return_value = _candidate(strategy_name="hitl", verified=True)
    locator.last_hitl_action.return_value = action
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    controller = SpinController(locator, MagicMock(), step_logger, _config())
    provider = MagicMock(side_effect=[_context_result(b) for b in balances])
    return controller, locator, provider


class TestOperatorTapIsNotAlwaysASpin:
    def test_a_bet_change_by_the_operator_is_not_counted_and_not_saved_as_spin(self):
        controller, locator, provider = _controller("resolved", [Decimal("25000"), Decimal("25000")])

        result = controller.spin("dev", "wrappin", "ep", 1, provider, True, Decimal("1.00"))

        assert result.data.outcome == "operator_action" and not result.data.verified
        locator.save_verified_candidate.assert_not_called()
        locator.reject_position.assert_not_called()

    def test_a_confirmed_spin_with_a_balance_drop_is_counted_and_saved(self):
        controller, locator, provider = _controller("spin_confirmed", [Decimal("25000"), Decimal("24999")])

        result = controller.spin("dev", "wrappin", "ep", 1, provider, True, Decimal("1.00"))

        assert result.data.outcome == "success"
        locator.save_verified_candidate.assert_called_once()

    def test_a_confirmed_spin_without_any_balance_change_is_not_trusted(self):
        controller, locator, provider = _controller("spin_confirmed", [Decimal("25000"), Decimal("25000")])

        result = controller.spin("dev", "wrappin", "ep", 1, provider, True, Decimal("1.00"))

        assert result.data.outcome == "operator_action"
        locator.save_verified_candidate.assert_not_called()


def _op_spin():
    return Result.ok(SpinResult(
        slot_name="s", spin_index=1, bet_amount=Decimal("1.00"), balance_before=None, balance_after=None,
        win_amount=None, verified=False, attempts=0, outcome="operator_action",
    ))


def _ok_spin(i):
    return Result.ok(SpinResult(
        slot_name="s", spin_index=i, bet_amount=Decimal("0.40"), balance_before=None,
        balance_after=Decimal("99"), win_amount=None, verified=True, attempts=1, outcome="success",
    ))


def _bet(verified=True):
    return Result.ok(BetResult(slot_name="s", starting_bet=Decimal("1"), final_bet=Decimal("0.4"),
                               min_bet_verified=verified, attempts=1))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


class TestSessionAfterOperatorAction:
    def test_operator_action_adds_no_played_amount_and_rechecks_the_bet(self):
        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.side_effect = [_bet(), _bet()]
        autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
        autopilot.spin.side_effect = [_op_spin(), _ok_spin(2), _ok_spin(3)]

        result = play_session(autopilot, "dev", "slot", Decimal("0.80"), 20)

        assert result.total_played == Decimal("0.80") and not result.failed
        assert autopilot.set_min_bet.call_count == 2

    def test_unverifiable_bet_after_the_operator_stops_the_session(self):
        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.side_effect = [_bet(), _bet(verified=False)]
        autopilot.get_balance_summary.return_value = (None, None)
        autopilot.spin.side_effect = [_op_spin()]

        result = play_session(autopilot, "dev", "slot", Decimal("1"), 20)

        assert result.failed and result.stop_reason == "BET_NOT_VERIFIED" and result.total_played == 0

    def test_too_many_operator_actions_stop_the_session(self):
        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.return_value = _bet()
        autopilot.get_balance_summary.return_value = (None, None)
        autopilot.spin.side_effect = [_op_spin() for _ in range(10)]

        result = play_session(autopilot, "dev", "slot", Decimal("1"), 50)

        assert result.failed and result.stop_reason == "BLOCKED"


def test_disconnected_phone_gets_a_clear_message(caplog):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = _bet()
    autopilot.get_balance_summary.return_value = (None, None)
    autopilot.spin.return_value = Result.fail("Screenshot file not created", "ADB_TIMEOUT")

    with caplog.at_level("ERROR"):
        result = play_session(autopilot, "dev", "slot", Decimal("1"), 5)

    assert result.stop_reason == "SPIN_ERROR" and "scollegato" in caplog.text


class TestScrollableBetRow:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.bet_menu.time.sleep", lambda s: None)

    def _selector(self):
        locator = MagicMock()
        locator.is_trusted_strategy.return_value = True
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap(True)
        verifier.touch_injector.swipe.return_value = Result.ok(True)
        return BetMenuSelector(locator, verifier), verifier

    CUT = [_box("1,00", 100, 1975), _box("1,40", 300, 1975), _box("2,00", 500, 1975), _box("3,00", 700, 1975)]
    MORE = [_box("0,40", 100, 1975), _box("0,50", 300, 1975), _box("1,00", 500, 1975), _box("1,40", 700, 1975)]

    def test_a_row_cut_at_the_left_edge_is_scrolled_and_the_true_lowest_is_chosen(self):
        selector, verifier = self._selector()
        provider = _provider(_context(self.MORE), _context(self.MORE), _context(MAIN_BOXES, posta=0.40))

        result = selector.select_from_open_menu("dev", "wrap", _context(self.CUT), provider)

        assert result.success and result.data.final_bet == Decimal("0.40")
        assert verifier.touch_injector.swipe.call_count == 2   # il secondo non rivela niente di piu' basso: si ferma
        assert verifier.verified_tap.call_args_list[0].args[1:3] == (100, 1975)

    def test_a_row_that_starts_far_from_the_edge_is_not_touched(self):
        selector, verifier = self._selector()
        full = [_box("0,10", 300, 1975), _box("0,20", 500, 1975), _box("0,50", 700, 1975), _box("1,00", 900, 1975)]
        provider = _provider(_context(MAIN_BOXES, posta=0.10))

        result = selector.select_from_open_menu("dev", "wrap", _context(full), provider)

        assert result.success
        verifier.touch_injector.swipe.assert_not_called()

    def test_scrolling_stops_when_nothing_lower_appears(self):
        selector, verifier = self._selector()
        provider = _provider(_context(self.CUT), _context(MAIN_BOXES, posta=1.00))

        result = selector.select_from_open_menu("dev", "wrap", _context(self.CUT), provider)

        assert result.success and result.data.final_bet == Decimal("1.00")
        assert verifier.touch_injector.swipe.call_count == 1
