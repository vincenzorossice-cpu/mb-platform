import logging
import time

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


class ScreenNavigator(ScreenNavigatorInterface):
    """Navigatore autonomo generico: usa un Decision Engine AI per guidare
    l'interfaccia di qualunque bookmaker verso un obiettivo in linguaggio
    naturale, senza coordinate o keyword hardcoded per-book."""

    LOW_CONFIDENCE_THRESHOLD = 0.15
    MAX_HISTORY = 5
    MAX_ELEMENTS_SUMMARY = 20

    def __init__(
        self,
        screen_reader,
        touch_injector,
        decision_engine_client: DecisionEngineClientInterface,
        max_steps: int = 20,
    ):
        # screen_reader e' tipizzato come la classe concreta ScreenReader (non
        # ScreenReaderInterface): read_screen(..., force_refresh=True) esiste solo
        # sull'implementazione concreta, non sull'ABC astratta. Gap preesistente
        # tra interfaccia e implementazione, non introdotto qui.
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.decision_engine_client = decision_engine_client
        self.max_steps = max_steps

    def navigate(self, device_id: str, goal: str, book: str = "", demo_mode: bool = False) -> Result[bool]:
        if demo_mode:
            goal = self._apply_demo_mode(goal)

        history: list[dict] = []
        # FIX-LOOP: rileva quando il Decision Engine ripropone la stessa
        # (azione, target) troppe volte di fila (es. tap su un elemento che non
        # produce alcun effetto reale) e prova a sbloccarsi con uno scroll forzato
        # prima di arrendersi.
        consecutive_same_action = 0
        last_action_signature = None
        recovery_attempted = False

        for step in range(1, self.max_steps + 1):
            screen_res = self.screen_reader.read_screen(device_id, force_refresh=True)
            if not screen_res.success:
                return Result.fail(f"Screen read failed: {screen_res.error}", "SCREEN_READ_FAILED")

            texts = self._extract_texts(screen_res.data.elements)
            state = self._build_state(book, goal, step, history, texts)
            questions = self._build_questions(texts)

            ask_res = self.decision_engine_client.ask(state, questions)
            if not ask_res.success:
                return Result.fail(f"Decision engine error: {ask_res.error}", ask_res.error_code)

            answers = ask_res.data.get("answers", {})
            action_answer = answers.get("action", {})
            action = action_answer.get("choice", "wait")
            confidence = action_answer.get("confidence", 0.0)
            target = answers.get("target", {}).get("choice")

            logger.info(f"Step {step}/{self.max_steps}: action={action} target={target} confidence={confidence}")

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
            last_action_signature = action_signature

            if consecutive_same_action >= _STUCK_LOOP_THRESHOLD:
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
                recovery_attempted = True
                consecutive_same_action = 0
                history.append({"step": step, "action": "scroll", "target": "down (recovery)"})
                history = history[-self.MAX_HISTORY:]
                continue

            self._execute(device_id, action, target, answers)
            history.append({"step": step, "action": action, "target": target})
            history = history[-self.MAX_HISTORY:]

        return Result.fail(f"Goal not reached after {self.max_steps} steps", "GOAL_NOT_REACHED")

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

    def _execute(self, device_id: str, action: str, target, answers: dict) -> None:
        """Esegue l'azione. Un fallimento viene loggato ma non interrompe il loop:
        il prossimo giro rilegge lo schermo e lascia ripianificare il Decision Engine."""
        if action == "tap":
            res = self.touch_injector.tap_element(device_id, target)
        elif action == "input_text":
            text = answers.get("text_to_type", {}).get("choice", "")
            res = self.touch_injector.input_text(device_id, text, field_hint=target)
        elif action == "scroll":
            res = self.touch_injector.scroll(device_id, "down")
        elif action == "wait":
            time.sleep(3)
            return
        else:
            logger.warning(f"Azione non riconosciuta: {action}")
            return

        if not res.success:
            logger.warning(f"Azione '{action}' su '{target}' fallita: {res.error}")

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
