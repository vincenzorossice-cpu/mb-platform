"""I jackpot in alto (milioni) non sono mai il saldo; la puntata si ricorda per tutta la sessione; un menu aperto dal
turbo non deve lasciare lo SPIN coperto."""
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.slot_map import TurboInfo
from orchestrator.modules.slot_autopilot.speedup_controller import SpeedupController
from orchestrator.modules.slot_autopilot.turbo_manager import TurboManager
from orchestrator.modules.slot_ocr.reader import SlotOCRReader, is_jackpot_balance, parse_bet, parse_saldo
from orchestrator.modules.slot_ocr.vision_reader import ResilientOCRReader
from orchestrator.tests.unit.autospin_world import AUTO, SPIN, Clock, World
from orchestrator.tests.unit.test_slot_autopilot_supervisor import _ocr, _vision

JACKPOTS = ["15 147 514.37", "1 133 076.33", "66335.03", "6054.74"]


# ---- Fix 1: parse_saldo -----------------------------------------------------------------------------------------------

def test_the_value_after_the_label_wins_over_the_jackpots():
    assert parse_saldo(JACKPOTS + ["SALDO:", "49 999.30", "ULTIMA VINCITA:", "1.00"]) == 49999.30


def test_two_huge_jackpots_before_the_label_are_ignored():
    assert parse_saldo(["15147513.58", "1133076.33", "SALDO:", "49999.30"]) == 49999.30


def test_a_lone_plausible_balance_is_the_balance():
    assert parse_saldo(["49999.30"], last_saldo=50000) == 49999.30


def test_only_a_jackpot_falls_back_to_the_previous_balance():
    assert parse_saldo(["15147513.58"], last_saldo=50000) == 50000


def test_a_balance_before_its_label_is_still_found():
    assert parse_saldo(["50000.00", "SALDO:"]) == 50000


def test_no_candidate_and_no_previous_balance_is_unknown():
    assert parse_saldo(["FUN", "SHINING CROWN"]) is None
    assert parse_saldo(["15147513.58"]) is None


def test_jackpot_rule():
    assert is_jackpot_balance(15147513.58, 50000) and is_jackpot_balance(600000, None)
    assert is_jackpot_balance(600, 50) and not is_jackpot_balance(49999.3, 50000) and not is_jackpot_balance(60000, 50000)


def _det(text, x, y, w=120, h=40):
    return ([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], text, 0.9)


def test_the_ocr_reader_gives_the_balance_after_the_label_not_a_jackpot():
    reader = SlotOCRReader()
    reader._get_raw_detections = lambda path: [
        _det("15 147 514.37", 100, 100), _det("1 133 076.33", 500, 100), _det("66335.03", 100, 170),
        _det("SALDO:", 100, 1900), _det("49 999.30", 300, 1900), _det("ULTIMA VINCITA:", 600, 1900),
        _det("1.00", 900, 1900)]

    assert reader.read_slot_state("x.png")["saldo"] == 49999.30


def _good(saldo, posta=0.10):
    return {"saldo": saldo, "posta": posta, "raw_texts": []}


def test_ocr_and_vision_agreeing_on_a_jackpot_are_still_discarded_and_the_last_balance_is_used(caplog):
    """Il caso del log (spin 4): saldo 15147513.58 letto da entrambi i lettori."""
    vision = _vision(_good(49999.30), _good(15147513.58), _good(15147513.58))
    reader = ResilientOCRReader(_ocr([_good(49999.30), _good(15147513.58), _good(15147513.58)]), vision,
                                capture=lambda: "/second.png", confirm_delay=0)
    first = reader.read_slot_state("/a.png")

    with caplog.at_level(logging.INFO):
        second = reader.read_slot_state("/b.png")

    assert first["saldo"] == 49999.30 and second["saldo"] == 49999.30
    assert "Saldo scartato: 15147513.58 (jackpot, > 10× precedente)" in caplog.text
    assert "Saldo non leggibile, uso ultimo saldo noto: 49999.3" in caplog.text


