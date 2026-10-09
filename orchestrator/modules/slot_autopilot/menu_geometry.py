"""Geometria del pannello del menu puntata: dove sta, e un punto di chiusura che cade SEMPRE fuori dal pannello.

Il pannello e' un box scuro (spesso in basso, sopra la barra di saldo/puntata). Un tocco "di chiusura" dentro il
pannello seleziona una cella: e' l'errore che ha portato la puntata da 0.20 a 0.40. Qui si trovano i bounds dallo
screenshot a menu aperto e si calcola un punto fuori; ogni punto di chiusura va controllato con `is_inside`."""
from typing import Optional

import numpy as np
from PIL import Image

_DARK_LUMA = 60              # pannello: quasi nero; lo sfondo sfocato del gioco e' piu' chiaro e colorato
_ROW_DARK_FRACTION = 0.60    # una riga e' "di pannello" se almeno il 60% dei pixel (fascia 8-92%) e' scuro
_GAP_TOLERANCE = 0.004       # righe non scure tollerate dentro il pannello (frazione dell'altezza)
BROWSER_BAR_FRACTION = 0.20  # sopra: barra del browser e di sistema (un tocco li' colpirebbe l'URL)
_ABOVE_MARGIN = 0.08         # distanza dal bordo alto del pannello
_BELOW_MARGIN = 0.06
_MIN_PANEL_FRACTION = 0.10   # senza ancora: un pannello con celle e' piu' alto di una barra di stato/saldo
_SEARCH_FROM, _SEARCH_TO = 0.35, 0.92   # senza ancora: dove puo' stare il pannello (frazione dell'altezza)
FALLBACK_Y_FRACTION = 0.30


def _dark_rows(screenshot_path: str) -> Optional[tuple]:
    try:
        with Image.open(screenshot_path) as image:
            gray = np.asarray(image.convert("L"))
    except OSError:
        return None
    height, width = gray.shape
    band = gray[:, int(width * 0.08):int(width * 0.92)]
    return (band < _DARK_LUMA).mean(axis=1) >= _ROW_DARK_FRACTION, height


def panel_bounds(screenshot_path: str, anchor_y: Optional[int] = None) -> Optional[tuple]:
    """(alto, basso) in pixel del pannello scuro. Con `anchor_y` (la riga delle celle lette dall'OCR) si espande da li';
    senza, si prende la striscia scura piu' alta nella parte bassa dello schermo. None se non si trova."""
    rows = _dark_rows(screenshot_path)
    if rows is None:
        return None
    dark, height = rows
    gap = max(2, int(height * _GAP_TOLERANCE))

    def expand(start: int) -> tuple:
        top = bottom = start
        misses = 0
        for y in range(start, -1, -1):
            if dark[y]:
                top, misses = y, 0
            else:
                misses += 1
                if misses > gap:
                    break
        misses = 0
        for y in range(start, height):
            if dark[y]:
                bottom, misses = y, 0
            else:
                misses += 1
                if misses > gap:
                    break
        return top, bottom

    if anchor_y is not None:
        start = min(max(int(anchor_y), 0), height - 1)
        if not dark[start]:
            near = [y for y in range(max(0, start - gap), min(height, start + gap + 1)) if dark[y]]
            if not near:
                return None
            start = near[0]
        return expand(start)

    best, y = None, int(height * _SEARCH_FROM)
    limit = int(height * _SEARCH_TO)
    while y < limit:
        if dark[y]:
            top, bottom = expand(y)
            tall_enough = bottom - top >= int(height * _MIN_PANEL_FRACTION)
            if tall_enough and top >= int(height * _SEARCH_FROM) - gap and (best is None or bottom - top > best[1] - best[0]):
                best = (top, bottom)
            y = bottom + 1
        else:
            y += 1
    return best


def outside_point(bounds: Optional[tuple], size: tuple) -> tuple:
    """Punto di chiusura fuori dal pannello: al centro, sopra il bordo alto (mai sopra la barra del browser); se non
    c'e' spazio sopra, sotto il bordo basso. Senza bounds: il 30% dell'altezza."""
    width, height = size
    if bounds is not None:
        top, bottom = bounds
        above = top - int(height * _ABOVE_MARGIN)
        if above >= int(height * BROWSER_BAR_FRACTION):
            return width // 2, above
        below = bottom + int(height * _BELOW_MARGIN)
        if below < int(height * 0.92):
            return width // 2, below
    return width // 2, int(height * FALLBACK_Y_FRACTION)


def is_inside(point: tuple, bounds: Optional[tuple], margin: int = 12) -> bool:
    """True se il punto cade nel pannello (il pannello occupa tutta la larghezza)."""
    if bounds is None:
        return False
    return bounds[0] - margin <= int(point[1]) <= bounds[1] + margin


def bounds_from_fractions(panel: tuple, height: int) -> tuple:
    return int(panel[0] * height), int(panel[1] * height)
