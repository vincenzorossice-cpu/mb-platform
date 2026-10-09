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
# Parametri (luminosita' minima, differenza massima tra i canali) provati in ordine all'apprendimento: la freccia di
# un pulsante traslucido puo' non essere bianca pura, quindi si sceglie quello che separa meglio pronto e occupato.
_MASK_PARAMS = ((200, 60), (180, 80), (160, 100), (140, 120), (120, 255), (100, 255))
_CELL_MIN_FRACTION = 0.25  # una cella 32x32 e' "accesa" se almeno il 25% dei suoi pixel e' chiaro
_MIN_ARROW_CELLS = 15      # sotto, il pulsante "pronto" non ha una freccia riconoscibile
_MAX_BUSY_SIMILARITY = 0.6 # pronto/occupato troppo simili: non distinguibili
_MAX_LEARNING_ATTEMPTS = 3
# Altri indizi quando la freccia non sparisce durante l'animazione (colore, luminosita', disegno del pulsante).
_FEAT_GRID = 4
_COLOR_MIN_DISTANCE = 28.0     # distanza CROMATICA media (colore senza luminosita') tra pulsante pronto e occupato
_LUMINANCE_MIN_DIFF = 22.0     # differenza di luminanza (quando il colore e' quasi uguale)
_PATCH_MIN_DIFF = 18.0         # differenza media della griglia 4x4 in grigi (spinner, overlay sopra il pulsante)
_FEATURE_MARGIN = 0.5          # "pronto" solo entro mezza separazione dal riferimento pronto e oltre dall'occupato


def button_mask(
    screenshot_path: str, x: int, y: int, half: int = HALF_PX, luma: int = _BRIGHT_LUMA, spread_max: int = _WHITE_SPREAD,
) -> Optional[np.ndarray]:
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
    brightness = pixels.mean(axis=2)
    spread = pixels.max(axis=2) - pixels.min(axis=2)
    bright = ((brightness >= luma) & (spread <= spread_max)).astype(np.uint8) * 255
    small = np.asarray(Image.fromarray(bright).resize((_GRID, _GRID), Image.BOX)) / 255.0
    return small >= _CELL_MIN_FRACTION


def button_features(screenshot_path: str, x: int, y: int, half: int = HALF_PX) -> Optional[dict]:
    """Colore medio, luminanza media e griglia 4x4 in grigi del pulsante (zona centrale del ritaglio)."""
    try:
        with Image.open(screenshot_path) as image:
            box = (max(0, int(x) - half), max(0, int(y) - half),
                   min(image.width, int(x) + half), min(image.height, int(y) + half))
            if box[2] <= box[0] or box[3] <= box[1]:
                return None
            crop = image.convert("RGB").crop(box)
    except OSError:
        return None
    arr = np.asarray(crop).astype(np.float64)
    h, w = arr.shape[:2]
    center = arr[int(h * 0.2):max(int(h * 0.8), int(h * 0.2) + 1), int(w * 0.2):max(int(w * 0.8), int(w * 0.2) + 1)]
    rgb = center.mean(axis=(0, 1))
    luma = float(0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2])
    grid = np.asarray(crop.convert("L").resize((_FEAT_GRID, _FEAT_GRID), Image.BOX)).astype(np.float64).flatten()
    chroma = [float(v - luma) for v in rgb]                  # il colore senza la luminosita'
    return {"rgb": [float(v) for v in rgb], "luma": luma, "chroma": chroma, "grid": [float(v) for v in grid]}


def feature_distance(method: str, a: dict, b: dict) -> float:
    if method == "color":      # solo la parte cromatica: una variazione di sola luminosita' e' un altro metodo
        return float(np.linalg.norm(np.array(a["chroma"]) - np.array(b["chroma"])))
    if method == "luminance":
        return abs(float(a["luma"]) - float(b["luma"]))
    return float(np.mean(np.abs(np.array(a["grid"]) - np.array(b["grid"]))))      # patch


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
    luma: int = _BRIGHT_LUMA
    spread_max: int = _WHITE_SPREAD
    method: str = "arrow"        # arrow | color | luminance | patch
    idle_feat: Optional[dict] = None
    busy_feat: Optional[dict] = None
    sep: float = 0.0


