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


def test_check_session_benvenuto_and_accedi_is_login_required(manager, parts):
    _set_screens(parts, _screen(_el("Bonus di benvenuto"),
                                _el("Accedi", "android.widget.Button")))
    assert manager.check_session(DEVICE, BOOK).data.status == LoginStatus.LOGIN_REQUIRED


def test_check_session_login_beats_logged_in(manager, parts):
    _set_screens(parts, _screen(_el("Saldo disponibile"),
                                _el("Accedi", "android.widget.Button")))
    assert manager.check_session(DEVICE, BOOK).data.status == LoginStatus.LOGIN_REQUIRED


def test_check_session_saldo_disponibile_logged_in(manager, parts):
    _set_screens(parts, _screen(_el("Saldo disponibile: 10€"), _el("Calcio")))
    assert manager.check_session(DEVICE, BOOK).data.status == LoginStatus.LOGGED_IN


def test_check_session_benvenuto_alone_not_logged_in(manager, parts):
    _set_screens(parts, _screen(_el("Benvenuto"), _el("Bonus di benvenuto"),
                                _el("Ti diamo il benvenuto")))
    assert manager.check_session(DEVICE, BOOK).data.status == LoginStatus.AMBIGUOUS


def test_check_session_account_marketing_not_logged_in(manager, parts):
    _set_screens(parts, _screen(_el("Collega il tuo account Fantacalcio"), _el("Saldo")))
    assert manager.check_session(DEVICE, BOOK).data.status != LoginStatus.LOGGED_IN


def test_check_session_form_labels_alone_not_login_required(manager, parts):
    # Pagina loggata (profilo): "Email"/"Password" da soli non devono dare LOGIN_REQUIRED
    _set_screens(parts, _screen(_el("Email"), _el("Password"), _el("Il mio conto")))
    assert manager.check_session(DEVICE, BOOK).data.status == LoginStatus.LOGGED_IN


def test_check_session_form_labels_alone_ambiguous(manager, parts):
    _set_screens(parts, _screen(_el("Email"), _el("Password"), _el("Promozioni")))
    assert manager.check_session(DEVICE, BOOK).data.status == LoginStatus.AMBIGUOUS


def test_login_indicator_lists_are_split(manager):
    assert "Accedi" in manager.LOGIN_BUTTON_INDICATORS
    assert not set(manager.LOGIN_BUTTON_INDICATORS) & set(manager.LOGIN_FORM_INDICATORS)
    assert {"Email", "Username", "Password", "PIN", "Nome utente"} == set(manager.LOGIN_FORM_INDICATORS)


def test_find_login_form_elements_uses_form_indicators(manager):
    screen = _screen(
        _el("Nome utente", "android.widget.EditText", EMAIL_BOUNDS),
        _el("PIN", "android.widget.EditText", PASSWORD_BOUNDS),
        _el("Entra", "android.widget.Button", BUTTON_BOUNDS))
    form = manager._find_login_form_elements(screen)
    assert form["username_element"].bounds == EMAIL_BOUNDS
    assert form["password_element"].bounds == PASSWORD_BOUNDS
    assert form["submit_element"].bounds == BUTTON_BOUNDS


def test_has_indicators_pin_not_in_wrappin(manager):
    assert not manager._has_indicators(_screen(_el("Wrappin' More Gold")), ["PIN"])
    assert not manager._has_indicators(_screen(_el("Accedire")), ["Accedi"])
    assert not manager._has_indicators(_screen(_el("Uscire")), ["Esci"])
    assert manager._has_indicators(_screen(_el("Inserisci il PIN")), ["PIN"])


def test_default_config_has_no_bare_saldo():
    for name, cfg in BOOK_NAVIGATION.items():
        assert "Saldo" not in cfg["logged_in_indicators"], name


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


# Layout reale Sisal (debug): EditText vuoti con rid neutro + label View separate
SISAL_EMAIL_BOUNDS = [100, 628, 980, 743]
SISAL_PASSWORD_BOUNDS = [100, 798, 980, 913]
SISAL_BUTTON_BOUNDS = [100, 1118, 980, 1213]


def _sisal_screen(email_text="", password_text=""):
    return _screen(
        _el("Wrappin' More Gold", "android.view.View", [100, 100, 980, 200]),
        _el(email_text, "android.widget.EditText", SISAL_EMAIL_BOUNDS, "_r_0_-form-item"),
        _el("Email / Username", "android.view.View", [125, 658, 492, 713]),
        _el(password_text, "android.widget.EditText", SISAL_PASSWORD_BOUNDS, "password"),
        _el("Password", "android.view.View", [125, 828, 352, 883]),
        _el("Accedi", "android.widget.Button", SISAL_BUTTON_BOUNDS))


