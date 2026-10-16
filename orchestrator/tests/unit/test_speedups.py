import json
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.autospin_manager import AutoSpinManager, CycleResult
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.session import play_session
from orchestrator.modules.slot_autopilot.slot_explorer import SlotExplorer
from orchestrator.modules.slot_autopilot.slot_map import AutospinInfo, Point, SlotMap, SlotMemory, TurboInfo
from orchestrator.modules.slot_autopilot.slot_setup import SlotSetup
from orchestrator.modules.slot_autopilot.speedup_controller import SpeedupController
from orchestrator.modules.slot_autopilot.turbo_manager import TurboManager
from orchestrator.tests.unit.autospin_world import AUTO, CONFIRM, OPTION_INF, SPIN, Clock, World
from orchestrator.tests.unit.test_slot_autopilot_session import _autopilot, _spin

SEQUENCE = [{"x": AUTO[0], "y": AUTO[1], "wait": 1.0, "note": "autospin"},
            {"x": OPTION_INF[0], "y": OPTION_INF[1], "wait": 1.0, "note": "numero di giri"},
            {"x": CONFIRM[0], "y": CONFIRM[1], "wait": 1.0, "note": "conferma"}]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


def _good_autospin(**kw):
    values = dict(status="present", location="main", button_coords=list(AUTO), type="infinite", verified=True,
                  stop_method="tap_spin", stop_coords=list(SPIN), touch_sequence=SEQUENCE)
    values.update(kw)
    return AutospinInfo(**values)


def _setup(tmp_path, world, demo=True, autospin=None, turbo=None):
    clock = Clock()
    explorer = SlotExplorer(None, world.screen_reader, world.touch_injector, ocr_reader=world.ocr, sleep=clock.sleep)
    explorer.autospin_manager = AutoSpinManager(world.screen_reader, world.touch_injector, world.ocr,
                                                button=world.button, sleep=clock.sleep, clock=clock)
    explorer.turbo_manager = TurboManager(world.screen_reader, world.touch_injector, world.ocr, sleep=clock.sleep)
    setup = SlotSetup(SlotMemory(str(tmp_path / "mem")), explorer, world.screen_reader, demo=demo)
    setup._maps["Slot"] = SlotMap(slot_name="Slot", screen=(400, 800), spin=Point(SPIN[0], SPIN[1], "", True),
                                  autospin=autospin, turbo=turbo)
    return setup


# ---- mappa: formato nuovo e mappe vecchie -------------------------------------------------------------------------------

def test_the_map_round_trips_autospin_and_turbo_and_old_maps_load_as_unknown(tmp_path):
    memory = SlotMemory(str(tmp_path / "mem"))
    smap = SlotMap(slot_name="Slot", screen=(400, 800), spin=Point(200, 700, "", True),
                   autospin=_good_autospin(max_spins=None, spin_options=[10, 100], period_seconds=3.2),
                   turbo=TurboInfo(status="absent"))
    assert memory.save(smap).success

    loaded = memory.load("Slot", (400, 800)).data
    assert loaded.autospin.usable() and loaded.autospin.spin_options == [10, 100] and loaded.autospin.period_seconds == 3.2
    assert loaded.autospin.touch_sequence[2]["note"] == "conferma" and loaded.turbo.status == "absent"
    assert "autospin present verificato" in loaded.summary()

    data = json.loads(open(memory.path("Slot")).read())
    del data["maps"]["400x800"]["autospin"], data["maps"]["400x800"]["turbo"]            # mappa di prima
    open(memory.path("Slot"), "w").write(json.dumps(data))
    old = memory.load("Slot", (400, 800)).data
    assert old.autospin is None and old.turbo is None and "autospin ?" in old.summary()


def test_only_a_present_verified_autospin_with_a_way_to_stop_is_usable():
    assert _good_autospin().usable()
    for broken in (dict(verified=False), dict(status="absent"), dict(stop_method=None), dict(touch_sequence=None)):
        assert not _good_autospin(**broken).usable()
    assert AutospinInfo.parse({"status": "bogus", "type": "x"}).status == "unknown"


# ---- SlotSetup / SlotExplorer: esplorazione e salvataggio --------------------------------------------------------------

