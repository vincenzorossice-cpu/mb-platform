import json
import logging
import os
import time
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.login_manager import ui_helpers
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
# Pagina bloccata ("wait" ripetuti): scala di recupero scroll -> refresh -> ri-navigazione. Il 1o livello scatta al 3o
# "wait" di fila, i successivi dopo altri _WAIT_RECOVERY_THRESHOLD "wait" (o subito se lo schermo non e' cambiato).
_WAIT_RECOVERY_THRESHOLD = 2
_REFRESH_WAIT_SECONDS = 5
# Tocchi scartati di fila (punto non precisabile): al 2o si cambia inquadratura con uno scroll, al 4o si ferma.
_REFRAME_AFTER_REJECTIONS = 2
_STOP_AFTER_REJECTIONS = 4

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
        start_url: Optional[str] = None,
        url_opener: Optional[Callable[[str, str], object]] = None,
        relogin: Optional[Callable[[str, str], object]] = None,
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
        # Recupero di una pagina bloccata / sessione scaduta (tutti opzionali, il navigatore non conosce URL ne' login):
        # url_opener(device, url) -> Result apre un URL (es. BookNavigator.open_chrome); start_url e' la pagina da cui
        # parte la navigazione; relogin(device, book) -> Result[LoginResult] rifa' il login (LoginExecutor.ensure_logged_in).
        self.start_url = start_url
        self.url_opener = url_opener
        self.relogin = relogin
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
        self.last_history = []   # storico completo (history, passata al modello, e' limitata a MAX_HISTORY), anche
        return self._run(device_id, goal, book, vision_mode)   # attraverso le ripartenze da capo

    def _run(
        self, device_id: str, goal: str, book: str, vision_mode: bool,
        wait_level: int = 0, recovery_restarts: int = 0, relogin_used: bool = False,
    ) -> Result[bool]:
        """Una navigazione, o una ripartenza da capo (storico, contatori e budget di passi nuovi). wait_level,
        recovery_restarts e relogin_used passano da una ripartenza all'altra: la scala di recupero e il nuovo login
        non ripartono da zero, altrimenti una pagina davvero bloccata girerebbe all'infinito."""
        vision_steps_used = 0
        force_vision_next_step = False

        history: list[dict] = []
        # FIX-LOOP: rileva quando il Decision Engine ripropone la stessa
        # (azione, target) troppe volte di fila (es. tap su un elemento che non
        # produce alcun effetto reale) e prova a sbloccarsi con uno scroll forzato
        # prima di arrendersi.
        consecutive_same_action = 0
        last_action_signature = None
        recovery_attempted = False

        vision_stuck_tried = False
        app_restores = 0
        rejections = 0
        recovery_signature = None   # schermo prima dell'ultimo recupero: se non cambia, il recupero non e' servito

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
                    self._event("rientro_nell_app")
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

            # Sessione scaduta? Si controlla solo con un segnale di blocco (due "wait" di fila), mai a ogni passo.
            if (
                screen_res.success and last_action_signature is not None and last_action_signature[0] == "wait"
                and consecutive_same_action >= _WAIT_RECOVERY_THRESHOLD
                and ui_helpers.session_state(screen_res.data) == "login_required"
            ):
                return self._recover_expired_session(
                    device_id, goal, book, vision_mode, wait_level, recovery_restarts, relogin_used)

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
                self._event("tocco_scartato")
                rejections += 1
                if rejections >= _STOP_AFTER_REJECTIONS:
                    logger.error(f"{rejections} tocchi scartati di fila: non riesco a precisare il punto, mi fermo.")
                    return Result.fail(
                        f"Cannot pin down the tap point after {rejections} rejections in a row", "TAP_NOT_LOCATABLE",
                    )
                if rejections == _REFRAME_AFTER_REJECTIONS:
                    self._reframe(device_id, history, step)
                continue
            rejections = 0

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

            wait_loop = False
            if action == "wait":
                needed = _STUCK_LOOP_THRESHOLD if wait_level == 0 else _WAIT_RECOVERY_THRESHOLD
                unchanged = (
                    wait_level > 0 and recovery_signature is not None and screen_res.success
                    and ui_helpers.screen_signature(screen_res.data) == recovery_signature
                )
                wait_loop = consecutive_same_action >= needed or unchanged
            else:
                wait_level, recovery_signature = 0, None   # GPT ha cambiato azione: il recupero ha funzionato

            if wait_loop:
                if (
                    wait_level == 0 and not use_vision and not vision_stuck_tried
                    and self._vision_allowed(vision_mode, vision_steps_used)
                ):
                    logger.warning(
                        f"Loop rilevato: azione '{action}' su '{target}' ripetuta {consecutive_same_action} volte. "
                        "Provo con la visione."
                    )
                    vision_stuck_tried = True
                    force_vision_next_step = True
                    consecutive_same_action = 0
                    continue
                finished, outcome = self._wait_recovery(
                    device_id, goal, book, vision_mode, wait_level, recovery_restarts, relogin_used,
                    screen_res, history, step, consecutive_same_action)
                if finished:
                    return outcome
                wait_level += 1
                recovery_signature = ui_helpers.screen_signature(screen_res.data) if screen_res.success else None
                consecutive_same_action = 0
                continue

            if action != "wait" and consecutive_same_action >= _STUCK_LOOP_THRESHOLD:
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
            if action == "scroll" and not note:
                # La pagina si e' mossa: uno scroll che avanza non e' un loop (liste lunghe, "Carica altro").
                consecutive_same_action = 0
                last_action_signature = None
            self._invalidate_cache()   # lo schermo e' cambiato: la prossima domanda e' sempre nuova
            self._record(history, step, action, f"{target} ({note})" if note else target)

        return Result.fail(f"Goal not reached after {self.max_steps} steps", "GOAL_NOT_REACHED")

    def _wait_recovery(
        self, device_id, goal, book, vision_mode, wait_level, recovery_restarts, relogin_used,
        screen_res, history, step, consecutive,
    ) -> tuple:
        """Il prossimo livello della scala di recupero su una pagina bloccata ("wait" ripetuti). Ritorna
        (True, Result) se la navigazione finisce qui (ripartenza da capo o fallimento), (False, None) se il recupero
        e' stato fatto sul posto e si prosegue."""
        level = wait_level + 1
        screen = screen_res.data if screen_res.success else None
        # Oltre lo scroll, prima di insistere (e prima di ri-navigare) si controlla che non sia la sessione scaduta.
        if level >= 2 and screen is not None and ui_helpers.session_state(screen) == "login_required":
            return True, self._recover_expired_session(
                device_id, goal, book, vision_mode, wait_level, recovery_restarts, relogin_used)

        if level == 1:
            logger.warning(f"Recovery 1/3: scroll (azione 'wait' ripetuta {consecutive} volte consecutive)")
            self.touch_injector.scroll(device_id, "down")
            self._invalidate_cache()
            self._record(history, step, "scroll", "down (recovery 1/3)")
            return False, None
        if level == 2:
            logger.warning("Recovery 2/3: refresh pagina")
            self._refresh_page(device_id, screen)
            self._invalidate_cache()
            self._record(history, step, "refresh", "pagina (recovery 2/3)")
            return False, None
        if level == 3:
            if recovery_restarts >= 1 or not (self.start_url and self.url_opener):
                logger.error("Recovery 3/3: ri-navigazione non disponibile (nessun URL di partenza o gia' fatta): "
                             "navigazione fallita.")
                return True, Result.fail("Page stuck and re-navigation is not available", "NAVIGATION_FAILED")
            logger.warning("Recovery 3/3: ri-naviga dall'inizio")
            self.last_history.append({"step": step, "action": "ri-naviga", "target": f"{self.start_url} (recovery 3/3)"})
            reopened = self._reopen_start(device_id)
            if not reopened.success:
                return True, Result.fail(f"Re-navigation failed: {reopened.error}", "NAVIGATION_FAILED")
            return True, self._run(device_id, goal, book, vision_mode, wait_level=3,
                                   recovery_restarts=recovery_restarts + 1, relogin_used=relogin_used)
        logger.error("Pagina ancora bloccata dopo scroll, refresh e ri-navigazione: navigazione fallita.")
        return True, Result.fail("Page still stuck after scroll, refresh and re-navigation", "NAVIGATION_FAILED")

    def _refresh_page(self, device_id: str, screen) -> None:
        """Ricarica la pagina: riapre l'URL corrente (letto dalla barra di Chrome) o, se non si legge, pull-to-refresh."""
        url = ui_helpers.current_chrome_url(screen)
        if url and self.url_opener is not None:
            logger.info("Refresh: riapro %s", url)
            opened = self.url_opener(device_id, url)
            if getattr(opened, "success", True):
                return
            logger.warning("Refresh con l'URL fallito (%s): provo il pull-to-refresh.", getattr(opened, "error", ""))
        for _ in range(2):   # fino in cima alla pagina, poi un trascinamento verso il basso che la ricarica
            self.touch_injector.scroll(device_id, "up", duration_ms=400, fraction=0.8)
        self.touch_injector.scroll(device_id, "up", duration_ms=700, fraction=0.5)
        time.sleep(_REFRESH_WAIT_SECONDS)

    def _reopen_start(self, device_id: str) -> Result[bool]:
        """Riapre l'URL di partenza. Senza start_url/url_opener non c'e' nulla da riaprire: si riparte da dove si e'."""
        if not (self.start_url and self.url_opener is not None):
            return Result.ok(False)
        try:
            opened = self.url_opener(device_id, self.start_url)
        except Exception as exc:
            return Result.fail(str(exc), "URL_OPEN_FAILED")
        if not getattr(opened, "success", True):
            return Result.fail(getattr(opened, "error", "apertura URL fallita"), "URL_OPEN_FAILED")
        return Result.ok(True)

    def _recover_expired_session(
        self, device_id, goal, book, vision_mode, wait_level, recovery_restarts, relogin_used,
    ) -> Result[bool]:
        """Il telefono mostra il pulsante di login a meta' navigazione: si rifa' il login (una sola volta) e si
        ri-naviga dall'inizio. Senza relogin, o se il login non riesce, SESSION_EXPIRED."""
        logger.warning("Sessione scaduta durante la navigazione, rifaccio login")
        if self.relogin is None:
            return Result.fail("Session expired during navigation and no relogin is available", "SESSION_EXPIRED")
        if relogin_used:
            return Result.fail("Session expired again after a new login", "SESSION_EXPIRED")
        try:
            login = self.relogin(device_id, book)
        except Exception as exc:
            return Result.fail(f"Re-login failed: {exc}", "SESSION_EXPIRED")
        logged_in = bool(getattr(login, "success", False)) and bool(getattr(getattr(login, "data", None), "is_logged_in", False))
        if not logged_in:
            status = getattr(getattr(login, "data", None), "status", None) or getattr(login, "error_code", None)
            logger.error("Login non riuscito durante la navigazione (%s): sessione scaduta.", status)
            return Result.fail(f"Re-login failed ({status})", "SESSION_EXPIRED")
        logger.info("Login rifatto: ri-navigo dall'inizio")
        self.last_history.append({"step": self._current_step, "action": "login", "target": "sessione scaduta, login rifatto"})
        reopened = self._reopen_start(device_id)
        if not reopened.success:
            return Result.fail(f"Re-navigation after login failed: {reopened.error}", "NAVIGATION_FAILED")
        return self._run(device_id, goal, book, vision_mode, wait_level=0,
                         recovery_restarts=recovery_restarts, relogin_used=True)

    def _event(self, kind: str) -> None:
        record = getattr(self.decision_engine_client, "record_event", None)
        if callable(record):
            record(kind, "navigazione")

    def _reframe(self, device_id: str, history: list, step: int) -> None:
        """Dopo due punti non precisabili di fila lo stesso bersaglio e' probabilmente a un bordo: uno scroll
        lento lo sposta e la prossima domanda parte da un'inquadratura nuova."""
        res = self.touch_injector.scroll(
            device_id, "down", duration_ms=_SCROLL_DURATION_MS, fraction=_SCROLL_FRACTION,
        )
        if not res.success:
            logger.warning(f"Scroll di cambio inquadratura fallito: {res.error}")
            return
        time.sleep(0.8)
        self._invalidate_cache()
        self._record(history, step, "scroll", "down (cambio inquadratura)")
        self._event("cambio_inquadratura")

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
            note = self._tap_effect_note(device_id, before, is_point=isinstance(target, tuple))
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

    def _tap_effect_note(self, device_id: str, before: str, is_point: bool = False) -> str:
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
            return "schermo invariato: il tocco non ha fatto nulla" + (" (punto forse impreciso)" if is_point else "")
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
