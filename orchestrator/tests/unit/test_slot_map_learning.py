import logging
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import (
    load_autopilot_element_memory, load_autopilot_flag, load_autopilot_value, save_autopilot_value,
)
from orchestrator.modules.screen_navigator.direct_point import build_direct_refiner
from orchestrator.modules.slot_autopilot import session
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.slot_explorer import SlotExplorer
from orchestrator.modules.slot_autopilot.slot_map import Point, SlotMap, SlotMemory
from orchestrator.modules.slot_autopilot.slot_setup import EXPLORED, NOT_MAPPED, USED, SlotSetup
from orchestrator.modules.slot_autopilot.spin_controller import SpinResult

SIZE = (200, 400)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)         # la memoria "legacy" di spin e icona vive in ./slot_memory
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.slot_explorer.time.monotonic", lambda: 0.0)


def _good_map(slot="Wrap", **kw):
    data = dict(slot_name=slot, screen=SIZE, spin=Point(100, 360, "spin", True),
                bet_menu_open=Point(60, 380, "monete", True), bet_menu_type="carousel", close_method="tap_outside",
                close_point=Point(100, 40, "fuori", True), min_bet="0.20", min_bet_proven=True,
                exit_point=Point(10, 10), explored_at="2026-10-08T10:00:00+00:00")
    data.update(kw)
    return SlotMap(**data)


# ---- 1. SlotMemory ------------------------------------------------------------------------------------------

class TestSlotMemory:
    def test_save_load_roundtrip_and_validation(self, tmp_path):
        memory = SlotMemory(str(tmp_path / "mem"))
        assert memory.save(_good_map()).success

        loaded = memory.load("Wrap", SIZE)

        assert loaded.success and loaded.data.spin.x == 100 and loaded.data.bet_menu_type == "carousel"
        assert not any(name.endswith(".tmp") for name in os.listdir(tmp_path / "mem"))   # scrittura atomica

    def test_two_screen_sizes_do_not_overwrite_each_other(self, tmp_path):
        memory = SlotMemory(str(tmp_path / "mem"))
        memory.save(_good_map())
        memory.save(_good_map(screen=(300, 600), spin=Point(150, 540, "spin", True)))

        assert memory.load("Wrap", SIZE).data.spin.x == 100
        assert memory.load("Wrap", (300, 600)).data.spin.x == 150
        assert memory.load("Wrap", (111, 222)).error_code == "SLOT_MAP_MISSING"

    def test_missing_corrupted_and_unverified_maps_are_not_usable(self, tmp_path):
        memory = SlotMemory(str(tmp_path / "mem"))
        assert memory.load("Wrap", SIZE).error_code == "SLOT_MAP_MISSING"
        os.makedirs(tmp_path / "mem")
        open(memory.path("Wrap"), "w").write("{ rotto")
        assert memory.load("Wrap", SIZE).error_code == "SLOT_MAP_MISSING"
        memory.save(_good_map(spin=Point(100, 360, "spin", False)))     # file valido ma spin non verificato
        assert memory.load("Wrap", SIZE).error_code == "SLOT_MAP_MISSING"

    def test_apply_seeds_the_engine_memory_over_a_dirty_one(self, tmp_path):
        memory = SlotMemory(str(tmp_path / "mem"))
        save_autopilot_value("Wrap", "min_bet", "1.4", str(tmp_path / "mem"))     # ricordo sporco

        seeded = memory.apply_to_memory(_good_map())

        assert seeded.data == 3                                          # spin, icona del menu, punto di chiusura
        elements = load_autopilot_element_memory("Wrap", str(tmp_path / "mem")).data["elements"]
        assert elements["bet_menu_close"]["x"] == 100
        assert load_autopilot_flag("Wrap", "bet_via_menu", str(tmp_path / "mem")) is True
        assert load_autopilot_flag("Wrap", "bet_row_static", str(tmp_path / "mem")) is False      # carosello
        assert load_autopilot_value("Wrap", "min_bet", str(tmp_path / "mem")) == "0.20"
        assert load_autopilot_value("Wrap", "min_bet_trusted", str(tmp_path / "mem")) == "0.20"

    def test_list_menu_and_plus_minus_set_the_right_flags(self, tmp_path):
        memory = SlotMemory(str(tmp_path / "mem"))
        memory.apply_to_memory(_good_map(bet_menu_type="list"))
        assert load_autopilot_flag("Wrap", "bet_row_static", str(tmp_path / "mem")) is True
        memory.apply_to_memory(_good_map(bet_menu_type="plus_minus", bet_menu_open=None,
                                         bet_minus=Point(30, 300, "meno", True)))
        assert load_autopilot_flag("Wrap", "bet_via_menu", str(tmp_path / "mem")) is False


