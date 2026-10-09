import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import session
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate
from orchestrator.modules.slot_autopilot.supervisor import SlotSupervisor
from orchestrator.tests.unit.test_slot_autopilot_bet_menu import MAIN_BOXES, MENU_BOXES, _context, _provider, _tap
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import _config


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.bet_menu.time.sleep", lambda s: None)


# ---- saldo non leggibile != saldo insufficiente ----------------------------------------------------------------

class TestUnreadableBalance:
    def _controller(self):
        return SpinController(MagicMock(), MagicMock(), MagicMock(), _config())

    def test_an_unreadable_balance_is_not_an_insufficient_balance(self):
        context = SimpleNamespace(slot_ocr_state={})
        assert self._controller()._check_invariants(True, None, Decimal("0.2"), context) == "UNSAFE_SCREEN_STATE"

    def test_a_read_balance_below_the_bet_still_is(self):
        context = SimpleNamespace(slot_ocr_state={})
        assert self._controller()._check_invariants(True, Decimal("0.05"), Decimal("0.2"), context) == "INSUFFICIENT_BALANCE"


def _blocked(invariant, detail=None):
    return Result.ok(SpinResult(slot_name="s", spin_index=1, bet_amount=Decimal("0.2"), balance_before=None,
                                balance_after=None, win_amount=None, verified=False, attempts=0, outcome="blocked",
                                violated_invariant=invariant, blocked_detail=detail))


def _ok(i):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal("0.20"), balance_before=Decimal("100"),
                                balance_after=Decimal("99.8"), win_amount=None, verified=True, attempts=1,
                                outcome="success"))


def _autopilot(spins, verdict="lost"):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(BetResult(
        slot_name="s", starting_bet=Decimal("1"), final_bet=Decimal("0.2"), min_bet_verified=True, attempts=1))
    autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
    autopilot.spin.side_effect = list(spins)
    autopilot.hitl_strategy = None
    autopilot.supervisor.check_in_game.return_value = verdict
    return autopilot


class TestExitPressedByHand:
    def test_an_unreadable_balance_leads_to_the_reentry_not_to_a_stop(self, monkeypatch):
        monkeypatch.setattr(session, "LOST_CHECK_UNREADABLE_SECONDS", 0)
        autopilot = _autopilot([_blocked("UNSAFE_SCREEN_STATE", "saldo/puntata non leggibili"), _ok(2), _ok(3)])
        reenter = MagicMock(return_value=Result.ok(True))

        result = session.play_session(autopilot, "dev", "slot", Decimal("0.40"), 30, reenter=reenter)

        assert not result.failed and result.reentries == 1 and result.total_played == Decimal("0.40")

    def test_a_real_blocked_spin_checks_whether_the_slot_is_gone_before_stopping(self):
        autopilot = _autopilot([_blocked("INSUFFICIENT_BALANCE"), _ok(2), _ok(3)])
        reenter = MagicMock(return_value=Result.ok(True))

        result = session.play_session(autopilot, "dev", "slot", Decimal("0.40"), 30, reenter=reenter)

        assert not result.failed and result.reentries == 1

    def test_a_real_blocked_spin_with_the_slot_still_there_stops_as_before(self):
        autopilot = _autopilot([_blocked("INSUFFICIENT_BALANCE")], verdict="in_game")

        result = session.play_session(autopilot, "dev", "slot", Decimal("5"), 30,
                                      reenter=MagicMock(return_value=Result.ok(True)))

        assert result.stop_reason == "BLOCKED" and result.reentries == 0


# ---- pannello rimasto aperto -------------------------------------------------------------------------------------

class TestSupervisorMenuCheck:
    def _supervisor(self, state):
        gpt = MagicMock()
        gpt._VISION_MAX_TOKENS = 300
        gpt._create_completion.return_value = Result.ok({"screen_state": state, "in_game": True, "action": "none"})
        reader = MagicMock()
        reader._screenshot.return_value = Result.ok("/tmp/x.png")
        return SlotSupervisor(gpt, reader, MagicMock())

    def test_modal_means_open_and_the_normal_budget_is_untouched(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor._encode_reduced", lambda p: "x")
        supervisor = self._supervisor("modal")

        assert supervisor.check_menu_open("d") == "open" and supervisor.calls == 0

    def test_idle_means_closed_and_the_budget_has_a_cap(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor._encode_reduced", lambda p: "x")
        supervisor = self._supervisor("idle")
        supervisor.max_menu_checks = 1

        assert supervisor.check_menu_open("d") == "closed" and supervisor.check_menu_open("d") == "unknown"


class TestEnsureClosed:
    def _selector(self, verdict, operator_tap=None):
        locator = MagicMock()
        locator.is_trusted_strategy.return_value = True
        toggle = LocateCandidate(x=540, y=2088, confidence=1, strategy_name="memory")

        def locate(name, ctx, exclude_strategies=()):
            if name == "bet_menu_close":
                return Result.fail("hint", "HITL_HINT_ONLY") if "hitl" not in exclude_strategies \
                    else Result.fail("no", "ELEMENT_NOT_LOCATABLE")
            return Result.ok(toggle)
        locator.locate.side_effect = locate
        locator.last_hitl_tap.return_value = operator_tap
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap(True)
        supervisor = MagicMock()
        supervisor.check_menu_open.return_value = verdict
        return BetMenuSelector(locator, verifier, supervisor=supervisor), locator, verifier

    def test_open_panel_goes_to_the_operator_and_the_point_is_remembered(self):
        selector, locator, verifier = self._selector("open", operator_tap=(472, 425))
        provider = _provider(_context(MENU_BOXES, posta=0.2), _context(MENU_BOXES, posta=0.2),
                             _context(MAIN_BOXES, posta=0.2), _context(MAIN_BOXES, posta=0.2))

        assert selector.ensure_closed("d", "wrap", provider) is True
        saved = [c.args for c in locator.save_verified_candidate.call_args_list if c.args[0] == "bet_menu_close"]
        assert saved and (saved[0][1].x, saved[0][1].y) == (472, 425)

    def test_closed_panel_costs_no_tap(self):
        selector, locator, verifier = self._selector("closed")

        assert selector.ensure_closed("d", "wrap", MagicMock()) is False
        verifier.verified_tap.assert_not_called()


class TestDeadToggleIsForgotten:
    def test_second_no_effect_in_a_row_removes_the_remembered_icon(self):
        locator = MagicMock()
        locator.locate.return_value = Result.ok(LocateCandidate(x=10, y=20, confidence=1, strategy_name="memory"))
        locator.is_trusted_strategy.return_value = True
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap(False)
        selector = BetMenuSelector(locator, verifier)

        with patch("orchestrator.modules.slot_autopilot.bet_menu.forget_autopilot_element") as forget:
            selector.select_lowest("d", "wrap", _context(MAIN_BOXES), MagicMock())
            forget.assert_not_called()
            selector.select_lowest("d", "wrap", _context(MAIN_BOXES), MagicMock())

        forget.assert_called_once()
        locator.reject_position.assert_called_with("bet_menu_toggle", 10, 20)


def test_arrow_diagnosis_line(caplog):
    autopilot = SimpleNamespace(spin_controller=SimpleNamespace(arrow_stats={
        "arrow": 7, "still": 3, "with_reference": True, "shots": 25, "spins": 10}))

    with caplog.at_level(logging.INFO):
        session._log_arrow_stats(autopilot, logging.getLogger("x"))

    assert "riferimento SI" in caplog.text and "7 spin chiusi dal pulsante" in caplog.text and "2.5" in caplog.text
