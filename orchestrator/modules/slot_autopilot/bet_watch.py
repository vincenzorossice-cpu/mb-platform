"""Sorveglianza della cella di puntata selezionata: un tocco accidentale su un'altra cella cambia la puntata senza che
nessuno lo veda (l'OCR legge i valori delle celle ma non quale e' evidenziata). A `set_min_bet` si ricorda l'aspetto
della cella col valore della puntata verificata; a ogni spin si confronta lo stesso ritaglio sullo screenshot gia'
scattato: se cambia, l'evidenziazione si e' spostata. Nessun colore hardcoded, nessuna lettura OCR per spin."""
import logging
from decimal import Decimal
from typing import Optional

import numpy as np
from PIL import Image

from orchestrator.modules.slot_autopilot.value_parser import parse_amount

logger = logging.getLogger(__name__)

_MIN_CELLS = 3                    # una riga di scelte di puntata ha almeno tre celle
_MARGIN_FRACTION = 0.25           # margine attorno al testo: dentro ci sta lo sfondo evidenziato
_THUMB = 12                       # impronta 12x12 RGB
_CHANGED_DIFF = 25.0              # differenza media (0-255) oltre la quale la cella non e' piu' quella
CHANGED_DIFF = _CHANGED_DIFF
_ROW_TOLERANCE = 0.6              # due celle sono sulla stessa riga se i centri distano meno del 60% dell'altezza


class BetWatch:
    CHANGED_DIFF = _CHANGED_DIFF

    def __init__(self):
        self._region: Optional[tuple] = None
        self._reference: Optional[np.ndarray] = None
        self.enabled = True
        self.last_delta: Optional[float] = None       # ultima differenza misurata (0-255), per capire i falsi allarmi

    @property
    def active(self) -> bool:
        return self.enabled and self._region is not None

    def reset(self) -> None:
        self._region = self._reference = None

    def learn(self, screenshot_path: str, text_boxes: list, final_bet) -> bool:
        """Memorizza la cella col valore `final_bet` dentro una riga di >= 3 celle. False se non c'e' una riga."""
        self.reset()
        if not self.enabled or final_bet is None or not screenshot_path or not text_boxes:
            return False
        target = Decimal(str(final_bet))
        cells = []
        for box in text_boxes:
            parsed = parse_amount(getattr(box, "text", ""))
            if parsed.success:
                cells.append((box, parsed.data))
        best = None
        for box, value in cells:
            if value != target:
                continue
            row = [b for b, _ in cells if abs(_cy(b) - _cy(box)) <= _ROW_TOLERANCE * _height(box)]
            if len(row) >= _MIN_CELLS:
                best = box
                break
        if best is None:
            logger.info("bet_watch: nessuna riga di celle di puntata con %s: non sorveglio la selezione.", target)
            return False
        try:
            with Image.open(screenshot_path) as image:
                region = _padded(best.bbox, image.size)
                self._reference = _thumb(image, region)
        except OSError as exc:
            logger.warning("bet_watch: screenshot illeggibile: %s", exc)
            return False
        self._region = region
        logger.info("bet_watch: sorveglio la cella di puntata %s a %s.", target, region)
        return True

    def check(self, screenshot_path: str) -> Optional[bool]:
        """True = la cella selezionata non e' piu' quella (puntata cambiata), False = uguale, None = non verificabile."""
        if not self.active or not screenshot_path:
            return None
        try:
            with Image.open(screenshot_path) as image:
                current = _thumb(image, self._region)
        except (OSError, ValueError):
            return None
        self.last_delta = float(np.mean(np.abs(current - self._reference)))
        return self.last_delta > _CHANGED_DIFF


def _cy(box) -> float:
    return (box.bbox[1] + box.bbox[3]) / 2


def _height(box) -> float:
    return max(1.0, float(box.bbox[3] - box.bbox[1]))


def _padded(bbox: tuple, size: tuple) -> tuple:
    x1, y1, x2, y2 = bbox
    margin = int(_MARGIN_FRACTION * max(1, y2 - y1))
    return max(0, x1 - margin), max(0, y1 - margin), min(size[0], x2 + margin), min(size[1], y2 + margin)


def _thumb(image: Image.Image, region: tuple) -> np.ndarray:
    crop = image.convert("RGB").crop(region).resize((_THUMB, _THUMB), Image.BOX)
    return np.asarray(crop, dtype=np.float32)
