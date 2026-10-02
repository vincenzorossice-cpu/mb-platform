from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.session import SlotSessionResult, play_session
from orchestrator.modules.slot_autopilot.spin_controller import SpinResult


def _spin(outcome="success", bet="0.10", understated=False, invariant=None, index=1):
    return Result.ok(SpinResult(
        slot_name="s", spin_index=index, bet_amount=Decimal(bet) if bet else None, balance_before=None,
        balance_after=Decimal("99.9"), win_amount=None, verified=outcome == "success", attempts=1,
        outcome=outcome, bet_understated=understated, violated_invariant=invariant,
    ))


def _autopilot(spins, bet_verified=True, aborted_reason=None, balances=(Decimal("100"), Decimal("99.7"))):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(BetResult(
        slot_name="s", starting_bet=Decimal("2"), final_bet=Decimal("0.1"), min_bet_verified=bet_verified,
        attempts=1, aborted_reason=aborted_reason,
    ))
    autopilot.get_balance_summary.return_value = balances
    autopilot.spin.side_effect = list(spins)
    return autopilot


def _play(autopilot, target="0.30", max_steps=50):
    return play_session(autopilot, "dev", "slot", Decimal(target), max_steps)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda seconds: None)


def test_reaching_the_target_is_a_clean_result():
    result = _play(_autopilot([_spin(index=i) for i in range(1, 6)]))

    assert isinstance(result, SlotSessionResult)
    assert result.total_played == Decimal("0.30") and result.spins == 3
    assert not result.failed and result.target_reached and result.stop_reason is None
    assert result.initial_balance == Decimal("100") and result.final_balance == Decimal("99.7")


def test_max_steps_ends_the_session_without_a_failure():
    result = _play(_autopilot([_spin(index=i) for i in range(1, 6)]), target="30", max_steps=2)

    assert result.spins == 2 and not result.failed and not result.target_reached


def test_prepare_failure_plays_nothing():
    autopilot = _autopilot([])
    autopilot.prepare.return_value = Result.fail("no device", "ADB_ERROR")

    result = _play(autopilot)

    assert result.failed and result.stop_reason == "PREPARE_FAILED"
    autopilot.set_min_bet.assert_not_called()


def test_an_unproven_minimum_plays_nothing_and_reports_why(caplog):
    autopilot = _autopilot([], bet_verified=False, aborted_reason="MIN_NOT_PROVEN")

    with caplog.at_level("ERROR"):
        result = _play(autopilot)

    assert result.failed and result.stop_reason == "BET_NOT_VERIFIED" and result.bet_aborted_reason == "MIN_NOT_PROVEN"
    assert "non spinno" in caplog.text
    autopilot.spin.assert_not_called()


def test_three_spins_without_effect_stop_the_session():
    result = _play(_autopilot([_spin("no_effect", index=i) for i in range(1, 6)]), target="30")

    assert result.failed and result.stop_reason == "NO_EFFECT" and result.spins == 3


def test_an_understated_bet_stops_the_session():
    result = _play(_autopilot([_spin(understated=True), _spin()]), target="30")

    assert result.failed and result.stop_reason == "BET_UNDERSTATED" and result.spins == 1


def test_a_failing_spin_call_is_an_error():
    autopilot = _autopilot([Result.fail("boom", "ADB_ERROR")])

    result = _play(autopilot)

    assert result.failed and result.stop_reason == "SPIN_ERROR"


def test_a_busy_screen_is_waited_out_without_using_steps():
    blocked = _spin("blocked", bet=None, invariant="UNSAFE_SCREEN_STATE")
    result = _play(_autopilot([blocked, blocked, _spin(index=1), _spin(index=2), _spin(index=3)]), max_steps=3)

    assert not result.failed and result.total_played == Decimal("0.30")


def test_another_safety_block_ends_the_session_without_a_failure():
    result = _play(_autopilot([_spin("blocked", bet=None, invariant="INSUFFICIENT_BALANCE")]), target="30")

    assert not result.failed and result.stop_reason == "BLOCKED" and not result.target_reached
