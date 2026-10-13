import base64
import difflib
import json
import os
import time
from datetime import datetime
from typing import Optional

import openai

from orchestrator.config.book_navigation import BOOK_NAVIGATION
from orchestrator.core.openai_compat import create_with_param_fallback
from orchestrator.core.openai_retry import call_with_rate_limit_retry
from orchestrator.core.result import Result
from orchestrator.modules.book_navigator.navigator import BookNavigator
from orchestrator.modules.login_manager import ui_helpers
from orchestrator.modules.login_manager.recipe import (
    DEFAULT_LOGGED_IN_INDICATORS,
    DEFAULT_LOGIN_FAILED_INDICATORS,
    LOGIN_BUTTON_INDICATORS,
    LoginRecipe,
    LoginStep,
    RecipeStore,
)
from orchestrator.modules.screen_reader.interface import ScreenContent

PAGE_TYPES = ("home_not_logged_in", "login_form", "logged_in", "unknown")

ANALYSIS_PROMPT = """Questo e' uno screenshot della pagina del bookmaker [BOOK] su un telefono Android.
Analizza il layout di login e rispondi SOLO con un oggetto JSON in questo formato:
{
  "page_type": "home_not_logged_in" | "login_form" | "logged_in" | "unknown",
  "has_login_button": true/false,
  "login_button_text": "testo esatto del pulsante o link per andare al login, oppure null",
  "has_form": true/false,
  "form_fields": [
    {"label": "testo esatto della label come scritto a schermo", "type": "text" | "password"}
  ],
  "submit_button_text": "testo esatto del pulsante di invio del form, oppure null",
  "has_captcha": true/false,
  "captcha_type": "reCAPTCHA o altro, oppure null",
  "has_2fa": true/false,
  "notes": "osservazioni aggiuntive"
}
Regole: "login_form" solo se sono visibili i campi di login (email/username, password).
"home_not_logged_in" se non sei loggato e il form non e' visibile ma c'e' un pulsante o link
per accedere. "logged_in" se la pagina mostra un utente gia' loggato. Copia i testi
ESATTAMENTE come appaiono a schermo (stessa lingua, maiuscole e punteggiatura)."""


