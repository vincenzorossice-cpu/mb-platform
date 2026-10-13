"""Schermate di ingresso di una slot (overlay nero "schermo intero", testo informativo, "Continua", benvenuto) superate
con il minimo di GPT. Per ogni schermata si prova una scala di strategie generiche, una per giro, verificando dallo
schermo successivo se il tocco ha funzionato: centro (per prima sull'overlay nero), pulsanti trovati per testo con
l'OCR, tasto SPIN, basso al centro e, solo alla fine, GPT. Ogni tocco che cambia lo schermo viene ricordato per quella
slot (`<slot>.intro.json`, per risoluzione) e ripetuto alla volta dopo senza strategie; un passo che non funziona piu'
viene dimenticato. Il gioco e' pronto quando la freccia dello spin appresa lo dice o l'OCR legge saldo e puntata."""
import json
import logging
import os
import re
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
_MAX_ROUNDS = 20
_MAX_SECONDS = 120.0
_IDLE_WAITS = 2                   # attese (forse sta caricando) prima di arrendersi con strategie esaurite
_MAX_BUTTON_TRIES = 2             # candidati "pulsante" provati per schermata
_SPIN_SETTLE_SECONDS = 4.0        # dopo un tocco su SPIN: il giro deve finire prima di rileggere saldo e puntata
_REMEMBERED_STEPS_ENOUGH = 3      # passi ricordati che hanno cambiato lo schermo = intro superate
_BOTTOM_FRACTION = 0.79           # ~1900 su 2400: dove stanno di solito "Continua"/"Tocca per continuare"
_SETTLE_SECONDS = 1.5
_CONTINUE_DESCRIPTION = (
    "the button or text to continue or start the game (for example 'Continua', 'Continue', 'Start', 'Play', "
    "'Tap to continue'); NEVER the spin button and never a bet control"
)


# Pulsanti generici per passare un'intro, per priorita'. Parola intera, testo breve; mai acquisto/uscita/menu.
_BUTTON_PHRASES = (
    "continua", "continue", "tocca per continuare", "tap to continue", "gioca", "play", "prova", "salta", "skip",
    "inizia", "start", "avanti", "next", "capito", "got it", "ok", "chiudi", "close",
)
_FORBIDDEN_WORDS = frozenset({
    "acquista", "compra", "buy", "gamble", "deposita", "scommetti", "bet", "esci", "exit", "menu", "reale",
    "denaro", "real", "money",
})
_MAX_BUTTON_TEXT = 30
_CLOSE_MARKS = ("x", "\u00d7", "\u2715", "\u2716")