def test_exploration_runs_only_in_demo_and_saves_the_outcome_in_the_map(tmp_path):
    world = World(tmp_path, kind="main")
    setup = _setup(tmp_path, world)

    result = setup.explore_autospin("d", "Slot")

    assert result.success and result.data.usable()
    assert setup.autospin_info("Slot").verified
    assert SlotMemory(str(tmp_path / "mem")).load("Slot", (400, 800)).data.autospin.verified       # salvato su disco


def test_not_in_demo_nothing_is_touched(tmp_path):
    world = World(tmp_path, kind="main")
    setup = _setup(tmp_path, world, demo=False)

    result = setup.explore_autospin("d", "Slot")

    assert not result.success and result.error_code == "AUTOSPIN_NOT_DEMO" and world.taps == []
    assert not setup.explore_turbo("d", "Slot").success and world.taps == []


def test_a_known_outcome_is_not_explored_again(tmp_path):
    world = World(tmp_path, kind="main")
    known = _setup(tmp_path, world, autospin=AutospinInfo(status="absent"))

    assert known.explore_autospin("d", "Slot").data.status == "absent" and world.taps == []
    tried_twice = _setup(tmp_path, world, autospin=AutospinInfo(status="unknown", attempts=2))
    assert tried_twice.explore_autospin("d", "Slot").data.attempts == 2 and world.taps == []


def test_absent_is_saved_so_the_next_run_does_not_search_again(tmp_path):
    world = World(tmp_path, kind="none")
    setup = _setup(tmp_path, world)

    assert setup.explore_autospin("d", "Slot").data.status == "absent"
    taps = len(world.taps)
    assert setup.explore_autospin("d", "Slot").data.status == "absent" and len(world.taps) == taps


def test_the_explorer_without_managers_fails_softly():
    explorer = SlotExplorer(None, MagicMock(), MagicMock())

    assert explorer.explore_autospin("d", "Slot").error_code == "AUTOSPIN_NO_MANAGER"
    assert explorer.explore_turbo("d").error_code == "TURBO_NO_MANAGER"


# ---- SpeedupController: preparazione e turbo ---------------------------------------------------------------------------

def _autopilot_for(setup, **config):
    autopilot = MagicMock()
    autopilot.config = AutoPilotConfig(device_id="d", slot_name="Slot", target_played_amount=Decimal("3"), **config)
    return autopilot


def test_prepare_uses_a_saved_autospin_without_exploring(tmp_path):
    world = World(tmp_path)
    setup = _setup(tmp_path, world, autospin=_good_autospin(), turbo=TurboInfo(status="absent"))
    controller = SpeedupController(_autopilot_for(setup), setup, "d", "Slot", Decimal("3"), logging.getLogger("t"))

    assert controller.prepare() is None and controller.autospin is not None and world.taps == []


def test_an_unstoppable_autospin_stops_the_session_loudly(tmp_path):
    world = World(tmp_path, kind="main", stop="never")
    setup = _setup(tmp_path, world, turbo=TurboInfo(status="absent"))
    controller = SpeedupController(_autopilot_for(setup), setup, "d", "Slot", Decimal("3"), logging.getLogger("t"))

    assert controller.prepare() == "AUTOSPIN_NOT_STOPPED"


def test_absent_autospin_logs_and_goes_on_manually(tmp_path, caplog):
    world = World(tmp_path, kind="none")
    setup = _setup(tmp_path, world, turbo=TurboInfo(status="absent"))
    controller = SpeedupController(_autopilot_for(setup), setup, "d", "Slot", Decimal("3"), logging.getLogger("t"))

    with caplog.at_level(logging.INFO):
        assert controller.prepare() is None

    assert controller.autospin is None and "Autospin: non disponibile, uso tap manuale" in caplog.text
    assert controller.maybe_cycle(Decimal("0"), 1, 100, False, 1, Decimal("0.1")) is None


def test_turbo_is_measured_with_normal_spins_and_confirmed_when_time_drops(tmp_path):
    world = World(tmp_path, kind="turbo")
    setup = _setup(tmp_path, world, autospin=AutospinInfo(status="absent"))
    controller = SpeedupController(_autopilot_for(setup, speedup_measure_spins=3), setup, "d", "Slot", Decimal("3"),
                                   logging.getLogger("t"))
    controller.prepare()
    assert controller.measuring_turbo and not world.turbo_on

    for seconds in (9.0, 6.0, 6.0, 6.0):                      # 3 spin senza turbo (il primo non conta)
        controller.note_spin(seconds)
    assert world.turbo_on and controller.measuring_turbo
    for seconds in (9.0, 3.0, 3.0, 3.0):                      # 3 spin con turbo
        controller.note_spin(seconds)

    info = setup.turbo_info("Slot")
    assert not controller.measuring_turbo and info.verified and (info.seconds_without, info.seconds_with) == (6.0, 3.0)
    assert world.turbo_on