class LoginMapper:
    """Mappa il flusso di login di un book con GPT Vision e salva il LoginRecipe.

    Da eseguire una volta per book (il flusso e' lo stesso per tutte le identita').
    Richiede OPENAI_API_KEY. Gli screenshot (PNG locali) vengono inviati a OpenAI e
    cancellati subito dopo; se la sessione risulta gia' attiva non viene inviato nulla.
    Il recipe generato NON viene provato qui: la prima esecuzione del LoginExecutor
    su telefono e' la validazione.
    """

    PAGE_LOAD_WAIT = 3
    NAV_WAIT = 5
    FIELD_WAIT = 3
    SUBMIT_WAIT = 5
    GPT_TIMEOUT = 60
    GPT_MAX_TOKENS = 800
    SNAP_CUTOFF = 0.75

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
        """Mappa il flusso di login completo di un book e salva il recipe.

        1. Apre il book (BookNavigator.open_chrome) e controlla che NON sia gia' loggato
        2. Screenshot -> GPT Vision
        3. Se e' la home con un link "Accedi": tap, attesa, secondo screenshot -> GPT Vision
        4. Aggancia le label di GPT ai testi reali letti da ScreenReader
        5. Genera e salva il recipe
        """
        if not self.openai_api_key:
            return Result.fail("OPENAI_API_KEY non configurata", "OPENAI_API_KEY_MISSING")
        book_key = book.strip().upper()
        cfg = BOOK_NAVIGATION.get(book_key, {})
        url = login_url or cfg.get("login_url") or cfg.get("balance_url")
        if not url:
            return Result.fail(f"Nessun URL di partenza per {book_key}", "NO_LOGIN_URL")

        screenshots: list[str] = []
        try:
            opened = self.book_navigator.open_chrome(device_id, url)
            if not opened.success:
                return Result.fail(opened.error or "Apertura Chrome fallita", "NAVIGATION_FAILED")
            time.sleep(self.PAGE_LOAD_WAIT)

            page1 = self._capture_and_analyze(device_id, book_key, screenshots)
            if not page1.success:
                return Result.fail(page1.error, page1.error_code)
            analysis1, screen1 = page1.data
            page_type = analysis1["page_type"]

            if page_type == "login_form":
                snapped = self._snap_analysis(analysis1, screen1, home=False)
                if not snapped.success:
                    return Result.fail(snapped.error, snapped.error_code)
                analyses = [snapped.data]

            elif page_type == "home_not_logged_in" and analysis1["has_login_button"] \
                    and analysis1["login_button_text"]:
                home = self._snap_analysis(analysis1, screen1, home=True)
                if not home.success:
                    return Result.fail(home.error, home.error_code)
                button = ui_helpers.find_element_by_text(screen1, home.data["login_button_text"])
                center = ui_helpers.element_center(button.bounds) if button else None
                if center is None:
                    return Result.fail("Pulsante di login non trovato sulla schermata", "MAP_LABEL_NOT_FOUND")
                tapped = self.touch_injector.tap_coordinates(device_id, center[0], center[1])
                if not tapped.success:
                    return Result.fail(tapped.error, tapped.error_code)
                time.sleep(self.NAV_WAIT)

                page2 = self._capture_and_analyze(device_id, book_key, screenshots)
                if not page2.success:
                    return Result.fail(page2.error, page2.error_code)
                analysis2, screen2 = page2.data
                if analysis2["page_type"] != "login_form":
                    return Result.fail(
                        f"Dopo il tap su '{home.data['login_button_text']}' la pagina non e' un form di "
                        f"login (page_type={analysis2['page_type']})", "MAP_FAILED")
                form = self._snap_analysis(analysis2, screen2, home=False)
                if not form.success:
                    return Result.fail(form.error, form.error_code)
                analyses = [home.data, form.data]

            else:
                return Result.fail(f"Pagina non mappabile (page_type={page_type})", "MAP_FAILED")

            generated = self._generate_recipe(analyses, book_key, url)
            if not generated.success:
                return generated
            saved = self.recipe_store.save_recipe(generated.data)
            if not saved.success:
                return Result.fail(saved.error, saved.error_code)
            return generated
        finally:
            for path in screenshots:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # --------------------------------------------------------
    # INTERNAL: GPT
    # --------------------------------------------------------

    def _analyze_screenshot(self, image_path: str, book: str) -> Result[dict]:
        """Manda lo screenshot a GPT Vision e riceve l'analisi (dict normalizzato)."""
        if not self.openai_api_key:
            return Result.fail("OPENAI_API_KEY non configurata", "OPENAI_API_KEY_MISSING")
        try:
            image_base64 = self._image_to_base64(image_path)
        except OSError as e:
            return Result.fail(f"Screenshot illeggibile: {e}", "GPT_VISION_IMAGE_READ_ERROR")

        reply = self._call_gpt_vision(image_base64, ANALYSIS_PROMPT.replace("[BOOK]", book))
        if not reply.success:
            return Result.fail(reply.error, reply.error_code)
        return self._parse_analysis(reply.data)

    @staticmethod
    def _parse_analysis(text: str) -> Result[dict]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
        try:
            raw = json.loads(cleaned)
        except ValueError:
            return Result.fail("Risposta GPT Vision non e' JSON valido", "GPT_VISION_PARSE_ERROR")
        if not isinstance(raw, dict):
            return Result.fail("Risposta GPT Vision non e' un oggetto JSON", "GPT_VISION_PARSE_ERROR")

        fields = []
        for item in raw.get("form_fields") or []:
            if isinstance(item, dict) and str(item.get("label") or "").strip():
                fields.append({"label": str(item["label"]).strip(), "type": str(item.get("type") or "text")})
        page_type = raw.get("page_type")
        return Result.ok({
            "page_type": page_type if page_type in PAGE_TYPES else "unknown",
            "has_login_button": bool(raw.get("has_login_button", False)),
            "login_button_text": (str(raw["login_button_text"]).strip() or None)
            if raw.get("login_button_text") else None,
            "has_form": bool(raw.get("has_form", False)),
            "form_fields": fields,
            "submit_button_text": (str(raw["submit_button_text"]).strip() or None)
            if raw.get("submit_button_text") else None,
            "has_captcha": bool(raw.get("has_captcha", False)),
            "captcha_type": raw.get("captcha_type") or None,
            "has_2fa": bool(raw.get("has_2fa", False)),
            "notes": str(raw.get("notes") or ""),
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
            # Solo tipo e status: il messaggio dell'API puo' citare parte della chiave.
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

    # --------------------------------------------------------
    # INTERNAL: capture / snapping
    # --------------------------------------------------------

    def _take_screenshot(self, device_id: str) -> Result[str]:
        try:
            return self.screen_reader._screenshot(device_id)
        except Exception as e:
            return Result.fail(f"Screenshot fallito: {e}", "SCREENSHOT_FAILED")

    def _capture_and_analyze(self, device_id: str, book_key: str, screenshots: list[str]):
        """Legge lo schermo, controlla che non sia gia' loggato (prima di inviare qualunque
        cosa a OpenAI), poi screenshot + analisi. Ok((analisi, ScreenContent))."""
        screen_res = self.screen_reader.read_screen(device_id)
        if not screen_res.success:
            return Result.fail(screen_res.error, screen_res.error_code or "SCREEN_READ_FAILED")
        screen = screen_res.data
        if (ui_helpers.has_indicators(screen, DEFAULT_LOGGED_IN_INDICATORS)
                and not ui_helpers.has_indicators(screen, LOGIN_BUTTON_INDICATORS)):
            return Result.fail(
                f"{book_key}: sessione gia' attiva, niente da mappare (nessuno screenshot inviato). "
                "Fai logout o usa un profilo Chrome pulito", "ALREADY_LOGGED_IN")

        shot = self._take_screenshot(device_id)
        if not shot.success:
            return Result.fail(shot.error, shot.error_code)
        screenshots.append(shot.data)

        analysis = self._analyze_screenshot(shot.data, book_key)
        if not analysis.success:
            return Result.fail(analysis.error, analysis.error_code)
        return Result.ok((analysis.data, screen))

    def _snap_text(self, text: str, screen_content: ScreenContent) -> Optional[str]:
        """Testo reale della schermata piu' vicino a quello scritto da GPT.

        GPT parafrasa ("Indirizzo email") mentre uiautomator vede "Email / Username": il
        recipe deve contenere il testo che l'Executor trovera' davvero. Ordine: identico,
        parola intera (il testo reale piu' corto che contiene il nostro, poi il piu' lungo
        contenuto nel nostro), infine somiglianza difflib >= SNAP_CUTOFF.
        """
        wanted = text.strip()
        texts = list(dict.fromkeys(e.text.strip() for e in screen_content.elements if e.text.strip()))
        lowered = {t.lower(): t for t in texts}
        if wanted.lower() in lowered:
            return lowered[wanted.lower()]

        containing = [t for t in texts if ui_helpers.word_in_text(wanted, t)]
        if containing:
            return min(containing, key=len)
        contained = [t for t in texts if len(t) >= 3 and ui_helpers.word_in_text(t, wanted)]
        if contained:
            return max(contained, key=len)

        close = difflib.get_close_matches(wanted.lower(), list(lowered), n=1, cutoff=self.SNAP_CUTOFF)
        return lowered[close[0]] if close else None

    def _snap_analysis(self, analysis: dict, screen_content: ScreenContent, home: bool) -> Result[dict]:
        """Copia dell'analisi con label e testi pulsante sostituiti dai testi reali."""
        snapped = dict(analysis)

        def snap(text: str, what: str) -> Result[str]:
            real = self._snap_text(text, screen_content)
            if real is None:
                return Result.fail(f"{what} '{text}' non trovato nella schermata letta da ScreenReader",
                                   "MAP_LABEL_NOT_FOUND")
            return Result.ok(real)

        if home:
            res = snap(analysis["login_button_text"], "Pulsante di login")
            if not res.success:
                return res
            snapped["login_button_text"] = res.data
            return Result.ok(snapped)

        snapped["form_fields"] = []
        for item in analysis["form_fields"]:
            res = snap(item["label"], "Campo")
            if not res.success:
                return res
            snapped["form_fields"].append({**item, "label": res.data})
        if analysis["submit_button_text"]:
            res = snap(analysis["submit_button_text"], "Pulsante di invio")
            if not res.success:
                return res
            snapped["submit_button_text"] = res.data
        return Result.ok(snapped)

    # --------------------------------------------------------
    # INTERNAL: recipe
    # --------------------------------------------------------

    def _generate_recipe(self, analyses: list[dict], book: str, login_url: str) -> Result[LoginRecipe]:
        """Recipe dalle analisi GPT.

        [home_not_logged_in, login_form]: tap_button login + tap_field per campo + tap_button submit.
        [login_form]: senza il primo step.
        """
        if not analyses:
            return Result.fail("Nessuna analisi da cui generare il recipe", "MAP_FAILED")
        form = analyses[-1]
        if form["page_type"] != "login_form":
            return Result.fail(f"Manca l'analisi del form di login (page_type={form['page_type']})", "MAP_FAILED")

        steps: list[LoginStep] = []
        if len(analyses) > 1:
            home = analyses[0]
            if not (home["page_type"] == "home_not_logged_in" and home["login_button_text"]):
                return Result.fail("La home non ha un pulsante di login identificato", "MAP_FAILED")
            steps.append(LoginStep(action="tap_button", target_text=home["login_button_text"],
                                   wait_after_seconds=self.NAV_WAIT,
                                   description="Naviga alla pagina di login"))

        if not form["form_fields"]:
            return Result.fail("Il form di login non ha campi identificati", "MAP_FAILED")
        for item in form["form_fields"]:
            steps.append(LoginStep(action="tap_field", target_label=item["label"], trigger_autofill=True,
                                   wait_after_seconds=self.FIELD_WAIT,
                                   description=f"Tap sul campo {item['label']} per triggerare autofill Chrome"))

        if not form["submit_button_text"]:
            return Result.fail("Il pulsante di invio del form non e' identificato", "MAP_FAILED")
        steps.append(LoginStep(action="tap_button", target_text=form["submit_button_text"],
                               wait_after_seconds=self.SUBMIT_WAIT, description="Submit form di login"))

        captcha_type = next((a["captcha_type"] for a in analyses if a.get("captcha_type")), None)
        return Result.ok(LoginRecipe(
            book=book,
            login_url=login_url,
            steps=steps,
            has_captcha=any(a.get("has_captcha") for a in analyses),
            captcha_type=captcha_type,
            logged_in_indicators=list(DEFAULT_LOGGED_IN_INDICATORS),
            login_failed_indicators=list(DEFAULT_LOGIN_FAILED_INDICATORS),
            mapped_at=datetime.now().isoformat(timespec="seconds"),
            mapped_by="gpt_vision",
        ))