class SpinButtonState:
    """Riferimento appreso per slot (file `slot_memory/<slot>.spin_button.json`) e classificazione di uno
    screenshot: True = pronto (freccia visibile), False = occupato, None = non utilizzabile/incerto."""

    def __init__(self, memory_dir: str = "slot_memory", debug_dir: str = "logs"):
        self.memory_dir = memory_dir
        self.debug_dir = debug_dir
        self._cache = {}
        # SPIN_BUTTON_RELEARN=1: il riferimento salvato di ogni slot viene cancellato la prima volta che serve.
        self._relearn = os.environ.get("SPIN_BUTTON_RELEARN", "").strip() in ("1", "true", "yes")
        self._relearn_done = set()
        self.last_status = {}      # slot -> frase sull'esito dell'ultimo apprendimento (per la console)

    def _maybe_reset(self, slot_name: str) -> None:
        if self._relearn and slot_name not in self._relearn_done:
            self._relearn_done.add(slot_name)
            try:
                os.remove(self._path(slot_name))
                logger.warning("pulsante spin: SPIN_BUTTON_RELEARN: riferimento di '%s' cancellato, si riapprende.", slot_name)
            except OSError:
                pass
            self._cache = {k: v for k, v in self._cache.items() if k[0] != slot_name}

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
        self._maybe_reset(slot_name)
        key = (slot_name, tuple(screen_size))
        if key in self._cache:
            return self._cache[key]
        reference = None
        data = self._load_raw(slot_name)
        try:
            if data.get("usable") and tuple(data.get("screen", ())) == tuple(screen_size) \
                    and data.get("method", "arrow") != "arrow":
                empty = np.zeros((_GRID, _GRID), dtype=bool)
                reference = SpinButtonReference(
                    idle=empty, busy=empty, screen=tuple(screen_size), threshold=0.0, busy_similarity=0.0,
                    method=str(data["method"]), idle_feat=data["idle_feat"], busy_feat=data["busy_feat"],
                    sep=float(data["sep"]),
                )
            elif data.get("usable") and tuple(data.get("screen", ())) == tuple(screen_size):
                reference = SpinButtonReference(
                    idle=_from_rows(data["idle"]), busy=_from_rows(data["busy"]), screen=tuple(screen_size),
                    threshold=float(data["threshold"]), busy_similarity=float(data["busy_similarity"]),
                    luma=int(data.get("luma", _BRIGHT_LUMA)), spread_max=int(data.get("spread_max", _WHITE_SPREAD)),
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
        if reference.method != "arrow":
            features = button_features(screenshot_path, x, y)
            if features is None:
                return None
            to_idle = feature_distance(reference.method, features, reference.idle_feat)
            to_busy = feature_distance(reference.method, features, reference.busy_feat)
            half = reference.sep * _FEATURE_MARGIN
            state = True if (to_idle < half and to_busy > half) else False if (to_busy < half and to_idle > half) else None
            logger.info("pulsante spin (%s): distanza da 'pronto' %.1f, da 'occupato' %.1f (separazione %.1f) -> %s",
                        reference.method, to_idle, to_busy, reference.sep,
                        "PRONTO" if state else "occupato" if state is False else "incerto")
            return state
        mask = button_mask(screenshot_path, x, y, luma=reference.luma, spread_max=reference.spread_max)
        if mask is None:
            logger.warning("pulsante spin: ritaglio illeggibile (%s): classificazione impossibile.", screenshot_path)
            return None
        to_idle = similarity(reference.idle, mask)
        to_busy = similarity(reference.busy, mask)
        ready = to_idle >= reference.threshold and to_idle > to_busy
        logger.info("pulsante spin: somiglianza con 'pronto' %.2f, con 'occupato' %.2f (soglia %.2f) -> %s",
                    to_idle, to_busy, reference.threshold, "PRONTO" if ready else "occupato")
        return ready

    def learn(self, slot_name: str, screen_size: tuple, x: int, y: int, idle_path: str, busy_path: str) -> str:
        """Apprende dai due screenshot. Ritorna 'learned', 'retry' (campioni non distinguibili: si riprova al
        prossimo spin) o 'unusable' (tentativi esauriti: per questa slot si usa il confronto tra screenshot).
        Nessuna uscita silenziosa: ogni esito scrive una riga e aggiorna `last_status`."""
        data = self._load_raw(slot_name)
        attempts = int(data.get("attempts", 0)) + 1 if tuple(data.get("screen", ())) == tuple(screen_size) else 1
        best = None                       # (somiglianza pronto/occupato, luma, spread, idle, busy)
        for luma, spread_max in _MASK_PARAMS:
            idle = button_mask(idle_path, x, y, luma=luma, spread_max=spread_max)
            busy = button_mask(busy_path, x, y, luma=luma, spread_max=spread_max)
            if idle is None or busy is None:
                self.last_status[slot_name] = "NON appreso: ritaglio del pulsante illeggibile (screenshot mancante o punto fuori schermo)"
                logger.warning("pulsante spin: apprendimento %d: %s (idle=%s, busy=%s, punto %d,%d)", attempts,
                               self.last_status[slot_name], idle_path, busy_path, x, y)
                return "retry"
            if int(idle.sum()) < _MIN_ARROW_CELLS:
                continue                  # con questi parametri la freccia del "pronto" non si vede
            sim = similarity(idle, busy)
            if best is None or sim < best[0]:
                best = (sim, luma, spread_max, idle, busy)
            if sim <= _MAX_BUSY_SIMILARITY:
                break                     # separazione sufficiente: parametri scelti
        separable = best is not None and best[0] <= _MAX_BUSY_SIMILARITY
        alternative = None if separable else self._alternative(idle_path, busy_path, x, y)
        usable = separable or alternative is not None
        gave_up = (not usable) and attempts >= _MAX_LEARNING_ATTEMPTS
        record = {"screen": list(screen_size), "center": [int(x), int(y)], "attempts": attempts,
                  "usable": bool(usable), "gave_up": gave_up}
        if alternative is not None:
            method, idle_feat, busy_feat, sep = alternative
            record.update({"method": method, "idle_feat": idle_feat, "busy_feat": busy_feat, "sep": sep})
            self.last_status[slot_name] = f"appreso con il metodo '{method}' (separazione {sep:.1f}): la freccia non sparisce"
            self._write(slot_name, record)
            self._cache.pop((slot_name, tuple(screen_size)), None)
            self._dump_debug(idle_path, busy_path, x, y)
            logger.info("pulsante spin: apprendimento %d: %s", attempts, self.last_status[slot_name])
            return "learned"
        if separable:
            sim, luma, spread_max, idle, busy = best
            record.update({"idle": _to_rows(idle), "busy": _to_rows(busy), "busy_similarity": sim,
                           "threshold": (1.0 + sim) / 2.0, "luma": luma, "spread_max": spread_max})
            self.last_status[slot_name] = (f"appreso (soglia di somiglianza {record['threshold']:.2f}, "
                                           f"luminosita' {luma}, {int(idle.sum())} celle nella freccia)")
        elif best is None:
            self.last_status[slot_name] = "NON appreso: nel pulsante 'pronto' non si riconosce nessuna freccia chiara"
        else:
            self.last_status[slot_name] = (f"NON appreso: pronto e occupato troppo simili ({best[0]:.2f}): la freccia non "
                                           f"sparisce durante l'animazione o non si distingue")
        self._write(slot_name, record)
        self._cache.pop((slot_name, tuple(screen_size)), None)
        self._dump_debug(idle_path, busy_path, x, y)
        level = logging.INFO if separable else logging.WARNING
        logger.log(level, "pulsante spin: apprendimento %d: %s%s", attempts, self.last_status[slot_name],
                   " - rinuncio per questa slot" if gave_up else ("" if separable else " - riprovo al prossimo spin"))
        if separable:
            return "learned"
        return "unusable" if gave_up else "retry"

    def _alternative(self, idle_path: str, busy_path: str, x: int, y: int) -> Optional[tuple]:
        """La freccia non separa pronto e occupato: si prova il colore, poi la luminanza, poi il disegno del pulsante.
        Ritorna (metodo, caratteristiche pronto, caratteristiche occupato, separazione) o None."""
        idle, busy = button_features(idle_path, x, y), button_features(busy_path, x, y)
        if idle is None or busy is None:
            return None
        color = feature_distance("color", idle, busy)
        if color >= _COLOR_MIN_DISTANCE:
            return "color", idle, busy, color
        luminance = feature_distance("luminance", idle, busy)
        if luminance >= _LUMINANCE_MIN_DIFF:
            return "luminance", idle, busy, luminance
        patch = feature_distance("patch", idle, busy)
        if patch >= _PATCH_MIN_DIFF:
            return "patch", idle, busy, patch
        return None

    def method(self, slot_name: str) -> Optional[str]:
        """Come si capisce che lo spin e' finito per questa slot: 'arrow' | 'color' | 'luminance' | 'patch', oppure
        'frame_zone' (nessun indizio sul pulsante: confronto della zona dei rulli); None se non si sa ancora."""
        data = self._load_raw(slot_name)
        if data.get("usable"):
            return str(data.get("method", "arrow"))
        return "frame_zone" if data.get("gave_up") else None

    def mark_unusable(self, slot_name: str, screen_size: tuple) -> None:
        """La mappa dice che su questa slot il pulsante non si distingue: niente tentativi di apprendimento (a meno che
        non ci sia gia' un riferimento utilizzabile)."""
        if self.reference(slot_name, screen_size) is not None:
            return
        self._write(slot_name, {"screen": list(screen_size), "attempts": _MAX_LEARNING_ATTEMPTS, "usable": False,
                                "gave_up": True})
        self._cache.pop((slot_name, tuple(screen_size)), None)

    def status(self, slot_name: str) -> Optional[str]:
        return self.last_status.get(slot_name)

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
