import base64
import json
import os
import re
import time
from datetime import datetime
from typing import Optional

import openai

from orchestrator.config.book_navigation import BOOK_NAVIGATION
from orchestrator.core.openai_compat import create_with_param_fallback
from orchestrator.core.openai_retry import call_with_rate_limit_retry
from orchestrator.core.result import Result
from orchestrator.modules.book_navigator.navigator import BookNavigator
from orchestrator.modules.login_manager import adb_input, ui_helpers
from orchestrator.modules.login_manager.recipe import (
    CAPTCHA_INDICATORS,
    DEFAULT_LOGGED_IN_INDICATORS,
    DEFAULT_LOGIN_FAILED_INDICATORS,
    LOGIN_BUTTON_INDICATORS,
    LoginRecipe,
    LoginStep,
    RecipeStore,
)
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement

GPT_ACTIONS = ("tap_button", "tap_field", "tap_coordinates", "press_enter", "done", "login_failed")

GUIDED_PROMPT = """Sei un assistente che guida il login automatico su un bookmaker italiano tramite un telefono Android.
Guarda lo screenshot e gli elementi UI letti dal telefono. Decidi la prossima azione.

Elementi UI visibili:
{elementi_ui}

Rispondi SOLO in JSON:
{"action": "tap_button o tap_field o tap_coordinates o press_enter o done o login_failed",
 "target_text": "testo del pulsante o null",
 "target_label": "label del campo o null",
 "x": null, "y": null,
 "description": "cosa stai facendo"}

Regole:
- Se vedi un popup Chrome Touch to Fill con un bottone Accedi, tappa quel bottone
- Se vedi una barra sopra la tastiera con un suggerimento email, tappa il suggerimento
- Se i campi sono già riempiti e serve premere il tasto Accedi del FORM (non della header), usa tap_coordinates con le coordinate del bottone submit in BASSO (y > 1000)
- Se la tastiera copre il bottone, usa press_enter per chiudere la tastiera
- Se vedi la pagina loggata (saldo, profilo, logout), rispondi done
- Se vedi un errore di login, rispondi login_failed"""

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class LoginMapper:
    """Mappa il flusso di login di un book con GPT Vision, step by step.

    A ogni step GPT guarda lo screenshot + gli elementi UI letti da ScreenReader e decide UNA
    azione (tap_button, tap_field, tap_coordinates, press_enter, done, login_failed); noi la
    eseguiamo, attendiamo e ripetiamo (max MAX_STEPS). Dagli step eseguiti si ricava il
    LoginRecipe, che il LoginExecutor ripete in modo deterministico.

    Mapper ed Executor risolvono i target con le stesse funzioni di ui_helpers: cio' che
    funziona qui colpisce lo stesso elemento al replay.

    Attenzione: il mapping esegue login REALI con le credenziali salvate in Chrome. Gli
    screenshot (PNG locali) vengono cancellati subito dopo la lettura; se il telefono e' gia'
    loggato al primo step non viene inviato nulla a OpenAI. Richiede OPENAI_API_KEY.
    """

    MAX_STEPS = 10
    STEP_WAIT = 3         # attesa dopo ogni azione
    PAGE_LOAD_WAIT = 3    # dopo l'apertura del book
    MAX_REPEATS = 3       # azioni identiche consecutive: oltre, MAP_STUCK (non martellare il submit)
    MAX_UI_ELEMENTS = 80
    MAX_UI_TEXT = 60
    MAX_HISTORY = 5
    MAX_EXTRA_INDICATORS = 5
    # attese scritte nel recipe (>= a quelle del mapping, mai sotto)
    TAP_WAIT = 5
    FIELD_WAIT = 3
    ENTER_WAIT = 3
    GPT_TIMEOUT = 60
    GPT_MAX_TOKENS = 800

    def __init__(
        self,
        screen_reader,
        touch_injector,
        openai_api_key: Optional[str] = None,
        model: str = "gpt-4o",
        book_navigator: Optional[BookNavigator] = None,
        recipe_store: Optional[RecipeStore] = None,
    ):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.openai_api_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model
        self.book_navigator = book_navigator or BookNavigator(screen_reader, touch_injector)
        self.recipe_store = recipe_store or RecipeStore()

    # --------------------------------------------------------
    # PUBLIC
    # --------------------------------------------------------

    def map_book_login(self, device_id: str, book: str, login_url: Optional[str] = None) -> Result[LoginRecipe]:
        """Mappa il flusso di login completo di un book con GPT Vision step-by-step.

        1. Apre il book (BookNavigator.open_chrome)
        2. Loop GPT-guided (max MAX_STEPS): screenshot + elementi UI -> azione -> esecuzione -> attesa
           - "done" -> login completato; "login_failed" -> fail
        3. Genera il recipe dagli step eseguiti e lo salva nel RecipeStore
        """
        if not self.openai_api_key:
            return Result.fail("OPENAI_API_KEY non configurata", "OPENAI_API_KEY_MISSING")
        book_key = book.strip().upper()
        cfg = BOOK_NAVIGATION.get(book_key, {})
        url = login_url or cfg.get("login_url") or cfg.get("balance_url")
        if not url:
            return Result.fail(f"Nessun URL di partenza per {book_key}", "NO_LOGIN_URL")

        opened = self.book_navigator.open_chrome(device_id, url)
        if not opened.success:
            return Result.fail(opened.error or "Apertura Chrome fallita", "NAVIGATION_FAILED")
        time.sleep(self.PAGE_LOAD_WAIT)

        executed: list[dict] = []     # step eseguiti con successo (diventano il recipe)
        history: list[str] = []       # esiti, riportati a GPT nello step successivo
        seen_texts: set[str] = set()  # testi delle schermate precedenti (per gli indicatori finali)
        pending: Optional[dict] = None
        last_key, repeats = None, 0
        has_captcha, captcha_type = False, None

        for step_num in range(1, self.MAX_STEPS + 1):
            guided = self._gpt_guided_step(device_id, step_num, history)
            if not guided.success:
                return Result.fail(guided.error, guided.error_code)
            data = guided.data
            screen = data["screen_content"]

            # la schermata di adesso dice se l'azione precedente ha cambiato lo stato
            if pending is not None:
                pending["changed"] = ui_helpers.screen_signature(screen) != pending.pop("_signature_before")
                pending = None
            if ui_helpers.has_indicators(screen, CAPTCHA_INDICATORS):
                has_captcha = True
                captcha_type = captcha_type or (
                    "reCAPTCHA" if ui_helpers.has_indicators(screen, ["reCAPTCHA"]) else "captcha")

            if data["status"] == "done":
                if not executed:
                    if step_num == 1:
                        return Result.fail("Sessione gia' attiva: niente da mappare", "ALREADY_LOGGED_IN")
                    return Result.fail("GPT ha dichiarato il login completato senza azioni eseguite", "MAP_FAILED")
                recipe = self._generate_recipe_from_steps(
                    book_key, url, executed, screen, exclude_texts=seen_texts,
                    has_captcha=has_captcha, captcha_type=captcha_type)
                saved = self.recipe_store.save_recipe(recipe)
                if not saved.success:
                    return Result.fail(saved.error, saved.error_code)
                return Result.ok(recipe)

            if data["status"] == "failed":
                reason = _EMAIL.sub("<email>", data.get("description") or "")
                return Result.fail(f"GPT ha rilevato un login fallito: {reason}".strip(), "MAP_LOGIN_FAILED")

            key = (data["action"], data["target_text"], data["target_label"], data["x"], data["y"])
            repeats = repeats + 1 if key == last_key else 1
            last_key = key
            if repeats > self.MAX_REPEATS:
                return Result.fail(
                    f"Stessa azione ripetuta {self.MAX_REPEATS} volte ({data['action']}): mapping interrotto",
                    "MAP_STUCK")

            outcome = self._execute_gpt_action(device_id, data, screen)
            seen_texts.update(e.text.strip() for e in screen.elements if e.text.strip())
            target = data["target_text"] or data["target_label"] or ""
            if not outcome.success:
                if outcome.error_code not in ("ELEMENT_NOT_FOUND", "INVALID_ACTION"):
                    return Result.fail(outcome.error, outcome.error_code)
                history.append(f"{data['action']} {target} fallita: {outcome.error}")
                continue

            history.append(f"{data['action']} {target} eseguita")
            record = {k: data.get(k) for k in (
                "action", "target_text", "target_label", "x", "y", "description",
                "tapped_x", "tapped_y", "resolved_text")}
            record["changed"] = True
            record["_signature_before"] = ui_helpers.screen_signature(screen)
            executed.append(record)
            pending = record
            time.sleep(self.STEP_WAIT)

        return Result.fail(f"Login non completato dopo {self.MAX_STEPS} step", "MAP_MAX_STEPS")

    # --------------------------------------------------------
    # INTERNAL: step GPT-guided
    # --------------------------------------------------------

    def _gpt_guided_step(self, device_id: str, step_num: int,
                         history: Optional[list[str]] = None) -> Result[dict]:
        """Uno step: legge lo schermo, screenshot -> GPT -> azione.

        Ritorna dict con action, target_text, target_label, x, y, description, status
        (done | failed | continue) e screen_content (la schermata letta in questo step).
        Allo step 1, se il telefono e' gia' loggato, non invia nulla a OpenAI.
        """
        screen_res = self.screen_reader.read_screen(device_id)
        if not screen_res.success:
            return Result.fail(screen_res.error, screen_res.error_code or "SCREEN_READ_FAILED")
        screen = screen_res.data

        if step_num == 1 and ui_helpers.has_indicators(screen, DEFAULT_LOGGED_IN_INDICATORS) \
                and not ui_helpers.has_indicators(screen, LOGIN_BUTTON_INDICATORS):
            return Result.fail(
                "Sessione gia' attiva, niente da mappare (nessuno screenshot inviato). "
                "Fai logout o usa un profilo Chrome pulito", "ALREADY_LOGGED_IN")

        image = self._capture_base64(device_id)
        if not image.success:
            return Result.fail(image.error, image.error_code)

        reply = self._call_gpt_vision(image.data, self._build_prompt(screen, history))
        if not reply.success:
            return Result.fail(reply.error, reply.error_code)
        action = self._parse_action(reply.data)
        if not action.success:
            return Result.fail(action.error, action.error_code)
        return Result.ok({**action.data, "screen_content": screen})

    def _execute_gpt_action(self, device_id: str, action_data: dict,
                            screen_content: ScreenContent) -> Result[bool]:
        """Esegue l'azione decisa da GPT.

        tap_button: ULTIMO Button con target_text (fallback: elemento non-Button, es. il link "Accedi")
        tap_field: cerca la label, trova l'EditText associato per geometria, tap
        tap_coordinates: tap su x, y
        press_enter: adb shell input keyevent 66
        Registra in action_data le coordinate toccate (tapped_x/y) e, per un tap_coordinates che cade
        su un elemento con testo risolvibile in modo identico, resolved_text (device-independent).
        """
        action = action_data.get("action")

        if action == "tap_button":
            text = action_data.get("target_text")
            if not text:
                return Result.fail("tap_button senza target_text", "INVALID_ACTION")
            element = (self._find_last_button_by_text(screen_content, text)
                       or ui_helpers.find_element_by_text(screen_content, text))
            if element is None:
                return Result.fail(f"Pulsante '{text}' non trovato", "ELEMENT_NOT_FOUND")
            return self._tap_element(device_id, element, action_data)

        if action == "tap_field":
            label = action_data.get("target_label")
            if not label:
                return Result.fail("tap_field senza target_label", "INVALID_ACTION")
            element = ui_helpers.find_field_by_label(screen_content, label)
            if element is None:
                return Result.fail(f"Campo '{label}' non trovato", "ELEMENT_NOT_FOUND")
            return self._tap_element(device_id, element, action_data)

        if action == "tap_coordinates":
            x, y = action_data.get("x"), action_data.get("y")
            if not isinstance(x, int) or not isinstance(y, int):
                return Result.fail("tap_coordinates senza x e y interi", "INVALID_ACTION")
            tapped = self.touch_injector.tap_coordinates(device_id, x, y)
            if not tapped.success:
                return Result.fail(tapped.error, tapped.error_code)
            action_data["tapped_x"], action_data["tapped_y"] = x, y
            element = ui_helpers.element_at_point(screen_content, x, y)
            if element is not None and "@" not in element.text:
                resolved = ui_helpers.resolve_tap_target(screen_content, element.text.strip())
                if resolved is not None and resolved.bounds == element.bounds:
                    action_data["resolved_text"] = element.text.strip()
            return Result.ok(True)

        if action == "press_enter":
            return adb_input.press_enter(device_id)

        return Result.fail(f"Azione non eseguibile '{action}'", "INVALID_ACTION")

    def _find_last_button_by_text(self, screen_content: ScreenContent, text: str) -> Optional[UIElement]:
        """ULTIMO Button con testo matching (il submit sta in fondo al form, non nell'header)."""
        return ui_helpers.find_last_button_by_text(screen_content, text)

    def _tap_element(self, device_id: str, element: UIElement, action_data: dict) -> Result[bool]:
        center = ui_helpers.element_center(element.bounds)
        if center is None:
            return Result.fail(f"Bounds non validi per '{element.text}'", "ELEMENT_NOT_FOUND")
        tapped = self.touch_injector.tap_coordinates(device_id, center[0], center[1])
        if not tapped.success:
            return Result.fail(tapped.error, tapped.error_code)
        action_data["tapped_x"], action_data["tapped_y"] = center
        if "@" in element.text:  # suggerimento email di Chrome: il recipe non deve contenerla
            action_data["resolved_text"] = element.text.strip()
        return Result.ok(True)

    # --------------------------------------------------------
    # INTERNAL: recipe
    # --------------------------------------------------------

    def _generate_recipe_from_steps(
        self, book: str, login_url: str, steps: list[dict], logged_in_screen: Optional[ScreenContent],
        exclude_texts: Optional[set] = None, has_captcha: bool = False, captcha_type: Optional[str] = None,
    ) -> LoginRecipe:
        """Recipe pulito dagli step GPT eseguiti.

        - scarta gli step che non hanno cambiato lo schermo (changed=False)
        - collassa gli step consecutivi identici tenendo l'ultimo (quello che ha funzionato)
        - tap_coordinates dentro un elemento risolvibile -> tap_button (indipendente dalla risoluzione)
        - testo con '@' (suggerimento email di Chrome) -> tap_coordinates: il recipe e' per book e
          non deve contenere l'email dell'identita' usata; idem per le description
        - tap_field con trigger_autofill=False: con popup/barra tastiera il campo si riempie solo
          dopo il tap sul suggerimento; l'esito lo dice verify_login
        """
        kept = [s for s in steps if s.get("changed", True)]
        collapsed: list[dict] = []
        for s in kept:
            if collapsed and self._step_key(collapsed[-1]) == self._step_key(s):
                collapsed[-1] = s
            else:
                collapsed.append(s)

        login_steps = [step for step in (self._to_login_step(s) for s in collapsed) if step is not None]
        return LoginRecipe(
            book=book,
            login_url=login_url,
            steps=login_steps,
            has_captcha=has_captcha,
            captcha_type=captcha_type if has_captcha else None,
            logged_in_indicators=self._final_indicators(logged_in_screen, exclude_texts or set()),
            login_failed_indicators=list(DEFAULT_LOGIN_FAILED_INDICATORS),
            mapped_at=datetime.now().isoformat(timespec="seconds"),
            mapped_by="gpt_vision",
        )

    @staticmethod
    def _step_key(step: dict) -> tuple:
        return (step.get("action"), (step.get("target_text") or "").strip().lower(),
                (step.get("target_label") or "").strip().lower(), step.get("x"), step.get("y"))

    def _to_login_step(self, s: dict) -> Optional[LoginStep]:
        action = s.get("action")
        description = _EMAIL.sub("<email>", (s.get("description") or "").strip())

        if action == "tap_field":
            return LoginStep("tap_field", target_label=s.get("target_label"), trigger_autofill=False,
                             wait_after_seconds=self.FIELD_WAIT,
                             description=description or f"Tap sul campo {s.get('target_label')}")
        if action == "press_enter":
            return LoginStep("press_enter", wait_after_seconds=self.ENTER_WAIT,
                             description=description or "Premi Enter")

        text = s.get("target_text") if action == "tap_button" else s.get("resolved_text")
        has_email = "@" in (s.get("target_text") or "") or "@" in (s.get("resolved_text") or "")
        if action in ("tap_button", "tap_coordinates") and text and not has_email:
            return LoginStep("tap_button", target_text=text, wait_after_seconds=self.TAP_WAIT,
                             description=description or f"Tap su {text}")

        # tap_coordinates non risolvibile, oppure testo con email: posizione assoluta
        x = s.get("tapped_x") if s.get("tapped_x") is not None else s.get("x")
        y = s.get("tapped_y") if s.get("tapped_y") is not None else s.get("y")
        if x is None or y is None:
            return None
        return LoginStep("tap_coordinates", x=x, y=y, wait_after_seconds=self.TAP_WAIT,
                         description=description or "Tap sulla posizione del suggerimento/pulsante")

    def _final_indicators(self, screen: Optional[ScreenContent], exclude_texts: set) -> list[str]:
        """Indicatori di sessione attiva dalla schermata finale: i default presenti piu' qualche testo
        breve assente dalle schermate precedenti. Mai saldo, nome utente o email."""
        if screen is None:
            return list(DEFAULT_LOGGED_IN_INDICATORS)
        found = [d for d in DEFAULT_LOGGED_IN_INDICATORS if ui_helpers.has_indicators(screen, [d])]
        taken = {t.strip().lower() for t in exclude_texts} | {f.lower() for f in found}
        extras: list[str] = []
        for e in screen.elements:
            text = e.text.strip()
            if (e.class_name == ui_helpers.EDIT_TEXT_CLASS or not 3 <= len(text) <= 30
                    or any(c.isdigit() for c in text) or "@" in text or re.search(r"[€$£]", text)
                    or text.lower() in taken):
                continue
            extras.append(text)
            taken.add(text.lower())
            if len(extras) >= self.MAX_EXTRA_INDICATORS:
                break
        return (found + extras) or list(DEFAULT_LOGGED_IN_INDICATORS)

    # --------------------------------------------------------
    # INTERNAL: GPT
    # --------------------------------------------------------

    def _build_prompt(self, screen: ScreenContent, history: Optional[list[str]]) -> str:
        prompt = GUIDED_PROMPT.replace("{elementi_ui}", self._format_ui_elements(screen))
        if history:
            recent = "\n".join(f"- {h}" for h in history[-self.MAX_HISTORY:])
            prompt += f"\n\nAzioni già eseguite (le più recenti):\n{recent}"
        return prompt

    def _format_ui_elements(self, screen: ScreenContent) -> str:
        lines = []
        for i, e in enumerate(screen.elements[:self.MAX_UI_ELEMENTS]):
            cls = (e.class_name or "?").rsplit(".", 1)[-1]
            text = e.text.replace("\n", " ")[:self.MAX_UI_TEXT]
            lines.append(f"[{i}] {cls}, text='{text}', bounds={e.bounds}, resource_id='{e.resource_id or ''}'")
        return "\n".join(lines) or "(nessun elemento letto)"

    @staticmethod
    def _parse_action(text: str) -> Result[dict]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
        try:
            raw = json.loads(cleaned)
        except ValueError:
            return Result.fail("Risposta GPT Vision non e' JSON valido", "GPT_VISION_PARSE_ERROR")
        if not isinstance(raw, dict) or raw.get("action") not in GPT_ACTIONS:
            return Result.fail("Risposta GPT Vision senza un'azione valida", "GPT_VISION_PARSE_ERROR")

        def as_text(value) -> Optional[str]:
            return str(value).strip() or None if value else None

        def as_int(value) -> Optional[int]:
            try:
                return int(float(value)) if value is not None and not isinstance(value, bool) else None
            except (TypeError, ValueError):
                return None

        action = raw["action"]
        return Result.ok({
            "action": action,
            "target_text": as_text(raw.get("target_text")),
            "target_label": as_text(raw.get("target_label")),
            "x": as_int(raw.get("x")),
            "y": as_int(raw.get("y")),
            "description": str(raw.get("description") or ""),
            "status": "done" if action == "done" else "failed" if action == "login_failed" else "continue",
        })

    def _call_gpt_vision(self, image_base64: str, prompt: str) -> Result[str]:
        """Chiama OpenAI (SDK, come il resto del repo) con immagine + prompt, ritorna il testo."""
        try:
            client = openai.OpenAI(api_key=self.openai_api_key, timeout=self.GPT_TIMEOUT)
            kwargs = dict(
                model=self.model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
                ]}],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=self.GPT_MAX_TOKENS,
            )
            response = call_with_rate_limit_retry(
                lambda: create_with_param_fallback(client.chat.completions.create, kwargs))
        except openai.APITimeoutError:
            return Result.fail("Timeout chiamando GPT Vision", "GPT_VISION_TIMEOUT")
        except openai.APIConnectionError:
            return Result.fail("GPT Vision non raggiungibile", "GPT_VISION_UNREACHABLE")
        except openai.APIStatusError as e:
            # Solo lo status: il messaggio dell'API puo' citare parte della chiave.
            return Result.fail(f"GPT Vision ha risposto con errore HTTP {e.status_code}", "GPT_VISION_HTTP_ERROR")
        except openai.OpenAIError as e:
            return Result.fail(f"Errore OpenAI ({type(e).__name__})", "GPT_VISION_UNREACHABLE")

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            content = None
        if not content or not str(content).strip():
            return Result.fail("Risposta GPT Vision vuota", "GPT_VISION_EMPTY")
        return Result.ok(str(content))

    @staticmethod
    def _image_to_base64(image_path: str) -> str:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")

    def _take_screenshot(self, device_id: str) -> Result[str]:
        try:
            return self.screen_reader._screenshot(device_id)
        except Exception as e:
            return Result.fail(f"Screenshot fallito: {e}", "SCREENSHOT_FAILED")

    def _capture_base64(self, device_id: str) -> Result[str]:
        """Screenshot -> base64; il PNG locale viene cancellato subito (privacy), anche su errore."""
        shot = self._take_screenshot(device_id)
        if not shot.success:
            return Result.fail(shot.error, shot.error_code)
        try:
            return Result.ok(self._image_to_base64(shot.data))
        except OSError as e:
            return Result.fail(f"Screenshot illeggibile: {e}", "GPT_VISION_IMAGE_READ_ERROR")
        finally:
            try:
                os.remove(shot.data)
            except OSError:
                pass
