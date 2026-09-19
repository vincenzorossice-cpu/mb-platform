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

MAX_NAV_DEPTH = 3
MIN_NAV_TEXT_LEN = 2

BALANCE_KEYWORDS = ["saldo", "balance", "disponibile", "wallet", "conto"]

NAV_KEYWORDS = [
    "il mio conto", "account", "profilo", "menu", "saldo",
    "balance", "conto", "wallet", "pannello", "impostazioni",
]

GUEST_KEYWORDS = [
    "cookie", "privacy", "termini", "guida", "contatti",
    "normativa", "affiliati", "mappa del sito",
]

LOGIN_KEYWORDS = ["login", "accedi", "log in", "registrati", "sign in"]

MY_BETS_KEYWORDS = ["scommesse", "my bets", "le mie scommesse"]

_AMOUNT_CHARS = ("€", "$", "£")


def _looks_like_amount(text: str) -> bool:
    return any(ch.isdigit() for ch in text) or any(c in text for c in _AMOUNT_CHARS)


def _find_direct_balance_element(elements):
    for elem in elements:
        if not elem.text:
            continue
        lower = elem.text.lower().strip()
        if any(kw in lower for kw in GUEST_KEYWORDS):
            continue
        if any(kw in lower for kw in BALANCE_KEYWORDS) and _looks_like_amount(elem.text):
            return elem
    return None


def _find_nav_candidate(elements, keywords):
    for elem in elements:
        if not elem.text:
            continue
        trimmed = elem.text.strip()
        if len(trimmed) <= MIN_NAV_TEXT_LEN:
            continue
        lower = trimmed.lower()
        if any(kw in lower for kw in GUEST_KEYWORDS):
            continue
        if any(kw in lower for kw in keywords):
            return elem
    return None


