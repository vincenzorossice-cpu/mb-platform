from unittest.mock import MagicMock, patch

import pytest

from orchestrator.config.book_navigation import BOOK_NAVIGATION
from orchestrator.core.result import Result
from orchestrator.modules.login_manager.interface import LoginStatus
from orchestrator.modules.login_manager.manager import LoginManager
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement

DEVICE = "emulator-5554"
BOOK = "SISAL"

EMAIL_BOUNDS = [100, 200, 400, 250]
PASSWORD_BOUNDS = [100, 300, 400, 350]
BUTTON_BOUNDS = [100, 450, 400, 520]


def _screen(*elements, screen_type="login_page"):
    return ScreenContent(elements=list(elements), screen_type=screen_type,
                         confidence=1.0, method="uiautomator")


def _el(text, cls="android.widget.TextView", bounds=None, resource_id=None):
    return UIElement(text=text, bounds=bounds or [0, 0, 0, 0],
                     resource_id=resource_id, class_name=cls)


MOCK_LOGGED_IN_SCREEN = _screen(
    _el("Saldo: 125.50€"), _el("Il mio conto"),
    _el("Logout", "android.widget.Button"), screen_type="home_page")

MOCK_LOGIN_FORM_SCREEN = _screen(
    _el("Accedi"),
    _el("Email", "android.widget.EditText", EMAIL_BOUNDS),
    _el("Password", "android.widget.EditText", PASSWORD_BOUNDS),
    _el("Accedi", "android.widget.Button", BUTTON_BOUNDS))

MOCK_USER_FILLED_SCREEN = _screen(
    _el("Accedi"),
    _el("mario@example.it", "android.widget.EditText", EMAIL_BOUNDS),
    _el("Password", "android.widget.EditText", PASSWORD_BOUNDS),
    _el("Accedi", "android.widget.Button", BUTTON_BOUNDS))

MOCK_BOTH_FILLED_SCREEN = _screen(
    _el("Accedi"),
    _el("mario@example.it", "android.widget.EditText", EMAIL_BOUNDS),
    _el("••••••••", "android.widget.EditText", PASSWORD_BOUNDS),
    _el("Accedi", "android.widget.Button", BUTTON_BOUNDS))

MOCK_CAPTCHA_SCREEN = _screen(
    _el("Email", "android.widget.EditText"),
    _el("Non sono un robot", "android.widget.CheckBox"),
    _el("Accedi", "android.widget.Button"))

MOCK_2FA_SCREEN = _screen(
    _el("Inserisci il codice di verifica"),
    _el("Codice", "android.widget.EditText"))

MOCK_LOGIN_FAILED_SCREEN = _screen(
    _el("Credenziali errate. Riprova."),
    _el("Email", "android.widget.EditText"),
    _el("Password", "android.widget.EditText"),
    _el("Accedi", "android.widget.Button"))

MOCK_NEUTRAL_SCREEN = _screen(_el("Promozioni"), _el("Calcio"), screen_type="unknown")


def _reader(*screens):
    """read_screen che restituisce le schermate in sequenza, poi ripete l'ultima."""
    seq = list(screens)

    def _read(device_id, *args, **kwargs):
        screen = seq.pop(0) if len(seq) > 1 else seq[0]
        return Result.ok(screen)

    return _read


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("orchestrator.modules.login_manager.manager.time.sleep"):
        yield


@pytest.fixture
def parts():
    screen_reader = MagicMock()
    screen_reader._screenshot.return_value = Result.ok("/tmp/shot.png")
    touch_injector = MagicMock()
    touch_injector.tap_coordinates.return_value = Result.ok(True)
    book_navigator = MagicMock()
    book_navigator.open_chrome.return_value = Result.ok(True)
    return screen_reader, touch_injector, book_navigator


@pytest.fixture
def manager(parts):
    screen_reader, touch_injector, book_navigator = parts
    return LoginManager(screen_reader, touch_injector, book_navigator=book_navigator)


def _set_screens(parts, *screens):
    parts[0].read_screen.side_effect = _reader(*screens)


# ---------------- check_session ----------------

def test_check_session_logged_in(manager, parts):
    _set_screens(parts, MOCK_LOGGED_IN_SCREEN)
    res = manager.check_session(DEVICE, BOOK)
    assert res.success and res.data.status == LoginStatus.LOGGED_IN


