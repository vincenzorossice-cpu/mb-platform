import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.modules.slot_autopilot.autospin_manager import AutoSpinManager, SampleReport
from orchestrator.modules.slot_autopilot.slot_map import AutospinInfo
from orchestrator.tests.unit.autospin_world import (
    AUTO, AUTO_IN_MENU, CONFIRM, MENU, OPTION_INF, SPIN, STOP_BUTTON, Clock, World,
)

BET = Decimal("0.10")
SEQUENCE = [{"x": AUTO[0], "y": AUTO[1], "wait": 1.0, "note": "autospin"},
            {"x": OPTION_INF[0], "y": OPTION_INF[1], "wait": 1.0, "note": "numero di giri"},
            {"x": CONFIRM[0], "y": CONFIRM[1], "wait": 1.0, "note": "conferma"}]


def _manager(world, clock=None, **kw):
    clock = clock or Clock()
    return AutoSpinManager(world.screen_reader, world.touch_injector, world.ocr, button=world.button,
                           sleep=clock.sleep, clock=clock, sample_interval=15, **kw), clock


def _info(**kw):
    values = dict(status="present", location="main", button_coords=list(AUTO), type="infinite", verified=True,
                  stop_method="tap_spin", stop_coords=list(SPIN), touch_sequence=SEQUENCE)
    values.update(kw)
    return AutospinInfo(**values)


# ---- esplorazione (SlotExplorer: test 10-13 della richiesta) --------------------------------------------------------

def test_found_on_the_main_screen_verified_by_its_effect_and_stop_is_proven(tmp_path):
    world = World(tmp_path, kind="main")
    manager, _ = _manager(world)

    result = manager.explore_autospin("d", "Slot", SPIN)

    info = result.data
    assert result.success and info.status == "present" and info.verified and info.location == "main"
    assert info.type == "infinite" and info.max_spins is None and info.spin_options == [10, 25, 100]
    assert info.stop_method == "tap_spin" and info.stop_coords == list(SPIN)
    assert [step["note"] for step in info.touch_sequence] == ["autospin", "numero di giri", "conferma"]
    assert not world.running                                       # alla fine l'autospin e' fermo
    assert SPIN not in world.taps[:world.taps.index(CONFIRM)]      # lo SPIN non e' mai un candidato


def test_a_stop_button_is_found_by_ocr_when_the_spin_does_not_stop_it(tmp_path):
    world = World(tmp_path, kind="main", stop="tap_stop")
    manager, _ = _manager(world)

    info = manager.explore_autospin("d", "Slot", SPIN).data

    assert info.verified and info.stop_method == "tap_stop" and info.stop_coords == list(STOP_BUTTON)


def test_found_inside_the_menu_when_the_main_screen_has_nothing(tmp_path):
    world = World(tmp_path, kind="menu")
    manager, _ = _manager(world)

    info = manager.explore_autospin("d", "Slot", SPIN).data

    assert info.status == "present" and info.location == "menu" and info.verified
    assert info.touch_sequence[0]["note"] == "apre menu" and (MENU[0], MENU[1]) in world.taps


def test_no_autospin_is_a_normal_outcome_not_an_error(tmp_path, caplog):
    world = World(tmp_path, kind="none")
    manager, _ = _manager(world)

    with caplog.at_level("INFO"):
        result = manager.explore_autospin("d", "Slot", SPIN)

    assert result.success and result.data.status == "absent" and not result.data.verified
    assert "Autospin: non trovato su questa slot" in caplog.text
    assert SPIN not in world.taps


def test_a_direct_start_control_is_accepted_by_its_effect(tmp_path):
    world = World(tmp_path, kind="direct")
    manager, _ = _manager(world)

    info = manager.explore_autospin("d", "Slot", SPIN).data

    assert info.status == "present" and info.verified and len(info.touch_sequence) == 1


def test_without_a_proven_stop_it_is_never_enabled_and_the_operator_is_asked(tmp_path):
    world = World(tmp_path, kind="main", stop="never")
    asked = []

    def operator(prompt):
        asked.append(prompt)
        world.running = False                                       # l'operatore lo ferma a mano
        return ""

    manager, _ = _manager(world, ask_operator=operator)

    result = manager.explore_autospin("d", "Slot", SPIN)

    assert asked and result.success and result.data.status == "unknown" and not result.data.verified
    assert result.data.attempts == 1 and not result.data.usable()


def test_an_autospin_that_cannot_be_stopped_is_an_error_without_an_operator(tmp_path):
    world = World(tmp_path, kind="main", stop="never")
    manager, _ = _manager(world)

    result = manager.explore_autospin("d", "Slot", SPIN)

    assert not result.success and result.error_code == "AUTOSPIN_RUNNING"


def test_a_control_that_does_not_make_the_slot_run_is_not_an_autospin(tmp_path):
    world = World(tmp_path, kind="main")
    world._tap = lambda *a: SimpleNamespace(success=True)           # il tocco non fa niente
    world.touch_injector = SimpleNamespace(tap_coordinates=world._tap)
    manager, _ = _manager(world)

    result = manager.explore_autospin("d", "Slot", SPIN)

    assert result.data.status == "absent"


# ---- esecuzione e monitoraggio -------------------------------------------------------------------------------------

