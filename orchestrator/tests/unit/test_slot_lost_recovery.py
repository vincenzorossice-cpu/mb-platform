from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.session import play_session
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.supervisor import SlotSupervisor
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import _candidate, _config, _context_result


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


def _bet(final="0.40", verified=True):
    return Result.ok(BetResult(slot_name="s", starting_bet=Decimal("1"), final_bet=Decimal(final),
                               min_bet_verified=verified, attempts=1))


def _ok(i, bet="0.40", before="100", after="99.6"):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal(bet), balance_before=Decimal(before),
                                balance_after=Decimal(after), win_amount=None, verified=True, attempts=1,
                                outcome="success"))


def _lost(i=1):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal("0.40"), balance_before=None,
                                balance_after=None, win_amount=None, verified=False, attempts=0, outcome="slot_lost"))


def _autopilot(spins, balances=(Decimal("100"), Decimal("99"))):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = _bet()
    autopilot.get_balance_summary.return_value = balances
    autopilot.spin.side_effect = list(spins)
    autopilot.hitl_strategy = None
    return autopilot


class TestSupervisorKnowsWhenTheSlotIsGone:
    def _supervisor(self, answer):
        gpt = MagicMock()
        gpt._VISION_MAX_TOKENS = 300
        gpt._create_completion.return_value = Result.ok(answer)
        reader = MagicMock()
        reader._screenshot.return_value = Result.ok("/tmp/x.png")
        return SlotSupervisor(gpt, reader, MagicMock()), gpt

    def test_in_game_false_means_lost_and_does_not_use_the_normal_budget(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor._encode_reduced", lambda p: "x")
        supervisor, _ = self._supervisor({"screen_state": "error", "in_game": False, "action": "none"})

        assert supervisor.check_in_game("d") == "lost"
        assert supervisor.calls == 0 and supervisor.lost_checks == 1

    def test_in_game_true_and_missing_field(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor._encode_reduced", lambda p: "x")
        assert self._supervisor({"screen_state": "idle", "in_game": True})[0].check_in_game("d") == "in_game"
        assert self._supervisor({"screen_state": "idle"})[0].check_in_game("d") == "unknown"

    def test_the_lost_checks_have_their_own_cap(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor._encode_reduced", lambda p: "x")
        supervisor, gpt = self._supervisor({"screen_state": "idle", "in_game": True})
        supervisor.max_lost_checks = 2
        results = [supervisor.check_in_game("d") for _ in range(3)]
        assert results == ["in_game", "in_game", "unknown"] and gpt._create_completion.call_count == 2

    def test_check_ready_reports_lost(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor._encode_reduced", lambda p: "x")
        supervisor, _ = self._supervisor({"screen_state": "error", "in_game": False, "action": "none"})
        assert supervisor.check_ready("d") == "lost"


class TestSpinControllerNoticesTheSlotIsGone:
    def _controller(self, verdict):
        locator = MagicMock()
        locator.locate.side_effect = [Result.fail("no", "ELEMENT_NOT_LOCATABLE"), Result.fail("no", "ELEMENT_NOT_LOCATABLE")]
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        controller = SpinController(locator, MagicMock(), step_logger, _config())
        controller.lost_check = MagicMock(return_value=verdict)
        return controller, locator

    def test_lost_means_no_operator_question(self):
        controller, locator = self._controller("lost")
        provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "s", "ep", 1, provider, True, Decimal("0.10"))

        assert result.data.outcome == "slot_lost"
        assert locator.locate.call_count == 1 and locator.locate.call_args.kwargs["exclude_strategies"] == ("hitl",)

    def test_in_game_asks_the_operator_as_before(self):
        controller, locator = self._controller("in_game")
        provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "s", "ep", 1, provider, True, Decimal("0.10"))

        assert result.data.outcome == "error" and locator.locate.call_count == 2


class TestReenteringTheSlot:
    def test_slot_lost_reenters_and_keeps_the_played_amount(self):
        autopilot = _autopilot([_ok(1), _lost(2), _ok(3), _ok(4)])
        reenter = MagicMock(return_value=Result.ok(True))

        result = play_session(autopilot, "dev", "slot", Decimal("1.20"), 30, reenter=reenter)

        assert not result.failed and result.total_played == Decimal("1.20") and result.reentries == 1
        reenter.assert_called_once_with("dev")
        assert autopilot.prepare.call_count == 2 and autopilot.set_min_bet.call_count == 2
        autopilot.bet_controller.forget_session_min.assert_called_once_with("slot")

    def test_without_reenter_a_lost_slot_stops_the_session(self):
        result = play_session(_autopilot([_ok(1), _lost(2)]), "dev", "slot", Decimal("5"), 30)

        assert result.failed and result.stop_reason == "SLOT_LOST"

    def test_after_three_reentries_it_stops(self):
        autopilot = _autopilot([_lost(i) for i in range(1, 8)])
        reenter = MagicMock(return_value=Result.ok(True))

        result = play_session(autopilot, "dev", "slot", Decimal("5"), 30, reenter=reenter)

        assert result.failed and result.stop_reason == "SLOT_LOST" and reenter.call_count == 3

    def test_a_failed_reentry_stops_when_there_is_no_operator(self):
        autopilot = _autopilot([_lost(1)])
        reenter = MagicMock(return_value=Result.fail("login?", "NAV_FAILED"))

        result = play_session(autopilot, "dev", "slot", Decimal("5"), 30, reenter=reenter)

        assert result.failed and result.stop_reason == "SLOT_LOST"

    def test_no_effect_with_sol_saying_lost_reenters_instead_of_asking(self):
        no_effect = Result.ok(SpinResult(slot_name="s", spin_index=1, bet_amount=Decimal("0.40"), balance_before=None,
                                         balance_after=None, win_amount=None, verified=False, attempts=1,
                                         outcome="no_effect"))
        autopilot = _autopilot([no_effect] * 3 + [_ok(4), _ok(5)])
        autopilot.supervisor.check_in_game.return_value = "lost"
        reenter = MagicMock(return_value=Result.ok(True))

        result = play_session(autopilot, "dev", "slot", Decimal("0.80"), 30, reenter=reenter)

        assert not result.failed and result.reentries == 1 and result.total_played == Decimal("0.80")

    def test_balance_net_is_summed_over_the_segments(self):
        autopilot = _autopilot([_ok(1), _lost(2), _ok(3)])
        autopilot.get_balance_summary.side_effect = [
            (Decimal("100"), None), (Decimal("100"), Decimal("97")),          # all'inizio e al rientro: 100 -> 97
            (Decimal("100"), Decimal("101")),                                 # fine: segmento dopo il rientro +1
        ]

        result = play_session(autopilot, "dev", "slot", Decimal("0.80"), 30,
                              reenter=MagicMock(return_value=Result.ok(True)))

        assert result.net == Decimal("-2") and result.initial_balance == Decimal("100")


class TestWrongBetCorrection:
    def test_four_small_drops_with_a_high_believed_bet_reread_and_correct_the_played_amount(self):
        spins = [_ok(i, bet="1.40", before="100", after="99.85") for i in range(1, 5)]
        spins += [_ok(i, bet="0.20", before="100", after="99.80") for i in range(5, 40)]
        autopilot = _autopilot(spins)
        autopilot.set_min_bet.side_effect = [_bet("1.40"), _bet("0.20")]

        result = play_session(autopilot, "dev", "slot", Decimal("5.60"), 60)

        # 4 spin creduti a 1.40 = 5.60 corretti a 4 x 0.20 = 0.80; poi spin da 0.20 fino a 5.60
        assert result.total_played == Decimal("5.60") and result.spins == 28
        assert autopilot.set_min_bet.call_count == 2

    def test_real_losses_of_the_full_bet_never_trigger_it(self):
        spins = [_ok(i, bet="0.40", before="100", after="99.60") for i in range(1, 7)]
        autopilot = _autopilot(spins)

        result = play_session(autopilot, "dev", "slot", Decimal("2.40"), 30)

        assert result.total_played == Decimal("2.40") and autopilot.set_min_bet.call_count == 1