# ---- 2. SlotExplorer con telefono e Sol finti ------------------------------------------------------------------

def _img(tmp_path, name, color):
    path = str(tmp_path / name)
    Image.new("RGB", SIZE, color).save(path)
    return path


class FakePhone:
    """closed -> (tocco su menu) -> menu -> (tocco fuori) -> closed; closed -> (tocco su spin) -> spun."""
    def __init__(self, tmp_path, menu_opens=True, spin_charges=True):
        self.paths = {"closed": _img(tmp_path, "c.png", "white"), "menu": _img(tmp_path, "m.png", "gray"),
                      "spun": _img(tmp_path, "s.png", "black")}
        self.state, self.taps, self.menu_opens, self.spin_charges = "closed", [], menu_opens, spin_charges

    def _screenshot(self, device):
        return Result.ok(self.paths[self.state])

    def tap_coordinates(self, device, x, y):
        self.taps.append((x, y))
        if self.state == "closed" and abs(x - 60) < 15 and abs(y - 380) < 15 and self.menu_opens:
            self.state = "menu"
        elif self.state == "menu" and y < 100:
            self.state = "closed"
        elif self.state == "closed" and abs(x - 100) < 15 and abs(y - 360) < 15:
            self.state = "spun" if self.spin_charges else "closed"
        return Result.ok(True)

    def read_slot_state(self, path):
        return {"saldo": 99.8 if path == self.paths["spun"] else 100.0}


def _sol(overview, menu):
    gpt = MagicMock()
    gpt._VISION_MAX_TOKENS = 300
    gpt._usage_by_purpose = {}

    def create(messages, max_tokens=None, purpose=""):
        gpt._usage_by_purpose[purpose] = gpt._usage_by_purpose.get(purpose, 0) + 1
        return Result.ok(overview if "bet selection menu has just been opened" not in messages[0]["content"] else menu)
    gpt._create_completion.side_effect = create
    return gpt


OVERVIEW = {"spin": {"x": 500, "y": 900, "description": "spin"}, "bet_menu_open": {"x": 300, "y": 950},
            "bet_menu_type": "carousel", "exit": {"x": 50, "y": 25, "description": "esci"}, "notes": ""}
MENU = {"menu_type": "carousel", "lowest_visible_bet": "1,40", "cells_cut_at_edge": True,
        "close": {"method": "tap_outside", "x": 500, "y": 100}}