def test_a_jackpot_never_becomes_the_trusted_reference():
    reader = ResilientOCRReader(_ocr([_good(15147513.58)]), _vision(_good(15147513.58)),
                                capture=lambda: "/second.png", confirm_delay=0)

    state = reader.read_slot_state("/a.png")

    assert state["saldo"] is None and reader._trusted["saldo"] is None        # nessun precedente: tetto assoluto


def test_the_balance_source_is_logged_once_per_change(caplog):
    reader = ResilientOCRReader(_ocr([_good(100.0), _good(99.9), _good(99.8)]), _vision(_good(100.0)),
                                capture=lambda: "/second.png", confirm_delay=0)

    with caplog.at_level(logging.INFO):
        for path in ("/1.png", "/2.png", "/3.png"):
            reader.read_slot_state(path)

    assert caplog.text.count("Saldo letto:") == 1 and "(dopo etichetta SALDO:)" in caplog.text


# ---- Fix 2: last_known_bet -----------------------------------------------------------------------------------------------

def _grid_reader(last=None):
    reader = SlotOCRReader()
    reader._get_raw_detections = lambda path: [
        _det("SALDO:", 100, 1900), _det("50 000.00", 300, 1900),
        *[_det(v, 120 + 200 * i, 1200) for i, v in enumerate(("0.10", "0.20", "0.30", "0.40", "0.50"))],
        *[_det("PUNTATA", 100 + 200 * i, 1230) for i in range(5)]]
    reader.last_known_bet = last
    return reader


def test_a_grid_with_a_known_bet_uses_it(caplog):
    with caplog.at_level(logging.WARNING):
        state = _grid_reader(last=0.10).read_slot_state("x.png")

    assert state["posta"] == 0.10 and "uso ultimo valore noto: 0.10" in caplog.text


def test_a_grid_with_no_known_bet_is_not_guessed():
    assert _grid_reader(last=None).read_slot_state("x.png")["posta"] is None


def test_a_single_value_is_read_and_remembered():
    reader = SlotOCRReader()
    reader._get_raw_detections = lambda path: [_det("PUNTATA", 100, 500), _det("0.20", 300, 505)]

    assert reader.read_slot_state("x.png")["posta"] == 0.20 and reader.last_known_bet == 0.20


def test_a_win_followed_by_an_unreadable_bet_does_not_block_the_session():
    reader = _grid_reader(last=None)
    reader.note_bet(Decimal("0.10"))                     # lo ha impostato set_min_bet

    assert reader.read_slot_state("x.png")["posta"] == 0.10


def test_without_a_bet_label_the_last_known_bet_is_not_applied():
    reader = SlotOCRReader()
    reader._get_raw_detections = lambda path: [_det("LOBBY", 100, 500), _det("50 000.00", 300, 505)]
    reader.last_known_bet = 0.10

    assert reader.read_slot_state("x.png")["posta"] is None


def test_a_successful_read_updates_the_reference_but_the_fallback_does_not():
    reader = SlotOCRReader()
    reader._get_raw_detections = lambda path: [_det("PUNTATA", 100, 500), _det("0.30", 300, 505)]
    reader.read_slot_state("x.png")
    assert reader.last_known_bet == 0.30
    reader._get_raw_detections = _grid_reader().__dict__["_get_raw_detections"]
    reader.read_slot_state("y.png")                        # griglia: usa 0.30 ma non lo "riconferma"
    assert reader.last_known_bet == 0.30


def test_parse_bet_still_follows_the_requested_rules():
    assert parse_bet(["0.10", "0.20", "0.30"], last_tapped_bet=0.10) == 0.10
    assert parse_bet(["0.10", "0.20", "0.30"]) is None and parse_bet(["0.10"]) == 0.10


