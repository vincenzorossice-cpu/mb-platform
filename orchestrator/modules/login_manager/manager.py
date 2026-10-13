import re
import time
from typing import Optional

from orchestrator.config.book_navigation import BOOK_NAVIGATION
from orchestrator.core.result import Result
from orchestrator.modules.book_navigator.navigator import BookNavigator
from orchestrator.modules.login_manager.interface import (
    LoginManagerInterface,
    LoginResult,
    LoginStatus,
)
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement

EDIT_TEXT_CLASS = "android.widget.EditText"

DEFAULT_BOOK_CONFIG = {
    "needs_manual_login": False,
    "login_url": None,
    "balance_url": None,
    "login_form_labels": {
        "username_label": "Email",
        "password_label": "Password",
        "submit_label": "Accedi",
    },
    "logged_in_indicators": [],
}

USERNAME_WORDS = ["email", "e-mail", "username", "nome utente", "utente"]
PASSWORD_WORDS = ["password", "pin"]
SUBMIT_WORDS = ["accedi", "login", "log in", "entra", "continua"]


class LoginManager(LoginManagerInterface):
    """Login ai book via autofill nativo di Chrome.

    Non scrive MAI password: fa solo tap sui campi (Chrome riempie) e sul
    pulsante di invio. Un solo tentativo di submit per chiamata: nessun retry
    a livello di business logic, per non rischiare il blocco dell'account.
    Limite noto: ScreenReader scarta i nodi senza text e senza resource-id,
    quindi un EditText vuoto e anonimo non e' visibile.
    """

    PAGE_LOAD_WAIT = 3
    AUTOFILL_WAIT = 2
    POST_LOGIN_WAIT = 5

    # Etichette comuni per riconoscere form di login
    LOGIN_INDICATORS = [
        "Accedi", "Registrati", "Login", "Log in", "Sign in",
        "Username", "Email", "Password", "Nome utente", "PIN",
    ]

    LOGGED_IN_INDICATORS = [
        "Il mio conto", "Profilo", "Logout", "Esci", "Saldo",
        "Disponibile", "Benvenuto", "Account", "Dashboard",
    ]

    CAPTCHA_INDICATORS = [
        "Non sono un robot", "reCAPTCHA", "captcha",
        "Verifica che non sei un robot",
    ]

    TWO_FA_INDICATORS = [
        "inserisci codice", "inserisci il codice", "codice di verifica",
        "codice SMS", "2FA", "autenticazione a due fattori",
    ]

    LOGIN_FAILED_INDICATORS = [
        "credenziali errate", "password non valida", "riprova",
        "accesso negato", "login errato", "dati non corretti",
    ]

    def __init__(
        self,
        screen_reader,
        touch_injector,
        device_manager=None,
        book_navigator: Optional[BookNavigator] = None,
    ):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.device_manager = device_manager
        self.book_navigator = book_navigator or BookNavigator(screen_reader, touch_injector)

    # --------------------------------------------------------
    # PUBLIC
    # --------------------------------------------------------

    def check_session(self, device_id: str, book: str) -> Result[LoginResult]:
        res = self._check_session_screen(device_id, book)
        if not res.success:
            return Result.fail(res.error, res.error_code)
        return Result.ok(res.data[0])

    def perform_login(self, device_id: str, book: str) -> Result[LoginResult]:
        book_key = self._book_key(book)
        cfg = self._get_book_config(book_key)

        if cfg["needs_manual_login"]:
            return Result.ok(LoginResult(
                LoginStatus.MANUAL_LOGIN_REQUIRED, book_key, device_id,
                f"{book_key}: login manuale via scrcpy richiesto",
            ))

        if self.device_manager is not None:
            if not self.device_manager.acquire_lock(device_id, f"login:{book_key}"):
                return Result.fail(f"Device {device_id} occupato", "DEVICE_BUSY")
        try:
            return self._perform_login_locked(device_id, book_key)
        finally:
            if self.device_manager is not None:
                self.device_manager.release_lock(device_id)

    def verify_login(self, device_id: str, book: str) -> Result[LoginResult]:
        book_key = self._book_key(book)
        cfg = self._get_book_config(book_key)
        screen_res = self.screen_reader.read_screen(device_id)
        if not screen_res.success:
            return Result.fail(screen_res.error, screen_res.error_code or "SCREEN_READ_FAILED")
        screen = screen_res.data

        if self._detect_captcha(screen):
            return self._hitl(LoginStatus.CAPTCHA_DETECTED, book_key, device_id, "CAPTCHA rilevato")
        if self._detect_2fa(screen):
            return self._hitl(LoginStatus.TWO_FA_REQUIRED, book_key, device_id, "2FA richiesta")
        if self._has_indicators(screen, self.LOGIN_FAILED_INDICATORS):
            return self._hitl(LoginStatus.LOGIN_FAILED, book_key, device_id,
                              "Credenziali errate o login rifiutato")
        if self._has_indicators(screen, self._logged_in_indicators(cfg)):
            return Result.ok(LoginResult(LoginStatus.LOGIN_SUCCESS, book_key, device_id,
                                         "Login riuscito"))
        return self._hitl(LoginStatus.AMBIGUOUS, book_key, device_id,
                          "Esito del login non determinabile")

    # --------------------------------------------------------
    # INTERNAL: flow
    # --------------------------------------------------------

    def _perform_login_locked(self, device_id: str, book_key: str) -> Result[LoginResult]:
        res = self._check_session_screen(device_id, book_key)
        if not res.success:
            return Result.fail(res.error, res.error_code)
        session, screen = res.data
        if session.status != LoginStatus.LOGIN_REQUIRED:
            return Result.ok(session)  # LOGGED_IN, CAPTCHA, 2FA, AMBIGUOUS, ...

        outcome = self._do_autofill_login(device_id, book_key, screen)
        if not outcome.success:
            return outcome
        if outcome.data is not None:
            return outcome  # esito terminale (autofill fallito, CAPTCHA, 2FA, ...)
        return self.verify_login(device_id, book_key)

    def _check_session_screen(self, device_id: str, book: str):
        """Come check_session ma restituisce (LoginResult, ScreenContent)."""
        book_key = self._book_key(book)
        cfg = self._get_book_config(book_key)

        url = cfg.get("login_url") or cfg.get("balance_url")
        if not url:
            return Result.ok((LoginResult(
                LoginStatus.NAVIGATION_FAILED, book_key, device_id,
                f"Nessun URL configurato per {book_key}"), None))

        opened = self._open_chrome_url(device_id, url)
        if not opened.success:
            return Result.ok((LoginResult(
                LoginStatus.NAVIGATION_FAILED, book_key, device_id,
                opened.error or "Apertura Chrome fallita"), None))
        time.sleep(self.PAGE_LOAD_WAIT)

        screen_res = self.screen_reader.read_screen(device_id)
        if not screen_res.success:
            return Result.fail(screen_res.error, screen_res.error_code or "SCREEN_READ_FAILED")
        screen = screen_res.data

        if self._detect_captcha(screen):
            status, msg = LoginStatus.CAPTCHA_DETECTED, "CAPTCHA rilevato"
        elif self._detect_2fa(screen):
            status, msg = LoginStatus.TWO_FA_REQUIRED, "2FA richiesta"
        elif self._has_indicators(screen, self._logged_in_indicators(cfg)):
            return Result.ok((LoginResult(LoginStatus.LOGGED_IN, book_key, device_id,
                                          "Sessione gia' attiva"), screen))
        elif self._has_indicators(screen, self.LOGIN_INDICATORS):
            return Result.ok((LoginResult(LoginStatus.LOGIN_REQUIRED, book_key, device_id,
                                          "Sessione assente: serve login"), screen))
        else:
            status, msg = LoginStatus.AMBIGUOUS, "Stato sessione non determinabile"

        hitl = self._hitl(status, book_key, device_id, msg)
        return Result.ok((hitl.data, screen))

    def _do_autofill_login(self, device_id: str, book: str,
                           screen_content: ScreenContent) -> Result[Optional[LoginResult]]:
        """Login via autofill. data=None: submit eseguito, resta da verificare l'esito;
        data=LoginResult: esito terminale (HITL)."""
        book_key = self._book_key(book)
        cfg = self._get_book_config(book_key)
        form = self._find_login_form_elements(screen_content, cfg)
        if not form["username_element"] and not form["password_element"]:
            return self._hitl(LoginStatus.AMBIGUOUS, book_key, device_id,
                              "Form di login non trovato")

        step = self._fill_and_submit(device_id, book_key, form)
        if not step.success or step.data is not None:
            return step

        # Login a due step: dopo il primo submit compare solo il campo password.
        screen_res = self.screen_reader.read_screen(device_id)
        if not screen_res.success:
            return Result.fail(screen_res.error, screen_res.error_code or "SCREEN_READ_FAILED")
        screen = screen_res.data
        if self._detect_captcha(screen):
            return self._hitl(LoginStatus.CAPTCHA_DETECTED, book_key, device_id, "CAPTCHA rilevato")
        if self._detect_2fa(screen):
            return self._hitl(LoginStatus.TWO_FA_REQUIRED, book_key, device_id, "2FA richiesta")
        if form["password_element"] is None:
            form2 = self._find_login_form_elements(screen, cfg)
            if form2["password_element"] is not None and form2["username_element"] is None:
                return self._fill_and_submit(device_id, book_key, form2)
        return Result.ok(None)

    def _fill_and_submit(self, device_id: str, book_key: str, form: dict) -> Result[Optional[LoginResult]]:
        """Tap sui campi (autofill), tap sul submit, attesa post-login e check CAPTCHA/2FA."""
        for key in ("username_element", "password_element"):
            element = form[key]
            if element is None:
                continue
            tapped = self._tap_element(device_id, element)
            if not tapped.success:
                return Result.fail(tapped.error, tapped.error_code)
            time.sleep(self.AUTOFILL_WAIT)
            screen_res = self.screen_reader.read_screen(device_id)
            if not screen_res.success:
                return Result.fail(screen_res.error, screen_res.error_code or "SCREEN_READ_FAILED")
            if not self._is_field_filled(screen_res.data, element):
                return self._hitl(LoginStatus.AUTOFILL_FAILED, book_key, device_id,
                                  f"Chrome non ha riempito il campo ({key})")

        submit = form["submit_element"]
        if submit is None:
            return self._hitl(LoginStatus.AMBIGUOUS, book_key, device_id,
                              "Pulsante di invio non trovato")
        tapped = self._tap_element(device_id, submit)
        if not tapped.success:
            return Result.fail(tapped.error, tapped.error_code)
        time.sleep(self.POST_LOGIN_WAIT)

        screen_res = self.screen_reader.read_screen(device_id)
        if not screen_res.success:
            return Result.fail(screen_res.error, screen_res.error_code or "SCREEN_READ_FAILED")
        if self._detect_captcha(screen_res.data):
            return self._hitl(LoginStatus.CAPTCHA_DETECTED, book_key, device_id, "CAPTCHA rilevato")
        if self._detect_2fa(screen_res.data):
            return self._hitl(LoginStatus.TWO_FA_REQUIRED, book_key, device_id, "2FA richiesta")
        return Result.ok(None)

    # --------------------------------------------------------
    # INTERNAL: screen analysis
    # --------------------------------------------------------

    def _find_login_form_elements(self, screen_content: ScreenContent, cfg: Optional[dict] = None) -> dict:
        labels = (cfg or DEFAULT_BOOK_CONFIG)["login_form_labels"]
        user_words = USERNAME_WORDS + [labels["username_label"].lower()]
        pass_words = PASSWORD_WORDS + [labels["password_label"].lower()]
        submit_words = SUBMIT_WORDS + [labels["submit_label"].lower()]

        elements = screen_content.elements
        edits = [e for e in elements if e.class_name == EDIT_TEXT_CLASS]

        def match(e: UIElement, words: list[str]) -> bool:
            blob = f"{e.text} {e.resource_id or ''}".lower()
            return any(w in blob for w in words)

        username = next((e for e in edits if match(e, user_words)), None)
        password = next((e for e in edits if e is not username and match(e, pass_words)), None)
        if username is None and password is None and edits:
            username = edits[0]
        if password is None and username is not None and len(edits) > 1:
            password = next((e for e in edits if e is not username), None)

        submit_candidates = [e for e in elements
                             if e.class_name != EDIT_TEXT_CLASS
                             and e.text.strip().lower() in submit_words]
        buttons = [e for e in submit_candidates if e.class_name and "Button" in e.class_name]
        submit = (buttons or submit_candidates or [None])[-1]

        return {"username_element": username, "password_element": password,
                "submit_element": submit}

    def _is_field_filled(self, screen_content: ScreenContent, element: UIElement) -> bool:
        """Il campo (stessi bounds o stesso resource_id) ora ha un testo diverso da quello di prima."""
        before = element.text.strip().lower()
        for e in screen_content.elements:
            if e.class_name != EDIT_TEXT_CLASS:
                continue
            same = e.bounds == element.bounds or (
                element.resource_id and e.resource_id == element.resource_id)
            if same:
                now = e.text.strip().lower()
                return bool(now) and now != before
        return False

    def _has_indicators(self, screen_content: ScreenContent, indicators: list[str]) -> bool:
        if screen_content is None:
            return False
        for ind in indicators:
            pattern = re.compile(r"(?<!\w)" + re.escape(ind.lower()) + r"(?!\w)")
            if any(pattern.search(e.text.lower()) for e in screen_content.elements):
                return True
        return False

    def _detect_captcha(self, screen_content: ScreenContent) -> bool:
        return self._has_indicators(screen_content, self.CAPTCHA_INDICATORS)

    def _detect_2fa(self, screen_content: ScreenContent) -> bool:
        return self._has_indicators(screen_content, self.TWO_FA_INDICATORS)

    # --------------------------------------------------------
    # INTERNAL: device / config helpers
    # --------------------------------------------------------

    def _open_chrome_url(self, device_id: str, url: str) -> Result[bool]:
        return self.book_navigator.open_chrome(device_id, url)

    def _take_screenshot(self, device_id: str) -> Result[str]:
        try:
            return self.screen_reader._screenshot(device_id)
        except Exception as e:
            return Result.fail(f"Screenshot fallito: {e}", "SCREENSHOT_FAILED")

    def _tap_element(self, device_id: str, element: UIElement) -> Result[bool]:
        if not element.bounds or len(element.bounds) != 4 or not any(element.bounds):
            return Result.fail(f"Bounds non validi per '{element.text}'", "INVALID_BOUNDS")
        x1, y1, x2, y2 = element.bounds
        return self.touch_injector.tap_coordinates(device_id, (x1 + x2) // 2, (y1 + y2) // 2)

    def _get_book_config(self, book: str) -> dict:
        cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
               for k, v in DEFAULT_BOOK_CONFIG.items()}
        cfg.update(BOOK_NAVIGATION.get(self._book_key(book), {}))
        return cfg

    def _logged_in_indicators(self, cfg: dict) -> list[str]:
        return list(cfg.get("logged_in_indicators") or []) + self.LOGGED_IN_INDICATORS

    @staticmethod
    def _book_key(book: str) -> str:
        return book.strip().upper()

    def _hitl(self, status: LoginStatus, book: str, device_id: str, message: str) -> Result[LoginResult]:
        """LoginResult per intervento umano, con screenshot se disponibile."""
        shot = self._take_screenshot(device_id)
        path = shot.data if shot.success else None
        return Result.ok(LoginResult(status, book, device_id, message, screenshot_path=path))
