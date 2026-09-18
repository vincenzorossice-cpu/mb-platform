import subprocess
import time
from typing import Optional
from orchestrator.core.result import Result
from orchestrator.modules.book_navigator.interface import (
    BookNavigatorInterface,
    NavigationResult,
)
from orchestrator.modules.screen_reader.interface import ScreenReaderInterface
from orchestrator.modules.touch_injector.interface import TouchInjectorInterface


class BookNavigator(BookNavigatorInterface):

    # Configurazione per-book
    BOOK_CONFIG = {
        "BETFAIR": {
            "url": "https://mobile.betfair.com",
            "balance_selectors": ["saldo", "balance", "wallet", "€"],
            "login_selectors": ["login", "accedi", "log in"],
            "my_bets_selectors": ["scommesse", "my bets", "le mie scommesse"],
            "needs_login": True,  # Betfair richiede login
        },
        # Altri book si aggiungeranno dopo
    }

    def __init__(
        self,
        screen_reader: Optional[ScreenReaderInterface] = None,
        touch_injector: Optional[TouchInjectorInterface] = None,
    ):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector

    # --------------------------------------------------------
    # PUBLIC: open_chrome
    # --------------------------------------------------------

    def open_chrome(self, device_id: str, url: str) -> Result[bool]:
        """Apre Chrome via ADB intent."""
        try:
            subprocess.run(
                ["adb", "-s", device_id, "shell", "am", "start", "-a",
                 "android.intent.action.VIEW", "-d", url],
                capture_output=True, text=True, timeout=10
            )
            time.sleep(5)  # Attendi caricamento
            return Result.ok(True)
        except Exception as e:
            return Result.fail(f"Cannot open Chrome: {e}", "CHROME_OPEN_FAILED")

    # --------------------------------------------------------
    # PUBLIC: navigate_to_balance
    # --------------------------------------------------------

    def navigate_to_balance(self, device_id: str, book: str) -> Result[NavigationResult]:
        """Naviga alla pagina del saldo per il book specificato.

        Flusso:
        1. Apri Chrome all'URL del book
        2. Leggi schermo
        3. Se trova elemento "Login"/"Accedi" -> needs_login=True, return (l'operatore fa login manuale)
        4. Se già loggato -> verifica se il saldo è visibile
        5. Se il saldo non è in home -> naviga alla pagina del saldo
        6. Verifica di essere sulla pagina giusta
        """
        book_upper = book.upper()
        config = self.BOOK_CONFIG.get(book_upper)
        if not config:
            return Result.fail(f"Book {book} not configured", "BOOK_NOT_CONFIGURED")

        # 1. Apri Chrome
        open_result = self.open_chrome(device_id, config["url"])
        if not open_result.success:
            return Result.fail(open_result.error, open_result.error_code)

        if self.screen_reader is None:
            return Result.ok(NavigationResult(
                success=True, current_screen_type="unknown",
                url=config["url"], needs_login=False,
                message="Chrome aperto, ScreenReader non disponibile per verifica"
            ))

        # 2. Leggi schermo
        screen = self.screen_reader.read_screen(device_id)
        if not screen.success:
            return Result.fail(f"Cannot read screen: {screen.error}", screen.error_code)

        elements = screen.data.elements
        texts_lower = [e.text.lower().strip() for e in elements if e.text]
        all_text = " ".join(texts_lower)

        # 3. Check login
        needs_login = any(sel in all_text for sel in config["login_selectors"])
        if needs_login:
            return Result.ok(NavigationResult(
                success=False, current_screen_type="login_page",
                url=config["url"], needs_login=True,
                message=f"Login richiesto per {book}. L'operatore deve fare login manualmente."
            ))

        # 4. Verifica se saldo è già visibile
        screen_type = screen.data.screen_type
        if screen_type == "balance_page":
            return Result.ok(NavigationResult(
                success=True, current_screen_type="balance_page",
                url=config["url"], needs_login=False,
                message=f"Pagina del saldo raggiunta per {book}"
            ))

        # 5. Cerca elemento per navigare al saldo
        for elem in elements:
            elem_lower = elem.text.lower().strip()
            if any(kw in elem_lower for kw in ["il mio conto", "account", "saldo", "balance", "wallet", "conto"]):
                if self.touch_injector:
                    tap_result = self.touch_injector.tap_element(device_id, elem.text)
                    if tap_result.success:
                        time.sleep(3)  # Attendi caricamento
                        # Verifica di essere sulla pagina del saldo
                        verify = self.screen_reader.read_screen(device_id)
                        if verify.success and verify.data.screen_type == "balance_page":
                            return Result.ok(NavigationResult(
                                success=True, current_screen_type="balance_page",
                                url=config["url"], needs_login=False,
                                message=f"Pagina del saldo raggiunta per {book}"
                            ))

        # 6. Se non trova il saldo, prova my_bets
        for elem in elements:
            elem_lower = elem.text.lower().strip()
            if any(kw in elem_lower for kw in config["my_bets_selectors"]):
                if self.touch_injector:
                    tap_result = self.touch_injector.tap_element(device_id, elem.text)
                    if tap_result.success:
                        time.sleep(3)
                        verify = self.screen_reader.read_screen(device_id)
                        if verify.success:
                            return Result.ok(NavigationResult(
                                success=True, current_screen_type=verify.data.screen_type,
                                url=config["url"], needs_login=False,
                                message=f"Navigato a {elem.text} per {book}"
                            ))

        # 7. Non trovato
        return Result.ok(NavigationResult(
            success=False, current_screen_type=screen_type or "unknown",
            url=config["url"], needs_login=False,
            message=f"Pagina del saldo non trovata per {book}. Screen type: {screen_type}"
        ))

    # --------------------------------------------------------
    # PUBLIC: navigate_to_my_bets
    # --------------------------------------------------------

    def navigate_to_my_bets(self, device_id: str, book: str) -> Result[NavigationResult]:
        """Naviga alla pagina My Bets / Scommesse per il book specificato.

        Flusso analogo a navigate_to_balance, ma cerca direttamente gli
        elementi di my_bets_selectors invece di quelli del saldo.
        """
        book_upper = book.upper()
        config = self.BOOK_CONFIG.get(book_upper)
        if not config:
            return Result.fail(f"Book {book} not configured", "BOOK_NOT_CONFIGURED")

        # 1. Apri Chrome
        open_result = self.open_chrome(device_id, config["url"])
        if not open_result.success:
            return Result.fail(open_result.error, open_result.error_code)

        if self.screen_reader is None:
            return Result.ok(NavigationResult(
                success=True, current_screen_type="unknown",
                url=config["url"], needs_login=False,
                message="Chrome aperto, ScreenReader non disponibile per verifica"
            ))

        # 2. Leggi schermo
        screen = self.screen_reader.read_screen(device_id)
        if not screen.success:
            return Result.fail(f"Cannot read screen: {screen.error}", screen.error_code)

        elements = screen.data.elements
        texts_lower = [e.text.lower().strip() for e in elements if e.text]
        all_text = " ".join(texts_lower)

        # 3. Check login
        needs_login = any(sel in all_text for sel in config["login_selectors"])
        if needs_login:
            return Result.ok(NavigationResult(
                success=False, current_screen_type="login_page",
                url=config["url"], needs_login=True,
                message=f"Login richiesto per {book}. L'operatore deve fare login manualmente."
            ))

        # 4. Verifica se la pagina my_bets è già visibile
        screen_type = screen.data.screen_type
        if screen_type == "my_bets":
            return Result.ok(NavigationResult(
                success=True, current_screen_type="my_bets",
                url=config["url"], needs_login=False,
                message=f"Pagina Scommesse raggiunta per {book}"
            ))

        # 5. Cerca elemento per navigare a my_bets
        for elem in elements:
            elem_lower = elem.text.lower().strip()
            if any(kw in elem_lower for kw in config["my_bets_selectors"]):
                if self.touch_injector:
                    tap_result = self.touch_injector.tap_element(device_id, elem.text)
                    if tap_result.success:
                        time.sleep(2)  # Attendi caricamento
                        verify = self.screen_reader.read_screen(device_id)
                        if verify.success:
                            return Result.ok(NavigationResult(
                                success=verify.data.screen_type == "my_bets",
                                current_screen_type=verify.data.screen_type,
                                url=config["url"], needs_login=False,
                                message=f"Navigato a {elem.text} per {book}"
                            ))

        # 6. Non trovato
        return Result.ok(NavigationResult(
            success=False, current_screen_type=screen_type or "unknown",
            url=config["url"], needs_login=False,
            message=f"Pagina Scommesse non trovata per {book}. Screen type: {screen_type}"
        ))

    # --------------------------------------------------------
    # PUBLIC: check_login_status
    # --------------------------------------------------------

    def check_login_status(self, device_id: str) -> Result[bool]:
        """Verifica se l'utente è loggato leggendo lo schermo corrente."""
        if self.screen_reader is None:
            return Result.fail("ScreenReader not available", "SR_NOT_AVAILABLE")

        screen = self.screen_reader.read_screen(device_id)
        if not screen.success:
            return Result.fail(screen.error, screen.error_code)

        texts_lower = [e.text.lower().strip() for e in screen.data.elements if e.text]
        all_text = " ".join(texts_lower)

        login_keywords = ["login", "accedi", "log in", "sign in"]
        is_logged_out = any(kw in all_text for kw in login_keywords)

        return Result.ok(not is_logged_out)