def test_turbo_without_a_visible_gain_is_switched_off_and_saved_absent(tmp_path):
    world = World(tmp_path, kind="turbo")
    setup = _setup(tmp_path, world, autospin=AutospinInfo(status="absent"))
    controller = SpeedupController(_autopilot_for(setup, speedup_measure_spins=3), setup, "d", "Slot", Decimal("3"),
                                   logging.getLogger("t"))
    controller.prepare()

    for seconds in (6.0, 6.0, 6.0, 6.0, 6.0, 5.8, 5.9, 6.0):
        controller.note_spin(seconds)

    assert setup.turbo_info("Slot").status == "absent" and not world.turbo_on


def test_no_turbo_phase_when_the_map_already_says_absent_or_not_in_demo(tmp_path):
    world = World(tmp_path, kind="turbo")
    absent = _setup(tmp_path, world, autospin=AutospinInfo(status="absent"), turbo=TurboInfo(status="absent"))
    controller = SpeedupController(_autopilot_for(absent), absent, "d", "Slot", Decimal("3"), logging.getLogger("t"))
    controller.prepare()
    real = _setup(tmp_path, world, demo=False, autospin=AutospinInfo(status="absent"))
    other = SpeedupController(_autopilot_for(real), real, "d", "Slot", Decimal("3"), logging.getLogger("t"))
    other.prepare()

    assert not controller.measuring_turbo and not other.measuring_turbo and world.taps == []


def test_a_verified_turbo_is_enabled_at_the_start_of_the_session(tmp_path):
    world = World(tmp_path, kind="turbo")
    turbo = TurboInfo(status="present", verified=True, touch_sequence=[{"x": AUTO[0], "y": AUTO[1], "wait": 1.0}],
                      seconds_without=6.0, seconds_with=3.0)
    setup = _setup(tmp_path, world, autospin=AutospinInfo(status="absent"), turbo=turbo)
    controller = SpeedupController(_autopilot_for(setup), setup, "d", "Slot", Decimal("3"), logging.getLogger("t"))

    controller.prepare()

    assert world.turbo_on and not controller.measuring_turbo


# ---- sessione: autospin fino al 90% e poi tap manuale ----------------------------------------------------------------

def _session(tmp_path, cycles, spins, autospin=None, target="3.0", config=None):
    world = World(tmp_path)
    setup = _setup(tmp_path, world, autospin=autospin if autospin is not None else _good_autospin(),
                   turbo=TurboInfo(status="absent"))
    setup._maps["slot"] = setup._maps["Slot"]
    setup._outcomes[("dev", "slot")] = Result.ok(SimpleNamespace())
    autopilot = _autopilot(spins)
    autopilot.config = config or AutoPilotConfig(device_id="dev", slot_name="slot", target_played_amount=Decimal(target))
    autopilot.slot_setup = setup
    autopilot._current_bet = Decimal("0.1")
    autopilot.autospin_cycle.side_effect = list(cycles)
    return autopilot, play_session(autopilot, "dev", "slot", Decimal(target), 500)


def test_autospin_runs_to_90_percent_then_manual_taps_finish_the_target(tmp_path, caplog):
    cycle = CycleResult("threshold", 26, Decimal("2.6"), True, 1)
    with caplog.at_level(logging.INFO):
        autopilot, result = _session(tmp_path, [cycle], [_spin(index=i) for i in range(1, 8)])

    assert result.target_reached and not result.failed and result.total_played == Decimal("3.0")
    assert autopilot.autospin_cycle.call_count == 1
    args = autopilot.autospin_cycle.call_args.args
    assert args[3] == Decimal("2.6") and args[2] == Decimal("0.1")             # 90% di 3.0 meno il primo spin manuale
    assert autopilot.spin.call_count == 1 + 3                                   # 1 prima, 3 manuali dopo (0,3 EUR)
    assert "Autospin: fermato al 90% del target, passo a tap manuale" in caplog.text
    assert result.spins == 30


