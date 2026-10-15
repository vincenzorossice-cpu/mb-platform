"""Turbo (quick spin): un controllo che accorcia il ciclo di spin. "Assente" e' un esito normale. Si decide dal
TEMPO: il turbo conta solo se il tempo medio per spin scende in modo evidente. La misura non ha un ciclo proprio: la
sessione fa i suoi spin normali prima e dopo l'attivazione e passa qui le medie."""
import logging
import time
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.autospin_manager import (
    SETTLE_SECONDS, SPIN_ZONE_PX, ScreenTools, _near,
)
from orchestrator.modules.slot_autopilot.slot_map import TurboInfo

logger = logging.getLogger(__name__)

TURBO_WORDS = ("TURBO", "QUICK", "QUICKSPIN", "QUICK SPIN", "VELOCE", "VELOCITA", "FAST", "SPEED", "RAPIDO")
MENU_WORDS = ("MENU", "IMPOSTAZIONI", "SETTINGS", "OPZIONI", "OPTIONS")
MAX_ATTEMPTS = 2                    # esplorazioni a vuoto per slot prima di non insistere

_SYSTEM = (
    "You are looking for the TURBO / QUICK SPIN / FAST PLAY control of a slot game on an Android phone (a button or "
    "toggle that makes the spins faster), and for the menu/settings button. You see ONE screenshot. Answer ONLY with "
    'JSON: {"turbo": {"x": int, "y": int, "description": str} | null, "menu": {"x": int, "y": int, '
    '"description": str} | null}. Coordinates are NORMALIZED to a 0-1000 grid (x from the left, y from the top), the '
    "CENTER of the control. NEVER indicate the main SPIN button. Use null when you cannot see it."
)


class TurboManager:
    def __init__(self, screen_reader, touch_injector, ocr_reader=None, ask: Optional[Callable] = None,
                 press_back: Optional[Callable[[], object]] = None, sleep: Callable[[float], None] = time.sleep,
                 min_gain: float = 0.2):
        self.tools = ScreenTools(screen_reader, touch_injector, ocr_reader, ask, sleep)
        self.press_back = press_back
        self.sleep = sleep
        self.min_gain = min_gain

    # ---- ricerca e attivazione --------------------------------------------------------------------------------------
    def explore_turbo(self, device_id: str, spin_point: Optional[tuple], previous: Optional[TurboInfo] = None
                      ) -> Result[TurboInfo]:
        """Cerca il turbo (principale, poi menu) e lo ATTIVA. Ritorna `present` non verificato con la sequenza di
        tocchi; la verifica e' `conclude` con i tempi misurati dalla sessione. Nessun candidato: `absent`."""
        attempts = previous.attempts if previous is not None else 0
        first = self.tools.shot(device_id)
        if first is None:
            return Result.fail("screenshot fallito", "TURBO_SCREENSHOT_FAILED")
        for level in ("main", "menu"):
            sequence, shot = [], first
            if level == "menu":
                menu = self._menu_point(first, spin_point)
                if menu is None:
                    continue
                changed, _, shot = self.tools.tap_changes(device_id, menu[0], menu[1])
                if not changed or shot is None:
                    continue
                sequence.append({"x": menu[0], "y": menu[1], "wait": SETTLE_SECONDS, "note": "apre menu"})
            for point in self._candidates(shot, spin_point):
                changed, _, after = self.tools.tap_changes(device_id, point[0], point[1])
                if not changed:
                    continue                                 # nessun effetto visibile: non e' un interruttore
                sequence.append({"x": point[0], "y": point[1], "wait": SETTLE_SECONDS, "note": "turbo"})
                logger.info("Turbo: candidato attivato (%s), misuro il tempo dei prossimi spin", level)
                return Result.ok(TurboInfo(status="present", location=level, button_coords=list(point),
                                           type="toggle", verified=False, touch_sequence=sequence, attempts=attempts))
            if level == "menu":                              # niente nel menu: lo si richiude
                self.tools.tap(device_id, sequence[0]["x"], sequence[0]["y"])
                self.sleep(SETTLE_SECONDS)
                if self.press_back is not None:
                    self.press_back()
        logger.info("Turbo: non trovato su questa slot")
        return Result.ok(TurboInfo(status="absent", attempts=attempts))

    def _menu_point(self, shot, spin_point) -> Optional[tuple]:
        for x, y, _ in self.tools.words(shot, MENU_WORDS):
            return x, y
        point = self.tools.sol_points(shot, _SYSTEM, "Find the menu / settings button.").get("menu")
        return point if point is not None and not (spin_point and _near(point, spin_point, SPIN_ZONE_PX)) else None

    def _candidates(self, shot, spin_point) -> list:
        found = [(x, y) for x, y, _ in self.tools.words(shot, TURBO_WORDS)]
        sol = self.tools.sol_points(shot, _SYSTEM, "Find the turbo / quick spin control.")
        if "turbo" in sol:
            found.append(sol["turbo"])
        unique = []
        for point in found:
            if (spin_point and _near(point, spin_point, SPIN_ZONE_PX)) or any(_near(point, o, 30) for o in unique):
                continue
            unique.append(point)
        return unique[:3]

    def enable_turbo(self, device_id: str, info: TurboInfo) -> bool:
        """Riesegue la sequenza salvata (a inizio sessione, slot appena caricata: il turbo parte spento)."""
        if info.status != "present" or not info.verified or not info.touch_sequence:
            return False
        for step in info.touch_sequence:
            self.tools.tap(device_id, step["x"], step["y"])
            self.sleep(float(step.get("wait", SETTLE_SECONDS)))
        return True

    def disable_turbo(self, device_id: str, info: TurboInfo) -> None:
        """Annulla un turbo attivato in esplorazione (secondo tocco sull'interruttore; poi chiude il menu)."""
        for step in reversed(info.touch_sequence or []):
            self.tools.tap(device_id, step["x"], step["y"])
            self.sleep(float(step.get("wait", SETTLE_SECONDS)))

    # ---- misura ----------------------------------------------------------------------------------------------------
    @staticmethod
    def measure_spin_time(seconds: list) -> Optional[float]:
        """Media dei secondi per spin (almeno 2 campioni positivi), None se non misurabile."""
        valid = [float(s) for s in seconds if isinstance(s, (int, float)) and s > 0]
        return sum(valid) / len(valid) if len(valid) >= 2 else None

    def conclude(self, device_id: str, info: TurboInfo, without: Optional[float], with_turbo: Optional[float]
                 ) -> TurboInfo:
        """Turbo confermato solo se il tempo scende almeno di `min_gain`; altrimenti si disattiva e si salva `absent`."""
        if without and with_turbo and with_turbo <= without * (1 - self.min_gain):
            info.verified, info.seconds_without, info.seconds_with = True, round(without, 2), round(with_turbo, 2)
            logger.info("Turbo: confermato (%.1f s -> %.1f s per spin)", without, with_turbo)
            return info
        logger.info("Turbo: il tempo non scende in modo evidente (%s -> %s): lo disattivo, salvo 'assente'",
                    without, with_turbo)
        self.disable_turbo(device_id, info)
        return TurboInfo(status="absent", attempts=info.attempts, seconds_without=without, seconds_with=with_turbo)
