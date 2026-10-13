"""Schermate di ingresso di una slot (overlay nero "schermo intero", testo informativo, "Continua", benvenuto) superate
con il minimo di GPT. Per ogni schermata si prova una scala di strategie generiche, una per giro, verificando dallo
schermo successivo se il tocco ha funzionato: centro (per prima sull'overlay nero), pulsanti trovati per testo con
l'OCR, attesa passiva (un banner spesso sparisce da solo), swipe, basso al centro e, solo alla fine, GPT. Mai il tasto
SPIN: lancerebbe uno spin a puntata di default. Ogni tocco che cambia lo schermo viene ricordato per quella
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
_MAX_ROUNDS = 30
_MAX_SECONDS = 240.0
_LOADING_MAX_TEXTS = 5            # schermata di caricamento: al massimo questi testi, nessun pulsante riconosciuto
_LOADING_WAITS = 5                # attese di _LOADING_SECONDS prima di passare alle strategie normali (15 s)
_LOADING_SECONDS = 3.0
_BANNER_Y_FRACTION = 0.70         # un banner sta in basso: serve almeno un testo sotto il 70% dell'altezza
_SWIPE_SECONDS = 2.0              # attesa dopo uno swipe (e `wait_after` dei passi salvati)
_SWIPE_DURATION_MS = 300
# Swipe per chiudere un banner, come % dello schermo: (nome, (x1, y1, x2, y2)). Le coordinate sono quelle richieste.
_SWIPES = (
    ("swipe_right", "destra", (0.8, 0.85, 0.2, 0.85)),
    ("swipe_left", "sinistra", (0.2, 0.85, 0.8, 0.85)),
    ("swipe_up", "su", (0.5, 0.9, 0.5, 0.5)),
)
_SWIPE_KINDS = tuple(name for name, _, _ in _SWIPES)
_IDLE_WAITS = 2                   # attese (forse sta caricando) prima di arrendersi con strategie esaurite
_MAX_BUTTON_TRIES = 2             # candidati "pulsante" provati per schermata
_PASSIVE_SECONDS = 30.0           # attesa passiva: non si tocca nulla e si rilegge lo schermo ogni _PASSIVE_INTERVAL
_PASSIVE_INTERVAL = 5.0
_PASSIVE_TOTAL_SECONDS = 90.0     # tetto dell'attesa passiva in una esecuzione, poi tocca all'operatore
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
        self._boxes_cache = None
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
            # I passi nati da un tocco su SPIN (vecchie memorie) non si ripetono mai: lancerebbero uno spin.
            return {key: [s for s in steps if isinstance(s, dict) and {"sig", "x", "y"} <= set(s)
                          and s.get("kind") != "spin"] for key, steps in screens.items()}
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
        taps = learned = attempts = waits = memory_changes = loading_waits = 0
        passive_left = _PASSIVE_TOTAL_SECONDS
        last_swipe = None               # swipe appena eseguito: l'esito si vede dalla prontezza del giro dopo
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
            self._boxes_cache = None            # il file dello screenshot puo' essere lo stesso di prima

            if pending is not None:
                gained, changed = self._judge_tap(pending, sig, slot_name, size)
                learned += gained
                memory_changes += 1 if changed and pending["kind"] == "memory" else 0
                last_swipe = pending if pending["kind"] in _SWIPE_KINDS else None
                pending = None
            if screen_sig is None or sig is None or not same_screen(sig, screen_sig):
                screen_sig, tried, tried_texts, waits = sig, set(), set(), 0

            dark = is_dark_overlay(path)
            ready = self._game_ready(device_id, slot_name, path, size, sig, dark)
            if last_swipe is not None:
                name = last_swipe["label"]
                if ready:
                    logger.info("intro: %s per banner → saldo leggibile", name)
                    learned += self._remember_swipe(last_swipe, slot_name, size)
                else:
                    logger.info("intro: %s per banner → nessun cambiamento", name)
                last_swipe = None
            if ready:
                logger.info("intro: gioco pronto dopo %d giri, %d tocchi (%d schermate nuove ricordate).",
                            round_no, taps, learned)
                return Result.ok(IntroOutcome(rounds=round_no, taps=taps, ready=True, learned=learned))

            if (memory_changes >= _REMEMBERED_STEPS_ENOUGH and not dark and sig
                    and not any(same_screen(step["sig"], sig) for step in self._load(slot_name, size))):
                logger.info("intro: %d passi ricordati eseguiti, schermo cambiato %d volte - intro superate",
                            memory_changes, memory_changes)
                return Result.ok(IntroOutcome(rounds=round_no, taps=taps, ready=True, learned=learned))

            if loading_waits < _LOADING_WAITS and self._is_loading(path, slot_name, size, sig, dark, tried):
                loading_waits += 1
                logger.info("intro: schermata di caricamento rilevata (%d elementi), attendo... (%d/%d)",
                            len(self._boxes(path) or []), loading_waits, _LOADING_WAITS)
                self.sleep(_LOADING_SECONDS)
                continue

            action = self._next_action(device_id, slot_name, path, size, sig, dark, ready, tried, tried_texts,
                                       passive_left)
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

            if action.get("passive"):
                seconds = min(float(action["seconds"]), passive_left)
                logger.info("intro: attesa passiva (%ds), il banner potrebbe sparire da solo", int(seconds))
                appeared, waited = self._passive_wait(device_id, slot_name, sig, seconds)
                passive_left -= waited
                if appeared:
                    logger.info("intro: banner sparito dopo %ds - saldo leggibile, esco dalle intro", int(waited))
                    if action["kind"] != "memory":
                        learned += self._remember_wait(sig, waited, slot_name, size)
                    return Result.ok(IntroOutcome(rounds=round_no, taps=taps, ready=True, learned=learned))
                logger.info("intro: attesa passiva finita, nulla e' cambiato")
                if action["kind"] == "memory":
                    logger.info("intro: l'attesa ricordata non basta piu', la dimentico.")
                    self._save(slot_name, size, [st for st in self._load(slot_name, size)
                                                 if not same_screen(st["sig"], sig)])
                continue

            attempts += 1
            action.update(sig=sig, number=attempts)
            loading_waits = 0
            if action.get("coords"):
                self._swipe(device_id, action["coords"])
            else:
                self.touch_injector.tap_coordinates(device_id, int(action["x"]), int(action["y"]))
            taps += 1
            pending = action
            self.sleep(action.get("wait") or _SETTLE_SECONDS)
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
        if tap["kind"] in _SWIPE_KINDS:
            return 0, changed               # uno swipe si ricorda solo se poi il gioco e' pronto
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
    def _next_action(self, device_id, slot_name, path, size, sig, dark, ready, tried: set, tried_texts: set,
                     passive_left: float = _PASSIVE_TOTAL_SECONDS):
        """Prossimo tocco da provare su questa schermata, o None se le strategie sono finite."""
        steps = self._load(slot_name, size)
        known = next((step for step in steps if same_screen(step["sig"], sig)), None) if sig else None
        if known is not None and "memory" not in tried:
            tried.add("memory")
            logger.info("intro: schermata gia' vista per '%s', ripeto il tocco ricordato (%d,%d).",
                        slot_name, known["x"], known["y"])
            if known.get("kind") == "passive_wait":
                seconds = float(known.get("wait_seconds") or _PASSIVE_SECONDS) + _PASSIVE_INTERVAL
                return {"kind": "memory", "passive": True, "seconds": seconds, "x": 0, "y": 0,
                        "label": f"attesa ricordata ({int(seconds)}s)"}
            if known.get("kind") == "swipe" and known.get("coords"):
                return {"kind": "memory", "x": known["x"], "y": known["y"], "coords": list(known["coords"]),
                        "wait": known.get("wait_after") or _SWIPE_SECONDS,
                        "label": f"swipe ricordato {list(known['coords'])}"}
            return {"kind": "memory", "x": known["x"], "y": known["y"],
                    "label": f"tocco ricordato ({int(known['x'])},{int(known['y'])})"}
        if ready is None:
            # Nessuno sa dire se il gioco e' pronto: niente tocchi alla cieca su una schermata che potrebbe essere gioco.
            order = ("center", "buttons", "gpt") if dark else ("buttons", "gpt")
        else:
            order = ((("center", "buttons") if dark else ("buttons", "center")) + ("passive_wait",) + _SWIPE_KINDS
                     + ("passive_wait_2", "bottom", "gpt"))
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
            if kind in _SWIPE_KINDS:
                tried.add(kind)
                action = self._swipe_action(kind, path, size)
                if action is None:
                    tried.update(_SWIPE_KINDS)          # niente banner in basso (o niente swipe): salto tutte e tre
                    continue
                tried.add("_swiped")
                return action
            if kind in ("passive_wait", "passive_wait_2"):
                tried.add(kind)
                if kind == "passive_wait_2" and "_swiped" not in tried:
                    continue                            # la seconda attesa ha senso solo dopo gli swipe
                boxes = self._boxes(path)
                if passive_left < _PASSIVE_INTERVAL or not boxes:
                    continue                            # budget finito, nessun OCR o schermo vuoto: niente da aspettare
                return {"kind": kind, "passive": True, "seconds": min(_PASSIVE_SECONDS, passive_left), "x": 0, "y": 0,
                        "label": "attesa passiva"}
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

    def _boxes(self, path: str) -> Optional[list]:
        """Box OCR dello screenshot (una sola lettura per giro, condivisa da caricamento, pulsanti e banner)."""
        if self._boxes_cache is not None and self._boxes_cache[0] == path:
            return self._boxes_cache[1]
        boxes = None
        if self.ocr is not None and callable(getattr(self.ocr, "read_text_boxes", None)):
            try:
                result = self.ocr.read_text_boxes(path)
                if getattr(result, "success", False):
                    boxes = list(result.data or [])
            except Exception as exc:
                logger.warning("intro: OCR dei pulsanti fallito: %s", exc)
        self._boxes_cache = (path, boxes)
        return boxes

    def _button_candidates(self, path: str) -> list:
        """[(testo, x, y)] dei pulsanti generici letti dall'OCR, per priorita'. Niente testi di acquisto/uscita/menu."""
        found = []
        for box in self._boxes(path) or []:
            rank = _button_rank(box.text)
            if rank is None:
                continue
            x1, y1, x2, y2 = box.bbox
            found.append((rank, -(y1 + y2) // 2, box.text.strip(), (x1 + x2) // 2, (y1 + y2) // 2))
        found.sort()
        return [(text, x, y) for _, _, text, x, y in found]

    def _is_loading(self, path, slot_name, size, sig, dark, tried: set) -> bool:
        """Schermata di caricamento: pochissimi testi e nessun pulsante riconosciuto (saldo/puntata sono gia' esclusi
        da _game_ready). Si aspetta invece di toccare a vuoto. L'overlay nero si chiude prima col centro."""
        if dark and "center" not in tried:
            return False
        if "memory" not in tried and sig and any(same_screen(step["sig"], sig) for step in self._load(slot_name, size)):
            return False
        boxes = self._boxes(path)
        if boxes is None:
            return False
        return len(boxes) <= _LOADING_MAX_TEXTS and not self._button_candidates(path)

    def _swipe_action(self, kind: str, path: str, size: tuple) -> Optional[dict]:
        if not callable(getattr(self.touch_injector, "swipe", None)):
            return None
        boxes = self._boxes(path)
        if not boxes:
            return None
        width, height = size
        if not any((box.bbox[1] + box.bbox[3]) / 2 > _BANNER_Y_FRACTION * height for box in boxes):
            logger.info("intro: nessun testo in basso, salto gli swipe per il banner")
            return None
        name, label, fractions = next(item for item in _SWIPES if item[0] == kind)
        x1, y1, x2, y2 = (int(fractions[0] * width), int(fractions[1] * height),
                          int(fractions[2] * width), int(fractions[3] * height))
        return {"kind": kind, "x": x1, "y": y1, "coords": [x1, y1, x2, y2], "wait": _SWIPE_SECONDS,
                "label": f"swipe {label}"}

    def _swipe(self, device_id: str, coords) -> None:
        swipe = getattr(self.touch_injector, "swipe", None)
        if callable(swipe):
            swipe(device_id, *[int(c) for c in coords], _SWIPE_DURATION_MS)

    def _remember_swipe(self, tap: dict, slot_name: str, size: tuple) -> int:
        if not tap.get("sig"):
            return 0
        step = {"sig": tap["sig"], "x": tap["x"], "y": tap["y"], "kind": "swipe",
                "action": f"{tap['kind']}_banner", "coords": tap["coords"], "wait_after": int(_SWIPE_SECONDS)}
        steps = [s for s in self._load(slot_name, size) if not same_screen(s["sig"], tap["sig"])]
        self._save(slot_name, size, steps + [step])
        logger.info("intro: swipe riuscito, lo ricordo per '%s'.", slot_name)
        return 1

    def _passive_wait(self, device_id: str, slot_name: str, sig, seconds: float) -> tuple:
        """Aspetta senza toccare nulla, rileggendo lo schermo ogni _PASSIVE_INTERVAL: (saldo/puntata comparsi, secondi
        attesi). Si ferma se la schermata cambia (il giro normale ricalcola la scala). Solo OCR: niente Sol a ogni lettura."""
        waited = 0.0
        while waited < seconds:
            self.sleep(_PASSIVE_INTERVAL)
            waited += _PASSIVE_INTERVAL
            shot = self.screen_reader._screenshot(device_id)
            if not shot.success:
                break
            self._boxes_cache = None
            if self._amounts_readable(shot.data):         # un banner che sparisce cambia anche la schermata: prima questo
                return True, waited
            new_sig = signature(shot.data)
            if sig and new_sig and not same_screen(sig, new_sig):
                logger.info("intro: durante l'attesa la schermata e' cambiata")
                break
        return False, waited

    def _remember_wait(self, sig, waited: float, slot_name: str, size: tuple) -> int:
        if not sig:
            return 0
        step = {"sig": sig, "x": 0, "y": 0, "kind": "passive_wait", "action": "passive_wait",
                "wait_seconds": int(waited)}
        steps = [st for st in self._load(slot_name, size) if not same_screen(st["sig"], sig)]
        self._save(slot_name, size, steps + [step])
        logger.info("intro: attesa riuscita, la ricordo per '%s'.", slot_name)
        return 1

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

    def _amounts_readable(self, path: str) -> bool:
        """Saldo OPPURE puntata leggibili dall'OCR: su alcune slot un'etichetta non si legge, ma basta una per sapere che
        il gioco e' attivo."""
        state = self._ocr_state(path)
        for field, name in (("saldo", "saldo"), ("posta", "puntata")):
            if state and state.get(field) is not None:
                logger.info("intro: %s leggibile (%s) - slot attiva, esco dalle intro", name, state[field])
                return True
        return False

    def _game_ready(self, device_id: str, slot_name: str, path: str, size: tuple, sig, dark: bool) -> Optional[bool]:
        """True/False se si sa dire se il gioco e' pronto; None se non c'e' modo di giudicare."""
        spin = self.spin_point(slot_name) if callable(self.spin_point) else None
        if self.button is not None and spin is not None and self.button.reference(slot_name, size) is not None:
            return self.button.classify(slot_name, size, path, int(spin[0]), int(spin[1])) is True
        if not dark and self._amounts_readable(path):
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
