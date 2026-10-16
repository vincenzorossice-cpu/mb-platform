"""Il "vigile": quando qualcosa di strano succede (tocco sullo spin senza addebito, bonus, free spin, schermo occupato a
lungo...) Sol guarda lo schermo e dice cosa sta succedendo e cosa fare; se non lo sa, chiede all'operatore in TESTO
LIBERO e il testo va a Sol come istruzione. Scala: regole locali -> Sol -> operatore.

Sicurezza: azioni di una lista corta (aspetta, premi lo spin NOTO della mappa, tocca un pulsante descritto che non sia
vietato, riprendi, chiedi, fermati); ogni azione e' verificata (lo schermo deve cambiare); tetti di chiamate."""
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_ocr.vision_reader import _encode_reduced

logger = logging.getLogger(__name__)

_PURPOSE = "vigile"
_SITUATIONS = ("normal", "bonus_intro", "bonus_running", "bonus_finished", "free_spins", "win_overlay", "popup",
               "menu_open", "lost", "unknown")
_ACTIONS = ("wait", "tap_spin", "tap_described", "resume", "ask_operator", "stop")
_FORBIDDEN_WORDS = ("acquista", "buy", "compra", "gamble", "raddoppia", "double", "esci", "exit", "home", "puntata",
                    "bet", "cassa", "deposit", "deposito", "menu", "impostazioni", "settings")
_CHANGED_DIFF = 0.02
_STATIC_DIFF = 0.005
_POLL_SECONDS = 3.0
_STATIC_NEEDS_CLICK_SECONDS = 8.0
_MIN_CONFIDENCE = 0.4
_MAX_TOKENS = 700
# Popup di vincita a tutto schermo ("CONGRATULAZIONI! HAI VINTO... PREMI OVUNQUE PER CONTINUARE"): non ha un bottone, si
# chiude toccando ovunque. Il centro dello schermo e' sicuro: il popup copre tutto.
_WIN_POPUP = re.compile(
    r"congratulazioni|hai\s+vinto|premi\s+ovunque|tocca\s+ovunque|tappa\s+ovunque|tap\s+anywhere|"
    r"tap\s+to\s+continue|press\s+anywhere|touch\s+anywhere", re.IGNORECASE)
_POPUP_NOTE = "popup_win_close: tap_center"
_FALLBACK_CENTER = (540, 1200)
_MAX_FAST_TAPS = 3

_SYSTEM = (
    "You are the supervisor ('vigile') of an automatic player of a slot machine game on an Android phone. The player "
    "just noticed something unusual and asks you what is going on. You see ONE screenshot plus context. Answer ONLY "
    "with JSON: "
    '{"situation": "normal|bonus_intro|bonus_running|bonus_finished|free_spins|win_overlay|popup|menu_open|lost|unknown", '
    '"action": "wait|tap_spin|tap_described|resume|ask_operator|stop", "wait_seconds": number, '
    '"target_description": string|null, "explanation": short string, "confidence": number 0-1, '
    '"learn": string|null}. '
    "free_spins = the game is INSIDE a free spins / free games round. Say free_spins ONLY if you SEE such indicators: a "
    "'Free Spins: N' or 'Free Games: N' counter, the words FREE SPINS / FREE GAMES on screen, or a clearly different "
    "bonus background or theme. If you see NO such indicator the game is normal: a normal spin whose win equals the bet "
    "leaves the balance unchanged, so answer situation 'normal' with action 'resume'. "
    "Rules: free spins and bonus rounds usually play BY THEMSELVES: while they run use action 'wait' and do NOT tap. "
    "Some bonuses show an intro that asks the player to press the spin button (or a Start/Continue button) to begin: "
    "then use 'tap_spin' (the player knows where the spin button is) or 'tap_described' with the exact control to "
    "press. When the normal game is back (reels idle, spin button visible, no overlay) use 'resume'. If you are not "
    "sure what is happening or what to press use 'ask_operator' and say what you see in explanation. NEVER choose to "
    "change the bet, press Exit/Home/Menu, buy a bonus, or gamble/double. 'learn' = ONE short sentence rule about this "
    "game worth remembering (for example how its bonus starts), or null."
)