def test_autospin_is_not_used_when_absent_or_unverified(tmp_path, caplog):
    for info in (AutospinInfo(status="absent"), _good_autospin(verified=False)):
        with caplog.at_level(logging.INFO):
            autopilot, result = _session(tmp_path, [], [_spin(index=i) for i in range(1, 8)], autospin=info, target="0.5")
        assert result.target_reached and autopilot.autospin_cycle.call_count == 0 and autopilot.spin.call_count == 5


def test_autospin_disabled_in_the_config_keeps_the_manual_behaviour(tmp_path):
    config = AutoPilotConfig(device_id="dev", slot_name="slot", target_played_amount=Decimal("0.5"),
                             autospin_enabled=False)
    autopilot, result = _session(tmp_path, [], [_spin(index=i) for i in range(1, 8)], target="0.5", config=config)

    assert autopilot.autospin_cycle.call_count == 0 and result.target_reached


def test_a_changed_bet_during_the_autospin_rereads_it_and_goes_manual(tmp_path):
    cycle = CycleResult("bet_changed", 12, Decimal("1.2"), True, 1, "la cella di puntata selezionata e' cambiata")
    after = CycleResult("threshold", 10, Decimal("1.0"), True, 1)
    autopilot, result = _session(tmp_path, [cycle, after], [_spin(index=i) for i in range(1, 30)])

    assert autopilot.set_min_bet.call_count == 2                                # iniziale + dopo l'autospin
    assert autopilot.autospin_cycle.call_count == 2 and result.target_reached   # poi l'autospin RIPARTE


def test_an_autospin_that_does_not_stop_ends_the_session_with_an_error(tmp_path):
    cycle = CycleResult("stop_failed", 5, Decimal("0.5"), False, 1, "l'autospin non si e' fermato")
    autopilot, result = _session(tmp_path, [cycle], [_spin(index=i) for i in range(1, 30)])

    assert result.failed and result.stop_reason == "AUTOSPIN_NOT_STOPPED"


def test_a_failed_cycle_turns_the_autospin_off_for_the_rest_of_the_session(tmp_path):
    cycle = CycleResult("needs_attention", 3, Decimal("0.3"), True, 1, "saldo illeggibile")
    autopilot, result = _session(tmp_path, [cycle, cycle], [_spin(index=i) for i in range(1, 30)])

    assert autopilot.autospin_cycle.call_count == 1 and result.target_reached


def test_free_spins_and_the_first_manual_spin_come_before_any_autospin(tmp_path):
    cycle = CycleResult("threshold", 20, Decimal("2.0"), True, 1)
    autopilot, _ = _session(tmp_path, [cycle], [_spin(index=i) for i in range(1, 30)])

    first_calls = [c[0] for c in autopilot.mock_calls if c[0] in ("spin", "autospin_cycle")]
    assert first_calls[0] == "spin"                                              # mai l'autospin prima del primo spin vero


# ---- faccia dell'autopilot e configurazione ---------------------------------------------------------------------------

def test_the_autopilot_facade_passes_the_spin_point_and_without_a_manager_returns_none():
    from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
    from orchestrator.tests.unit.test_slot_autopilot_autopilot import _collaborators, _config
    autopilot = SlotAutoPilot(**_collaborators(config=_config()))
    assert autopilot.autospin_cycle("d", _good_autospin(), Decimal("0.1"), Decimal("1"), 50, 5.0) is None

    manager = MagicMock()
    autopilot.autospin_manager = manager
    autopilot.slot_setup = SimpleNamespace(spin_point=lambda slot: (200, 700))
    autopilot.autospin_cycle("d", _good_autospin(), Decimal("0.1"), Decimal("1"), 50, 5.0)

    assert manager.run_cycle.call_args.args[3] == (200, 700) and manager.run_cycle.call_args.args[1] == "pirots5"


def test_the_new_options_have_sensible_defaults_and_are_validated():
    config = AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"))
    assert config.autospin_enabled and config.autospin_sample_interval == 15 and config.autospin_stop_threshold == 0.9
    assert config.validate().success
    bad = AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"), autospin_stop_threshold=1.5)
    assert bad.validate().error_code == "CONFIG_INVALID"
    bad = AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"), autospin_sample_interval=0)
    assert bad.validate().error_code == "CONFIG_INVALID"
