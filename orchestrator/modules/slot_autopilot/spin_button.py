"""Stato del pulsante spin (freccia a ruota visibile = il gioco e' pronto, assente = animazione in corso).

Locale, senza AI. Si apprende dal gioco stesso al primo spin: un ritaglio "pronto" (prima del tocco) e uno
"occupato" (circa 1 s dopo il tocco); la maschera dei pixel chiari (freccia e anello) dice se la freccia c'e'.
Un confronto tra screenshot di tutto lo schermo non basta: overlay di vincita e fondi traslucidi si muovono anche
quando il gioco e' pronto."""
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image

from orchestrator.modules.provider_profile.autopilot_memory import _slugify

logger = logging.getLogger(__name__)

HALF_PX = 110              # ritaglio di ~220 px attorno al pulsante
_GRID = 32                 # maschera ridotta a 32x32
_BRIGHT_LUMA = 200         # pixel "chiaro": luminosita' minima...
_WHITE_SPREAD = 60         # ...e quasi bianco (differenza massima tra i canali)
_CELL_MIN_FRACTION = 0.25  # una cella 32x32 e' "accesa" se almeno il 25% dei suoi pixel e' chiaro
_MIN_ARROW_CELLS = 15      # sotto, il pulsante "pronto" non ha una freccia riconoscibile
_MAX_BUSY_SIMILARITY = 0.6 # pronto/occupato troppo simili: non distinguibili
_MAX_LEARNING_ATTEMPTS = 3


