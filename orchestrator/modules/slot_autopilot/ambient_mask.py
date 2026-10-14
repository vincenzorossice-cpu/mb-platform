"""Animazioni ambientali (contatori jackpot, banner, luci): cambiano da sole anche a slot ferma e impediscono al
confronto dei frame di dire "fermo". A slot ferma si scattano pochi screenshot e si segnano le celle di una griglia che
cambiano da sole; da lì il confronto di calma le ignora. Salvata per slot e risoluzione."""
import json
import logging
import os
from typing import Optional

import numpy as np
from PIL import Image, ImageChops, ImageDraw

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import _slugify

logger = logging.getLogger(__name__)

GRID = (12, 24)                   # colonne x righe
_CELL_CHANGED = 0.4               # differenza media (0-255) in una cella oltre la quale "cambia da sola"
_MAX_COVERAGE = 0.60              # una maschera che copre piu' del 60% dello schermo non serve a nulla


class AmbientMask:
    def __init__(self, size: tuple, cells: set):
        self.size = tuple(size)
        self.cells = set(cells)

    def covers(self, size: tuple) -> bool:
        return tuple(size) == self.size

    def masked_diff(self, path_a: str, path_b: str, region: Optional[tuple] = None) -> Result[float]:
        """Differenza 0..1 come `frame_diff.diff_score`, ma con le celle ambientali azzerate e normalizzata sui pixel
        che contano."""
        try:
            with Image.open(path_a) as a, Image.open(path_b) as b:
                ga, gb = a.convert("L"), b.convert("L")
        except OSError as exc:
            return Result.fail(f"Cannot read image: {exc}", "IMAGE_READ_ERROR")
        if ga.size != gb.size:
            return Result.fail(f"Image size mismatch: {ga.size} vs {gb.size}", "IMAGE_SIZE_MISMATCH")
        diff = ImageChops.difference(ga, gb)
        valid = Image.new("L", diff.size, 255)
        if self.covers(diff.size):
            draw_diff, draw_valid = ImageDraw.Draw(diff), ImageDraw.Draw(valid)
            cols, rows = GRID
            cell_w, cell_h = diff.size[0] / cols, diff.size[1] / rows
            for col, row in self.cells:
                box = (int(col * cell_w), int(row * cell_h), int((col + 1) * cell_w), int((row + 1) * cell_h))
                draw_diff.rectangle(box, fill=0)
                draw_valid.rectangle(box, fill=0)
        if region is not None:
            diff, valid = diff.crop(region), valid.crop(region)
        counted = valid.histogram()[255]
        if counted == 0:
            return Result.ok(0.0)
        total = sum(value * count for value, count in enumerate(diff.histogram()))
        return Result.ok(total / counted / 255.0)


def learn_mask(paths: list) -> Optional[AmbientMask]:
    """Maschera dalle coppie consecutive di screenshot a slot ferma. None se non c'e' nulla da mascherare o se la
    maschera sarebbe inutilizzabile."""
    if len(paths) < 2:
        return None
    cols, rows = GRID
    changed = set()
    size = None
    try:
        previous = None
        for path in paths:
            with Image.open(path) as image:
                gray = image.convert("L")
            size = size or gray.size
            if gray.size != size:
                return None
            if previous is not None:
                cells = np.asarray(ImageChops.difference(previous, gray).resize((cols, rows), Image.BOX), dtype=float)
                for row, col in zip(*np.nonzero(cells > _CELL_CHANGED)):
                    changed.add((int(col), int(row)))
            previous = gray
    except OSError:
        return None
    if not changed:
        return None
    grown = {(c + dc, r + dr) for c, r in changed for dc in (-1, 0, 1) for dr in (-1, 0, 1)
             if 0 <= c + dc < cols and 0 <= r + dr < rows}
    if len(grown) / (cols * rows) > _MAX_COVERAGE:
        logger.warning("ambiente: la maschera coprirebbe %d%% dello schermo: la scarto.",
                       int(100 * len(grown) / (cols * rows)))
        return None
    return AmbientMask(size, grown)


def _path(memory_dir: str, slot_name: str) -> str:
    return os.path.join(memory_dir, f"{_slugify(slot_name)}.ambient.json")


def _key(size: tuple) -> str:
    return f"{size[0]}x{size[1]}"


def load_mask(memory_dir: str, slot_name: str, size: tuple) -> Optional[AmbientMask]:
    try:
        with open(_path(memory_dir, slot_name), encoding="utf-8") as handle:
            entry = json.load(handle).get(_key(size))
        if entry is None:
            return None
        return AmbientMask(size, {(int(c), int(r)) for c, r in entry["cells"]})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def save_mask(memory_dir: str, slot_name: str, mask: AmbientMask) -> None:
    try:
        os.makedirs(memory_dir, exist_ok=True)
        try:
            with open(_path(memory_dir, slot_name), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        data[_key(mask.size)] = {"cells": sorted([c, r] for c, r in mask.cells)}
        with open(_path(memory_dir, slot_name), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError as exc:
        logger.warning("ambiente: maschera non salvata: %s", exc)
