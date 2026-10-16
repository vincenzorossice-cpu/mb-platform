import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
from orchestrator.modules.slot_autopilot.autospin_manager import AutoSpinManager, CycleResult
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.popup_guard import PopupGuard, PopupOutcome
from orchestrator.modules.slot_autopilot.session import play_session
from orchestrator.modules.slot_autopilot.slot_map import AutospinInfo, Point, SlotMap, SlotMemory, TurboInfo
from orchestrator.modules.slot_autopilot.speedup_controller import SpeedupController
from orchestrator.modules.slot_autopilot.spin_controller import SpinResult
from orchestrator.tests.unit.autospin_world import AUTO, CONFIRM, OPTION_INF, SPIN, Clock, World
from orchestrator.tests.unit.test_slot_autopilot_session import _autopilot
from orchestrator.tests.unit.test_speedups import _good_autospin, _setup

BET = Decimal("0.10")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


class FakeGuard(PopupGuard):
    """Guardia finta: `popups` e' la sequenza di esiti dei prossimi `clear`."""
    def __init__(self, outcomes):
        super().__init__(None, None)
        self.outcomes, self.calls = list(outcomes), []

    def clear(self, device_id, slot_name, suspect=False):
        self.calls.append(suspect)
        return self.outcomes.pop(0) if self.outcomes else Result.ok(PopupOutcome())

    def inspect(self, device_id, slot_name, suspect=False):
        return SimpleNamespace(kind=None)


def _win_spin(index, win):
    return Result.ok(SpinResult(
        slot_name="s", spin_index=index, bet_amount=BET, balance_before=Decimal("100"),
        balance_after=Decimal("100") - BET + Decimal(str(win)), win_amount=Decimal(str(win)) or None, verified=True,
        attempts=1, outcome="success"))


def _session(spins, guard, target="0.30"):
    autopilot = _autopilot(spins)
    autopilot.config = AutoPilotConfig(device_id="dev", slot_name="slot", target_played_amount=Decimal(target))
    autopilot.gamble_guard = guard
    autopilot._current_bet = BET
    return autopilot, play_session(autopilot, "dev", "slot", Decimal(target), 50)


# ---- Fix 2: la puntata si rilegge dopo le vincite -----------------------------------------------------------------------

def test_a_big_win_forces_a_bet_recheck_even_without_a_popup():
    guard = FakeGuard([])
    autopilot, result = _session([_win_spin(1, "0.50"), _win_spin(2, 0), _win_spin(3, 0)], guard)

    assert autopilot.set_min_bet.call_count == 2                       # iniziale + dopo la vincita da 5x
    assert result.target_reached


def test_a_small_win_costs_nothing_extra():
    guard = FakeGuard([])
    autopilot, _ = _session([_win_spin(1, "0.05"), _win_spin(2, 0), _win_spin(3, 0)], guard)

    assert autopilot.set_min_bet.call_count == 1 and guard.calls == []   # nessun popup da cercare sotto 2x


def test_a_medium_win_looks_for_a_popup_and_rechecks_only_if_one_was_closed():
    quiet = FakeGuard([Result.ok(PopupOutcome())])
    autopilot, _ = _session([_win_spin(1, "0.30"), _win_spin(2, 0), _win_spin(3, 0)], quiet)
    assert quiet.calls == [True] and autopilot.set_min_bet.call_count == 1

    closed = FakeGuard([Result.ok(PopupOutcome(True, "gamble", (300, 600), "ocr"))])
    autopilot, _ = _session([_win_spin(1, "0.30"), _win_spin(2, 0), _win_spin(3, 0)], closed)
    assert autopilot.set_min_bet.call_count == 2


def test_a_win_with_a_gamble_popup_closes_it_and_rereads_the_bet_before_the_next_spin(caplog):
    guard = FakeGuard([Result.ok(PopupOutcome(True, "gamble", (300, 600), "ocr"))])
    autopilot = _autopilot([_win_spin(1, "36.10"), _win_spin(2, 0), _win_spin(3, 0), _win_spin(4, 0)])
    order = []
    autopilot.set_min_bet.side_effect = lambda device: order.append("set_min_bet") or autopilot.set_min_bet.return_value
    queue = iter(list(autopilot.spin.side_effect))
    autopilot.spin.side_effect = lambda *args, **kwargs: order.append("spin") or next(queue)
    autopilot.config = AutoPilotConfig(device_id="dev", slot_name="slot", target_played_amount=Decimal("0.30"))
    autopilot.gamble_guard = guard

    with caplog.at_level(logging.INFO):
        play_session(autopilot, "dev", "slot", Decimal("0.30"), 50)

    assert order[:4] == ["set_min_bet", "spin", "set_min_bet", "spin"]       # la rilettura precede lo spin dopo la vincita


