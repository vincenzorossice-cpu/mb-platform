import json
import os
from unittest.mock import patch

from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_autopilot.strategies.memory_strategy import MemoryStrategy


def _context(**overrides):
    defaults = dict(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/shot.png",
        screen_width=1080, screen_height=2400, profile=None,
    )
    defaults.update(overrides)
    return LocateContext(**defaults)


class TestMemoryStrategy:
    def test_spin_reads_known_spin_coords(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch(
            "orchestrator.modules.slot_autopilot.strategies.memory_strategy.load_slot_memory",
            return_value={
                "known_spin_coords": (540, 2000), "learned_hints": [],
                "known_min_bet": None, "known_bet_menu_toggle": None,
            },
        ):
            strategy = MemoryStrategy(memory_dir=str(tmp_path))
            result = strategy.locate("spin", _context())
        assert result.success
        assert (result.data.x, result.data.y) == (540, 2000)
        assert result.data.confidence == 0.9

    def test_bet_menu_toggle_reads_known_bet_menu_toggle(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch(
            "orchestrator.modules.slot_autopilot.strategies.memory_strategy.load_slot_memory",
            return_value={
                "known_spin_coords": None, "learned_hints": [],
                "known_min_bet": None, "known_bet_menu_toggle": (900, 2100),
            },
        ):
            strategy = MemoryStrategy(memory_dir=str(tmp_path))
            result = strategy.locate("bet_menu_toggle", _context())
        assert result.success
        assert (result.data.x, result.data.y) == (900, 2100)

    def test_arbitrary_element_reads_autopilot_memory_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        os.makedirs(tmp_path / "slot_memory", exist_ok=True)
        data = {"elements": {"bet_minus": {"x": 200, "y": 2200, "norm_x": 0.18, "norm_y": 0.91}}}
        with open(tmp_path / "slot_memory" / "pirots5.autopilot.json", "w") as f:
            json.dump(data, f)

        strategy = MemoryStrategy(memory_dir=str(tmp_path / "slot_memory"))
        result = strategy.locate("bet_minus", _context())
        assert result.success
        assert (result.data.x, result.data.y) == (200, 2200)

    def test_missing_memory_fails(self, tmp_path):
        strategy = MemoryStrategy(memory_dir=str(tmp_path / "slot_memory"))
        result = strategy.locate("bet_minus", _context())
        assert not result.success
        assert result.error_code == "MEMORY_ELEMENT_UNKNOWN"


class TestAutopilotFlags:
    def test_flag_defaults_to_false_and_roundtrips(self, tmp_path):
        from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_flag, save_autopilot_flag
        assert load_autopilot_flag("slot_x", "bet_via_menu", str(tmp_path)) is False
        save_autopilot_flag("slot_x", "bet_via_menu", True, str(tmp_path))
        assert load_autopilot_flag("slot_x", "bet_via_menu", str(tmp_path)) is True

    def test_flags_and_elements_do_not_overwrite_each_other(self, tmp_path):
        from orchestrator.modules.provider_profile.autopilot_memory import (
            load_autopilot_element_memory, save_autopilot_element, save_autopilot_flag,
        )
        save_autopilot_element("slot_x", "bet_minus", 1, 2, 0.1, 0.2, str(tmp_path))
        save_autopilot_flag("slot_x", "bet_via_menu", True, str(tmp_path))
        save_autopilot_element("slot_x", "spin", 3, 4, 0.3, 0.4, str(tmp_path))

        data = load_autopilot_element_memory("slot_x", str(tmp_path)).data
        assert set(data["elements"]) == {"bet_minus", "spin"}
        assert data["flags"] == {"bet_via_menu": True}
