"""Popup che bloccano la slot dopo una vincita, e in particolare quello del RADDOPPIO (gamble): propone di raddoppiare la
vincita con "Raddoppia" e di incassarla con "Raccogli". Se resta aperto la slot si ferma e un tocco sbagliato (il
centro, una carta, "Raddoppia") puo' perdere la vincita o cambiare la puntata. Qui si tocca SOLO il pulsante di incasso.

Un popup di raddoppio e' riconosciuto solo se compaiono insieme una parola di raddoppio e un pulsante di incasso:
"Scommetti" o "Incassa" da soli esistono anche nell'interfaccia normale. Il pulsante di incasso non e' mai un testo che
contiene una parola di raddoppio, ne' sta vicino a "Raddoppia"."""
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.slot_intro import same_screen, signature
from orchestrator.modules.slot_autopilot.slot_warden import is_win_popup

logger = logging.getLogger(__name__)

GAMBLE_WORDS = ("raddoppia la vincita", "raddoppia", "gamble", "double", "double up", "scommetti")
STRONG_GAMBLE_WORDS = ("raddoppia la vincita", "raddoppia", "gamble", "double", "double up")   # "scommetti" e' anche un'etichetta normale
COLLECT_WORDS = ("raccogli", "collect", "take win", "prendi", "incassa")
MAX_ATTEMPTS = 3
MAX_VISION_CALLS = 3
DOUBLE_SAFE_DISTANCE_PX = 80        # il pulsante di incasso sta almeno cosi' lontano da "Raddoppia"
SETTLE_SECONDS = 1.5
_CHANGED = 0.02

_SYSTEM = (
    "You are looking at a slot machine game on an Android phone right after a win. Decide whether a GAMBLE / DOUBLE-UP "
    "popup is covering the game (it offers to double the win, with a button to gamble and a button to COLLECT / take the "
    "win). Answer ONLY with JSON: "
    '{"gamble_popup": bool, "collect": {"x": int, "y": int} | null, "double": {"x": int, "y": int} | null}. '
    "Coordinates are NORMALIZED to a 0-1000 grid (x from the left, y from the top), the CENTER of each button; the "
    "buttons may be icons without text. collect = the button that keeps the win and closes the popup. double = the "
    "button that gambles/doubles. NEVER put the double button as collect. Use null when you cannot tell."
)


@dataclass
class PopupStatus:
    kind: Optional[str] = None          # "gamble" | "win" | None
    collect: Optional[tuple] = None     # dove toccare (gamble)
    source: str = ""                    # ocr | memory | vision
    path: Optional[str] = None
    size: Optional[tuple] = None
    double_points: Optional[list] = None


@dataclass
class PopupOutcome:
    closed: bool = False
    kind: Optional[str] = None
    collect: Optional[tuple] = None
    source: str = ""


