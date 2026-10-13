from dataclasses import dataclass
from enum import Enum
from typing import Optional


class LoginStatus(Enum):
    LOGGED_IN = "logged_in"                    # sessione già attiva
    LOGIN_REQUIRED = "login_required"          # sessione assente: serve eseguire il recipe
    LOGIN_SUCCESS = "login_success"            # login effettuato con successo
    LOGIN_FAILED = "login_failed"              # credenziali errate
    AUTOFILL_FAILED = "autofill_failed"        # Chrome non ha riempito il campo
    CAPTCHA_DETECTED = "captcha_detected"
    TWO_FA_REQUIRED = "two_fa_required"
    RECIPE_NOT_FOUND = "recipe_not_found"      # book non ancora mappato dal LoginMapper
    RECIPE_STEP_FAILED = "recipe_step_failed"  # elemento del recipe non trovato: sito cambiato, ri-mappare
    AMBIGUOUS = "ambiguous"                    # stato non determinabile
    NAVIGATION_FAILED = "navigation_failed"


# "Sono dentro": sessione gia' attiva oppure login appena riuscito.
LOGGED_IN_STATUSES = (LoginStatus.LOGGED_IN, LoginStatus.LOGIN_SUCCESS)


@dataclass
class LoginResult:
    status: LoginStatus
    book: str
    device_id: str
    message: str
    screenshot_path: Optional[str] = None  # screenshot per HITL
    details: Optional[dict] = None

    @property
    def is_logged_in(self) -> bool:
        return self.status in LOGGED_IN_STATUSES