def test_find_username_via_label(manager):
    screen = _screen(
        _el("", "android.widget.EditText", SISAL_EMAIL_BOUNDS, "_r_0_-form-item"),
        _el("Email / Username", "android.view.View", [125, 658, 492, 713]))
    form = manager._find_login_form_elements(screen)
    assert form["username_element"].bounds == SISAL_EMAIL_BOUNDS


def test_find_password_via_label(manager):
    screen = _screen(
        _el("", "android.widget.EditText", SISAL_EMAIL_BOUNDS, "_r_0_-form-item"),
        _el("Email / Username", "android.view.View", [125, 658, 492, 713]),
        _el("", "android.widget.EditText", SISAL_PASSWORD_BOUNDS, "field_2"),
        _el("Password", "android.view.View", [125, 828, 352, 883]))
    form = manager._find_login_form_elements(screen)
    assert form["password_element"].bounds == SISAL_PASSWORD_BOUNDS
    assert form["username_element"].bounds == SISAL_EMAIL_BOUNDS


def test_find_nearest_edittext_contained(manager):
    big = _el("", "android.widget.EditText", SISAL_EMAIL_BOUNDS)
    # piccolo EditText con il centro piu' vicino al centro della label, ma che non la contiene
    small = _el("", "android.widget.EditText", [300, 690, 320, 700])
    label = _el("Email / Username", "android.view.View", [125, 658, 492, 713])
    assert manager._find_nearest_edittext(label, [small, big, label]) is big


def test_find_nearest_edittext_closest(manager):
    near = _el("", "android.widget.EditText", [200, 0, 400, 50])
    far = _el("", "android.widget.EditText", [200, 300, 400, 350])
    label = _el("Email", "android.view.View", [0, 0, 100, 50])
    assert manager._find_nearest_edittext(label, [far, near, label]) is near


def test_find_nearest_no_edittext(manager):
    label = _el("Email", "android.view.View", [0, 0, 100, 50])
    assert manager._find_nearest_edittext(label, [label, _el("Accedi", "android.widget.Button")]) is None


def test_find_form_elements_sisal_real(manager):
    form = manager._find_login_form_elements(_sisal_screen())
    assert form["username_element"].bounds == SISAL_EMAIL_BOUNDS
    assert form["password_element"].bounds == SISAL_PASSWORD_BOUNDS
    assert form["submit_element"].bounds == SISAL_BUTTON_BOUNDS


def test_find_form_label_forgot_password_does_not_win(manager):
    screen = _screen(
        # link "dimenticato" sopra il campo email: il suo EditText piu' vicino e' l'email
        _el("Hai dimenticato la password?", "android.view.View", [100, 560, 600, 600]),
        _el("", "android.widget.EditText", SISAL_EMAIL_BOUNDS, "field_1"),
        _el("Email / Username", "android.view.View", [125, 658, 492, 713]),
        _el("", "android.widget.EditText", SISAL_PASSWORD_BOUNDS, "field_2"),
        _el("Password", "android.view.View", [125, 828, 352, 883]))
    form = manager._find_login_form_elements(screen)
    assert form["username_element"].bounds == SISAL_EMAIL_BOUNDS
    assert form["password_element"].bounds == SISAL_PASSWORD_BOUNDS


def test_find_form_same_edittext_conflict(manager):
    # Step 2: un solo campo (password). Il testo con "email" non deve rubarlo.
    screen = _screen(
        _el("Accedi con la tua email", "android.view.View", [100, 500, 700, 560]),
        _el("", "android.widget.EditText", SISAL_EMAIL_BOUNDS, "field"),
        _el("Password", "android.view.View", [125, 658, 352, 713]))
    form = manager._find_login_form_elements(screen)
    assert form["password_element"].bounds == SISAL_EMAIL_BOUNDS
    assert form["username_element"] is None


def test_perform_login_sisal_real_layout(manager, parts):
    _set_screens(parts, _sisal_screen(), _sisal_screen("mario@example.it"),
                 _sisal_screen("mario@example.it", "••••••••"), MOCK_LOGGED_IN_SCREEN)
    res = manager.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    taps = [c.args[1:] for c in parts[1].tap_coordinates.call_args_list]
    assert taps == [(540, 685), (540, 855), (540, 1165)]


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