def test_a_popup_that_cannot_be_closed_stops_the_session_without_more_spins():
    guard = FakeGuard([Result.fail("senza Raccogli", "GAMBLE_NO_COLLECT")])
    autopilot, result = _session([_win_spin(1, "36.10"), _win_spin(2, 0)], guard)

    assert result.failed and result.stop_reason == "POPUP_NOT_CLOSED" and autopilot.spin.call_count == 1


def test_the_warden_gamble_flag_makes_the_session_reread_the_bet():
    guard = FakeGuard([])
    autopilot = _autopilot([_win_spin(1, 0), _win_spin(2, 0), _win_spin(3, 0)])
    autopilot.config = AutoPilotConfig(device_id="dev", slot_name="slot", target_played_amount=Decimal("0.30"))
    autopilot.gamble_guard = guard
    autopilot.warden = SimpleNamespace(handle=MagicMock(return_value="resume"), calls=0, gamble_collected=True)
    anomaly = SpinResult(slot_name="s", spin_index=1, bet_amount=BET, balance_before=Decimal("100"),
                         balance_after=Decimal("100"), win_amount=None, verified=False, attempts=1, outcome="success",
                         anomaly=True)
    autopilot.spin.side_effect = [Result.ok(anomaly)] + list(autopilot.spin.side_effect)

    play_session(autopilot, "dev", "slot", Decimal("0.30"), 50)

    assert autopilot.warden.handle.call_count == 1 and autopilot.warden.gamble_collected is False
    assert autopilot.set_min_bet.call_count == 2


# ---- Fix 4: l'autospin parte solo a puntata verificata e senza popup -----------------------------------------------------

def _real_autopilot(tmp_path, guard=None, bet_ok=True):
    from orchestrator.tests.unit.test_slot_autopilot_autopilot import _collaborators, _config
    deps = _collaborators(config=_config())
    autopilot = SlotAutoPilot(**deps)
    autopilot.gamble_guard = guard
    result = BetResult(slot_name="pirots5", starting_bet=Decimal("0.30"), final_bet=BET, min_bet_verified=bet_ok,
                       attempts=1, aborted_reason=None if bet_ok else "BET_UNREADABLE")
    autopilot.set_min_bet = MagicMock(return_value=Result.ok(result))
    return autopilot


def test_preflight_lowers_the_bet_first_and_only_then_lets_the_autospin_go(tmp_path):
    autopilot = _real_autopilot(tmp_path, FakeGuard([Result.ok(PopupOutcome())]))
    autopilot.screen_reader._screenshot.return_value = Result.fail("x", "X")

    assert autopilot._autospin_preflight("d") is None
    autopilot.set_min_bet.assert_called_once_with("d")


def test_preflight_blocks_an_unverified_bet_and_an_unclosable_popup(tmp_path):
    assert "puntata" in _real_autopilot(tmp_path, FakeGuard([]), bet_ok=False)._autospin_preflight("d")
    blocked = _real_autopilot(tmp_path, FakeGuard([Result.fail("x", "POPUP_NOT_CLOSED")]))
    assert "popup" in blocked._autospin_preflight("d")
    blocked.set_min_bet.assert_not_called()                              # prima il popup, poi la puntata


def test_preflight_closes_a_popup_before_checking_the_bet(tmp_path):
    guard = FakeGuard([Result.ok(PopupOutcome(True, "gamble", (300, 600), "ocr"))])
    autopilot = _real_autopilot(tmp_path, guard)
    autopilot.screen_reader._screenshot.return_value = Result.fail("x", "X")
    order = []
    original = guard.clear
    guard.clear = lambda *a, **k: order.append("popup") or original(*a, **k)
    autopilot.set_min_bet.side_effect = lambda d: order.append("bet") or Result.ok(BetResult(
        slot_name="s", starting_bet=BET, final_bet=BET, min_bet_verified=True, attempts=0))

    assert autopilot._autospin_preflight("d") is None and order == ["popup", "bet"]


