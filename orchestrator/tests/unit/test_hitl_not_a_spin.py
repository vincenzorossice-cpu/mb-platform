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

    def _selector(self, tmp_path, probe=True):
        locator = MagicMock()
        locator.is_trusted_strategy.return_value = True
        locator.memory_dir = str(tmp_path)
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap(True)
        verifier.touch_injector.swipe.return_value = Result.ok(True)
        return BetMenuSelector(locator, verifier, scroll_probe=probe), verifier

    CUT = [_box("1,00", 300, 1975), _box("1,40", 500, 1975), _box("2,00", 700, 1975), _box("3,00", 900, 1975)]
    LEFT = [_box("0,40", 300, 1975), _box("0,50", 500, 1975), _box("1,00", 700, 1975), _box("1,40", 900, 1975)]
    FAR = [_box("0,20", 300, 1975), _box("0,40", 500, 1975), _box("0,50", 700, 1975), _box("1,00", 900, 1975)]

    def test_a_low_cell_hidden_in_one_direction_is_found(self, tmp_path):
        selector, verifier = self._selector(tmp_path)
        provider = _provider(_context(self.LEFT), _context(self.FAR), _context(self.FAR), _context(MAIN_BOXES, posta=0.20))

        result = selector.select_from_open_menu("dev", "wrap", _context(self.CUT), provider)

        assert result.success and result.data.final_bet == Decimal("0.20")
        assert verifier.verified_tap.call_args_list[0].args[1:3] == (300, 1975)
        first = verifier.touch_injector.swipe.call_args_list[0].args
        assert first[1] > first[3]                       # il primo gesto e' verso SINISTRA

    def test_a_low_cell_hidden_on_the_other_side_is_found_too(self, tmp_path):
        selector, verifier = self._selector(tmp_path)
        # a sinistra non scende e si torna indietro; a destra compare 0.20
        provider = _provider(_context(self.CUT), _context(self.FAR), _context(self.FAR), _context(MAIN_BOXES, posta=0.20))
        # il primo passaggio a sinistra mostra solo valori piu' alti (CUT senza 1,00)
        higher = [_box("1,40", 300, 1975), _box("2,00", 500, 1975), _box("3,00", 700, 1975), _box("5,00", 900, 1975)]
        provider = _provider(_context(higher), _context(self.CUT), _context(self.FAR), _context(self.FAR),
                             _context(MAIN_BOXES, posta=0.20))

        result = selector.select_from_open_menu("dev", "wrap", _context(self.CUT), provider)

        assert result.success and result.data.final_bet == Decimal("0.20")
        directions = [c.args[1] > c.args[3] for c in verifier.touch_injector.swipe.call_args_list]
        assert directions[0] is True and False in directions            # prima sinistra, poi destra

    def test_a_row_that_does_not_move_is_proven_and_never_probed_again(self, tmp_path):
        selector, verifier = self._selector(tmp_path)
        provider = _provider(_context(self.CUT), _context(self.CUT), _context(MAIN_BOXES, posta=1.00))

        first = selector.select_from_open_menu("dev", "wrap", _context(self.CUT), provider)

        assert first.success and first.data.proven
        swipes_first = verifier.touch_injector.swipe.call_count
        assert swipes_first == 2                                         # una per direzione, nessun annullamento

        provider = _provider(_context(MAIN_BOXES, posta=1.00))
        second = selector.select_from_open_menu("dev", "wrap", _context(self.CUT), provider)

        assert second.success and second.data.proven
        assert verifier.touch_injector.swipe.call_count == swipes_first  # ricordato: nessun altro gesto

    def test_a_lower_cell_found_is_not_proof_of_the_minimum(self, tmp_path):
        selector, _ = self._selector(tmp_path)
        provider = _provider(_context(self.LEFT), _context(self.LEFT), _context(MAIN_BOXES, posta=0.40))

        result = selector.select_from_open_menu("dev", "wrap", _context(self.CUT), provider)

        assert result.success and result.data.final_bet == Decimal("0.40") and not result.data.proven

    def test_probe_is_off_by_default(self, tmp_path):
        selector, verifier = self._selector(tmp_path, probe=False)
        provider = _provider(_context(MAIN_BOXES, posta=1.00))

        assert selector.select_from_open_menu("dev", "wrap", _context(self.CUT), provider).success
        verifier.touch_injector.swipe.assert_not_called()


# ---- "mai fermarsi da soli": chiede all'operatore -------------------------------------------------------------

class _Hitl:
    def __init__(self, answers):
        self.answers = list(answers)
        self.interventions = 0
        self.reasons = []

    def ask_for_help(self, device_id, reason, current_bet=None):
        self.reasons.append(reason)
        return self.answers.pop(0)


def _no_effect():
    return Result.ok(SpinResult(
        slot_name="s", spin_index=1, bet_amount=Decimal("0.40"), balance_before=None, balance_after=None,
        win_amount=None, verified=False, attempts=1, outcome="no_effect",
    ))


def _stuck_autopilot(spins, hitl):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = _bet()
    autopilot.get_balance_summary.return_value = (None, None)
    autopilot.spin.side_effect = list(spins)
    autopilot.hitl_strategy = hitl
    return autopilot


