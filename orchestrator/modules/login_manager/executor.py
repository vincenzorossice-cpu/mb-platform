import time
from typing import Optional

from orchestrator.config.book_navigation import BOOK_NAVIGATION
from orchestrator.core.result import Result
from orchestrator.modules.book_navigator.navigator import BookNavigator
from orchestrator.modules.login_manager import adb_input, ui_helpers
from orchestrator.modules.login_manager.interface import LoginResult, LoginStatus
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


class LoginExecutor:
    """Esegue il login in modo deterministico seguendo il LoginRecipe del book.

    Solo tap: Chrome riempie i campi con l'autofill, l'Executor non scrive MAI password.
    Un solo tentativo di submit per chiamata, nessun retry (un secondo tentativo con
    credenziali sbagliate rischia il blocco dell'account). Book senza recipe:
    RECIPE_NOT_FOUND (va prima mappato dal LoginMapper).
    """

    PAGE_LOAD_WAIT = 3
    VERIFY_RETRY_WAIT = 3  # verify_login: attesa prima della rilettura se la schermata e' ambigua

    # Pulsanti presenti solo da sloggati: se ce n'e' uno la sessione NON e' attiva,
    # anche se un testo marketing contiene un indicatore di sessione.
    LOGIN_BUTTON_INDICATORS = list(LOGIN_BUTTON_INDICATORS)
    # Solo indicatori affidabili: niente "Benvenuto", "Account", "Saldo" da soli.
    LOGGED_IN_INDICATORS = list(DEFAULT_LOGGED_IN_INDICATORS)
    LOGIN_FAILED_INDICATORS = list(DEFAULT_LOGIN_FAILED_INDICATORS)
    CAPTCHA_INDICATORS = list(CAPTCHA_INDICATORS)
    TWO_FA_INDICATORS = ["inserisci codice", "codice SMS", "codice di verifica", "2FA"]

    def __init__(
        self,
        screen_reader,
        touch_injector,
        book_navigator: Optional[BookNavigator] = None,
        recipe_store: Optional[RecipeStore] = None,
        device_manager=None,
    ):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.book_navigator = book_navigator or BookNavigator(screen_reader, touch_injector)
        self.recipe_store = recipe_store or RecipeStore()
        self.device_manager = device_manager

    # --------------------------------------------------------
    # PUBLIC
    # --------------------------------------------------------

    def perform_login(self, device_id: str, book: str, lock_device: bool = True) -> Result[LoginResult]:
        """Flusso completo: recipe -> apri il book -> check sessione -> esegui recipe -> verifica.

        lock_device=False: non prende il lock del DeviceManager (il device e' gia' del chiamante,
        il lock non e' rientrante). Un solo tentativo di submit, nessun retry.
        """
        book_key = self._book_key(book)
        use_lock = lock_device and self.device_manager is not None
        if use_lock:
            if not self.device_manager.acquire_lock(device_id, f"login:{book_key}"):
                return Result.fail(f"Device {device_id} occupato", "DEVICE_BUSY")
        try:
            return self._perform_login_locked(device_id, book_key)
        finally:
            if use_lock:
                self.device_manager.release_lock(device_id)

    def ensure_logged_in(self, device_id: str, book: str) -> Result[LoginResult]:
        """Verifica che la sessione sia attiva e, se scaduta, rifa il login.

        1. check_session
        2. LOGGED_IN -> ritorna LOGGED_IN
        3. LOGIN_REQUIRED -> perform_login (un solo tentativo) e ritorna il suo esito
           (LOGIN_SUCCESS, LOGIN_FAILED, CAPTCHA_DETECTED, ...)
        4. Qualunque altro stato (CAPTCHA, 2FA, AMBIGUOUS, NAVIGATION_FAILED, ...): ritornato cosi' com'e'
        Per sapere se si e' dentro: LoginResult.is_logged_in (LOGGED_IN o LOGIN_SUCCESS).

        Da chiamare prima di ogni operazione sul book e quando la sessione puo' essere scaduta a meta'
        operazione. Attenzione:
        - check_session NAVIGA a login_url: il telefono esce dalla pagina corrente (es. un gioco);
          dopo la chiamata il chiamante deve riportarlo dove serviva.
        - il device e' del chiamante, che di solito tiene gia' il lock: qui il login non lo riprende
          (il lock del DeviceManager non e' rientrante e darebbe sempre DEVICE_BUSY).
        """
        checked = self.check_session(device_id, book)
        if not checked.success or checked.data.status != LoginStatus.LOGIN_REQUIRED:
            return checked  # LOGGED_IN, oppure uno stato che il login non risolve, oppure un errore
        return self.perform_login(device_id, book, lock_device=False)

    def check_session(self, device_id: str, book: str) -> Result[LoginResult]:
        """Apre il book e legge la schermata per capire se la sessione e' attiva.

        Ordine: CAPTCHA/2FA -> pulsante di login (LOGIN_REQUIRED, priorita') -> indicatori
        di sessione (LOGGED_IN) -> AMBIGUOUS. Non richiede il recipe: senza, l'URL viene
        da BOOK_NAVIGATION.
        """
        book_key = self._book_key(book)
        recipe_res = self.recipe_store.load_recipe(book_key)
        if not recipe_res.success:
            return Result.fail(recipe_res.error, recipe_res.error_code)
        recipe = recipe_res.data
        cfg = BOOK_NAVIGATION.get(book_key, {})
        url = recipe.login_url if recipe else (cfg.get("login_url") or cfg.get("balance_url"))
        res = self._open_and_classify(device_id, book_key, url, recipe)
        if not res.success:
            return Result.fail(res.error, res.error_code)
        return Result.ok(res.data[0])

    def verify_login(self, device_id: str, book: str) -> Result[LoginResult]:
        """Verifica il login leggendo la schermata post-login.

        CAPTCHA/2FA -> LOGIN_FAILED_INDICATORS (LOGIN_FAILED) -> LOGGED_IN_INDICATORS (LOGIN_SUCCESS).
        Se la schermata e' ambigua (la pagina puo' non aver finito di caricare) attende
        VERIFY_RETRY_WAIT secondi e rilegge UNA volta; ancora ambigua -> AMBIGUOUS.
        Gli esiti definitivi non aspettano.
        """
        book_key = self._book_key(book)
        recipe_res = self.recipe_store.load_recipe(book_key)
        recipe = recipe_res.data if recipe_res.success else None

        for attempt in range(2):
            screen_res = self._read(device_id)
            if not screen_res.success:
                return Result.fail(screen_res.error, screen_res.error_code)
            outcome = self._evaluate_login_screen(screen_res.data, book_key, device_id, recipe)
            if outcome is not None:
                return outcome
            if attempt == 0:
                time.sleep(self.VERIFY_RETRY_WAIT)
        return self._hitl(LoginStatus.AMBIGUOUS, book_key, device_id, "Esito del login non determinabile")

    def _evaluate_login_screen(self, screen: ScreenContent, book_key: str, device_id: str,
                               recipe: Optional[LoginRecipe]) -> Optional[Result[LoginResult]]:
        """Esito del login dalla schermata, o None se ambigua."""
        if self._detect_captcha(screen):
            return self._hitl(LoginStatus.CAPTCHA_DETECTED, book_key, device_id, "CAPTCHA rilevato")
        if self._detect_2fa(screen):
            return self._hitl(LoginStatus.TWO_FA_REQUIRED, book_key, device_id, "2FA richiesta")
        failed = self.LOGIN_FAILED_INDICATORS + (recipe.login_failed_indicators if recipe else [])
        if self._has_indicators(screen, failed):
            return self._hitl(LoginStatus.LOGIN_FAILED, book_key, device_id,
                              "Credenziali errate o login rifiutato")
        if self._has_indicators(screen, self._logged_in_indicators(recipe)):
            return Result.ok(LoginResult(LoginStatus.LOGIN_SUCCESS, book_key, device_id, "Login riuscito"))
        return None

    # --------------------------------------------------------
    # INTERNAL: flow
    # --------------------------------------------------------

    def _perform_login_locked(self, device_id: str, book_key: str) -> Result[LoginResult]:
        recipe_res = self.recipe_store.load_recipe(book_key)
        if not recipe_res.success:
            return Result.fail(recipe_res.error, recipe_res.error_code)
        recipe = recipe_res.data
        if recipe is None:
            return Result.ok(LoginResult(
                LoginStatus.RECIPE_NOT_FOUND, book_key, device_id,
                f"Nessun recipe per {book_key}: va mappato con il LoginMapper"))

        res = self._open_and_classify(device_id, book_key, recipe.login_url, recipe)
        if not res.success:
            return Result.fail(res.error, res.error_code)
        session = res.data[0]
        if session.status != LoginStatus.LOGIN_REQUIRED:
            return Result.ok(session)  # LOGGED_IN, CAPTCHA, 2FA, AMBIGUOUS, NAVIGATION_FAILED

        return self._execute_recipe(device_id, recipe)

    def _open_and_classify(self, device_id: str, book_key: str, url: Optional[str],
                           recipe: Optional[LoginRecipe]):
        """Apre l'URL, attende, legge e classifica. Ritorna Result[(LoginResult, ScreenContent|None)]."""
        if not url:
            return Result.ok((LoginResult(
                LoginStatus.NAVIGATION_FAILED, book_key, device_id,
                f"Nessun URL configurato per {book_key}"), None))
        opened = self.book_navigator.open_chrome(device_id, url)
        if not opened.success:
            return Result.ok((LoginResult(
                LoginStatus.NAVIGATION_FAILED, book_key, device_id,
                opened.error or "Apertura Chrome fallita"), None))
        time.sleep(self.PAGE_LOAD_WAIT)

        screen_res = self._read(device_id)
        if not screen_res.success:
            return Result.fail(screen_res.error, screen_res.error_code)
        screen = screen_res.data
        return Result.ok((self._classify(screen, book_key, device_id, recipe), screen))

    def _classify(self, screen: ScreenContent, book_key: str, device_id: str,
                  recipe: Optional[LoginRecipe]) -> LoginResult:
        if self._detect_captcha(screen):
            return self._hitl(LoginStatus.CAPTCHA_DETECTED, book_key, device_id, "CAPTCHA rilevato").data
        if self._detect_2fa(screen):
            return self._hitl(LoginStatus.TWO_FA_REQUIRED, book_key, device_id, "2FA richiesta").data
        if self._has_indicators(screen, self.LOGIN_BUTTON_INDICATORS):
            return LoginResult(LoginStatus.LOGIN_REQUIRED, book_key, device_id,
                               "Sessione assente: serve login")
        if self._has_indicators(screen, self._logged_in_indicators(recipe)):
            return LoginResult(LoginStatus.LOGGED_IN, book_key, device_id, "Sessione gia' attiva")
        return self._hitl(LoginStatus.AMBIGUOUS, book_key, device_id,
                          "Stato sessione non determinabile").data

    def _execute_recipe(self, device_id: str, recipe: LoginRecipe) -> Result[LoginResult]:
        """Esegue gli step in sequenza e poi verify_login.

        Esiti terminali: step che non trova l'elemento (RECIPE_STEP_FAILED), campo non
        riempito dall'autofill (AUTOFILL_FAILED), step check_session che trova la
        sessione attiva (LOGGED_IN, il login si ferma).
        """
        for index, step in enumerate(recipe.steps):
            res = self._execute_step(device_id, step, recipe)
            if res.success:
                if res.data is False:  # check_session: gia' loggato
                    return Result.ok(LoginResult(LoginStatus.LOGGED_IN, recipe.book, device_id,
                                                 "Sessione gia' attiva (step check_session)"))
                continue

            details = {"step_index": index, "action": step.action,
                       "target": step.target_text or step.target_label,
                       "description": step.description}
            if res.error_code == "ELEMENT_NOT_FOUND":
                details["action_required"] = "remap_book"
                return self._hitl(LoginStatus.RECIPE_STEP_FAILED, recipe.book, device_id,
                                  f"Step {index} ({step.description or step.action}): {res.error} "
                                  f"- sito cambiato: ri-mappare il book con il LoginMapper",
                                  details)
            if res.error_code == "AUTOFILL_NOT_FILLED":
                return self._hitl(LoginStatus.AUTOFILL_FAILED, recipe.book, device_id,
                                  f"Chrome non ha riempito il campo '{step.target_label}'", details)
            return Result.fail(res.error, res.error_code)

        return self.verify_login(device_id, recipe.book)

    def _execute_step(self, device_id: str, step: LoginStep, recipe: Optional[LoginRecipe] = None) -> Result[bool]:
        """Un singolo step. Ok(True): prosegui. Ok(False): sessione gia' attiva, fermati.
        Fail ELEMENT_NOT_FOUND / AUTOFILL_NOT_FILLED / errori di lettura e tap."""
        if step.action == "wait":
            time.sleep(step.wait_after_seconds)
            return Result.ok(True)

        if step.action == "press_enter":
            pressed = adb_input.press_enter(device_id)
            if not pressed.success:
                return Result.fail(pressed.error, pressed.error_code)
            time.sleep(step.wait_after_seconds)
            return Result.ok(True)

        if step.action == "tap_coordinates":
            tapped = self.touch_injector.tap_coordinates(device_id, step.x, step.y)
            if not tapped.success:
                return Result.fail(tapped.error, tapped.error_code)
            time.sleep(step.wait_after_seconds)
            return Result.ok(True)

        screen_res = self._read(device_id)
        if not screen_res.success:
            return Result.fail(screen_res.error, screen_res.error_code)
        screen = screen_res.data

        if step.action == "check_session":
            # Come _classify ma senza screenshot HITL: a meta' recipe basta sapere se si e' gia' loggati.
            active = (not self._has_indicators(screen, self.LOGIN_BUTTON_INDICATORS)
                      and self._has_indicators(screen, self._logged_in_indicators(recipe)))
            return Result.ok(not active)

        if step.action == "tap_button":
            element = self._find_element_by_text(screen, step.target_text)
            if element is None:
                return Result.fail(f"Pulsante '{step.target_text}' non trovato", "ELEMENT_NOT_FOUND")
            tapped = self._tap_element(device_id, element)
            if not tapped.success:
                return Result.fail(tapped.error, tapped.error_code)
            time.sleep(step.wait_after_seconds)
            return Result.ok(True)

        if step.action == "tap_field":
            element = self._find_field_by_label(screen, step.target_label)
            if element is None:
                return Result.fail(f"Campo '{step.target_label}' non trovato", "ELEMENT_NOT_FOUND")
            tapped = self._tap_element(device_id, element)
            if not tapped.success:
                return Result.fail(tapped.error, tapped.error_code)
            time.sleep(step.wait_after_seconds)
            if step.trigger_autofill:
                after = self._read(device_id)
                if not after.success:
                    return Result.fail(after.error, after.error_code)
                if not ui_helpers.is_field_filled(after.data, element, step.target_label):
                    return Result.fail(f"Campo '{step.target_label}' non riempito",
                                       "AUTOFILL_NOT_FILLED")
            return Result.ok(True)

        return Result.fail(f"Azione sconosciuta '{step.action}'", "ELEMENT_NOT_FOUND")

    # --------------------------------------------------------
    # INTERNAL: screen analysis (wrapper sottili su ui_helpers)
    # --------------------------------------------------------

    def _find_element_by_text(self, screen_content: ScreenContent, text: str) -> Optional[UIElement]:
        """Elemento da toccare per quel testo (parola intera, case-insensitive): l'ULTIMO Button,
        altrimenti un elemento non-Button. Stessa regola del LoginMapper."""
        return ui_helpers.resolve_tap_target(screen_content, text)

    def _find_field_by_label(self, screen_content: ScreenContent, label: str) -> Optional[UIElement]:
        """EditText associato a una label (separata dall'EditText o nel suo testo)."""
        return ui_helpers.find_field_by_label(screen_content, label)

    def _has_indicators(self, screen_content: ScreenContent, indicators: list[str]) -> bool:
        return ui_helpers.has_indicators(screen_content, indicators)

    def _detect_captcha(self, screen_content: ScreenContent) -> bool:
        return self._has_indicators(screen_content, self.CAPTCHA_INDICATORS)

    def _detect_2fa(self, screen_content: ScreenContent) -> bool:
        return self._has_indicators(screen_content, self.TWO_FA_INDICATORS)

    # --------------------------------------------------------
    # INTERNAL: device helpers
    # --------------------------------------------------------

    def _read(self, device_id: str) -> Result[ScreenContent]:
        res = self.screen_reader.read_screen(device_id)
        if not res.success:
            return Result.fail(res.error, res.error_code or "SCREEN_READ_FAILED")
        return res

    def _tap_element(self, device_id: str, element: UIElement) -> Result[bool]:
        center = ui_helpers.element_center(element.bounds)
        if center is None:
            return Result.fail(f"Bounds non validi per '{element.text}'", "INVALID_BOUNDS")
        return self.touch_injector.tap_coordinates(device_id, center[0], center[1])

    def _take_screenshot(self, device_id: str) -> Result[str]:
        try:
            return self.screen_reader._screenshot(device_id)
        except Exception as e:
            return Result.fail(f"Screenshot fallito: {e}", "SCREENSHOT_FAILED")

    def _logged_in_indicators(self, recipe: Optional[LoginRecipe]) -> list[str]:
        return (list(recipe.logged_in_indicators) if recipe else []) + self.LOGGED_IN_INDICATORS

    @staticmethod
    def _book_key(book: str) -> str:
        return book.strip().upper()

    def _hitl(self, status: LoginStatus, book: str, device_id: str, message: str,
              details: Optional[dict] = None) -> Result[LoginResult]:
        """LoginResult per intervento umano, con screenshot se disponibile."""
        shot = self._take_screenshot(device_id)
        path = shot.data if shot.success else None
        return Result.ok(LoginResult(status, book, device_id, message,
                                     screenshot_path=path, details=details))