def test_infinite_autospin_is_sampled_every_15_spins_with_one_balance_reading_each(tmp_path):
    world = World(tmp_path)
    manager, clock = _manager(world)
    readings = []
    original = manager.tools.balance
    manager.tools.balance = lambda path: readings.append(path) or original(path)

    result = manager.run_cycle("d", "Slot", _info(), SPIN, BET, Decimal("3.0"), 1000, period=5.0)

    assert result.reason == "threshold" and result.stopped and not world.running
    assert result.credited_spins == 30 and result.credited_played == Decimal("3.0")      # si ferma al budget, non oltre
    assert clock.now - 1000 >= 2 * 75                                  # campioni ogni 15 giri x 5 s = 75 s
    assert len(readings) < 40                                          # pochissime letture del saldo, non una per giro


def test_the_credit_is_always_conservative_never_above_the_real_spins(tmp_path):
    world = World(tmp_path)
    manager, clock = _manager(world)

    result = manager.run_cycle("d", "Slot", _info(), SPIN, BET, Decimal("5.0"), 1000, period=5.0)

    real_spins = (clock.now - 1000) / 5.0                              # il tempo trascorso / periodo vero
    assert 0 < result.credited_spins <= real_spins


def test_fixed_cycle_that_ends_is_restarted(tmp_path, caplog):
    world = World(tmp_path, fixed=True)
    world.stall_after_shots = 8                                      # il ciclo finisce da solo
    manager, _ = _manager(world)

    with caplog.at_level("INFO"):
        result = manager.run_cycle("d", "Slot", _info(type="fixed", max_spins=100, counter_location="button"),
                                   SPIN, BET, Decimal("6.0"), 1000, period=5.0)

    assert result.cycles >= 2 and result.reason in ("threshold", "budget")
    assert "ciclo completato, riavvio" in caplog.text


def test_a_popup_that_stops_it_is_closed_and_the_autospin_restarted(tmp_path, caplog):
    world = World(tmp_path)
    manager, clock = _manager(world)
    original = manager.monitor_sample
    calls = {"n": 0}

    def sample(device, slot, run, bet):
        calls["n"] += 1
        if calls["n"] == 2:
            world.running, world.popup = False, True                 # compare un popup e l'autospin si ferma
        return original(device, slot, run, bet)

    manager.monitor_sample = sample

    with caplog.at_level("INFO"):
        result = manager.run_cycle("d", "Slot", _info(), SPIN, BET, Decimal("6.0"), 1000, period=5.0)

    assert result.reason in ("threshold", "budget") and result.cycles >= 2
    assert (world.taps.count((200, 400)) >= 1)                       # tocco al centro per chiudere il popup
    assert "fermato da popup, riavvio" in caplog.text


def test_a_changed_bet_cell_stops_everything(tmp_path):
    world = World(tmp_path)
    watch = MagicMock()
    watch.check.side_effect = [False, True]
    manager, _ = _manager(world, bet_watch=watch)

    result = manager.run_cycle("d", "Slot", _info(), SPIN, BET, Decimal("20"), 1000, period=5.0)

    assert result.reason == "bet_changed" and result.stopped and not world.running


def test_low_balance_stops_before_the_money_runs_out(tmp_path):
    world = World(tmp_path, balance="1.50")
    manager, _ = _manager(world)

    result = manager.run_cycle("d", "Slot", _info(), SPIN, BET, Decimal("50"), 1000, period=5.0)

    assert result.reason == "low_balance" and not world.running


def test_a_start_that_does_not_run_is_reported_without_touching_anything_else(tmp_path):
    world = World(tmp_path, kind="none")
    manager, _ = _manager(world)

    result = manager.run_cycle("d", "Slot", _info(), SPIN, BET, Decimal("5"), 1000, period=5.0)

    assert result.reason == "start_failed" and not world.running


def test_an_unverified_or_unstoppable_autospin_cannot_start(tmp_path):
    world = World(tmp_path)
    manager, _ = _manager(world)

    for info in (_info(verified=False), _info(stop_method=None), _info(status="absent"), _info(touch_sequence=None)):
        assert manager.run_cycle("d", "Slot", info, SPIN, BET, Decimal("5"), 1000, 5.0).reason == "start_failed"
    assert world.taps == []


def test_a_counter_on_the_button_gives_the_exact_spin_count(tmp_path):
    world = World(tmp_path, fixed=True)
    manager, _ = _manager(world)
    shot = manager.tools.shot("d")
    run = SimpleNamespace(counter_start=100, counter_credited=0, spin_point=SPIN, period=5.0)

    credited = manager._spins_since(run, shot, 75.0)
    # senza contatore leggibile nello screenshot (autospin fermo): ripiego sul tempo, prudente
    assert credited == int(75 // (5.0 * manager.period_safety))


def test_unreadable_balance_three_samples_in_a_row_needs_attention(tmp_path):
    world = World(tmp_path)
    manager, _ = _manager(world)
    run = manager.start_autospin("d", "Slot", _info(), SPIN, 5.0).data
    world.ocr.read_slot_state = lambda path: {}

    states = [manager.monitor_sample("d", "Slot", run, BET).state for _ in range(3)]

    assert states == ["ok", "ok", "needs_attention"]
