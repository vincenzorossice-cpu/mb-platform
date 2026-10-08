"""Schermate di ingresso di una slot (overlay nero "schermo intero", "Continua", benvenuto) superate con il minimo
di GPT: l'overlay nero si chiude toccando ovunque (nessuna chiamata), le schermate gia' viste per quella slot si
ripetono dalla memoria (nessuna chiamata), una schermata nuova costa UNA chiamata e viene ricordata."""
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import _slugify

logger = logging.getLogger(__name__)

_SIG_SIZE = 24
_SAME_SCREEN_DIFF = 14.0          # differenza media (0-255) sotto cui due schermate sono "la stessa"
_DARK_LUMA = 45
_DARK_FRACTION = 0.80             # overlay nero: almeno l'80% dei pixel scuri
_MAX_ROUNDS = 12
_MAX_SECONDS = 90.0
_SETTLE_SECONDS = 1.5
_CONTINUE_DESCRIPTION = (
    "the button or text to continue or start the game (for example 'Continua', 'Continue', 'Start', 'Play', "
    "'Tap to continue'); NEVER the spin button and never a bet control"
)


@dataclass
class IntroOutcome:
    rounds: int
    taps: int
    ready: bool
    learned: int = 0       # schermate nuove ricordate in questo giro


def signature(path: str) -> Optional[list]:
    """Impronta della schermata: 24x24 in scala di grigi (lista di interi)."""
    try:
        with Image.open(path) as image:
            small = image.convert("L").resize((_SIG_SIZE, _SIG_SIZE), Image.BOX)
            return [int(v) for v in np.asarray(small).flatten()]
    except OSError:
        return None


def same_screen(a: list, b: list) -> bool:
    return bool(a) and bool(b) and len(a) == len(b) and float(np.mean(np.abs(np.array(a) - np.array(b)))) < _SAME_SCREEN_DIFF


def is_dark_overlay(path: str) -> bool:
    """Schermo quasi tutto nero (overlay "schermo intero", caricamento): si chiude toccando ovunque."""
    try:
        with Image.open(path) as image:
            gray = np.asarray(image.convert("L").resize((64, 64), Image.BOX))
    except OSError:
        return False
    return float((gray < _DARK_LUMA).mean()) >= _DARK_FRACTION