def _has(text: str, words: tuple) -> bool:
    return any(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", text, re.IGNORECASE) for w in words)


class PopupGuard:
    def __init__(self, screen_reader, touch_injector, ocr_reader=None, ask: Optional[Callable] = None,
                 saved: Optional[Callable] = None, save: Optional[Callable] = None,
                 sleep: Callable[[float], None] = time.sleep, max_vision_calls: int = MAX_VISION_CALLS):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.ocr_reader = ocr_reader
        self.ask = ask
        self.saved = saved                  # saved(slot) -> (coords, firma) | None
        self.save = save                    # save(slot, coords, firma)
        self.sleep = sleep
        self.max_vision_calls = max_vision_calls
        self.vision_calls = 0
        self.closed_count = 0

    # ---- lettura --------------------------------------------------------------------------------------------------
    def _shot(self, device_id: str):
        result = self.screen_reader._screenshot(device_id)
        if not result.success:
            return None
        try:
            with Image.open(result.data) as image:
                return result.data, image.size
        except OSError:
            return None

    def _boxes(self, path: str) -> list:
        reader = getattr(self.ocr_reader, "read_text_boxes", None)
        if not callable(reader):
            return []
        try:
            result = reader(path)
            return list(result.data or []) if getattr(result, "success", False) else []
        except Exception as exc:
            logger.warning("popup: OCR dei riquadri fallito: %s", exc)
            return []

    @staticmethod
    def _center(box) -> tuple:
        x1, y1, x2, y2 = box.bbox
        return (x1 + x2) // 2, (y1 + y2) // 2

    def inspect(self, device_id: str, slot_name: str, suspect: bool = False) -> PopupStatus:
        """Che cosa c'e' sullo schermo: popup di raddoppio, popup di vincita senza bottoni, o niente."""
        shot = self._shot(device_id)
        if shot is None:
            return PopupStatus()
        path, size = shot
        boxes = self._boxes(path)
        texts = [str(getattr(b, "text", "")) for b in boxes]
        double_boxes = [b for b in boxes if _has(str(getattr(b, "text", "")), GAMBLE_WORDS)]
        double_points = [self._center(b) for b in double_boxes]
        collect_boxes = [b for b in boxes if _has(str(getattr(b, "text", "")), COLLECT_WORDS)
                         and not _has(str(getattr(b, "text", "")), GAMBLE_WORDS)]
        if double_boxes and collect_boxes:
            safe = [self._center(b) for b in collect_boxes
                    if all(math.hypot(self._center(b)[0] - d[0], self._center(b)[1] - d[1]) >= DOUBLE_SAFE_DISTANCE_PX
                           for d in double_points)]
            return PopupStatus("gamble", safe[0] if safe else None, "ocr", path, size, double_points)
        strong = [b for b in double_boxes if _has(str(getattr(b, "text", "")), STRONG_GAMBLE_WORDS)]
        if strong and not collect_boxes:
            found = self._vision(path, size, slot_name)           # le parole ci sono, il pulsante e' un'icona
            if found is not None:
                return PopupStatus("gamble", found, "vision", path, size, double_points)
            return PopupStatus("gamble", None, "ocr", path, size, double_points)
        if is_win_popup(" ".join(texts)):
            return PopupStatus("win", None, "ocr", path, size)
        if suspect:
            remembered = self._remembered(slot_name, path)
            if remembered is not None:
                return PopupStatus("gamble", remembered, "memory", path, size)
            found = self._vision(path, size, slot_name)
            if found is not None:
                return PopupStatus("gamble", found, "vision", path, size)
        return PopupStatus(None, None, "", path, size)

    def _remembered(self, slot_name: str, path: str) -> Optional[tuple]:
        if not callable(self.saved):
            return None
        try:
            saved = self.saved(slot_name)
        except Exception:
            return None
        if not saved or not saved[1]:
            return None
        sig = signature(path)
        return tuple(saved[0]) if sig and same_screen(saved[1], sig) else None

    def _vision(self, path: str, size: tuple, slot_name: str) -> Optional[tuple]:
        """Sol indica Collect e Double su un popup a sole icone. Valido solo se Collect esiste, sta nello schermo e
        non coincide con Double."""
        if self.ask is None or self.vision_calls >= self.max_vision_calls:
            return None
        self.vision_calls += 1
        try:
            answer = self.ask(path, _SYSTEM, "Is there a gamble/double-up popup? Where is the COLLECT button?")
        except Exception as exc:
            logger.warning("popup: domanda a Sol fallita: %s", exc)
            return None
        if not isinstance(answer, dict) or answer.get("gamble_popup") is not True:
            return None
        points = {}
        for name in ("collect", "double"):
            raw = answer.get(name)
            try:
                nx, ny = float(raw["x"]), float(raw["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= nx <= 1000 and 0 <= ny <= 1000:
                points[name] = (round(nx / 1000 * size[0]), round(ny / 1000 * size[1]))
        collect = points.get("collect")
        if collect is None:
            return None
        double = points.get("double")
        if double is not None and math.hypot(collect[0] - double[0], collect[1] - double[1]) < DOUBLE_SAFE_DISTANCE_PX:
            logger.warning("popup: Sol indica Collect vicino a Double: non tocco.")
            return None
        return collect

    # ---- chiusura ---------------------------------------------------------------------------------------------------
    def clear(self, device_id: str, slot_name: str, suspect: bool = False) -> Result[PopupOutcome]:
        """Chiude il popup (se c'e'): raddoppio -> SOLO il pulsante di incasso; vincita senza bottoni -> tocco al
        centro. Ritorna `closed=False` se non c'era niente; fail se c'e' ma non si riesce a chiuderlo senza rischi."""
        outcome = PopupOutcome()
        popup_signature = None
        last_changed = True
        for _ in range(MAX_ATTEMPTS):
            status = self.inspect(device_id, slot_name, suspect and not outcome.closed)
            if status.kind is None:
                if outcome.closed and not last_changed:
                    return Result.fail("il tocco non ha cambiato lo schermo: popup ancora aperto", "POPUP_NOT_CLOSED")
                if outcome.closed and outcome.kind == "gamble" and outcome.collect and popup_signature \
                        and outcome.source != "memory" and callable(self.save):
                    try:
                        self.save(slot_name, list(outcome.collect), popup_signature)
                    except Exception as exc:
                        logger.warning("popup: regola non salvata: %s", exc)
                return Result.ok(outcome)
            if status.kind == "gamble":
                if status.collect is None:
                    logger.warning("Vigile: popup di raddoppio senza un pulsante di incasso identificabile: non tocco.")
                    return Result.fail("popup di raddoppio senza Raccogli identificabile", "GAMBLE_NO_COLLECT")
                logger.info("Vigile: popup raddoppio vincita rilevato, raccolgo vincita (%d,%d, da %s)",
                            status.collect[0], status.collect[1], status.source)
                popup_signature = popup_signature or signature(status.path)
                target, kind = status.collect, "gamble"
            else:
                logger.info("Vigile: popup vincita rilevato, tap al centro per chiudere")
                target, kind = (status.size[0] // 2, status.size[1] // 2), "win"
            self.touch_injector.tap_coordinates(device_id, int(target[0]), int(target[1]))
            self.sleep(SETTLE_SECONDS)
            after = self._shot(device_id)
            diff = diff_score(status.path, after[0]) if after is not None else None
            last_changed = bool(diff is not None and diff.success and diff.data >= _CHANGED)
            outcome = PopupOutcome(True, kind, tuple(target), status.source)
            self.closed_count += 1
        final = self.inspect(device_id, slot_name, False)
        if final.kind is None:
            return Result.ok(outcome)
        return Result.fail("il popup non si chiude", "POPUP_NOT_CLOSED")
