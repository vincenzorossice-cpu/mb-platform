from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_value, save_autopilot_value
from orchestrator.modules.slot_autopilot import session
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.slot_explorer import SlotExplorer
from orchestrator.modules.slot_autopilot.slot_map import Point, SlotMemory
from orchestrator.modules.slot_autopilot.slot_setup import EXPLORED, USED, SlotSetup
from orchestrator.modules.slot_autopilot.spin_controller import SpinResult
from orchestrator.tests.unit.test_slot_map_learning import FakePhone, MENU, OVERVIEW, SIZE, _good_map, _sol


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


class TestTheExplorerNeverTouchesTheSpin:
    @pytest.mark.parametrize("demo", [True, False])
    def test_no_tap_on_the_spin_and_the_point_stays_pending(self, tmp_path, demo):
        phone = FakePhone(tmp_path)
        explorer = SlotExplorer(_sol(OVERVIEW, MENU), phone, phone, ocr_reader=phone, sleep=lambda s: None)

        smap = explorer.explore("d", "Wrap", demo=demo).data

        assert (100, 360) not in phone.taps                 # nessuno spin a puntata di default
        assert smap.spin_pending and not smap.spin.verified and smap.bet_menu_open.verified

    def test_a_missing_spin_point_is_searched_with_the_grid_without_tapping(self, tmp_path):
        phone = FakePhone(tmp_path)
        overview = dict(OVERVIEW, spin=None)
        describe = MagicMock(return_value=(100, 360))
        explorer = SlotExplorer(_sol(overview, MENU), phone, phone, describe=describe, sleep=lambda s: None)

        smap = explorer.explore("d", "Wrap").data

        assert smap.spin.x == 100 and (100, 360) not in phone.taps


def _spin(i, before="100", after="99.8", bet="0.20", understated=False):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal(bet), balance_before=Decimal(before),
                                balance_after=Decimal(after), win_amount=None, verified=True, attempts=1,
                                outcome="success", bet_understated=understated))


def _bet(final="0.2", starting="1.0"):
    return Result.ok(BetResult(slot_name="s", starting_bet=Decimal(starting), final_bet=Decimal(final),
                               min_bet_verified=True, attempts=1))


class TestOrderAndSafetyNet:
    def _autopilot(self, tmp_path, spins, calls):
        phone = FakePhone(tmp_path)
        explorer = MagicMock()
        explorer.explore.return_value = Result.ok(_good_map(spin=Point(100, 360, "spin", False), spin_pending=True))
        setup = SlotSetup(SlotMemory(str(tmp_path / "mem")), explorer, phone)
        original = setup.prepare
        setup.prepare = lambda d, s: calls.append("esplorazione") or original(d, s)
        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.side_effect = lambda device: calls.append("puntata minima") or _bet()
        autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
        spin_iter = iter(spins)
        autopilot.spin.side_effect = lambda *a, **k: calls.append("spin") or next(spin_iter)
        autopilot.hitl_strategy = None
        autopilot.slot_setup = setup
        return autopilot, setup

    def test_the_bet_is_lowered_before_any_spin(self, tmp_path):
        calls = []
        autopilot, _ = self._autopilot(tmp_path, [_spin(1), _spin(2)], calls)

        session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 20)

        assert calls[:3] == ["esplorazione", "puntata minima", "spin"]

    def test_the_first_real_spin_confirms_the_spin_point_of_the_map(self, tmp_path):
        calls = []
        autopilot, setup = self._autopilot(tmp_path, [_spin(1), _spin(2)], calls)

        session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 20)

        saved = setup.memory.load("Wrap", SIZE).data
        assert saved.spin.verified and not saved.spin_pending

    def test_a_first_spin_at_one_euro_is_not_counted_and_the_bet_is_reset(self, tmp_path):
        calls = []
        wrong = _spin(1, before="500", after="498.9", understated=True)         # calo 1.10 con puntata creduta 0.20
        autopilot, _ = self._autopilot(tmp_path, [wrong, _spin(2), _spin(3)], calls)

        result = session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 20)

        assert not result.failed and result.total_played == Decimal("0.40")      # i due spin giusti; quello a 1 EUR no
        assert calls.count("puntata minima") == 2                                # iniziale + riverifica

    def test_a_second_wrong_first_spin_is_handled_and_a_third_stops(self, tmp_path):
        calls = []
        wrong = [_spin(i, before="500", after="498.9", understated=True) for i in range(1, 5)]
        autopilot, _ = self._autopilot(tmp_path, wrong, calls)

        result = session.play_session(autopilot, "d", "Wrap", Decimal("50"), 20)

        assert result.stop_reason == "BET_UNDERSTATED" and result.total_played == Decimal("0")


class TestTheMinimumLearnedStaysInTheMap:
    def _setup(self, tmp_path):
        phone = FakePhone(tmp_path)
        setup = SlotSetup(SlotMemory(str(tmp_path / "mem")), MagicMock(), phone)
        setup.memory.save(_good_map(min_bet=None, min_bet_proven=False))
        setup.prepare("d", "Wrap")
        return setup

    def test_a_proven_minimum_is_saved_in_the_map(self, tmp_path):
        setup = self._setup(tmp_path)
        save_autopilot_value("Wrap", "min_bet_trusted", "0.20", str(tmp_path / "mem"))

        assert setup.learn_bet_menu("d", "Wrap", Decimal("0.20")).data is True

        saved = setup.memory.load("Wrap", SIZE).data
        assert saved.min_bet == "0.20" and saved.min_bet_proven

    def test_an_unproven_minimum_is_only_a_hint(self, tmp_path):
        setup = self._setup(tmp_path)

        setup.learn_bet_menu("d", "Wrap", Decimal("0.40"))

        saved = setup.memory.load("Wrap", SIZE).data
        assert saved.min_bet == "0.40" and not saved.min_bet_proven

    def test_the_next_run_loads_the_minimum_from_the_map_without_exploring(self, tmp_path):
        setup = self._setup(tmp_path)
        save_autopilot_value("Wrap", "min_bet_trusted", "0.20", str(tmp_path / "mem"))
        setup.learn_bet_menu("d", "Wrap", Decimal("0.20"))
        fresh_explorer = MagicMock()
        phone = FakePhone(tmp_path)
        next_run = SlotSetup(SlotMemory(str(tmp_path / "mem2" if False else tmp_path / "mem")), fresh_explorer, phone)

        outcome = next_run.prepare("d", "Wrap")

        assert outcome.data.status == USED
        fresh_explorer.explore.assert_not_called()
        assert load_autopilot_value("Wrap", "min_bet_trusted", str(tmp_path / "mem")) == "0.20"