@dataclass
class Decision:
    situation: str
    action: str
    wait_seconds: float = 0.0
    target_description: Optional[str] = None
    explanation: str = ""
    confidence: float = 0.0
    learn: Optional[str] = None


def parse_decision(data) -> Optional[Decision]:
    if not isinstance(data, dict) or data.get("action") not in _ACTIONS:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        wait_seconds = max(0.0, min(float(data.get("wait_seconds") or 0.0), 30.0))
    except (TypeError, ValueError):
        wait_seconds = 0.0
    situation = data.get("situation") if data.get("situation") in _SITUATIONS else "unknown"
    return Decision(
        situation=situation, action=data["action"], wait_seconds=wait_seconds,
        target_description=data.get("target_description") or None, explanation=str(data.get("explanation") or ""),
        confidence=max(0.0, min(confidence, 1.0)), learn=(str(data["learn"]).strip() if data.get("learn") else None),
    )


def is_win_popup(*texts: Optional[str]) -> bool:
    return any(_WIN_POPUP.search(t or "") for t in texts)


def is_forbidden_target(description: str) -> bool:
    text = (description or "").lower()
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in _FORBIDDEN_WORDS)


class SlotWarden:
    """spin_point(slot) -> (x, y) | None; describe(frase, LocateContext) -> (x, y) | None; ready_probe(slot, path) ->
    True/False/None (freccia dello spin visibile); notes(slot) -> [str]; add_note(slot, testo)."""

    def __init__(self, gpt_client, screen_reader, touch_injector, spin_point: Callable, describe: Optional[Callable] = None,
                 ready_probe: Optional[Callable] = None, notes: Optional[Callable] = None,
                 add_note: Optional[Callable] = None, ask_text: Callable[[str], str] = input,
                 sleep: Callable[[float], None] = time.sleep, max_calls: int = 50, max_calls_per_incident: int = 25,
                 max_operator_turns: int = 6, bonus_timeout_seconds: float = 360.0,
                 popup_probe: Optional[Callable] = None):
        self.gpt_client = gpt_client
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.spin_point = spin_point
        self.describe = describe
        self.ready_probe = ready_probe
        self.notes = notes
        self.add_note = add_note
        self.ask_text = ask_text
        self.sleep = sleep
        self.max_calls = max_calls
        self.max_calls_per_incident = max_calls_per_incident
        self.max_operator_turns = max_operator_turns
        self.bonus_timeout_seconds = bonus_timeout_seconds
        self.calls = 0
        self.incidents = 0
        self.bonus_events = 0
        self.bonus_seconds = 0.0
        self.free_spin_confirmations = 0
        self.popup_probe = popup_probe          # probe(path) -> True se lo schermo mostra un popup di vincita (OCR)
        self._hint: Optional[str] = None        # avviso per la prossima chiamata a Sol
        # Guardia dei popup (PopupGuard): il popup di RADDOPPIO si chiude prima di ogni altra logica, toccando solo Raccogli.
        self.gamble_guard = None
        self.gamble_collected = False           # la sessione deve rileggere la puntata (il popup puo' averla cambiata)

    # ---- ingresso -------------------------------------------------------------------------------------------------
    def handle(self, device_id: str, slot_name: str, trigger: str, history: Optional[list] = None) -> str:
        """Gestisce un episodio. Ritorna 'resume' (si puo' tornare a giocare), 'stop' (l'operatore vuole fermarsi) o
        'give_up' (nemmeno Sol e l'operatore l'hanno risolto: chi chiama decide)."""
        self.incidents += 1
        logger.warning("Vigile: %s", trigger)
        incident_calls = ineffective = operator_turns = 0
        waited = 0.0
        operator_text, operator_helped, learned = None, False, None
        in_bonus = False
        fast_taps = 0
        while True:
            if incident_calls >= self.max_calls_per_incident or self.calls >= self.max_calls:
                logger.warning("Vigile: budget di chiamate finito per questo episodio.")
                return "give_up" if not self._can_ask() else self._ask_or_finish(device_id, slot_name, "budget finito")
            if self.gamble_guard is not None:
                cleared = self._clear_gamble(device_id, slot_name)
                if cleared == "resume":
                    return "resume"
            shot = self._screenshot(device_id)
            if shot is None:
                return "give_up"
            if fast_taps < _MAX_FAST_TAPS and self._popup_remembered(slot_name) and self._popup_on_screen(shot[0]):
                # Regola gia' appresa: nessuna chiamata a Sol, tap al centro e si riguarda.
                fast_taps += 1
                logger.info("Vigile: popup vincita rilevato, tap al centro per chiudere (regola ricordata, senza Sol).")
                if self._close_win_popup(device_id, slot_name, shot[0], remembered=True):
                    self.sleep(1.0)
                    continue
            decision = self._assess(shot[0], slot_name, trigger, history or [], operator_text)
            self.calls += 1
            incident_calls += 1
            operator_used = operator_text is not None
            operator_text = None
            if decision is None:
                ineffective += 1
                if ineffective >= 3:
                    return self._operator_or_give_up(device_id, slot_name, "Sol non risponde")
                continue
            logger.info("Vigile: %s -> %s (%s) fiducia %.2f", decision.situation, decision.action,
                        decision.explanation, decision.confidence)
            if decision.situation == "free_spins":
                # Free spin confermati da Sol (indizi visivi): la sessione entra nella modalita' free spin e prosegue con
                # controlli locali, senza richiamare Sol a ogni giro gratuito.
                self.free_spin_confirmations += 1
                return "free_spins"
            if decision.learn:
                learned = decision.learn
            if decision.situation in ("bonus_intro", "bonus_running") and not in_bonus:
                in_bonus = True
                self.bonus_events += 1

            if decision.action == "resume":
                self._store_note(slot_name, learned if operator_helped else None)
                return "resume"
            if decision.action == "stop":
                return "stop"
            low_confidence = decision.confidence < _MIN_CONFIDENCE and decision.action != "wait"
            if decision.action == "ask_operator" or low_confidence:
                operator_turns += 1
                verdict, operator_text = self._operator_turn(device_id, slot_name, decision, operator_turns)
                if verdict in ("stop", "give_up"):
                    return verdict
                if verdict == "resume":
                    return "resume"
                operator_helped = True
                continue
            if decision.action == "wait":
                seconds = decision.wait_seconds or _POLL_SECONDS * 3
                waited += self._wait_locally(device_id, slot_name, seconds)
                if waited > self.bonus_timeout_seconds:
                    return self._operator_or_give_up(device_id, slot_name, "il bonus dura troppo")
                ineffective = 0
                continue
            changed = self._act(device_id, slot_name, decision, shot[0])
            ineffective = 0 if changed else ineffective + 1
            if ineffective >= 3:
                return self._operator_or_give_up(device_id, slot_name, "le azioni non cambiano lo schermo")
            self.sleep(1.0)

    def _clear_gamble(self, device_id: str, slot_name: str) -> Optional[str]:
        """Prima di tutto: un popup di raddoppio vincita? Si tocca SOLO Raccogli (mai Raddoppia). 'resume' se chiuso."""
        try:
            outcome = self.gamble_guard.clear(device_id, slot_name, suspect=True)
        except Exception as exc:
            logger.warning("Vigile: controllo del popup di raddoppio fallito: %s", exc)
            return None
        if outcome.success and outcome.data.closed and outcome.data.kind == "gamble":
            self.gamble_collected = True
            return "resume"
        return None

    # ---- azioni ---------------------------------------------------------------------------------------------------
    def _act(self, device_id: str, slot_name: str, decision: Decision, before_path: str) -> bool:
        if decision.action == "tap_spin":
            point = self.spin_point(slot_name) if callable(self.spin_point) else None
            if point is None:
                logger.warning("Vigile: serve lo spin ma non conosco il suo punto.")
                return False
            logger.info("Vigile: premo lo spin noto a (%d,%d).", point[0], point[1])
        else:   # tap_described
            description = decision.target_description or ""
            if not description or is_forbidden_target(description):
                logger.warning("Vigile: bersaglio non ammesso ('%s'): non tocco.", description)
                return False
            if is_win_popup(description, decision.explanation):
                return self._close_win_popup(device_id, slot_name, before_path, remembered=False)
            if self.describe is None:
                return False
            size = self._size(before_path)
            point = self.describe(description, LocateContext(
                device_id=device_id, slot_name=slot_name, screenshot_path=before_path,
                screen_width=size[0], screen_height=size[1])) if size else None
            if point is None:
                logger.warning("Vigile: non trovo '%s' con precisione: non tocco.", description)
                return False
            logger.info("Vigile: tocco '%s' a (%d,%d).", description, point[0], point[1])
        self.touch_injector.tap_coordinates(device_id, int(point[0]), int(point[1]))
        self.sleep(1.5)
        after = self._screenshot(device_id)
        if after is None:
            return False
        diff = diff_score(before_path, after[0])
        return bool(diff.success and diff.data >= _CHANGED_DIFF)

    def _popup_remembered(self, slot_name: str) -> bool:
        if not callable(self.popup_probe) or not callable(self.notes):
            return False
        try:
            return any(str(n).startswith(_POPUP_NOTE) for n in (self.notes(slot_name) or []))
        except Exception:
            return False

    def _popup_on_screen(self, path: str) -> bool:
        try:
            return bool(self.popup_probe(path))
        except Exception as exc:    # la corsia veloce e' un'ottimizzazione: se la sonda fallisce si va da Sol
            logger.warning("Vigile: sonda del popup fallita: %s", exc)
            return False

    def _close_win_popup(self, device_id: str, slot_name: str, before_path: str, remembered: bool) -> bool:
        """Tap al centro dello schermo (nessuna ricerca di un elemento). La regola si salva solo se lo schermo cambia."""
        size = self._size(before_path)
        point = (size[0] // 2, size[1] // 2) if size else _FALLBACK_CENTER
        if not remembered:
            logger.info("Vigile: popup vincita rilevato, tap al centro per chiudere")
        self.touch_injector.tap_coordinates(device_id, int(point[0]), int(point[1]))
        self.sleep(1.5)
        after = self._screenshot(device_id)
        diff = diff_score(before_path, after[0]) if after is not None else None
        changed = bool(diff is not None and diff.success and diff.data >= _CHANGED_DIFF)
        if changed:
            if not remembered:
                self._store_note(slot_name, _POPUP_NOTE)
        else:
            self._hint = "The tap in the center of the screen did not close this popup: do not repeat it, find another way."
            logger.warning("Vigile: il tap al centro non ha chiuso il popup.")
        return changed

    def _wait_locally(self, device_id: str, slot_name: str, seconds: float) -> float:
        """Attesa con controlli locali (nessun GPT): si esce prima se la freccia dello spin torna visibile o se lo
        schermo resta fermo a lungo (di solito chiede un tocco). Ritorna i secondi aspettati (a passi di
        _POLL_SECONDS; il tempo degli screenshot si aggiunge e rende l'attesa un po' piu' lunga, mai piu' corta)."""
        elapsed = static_for = 0.0
        previous = self._screenshot(device_id)
        while elapsed < seconds:
            self.sleep(_POLL_SECONDS)
            elapsed += _POLL_SECONDS
            current = self._screenshot(device_id)
            if current is None:
                break
            if callable(self.ready_probe) and self.ready_probe(slot_name, current[0]) is True:
                break
            if previous is not None:
                diff = diff_score(previous[0], current[0])
                if diff.success and diff.data < _STATIC_DIFF:
                    static_for += _POLL_SECONDS
                    if static_for >= _STATIC_NEEDS_CLICK_SECONDS:
                        break
                else:
                    static_for = 0.0
            previous = current
        self.bonus_seconds += elapsed
        return elapsed

    # ---- operatore ---------------------------------------------------------------------------------------------------
    def _can_ask(self) -> bool:
        return callable(self.ask_text)

    def _operator_or_give_up(self, device_id: str, slot_name: str, reason: str) -> str:
        if not self._can_ask():
            return "give_up"
        return self._ask_or_finish(device_id, slot_name, reason)

    def _ask_or_finish(self, device_id: str, slot_name: str, reason: str) -> str:
        """Senza piu' chiamate a Sol da usare: si chiede all'operatore solo se vuole riprovare da solo ('skip') o
        fermare; un testo libero non si puo' piu' interpretare."""
        verdict, _ = self._operator_turn(device_id, slot_name, Decision("unknown", "ask_operator", explanation=reason),
                                         self.max_operator_turns)
        return verdict if verdict in ("resume", "stop") else "give_up"

    def _operator_turn(self, device_id: str, slot_name: str, decision: Decision, turn: int) -> tuple:
        """Chiede all'operatore in testo libero. Ritorna (verdetto, testo): 'continue' con il testo da passare a Sol,
        'resume' (skip), 'stop', 'give_up' (turni finiti o nessun terminale)."""
        if turn > self.max_operator_turns or not callable(self.ask_text):
            return "give_up", None
        print("\n" + "=" * 60)
        print(f"VIGILE: {decision.explanation or 'non so cosa sta succedendo'}")
        print(f"(situazione: {decision.situation})")
        print("=" * 60)
        print("Scrivi cosa sta succedendo o cosa devo fare (testo libero), oppure:")
        print("  - 'x,y' per un tocco diretto (es. 540,1776)")
        print("  - 'skip' per riprovare da solo, 'stop' per fermare tutto")
        try:
            text = self.ask_text("> ").strip()
        except (EOFError, KeyboardInterrupt, OSError):
            return "give_up", None
        lowered = text.lower()
        if lowered == "stop":
            return "stop", None
        if lowered in ("skip", ""):
            return "resume", None
        match = re.fullmatch(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            self.touch_injector.tap_coordinates(device_id, int(match.group(1)), int(match.group(2)))
            self.sleep(1.5)
            return "continue", "the operator tapped the point you were shown; look at the new screen and decide"
        return "continue", text

    # ---- Sol ---------------------------------------------------------------------------------------------------------
    def _assess(self, path: str, slot_name: str, trigger: str, history: list, operator_text: Optional[str]) -> Optional[Decision]:
        try:
            encoded = _encode_reduced(path)
        except OSError:
            return None
        spin = self.spin_point(slot_name) if callable(self.spin_point) else None
        notes = (self.notes(slot_name) if callable(self.notes) else []) or []
        context = [f"Slot: {slot_name}.", f"Why you are called: {trigger}.",
                   f"The spin button is always at pixel {spin}." if spin else "The spin button position is unknown."]
        if history:
            context.append("Recent events (oldest first): " + " | ".join(history[-8:]))
        if notes:
            context.append("Rules already learned about this game: " + " | ".join(notes))
        if self._hint:
            context.append(self._hint)
            self._hint = None
        if operator_text:
            context.append(f"INSTRUCTION FROM THE HUMAN OPERATOR (follow it, it knows the game): {operator_text}")
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "\n".join(context)},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ]},
        ]
        result = self.gpt_client._create_completion(messages, max_tokens=_MAX_TOKENS, purpose=_PURPOSE)
        if not result.success:
            logger.warning("Vigile: nessuna risposta utilizzabile da Sol (%s).", getattr(result, "error", None))
            return None
        return parse_decision(result.data)

    def _store_note(self, slot_name: str, note: Optional[str]) -> None:
        if note and callable(self.add_note):
            try:
                self.add_note(slot_name, note)
                logger.info("Vigile: regola appresa per '%s': %s", slot_name, note)
            except Exception as exc:   # imparare non deve mai rompere la sessione
                logger.warning("Vigile: nota non salvata: %s", exc)

    def _screenshot(self, device_id: str):
        shot = self.screen_reader._screenshot(device_id)
        return (shot.data,) if shot.success else None

    @staticmethod
    def _size(path: str):
        try:
            with Image.open(path) as image:
                return image.size
        except OSError:
            return None

    def summary(self) -> str:
        return (f"Vigile: {self.incidents} interventi, {self.calls} chiamate a Sol, {self.bonus_events} bonus gestiti "
                f"({self.bonus_seconds:.0f} s di attesa), {self.free_spin_confirmations} conferme di free spin")