class TestAskTheOperatorInsteadOfStopping:
    def test_three_spins_without_effect_ask_the_operator_and_the_session_goes_on(self):
        hitl = _Hitl(["hint"])
        autopilot = _stuck_autopilot([_no_effect()] * 3 + [_ok_spin(4), _ok_spin(5)], hitl)

        result = play_session(autopilot, "dev", "slot", Decimal("0.80"), 20)

        assert not result.failed and result.total_played == Decimal("0.80")
        assert len(hitl.reasons) == 1 and "senza addebito" in hitl.reasons[0]
        assert autopilot.set_min_bet.call_count == 2          # puntata riverificata dopo l'aiuto

    def test_stop_from_the_operator_ends_the_session(self):
        autopilot = _stuck_autopilot([_no_effect()] * 3, _Hitl(["stop"]))

        result = play_session(autopilot, "dev", "slot", Decimal("5"), 20)

        assert result.failed and result.stop_reason == "NO_EFFECT"

    def test_after_five_requests_the_session_stops_anyway(self):
        hitl = _Hitl(["skip"] * 10)
        autopilot = _stuck_autopilot([_no_effect()] * 40, hitl)

        result = play_session(autopilot, "dev", "slot", Decimal("5"), 100)

        assert result.failed and result.stop_reason == "NO_EFFECT" and len(hitl.reasons) == 5

    def test_without_an_operator_it_stops_as_before(self):
        autopilot = _stuck_autopilot([_no_effect()] * 3, None)

        result = play_session(autopilot, "dev", "slot", Decimal("5"), 20)

        assert result.failed and result.stop_reason == "NO_EFFECT"

    def test_bet_understated_always_stops(self):
        understated = Result.ok(SpinResult(
            slot_name="s", spin_index=1, bet_amount=Decimal("0.40"), balance_before=None, balance_after=None,
            win_amount=None, verified=True, attempts=1, outcome="success", bet_understated=True,
        ))
        hitl = _Hitl(["skip"])
        autopilot = _stuck_autopilot([understated] * 3, hitl)      # riletta 2 volte, alla terza ci si ferma

        result = play_session(autopilot, "dev", "slot", Decimal("5"), 20)

        assert result.stop_reason == "BET_UNDERSTATED" and hitl.reasons == []

    def test_unverified_minimum_bet_asks_before_giving_up(self):
        hitl = _Hitl(["skip"])
        autopilot = _stuck_autopilot([_ok_spin(1), _ok_spin(2)], hitl)
        autopilot.set_min_bet.side_effect = [_bet(verified=False), _bet()]

        result = play_session(autopilot, "dev", "slot", Decimal("0.80"), 20)

        assert not result.failed and len(hitl.reasons) == 1

    def test_disconnected_phone_waits_for_the_operator_and_retries(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        autopilot = _stuck_autopilot(
            [Result.fail("Screenshot file not created", "ADB_TIMEOUT"), _ok_spin(1), _ok_spin(2)], _Hitl([]))

        result = play_session(autopilot, "dev", "slot", Decimal("0.80"), 20)

        assert not result.failed and result.total_played == Decimal("0.80")


class TestBetMenuCloseTaughtByTheOperator:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.bet_menu.time.sleep", lambda s: None)

    def _selector(self, memory_point=None, operator_tap=None):
        from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate
        locator = MagicMock()
        toggle = LocateCandidate(x=540, y=2088, confidence=1, strategy_name="memory")

        def locate(name, ctx, exclude_strategies=()):
            if name == "bet_menu_close":
                if "hitl" not in exclude_strategies:
                    return Result.fail("hint", "HITL_HINT_ONLY")
                if memory_point:
                    return Result.ok(LocateCandidate(x=memory_point[0], y=memory_point[1], confidence=1, strategy_name="memory"))
                return Result.fail("no", "ELEMENT_NOT_LOCATABLE")
            return Result.ok(toggle)

        locator.locate.side_effect = locate
        locator.last_hitl_tap.return_value = operator_tap
        locator.is_trusted_strategy.return_value = True
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap(True)
        return BetMenuSelector(locator, verifier), locator, verifier

    def test_the_operator_closes_the_menu_and_the_point_is_remembered(self):
        selector, locator, verifier = self._selector(operator_tap=(30, 300))
        from orchestrator.tests.unit.test_slot_autopilot_bet_menu import MENU_BOXES
        provider = _provider(_context(MENU_BOXES, posta=0.10), _context(MENU_BOXES, posta=0.10),
                             _context(MAIN_BOXES, posta=0.10), _context(MAIN_BOXES, posta=0.10))

        result = selector.select_from_open_menu("dev", "wrap", _context(MENU_BOXES), provider, (788, 1775))

        assert result.success
        saved = [c.args for c in locator.save_verified_candidate.call_args_list if c.args[0] == "bet_menu_close"]
        assert saved and (saved[0][1].x, saved[0][1].y) == (30, 300)

    def test_a_remembered_close_point_is_used_without_asking(self):
        selector, locator, verifier = self._selector(memory_point=(30, 300))
        from orchestrator.tests.unit.test_slot_autopilot_bet_menu import MENU_BOXES
        provider = _provider(_context(MENU_BOXES, posta=0.10), _context(MAIN_BOXES, posta=0.10),
                             _context(MAIN_BOXES, posta=0.10))

        result = selector.select_from_open_menu("dev", "wrap", _context(MENU_BOXES), provider, (788, 1775))

        assert result.success
        taps = [c.args[1:3] for c in verifier.verified_tap.call_args_list]
        assert (30, 300) in taps
        assert not any(c.args[0] == "bet_menu_close" and "hitl" not in c.kwargs.get("exclude_strategies", ())
                       for c in locator.locate.call_args_list)