def _button_rank(text: str) -> Optional[int]:
    """Priorita' del testo come pulsante da toccare in un'intro (0 = migliore), None se non lo e'."""
    clean = (text or "").strip().casefold()
    if not clean or len(clean) > _MAX_BUTTON_TEXT:
        return None
    words = re.findall(r"\w+", clean)
    if any(word in _FORBIDDEN_WORDS for word in words):
        return None
    for rank, phrase in enumerate(_BUTTON_PHRASES):
        parts = phrase.split()
        if any(words[i:i + len(parts)] == parts for i in range(len(words) - len(parts) + 1)):
            return rank
    if clean in _CLOSE_MARKS:                      # una "X" isolata, ultima scelta ("X2" e' un moltiplicatore)
        return len(_BUTTON_PHRASES)
    return None


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
    ocr: OCR semplice (read_slot_state / read_text_boxes), MAI il wrapper che chiama GPT;
    supervisor (SlotSupervisor) come ultima verifica del "gioco pronto" quando la freccia non e' ancora nota."""

    def __init__(self, screen_reader, touch_injector, describe=None, button=None, spin_point=None, supervisor=None,
                 memory_dir: str = "slot_memory", sleep: Callable[[float], None] = time.sleep,
                 max_rounds: int = _MAX_ROUNDS, max_seconds: float = _MAX_SECONDS, ocr=None):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.describe = describe
        self.button = button
        self.spin_point = spin_point
        self.supervisor = supervisor
        self.ocr = ocr
        self.memory_dir = memory_dir
        self.sleep = sleep
        self.max_rounds = max_rounds
        self.max_seconds = max_seconds

    # ---- memoria -------------------------------------------------------------------------------------------
    def _path(self, slot_name: str) -> str:
        return os.path.join(self.memory_dir, f"{_slugify(slot_name)}.intro.json")

    @staticmethod
    def _key(size: tuple) -> str:
        return f"{size[0]}x{size[1]}"

    def _load_all(self, slot_name: str) -> dict:
        """{"1080x2400": [passi]}; legge anche il vecchio formato {"screen": [w, h], "steps": [...]}."""
        try:
            with open(self._path(slot_name), encoding="utf-8") as memory_file:
                data = json.load(memory_file)
            screens = {}
            if isinstance(data.get("screens"), dict):
                screens = {key: steps for key, steps in data["screens"].items() if isinstance(steps, list)}
            elif data.get("screen"):
                screens = {self._key(data["screen"]): data.get("steps", [])}
            return {key: [s for s in steps if isinstance(s, dict) and {"sig", "x", "y"} <= set(s)]
                    for key, steps in screens.items()}
        except (OSError, ValueError, AttributeError, TypeError, IndexError):
            return {}

    def _load(self, slot_name: str, size: tuple) -> list:
        return self._load_all(slot_name).get(self._key(size), [])

    def _save(self, slot_name: str, size: tuple, steps: list) -> None:
        try:
            screens = self._load_all(slot_name)
            if steps:
                screens[self._key(size)] = steps
            else:
                screens.pop(self._key(size), None)
            os.makedirs(self.memory_dir, exist_ok=True)
            with open(self._path(slot_name), "w", encoding="utf-8") as memory_file:
                json.dump({"screens": screens}, memory_file)
        except OSError as exc:
            logger.warning("intro: impossibile salvare le schermate di ingresso: %s", exc)

    # ---- ciclo ---------------------------------------------------------------------------------------------
    def run(self, device_id: str, slot_name: str) -> Result[IntroOutcome]:
        started = time.monotonic()
        taps = learned = attempts = waits = memory_changes = 0
        pending = None                  # ultimo tocco, il cui esito si legge dallo schermo del giro dopo
        screen_sig = None               # schermata su cui si stanno provando le strategie
        tried: set = set()
        tried_texts: set = set()
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

            if pending is not None:
                gained, changed = self._judge_tap(pending, sig, slot_name, size)
                learned += gained
                memory_changes += 1 if changed and pending["kind"] == "memory" else 0
                pending = None
            if screen_sig is None or sig is None or not same_screen(sig, screen_sig):
                screen_sig, tried, tried_texts, waits = sig, set(), set(), 0

            dark = is_dark_overlay(path)
            ready = self._game_ready(device_id, slot_name, path, size, sig, dark)
            if ready:
                logger.info("intro: gioco pronto dopo %d giri, %d tocchi (%d schermate nuove ricordate).",
                            round_no, taps, learned)
                return Result.ok(IntroOutcome(rounds=round_no, taps=taps, ready=True, learned=learned))

            if (memory_changes >= _REMEMBERED_STEPS_ENOUGH and not dark and sig
                    and not any(same_screen(step["sig"], sig) for step in self._load(slot_name, size))):
                logger.info("intro: %d passi ricordati eseguiti, schermo cambiato %d volte - intro superate",
                            memory_changes, memory_changes)
                return Result.ok(IntroOutcome(rounds=round_no, taps=taps, ready=True, learned=learned))

            action = self._next_action(device_id, slot_name, path, size, sig, dark, ready, tried, tried_texts)
            if action is None:
                if ready is None:
                    logger.info("intro: nessun pulsante da toccare e nessun modo di verificare: considero il gioco pronto.")
                    return Result.ok(IntroOutcome(rounds=round_no, taps=taps, ready=True, learned=learned))
                if waits < _IDLE_WAITS:                 # forse sta ancora caricando
                    waits += 1
                    self.sleep(_SETTLE_SECONDS * 1.5)
                    continue
                logger.warning("intro: %d tentativi, tutte le strategie provate senza effetto: serve l'operatore.",
                               attempts)
                return Result.fail("la schermata non cambia dopo piu' tocchi", "INTRO_STUCK")

            attempts += 1
            action.update(sig=sig, number=attempts)
            self.touch_injector.tap_coordinates(device_id, int(action["x"]), int(action["y"]))
            taps += 1
            pending = action
            self.sleep(_SPIN_SETTLE_SECONDS if action["kind"] == "spin" else _SETTLE_SECONDS)
        return Result.fail("le schermate di ingresso non finiscono", "INTRO_NOT_FINISHED")

    def _judge_tap(self, tap: dict, sig, slot_name: str, size: tuple) -> tuple:
        """Logga l'esito del tocco e aggiorna la memoria. Ritorna (schermate nuove ricordate, schermo cambiato)."""
        changed = bool(sig) and bool(tap["sig"]) and not same_screen(tap["sig"], sig)
        logger.info("intro: tentativo %d - %s - schermo %s", tap["number"], tap["label"],
                    "cambiato" if changed else "non cambiato")
        steps = self._load(slot_name, size)
        same = lambda step: same_screen(step["sig"], tap["sig"])          # noqa: E731
        if tap["kind"] == "memory":
            if not changed:
                logger.info("intro: il tocco ricordato non funziona piu', lo dimentico.")
                self._save(slot_name, size, [step for step in steps if not same(step)])
            return 0, changed
        if tap["kind"] == "spin":
            return 0, changed               # uno spin ricordato = uno spin a puntata di default a ogni esecuzione
        if not changed or not tap["sig"]:
            return 0, changed
        step = {"sig": tap["sig"], "x": int(tap["x"]), "y": int(tap["y"]), "kind": tap["kind"]}
        if tap.get("text"):
            step["text"] = tap["text"]
        self._save(slot_name, size, [s for s in steps if not same(s)] + [step])
        logger.info("intro: schermata superata con '%s' (%d,%d): la ricordo per '%s'.",
                    tap["kind"], tap["x"], tap["y"], slot_name)
        return 1, changed

    # ---- strategie -----------------------------------------------------------------------------------------
    def _next_action(self, device_id, slot_name, path, size, sig, dark, ready, tried: set, tried_texts: set):
        """Prossimo tocco da provare su questa schermata, o None se le strategie sono finite."""
        steps = self._load(slot_name, size)
        known = next((step for step in steps if same_screen(step["sig"], sig)), None) if sig else None
        if known is not None and "memory" not in tried:
            tried.add("memory")
            logger.info("intro: schermata gia' vista per '%s', ripeto il tocco ricordato (%d,%d).",
                        slot_name, known["x"], known["y"])
            return {"kind": "memory", "x": known["x"], "y": known["y"],
                    "label": f"tocco ricordato ({int(known['x'])},{int(known['y'])})"}
        if ready is None:
            # Nessuno sa dire se il gioco e' pronto: niente tocchi alla cieca su una schermata che potrebbe essere gioco.
            order = ("center", "buttons", "gpt") if dark else ("buttons", "gpt")
        else:
            order = ("center", "buttons", "spin", "bottom", "gpt") if dark else ("buttons", "center", "spin", "bottom", "gpt")
        width, height = size
        for kind in order:
            if kind in tried:
                continue
            if kind == "center":
                tried.add(kind)
                x, y = width // 2, height // 2
                return {"kind": kind, "x": x, "y": y, "label": f"tap centro ({x},{y})"}
            if kind == "buttons":
                candidates = [c for c in self._button_candidates(path) if (c[0], c[1], c[2]) not in tried_texts]
                if not candidates or len(tried_texts) >= _MAX_BUTTON_TRIES:
                    tried.add(kind)
                    if not tried_texts:
                        logger.info("intro: cercato pulsanti: nessuno trovato")
                    continue
                text, x, y = candidates[0]
                tried_texts.add((text, x, y))
                return {"kind": kind, "x": x, "y": y, "text": text,
                        "label": f"cercato pulsanti: trovato '{text}' a ({x},{y})"}
            if kind == "spin":
                tried.add(kind)
                point = self._spin_point(slot_name, path)
                if point is None:
                    logger.info("intro: cercato SPIN: non trovato")
                    continue
                return {"kind": kind, "x": point[0], "y": point[1],
                        "label": f"cercato SPIN: trovato a ({point[0]},{point[1]})"}
            if kind == "bottom":
                tried.add(kind)
                x, y = width // 2, int(_BOTTOM_FRACTION * height)
                return {"kind": kind, "x": x, "y": y, "label": f"tap in basso al centro ({x},{y})"}
            if kind == "gpt":
                tried.add(kind)
                point = self._ask_continue(device_id, slot_name, path, size)
                if point is None:
                    logger.info("intro: GPT non ha trovato nessun pulsante")
                    continue
                return {"kind": kind, "x": int(point[0]), "y": int(point[1]),
                        "label": f"GPT: pulsante a ({int(point[0])},{int(point[1])})"}
        return None

    def _button_candidates(self, path: str) -> list:
        """[(testo, x, y)] dei pulsanti generici letti dall'OCR, per priorita'. Niente testi di acquisto/uscita/menu."""
        if self.ocr is None or not callable(getattr(self.ocr, "read_text_boxes", None)):
            return []
        try:
            boxes = self.ocr.read_text_boxes(path)
        except Exception as exc:
            logger.warning("intro: OCR dei pulsanti fallito: %s", exc)
            return []
        if not getattr(boxes, "success", False):
            return []
        found = []
        for box in boxes.data or []:
            rank = _button_rank(box.text)
            if rank is None:
                continue
            x1, y1, x2, y2 = box.bbox
            found.append((rank, -(y1 + y2) // 2, box.text.strip(), (x1 + x2) // 2, (y1 + y2) // 2))
        found.sort()
        return [(text, x, y) for _, _, text, x, y in found]

    def _spin_point(self, slot_name: str, path: str):
        point = self.spin_point(slot_name) if callable(self.spin_point) else None
        if point is not None:
            return int(point[0]), int(point[1])
        state = self._ocr_state(path)
        coords = state.get("spin_button_coords") if state else None
        return (int(coords[0]), int(coords[1])) if coords else None

    # ---- prontezza -----------------------------------------------------------------------------------------
    def _ocr_state(self, path: str) -> Optional[dict]:
        if self.ocr is None or not callable(getattr(self.ocr, "read_slot_state", None)):
            return None
        try:
            state = self.ocr.read_slot_state(path)
        except Exception as exc:
            logger.warning("intro: lettura OCR fallita: %s", exc)
            return None
        return state if isinstance(state, dict) else None

    def _game_ready(self, device_id: str, slot_name: str, path: str, size: tuple, sig, dark: bool) -> Optional[bool]:
        """True/False se si sa dire se il gioco e' pronto; None se non c'e' modo di giudicare."""
        spin = self.spin_point(slot_name) if callable(self.spin_point) else None
        if self.button is not None and spin is not None and self.button.reference(slot_name, size) is not None:
            return self.button.classify(slot_name, size, path, int(spin[0]), int(spin[1])) is True
        if not dark:
            state = self._ocr_state(path)
            # Basta uno dei due: su alcune slot l'OCR non legge l'etichetta della puntata (o del saldo).
            for field, name in (("saldo", "saldo"), ("posta", "puntata")):
                if state and state.get(field) is not None:
                    logger.info("intro: %s leggibile (%s) - slot attiva, esco dalle intro", name, state[field])
                    return True
        if self.supervisor is None:
            return None
        if dark or not self._worth_asking(slot_name, size, sig):
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

    ocr = getattr(autopilot, "ocr_reader", None)
    ocr = getattr(ocr, "ocr", ocr)               # l'OCR semplice: il wrapper con GPT costerebbe una chiamata a lettura
    return SlotIntro(
        autopilot.screen_reader, autopilot.touch_injector, ocr=ocr,
        describe=getattr(gpt, "locate_described", None),
        button=getattr(getattr(autopilot, "spin_controller", None), "button", None),
        spin_point=spin_point, supervisor=getattr(autopilot, "supervisor", None), memory_dir=memory_dir,
    )
