from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from orchestrator.core.result import Result


class LoginStatus(Enum):
    LOGGED_IN = "logged_in"                          # sessione già attiva
    LOGIN_SUCCESS = "login_success"                  # login effettuato con successo
    LOGIN_FAILED = "login_failed"                    # credenziali errate
    AUTOFILL_FAILED = "autofill_failed"              # Chrome non ha riempito i campi
    CAPTCHA_DETECTED = "captcha_detected"
    TWO_FA_REQUIRED = "two_fa_required"
    MANUAL_LOGIN_REQUIRED = "manual_login_required"  # book che richiede login manuale
    AMBIGUOUS = "ambiguous"                          # stato non determinabile
    NAVIGATION_FAILED = "navigation_failed"
    LOGIN_REQUIRED = "login_required"                # check_session: sessione assente, serve login


@dataclass
class LoginResult:
    status: LoginStatus
    book: str
    device_id: str
    message: str
    screenshot_path: Optional[str] = None  # screenshot per HITL
    details: Optional[dict] = None


class LoginManagerInterface(ABC):
    @abstractmethod
    def check_session(self, device_id: str, book: str) -> Result[LoginResult]:
        """Controlla se la sessione è attiva o serve login."""

    @abstractmethod
    def perform_login(self, device_id: str, book: str) -> Result[LoginResult]:
        """Login completo: check session -> autofill -> verify."""

    @abstractmethod
    def verify_login(self, device_id: str, book: str) -> Result[LoginResult]:
        """Verifica se il login è riuscito leggendo la schermata post-login."""