class TestSlotExplorer:
    def _explorer(self, tmp_path, phone=None, overview=OVERVIEW, describe=None):
        phone = phone or FakePhone(tmp_path)
        gpt = _sol(overview, MENU)
        explorer = SlotExplorer(gpt, phone, phone, ocr_reader=phone, describe=describe, sleep=lambda s: None)
        return explorer, phone, gpt

    def test_builds_a_verified_map_with_two_sol_calls(self, tmp_path):
        explorer, phone, gpt = self._explorer(tmp_path)

        result = explorer.explore("d", "Wrap", demo=True)

        smap = result.data
        assert result.success and smap.spin.verified and smap.spin.x == 100 and smap.spin.y == 360
        assert smap.bet_menu_open.verified and smap.bet_menu_type == "carousel"
        assert smap.close_method == "tap_outside" and smap.close_point.verified
        assert smap.exit_point is not None and not smap.exit_point.verified          # Esci non si tocca
        assert (25 * 200 // 1000, 25) not in phone.taps and sum(gpt._usage_by_purpose.values()) == 2

    def test_a_declared_minimum_is_stored_as_proven(self, tmp_path):
        explorer, _, _ = self._explorer(tmp_path)

        smap = explorer.explore("d", "Wrap", demo=True, known_min_bet=Decimal("0.20")).data

        assert smap.min_bet == "0.20" and smap.min_bet_proven

    def test_a_point_that_opens_nothing_gets_one_grid_fallback(self, tmp_path):
        phone = FakePhone(tmp_path)
        wrong = dict(OVERVIEW, bet_menu_open={"x": 900, "y": 100})                 # tocco che non fa nulla
        describe = MagicMock(return_value=(60, 380))
        explorer, _, _ = self._explorer(tmp_path, phone=phone, overview=wrong, describe=describe)

        result = explorer.explore("d", "Wrap", demo=True)

        assert result.success and describe.call_count == 1 and result.data.bet_menu_open.x == 60

    def test_with_real_money_the_spin_is_not_touched_and_no_map_comes_out(self, tmp_path):
        explorer, phone, _ = self._explorer(tmp_path)

        result = explorer.explore("d", "Wrap", demo=False)

        assert not result.success and result.error_code == "EXPLORE_INCOMPLETE"
        assert (100, 360) not in phone.taps

    def test_a_spin_that_does_not_charge_is_not_saved(self, tmp_path):
        explorer, _, _ = self._explorer(tmp_path, phone=FakePhone(tmp_path, spin_charges=False))

        assert explorer.explore("d", "Wrap", demo=True).error_code == "EXPLORE_INCOMPLETE"


# ---- 3. SlotSetup: mappa -> zero GPT; niente mappa -> esplora; tap che fallisce -> ri-esplora -------------------

class TestSlotSetup:
    def _setup(self, tmp_path, explorer, **kw):
        phone = FakePhone(tmp_path)
        gpt = SimpleNamespace(_usage_by_purpose={})
        return SlotSetup(SlotMemory(str(tmp_path / "mem")), explorer, phone, gpt_client=gpt, **kw), phone

    def test_existing_map_is_used_with_zero_gpt(self, tmp_path):
        explorer = MagicMock()
        setup, _ = self._setup(tmp_path, explorer)
        setup.memory.save(_good_map())

        outcome = setup.prepare("d", "Wrap")

        assert outcome.data.status == USED and outcome.data.gpt_calls == 0 and setup.status == USED
        explorer.explore.assert_not_called()
        assert load_autopilot_element_memory("Wrap", str(tmp_path / "mem")).data["elements"]["bet_menu_close"]["x"] == 100

    def test_no_map_explores_saves_and_uses_it(self, tmp_path):
        explorer = MagicMock()
        explorer.explore.return_value = Result.ok(_good_map())
        setup, _ = self._setup(tmp_path, explorer)

        outcome = setup.prepare("d", "Wrap")

        assert outcome.data.status == EXPLORED and setup.memory.load("Wrap", SIZE).success
        explorer.explore.assert_called_once()

    def test_a_failed_exploration_does_not_block_the_run(self, tmp_path):
        explorer = MagicMock()
        explorer.explore.return_value = Result.fail("spin", "EXPLORE_INCOMPLETE")
        setup, _ = self._setup(tmp_path, explorer)

        assert setup.prepare("d", "Wrap").data.status == NOT_MAPPED

    def test_declared_min_bet_updates_an_existing_map(self, tmp_path):
        setup, _ = self._setup(tmp_path, MagicMock(), known_min_bet=Decimal("0.10"))
        setup.memory.save(_good_map())

        setup.prepare("d", "Wrap")

        assert setup.memory.load("Wrap", SIZE).data.min_bet == "0.10"

    def test_reexplore_replaces_only_the_requested_part(self, tmp_path):
        explorer = MagicMock()
        fixed = _good_map(spin=Point(150, 350, "nuovo", True))
        explorer.explore.return_value = Result.ok(fixed)
        setup, _ = self._setup(tmp_path, explorer)
        setup.memory.save(_good_map())

        assert setup.reexplore("d", "Wrap", "spin").success
        assert explorer.explore.call_args.kwargs["only"] == "spin"
        assert setup.memory.load("Wrap", SIZE).data.spin.x == 150


# ---- 4. play_session con la mappa ------------------------------------------------------------------------------

def _ok_spin(i):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal("0.20"), balance_before=Decimal("100"),
                                balance_after=Decimal("99.8"), win_amount=None, verified=True, attempts=1,
                                outcome="success"))


def _no_effect():
    return Result.ok(SpinResult(slot_name="s", spin_index=1, bet_amount=Decimal("0.20"), balance_before=None,
                                balance_after=None, win_amount=None, verified=False, attempts=1, outcome="no_effect"))


