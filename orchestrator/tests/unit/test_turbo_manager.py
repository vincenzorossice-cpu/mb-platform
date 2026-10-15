import pytest

from orchestrator.modules.slot_autopilot.slot_map import TurboInfo
from orchestrator.modules.slot_autopilot.turbo_manager import TurboManager
from orchestrator.tests.unit.autospin_world import AUTO, AUTO_IN_MENU, MENU, SPIN, Clock, World


def _manager(world, **kw):
    clock = Clock()
    return TurboManager(world.screen_reader, world.touch_injector, world.ocr, sleep=clock.sleep, **kw)


def test_a_turbo_on_the_main_screen_is_found_and_activated(tmp_path):
    world = World(tmp_path, kind="turbo")
    manager = _manager(world)

    result = manager.explore_turbo("d", SPIN)

    info = result.data
    assert result.success and info.status == "present" and not info.verified      # la conferma e' dal tempo
    assert info.location == "main" and info.type == "toggle" and world.turbo_on
    assert [step["note"] for step in info.touch_sequence] == ["turbo"] and SPIN not in world.taps


def test_a_turbo_in_the_menu_is_found(tmp_path):
    world = World(tmp_path, kind="turbo_menu")
    manager = _manager(world)

    info = manager.explore_turbo("d", SPIN).data

    assert info.status == "present" and info.location == "menu" and world.turbo_on
    assert [step["note"] for step in info.touch_sequence] == ["apre menu", "turbo"]


def test_absent_is_a_normal_outcome(tmp_path, caplog):
    world = World(tmp_path, kind="none")
    manager = _manager(world)

    with caplog.at_level("INFO"):
        result = manager.explore_turbo("d", SPIN)

    assert result.success and result.data.status == "absent" and not world.turbo_on
    assert "Turbo: non trovato su questa slot" in caplog.text


def test_the_time_must_drop_visibly_for_the_turbo_to_be_confirmed(tmp_path):
    world = World(tmp_path, kind="turbo")
    manager = _manager(world)
    info = manager.explore_turbo("d", SPIN).data

    final = manager.conclude("d", info, without=5.9, with_turbo=3.1)

    assert final.status == "present" and final.verified
    assert (final.seconds_without, final.seconds_with) == (5.9, 3.1) and world.turbo_on


def test_a_turbo_that_changes_nothing_is_switched_off_and_saved_as_absent(tmp_path):
    world = World(tmp_path, kind="turbo")
    manager = _manager(world)
    info = manager.explore_turbo("d", SPIN).data
    assert world.turbo_on

    final = manager.conclude("d", info, without=5.9, with_turbo=5.6)             # -5%: non evidente

    assert final.status == "absent" and not final.verified
    assert not world.turbo_on                                                       # secondo tocco: disattivato


def test_unmeasurable_times_do_not_confirm(tmp_path):
    world = World(tmp_path, kind="turbo")
    manager = _manager(world)
    info = manager.explore_turbo("d", SPIN).data

    assert manager.conclude("d", info, None, 3.0).status == "absent"


def test_measure_needs_at_least_two_valid_samples():
    assert TurboManager.measure_spin_time([6.0, 4.0]) == 5.0
    assert TurboManager.measure_spin_time([6.0]) is None
    assert TurboManager.measure_spin_time([0, None, "x"]) is None


def test_a_saved_turbo_is_enabled_by_replaying_its_sequence_only_if_verified(tmp_path):
    world = World(tmp_path, kind="turbo")
    manager = _manager(world)
    sequence = [{"x": AUTO[0], "y": AUTO[1], "wait": 1.0, "note": "turbo"}]

    assert not manager.enable_turbo("d", TurboInfo(status="present", verified=False, touch_sequence=sequence))
    assert not manager.enable_turbo("d", TurboInfo(status="absent", verified=True, touch_sequence=sequence))
    assert not world.turbo_on
    assert manager.enable_turbo("d", TurboInfo(status="present", verified=True, touch_sequence=sequence))
    assert world.turbo_on
