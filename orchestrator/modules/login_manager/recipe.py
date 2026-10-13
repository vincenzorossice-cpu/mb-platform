import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from typing import Optional

from orchestrator.core.result import Result

VALID_ACTIONS = ("tap_button", "tap_field", "tap_coordinates", "press_enter", "wait", "check_session")

# Indicatori di default: usati dal Mapper per i nuovi recipe e uniti a quelli del
# recipe dall'Executor. Solo testi affidabili (niente "Benvenuto", "Account", "Saldo").
LOGIN_BUTTON_INDICATORS = ["Accedi", "Registrati", "Login", "Log in", "Sign in"]
CAPTCHA_INDICATORS = ["Non sono un robot", "reCAPTCHA", "recaptcha", "captcha"]
DEFAULT_LOGGED_IN_INDICATORS = [
    "Logout", "Esci", "Il mio conto", "Profilo", "Saldo disponibile", "Effettua logout",
]
DEFAULT_LOGIN_FAILED_INDICATORS = [
    "credenziali errate", "password non valida", "riprova", "accesso negato", "dati non corretti",
]

DEFAULT_RECIPES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "login_recipes.json",
)


@dataclass
class LoginStep:
    action: str                          # uno di VALID_ACTIONS (press_enter = keyevent 66, senza parametri)
    target_text: Optional[str] = None    # tap_button: testo del pulsante
    target_label: Optional[str] = None   # tap_field: label del campo
    x: Optional[int] = None              # tap_coordinates
    y: Optional[int] = None
    # Ignorato dall'Executor (resta per i recipe gia' salvati): col popup Touch to Fill il campo si riempie solo dopo
    # il tap sul suggerimento, quindi il riempimento si verifica a fine recipe (verify_login), non dopo il tap.
    trigger_autofill: bool = False
    wait_after_seconds: float = 3.0
    description: str = ""


@dataclass
class LoginRecipe:
    book: str
    login_url: str
    steps: list[LoginStep]
    has_captcha: bool = False
    captcha_type: Optional[str] = None
    logged_in_indicators: list[str] = field(default_factory=lambda: list(DEFAULT_LOGGED_IN_INDICATORS))
    login_failed_indicators: list[str] = field(default_factory=lambda: list(DEFAULT_LOGIN_FAILED_INDICATORS))
    mapped_at: str = ""
    mapped_by: str = ""


def _validate_step(step: LoginStep) -> Optional[str]:
    """Messaggio di errore se lo step non e' eseguibile, altrimenti None."""
    if step.action not in VALID_ACTIONS:
        return f"azione non valida '{step.action}'"
    if step.action == "tap_button" and not step.target_text:
        return "tap_button richiede target_text"
    if step.action == "tap_field" and not step.target_label:
        return "tap_field richiede target_label"
    if step.action == "tap_coordinates" and (step.x is None or step.y is None):
        return "tap_coordinates richiede x e y"
    return None


def _step_from_dict(data: dict) -> LoginStep:
    known = {f.name for f in fields(LoginStep)}
    return LoginStep(**{k: v for k, v in data.items() if k in known})


def _recipe_from_dict(book: str, data: dict) -> Result[LoginRecipe]:
    try:
        steps = [_step_from_dict(s) for s in data.get("steps", [])]
        for index, step in enumerate(steps):
            problem = _validate_step(step)
            if problem:
                return Result.fail(f"Recipe {book}, step {index}: {problem}", "RECIPE_INVALID")
        if not data.get("login_url"):
            return Result.fail(f"Recipe {book}: login_url mancante", "RECIPE_INVALID")
        return Result.ok(LoginRecipe(
            book=book,
            login_url=data["login_url"],
            steps=steps,
            has_captcha=bool(data.get("has_captcha", False)),
            captcha_type=data.get("captcha_type"),
            logged_in_indicators=list(data.get("logged_in_indicators", DEFAULT_LOGGED_IN_INDICATORS)),
            login_failed_indicators=list(data.get("login_failed_indicators", DEFAULT_LOGIN_FAILED_INDICATORS)),
            mapped_at=data.get("mapped_at", ""),
            mapped_by=data.get("mapped_by", ""),
        ))
    except (TypeError, ValueError, AttributeError) as e:
        return Result.fail(f"Recipe {book} non valido: {e}", "RECIPE_INVALID")


class RecipeStore:
    """Salva e carica i login recipe per book (un file JSON, chiavi in maiuscolo).

    I recipe sono per book, non per identita': il flusso di login e' lo stesso.
    Il file e' orchestrator/config/login_recipes.json. Un file illeggibile non
    viene mai sovrascritto: il salvataggio fallisce con RECIPE_FILE_CORRUPT.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_RECIPES_PATH

    # --------------------------------------------------------
    # PUBLIC
    # --------------------------------------------------------

    def save_recipe(self, recipe: LoginRecipe) -> Result[bool]:
        """Salva o aggiorna un recipe (scrittura atomica)."""
        for index, step in enumerate(recipe.steps):
            problem = _validate_step(step)
            if problem:
                return Result.fail(f"Recipe {recipe.book}, step {index}: {problem}", "RECIPE_INVALID")
        raw = self._read_all()
        if not raw.success:
            return Result.fail(raw.error, raw.error_code)
        data = raw.data
        data[self._key(recipe.book)] = asdict(recipe)
        return self._write_all(data)

    def load_recipe(self, book: str) -> Result[Optional[LoginRecipe]]:
        """Recipe del book, o Result.ok(None) se non e' mappato."""
        raw = self._read_all()
        if not raw.success:
            return Result.fail(raw.error, raw.error_code)
        entry = raw.data.get(self._key(book))
        if entry is None:
            return Result.ok(None)
        return _recipe_from_dict(self._key(book), entry)

    def has_recipe(self, book: str) -> bool:
        raw = self._read_all()
        return raw.success and self._key(book) in raw.data

    def list_books(self) -> list[str]:
        raw = self._read_all()
        return sorted(raw.data.keys()) if raw.success else []

    # --------------------------------------------------------
    # INTERNAL
    # --------------------------------------------------------

    @staticmethod
    def _key(book: str) -> str:
        return book.strip().upper()

    def _read_all(self) -> Result[dict]:
        if not os.path.exists(self.path):
            return Result.ok({})
        try:
            with open(self.path, encoding="utf-8") as f:
                content = f.read()
            data = json.loads(content) if content.strip() else {}
        except (OSError, ValueError) as e:
            return Result.fail(f"File recipe illeggibile ({self.path}): {e}", "RECIPE_FILE_CORRUPT")
        if not isinstance(data, dict):
            return Result.fail(f"File recipe non valido ({self.path}): atteso un oggetto JSON",
                               "RECIPE_FILE_CORRUPT")
        return Result.ok(data)

    def _write_all(self, data: dict) -> Result[bool]:
        directory = os.path.dirname(self.path) or "."
        temp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            descriptor, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.path)
            return Result.ok(True)
        except OSError as e:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            return Result.fail(f"Scrittura recipe fallita: {e}", "RECIPE_WRITE_FAILED")