def test_the_resilient_reader_forwards_the_bet_and_the_autopilot_reports_it():
    from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
    from orchestrator.modules.slot_autopilot.bet_controller import BetResult
    from orchestrator.tests.unit.test_slot_autopilot_autopilot import _collaborators, _config
    inner = SlotOCRReader()
    wrapper = ResilientOCRReader(inner, None)
    wrapper.note_bet(Decimal("0.10"))
    assert inner.last_known_bet == 0.10

    deps = _collaborators(config=_config(bet_watch_enabled=False))
    deps["ocr_reader"] = wrapper
    deps["bet_controller"].set_min_bet.return_value = Result.ok(BetResult(
        slot_name="pirots5", starting_bet=Decimal("1"), final_bet=Decimal("0.20"), min_bet_verified=True, attempts=1))
    SlotAutoPilot(**deps).set_min_bet("dev")

    assert inner.last_known_bet == 0.20


# ---- Fix 3: il turbo che apre un menu --------------------------------------------------------------------------------------

def _turbo(world, **kw):
    clock = Clock()
    return TurboManager(world.screen_reader, world.touch_injector, world.ocr, sleep=clock.sleep,
                        ready_probe=lambda path: not world.states[path]["panel"], **kw)


def _menu_opener_world(tmp_path, closable=True):
    """TURBO e' in realta' un pulsante che apre un pannello (Ultra Play): lo SPIN resta coperto finche' non si chiude."""
    world = World(tmp_path, kind="main")
    world.ocr.read_text_boxes = lambda path: Result.ok(
        [SimpleNamespace(text="TURBO", bbox=(AUTO[0] - 30, AUTO[1] - 15, AUTO[0] + 30, AUTO[1] + 15), confidence=1)]
        if not world.states[path]["panel"] else
        [SimpleNamespace(text="CHIUDI" if closable else "ULTRA PLAY", bbox=(190, 740, 250, 770), confidence=1)])
    return world


def test_a_turbo_candidate_that_opens_a_menu_is_closed_and_is_not_a_toggle(tmp_path, caplog):
    world = _menu_opener_world(tmp_path)
    original = world._tap

    def tap(device, x, y):
        if world.panel_open and abs(x - 220) < 60 and abs(y - 755) < 40:       # tocco su "CHIUDI"
            world.panel_open = False
            world.taps.append((x, y))
            return Result.ok(True)
        return original(device, x, y)

    world.touch_injector.tap_coordinates = tap

    with caplog.at_level(logging.INFO):
        result = _turbo(world).explore_turbo("d", SPIN)

    assert result.success and result.data.status == "absent" and not world.panel_open
    assert "il tocco ha aperto un menu, non e' un interruttore: chiudo" in caplog.text


def test_a_menu_that_cannot_be_closed_is_an_error_for_the_caller(tmp_path):
    world = _menu_opener_world(tmp_path, closable=False)
    manager = _turbo(world, press_back=None)
    manager.tools.press_back = None
    original = world._tap
    # il secondo tocco sull'icona (extra_taps) la richiude: lo impediamo
    world.touch_injector.tap_coordinates = lambda d, x, y: (
        Result.ok(True) if world.panel_open else original(d, x, y))

    result = manager.explore_turbo("d", SPIN)

    assert not result.success and result.error_code == "TURBO_MENU_STUCK"


def test_a_real_toggle_that_leaves_the_spin_free_is_accepted_without_closing_anything(tmp_path):
    world = World(tmp_path, kind="turbo")
    manager = TurboManager(world.screen_reader, world.touch_injector, world.ocr, sleep=Clock().sleep,
                           ready_probe=lambda path: True)

    result = manager.explore_turbo("d", SPIN)

    assert result.data.status == "present" and world.taps == [AUTO] and world.turbo_on


def test_a_turbo_inside_a_menu_closes_the_menu_before_the_measurement(tmp_path):
    world = World(tmp_path, kind="turbo_menu")
    probes = []
    manager = TurboManager(world.screen_reader, world.touch_injector, world.ocr, sleep=Clock().sleep,
                           ready_probe=lambda path: probes.append(path) or not world.states[path]["menu"])

    result = manager.explore_turbo("d", SPIN)

    assert result.data.status == "present" and world.turbo_on and not world.menu_open     # menu richiuso