def button_mask(screenshot_path: str, x: int, y: int, half: int = HALF_PX) -> Optional[np.ndarray]:
    """Maschera booleana 32x32 dei pixel chiari attorno al pulsante; None se l'immagine non si legge."""
    try:
        with Image.open(screenshot_path) as image:
            box = (max(0, int(x) - half), max(0, int(y) - half),
                   min(image.width, int(x) + half), min(image.height, int(y) + half))
            if box[2] <= box[0] or box[3] <= box[1]:
                return None
            pixels = np.asarray(image.convert("RGB").crop(box)).astype(np.int16)
    except OSError:
        return None
    luma = pixels.mean(axis=2)
    spread = pixels.max(axis=2) - pixels.min(axis=2)
    bright = ((luma >= _BRIGHT_LUMA) & (spread <= _WHITE_SPREAD)).astype(np.uint8) * 255
    small = np.asarray(Image.fromarray(bright).resize((_GRID, _GRID), Image.BOX)) / 255.0
    return small >= _CELL_MIN_FRACTION


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Somiglianza (intersezione su unione) di due maschere; 1.0 se entrambe vuote."""
    union = np.logical_or(a, b).sum()
    return 1.0 if union == 0 else float(np.logical_and(a, b).sum() / union)


def _to_rows(mask: np.ndarray) -> list:
    return ["".join("1" if cell else "0" for cell in row) for row in mask]


def _from_rows(rows: list) -> np.ndarray:
    return np.array([[ch == "1" for ch in row] for row in rows], dtype=bool)


@dataclass
class SpinButtonReference:
    idle: np.ndarray
    busy: np.ndarray
    screen: tuple
    threshold: float
    busy_similarity: float


class SpinButtonState:
    """Riferimento appreso per slot (file `slot_memory/<slot>.spin_button.json`) e classificazione di uno
    screenshot: True = pronto (freccia visibile), False = occupato, None = non utilizzabile/incerto."""

    def __init__(self, memory_dir: str = "slot_memory", debug_dir: str = "logs"):
        self.memory_dir = memory_dir
        self.debug_dir = debug_dir
        self._cache = {}

    def _path(self, slot_name: str) -> str:
        return os.path.join(self.memory_dir, f"{_slugify(slot_name)}.spin_button.json")

    def _load_raw(self, slot_name: str) -> dict:
        try:
            with open(self._path(slot_name), encoding="utf-8") as memory_file:
                data = json.load(memory_file)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def reference(self, slot_name: str, screen_size: tuple) -> Optional[SpinButtonReference]:
        """Il riferimento appreso, se esiste, e' utilizzabile e vale per questa dimensione dello schermo."""
        key = (slot_name, tuple(screen_size))
        if key in self._cache:
            return self._cache[key]
        reference = None
        data = self._load_raw(slot_name)
        try:
            if data.get("usable") and tuple(data.get("screen", ())) == tuple(screen_size):
                reference = SpinButtonReference(
                    idle=_from_rows(data["idle"]), busy=_from_rows(data["busy"]), screen=tuple(screen_size),
                    threshold=float(data["threshold"]), busy_similarity=float(data["busy_similarity"]),
                )
        except (KeyError, TypeError, ValueError):
            reference = None
        self._cache[key] = reference
        return reference

    def needs_learning(self, slot_name: str, screen_size: tuple) -> bool:
        """True finche' non c'e' un riferimento e non si e' esaurito il numero di tentativi."""
        if self.reference(slot_name, screen_size) is not None:
            return False
        data = self._load_raw(slot_name)
        if tuple(data.get("screen", ())) != tuple(screen_size):
            return True
        return not data.get("gave_up") and int(data.get("attempts", 0)) < _MAX_LEARNING_ATTEMPTS

    def classify(self, slot_name: str, screen_size: tuple, screenshot_path: str, x: int, y: int) -> Optional[bool]:
        reference = self.reference(slot_name, screen_size)
        if reference is None:
            return None
        mask = button_mask(screenshot_path, x, y)
        if mask is None:
            return None
        to_idle = similarity(reference.idle, mask)
        to_busy = similarity(reference.busy, mask)
        ready = to_idle >= reference.threshold and to_idle > to_busy
        logger.info("pulsante spin: somiglianza con 'pronto' %.2f, con 'occupato' %.2f (soglia %.2f) -> %s",
                    to_idle, to_busy, reference.threshold, "PRONTO" if ready else "occupato")
        return ready

    def learn(self, slot_name: str, screen_size: tuple, x: int, y: int, idle_path: str, busy_path: str) -> str:
        """Apprende dai due screenshot. Ritorna 'learned', 'retry' (campioni non distinguibili: si riprova al
        prossimo spin) o 'unusable' (tentativi esauriti: per questa slot si usa il confronto tra screenshot)."""
        idle = button_mask(idle_path, x, y)
        busy = button_mask(busy_path, x, y)
        if idle is None or busy is None:
            return "retry"
        data = self._load_raw(slot_name)
        attempts = int(data.get("attempts", 0)) + 1 if tuple(data.get("screen", ())) == tuple(screen_size) else 1
        busy_similarity = similarity(idle, busy)
        separable = int(idle.sum()) >= _MIN_ARROW_CELLS and busy_similarity <= _MAX_BUSY_SIMILARITY
        record = {"screen": list(screen_size), "center": [int(x), int(y)], "attempts": attempts,
                  "usable": bool(separable), "gave_up": (not separable) and attempts >= _MAX_LEARNING_ATTEMPTS}
        if separable:
            record.update({"idle": _to_rows(idle), "busy": _to_rows(busy), "busy_similarity": busy_similarity,
                           "threshold": (1.0 + busy_similarity) / 2.0})
        self._write(slot_name, record)
        self._cache.pop((slot_name, tuple(screen_size)), None)
        self._dump_debug(idle_path, busy_path, x, y)
        logger.info("pulsante spin: apprendimento %d: somiglianza pronto/occupato %.2f, celle della freccia %d -> %s",
                    attempts, busy_similarity, int(idle.sum()),
                    "APPRESO" if separable else ("rinuncio" if record["gave_up"] else "riprovo"))
        if separable:
            return "learned"
        return "unusable" if record["gave_up"] else "retry"

    def _write(self, slot_name: str, record: dict) -> None:
        try:
            os.makedirs(self.memory_dir, exist_ok=True)
            with open(self._path(slot_name), "w", encoding="utf-8") as memory_file:
                json.dump(record, memory_file)
        except OSError as exc:
            logger.warning("pulsante spin: impossibile salvare il riferimento: %s", exc)

    def _dump_debug(self, idle_path: str, busy_path: str, x: int, y: int) -> None:
        """I due ritagli in logs/ per controllarli a occhio (mai bloccante)."""
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            for name, path in (("pronto", idle_path), ("occupato", busy_path)):
                with Image.open(path) as image:
                    box = (max(0, int(x) - HALF_PX), max(0, int(y) - HALF_PX),
                           min(image.width, int(x) + HALF_PX), min(image.height, int(y) + HALF_PX))
                    image.convert("RGB").crop(box).save(os.path.join(self.debug_dir, f"spin_button_{name}.png"))
        except OSError as exc:
            logger.debug("ritagli del pulsante non salvati: %s", exc)