class BookNavigator(BookNavigatorInterface):

    # Configurazione per-book: solo URL. Le keyword di navigazione sono
    # generiche e valgono per qualunque sito (vedi costanti di modulo sopra).
    BOOK_CONFIG = {
        "BETFAIR": {"url": "https://www.betfair.it/exchange/plus/"},
        "SISAL": {"url": "https://m.sisal.it"},
        "STANLEYBET": {"url": "https://m.stanleybet.it"},
        "BET365": {"url": "https://www.bet365.it"},
        "SNAI": {"url": "https://m.snai.it"},
        "EUROBET": {"url": "https://m.eurobet.it"},
        "GOLDBET": {"url": "https://m.goldbet.it"},
        "LOTTOMATICA": {"url": "https://m.lottomatica.it"},
        "PLANETWIN": {"url": "https://m.planetwin365.it"},
        "BETFLAG": {"url": "https://m.betflag.it"},
        "BWIN": {"url": "https://m.bwin.it"},
        "BETSSON": {"url": "https://m.betsson.it"},
        "POKERSTARS": {"url": "https://m.pokerstars.it"},
        "WILLIAMHILL": {"url": "https://m.williamhill.it"},
        "UNIBET": {"url": "https://m.unibet.it"},
        "DAZNBET": {"url": "https://m.daznbet.it"},
        "LEOVEGAS": {"url": "https://m.leovegas.it"},
        "NETBET": {"url": "https://m.netbet.it"},
        "CODERE": {"url": "https://m.codere.it"},
        "STAKE": {"url": "https://m.stake.it"},
    }

    def __init__(
        self,
        screen_reader: Optional[ScreenReaderInterface] = None,
        touch_injector: Optional[TouchInjectorInterface] = None,
    ):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector

    def _resolve_url(self, book_upper: str) -> str:
        config = self.BOOK_CONFIG.get(book_upper)
        if config:
            return config["url"]
        return f"https://m.{book_upper.lower()}.it"

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

        Approccio generico (nessuna configurazione per-book oltre l'URL):
        apre Chrome, poi esplora fino a MAX_NAV_DEPTH schermate cercando
        il saldo direttamente o un elemento di navigazione da tappare.
        """
        book_upper = book.upper()
        url = self._resolve_url(book_upper)

        open_result = self.open_chrome(device_id, url)
        if not open_result.success:
            return Result.fail(open_result.error, open_result.error_code)

        if self.screen_reader is None:
            return Result.ok(NavigationResult(
                success=True, current_screen_type="unknown",
                url=url, needs_login=False,
                message="Chrome aperto, ScreenReader non disponibile per verifica"
            ))

        elements = []
        screen_type = "unknown"

        for _depth in range(MAX_NAV_DEPTH):
            screen = self.screen_reader.read_screen(device_id)
            if not screen.success:
                return Result.fail(f"Cannot read screen: {screen.error}", screen.error_code)

            elements = screen.data.elements
            screen_type = screen.data.screen_type

            if screen_type == "balance_page":
                return Result.ok(NavigationResult(
                    success=True, current_screen_type="balance_page",
                    url=url, needs_login=False,
                    message=f"Pagina del saldo raggiunta per {book}"
                ))

            if _find_direct_balance_element(elements) is not None:
                return Result.ok(NavigationResult(
                    success=True, current_screen_type=screen_type,
                    url=url, needs_login=False,
                    message=f"Saldo individuato direttamente sullo schermo per {book}"
                ))

            nav_element = _find_nav_candidate(elements, NAV_KEYWORDS)
            if nav_element is None or self.touch_injector is None:
                break

            tap_result = self.touch_injector.tap_element(device_id, nav_element.text)
            if not tap_result.success:
                break

            time.sleep(3)  # Attendi caricamento

        all_text = " ".join(e.text.lower().strip() for e in elements if e.text)
        if any(kw in all_text for kw in LOGIN_KEYWORDS):
            return Result.ok(NavigationResult(
                success=False, current_screen_type="login_page",
                url=url, needs_login=True,
                message=f"Login richiesto per {book}. L'operatore deve fare login manualmente."
            ))

        return Result.ok(NavigationResult(
            success=False, current_screen_type=screen_type or "unknown",
            url=url, needs_login=False,
            message=f"Pagina del saldo non trovata per {book} dopo {MAX_NAV_DEPTH} tentativi. Screen type: {screen_type}"
        ))

    # --------------------------------------------------------
    # PUBLIC: navigate_to_my_bets
    # --------------------------------------------------------

    def navigate_to_my_bets(self, device_id: str, book: str) -> Result[NavigationResult]:
        """Naviga alla pagina My Bets / Scommesse per il book specificato."""
        book_upper = book.upper()
        url = self._resolve_url(book_upper)

        open_result = self.open_chrome(device_id, url)
        if not open_result.success:
            return Result.fail(open_result.error, open_result.error_code)

        if self.screen_reader is None:
            return Result.ok(NavigationResult(
                success=True, current_screen_type="unknown",
                url=url, needs_login=False,
                message="Chrome aperto, ScreenReader non disponibile per verifica"
            ))

        screen = self.screen_reader.read_screen(device_id)
        if not screen.success:
            return Result.fail(f"Cannot read screen: {screen.error}", screen.error_code)

        elements = screen.data.elements
        all_text = " ".join(e.text.lower().strip() for e in elements if e.text)

        if any(kw in all_text for kw in LOGIN_KEYWORDS):
            return Result.ok(NavigationResult(
                success=False, current_screen_type="login_page",
                url=url, needs_login=True,
                message=f"Login richiesto per {book}. L'operatore deve fare login manualmente."
            ))

        screen_type = screen.data.screen_type
        if screen_type == "my_bets":
            return Result.ok(NavigationResult(
                success=True, current_screen_type="my_bets",
                url=url, needs_login=False,
                message=f"Pagina Scommesse raggiunta per {book}"
            ))

        nav_element = _find_nav_candidate(elements, MY_BETS_KEYWORDS)
        if nav_element is not None and self.touch_injector is not None:
            tap_result = self.touch_injector.tap_element(device_id, nav_element.text)
            if tap_result.success:
                time.sleep(2)  # Attendi caricamento
                verify = self.screen_reader.read_screen(device_id)
                if not verify.success:
                    return Result.fail(f"Cannot read screen: {verify.error}", verify.error_code)
                return Result.ok(NavigationResult(
                    success=verify.data.screen_type == "my_bets",
                    current_screen_type=verify.data.screen_type,
                    url=url, needs_login=False,
                    message=f"Navigato a {nav_element.text} per {book}"
                ))

        return Result.ok(NavigationResult(
            success=False, current_screen_type=screen_type or "unknown",
            url=url, needs_login=False,
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

        all_text = " ".join(e.text.lower().strip() for e in screen.data.elements if e.text)
        is_logged_out = any(kw in all_text for kw in LOGIN_KEYWORDS)

        return Result.ok(not is_logged_out)
