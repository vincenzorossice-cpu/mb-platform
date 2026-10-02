import json
import logging
import os
import time
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.interface import (
    DecisionEngineClientInterface,
    choice_question,
    noul_question,
)
from orchestrator.modules.screen_navigator.interface import ScreenNavigatorInterface
from orchestrator.modules.screen_reader.interface import UIElement

logger = logging.getLogger(__name__)

# FIX-LOOP: numero di volte consecutive che la stessa (azione, target) puo'
# essere proposta dal Decision Engine prima di considerarla un loop
_STUCK_LOOP_THRESHOLD = 3

# Scroll "dolce": swipe lento e corto (un terzo di schermo), cosi' il browser non scorre per inerzia
# e ogni box della lista resta visibile almeno una volta con il suo titolo.
_SCROLL_DURATION_MS = 900
_SCROLL_FRACTION = 0.3
# Sotto questa frazione di altezza c'e' la barra di navigazione di sistema (indietro/home/recenti):
# un tocco li' fa uscire dal browser.
_SYSTEM_BAR_FRACTION = 0.95
# Differenza sotto la quale due screenshot sono "lo stesso schermo" (la pagina non si e' mossa).
_PAGE_UNCHANGED_THRESHOLD = 0.01
_MAX_APP_RESTORES = 3

_BOOK_HINTS_PATH = os.path.join(os.path.dirname(__file__), "book_hints.json")


def load_book_hints(book: str, path: str = _BOOK_HINTS_PATH) -> list[str]:
    """Suggerimenti scritti a mano per un book (file JSON modificabile); lista vuota se il file o il book mancano."""
    try:
        with open(path, encoding="utf-8") as hints_file:
            hints = json.load(hints_file).get((book or "").upper(), [])
    except (OSError, ValueError, AttributeError):
        return []
    return [str(hint) for hint in hints] if isinstance(hints, list) else []


# Frazioni (x, y) del centro di ciascuna delle 9 regioni della griglia 3x3 usata dalla visione.
_REGION_FRACTIONS = {
    "top-left": (1 / 6, 1 / 6), "top-center": (1 / 2, 1 / 6), "top-right": (5 / 6, 1 / 6),
    "middle-left": (1 / 6, 1 / 2), "center": (1 / 2, 1 / 2), "middle-right": (5 / 6, 1 / 2),
    "bottom-left": (1 / 6, 5 / 6), "bottom-center": (1 / 2, 5 / 6), "bottom-right": (5 / 6, 5 / 6),
}


def _create_default_decision_engine_client() -> DecisionEngineClientInterface:
    """Sceglie il Decision Engine in base a DECISION_ENGINE_PROVIDER (default: openjev)."""
    provider = os.environ.get("DECISION_ENGINE_PROVIDER", "openjev").lower()
    if provider in ("gpt4o", "gpt6sol"):   # il modello lo sceglie GPTDecisionClient (resolve_model)
        from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient

        return GPTDecisionClient()

    from orchestrator.modules.decision_engine.client import DecisionEngineClient

    return DecisionEngineClient()