def _autopilot(spins, setup):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(BetResult(
        slot_name="s", starting_bet=Decimal("1"), final_bet=Decimal("0.2"), min_bet_verified=True, attempts=1))
    autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
    autopilot.spin.side_effect = list(spins)
    autopilot.hitl_strategy = None
    autopilot.supervisor.check_in_game.return_value = "in_game"
    autopilot.slot_setup = setup
    autopilot.gpt_client = SimpleNamespace(_usage={}, _usage_by_purpose={})
    return autopilot


class TestPlaySessionWithTheMap:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)

    def _setup(self, tmp_path, explorer):
        phone = FakePhone(tmp_path)
        return SlotSetup(SlotMemory(str(tmp_path / "mem")), explorer, phone, gpt_client=None), phone

    def test_a_mapped_slot_plays_with_zero_gpt_and_says_so(self, tmp_path, caplog):
        explorer = MagicMock()
        setup, _ = self._setup(tmp_path, explorer)
        setup.memory.save(_good_map())
        autopilot = _autopilot([_ok_spin(1), _ok_spin(2)], setup)

        with caplog.at_level(logging.INFO):
            result = session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 20)

        assert not result.failed and result.total_played == Decimal("0.40")
        explorer.explore.assert_not_called()
        assert "Slot memory: USATA" in caplog.text and "Costo GPT del gioco: $0" in caplog.text

    def test_a_new_slot_is_explored_and_reported(self, tmp_path, caplog):
        explorer = MagicMock()
        explorer.explore.return_value = Result.ok(_good_map())
        setup, _ = self._setup(tmp_path, explorer)
        autopilot = _autopilot([_ok_spin(1), _ok_spin(2)], setup)

        with caplog.at_level(logging.INFO):
            session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 20)

        explorer.explore.assert_called_once()
        assert "Slot memory: ESPLORATA" in caplog.text

    def test_a_saved_spin_that_fails_three_times_triggers_one_reexploration_before_the_operator(self, tmp_path):
        explorer = MagicMock()
        explorer.explore.return_value = Result.ok(_good_map())
        setup, _ = self._setup(tmp_path, explorer)
        setup.memory.save(_good_map())
        autopilot = _autopilot([_no_effect()] * 3 + [_ok_spin(4), _ok_spin(5)], setup)

        result = session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 20)

        assert not result.failed and result.total_played == Decimal("0.40")
        assert explorer.explore.call_count == 1 and explorer.explore.call_args.kwargs["only"] == "spin"

    def test_without_a_setup_nothing_changes(self):
        autopilot = _autopilot([_ok_spin(1), _ok_spin(2)], None)

        assert not session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 20).failed


# ---- 5. navigazione: punto diretto, griglia solo come ripiego ---------------------------------------------------

class TestDirectNavigation:
    def _gpt(self, answer):
        gpt = MagicMock()
        gpt._create_completion.return_value = answer
        return gpt

    def _shot(self, tmp_path):
        path = str(tmp_path / "n.png")
        Image.new("RGB", (1000, 2000)).save(path)
        return path

    def test_a_reliable_direct_point_skips_the_grid(self, tmp_path):
        grid, stats = MagicMock(), {}
        refine = build_direct_refiner(self._gpt(Result.ok({"x": 500, "y": 250})), grid, stats)

        assert refine("il pulsante Prova", self._shot(tmp_path), 1000, 2000) == (500, 500)
        grid.assert_not_called()
        assert stats == {"direct": 1, "grid": 0}

    def test_the_same_description_twice_means_the_point_failed_so_the_grid_is_used(self, tmp_path):
        grid = MagicMock(return_value=(10, 20))
        refine = build_direct_refiner(self._gpt(Result.ok({"x": 500, "y": 250})), grid, {})
        shot = self._shot(tmp_path)

        refine("il pulsante Prova", shot, 1000, 2000)
        assert refine("il pulsante Prova", shot, 1000, 2000) == (10, 20)

    def test_no_answer_or_out_of_range_falls_back_to_the_grid(self, tmp_path):
        grid = MagicMock(return_value=(1, 2))
        shot = self._shot(tmp_path)
        for answer in (Result.fail("x", "E"), Result.ok({"found": False}), Result.ok({"x": 5000, "y": 1})):
            assert build_direct_refiner(self._gpt(answer), grid, {})("d", shot, 1000, 2000) == (1, 2)