def test_check_session_needs_login(manager, parts):
    _set_screens(parts, MOCK_LOGIN_FORM_SCREEN)
    res = manager.check_session(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_REQUIRED


def test_check_session_captcha(manager, parts):
    _set_screens(parts, MOCK_CAPTCHA_SCREEN)
    res = manager.check_session(DEVICE, BOOK)
    assert res.data.status == LoginStatus.CAPTCHA_DETECTED
    assert res.data.screenshot_path == "/tmp/shot.png"


def test_check_session_2fa(manager, parts):
    _set_screens(parts, MOCK_2FA_SCREEN)
    res = manager.check_session(DEVICE, BOOK)
    assert res.data.status == LoginStatus.TWO_FA_REQUIRED


def test_check_session_ambiguous(manager, parts):
    _set_screens(parts, MOCK_NEUTRAL_SCREEN)
    res = manager.check_session(DEVICE, BOOK)
    assert res.data.status == LoginStatus.AMBIGUOUS


# ---------------- perform_login ----------------

def test_perform_login_already_logged(manager, parts):
    _set_screens(parts, MOCK_LOGGED_IN_SCREEN)
    res = manager.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGGED_IN
    parts[1].tap_coordinates.assert_not_called()


def test_perform_login_success(manager, parts):
    _set_screens(parts, MOCK_LOGIN_FORM_SCREEN, MOCK_USER_FILLED_SCREEN,
                 MOCK_BOTH_FILLED_SCREEN, MOCK_LOGGED_IN_SCREEN)
    res = manager.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    # tap: username, password, submit (centri dei bounds)
    taps = [c.args[1:] for c in parts[1].tap_coordinates.call_args_list]
    assert taps == [(250, 225), (250, 325), (250, 485)]


def test_perform_login_autofill_failed(manager, parts):
    _set_screens(parts, MOCK_LOGIN_FORM_SCREEN)  # il campo resta "Email"
    res = manager.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.AUTOFILL_FAILED
    assert parts[1].tap_coordinates.call_count == 1  # niente submit


def test_perform_login_failed_credentials(manager, parts):
    _set_screens(parts, MOCK_LOGIN_FORM_SCREEN, MOCK_USER_FILLED_SCREEN,
                 MOCK_BOTH_FILLED_SCREEN, MOCK_LOGIN_FAILED_SCREEN)
    res = manager.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_FAILED


def test_perform_login_manual_book(manager, parts):
    with patch.dict(BOOK_NAVIGATION["BETFAIR"], {"needs_manual_login": True}):
        res = manager.perform_login(DEVICE, "BETFAIR")
    assert res.data.status == LoginStatus.MANUAL_LOGIN_REQUIRED
    parts[2].open_chrome.assert_not_called()
    parts[0].read_screen.assert_not_called()


def test_perform_login_captcha_during_login(manager, parts):
    _set_screens(parts, MOCK_LOGIN_FORM_SCREEN, MOCK_USER_FILLED_SCREEN,
                 MOCK_BOTH_FILLED_SCREEN, MOCK_CAPTCHA_SCREEN)
    res = manager.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.CAPTCHA_DETECTED


def test_perform_login_2fa_during_login(manager, parts):
    _set_screens(parts, MOCK_LOGIN_FORM_SCREEN, MOCK_USER_FILLED_SCREEN,
                 MOCK_BOTH_FILLED_SCREEN, MOCK_2FA_SCREEN)
    res = manager.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.TWO_FA_REQUIRED


def test_perform_login_navigation_failed(manager, parts):
    parts[2].open_chrome.return_value = Result.fail("adb down", "CHROME_OPEN_FAILED")
    res = manager.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.NAVIGATION_FAILED
    parts[0].read_screen.assert_not_called()


def test_perform_login_never_types_text(manager, parts):
    _set_screens(parts, MOCK_LOGIN_FORM_SCREEN, MOCK_USER_FILLED_SCREEN,
                 MOCK_BOTH_FILLED_SCREEN, MOCK_LOGGED_IN_SCREEN)
    manager.perform_login(DEVICE, BOOK)
    parts[1].input_text.assert_not_called()


def test_perform_login_device_lock(parts):
    dm = MagicMock()
    dm.acquire_lock.return_value = False
    mgr = LoginManager(parts[0], parts[1], device_manager=dm, book_navigator=parts[2])
    res = mgr.perform_login(DEVICE, BOOK)
    assert not res.success and res.error_code == "DEVICE_BUSY"
    dm.release_lock.assert_not_called()

    dm.acquire_lock.return_value = True
    _set_screens(parts, MOCK_LOGGED_IN_SCREEN)
    mgr.perform_login(DEVICE, BOOK)
    dm.release_lock.assert_called_once_with(DEVICE)


# ---------------- form / verify / utility ----------------

def test_find_login_form_elements(manager):
    form = manager._find_login_form_elements(MOCK_LOGIN_FORM_SCREEN)
    assert form["username_element"].bounds == EMAIL_BOUNDS
    assert form["password_element"].bounds == PASSWORD_BOUNDS
    assert form["submit_element"].bounds == BUTTON_BOUNDS


def test_find_login_form_elements_no_form(manager):
    form = manager._find_login_form_elements(MOCK_LOGGED_IN_SCREEN)
    assert form == {"username_element": None, "password_element": None,
                    "submit_element": None}


def test_verify_login_success(manager, parts):
    _set_screens(parts, _screen(_el("Saldo 10€"), _el("Profilo")))
    assert manager.verify_login(DEVICE, BOOK).data.status == LoginStatus.LOGIN_SUCCESS


def test_verify_login_failed(manager, parts):
    _set_screens(parts, MOCK_LOGIN_FAILED_SCREEN)
    assert manager.verify_login(DEVICE, BOOK).data.status == LoginStatus.LOGIN_FAILED


def test_has_indicators(manager):
    assert manager._has_indicators(MOCK_LOGGED_IN_SCREEN, ["logout"])
    assert not manager._has_indicators(MOCK_LOGGED_IN_SCREEN, ["Password"])
    # match a parola intera: "Esci" non deve scattare dentro "Crescita"
    assert not manager._has_indicators(_screen(_el("Crescita")), ["Esci"])


def test_get_book_config(manager):
    cfg = manager._get_book_config("sisal")
    assert cfg["balance_url"] == BOOK_NAVIGATION["SISAL"]["balance_url"]
    assert cfg["needs_manual_login"] is False


def test_get_book_config_unknown(manager):
    cfg = manager._get_book_config("NON-ESISTE")
    assert cfg["needs_manual_login"] is False
    assert cfg["balance_url"] is None
    assert cfg["login_form_labels"]["submit_label"] == "Accedi"