class SlotIntro:
    """describe(frase, LocateContext) -> (x, y) | None (GPTLocatorStrategy.locate_described);
    spin_point(slot) -> (x, y) | None per riconoscere il gioco pronto con la freccia appresa (`button`);
    supervisor (SlotSupervisor) come ultima verifica del "gioco pronto" quando la freccia non e' ancora nota."""

    def __init__(self, screen_reader, touch_injector, describe=None, button=None, spin_point=None, supervisor=None,
                 memory_dir: str = "slot_memory", sleep: Callable[[float], None] = time.sleep,
                 max_rounds: int = _MAX_ROUNDS, max_seconds: float = _MAX_SECONDS):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.describe = describe
        self.button = button
        self.spin_point = spin_point
        self.supervisor = supervisor
        self.memory_dir = memory_dir
        self.sleep = sleep
        self.max_rounds = max_rounds
        self.max_seconds = max_seconds

    # ---- memoria -------------------------------------------------------------------------------------------
    def _path(self, slot_name: str) -> str:
        return os.path.join(self.memory_dir, f"{_slugify(slot_name)}.intro.json")

    def _load(self, slot_name: str, size: tuple) -> list:
        try:
            with open(self._path(slot_name), encoding="utf-8") as memory_file:
                data = json.load(memory_file)
            if tuple(data.get("screen", ())) == tuple(size):
                return [step for step in data.get("steps", []) if {"sig", "x", "y"} <= set(step)]
        except (OSError, ValueError, AttributeError):
            pass
        return []

    def _save(self, slot_name: str, size: tuple, steps: list) -> None:
        try:
            os.makedirs(self.memory_dir, exist_ok=True)
            with open(self._path(slot_name), "w", encoding="utf-8") as memory_file:
                json.dump({"screen": list(size), "steps": steps}, memory_file)
        except OSError as exc:
            logger.warning("intro: impossibile salvare le schermate di ingresso: %s", exc)

    # ---- ciclo ---------------------------------------------------------------------------------------------
    def run(self, device_id: str, slot_name: str) -> Result[IntroOutcome]:
        started = time.monotonic()
        taps = learned = 0
        stuck = 0
        previous_sig = None
        for round_no in range(1, self.max_rounds + 1):
            if time.monotonic() - started > self.max_seconds:
                break
            shot = self.screen_reader._screenshot(device_id)
            if not shot.success:
                return Result.fail(f"screenshot fallito: {shot.error}", shot.error_code or "SCREENSHOT_FAILED")
            path = shot.data
            try:
                with Image.open(path) as image:
                    size = image.size
            except OSError:
                return Result.fail("screenshot illeggibile", "SCREENSHOT_UNREADABLE")
            sig = signature(path)

            ready = self._game_ready(device_id, slot_name, path, size, sig)
            if ready:
                logger.info("intro: gioco pronto dopo %d giri, %d tocchi (%d schermate nuove ricordate).",
                            round_no, taps, learned)
                return Result.ok(IntroOutcome(rounds=round_no, taps=taps, ready=True, learned=learned))

            stuck = stuck + 1 if (previous_sig is not None and same_screen(sig, previous_sig)) else 0
            previous_sig = sig
            if stuck >= 3:
                return Result.fail("la schermata non cambia dopo piu' tocchi", "INTRO_STUCK")

            if is_dark_overlay(path):
                x, y = size[0] // 2, size[1] // 2
                logger.info("intro: overlay nero, tocco al centro (%d,%d) senza GPT.", x, y)
                self.touch_injector.tap_coordinates(device_id, x, y)
                taps += 1
                self.sleep(_SETTLE_SECONDS)
                continue

            steps = self._load(slot_name, size)
            known = next((step for step in steps if same_screen(step["sig"], sig)), None) if sig else None
            if known is not None:
                logger.info("intro: schermata gia' vista per '%s', tocco ricordato (%d,%d) senza GPT.",
                            slot_name, known["x"], known["y"])
                self.touch_injector.tap_coordinates(device_id, int(known["x"]), int(known["y"]))
                taps += 1
                self.sleep(_SETTLE_SECONDS)
                continue

            point = self._ask_continue(device_id, slot_name, path, size)
            if point is None and ready is None:
                logger.info("intro: nessun pulsante da toccare e nessun modo di verificare: considero il gioco pronto.")
                return Result.ok(IntroOutcome(rounds=round_no, taps=taps, ready=True, learned=learned))
            if point is None:
                self.sleep(_SETTLE_SECONDS * 1.5)       # forse sta ancora caricando
                continue
            self.touch_injector.tap_coordinates(device_id, int(point[0]), int(point[1]))
            taps += 1
            self.sleep(_SETTLE_SECONDS)
            after = self.screen_reader._screenshot(device_id)
            after_sig = signature(after.data) if after.success else None
            if sig and after_sig and not same_screen(sig, after_sig):
                steps.append({"sig": sig, "x": int(point[0]), "y": int(point[1])})
                self._save(slot_name, size, steps)
                learned += 1
                logger.info("intro: schermata nuova superata toccando (%d,%d): la ricordo per '%s'.",
                            point[0], point[1], slot_name)
        return Result.fail("le schermate di ingresso non finiscono", "INTRO_NOT_FINISHED")

    def _game_ready(self, device_id: str, slot_name: str, path: str, size: tuple, sig) -> Optional[bool]:
        """True/False se si sa dire se il gioco e' pronto; None se non c'e' modo di giudicare."""
        spin = self.spin_point(slot_name) if callable(self.spin_point) else None
        if self.button is not None and spin is not None and self.button.reference(slot_name, size) is not None:
            return self.button.classify(slot_name, size, path, int(spin[0]), int(spin[1])) is True
        if self.supervisor is None:
            return None
        if is_dark_overlay(path) or not self._worth_asking(slot_name, size, sig):
            return False
        return self._supervisor_says_ready(path)

    def _worth_asking(self, slot_name: str, size: tuple, sig) -> bool:
        """La chiamata a Sol serve solo per una schermata NON nota come intro (le note si ripetono dalla memoria)."""
        steps = self._load(slot_name, size)
        return not (sig and any(same_screen(step["sig"], sig) for step in steps))

    def _supervisor_says_ready(self, path: str) -> bool:
        observation = self.supervisor.observe(path, "a slot game was just opened: is it fully loaded and ready to "
                                                    "play, or is an intro/welcome/overlay still covering it?")
        if observation is None:
            return False
        return observation.screen_state in ("idle", "win_presentation") or observation.spin_ready is True

    def _ask_continue(self, device_id: str, slot_name: str, path: str, size: tuple):
        if self.describe is None:
            return None
        from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
        try:
            return self.describe(_CONTINUE_DESCRIPTION, LocateContext(
                device_id=device_id, slot_name=slot_name, screenshot_path=path,
                screen_width=size[0], screen_height=size[1],
            ))
        except Exception as exc:
            logger.warning("intro: ricerca del pulsante per continuare fallita: %s", exc)
            return None


def build_slot_intro(autopilot, memory_dir: str = "slot_memory") -> SlotIntro:
    """Collega SlotIntro a quello che l'autopilot ha gia' (nessuna nuova dipendenza)."""
    strategies = getattr(getattr(autopilot, "locator", None), "strategies", {}) or {}
    gpt = strategies.get("gpt")
    memory = strategies.get("memory")

    def spin_point(slot_name: str):
        if memory is None:
            return None
        from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
        found = memory.locate("spin", LocateContext(
            device_id="", slot_name=slot_name, screenshot_path="", screen_width=0, screen_height=0))
        return (found.data.x, found.data.y) if found.success else None

    return SlotIntro(
        autopilot.screen_reader, autopilot.touch_injector,
        describe=getattr(gpt, "locate_described", None),
        button=getattr(getattr(autopilot, "spin_controller", None), "button", None),
        spin_point=spin_point, supervisor=getattr(autopilot, "supervisor", None), memory_dir=memory_dir,
    )
