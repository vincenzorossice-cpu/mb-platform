"""Autospin: la slot gira da sola e il bot controlla a campione invece di toccare lo spin a ogni giro.

Si cerca dall'EFFETTO, non dall'icona: dopo il tocco su un candidato compare un pannello con i numeri di giri, o un
contatore sul pulsante spin, o il saldo comincia a muoversi. Non si abilita nulla se non si e' verificato anche COME SI
FERMA. Il tocco sullo SPIN non e' mai un candidato (a puntata di default spenderebbe piu' del minimo). L'esplorazione
gira solo in demo e dopo la puntata minima verificata; l'esecuzione conta i giri in modo prudente (sottostima)."""
import logging
import math
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.slot_map import AutospinInfo, now_iso

logger = logging.getLogger(__name__)

AUTO_WORDS = ("AUTOSPIN", "AUTO SPIN", "AUTOPLAY", "AUTO PLAY", "AUTOMATIC", "AUTOMATICI", "AUTOGIRO", "AUTO")
MENU_WORDS = ("MENU", "IMPOSTAZIONI", "SETTINGS", "OPZIONI", "OPTIONS")
STOP_WORDS = ("STOP", "FERMA", "INTERROMPI", "ARRESTA")
CONFIRM_WORDS = ("START", "AVVIA", "INIZIA", "PLAY", "GIOCA", "OK", "CONFERMA", "CONFIRM")
UNLIMITED_WORDS = ("ILLIMITATO", "ILLIMITATI", "INFINITO", "INFINITE", "UNLIMITED", "∞")
LIMIT_WORDS = ("LIMITE", "LIMIT", "PERDITA", "LOSS")

SETTLE_SECONDS = 1.5
MAX_TAPS = 10                       # tocchi totali dell'esplorazione
CANDIDATES_PER_LEVEL = 3
SCREEN_CHANGED = 0.02               # differenza dopo un tocco oltre cui "lo schermo e' cambiato"
SPIN_ZONE_PX = 70                   # un candidato dentro questa distanza dallo spin e' lo spin: mai toccato
COUNTER_RADIUS_PX = 200             # un numero entro questa distanza dallo spin e' il contatore sul pulsante
MIN_OPTIONS = 2                     # un pannello di giri ha almeno due numeri
_OPTION_RANGE = (5, 100000)         # valori plausibili come numero di giri
_POLL_SECONDS = 2.0
_STOP_READS = 3
_RUNNING_READS = 3
MAX_UNKNOWN_ATTEMPTS = 2            # esplorazioni fallite per slot prima di non insistere

_EXPLORE_SYSTEM = (
    "You are looking for the AUTOSPIN / AUTOPLAY control of a slot game on an Android phone (a control that makes the "
    "game spin by itself for a number of rounds), and for the menu/settings button. You see ONE screenshot. Answer ONLY "
    'with JSON: {"autospin": {"x": int, "y": int, "description": str} | null, "menu": {"x": int, "y": int, '
    '"description": str} | null}. Coordinates are NORMALIZED to a 0-1000 grid (x from the left, y from the top), the '
    "CENTER of the control. Its icon varies from game to game (play triangle, circular arrows, the word AUTO, "
    "a gear, three dots or lines for the menu). NEVER indicate the main SPIN button. Use null when you cannot see it."
)


@dataclass
class Shot:
    path: str
    size: tuple


@dataclass
class SampleReport:
    state: str          # ok | bet_changed | low_balance | stalled | stalled_blocked | needs_attention
    balance: Optional[Decimal] = None
    spins: int = 0      # giri accreditati da questo campione (prudenti)
    message: str = ""


@dataclass
class AutospinRun:
    info: AutospinInfo
    period: float                         # secondi per giro (reali, senza il margine di prudenza)
    started_at: float
    last_sample_at: float
    last_balance: Optional[Decimal] = None
    counter_start: Optional[int] = None
    counter_credited: int = 0
    cycles: int = 1
    unreadable: int = 0
    spin_point: Optional[tuple] = None