class ScreenNavigator(ScreenNavigatorInterface):
    """Navigatore autonomo generico: usa un Decision Engine AI per guidare
    l'interfaccia di qualunque bookmaker verso un obiettivo in linguaggio
    naturale, senza coordinate o keyword hardcoded per-book."""

    LOW_CONFIDENCE_THRESHOLD = 0.15
    MAX_HISTORY = 5
    TAP_SETTLE_SECONDS = 1.0   # attesa prima di confrontare lo schermo dopo un tocco
    MAX_ELEMENTS_SUMMARY = 20

    def __init__(
        self,
        screen_reader,
        touch_injector,
        decision_engine_client: DecisionEngineClientInterface = None,
        max_steps: int = 20,
        vision_fallback: bool = False,
        max_vision_steps: int = 5,
        region_refiner: Optional[Callable] = None,
        step_observer: Optional[Callable[[int, str], None]] = None,
        app_guard: Optional[Callable[[str], bool]] = None,
        app_restore: Optional[Callable[[str], object]] = None,
    ):
        # screen_reader e' tipizzato come la classe concreta ScreenReader (non
        # ScreenReaderInterface): read_screen(..., force_refresh=True) esiste solo
        # sull'implementazione concreta, non sull'ABC astratta. Gap preesistente
        # tra interfaccia e implementazione, non introdotto qui.
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.decision_engine_client = decision_engine_client or _create_default_decision_engine_client()
        self.max_steps = max_steps
        # Con vision_fallback=True, quando il passo a solo testo non basta (confidenza bassa, schermo senza
        # testi leggibili, loop) lo stesso passo si ripete UNA volta con la visione, fino a max_vision_steps.
        self.vision_fallback = vision_fallback
        self.max_vision_steps = max_vision_steps
        # Opzionale: (descrizione, screenshot, larghezza, altezza) -> (x, y) | None, per precisare una
        # regione 3x3 (troppo grossolana per un'icona) con la ricerca a griglia.
        self.region_refiner = region_refiner
        # step_observer(step, percorso_screenshot): chiamato con lo screenshot guardato a ogni passo con la visione.
        self.step_observer = step_observer
        # app_guard(device) -> True se l'app attesa (es. Chrome) e' davanti; se no, app_restore(device) la riporta
        # davanti senza chiamare il modello (al massimo _MAX_APP_RESTORES volte per navigazione).
        self.app_guard = app_guard
        self.app_restore = app_restore
        self._current_step = 0
        self.last_history: list[dict] = []   # azioni dell'ultima navigazione, leggibili anche dopo un fallimento

    def _can_see(self) -> bool:
        return callable(getattr(self.decision_engine_client, "ask_with_image", None))

    def navigate(
        self, device_id: str, goal: str, book: str = "", demo_mode: bool = False, vision_mode: bool = False,
    ) -> Result[bool]:
        if demo_mode:
            goal = self._apply_demo_mode(goal)
        if vision_mode and not self._can_see():
            logger.warning("Vision richiesta ma questo Decision Engine non vede le immagini: resto sul testo.")
            vision_mode = False
        vision_steps_used = 0
        force_vision_next_step = False

        history: list[dict] = []
        self.last_history = []   # storico completo (history, passata al modello, e' limitata a MAX_HISTORY)
        # FIX-LOOP: rileva quando il Decision Engine ripropone la stessa
        # (azione, target) troppe volte di fila (es. tap su un elemento che non
        # produce alcun effetto reale) e prova a sbloccarsi con uno scroll forzato
        # prima di arrendersi.
        consecutive_same_action = 0
        last_action_signature = None
        recovery_attempted = False

        vision_stuck_tried = False
        app_restores = 0

        for step in range(1, self.max_steps + 1):
            self._current_step = step
            use_vision = vision_mode or force_vision_next_step
            force_vision_next_step = False

            if self.app_guard is not None and self.app_restore is not None and not self._app_in_front(device_id):
                if app_restores < _MAX_APP_RESTORES:
                    app_restores += 1
                    logger.warning(f"Step {step}: l'app attesa non e' piu' davanti: la riporto davanti.")
                    self.app_restore(device_id)
                    self._record(history, step, "rientro nell'app", f"tentativo {app_restores}")
                    time.sleep(2)

            screen_res = self.screen_reader.read_screen(device_id, force_refresh=True)
            if screen_res.success:
                texts = self._extract_texts(screen_res.data.elements)
            elif self._vision_allowed(vision_mode, vision_steps_used):
                logger.warning("Lettura schermo fallita (%s): provo con la visione.", screen_res.error)
                texts, use_vision = [], True
            else:
                return Result.fail(f"Screen read failed: {screen_res.error}", "SCREEN_READ_FAILED")

            if not use_vision and not texts and self._vision_allowed(vision_mode, vision_steps_used):
                logger.info("Nessun testo leggibile sullo schermo: provo con la visione.")
                use_vision = True

            state = self._build_state(book, goal, step, history, texts)
            questions = self._build_questions(texts)

            if use_vision:
                decision = self._ask_vision(device_id, state, questions)
                if not vision_mode:
                    vision_steps_used += 1
            else:
                decision = self._ask_text(state, questions)
                if (
                    decision.success and decision.data["confidence"] < self.LOW_CONFIDENCE_THRESHOLD
                    and self._vision_allowed(vision_mode, vision_steps_used)
                ):
                    logger.info("Confidenza bassa a solo testo (%s): ripeto il passo con la visione.",
                                decision.data["confidence"])
                    decision = self._ask_vision(device_id, state, questions)
                    vision_steps_used += 1
            if not decision.success:
                return Result.fail(decision.error, decision.error_code)

            action = decision.data["action"]
            confidence = decision.data["confidence"]
            target = decision.data["target"]
            answers = decision.data["answers"]

            logger.info(f"Step {step}/{self.max_steps}: action={action} target={target} confidence={confidence}")
            reasoning = (answers.get("reasoning") or {}).get("choice")
            if reasoning:
                logger.info(f"Step {step} ragionamento: {reasoning}")

            if decision.data.get("rejected"):
                logger.warning(f"Step {step}: tocco scartato ({decision.data['rejected']}) su {target}.")
                self._record(history, step, "tap scartato", f"{decision.data['rejected']} {target}")
                continue

            if confidence < self.LOW_CONFIDENCE_THRESHOLD:
                return Result.fail(f"Low confidence on action decision: {confidence}", "LOW_CONFIDENCE_HITL")

            if action == "done":
                return Result.ok(True)

            action_signature = (action, target)
            if action_signature == last_action_signature:
                consecutive_same_action += 1
            else:
                consecutive_same_action = 1
                recovery_attempted = False
                vision_stuck_tried = False
            last_action_signature = action_signature

            if consecutive_same_action >= _STUCK_LOOP_THRESHOLD:
                if not use_vision and not vision_stuck_tried and self._vision_allowed(vision_mode, vision_steps_used):
                    # Il solo testo gira a vuoto: prima di scrollare si prova a guardare lo schermo.
                    logger.warning(
                        f"Loop rilevato: azione '{action}' su '{target}' ripetuta {consecutive_same_action} volte. "
                        "Provo con la visione."
                    )
                    vision_stuck_tried = True
                    force_vision_next_step = True
                    consecutive_same_action = 0
                    continue
                if recovery_attempted:
                    logger.warning(
                        f"Loop persistente su azione '{action}' su '{target}' anche dopo il recovery: "
                        "navigazione bloccata."
                    )
                    return Result.fail("Stuck in loop", "NAVIGATION_STUCK")

                logger.warning(
                    f"Loop rilevato: azione '{action}' su '{target}' ripetuta "
                    f"{consecutive_same_action} volte consecutive. Forzo uno scroll di recovery."
                )
                self.touch_injector.scroll(device_id, "down")
                self._invalidate_cache()
                recovery_attempted = True
                consecutive_same_action = 0
                self._record(history, step, "scroll", "down (recovery)")
                continue

            note = self._execute(device_id, action, target, answers)
            self._invalidate_cache()   # lo schermo e' cambiato: la prossima domanda e' sempre nuova
            self._record(history, step, action, f"{target} ({note})" if note else target)

        return Result.fail(f"Goal not reached after {self.max_steps} steps", "GOAL_NOT_REACHED")

    def _app_in_front(self, device_id: str) -> bool:
        try:
            return bool(self.app_guard(device_id))
        except Exception as exc:
            logger.warning("app_guard fallito (%s): lo considero a posto.", exc)
            return True

    def _record(self, history: list[dict], step: int, action: str, target) -> None:
        """Registra l'azione: lo storico passato al modello e' limitato a MAX_HISTORY, quello completo no."""
        entry = {"step": step, "action": action, "target": target}
        self.last_history.append(entry)
        history.append(entry)
        del history[:-self.MAX_HISTORY]

    def _vision_allowed(self, vision_mode: bool, vision_steps_used: int) -> bool:
        """Il ripiego sulla visione e' ammesso solo se attivo, se il motore vede le immagini e se il tetto
        di passi con la visione non e' esaurito."""
        return (
            not vision_mode and self.vision_fallback and self._can_see()
            and vision_steps_used < self.max_vision_steps
        )

    def _invalidate_cache(self) -> None:
        invalidate = getattr(self.decision_engine_client, "invalidate_cache", None)
        if callable(invalidate):
            invalidate()

    def _ask_text(self, state: str, questions: dict) -> Result[dict]:
        ask_res = self.decision_engine_client.ask(state, questions)
        if not ask_res.success:
            return Result.fail(f"Decision engine error: {ask_res.error}", ask_res.error_code)
        answers = ask_res.data.get("answers", {})
        action_answer = answers.get("action", {})
        return Result.ok({
            "action": action_answer.get("choice", "wait"),
            "confidence": action_answer.get("confidence", 0.0),
            "target": answers.get("target", {}).get("choice"),
            "answers": answers,
        })

    def _ask_vision(self, device_id: str, state: str, questions: dict) -> Result[dict]:
        """Passo con la visione: screenshot, domanda al motore, e il bersaglio diventa un testo da tappare
        o un punto (x, y)."""
        shot = self.screen_reader._screenshot(device_id)
        if not shot.success:
            return Result.fail(f"Screenshot failed: {shot.error}", shot.error_code or "SCREENSHOT_FAILED")
        try:
            from PIL import Image
            with Image.open(shot.data) as image:
                width, height = image.size
        except OSError as exc:
            return Result.fail(f"Screenshot unreadable: {exc}", "SCREENSHOT_FAILED")

        if self.step_observer is not None:
            try:
                self.step_observer(self._current_step, shot.data)
            except Exception as exc:
                logger.warning("step_observer fallito: %s", exc)

        ask_res = self.decision_engine_client.ask_with_image(shot.data, state, questions, width, height)
        if not ask_res.success:
            return Result.fail(f"Decision engine error: {ask_res.error}", ask_res.error_code)
        answers = ask_res.data.get("answers", {})
        action_answer = answers.get("action", {})

        raw_target = answers.get("target", {}).get("choice")
        target = None
        refine_failed = False
        if isinstance(raw_target, dict):
            if raw_target.get("text"):
                target = raw_target["text"]
            elif raw_target.get("region") in _REGION_FRACTIONS:
                description = (answers.get("target_description") or {}).get("choice")
                point = self._refine_region(description, shot.data, width, height)
                if point is None and self.region_refiner is not None and description:
                    refine_failed = True   # niente tocco alla cieca al centro della regione
                else:
                    if point is None:
                        fx, fy = _REGION_FRACTIONS[raw_target["region"]]
                        point = (round(width * fx), round(height * fy))
                    target = (int(point[0]), int(point[1]))
        elif isinstance(raw_target, str):
            target = raw_target
        decision = {
            "action": action_answer.get("choice", "wait"),
            "confidence": action_answer.get("confidence", 0.0),
            "target": target,
            "answers": answers,
        }
        if (
            decision["action"] == "tap" and isinstance(target, tuple)
            and target[1] > height * _SYSTEM_BAR_FRACTION
        ):
            decision["rejected"] = "barra di sistema"
        elif decision["action"] == "tap" and refine_failed:
            decision["rejected"] = "punto non precisato"
        return Result.ok(decision)

    def _refine_region(self, description, screenshot_path: str, width: int, height: int):
        if not description or self.region_refiner is None:
            return None
        try:
            return self.region_refiner(description, screenshot_path, width, height)
        except Exception as exc:
            logger.warning("Affinamento della regione fallito (%s).", exc)
            return None

    def _apply_demo_mode(self, goal: str) -> str:
        """Aggiunge l'intento demo/for-fun al goal, cosi' il Decision Engine
        cerca 'Prova'/'For fun' invece di 'Gioca' con soldi veri."""
        return f"{goal} (modalita' demo: cerca il pulsante 'Prova' o 'For fun', NON 'Gioca' con soldi veri)"

    def _build_questions(self, texts: list[str]) -> dict:
        candidates = {t: t for t in texts[: self.MAX_ELEMENTS_SUMMARY]}
        candidates.update({"up": "up", "down": "down", "left": "left", "right": "right"})
        return {
            "action": choice_question(
                "Quale azione compiere per avvicinarsi all'obiettivo?",
                {
                    "tap": "Tocca un elemento a schermo",
                    "input_text": "Inserisci testo in un campo",
                    "scroll": "Scorri la pagina",
                    "wait": "Attendi il caricamento",
                    "done": "Obiettivo raggiunto, ferma la navigazione",
                },
            ),
            "target": choice_question(
                "Su quale elemento agire (testo esatto)? Per scroll: direzione up/down/left/right.",
                candidates,
            ),
            "text_to_type": noul_question("Testo da digitare, solo se l'azione e' input_text"),
        }

    def _execute(self, device_id: str, action: str, target, answers: dict) -> str:
        """Esegue l'azione. Un fallimento viene loggato ma non interrompe il loop:
        il prossimo giro rilegge lo schermo e lascia ripianificare il Decision Engine."""
        note = ""
        if action == "tap":
            before = self._screenshot_path(device_id)
            if isinstance(target, tuple):
                res = self.touch_injector.tap_coordinates(device_id, target[0], target[1])
            else:
                res = self.touch_injector.tap_element(device_id, target)
            note = self._tap_effect_note(device_id, before)
        elif action == "input_text":
            text = answers.get("text_to_type", {}).get("choice", "")
            res = self.touch_injector.input_text(device_id, text, field_hint=target)
        elif action == "scroll":
            return self._scroll(device_id, target)
        elif action == "wait":
            time.sleep(3)
            return ""
        else:
            logger.warning(f"Azione non riconosciuta: {action}")
            return ""

        if not res.success:
            logger.warning(f"Azione '{action}' su '{target}' fallita: {res.error}")
        return note

    def _tap_effect_note(self, device_id: str, before: str) -> str:
        """Confronto locale (0 token) tra lo schermo prima e dopo il tocco: l'esito di tap_element si basa sugli
        elementi letti e su una pagina web di Chrome non e' affidabile."""
        if not before:
            return ""
        time.sleep(self.TAP_SETTLE_SECONDS)
        after = self._screenshot_path(device_id)
        if not after:
            return ""
        from orchestrator.modules.slot_autopilot.frame_diff import diff_score
        diff = diff_score(before, after)
        if diff.success and diff.data < _PAGE_UNCHANGED_THRESHOLD:
            return "schermo invariato: il tocco non ha fatto nulla"
        return ""

    def _scroll(self, device_id: str, target) -> str:
        """Scroll lento e corto nella direzione scelta (up/down, default down). Se lo schermo non cambia la
        pagina e' finita: lo si dice al modello (nota nello storico), che puo' cercare 'Carica altro'."""
        direction = target if target in ("up", "down") else "down"
        before = self._screenshot_path(device_id)
        res = self.touch_injector.scroll(
            device_id, direction, duration_ms=_SCROLL_DURATION_MS, fraction=_SCROLL_FRACTION,
        )
        if not res.success:
            logger.warning(f"Scroll fallito: {res.error}")
            return ""
        time.sleep(0.8)   # fine dell'inerzia residua
        after = self._screenshot_path(device_id)
        if before and after:
            from orchestrator.modules.slot_autopilot.frame_diff import diff_score
            diff = diff_score(before, after)
            if diff.success and diff.data < _PAGE_UNCHANGED_THRESHOLD:
                return "fine pagina, schermo invariato"
        return ""

    def _screenshot_path(self, device_id: str) -> str:
        try:
            shot = self.screen_reader._screenshot(device_id)
        except Exception:
            return ""
        return shot.data if shot.success is True and isinstance(shot.data, str) else ""

    def _extract_texts(self, elements: list[UIElement]) -> list[str]:
        """Testi non vuoti, deduplicati, in ordine di apparizione. Esclude gli
        elementi invisibili (bounds [0,0,0,0], es. un menu hamburger chiuso) che
        non possono essere tappati realmente. Nessuna euristica 'clickable':
        UIElement non ha questo campo e non c'e' modo di inferirlo."""
        seen: set[str] = set()
        out: list[str] = []
        for el in elements:
            if self._is_invisible(el.bounds):
                continue
            t = el.text.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t[:80])
        return out

    def _is_invisible(self, bounds: list[int]) -> bool:
        return len(bounds) >= 4 and bounds[0] == 0 and bounds[1] == 0 and bounds[2] == 0 and bounds[3] == 0

    def _build_state(self, book: str, goal: str, step: int, history: list[dict], texts: list[str]) -> str:
        elements_block = "\n".join(texts[: self.MAX_ELEMENTS_SUMMARY])
        intro = (
            "You are navigating a bookmaker website on Android. Look at the screen "
            "elements and decide what to tap to reach the goal."
        )
        parts = [intro]
        hints = self._goal_hints(goal)
        if hints:
            parts.append(hints)
        book_hints = load_book_hints(book)
        if book_hints:
            parts.append(f"Notes about {book}:\n" + "\n".join(f"- {hint}" for hint in book_hints))
        parts.append(f"Book: {book}\nGoal: {goal}\nStep: {step}")
        parts.append(f"Previous actions: {self._format_history(history)}")
        parts.append(f"Current screen elements:\n{elements_block}")
        return "\n\n".join(parts)

    def _goal_hints(self, goal: str) -> str:
        """Suggerimenti aggiuntivi al Decision Engine basati su parole chiave nel
        goal, per orientare la ricerca sui bottoni tipici delle promo/slot dei
        bookmaker italiani."""
        goal_lower = goal.lower()
        hints = []
        if any(kw in goal_lower for kw in ("attiva", "promo", "slot")):
            hints.append("Look for buttons with text like 'Attiva', 'Aderisci', 'Gioca', 'Prova'.")
        if any(kw in goal_lower for kw in ("demo", "for fun")):
            hints.append("Look for 'Prova' or 'For fun' button, NOT 'Gioca' with real money.")
        return " ".join(hints)

    def _format_history(self, history: list[dict]) -> str:
        if not history:
            return "None"
        return "[" + ", ".join(f"{h['action']} '{h['target']}' at step {h['step']}" for h in history) + "]"
