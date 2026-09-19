from abc import ABC, abstractmethod
from dataclasses import dataclass
from orchestrator.core.result import Result


@dataclass
class NavigationResult:
    success: bool
    current_screen_type: str    # "balance_page" | "my_bets" | "login_page" | "unknown"
    url: str                    # URL corrente (se leggibile)
    needs_login: bool           # True se serve login manuale
    message: str                # Messaggio descrittivo


class BookNavigatorInterface(ABC):
    @abstractmethod
    def navigate_to_balance(self, device_id: str, book: str) -> Result[NavigationResult]:
        """Naviga alla pagina del saldo per il book specificato."""

    @abstractmethod
    def navigate_to_my_bets(self, device_id: str, book: str) -> Result[NavigationResult]:
        """Naviga alla pagina My Bets / Scommesse."""

    @abstractmethod
    def open_chrome(self, device_id: str, url: str) -> Result[bool]:
        """Apre Chrome su un URL specifico via ADB."""

    @abstractmethod
    def check_login_status(self, device_id: str) -> Result[bool]:
        """Verifica se l'utente è loggato sul book corrente."""

    @abstractmethod
    def auto_login(self, device_id: str, book: str) -> Result[NavigationResult]:
        """Tenta un login automatico (MAX 1 tentativo) se Chrome ha credenziali salvate."""