@dataclass
class CycleResult:
    reason: str         # threshold | budget | bet_changed | low_balance | needs_attention | start_failed | stop_failed
    credited_spins: int = 0
    credited_played: Decimal = Decimal("0")
    stopped: bool = True
    cycles: int = 0
    message: str = ""


class ScreenTools:
    """Strumenti condivisi con il TurboManager: screenshot, box OCR, saldo, tocchi verificati dall'effetto."""

    def __init__(self, screen_reader, touch_injector, ocr_reader=None, ask: Optional[Callable] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.ocr_reader = ocr_reader
        self.ask = ask
        self.sleep = sleep
        self._boxes_cache = (None, [])

    def shot(self, device_id: str) -> Optional[Shot]:
        result = self.screen_reader._screenshot(device_id)
        if not result.success:
            return None
        try:
            with Image.open(result.data) as image:
                return Shot(result.data, image.size)
        except OSError:
            return None

    def boxes(self, shot: Shot) -> list:
        if self._boxes_cache[0] == shot.path and self._boxes_cache[1]:
            return self._boxes_cache[1]
        boxes = []
        reader = getattr(self.ocr_reader, "read_text_boxes", None)
        if callable(reader):
            try:
                result = reader(shot.path)
                if getattr(result, "success", False):
                    boxes = list(result.data or [])
            except Exception as exc:
                logger.warning("autospin: OCR dei riquadri fallito: %s", exc)
        self._boxes_cache = (shot.path, boxes)
        return boxes

    def balance(self, path: str) -> Optional[Decimal]:
        reader = getattr(self.ocr_reader, "read_slot_state", None)
        if not callable(reader):
            return None
        try:
            value = reader(path).get("saldo")
            return Decimal(str(value)) if value is not None else None
        except Exception:
            return None

    def tap(self, device_id: str, x: int, y: int) -> None:
        self.touch_injector.tap_coordinates(device_id, int(x), int(y))

    def tap_changes(self, device_id: str, x: int, y: int) -> tuple:
        """(cambiato, schermo_prima, schermo_dopo)."""
        before = self.shot(device_id)
        self.tap(device_id, x, y)
        self.sleep(SETTLE_SECONDS)
        after = self.shot(device_id)
        if before is None or after is None or before.size != after.size:
            return False, before, after
        diff = diff_score(before.path, after.path)
        return bool(diff.success and diff.data >= SCREEN_CHANGED), before, after

    def words(self, shot: Shot, words: tuple) -> list:
        """[(x, y, testo)] dei riquadri OCR che contengono una delle parole (intera)."""
        found = []
        for box in self.boxes(shot):
            text = str(getattr(box, "text", "")).upper()
            if any(re.search(rf"(?<![A-Z0-9]){re.escape(word)}(?![A-Z0-9])", text) for word in words):
                x1, y1, x2, y2 = box.bbox
                found.append(((x1 + x2) // 2, (y1 + y2) // 2, text))
        return found

    def sol_points(self, shot: Shot, system: str, user: str) -> dict:
        """Punti indicati da Sol (coordinate 0-1000) in pixel: {nome: (x, y)}."""
        if self.ask is None:
            return {}
        try:
            answer = self.ask(shot.path, system, user)
        except Exception as exc:
            logger.warning("autospin: domanda a Sol fallita: %s", exc)
            return {}
        points = {}
        for name, raw in (answer or {}).items():
            try:
                nx, ny = float(raw["x"]), float(raw["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= nx <= 1000 and 0 <= ny <= 1000:
                points[name] = (round(nx / 1000 * shot.size[0]), round(ny / 1000 * shot.size[1]))
        return points


def _near(a: tuple, b: tuple, radius: float) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= radius


class AutoSpinManager:
    def __init__(self, screen_reader, touch_injector, ocr_reader=None, ask: Optional[Callable] = None, button=None,
                 bet_watch=None, ask_operator: Optional[Callable[[str], str]] = None,
                 press_back: Optional[Callable[[], object]] = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, sample_interval: int = 15, period_safety: float = 1.2,
                 default_period: float = 6.0, popup_guard=None,
                 before_start: Optional[Callable[[], Optional[str]]] = None):
        self.tools = ScreenTools(screen_reader, touch_injector, ocr_reader, ask, sleep)
        self.button = button
        self.bet_watch = bet_watch
        self.ask_operator = ask_operator
        self.press_back = press_back
        self.sleep = sleep
        self.clock = clock
        self.sample_interval = max(1, int(sample_interval))
        self.period_safety = max(1.0, float(period_safety))
        self.default_period = default_period
        self.popup_guard = popup_guard           # PopupGuard: i popup (raddoppio!) si chiudono col pulsante giusto
        self.before_start = before_start         # () -> None (via libera) | motivo: puntata verificata, nessun popup

    # ---- esplorazione --------------------------------------------------------------------------------------------------
    def explore_autospin(self, device_id: str, slot_name: str, spin_point: Optional[tuple],
                         bet_panel_point: Optional[tuple] = None, previous: Optional[AutospinInfo] = None
                         ) -> Result[AutospinInfo]:
        """Cerca l'autospin: schermata principale, poi menu, poi pannello puntata (mai oltre due livelli). Result con
        `AutospinInfo` (present verificato | absent | unknown); fail solo se non si riesce a vedere lo schermo o se un
        autospin avviato non si lascia fermare (AUTOSPIN_RUNNING: serve l'operatore)."""
        first = self.tools.shot(device_id)
        if first is None:
            return Result.fail("screenshot fallito", "AUTOSPIN_SCREENSHOT_FAILED")
        attempts = (previous.attempts if previous is not None else 0)
        sequence: list = []
        taps = [0]
        levels = [("main", None)]
        if bet_panel_point is not None:
            levels.append(("bet_panel", bet_panel_point))
        levels.append(("menu", None))
        for level, opener in levels:
            shot = first if level == "main" else None
            opened = False
            if level != "main":
                shot, opened = self._open_level(device_id, level, opener, first, sequence, taps, spin_point)
                if shot is None:
                    continue
            hit = self._search(device_id, slot_name, level, shot, spin_point, sequence, taps)
            if hit is not None:
                return self._complete(device_id, slot_name, level, hit, spin_point, sequence, attempts)
            if opened:
                self._close_level(device_id, level, opener, first, sequence, taps)
        if self._busy(device_id, slot_name, spin_point) is True:
            self._safe_stop(device_id, slot_name, spin_point)
        logger.info("Autospin: non trovato su questa slot")
        return Result.ok(AutospinInfo(status="absent", attempts=attempts, explored_at=now_iso()))

    def _open_level(self, device_id, level, opener, first: Shot, sequence: list, taps: list, spin_point):
        point = opener
        if level == "menu":
            point = self._menu_point(first, spin_point)
        if point is None or taps[0] >= MAX_TAPS:
            return None, False
        changed, _, after = self.tools.tap_changes(device_id, point[0], point[1])
        taps[0] += 1
        if not changed or after is None:
            return None, False
        sequence.append({"x": point[0], "y": point[1], "wait": SETTLE_SECONDS, "note": f"apre {level}"})
        return after, True

    def _close_level(self, device_id, level, opener, first: Shot, sequence: list, taps: list) -> None:
        """Riporta la slot alla schermata di partenza: stesso tocco (toggle), altrimenti il tasto indietro."""
        if sequence and sequence[-1].get("note") == f"apre {level}":
            last = sequence.pop()
            self.tools.tap(device_id, last["x"], last["y"])
            self.sleep(SETTLE_SECONDS)
            now = self.tools.shot(device_id)
            if now is not None and first is not None:
                diff = diff_score(first.path, now.path)
                if diff.success and diff.data < 0.06:
                    return
            if self.press_back is not None:
                self.press_back()
                self.sleep(SETTLE_SECONDS)

    def _menu_point(self, shot: Shot, spin_point) -> Optional[tuple]:
        for x, y, _ in self.tools.words(shot, MENU_WORDS):
            return x, y
        sol = self.tools.sol_points(shot, _EXPLORE_SYSTEM, "Find the menu / settings button.")
        point = sol.get("menu")
        return point if point is not None and not self._is_spin(point, spin_point) else None

    @staticmethod
    def _is_spin(point, spin_point) -> bool:
        return spin_point is not None and _near(point, spin_point, SPIN_ZONE_PX)

    def _candidates(self, shot: Shot, spin_point) -> list:
        found = [(x, y) for x, y, _ in self.tools.words(shot, AUTO_WORDS)]
        sol = self.tools.sol_points(shot, _EXPLORE_SYSTEM, "Find the autospin / autoplay control.")
        if "autospin" in sol:
            found.append(sol["autospin"])
        unique = []
        for point in found:
            if self._is_spin(point, spin_point) or any(_near(point, other, 30) for other in unique):
                continue                                         # lo SPIN non si tocca; i doppioni nemmeno
            unique.append(point)
        return unique[:CANDIDATES_PER_LEVEL]

    def _search(self, device_id, slot_name, level, shot: Shot, spin_point, sequence: list, taps: list):
        for point in self._candidates(shot, spin_point):
            if taps[0] >= MAX_TAPS:
                break
            changed, before, after = self.tools.tap_changes(device_id, point[0], point[1])
            taps[0] += 1
            if after is None:
                continue
            effect = self._effect(device_id, slot_name, before, after, spin_point)
            if effect is not None:
                sequence.append({"x": point[0], "y": point[1], "wait": SETTLE_SECONDS, "note": "autospin"})
                return {"point": point, "effect": effect, "after": after}
            if changed:                                          # ha aperto altro: si annulla con lo stesso tocco
                self.tools.tap(device_id, point[0], point[1])
                taps[0] += 1
                self.sleep(SETTLE_SECONDS)
        return None

    # ---- l'effetto -----------------------------------------------------------------------------------------------------
    def _numbers(self, shot: Shot) -> list:
        """[(valore, x, y)] dei riquadri OCR che sono un numero di giri plausibile (interi)."""
        numbers = []
        for box in self.tools.boxes(shot):
            text = str(getattr(box, "text", "")).strip()
            if re.fullmatch(r"\d{1,6}", text) and _OPTION_RANGE[0] <= int(text) <= _OPTION_RANGE[1]:
                x1, y1, x2, y2 = box.bbox
                numbers.append((int(text), (x1 + x2) // 2, (y1 + y2) // 2))
        return numbers

    def _effect(self, device_id, slot_name, before: Shot, after: Shot, spin_point) -> Optional[dict]:
        """Cosa ha fatto il tocco: {"kind": "panel"|"counter"|"running", ...} o None (non e' l'autospin)."""
        before_values = {value for value, _, _ in self._numbers(before)}
        numbers = [n for n in self._numbers(after) if n[0] not in before_values]     # solo i numeri COMPARSI col tocco
        unlimited = bool(self.tools.words(after, UNLIMITED_WORDS))
        if len(numbers) >= MIN_OPTIONS or (numbers and unlimited):
            return {"kind": "panel", "options": numbers, "unlimited": unlimited,
                    "limit": bool(self.tools.words(after, LIMIT_WORDS))}
        if spin_point is not None:
            near_before = {v for v, x, y in self._numbers(before) if _near((x, y), spin_point, COUNTER_RADIUS_PX)}
            near_after = [v for v, x, y in self._numbers(after) if _near((x, y), spin_point, COUNTER_RADIUS_PX)]
            if any(v not in near_before for v in near_after):
                return {"kind": "counter", "value": near_after[0]}
        if self._running(device_id, slot_name, spin_point):
            return {"kind": "running"}
        return None

    def _ready(self, slot_name, shot: Shot, spin_point) -> Optional[bool]:
        """True = freccia dello spin visibile (gioco pronto), False = occupato, None = non classificabile."""
        if self.button is None or spin_point is None:
            return None
        try:
            return self.button.classify(slot_name, shot.size, shot.path, int(spin_point[0]), int(spin_point[1]))
        except Exception:
            return None

    def _running(self, device_id, slot_name, spin_point) -> bool:
        """La slot sta girando da sola: il saldo cambia tra le letture o il pulsante spin e' occupato."""
        balances, busy = [], 0
        for index in range(_RUNNING_READS):
            if index:
                self.sleep(_POLL_SECONDS)
            shot = self.tools.shot(device_id)
            if shot is None:
                continue
            balances.append(self.tools.balance(shot.path))
            if self._ready(slot_name, shot, spin_point) is False:
                busy += 1
        known = [b for b in balances if b is not None]
        return len(set(known)) > 1 or busy >= 2

    def _busy(self, device_id, slot_name, spin_point) -> Optional[bool]:
        shot = self.tools.shot(device_id)
        return None if shot is None else (self._ready(slot_name, shot, spin_point) is False)

    def _stopped(self, device_id, slot_name, spin_point) -> bool:
        """Fermo davvero: saldo uguale su tutte le letture e la freccia (se si sa leggere) e' tornata."""
        balances, ready = [], []
        for index in range(_STOP_READS):
            if index:
                self.sleep(_POLL_SECONDS)
            shot = self.tools.shot(device_id)
            if shot is None:
                return False
            balances.append(self.tools.balance(shot.path))
            ready.append(self._ready(slot_name, shot, spin_point))
        if None in balances or len(set(balances)) != 1:
            return False
        return ready[-1] is not False

    # ---- completamento: opzioni, avvio, FERMATA verificata --------------------------------------------------------------
    def _complete(self, device_id, slot_name, level, hit, spin_point, sequence: list, attempts: int
                  ) -> Result[AutospinInfo]:
        effect = hit["effect"]
        info = AutospinInfo(status="unknown", location=level, button_coords=list(hit["point"]), attempts=attempts,
                            explored_at=now_iso())
        if effect["kind"] == "panel":
            numbers = sorted(effect["options"])
            info.spin_options = [v for v, _, _ in numbers] or None
            info.requires_loss_limit = bool(effect["limit"])
            if effect["unlimited"] and not [v for v, _, _ in numbers if v >= 1000]:
                info.type, info.max_spins = "infinite", None
            elif numbers:
                info.type, info.max_spins = "fixed", numbers[-1][0]
            chosen = self.tools.words(hit["after"], UNLIMITED_WORDS)
            target = chosen[0][:2] if chosen else ((numbers[-1][1], numbers[-1][2]) if numbers else None)
            if target is not None:
                self.tools.tap(device_id, *target)
                self.sleep(SETTLE_SECONDS)
                sequence.append({"x": target[0], "y": target[1], "wait": SETTLE_SECONDS, "note": "numero di giri"})
            confirm = self.tools.shot(device_id)
            for x, y, _ in (self.tools.words(confirm, CONFIRM_WORDS) if confirm is not None else [])[:1]:
                self.tools.tap(device_id, x, y)
                self.sleep(SETTLE_SECONDS)
                sequence.append({"x": x, "y": y, "wait": SETTLE_SECONDS, "note": "conferma"})
        elif effect["kind"] == "counter":
            info.counter_location = "button"
        info.touch_sequence = [dict(step) for step in sequence]
        if not self._running(device_id, slot_name, spin_point):
            logger.warning("Autospin: il controllo trovato non fa girare la slot da sola: non lo uso.")
            info.status = "unknown"
            info.attempts = attempts + 1
            return Result.ok(info)
        info.period_seconds = self._measure_period(device_id, slot_name, spin_point)
        method = self._verify_stop(device_id, slot_name, spin_point, info)
        if method is None:
            return self._operator_stop(device_id, slot_name, spin_point, info, attempts)
        info.stop_method = method[0]
        info.stop_coords = list(method[1]) if method[1] is not None else None
        info.status = "present"
        info.verified = not info.requires_loss_limit
        if info.requires_loss_limit:
            logger.warning("Autospin: richiede un limite di perdita: non lo abilito senza l'operatore.")
        logger.info("Autospin: trovato (%s, tipo %s, fermata %s), verificato=%s.", level, info.type, info.stop_method,
                    info.verified)
        return Result.ok(info)

    def _measure_period(self, device_id, slot_name, spin_point) -> Optional[float]:
        """Secondi per giro: fronti 'occupato' -> 'pronto' della freccia in una finestra di ~12 s. None se non si vede."""
        edges, last, started = 0, None, self.clock()
        for _ in range(8):
            shot = self.tools.shot(device_id)
            if shot is None:
                break
            ready = self._ready(slot_name, shot, spin_point)
            if last is False and ready is True:
                edges += 1
            if ready is not None:
                last = ready
            self.sleep(0.5)
        elapsed = self.clock() - started
        return round(elapsed / edges, 2) if edges >= 3 and elapsed > 0 else None

    def _verify_stop(self, device_id, slot_name, spin_point, info: AutospinInfo):
        """(metodo, coordinate) della fermata che ha funzionato, o None. Prima il tocco sullo SPIN (di solito diventa
        Stop), poi un pulsante Stop trovato dall'OCR."""
        if spin_point is not None:
            self.tools.tap(device_id, spin_point[0], spin_point[1])
            self.sleep(SETTLE_SECONDS)
            if self._stopped(device_id, slot_name, spin_point):
                return "tap_spin", tuple(spin_point)
        shot = self.tools.shot(device_id)
        for x, y, _ in (self.tools.words(shot, STOP_WORDS) if shot is not None else [])[:2]:
            self.tools.tap(device_id, x, y)
            self.sleep(SETTLE_SECONDS)
            if self._stopped(device_id, slot_name, spin_point):
                return "tap_stop", (x, y)
        return None

    def _operator_stop(self, device_id, slot_name, spin_point, info: AutospinInfo, attempts: int
                       ) -> Result[AutospinInfo]:
        """Autospin avviato ma non si ferma: mai lasciarlo girare. Prima l'operatore, poi errore."""
        if self.ask_operator is not None:
            try:
                answer = self.ask_operator(
                    "Ho avviato l'autospin ma non riesco a fermarlo. Fermalo tu sul telefono e premi INVIO "
                    "(oppure scrivi 'stop' per interrompere).")
            except (EOFError, KeyboardInterrupt):
                answer = "stop"
            if str(answer).strip().lower() != "stop" and self._stopped(device_id, slot_name, spin_point):
                info.status, info.verified, info.attempts = "unknown", False, attempts + 1
                logger.warning("Autospin: fermato dall'operatore, la fermata automatica non e' verificata: non lo uso.")
                return Result.ok(info)
        return Result.fail("autospin avviato e non fermabile", "AUTOSPIN_RUNNING")

    def _safe_stop(self, device_id, slot_name, spin_point) -> None:
        if spin_point is not None:
            self.tools.tap(device_id, spin_point[0], spin_point[1])
            self.sleep(SETTLE_SECONDS)

    # ---- esecuzione ----------------------------------------------------------------------------------------------------
    def start_autospin(self, device_id: str, slot_name: str, info: AutospinInfo, spin_point: Optional[tuple],
                       period: float) -> Result[AutospinRun]:
        if not info.usable():
            return Result.fail("autospin non utilizzabile (non verificato)", "AUTOSPIN_NOT_USABLE")
        if self.before_start is not None:
            blocker = self.before_start()
            if blocker:
                logger.warning("Autospin: non parte (%s)", blocker)
                return Result.fail(blocker, "AUTOSPIN_PREFLIGHT")
        for step in info.touch_sequence or []:
            self.tools.tap(device_id, step["x"], step["y"])
            self.sleep(float(step.get("wait", SETTLE_SECONDS)))
        if not self._running(device_id, slot_name, spin_point):
            return Result.fail("l'autospin non e' partito", "AUTOSPIN_NOT_STARTED")
        now = self.clock()
        shot = self.tools.shot(device_id)
        balance = self.tools.balance(shot.path) if shot is not None else None
        counter = self._counter(shot, spin_point) if shot is not None and info.counter_location else None
        logger.info("Autospin: avviato (tipo: %s, giri: %s)", info.type, info.max_spins or "illimitati")
        return Result.ok(AutospinRun(info=info, period=period, started_at=now, last_sample_at=now,
                                     last_balance=balance, counter_start=counter, spin_point=spin_point))

    def _counter(self, shot: Shot, spin_point) -> Optional[int]:
        if spin_point is None:
            return None
        values = [v for v, x, y in self._numbers(shot) if _near((x, y), spin_point, COUNTER_RADIUS_PX)]
        return values[0] if values else None

    def monitor_sample(self, device_id: str, slot_name: str, run: AutospinRun, bet: Decimal) -> SampleReport:
        """UN campione: saldo (1 lettura OCR), impronta della cella di puntata, pulsante spin."""
        shot = self.tools.shot(device_id)
        if shot is None:
            run.unreadable += 1
            return SampleReport("needs_attention" if run.unreadable >= 3 else "ok", message="screenshot fallito")
        now = self.clock()
        elapsed = max(0.0, now - run.last_sample_at)
        run.last_sample_at = now
        if self.bet_watch is not None and self.bet_watch.check(shot.path) is True:
            return SampleReport("bet_changed", message="la cella di puntata selezionata e' cambiata")
        balance = self.tools.balance(shot.path)
        if balance is None:
            run.unreadable += 1
            if run.unreadable >= 3:
                return SampleReport("needs_attention", message="saldo illeggibile per 3 campioni")
            return SampleReport("ok", None, self._time_spins(run, elapsed), "saldo illeggibile")
        run.unreadable = 0
        if bet is not None and bet > 0 and balance < bet * self.sample_interval:
            return SampleReport("low_balance", balance, 0, "saldo troppo basso per un altro campione")
        previous, run.last_balance = run.last_balance, balance
        ready = self._ready(slot_name, shot, run.spin_point)
        if previous is None or balance != previous:
            return SampleReport("ok", balance, self._spins_since(run, shot, elapsed))
        # Saldo fermo: autospin finito/interrotto (pulsante pronto) o popup (pulsante non pronto).
        return SampleReport("stalled" if ready is True else "stalled_blocked", balance, 0,
                            "saldo invariato dall'ultimo campione")

    def _time_spins(self, run: AutospinRun, elapsed: float) -> int:
        return int(elapsed // (max(run.period, 0.5) * self.period_safety))

    def _spins_since(self, run: AutospinRun, shot: Shot, elapsed: float) -> int:
        """Giri dall'ultimo campione: dal contatore se si legge (esatto), altrimenti dal tempo (prudente)."""
        if run.counter_start is not None:
            value = self._counter(shot, run.spin_point)
            if value is not None and 0 <= value <= run.counter_start:
                done = run.counter_start - value
                gained = max(0, done - run.counter_credited)
                run.counter_credited = done
                return gained
        return self._time_spins(run, elapsed)

    def _close_popup(self, device_id: str, slot_name: str) -> bool:
        """Chiude il popup che ha fermato l'autospin. Con la guardia: raddoppio -> SOLO Raccogli. Senza guardia il
        tocco al centro e' ammesso solo se l'OCR non vede parole di raddoppio (il centro potrebbe essere "Raddoppia")."""
        if self.popup_guard is not None:
            outcome = self.popup_guard.clear(device_id, slot_name, suspect=True)
            if not outcome.success:
                return False
            if outcome.data.closed:
                return True
        shot = self.tools.shot(device_id)
        if shot is None:
            return True
        from orchestrator.modules.slot_autopilot.popup_guard import GAMBLE_WORDS, _has
        if any(_has(str(getattr(b, "text", "")), GAMBLE_WORDS) for b in self.tools.boxes(shot)):
            logger.warning("Autospin: parole di raddoppio a schermo e nessuna guardia: non tocco il centro.")
            return False
        self.tools.tap(device_id, shot.size[0] // 2, shot.size[1] // 2)
        self.sleep(SETTLE_SECONDS)
        logger.info("Autospin: schermo fermo (popup?), tocco al centro e ricontrollo")
        return True

    def stop_autospin(self, device_id: str, slot_name: str, info: AutospinInfo, spin_point: Optional[tuple]
                      ) -> Result[bool]:
        point = info.stop_coords if info.stop_method in ("tap_stop", "tap_button") and info.stop_coords else spin_point
        if point is None:
            return Result.fail("nessun modo noto di fermare l'autospin", "AUTOSPIN_NO_STOP")
        for _ in range(2):
            self.tools.tap(device_id, point[0], point[1])
            self.sleep(SETTLE_SECONDS)
            if self._stopped(device_id, slot_name, spin_point):
                return Result.ok(True)
        return Result.fail("l'autospin non si e' fermato", "AUTOSPIN_NOT_STOPPED")

    def restart_autospin(self, device_id: str, slot_name: str, run: AutospinRun, spin_point) -> Result[AutospinRun]:
        started = self.start_autospin(device_id, slot_name, run.info, spin_point, run.period)
        if not started.success:
            return started
        new = started.data
        new.cycles = run.cycles + 1
        return Result.ok(new)

    def run_cycle(self, device_id: str, slot_name: str, info: AutospinInfo, spin_point: Optional[tuple],
                  bet: Decimal, played_budget: Decimal, spins_budget: int, period: float) -> CycleResult:
        """Avvia e sorveglia l'autospin finche' il giocato prudente raggiunge `played_budget` (o finiscono i giri
        disponibili), poi lo ferma con fermata verificata. Gestisce giri fissi finiti, popup e riavvii."""
        started = self.start_autospin(device_id, slot_name, info, spin_point, period)
        if not started.success:
            return CycleResult("preflight_failed" if started.error_code == "AUTOSPIN_PREFLIGHT" else "start_failed",
                               message=started.error or "")
        run = started.data
        credited_spins = 0
        reason, message = "budget", ""
        popup_tried = False
        idle = 0
        while True:
            budget_spins = min(spins_budget, int(played_budget / bet)) if bet else spins_budget
            if credited_spins >= budget_spins:
                reason = "threshold" if bet and credited_spins * bet >= played_budget else "budget"
                break
            unit = max(run.period, 0.5) * self.period_safety        # secondi che accreditano UN giro
            wait = min(self.sample_interval * max(run.period, 0.5), max(1, budget_spins - credited_spins) * unit)
            self.sleep(max(wait, unit))
            report = self.monitor_sample(device_id, slot_name, run, bet)
            credited_spins += report.spins
            idle = idle + 1 if report.state == "ok" and report.spins == 0 else 0
            if idle >= 5:
                reason, message = "needs_attention", "nessun giro accreditato per 5 campioni"
                break
            if report.state == "ok":
                popup_tried = False
                continue
            if report.state in ("bet_changed", "low_balance", "needs_attention"):
                reason, message = report.state, report.message
                break
            if report.state == "stalled_blocked" and not popup_tried:
                popup_tried = True
                if not self._close_popup(device_id, slot_name):
                    reason, message = "needs_attention", "popup che non si chiude senza rischi"
                    break
                continue
            logger.info("Autospin: %s, riavvio" % ("fermato da popup" if (report.state == "stalled_blocked" or popup_tried)
                                                   else "ciclo completato"))
            restarted = self.restart_autospin(device_id, slot_name, run, spin_point)
            popup_tried = False
            if not restarted.success:
                reason = "preflight_failed" if restarted.error_code == "AUTOSPIN_PREFLIGHT" else "needs_attention"
                message = restarted.error or "riavvio fallito"
                break
            run = restarted.data
        stopped = self.stop_autospin(device_id, slot_name, info, spin_point)
        if not stopped.success:
            return CycleResult("stop_failed", credited_spins, bet * credited_spins if bet else Decimal("0"), False,
                               run.cycles, stopped.error or "")
        return CycleResult(reason, credited_spins, bet * credited_spins if bet else Decimal("0"), True, run.cycles,
                           message)