def test_preflight_refuses_when_the_selected_bet_cell_is_not_the_verified_one(tmp_path):
    autopilot = _real_autopilot(tmp_path, FakeGuard([Result.ok(PopupOutcome())]))
    autopilot.bet_watch = SimpleNamespace(active=True, check=lambda path: True)
    autopilot.screen_reader._screenshot.return_value = Result.ok("/tmp/none.png")

    assert "cella" in autopilot._autospin_preflight("d")


def test_the_autospin_does_not_start_while_the_preflight_says_no(tmp_path):
    world = World(tmp_path)
    clock = Clock()
    manager = AutoSpinManager(world.screen_reader, world.touch_injector, world.ocr, button=world.button,
                              sleep=clock.sleep, clock=clock, before_start=lambda: "puntata non verificata al minimo")

    result = manager.run_cycle("d", "Slot", _good_autospin(), SPIN, BET, Decimal("3"), 100, 5.0)

    assert result.reason == "preflight_failed" and world.taps == []


def test_every_restart_runs_the_preflight_again(tmp_path):
    world = World(tmp_path)
    world.stall_after_shots = 8
    clock = Clock()
    calls = []
    manager = AutoSpinManager(world.screen_reader, world.touch_injector, world.ocr, button=world.button,
                              sleep=clock.sleep, clock=clock, before_start=lambda: calls.append(1) and None)

    result = manager.run_cycle("d", "Slot", _good_autospin(), SPIN, BET, Decimal("6.0"), 1000, 5.0)

    assert result.cycles >= 2 and len(calls) == result.cycles                # uno per ogni avvio


def test_a_restart_blocked_by_the_preflight_ends_the_cycle_with_the_autospin_stopped(tmp_path):
    world = World(tmp_path)
    world.stall_after_shots = 8
    clock = Clock()
    answers = iter([None, "puntata cambiata e non riportata"])
    manager = AutoSpinManager(world.screen_reader, world.touch_injector, world.ocr, button=world.button,
                              sleep=clock.sleep, clock=clock, before_start=lambda: next(answers))

    result = manager.run_cycle("d", "Slot", _good_autospin(), SPIN, BET, Decimal("6.0"), 1000, 5.0)

    assert result.reason == "preflight_failed" and not world.running


def test_a_gamble_popup_that_stops_the_autospin_is_closed_with_collect_never_the_center(tmp_path):
    world = World(tmp_path)
    clock = Clock()
    guard = FakeGuard([Result.ok(PopupOutcome(True, "gamble", (300, 600), "ocr"))])
    manager = AutoSpinManager(world.screen_reader, world.touch_injector, world.ocr, button=world.button,
                              sleep=clock.sleep, clock=clock, popup_guard=guard)
    original = manager.monitor_sample
    calls = {"n": 0}

    def sample(device, slot, run, bet):
        calls["n"] += 1
        if calls["n"] == 2:
            world.running, world.popup = False, True
        return original(device, slot, run, bet)

    manager.monitor_sample = sample

    result = manager.run_cycle("d", "Slot", _good_autospin(), SPIN, BET, Decimal("6.0"), 1000, 5.0)

    assert guard.calls == [True] and result.cycles >= 2
    assert (200, 400) not in world.taps                                 # nessun tocco cieco al centro


def test_gamble_words_on_screen_without_a_guard_prevent_the_blind_center_tap(tmp_path):
    world = World(tmp_path)
    clock = Clock()
    manager = AutoSpinManager(world.screen_reader, world.touch_injector, world.ocr, button=world.button,
                              sleep=clock.sleep, clock=clock)
    world.ocr.read_text_boxes = lambda path: Result.ok([SimpleNamespace(text="RADDOPPIA", bbox=(10, 10, 90, 40), confidence=1)])

    assert manager._close_popup("d", "Slot") is False and (200, 400) not in world.taps


# ---- bet_changed durante l'autospin: ferma, abbassa, riavvia -----------------------------------------------------------

def _controller_with(cycles, tmp_path):
    world = World(tmp_path)
    setup = _setup(tmp_path, world, autospin=_good_autospin(), turbo=TurboInfo(status="absent"))
    autopilot = MagicMock()
    autopilot.config = AutoPilotConfig(device_id="d", slot_name="Slot", target_played_amount=Decimal("100"))
    autopilot.autospin_cycle.side_effect = list(cycles)
    controller = SpeedupController(autopilot, setup, "d", "Slot", Decimal("100"), logging.getLogger("t"))
    controller.prepare()
    return controller


