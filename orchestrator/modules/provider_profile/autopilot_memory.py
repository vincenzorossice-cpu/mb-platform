import json
import logging
import os
from datetime import datetime, timezone

from orchestrator.core.result import Result

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip().lower())
    return safe or "unknown"


def _path(slot_name: str, memory_dir: str) -> str:
    return os.path.join(memory_dir, f"{_slugify(slot_name)}.autopilot.json")


def load_autopilot_element_memory(slot_name: str, memory_dir: str = "slot_memory") -> Result[dict]:
    """Memoria generica per elementi (bet_minus, bet_plus, ecc.) che lo schema
    fisso di slot_memory/<slot>.json (gestito da test_slot_autonomous.py, mai
    modificato) non supporta. File satellite separato: nessun rischio di
    collisione/perdita di dati con quel file."""
    path = _path(slot_name, memory_dir)
    if not os.path.exists(path):
        return Result.ok({"elements": {}, "flags": {}})
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return Result.fail(f"Invalid autopilot memory JSON: {exc}", "AUTOPILOT_MEMORY_INVALID_JSON")
    data.setdefault("elements", {})
    data.setdefault("flags", {})
    return Result.ok(data)


def save_autopilot_element(
    slot_name: str, element_name: str, x: int, y: int, norm_x: float, norm_y: float,
    memory_dir: str = "slot_memory",
) -> Result[bool]:
    load_result = load_autopilot_element_memory(slot_name, memory_dir)
    if not load_result.success:
        return Result.fail(load_result.error, load_result.error_code)
    data = load_result.data
    data["elements"][element_name] = {
        "x": x, "y": y, "norm_x": norm_x, "norm_y": norm_y,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _path(slot_name, memory_dir)
    try:
        os.makedirs(memory_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        return Result.fail(f"Cannot write autopilot memory: {exc}", "AUTOPILOT_MEMORY_WRITE_ERROR")
    return Result.ok(True)


def save_autopilot_flag(
    slot_name: str, flag_name: str, value: bool, memory_dir: str = "slot_memory",
) -> Result[bool]:
    """Fatti booleani sulla slot (es. 'bet_via_menu': la puntata si cambia
    dal menu, non con un tasto meno). Stesso file satellite degli elementi."""
    load_result = load_autopilot_element_memory(slot_name, memory_dir)
    if not load_result.success:
        return Result.fail(load_result.error, load_result.error_code)
    data = load_result.data
    data["flags"][flag_name] = bool(value)
    try:
        os.makedirs(memory_dir, exist_ok=True)
        with open(_path(slot_name, memory_dir), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        return Result.fail(f"Cannot write autopilot memory: {exc}", "AUTOPILOT_MEMORY_WRITE_ERROR")
    return Result.ok(True)


def load_autopilot_flag(slot_name: str, flag_name: str, memory_dir: str = "slot_memory") -> bool:
    result = load_autopilot_element_memory(slot_name, memory_dir)
    return bool(result.success and result.data.get("flags", {}).get(flag_name, False))
