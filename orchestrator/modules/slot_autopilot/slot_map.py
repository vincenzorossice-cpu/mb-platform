"""Mappa verificata di una slot ("learn once, remember forever").

Un file per slot, `slot_memory/<slug>.slot_map.json`, con una mappa per dimensione di schermo (due telefoni diversi non
si sporcano a vicenda). La mappa e' l'unica fonte che l'esploratore (Sol) scrive e che il passo di preparazione legge:
`apply_to_memory` la travasa nella memoria che il motore gia' consulta (elementi, flag, minimo), cosi' le run
successive usano le coordinate salvate senza chiamare GPT. Le mappe sono locali a ogni PC (non vanno nel repo)."""
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import (
    _slugify, save_autopilot_element, save_autopilot_flag, save_autopilot_value,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BET_MENU_TYPES = ("carousel", "list", "dropdown", "plus_minus", "other")
CLOSE_METHODS = ("tap_outside", "x_button", "toggle", "other")


@dataclass
class Point:
    x: int
    y: int
    description: str = ""
    verified: bool = False

    @staticmethod
    def parse(raw) -> Optional["Point"]:
        try:
            return Point(int(raw["x"]), int(raw["y"]), str(raw.get("description", "")), bool(raw.get("verified", False)))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class SlotMap:
    slot_name: str
    screen: tuple                                   # (larghezza, altezza) in pixel
    spin: Optional[Point] = None
    bet_menu_open: Optional[Point] = None           # dove toccare per aprire il menu puntata
    bet_menu_type: str = "other"                    # carousel | list | dropdown | plus_minus | other
    bet_minus: Optional[Point] = None               # solo plus_minus
    bet_plus: Optional[Point] = None
    close_method: str = "other"                     # tap_outside | x_button | toggle | other
    close_point: Optional[Point] = None
    min_bet: Optional[str] = None                   # Decimal come stringa
    min_bet_proven: bool = False
    exit_point: Optional[Point] = None
    explored_at: str = ""
    method: str = "sol"                             # sol | operator
    spin_pending: bool = False                      # soldi veri: punto di Sol non ancora provato, lo conferma il 1o spin

    def key(self) -> str:
        return f"{self.screen[0]}x{self.screen[1]}"

    def is_usable(self) -> bool:
        """Minimo per giocare: il pulsante spin verificato (o in attesa di conferma col primo spin vero, soldi veri)."""
        if self.spin is None or not (self.spin.verified or self.spin_pending):
            return False
        if self.bet_menu_type == "plus_minus":
            return self.bet_minus is not None
        return True

    def summary(self) -> str:
        """Una riga leggibile di cosa sa la mappa (per la console)."""
        spin = "mancante" if self.spin is None else (
            f"({self.spin.x},{self.spin.y}) " + ("verificato" if self.spin.verified else "da confermare col primo spin"))
        menu = "nessuno (puntata con +/-)" if self.bet_menu_type == "plus_minus" else (
            "non mappato" if self.bet_menu_open is None else
            f"{self.bet_menu_type} ({self.bet_menu_open.x},{self.bet_menu_open.y})")
        close = "?" if self.close_point is None and self.close_method == "other" else (
            self.close_method + ("" if self.close_point is None else f" ({self.close_point.x},{self.close_point.y})"))
        minimum = "?" if self.min_bet is None else f"{self.min_bet}{' dimostrato' if self.min_bet_proven else ''}"
        return f"spin {spin}; menu puntata {menu}; chiusura {close}; minimo {minimum}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["screen"] = list(self.screen)
        return data

    @staticmethod
    def from_dict(data: dict) -> Optional["SlotMap"]:
        try:
            screen = tuple(int(v) for v in data["screen"])
            if len(screen) != 2 or screen[0] <= 0 or screen[1] <= 0:
                return None
            bet_type = data.get("bet_menu_type", "other")
            close_method = data.get("close_method", "other")
            return SlotMap(
                slot_name=str(data.get("slot_name", "")), screen=screen,
                spin=Point.parse(data["spin"]) if data.get("spin") else None,
                bet_menu_open=Point.parse(data["bet_menu_open"]) if data.get("bet_menu_open") else None,
                bet_menu_type=bet_type if bet_type in BET_MENU_TYPES else "other",
                bet_minus=Point.parse(data["bet_minus"]) if data.get("bet_minus") else None,
                bet_plus=Point.parse(data["bet_plus"]) if data.get("bet_plus") else None,
                close_method=close_method if close_method in CLOSE_METHODS else "other",
                close_point=Point.parse(data["close_point"]) if data.get("close_point") else None,
                min_bet=str(data["min_bet"]) if data.get("min_bet") is not None else None,
                min_bet_proven=bool(data.get("min_bet_proven", False)),
                exit_point=Point.parse(data["exit_point"]) if data.get("exit_point") else None,
                explored_at=str(data.get("explored_at", "")), method=str(data.get("method", "sol")),
                spin_pending=bool(data.get("spin_pending", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SlotMemory:
    """Carica/salva le mappe in `slot_memory/<slug>.slot_map.json` (una per dimensione schermo)."""

    def __init__(self, memory_dir: str = "slot_memory"):
        self.memory_dir = memory_dir

    def path(self, slot_name: str) -> str:
        return os.path.join(self.memory_dir, f"{_slugify(slot_name)}.slot_map.json")

    def _read_file(self, slot_name: str) -> Result[dict]:
        path = self.path(slot_name)
        if not os.path.exists(path):
            return Result.ok({"version": SCHEMA_VERSION, "maps": {}})
        try:
            with open(path, encoding="utf-8") as map_file:
                data = json.load(map_file)
        except (OSError, ValueError) as exc:
            return Result.fail(f"mappa illeggibile: {exc}", "SLOT_MAP_INVALID")
        if not isinstance(data, dict) or not isinstance(data.get("maps"), dict):
            return Result.fail("formato della mappa non valido", "SLOT_MAP_INVALID")
        return Result.ok(data)

    def load(self, slot_name: str, screen: tuple) -> Result[SlotMap]:
        """La mappa per QUESTA dimensione di schermo; SLOT_MAP_MISSING se non c'e' o non e' utilizzabile."""
        file_result = self._read_file(slot_name)
        if not file_result.success:
            return Result.fail(file_result.error, "SLOT_MAP_MISSING")
        raw = file_result.data["maps"].get(f"{int(screen[0])}x{int(screen[1])}")
        slot_map = SlotMap.from_dict(raw) if isinstance(raw, dict) else None
        if slot_map is None or tuple(slot_map.screen) != (int(screen[0]), int(screen[1])):
            return Result.fail("nessuna mappa per questo schermo", "SLOT_MAP_MISSING")
        if not slot_map.is_usable():
            return Result.fail("mappa incompleta: spin non verificato", "SLOT_MAP_MISSING")
        return Result.ok(slot_map)

    def save(self, slot_map: SlotMap) -> Result[bool]:
        """Scrittura atomica (file temporaneo + rename): una interruzione non lascia una mappa a meta'."""
        if not slot_map.screen or not slot_map.slot_name:
            return Result.fail("mappa senza nome o schermo", "SLOT_MAP_INVALID")
        existing = self._read_file(slot_map.slot_name)
        data = existing.data if existing.success else {"version": SCHEMA_VERSION, "maps": {}}
        data["version"] = SCHEMA_VERSION
        data["maps"][slot_map.key()] = slot_map.to_dict()
        try:
            os.makedirs(self.memory_dir, exist_ok=True)
            descriptor, temp_path = tempfile.mkstemp(dir=self.memory_dir, suffix=".tmp")
            with os.fdopen(descriptor, "w", encoding="utf-8") as temp_file:
                json.dump(data, temp_file, indent=2)
            os.replace(temp_path, self.path(slot_map.slot_name))
        except OSError as exc:
            return Result.fail(f"mappa non salvata: {exc}", "SLOT_MAP_WRITE_ERROR")
        return Result.ok(True)

    def forget(self, slot_name: str, screen: tuple) -> None:
        file_result = self._read_file(slot_name)
        if not file_result.success:
            return
        file_result.data["maps"].pop(f"{int(screen[0])}x{int(screen[1])}", None)
        try:
            with open(self.path(slot_name), "w", encoding="utf-8") as map_file:
                json.dump(file_result.data, map_file, indent=2)
        except OSError:
            pass

    # ---- travaso nella memoria che il motore gia' consulta ----------------------------------------------------
    def apply_to_memory(self, slot_map: SlotMap) -> Result[int]:
        """Semina `<slot>.autopilot.json` (+ memoria legacy di spin e icona del menu) con le coordinate della mappa:
        da qui il motore le usa senza GPT. La mappa e' l'autorita': sovrascrive anche un ricordo sporco.
        Ritorna il numero di elementi seminati."""
        from orchestrator.tests.validation.test_slot_autonomous import _persist_slot_memory

        slot, (width, height) = slot_map.slot_name, slot_map.screen
        seeded = 0

        def element(name: str, point: Optional[Point]) -> None:
            nonlocal seeded
            if point is None:
                return
            if name == "spin":
                _persist_slot_memory(slot, known_spin_coords=(point.x, point.y))
            elif name == "bet_menu_toggle":
                _persist_slot_memory(slot, known_bet_menu_toggle=(point.x, point.y))
            else:
                save_autopilot_element(slot, name, point.x, point.y, point.x / width, point.y / height, self.memory_dir)
            seeded += 1

        element("spin", slot_map.spin)
        element("bet_menu_toggle", slot_map.bet_menu_open)
        element("bet_menu_close", slot_map.close_point if slot_map.close_method != "toggle" else None)
        element("bet_minus", slot_map.bet_minus)
        element("bet_plus", slot_map.bet_plus)
        via_menu = slot_map.bet_menu_type != "plus_minus" and slot_map.bet_menu_open is not None
        save_autopilot_flag(slot, "bet_via_menu", via_menu, self.memory_dir)
        # Una lista tutta visibile (o un dropdown) non si scorre: la sonda di scorrimento non serve.
        save_autopilot_flag(slot, "bet_row_static", slot_map.bet_menu_type in ("list", "dropdown"), self.memory_dir)
        if slot_map.min_bet is not None:
            save_autopilot_value(slot, "min_bet", slot_map.min_bet, self.memory_dir)
            if slot_map.min_bet_proven:
                save_autopilot_value(slot, "min_bet_trusted", slot_map.min_bet, self.memory_dir)
        return Result.ok(seeded)

    # ---- reset con copia di sicurezza -------------------------------------------------------------------------
    _SLOT_FILE_SUFFIXES = (".slot_map.json", ".autopilot.json", ".json", ".spin_button.json", ".intro.json")

    def reset(self, slot_name: str) -> Result[list]:
        """Sposta (non cancella) tutti i file di memoria di QUESTA slot in `<memory_dir>/_backup/<data-ora>/`:
        la slot riparte da zero e si puo' rimettere tutto a mano. Le altre slot non si toccano."""
        slug = _slugify(slot_name)
        backup = os.path.join(self.memory_dir, "_backup", time.strftime("%Y%m%d_%H%M%S"))
        moved, seen = [], set()
        for directory in (self.memory_dir, "slot_memory"):
            for suffix in self._SLOT_FILE_SUFFIXES:
                path = os.path.join(directory, slug + suffix)
                key = os.path.abspath(path)
                if key in seen or not os.path.isfile(path):
                    continue
                seen.add(key)
                try:
                    os.makedirs(backup, exist_ok=True)
                    target = os.path.join(backup, os.path.basename(path))
                    shutil.move(path, target)
                    moved.append(target)
                except OSError as exc:
                    return Result.fail(f"memoria non spostata ({path}): {exc}", "SLOT_MEMORY_RESET_FAILED")
        return Result.ok(moved)