def test_after_a_bet_change_the_autospin_restarts_up_to_three_times(tmp_path):
    changed = CycleResult("bet_changed", 5, Decimal("0.5"), True, 1, "cella cambiata")
    controller = _controller_with([changed] * 5, tmp_path)

    outcomes = [controller.maybe_cycle(Decimal("1"), 1, 500, False, 1, BET) for _ in range(5)]

    assert [o.recheck_bet if o else None for o in outcomes[:4]] == [True, True, True, True]
    assert outcomes[4] is None                                              # dopo il 4o cambio: tap manuale


# ---- Fix 3: turbo prima dell'autospin ------------------------------------------------------------------------------------

def test_turbo_is_explored_and_logged_before_the_autospin_even_starts(tmp_path, caplog):
    world = World(tmp_path, kind="turbo")
    setup = _setup(tmp_path, world)
    autopilot = MagicMock()
    autopilot.config = AutoPilotConfig(device_id="d", slot_name="Slot", target_played_amount=Decimal("3"),
                                       speedup_measure_spins=2)
    controller = SpeedupController(autopilot, setup, "d", "Slot", Decimal("3"), logging.getLogger("t"))

    with caplog.at_level(logging.INFO):
        controller.prepare()
        assert controller.autospin is None and controller.measuring_turbo        # l'autospin aspetta il turbo
        assert "Turbo: non ancora esplorato" in caplog.text and "Autospin: lo esplorerò dopo il turbo" in caplog.text
        assert controller.maybe_cycle(Decimal("0"), 1, 100, False, 1, BET) is None
        world.kind = "none"                                                      # nessun turbo: passa all'autospin
        for seconds in (6.0, 6.0, 6.0):
            controller.note_spin(seconds)

    assert not controller.measuring_turbo and "Turbo: assente su questa slot" in caplog.text
    assert setup.turbo_info("Slot").status == "absent"
    assert setup.autospin_info("Slot").status == "absent"                       # poi esplorato


def test_a_confirmed_turbo_is_active_before_the_autospin_is_explored(tmp_path, caplog):
    world = World(tmp_path, kind="turbo")
    setup = _setup(tmp_path, world, autospin=AutospinInfo(status="unknown"))
    autopilot = MagicMock()
    autopilot.config = AutoPilotConfig(device_id="d", slot_name="Slot", target_played_amount=Decimal("3"),
                                       speedup_measure_spins=2)
    controller = SpeedupController(autopilot, setup, "d", "Slot", Decimal("3"), logging.getLogger("t"))
    controller.prepare()
    order = []
    original = setup.explore_autospin
    setup.explore_autospin = lambda *a, **k: order.append(("autospin", world.turbo_on)) or original(*a, **k)

    for seconds in (9.0, 6.0, 6.0, 9.0, 3.0, 3.0):
        controller.note_spin(seconds)

    assert order and order[0] == ("autospin", True)                           # esplorato con il turbo gia' attivo


def test_a_known_absent_turbo_is_logged_and_the_autospin_goes_first(tmp_path, caplog):
    world = World(tmp_path, kind="none")
    setup = _setup(tmp_path, world, turbo=TurboInfo(status="absent"))
    autopilot = MagicMock()
    autopilot.config = AutoPilotConfig(device_id="d", slot_name="Slot", target_played_amount=Decimal("3"))
    controller = SpeedupController(autopilot, setup, "d", "Slot", Decimal("3"), logging.getLogger("t"))

    with caplog.at_level(logging.INFO):
        controller.prepare()

    assert "Turbo: già noto (assente)" in caplog.text and setup.autospin_info("Slot").status == "absent"


# ---- mappa: il popup imparato si ricorda ---------------------------------------------------------------------------------

def test_the_learned_gamble_popup_is_saved_in_the_map_and_read_back(tmp_path):
    memory = SlotMemory(str(tmp_path / "mem"))
    world = World(tmp_path)
    setup = _setup(tmp_path, world)
    setup.memory = memory

    assert setup.gamble_info("Slot") is None
    setup.save_gamble("Slot", (300, 600), [1, 2, 3])

    assert setup.gamble_info("Slot") == ((300, 600), [1, 2, 3])
    loaded = memory.load("Slot", (400, 800)).data
    assert loaded.gamble_popup and loaded.collect_coords == [300, 600] and loaded.gamble_signature == [1, 2, 3]