def test_disabling_a_turbo_in_a_menu_reopens_the_menu_and_closes_it_again(tmp_path):
    world = World(tmp_path, kind="turbo_menu")
    manager = TurboManager(world.screen_reader, world.touch_injector, world.ocr, sleep=Clock().sleep,
                           ready_probe=lambda path: not world.states[path]["menu"])
    info = manager.explore_turbo("d", SPIN).data
    assert world.turbo_on and not world.menu_open

    manager.disable_turbo("d", info)

    assert not world.turbo_on and not world.menu_open


# ---- rete di sicurezza in sessione: lo SPIN non risponde ------------------------------------------------------------------

def _controller(tmp_path, world, probe):
    from orchestrator.tests.unit.test_speedups import _autopilot_for, _setup
    setup = _setup(tmp_path, world, autospin=None, turbo=TurboInfo(status="absent"))
    setup.explorer.turbo_manager = TurboManager(world.screen_reader, world.touch_injector, world.ocr,
                                                sleep=Clock().sleep, ready_probe=probe)
    return SpeedupController(_autopilot_for(setup), setup, "d", "Slot", Decimal("3"), logging.getLogger("t")), setup


def test_blocked_spin_with_an_open_menu_closes_it_and_retries(tmp_path, caplog):
    world = _menu_opener_world(tmp_path)
    world.panel_open = True
    original = world._tap

    def tap(device, x, y):
        if world.panel_open and abs(x - 220) < 60 and abs(y - 755) < 40:
            world.panel_open = False
            return Result.ok(True)
        return original(device, x, y)

    world.touch_injector.tap_coordinates = tap
    controller, _ = _controller(tmp_path, world, lambda path: not world.states[path]["panel"])

    with caplog.at_level(logging.INFO):
        assert controller.recover_blocked_spin() is True

    assert not world.panel_open and "Turbo: menu aperto blocca SPIN, chiudo menu" in caplog.text


def test_blocked_spin_with_a_menu_that_stays_open_disables_the_turbo_and_saves_absent(tmp_path):
    world = _menu_opener_world(tmp_path, closable=False)
    world.panel_open = True
    world.touch_injector.tap_coordinates = lambda d, x, y: Result.ok(True)
    controller, setup = _controller(tmp_path, world, lambda path: not world.states[path]["panel"])
    controller._turbo_info = TurboInfo(status="present", touch_sequence=[{"x": AUTO[0], "y": AUTO[1], "wait": 1.0}])

    assert controller.recover_blocked_spin() is True
    assert setup.turbo_info("Slot").status == "absent"


def test_blocked_spin_with_a_free_spin_button_is_not_a_menu_problem(tmp_path):
    world = World(tmp_path, kind="none")
    controller, _ = _controller(tmp_path, world, lambda path: True)

    assert controller.recover_blocked_spin() is False and world.taps == []


def test_the_session_tries_to_recover_before_giving_up_on_no_effect_spins(tmp_path):
    from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
    from orchestrator.modules.slot_autopilot.session import play_session
    from orchestrator.modules.slot_autopilot.spin_controller import SpinResult
    from orchestrator.tests.unit.test_slot_autopilot_session import _autopilot, _spin
    from orchestrator.tests.unit.test_speedups import _setup
    world = World(tmp_path, kind="none")
    setup = _setup(tmp_path, world, autospin=None, turbo=TurboInfo(status="absent"))
    setup._maps["slot"] = setup._maps["Slot"]
    setup._outcomes[("dev", "slot")] = Result.ok(SimpleNamespace())
    no_effect = lambda i: Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal("0.10"),
                                               balance_before=Decimal("100"), balance_after=Decimal("100"),
                                               win_amount=None, verified=False, attempts=1, outcome="no_effect"))
    autopilot = _autopilot([no_effect(1), no_effect(2), no_effect(3)] + [_spin(index=i) for i in range(4, 12)])
    autopilot.config = AutoPilotConfig(device_id="dev", slot_name="slot", target_played_amount=Decimal("0.30"))
    autopilot.slot_setup = setup
    setup.explorer.turbo_manager = SimpleNamespace(close_open_menu=MagicMock(return_value="closed"))

    result = play_session(autopilot, "dev", "slot", Decimal("0.30"), 50)

    assert result.target_reached and not result.failed
    setup.explorer.turbo_manager.close_open_menu.assert_called_once()
