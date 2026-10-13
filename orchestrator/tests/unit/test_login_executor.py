import json
import os
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.login_manager import ui_helpers
from orchestrator.modules.login_manager.executor import LoginExecutor
from orchestrator.modules.login_manager.interface import LOGGED_IN_STATUSES, LoginResult, LoginStatus
from orchestrator.modules.login_manager.recipe import LoginRecipe, LoginStep, RecipeStore
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement

DEVICE = "emulator-5554"
BOOK = "SISAL"

EMAIL_BOUNDS = [100, 628, 980, 743]
PASSWORD_BOUNDS = [100, 798, 980, 913]
BUTTON_BOUNDS = [100, 1118, 980, 1213]
HOME_LINK_BOUNDS = [800, 50, 980, 120]


def _screen(*elements, screen_type="login_page"):
    return ScreenContent(elements=list(elements), screen_type=screen_type,
                         confidence=1.0, method="uiautomator")


def _el(text, cls="android.widget.TextView", bounds=None, resource_id=None):
    return UIElement(text=text, bounds=bounds or [0, 0, 0, 0],
                     resource_id=resource_id, class_name=cls)


def _home_screen():
    """Home Sisal: "Accedi" e' un link (View) verso la pagina di login."""
    return _screen(_el("Bonus di benvenuto"),
                   _el("Accedi", "android.view.View", HOME_LINK_BOUNDS),
                   screen_type="home_page")


def _login_page(email="", password=""):
    """Pagina di login Sisal (debug reale): EditText con rid neutro + label View separate."""
    return _screen(
        _el("Wrappin' More Gold", "android.view.View", [100, 100, 980, 200]),
        _el("Accedi", "android.view.View", [100, 300, 400, 360]),  # titolo della pagina
        _el(email, "android.widget.EditText", EMAIL_BOUNDS, "_r_0_-form-item"),
        _el("Email / Username", "android.view.View", [125, 658, 492, 713]),
        _el(password, "android.widget.EditText", PASSWORD_BOUNDS, "password"),
        _el("Password", "android.view.View", [125, 828, 352, 883]),
        _el("Accedi", "android.widget.Button", BUTTON_BOUNDS))


LOGGED_IN_SCREEN = _screen(_el("Saldo disponibile: 125.50€"), _el("Il mio conto"),
                           _el("Logout", "android.widget.Button"), screen_type="home_page")
CAPTCHA_SCREEN = _screen(_el("Non sono un robot", "android.widget.CheckBox"),
                         _el("Accedi", "android.widget.Button"))
TWO_FA_SCREEN = _screen(_el("Inserisci il codice di verifica"), _el("Codice", "android.widget.EditText"))
LOGIN_FAILED_SCREEN = _screen(_el("Credenziali errate. Riprova."), _el("Accedi", "android.widget.Button"))
NEUTRAL_SCREEN = _screen(_el("Promozioni"), _el("Calcio"), screen_type="unknown")


def _recipe(steps=None):
    return LoginRecipe(
        book=BOOK, login_url="https://m.sisal.it",
        steps=steps if steps is not None else [
            LoginStep("tap_button", target_text="Accedi", wait_after_seconds=5,
                      description="Naviga alla pagina di login"),
            LoginStep("tap_field", target_label="Email / Username", trigger_autofill=True,
                      wait_after_seconds=3, description="Tap sul campo email"),
            LoginStep("tap_field", target_label="Password", trigger_autofill=True,
                      wait_after_seconds=3, description="Tap sul campo password"),
            LoginStep("tap_button", target_text="Accedi", wait_after_seconds=5,
                      description="Submit form di login"),
        ],
        has_captcha=True, captcha_type="reCAPTCHA",
        logged_in_indicators=["Logout", "Il mio conto", "Saldo disponibile"],
        login_failed_indicators=["credenziali errate", "riprova"],
        mapped_at="2026-09-20T14:00:00", mapped_by="gpt_vision")


def _reader(*screens):
    """read_screen che restituisce le schermate in sequenza, poi ripete l'ultima."""
    seq = list(screens)

    def _read(device_id, *args, **kwargs):
        screen = seq.pop(0) if len(seq) > 1 else seq[0]
        return Result.ok(screen)

    return _read


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("orchestrator.modules.login_manager.executor.time.sleep"):
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
def store(tmp_path):
    return RecipeStore(str(tmp_path / "recipes.json"))


@pytest.fixture
def executor(parts, store):
    return LoginExecutor(parts[0], parts[1], book_navigator=parts[2], recipe_store=store)


def _set_screens(parts, *screens):
    parts[0].read_screen.side_effect = _reader(*screens)


def _taps(parts):
    return [c.args[1:] for c in parts[1].tap_coordinates.call_args_list]


def _sisal_flow(*after):
    """Letture di perform_login con _recipe(): apertura (HOME), step 1 (HOME), un'unica lettura per ciascuno dei due
    tap_field e per il submit (nessuna rilettura di verifica dopo il tap), poi le schermate di verify_login."""
    return (_home_screen(), _home_screen(), _login_page(), _login_page(), _login_page(), *after)


# ---------------- perform_login ----------------

def test_perform_login_already_logged_in(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, LOGGED_IN_SCREEN)
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGGED_IN
    parts[1].tap_coordinates.assert_not_called()  # recipe non eseguito


def test_perform_login_recipe_not_found(executor, parts):
    res = executor.perform_login(DEVICE, BOOK)
    assert res.success and res.data.status == LoginStatus.RECIPE_NOT_FOUND
    parts[2].open_chrome.assert_not_called()


def test_perform_login_success(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, *_sisal_flow(LOGGED_IN_SCREEN))
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    # link Accedi della home, campo email, campo password, Button (non il titolo) di submit
    assert _taps(parts) == [(890, 85), (540, 685), (540, 855), (540, 1165)]
    parts[2].open_chrome.assert_called_once_with(DEVICE, "https://m.sisal.it")


def test_form_still_visible_at_the_end_is_an_autofill_failure_after_all_steps(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, *_sisal_flow(_login_page()))     # a fine recipe il form di login e' ancora li', senza errori
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.AUTOFILL_FAILED
    assert res.data.details == {"form_still_visible": True} and res.data.screenshot_path
    # nessuno step si e' fermato: tutti i tap sono stati fatti, submit compreso
    assert _taps(parts) == [(890, 85), (540, 685), (540, 855), (540, 1165)]


def test_perform_login_failed_credentials(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, *_sisal_flow(LOGIN_FAILED_SCREEN))
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_FAILED


def test_perform_login_captcha_after_submit(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, *_sisal_flow(CAPTCHA_SCREEN))
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.CAPTCHA_DETECTED
    assert res.data.screenshot_path == "/tmp/shot.png"


def test_perform_login_captcha_on_open(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, CAPTCHA_SCREEN)
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.CAPTCHA_DETECTED
    parts[1].tap_coordinates.assert_not_called()


def test_perform_login_2fa(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, *_sisal_flow(TWO_FA_SCREEN))
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.TWO_FA_REQUIRED


def test_perform_login_navigation_failed(executor, parts, store):
    store.save_recipe(_recipe())
    parts[2].open_chrome.return_value = Result.fail("adb down", "CHROME_OPEN_FAILED")
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.NAVIGATION_FAILED
    parts[0].read_screen.assert_not_called()


def test_perform_login_never_types_text(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, *_sisal_flow(LOGGED_IN_SCREEN))
    executor.perform_login(DEVICE, BOOK)
    parts[1].input_text.assert_not_called()


def test_perform_login_device_lock(parts, store):
    store.save_recipe(_recipe())
    dm = MagicMock()
    dm.acquire_lock.return_value = False
    ex = LoginExecutor(parts[0], parts[1], book_navigator=parts[2], recipe_store=store, device_manager=dm)
    res = ex.perform_login(DEVICE, BOOK)
    assert not res.success and res.error_code == "DEVICE_BUSY"
    dm.release_lock.assert_not_called()

    dm.acquire_lock.return_value = True
    _set_screens(parts, LOGGED_IN_SCREEN)
    ex.perform_login(DEVICE, BOOK)
    dm.release_lock.assert_called_once_with(DEVICE)


# ---------------- check_session ----------------

def test_check_session_logged_in(executor, parts):
    _set_screens(parts, _screen(_el("Logout"), _el("Saldo disponibile")))
    assert executor.check_session(DEVICE, BOOK).data.status == LoginStatus.LOGGED_IN


def test_check_session_needs_login(executor, parts):
    _set_screens(parts, _home_screen())
    assert executor.check_session(DEVICE, BOOK).data.status == LoginStatus.LOGIN_REQUIRED


def test_check_session_ambiguous(executor, parts):
    _set_screens(parts, NEUTRAL_SCREEN)
    assert executor.check_session(DEVICE, BOOK).data.status == LoginStatus.AMBIGUOUS


def test_check_session_benvenuto_not_logged_in(executor, parts):
    _set_screens(parts, _screen(_el("Bonus di benvenuto"), _el("Accedi", "android.view.View", HOME_LINK_BOUNDS)))
    assert executor.check_session(DEVICE, BOOK).data.status == LoginStatus.LOGIN_REQUIRED


def test_check_session_login_button_beats_logged_in(executor, parts):
    _set_screens(parts, _screen(_el("Saldo disponibile"), _el("Accedi", "android.view.View", HOME_LINK_BOUNDS)))
    assert executor.check_session(DEVICE, BOOK).data.status == LoginStatus.LOGIN_REQUIRED


def test_check_session_benvenuto_alone_ambiguous(executor, parts):
    _set_screens(parts, _screen(_el("Benvenuto"), _el("collega il tuo account Fantacalcio")))
    assert executor.check_session(DEVICE, BOOK).data.status == LoginStatus.AMBIGUOUS


# ---------------- _execute_recipe / _execute_step ----------------

def test_execute_recipe_tap_button(executor, parts):
    _set_screens(parts, _home_screen())
    step = LoginStep("tap_button", target_text="Accedi")
    res = executor._execute_step(DEVICE, step, _recipe())
    assert res.success and res.data is True
    assert _taps(parts) == [(890, 85)]


def test_execute_recipe_tap_field(executor, parts):
    _set_screens(parts, _login_page())
    step = LoginStep("tap_field", target_label="Email / Username", trigger_autofill=True)
    res = executor._execute_step(DEVICE, step, _recipe())
    assert res.success and res.data is True
    assert _taps(parts) == [(540, 685)]
    assert parts[0].read_screen.call_count == 1          # nessuna rilettura per verificare il riempimento


def test_tap_field_never_checks_that_the_field_is_filled(executor, parts):
    # il campo resta vuoto (col popup Touch to Fill si riempie solo dopo il tap sul suggerimento): lo step va avanti
    _set_screens(parts, _login_page())
    step = LoginStep("tap_field", target_label="Email / Username", trigger_autofill=True)
    res = executor._execute_step(DEVICE, step, _recipe())
    assert res.success and res.data is True
    assert parts[0].read_screen.call_count == 1
    assert not hasattr(ui_helpers, "is_field_filled") and not hasattr(executor, "_is_field_filled")


def test_execute_recipe_tap_coordinates_and_wait(executor, parts):
    assert executor._execute_step(DEVICE, LoginStep("tap_coordinates", x=10, y=20)).success
    assert _taps(parts) == [(10, 20)]
    assert executor._execute_step(DEVICE, LoginStep("wait", wait_after_seconds=2)).data is True


def test_execute_recipe_check_session_stops(executor, parts):
    recipe = _recipe([LoginStep("check_session"),
                      LoginStep("tap_button", target_text="Accedi")])
    _set_screens(parts, LOGGED_IN_SCREEN)
    res = executor._execute_recipe(DEVICE, recipe)
    assert res.data.status == LoginStatus.LOGGED_IN
    parts[1].tap_coordinates.assert_not_called()


def test_execute_recipe_check_session_continues_when_not_logged(executor, parts):
    recipe = _recipe([LoginStep("check_session"), LoginStep("tap_button", target_text="Accedi")])
    _set_screens(parts, _home_screen(), _home_screen(), LOGGED_IN_SCREEN)
    res = executor._execute_recipe(DEVICE, recipe)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    assert _taps(parts) == [(890, 85)]


def test_execute_recipe_step_failed(executor, parts):
    recipe = _recipe([LoginStep("tap_button", target_text="Entra", description="Pulsante cambiato")])
    _set_screens(parts, _home_screen())
    res = executor._execute_recipe(DEVICE, recipe)
    assert res.data.status == LoginStatus.RECIPE_STEP_FAILED
    assert res.data.details["step_index"] == 0 and res.data.details["target"] == "Entra"
    parts[1].tap_coordinates.assert_not_called()


# ---------------- verify_login ----------------

def test_verify_login_success(executor, parts):
    _set_screens(parts, _screen(_el("Saldo disponibile"), _el("Profilo")))
    assert executor.verify_login(DEVICE, BOOK).data.status == LoginStatus.LOGIN_SUCCESS


def test_verify_login_failed(executor, parts):
    _set_screens(parts, LOGIN_FAILED_SCREEN)
    assert executor.verify_login(DEVICE, BOOK).data.status == LoginStatus.LOGIN_FAILED


def test_verify_login_ambiguous(executor, parts):
    _set_screens(parts, NEUTRAL_SCREEN)
    assert executor.verify_login(DEVICE, BOOK).data.status == LoginStatus.AMBIGUOUS


# ---------------- ui_helpers / wrapper ----------------

def test_find_field_by_label(executor):
    screen = _login_page()
    assert executor._find_field_by_label(screen, "Email / Username").bounds == EMAIL_BOUNDS
    assert executor._find_field_by_label(screen, "Password").bounds == PASSWORD_BOUNDS
    assert executor._find_field_by_label(screen, "Codice fiscale") is None


def test_find_field_by_label_forgot_password_link_does_not_win(executor):
    screen = _screen(
        _el("Hai dimenticato la password?", "android.view.View", [100, 560, 600, 600]),
        _el("", "android.widget.EditText", EMAIL_BOUNDS, "field_1"),
        _el("Email / Username", "android.view.View", [125, 658, 492, 713]),
        _el("", "android.widget.EditText", PASSWORD_BOUNDS, "field_2"),
        _el("Password", "android.view.View", [125, 828, 352, 883]))
    assert executor._find_field_by_label(screen, "Password").bounds == PASSWORD_BOUNDS


def test_find_field_by_label_inside_edittext_text(executor):
    screen = _screen(_el("Email", "android.widget.EditText", EMAIL_BOUNDS))
    assert executor._find_field_by_label(screen, "Email").bounds == EMAIL_BOUNDS


def test_find_element_by_text_prefers_exact_then_button(executor):
    screen = _screen(
        _el("Accedi per vedere il saldo", "android.view.View", [0, 300, 500, 360]),
        _el("Accedi", "android.view.View", [100, 300, 400, 360]),
        _el("Accedi", "android.widget.Button", BUTTON_BOUNDS))
    assert executor._find_element_by_text(screen, "accedi").bounds == BUTTON_BOUNDS
    assert executor._find_element_by_text(screen, "Registrati") is None


def test_find_nearest_edittext_contained():
    big = _el("", "android.widget.EditText", EMAIL_BOUNDS)
    small = _el("", "android.widget.EditText", [300, 690, 320, 700])  # centro piu' vicino, ma non contiene
    label = _el("Email / Username", "android.view.View", [125, 658, 492, 713])
    assert ui_helpers.find_nearest_edittext(label, [small, big, label]) is big


def test_find_nearest_edittext_closest():
    near = _el("", "android.widget.EditText", [200, 0, 400, 50])
    far = _el("", "android.widget.EditText", [200, 300, 400, 350])
    label = _el("Email", "android.view.View", [0, 0, 100, 50])
    assert ui_helpers.find_nearest_edittext(label, [far, near, label]) is near


def test_find_nearest_no_edittext():
    label = _el("Email", "android.view.View", [0, 0, 100, 50])
    assert ui_helpers.find_nearest_edittext(label, [label]) is None


def test_has_indicators_whole_word(executor):
    assert not executor._has_indicators(_screen(_el("Wrappin' More Gold")), ["PIN"])
    assert not executor._has_indicators(_screen(_el("Uscire")), ["Esci"])
    assert executor._has_indicators(_screen(_el("Inserisci il PIN")), ["PIN"])


# ---------------- RecipeStore ----------------

def test_recipe_store_save_load(store):
    assert store.save_recipe(_recipe()).success
    res = store.load_recipe("sisal")  # chiave case-insensitive
    assert res.success and res.data == _recipe()
    assert store.has_recipe("SISAL")
    assert not [f for f in os.listdir(os.path.dirname(store.path)) if f.endswith(".tmp")]


def test_recipe_store_not_found(store):
    res = store.load_recipe("NON-ESISTE")
    assert res.success and res.data is None
    assert not store.has_recipe("NON-ESISTE")


def test_recipe_store_list_books(store):
    store.save_recipe(_recipe())
    other = _recipe()
    other.book = "SNAI"
    store.save_recipe(other)
    assert store.list_books() == ["SISAL", "SNAI"]


def test_recipe_store_ignores_unknown_step_keys(store):
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump({"SISAL": {"login_url": "https://m.sisal.it", "steps": [
            {"action": "tap_button", "target_text": "Accedi", "campo_nuovo": 1}]}}, f)
    res = store.load_recipe("SISAL")
    assert res.success and res.data.steps[0].target_text == "Accedi"


def test_recipe_store_rejects_invalid_step(store):
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump({"SISAL": {"login_url": "https://m.sisal.it", "steps": [{"action": "tap_button"}]}}, f)
    res = store.load_recipe("SISAL")
    assert not res.success and res.error_code == "RECIPE_INVALID"


def test_recipe_store_corrupt_file_is_not_overwritten(store):
    with open(store.path, "w", encoding="utf-8") as f:
        f.write("{ non json")
    assert store.load_recipe("SISAL").error_code == "RECIPE_FILE_CORRUPT"
    assert store.save_recipe(_recipe()).error_code == "RECIPE_FILE_CORRUPT"
    with open(store.path, encoding="utf-8") as f:
        assert f.read() == "{ non json"


# ---------------- verify_login: una rilettura se la pagina non ha finito di caricare ----------------

def test_verify_login_ambiguous_then_success(executor, parts):
    _set_screens(parts, NEUTRAL_SCREEN, LOGGED_IN_SCREEN)
    with patch("orchestrator.modules.login_manager.executor.time.sleep") as sleep:
        res = executor.verify_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    assert parts[0].read_screen.call_count == 2
    sleep.assert_called_once_with(3)
    parts[0]._screenshot.assert_not_called()  # lo screenshot HITL scatta solo per l'esito finale ambiguo


def test_verify_login_ambiguous_then_failed(executor, parts):
    _set_screens(parts, NEUTRAL_SCREEN, LOGIN_FAILED_SCREEN)
    res = executor.verify_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_FAILED
    assert parts[0].read_screen.call_count == 2


def test_verify_login_still_ambiguous(executor, parts):
    _set_screens(parts, NEUTRAL_SCREEN, NEUTRAL_SCREEN)
    res = executor.verify_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.AMBIGUOUS
    assert parts[0].read_screen.call_count == 2  # una sola rilettura, non di piu'
    assert parts[0]._screenshot.call_count == 1


@pytest.mark.parametrize("screen, status", [
    (LOGGED_IN_SCREEN, LoginStatus.LOGIN_SUCCESS),
    (LOGIN_FAILED_SCREEN, LoginStatus.LOGIN_FAILED),
    (CAPTCHA_SCREEN, LoginStatus.CAPTCHA_DETECTED),
    (TWO_FA_SCREEN, LoginStatus.TWO_FA_REQUIRED),
])
def test_verify_login_definitive_outcome_does_not_wait(executor, parts, screen, status):
    _set_screens(parts, screen)
    with patch("orchestrator.modules.login_manager.executor.time.sleep") as sleep:
        res = executor.verify_login(DEVICE, BOOK)
    assert res.data.status == status
    assert parts[0].read_screen.call_count == 1
    sleep.assert_not_called()


def test_verify_login_second_read_error_is_propagated(executor, parts):
    parts[0].read_screen.side_effect = [Result.ok(NEUTRAL_SCREEN), Result.fail("boom", "ADB_TIMEOUT")]
    res = executor.verify_login(DEVICE, BOOK)
    assert not res.success and res.error_code == "ADB_TIMEOUT"


def test_perform_login_page_loads_late(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, *_sisal_flow(NEUTRAL_SCREEN, LOGGED_IN_SCREEN))  # prima lettura post-submit: pagina non ancora caricata
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS


# ---------------- ensure_logged_in: sessioni che scadono a meta' operazione ----------------

def _status_result(status):
    return Result.ok(LoginResult(status, BOOK, DEVICE, "x"))


def test_ensure_logged_in_already_active(executor, parts):
    _set_screens(parts, LOGGED_IN_SCREEN)  # senza recipe: check_session usa BOOK_NAVIGATION
    res = executor.ensure_logged_in(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGGED_IN and res.data.is_logged_in
    parts[1].tap_coordinates.assert_not_called()


def test_ensure_logged_in_needs_login(executor):
    with patch.object(executor, "check_session", return_value=_status_result(LoginStatus.LOGIN_REQUIRED)), \
         patch.object(executor, "perform_login", return_value=_status_result(LoginStatus.LOGIN_SUCCESS)) as perform:
        res = executor.ensure_logged_in(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS and res.data.is_logged_in
    perform.assert_called_once_with(DEVICE, BOOK, lock_device=False)


def test_ensure_logged_in_login_fails(executor):
    with patch.object(executor, "check_session", return_value=_status_result(LoginStatus.LOGIN_REQUIRED)), \
         patch.object(executor, "perform_login", return_value=_status_result(LoginStatus.LOGIN_FAILED)):
        res = executor.ensure_logged_in(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_FAILED and not res.data.is_logged_in


@pytest.mark.parametrize("status", [
    LoginStatus.CAPTCHA_DETECTED, LoginStatus.TWO_FA_REQUIRED, LoginStatus.AMBIGUOUS,
    LoginStatus.NAVIGATION_FAILED,
])
def test_ensure_logged_in_other_states_are_returned_without_login(executor, status):
    with patch.object(executor, "check_session", return_value=_status_result(status)), \
         patch.object(executor, "perform_login") as perform:
        res = executor.ensure_logged_in(DEVICE, BOOK)
    assert res.data.status == status
    perform.assert_not_called()


def test_ensure_logged_in_check_session_error_is_propagated(executor):
    failure = Result.fail("lettura fallita", "ADB_TIMEOUT")
    with patch.object(executor, "check_session", return_value=failure), \
         patch.object(executor, "perform_login") as perform:
        res = executor.ensure_logged_in(DEVICE, BOOK)
    assert not res.success and res.error_code == "ADB_TIMEOUT"
    perform.assert_not_called()


def test_ensure_logged_in_recipe_not_found(executor, parts):
    _set_screens(parts, _home_screen())  # sloggato, ma il book non e' mai stato mappato
    res = executor.ensure_logged_in(DEVICE, BOOK)
    assert res.data.status == LoginStatus.RECIPE_NOT_FOUND


def test_ensure_logged_in_full_relogin(executor, parts, store):
    store.save_recipe(_recipe())
    _set_screens(parts, _home_screen(), *_sisal_flow(LOGGED_IN_SCREEN))   # check_session + perform_login
    res = executor.ensure_logged_in(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS and res.data.is_logged_in
    assert _taps(parts) == [(890, 85), (540, 685), (540, 855), (540, 1165)]


def test_ensure_logged_in_does_not_retake_the_device_lock(parts, store):
    # chi chiama ensure_logged_in sta gia' facendo l'operazione e tiene il lock (non rientrante)
    store.save_recipe(_recipe())
    dm = MagicMock()
    dm.acquire_lock.return_value = False
    ex = LoginExecutor(parts[0], parts[1], book_navigator=parts[2], recipe_store=store, device_manager=dm)
    _set_screens(parts, _home_screen(), *_sisal_flow(LOGGED_IN_SCREEN))
    res = ex.ensure_logged_in(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    dm.acquire_lock.assert_not_called()
    dm.release_lock.assert_not_called()
    # perform_login chiamato direttamente continua a prendere il lock
    assert ex.perform_login(DEVICE, BOOK).error_code == "DEVICE_BUSY"


def test_login_result_is_logged_in():
    assert set(LOGGED_IN_STATUSES) == {LoginStatus.LOGGED_IN, LoginStatus.LOGIN_SUCCESS}
    for status in LoginStatus:
        result = LoginResult(status, BOOK, DEVICE, "x")
        assert result.is_logged_in == (status in LOGGED_IN_STATUSES)


# ---------------- tap_field non verifica il riempimento: col popup Touch to Fill il campo e' vuoto fino al tap ----------

POPUP_BOUNDS = [0, 1700, 1080, 1800]


def _popup_page():
    """Pagina di login con il popup Chrome Touch to Fill: il suo Button "Accedi" e' l'ULTIMO della schermata."""
    return _screen(*_login_page().elements, _el("Accedi", "android.widget.Button", POPUP_BOUNDS))


def _two_fields_and_submit():
    return _recipe([
        LoginStep("tap_field", target_label="Email / Username", trigger_autofill=True, wait_after_seconds=3),
        LoginStep("tap_field", target_label="Password", trigger_autofill=True, wait_after_seconds=3),
        LoginStep("tap_button", target_text="Accedi", wait_after_seconds=5),
    ])


def test_two_fields_and_submit_all_run_then_verify_login_decides(executor, parts, store):
    store.save_recipe(_two_fields_and_submit())
    assert store.load_recipe(BOOK).data.steps[0].trigger_autofill is True       # recipe vecchio: il flag e' ignorato
    # campi sempre vuoti dopo i tap (i campi si riempiono col popup): nessuno step si ferma
    _set_screens(parts, _home_screen(), _login_page(), _login_page(), _login_page(), LOGGED_IN_SCREEN)
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    assert _taps(parts) == [(540, 685), (540, 855), (540, 1165)]
    assert parts[0].read_screen.call_count == 5      # apertura + 3 step + verify: nessuna rilettura di verifica


def test_two_fields_and_submit_with_the_form_still_visible_is_an_autofill_failure(executor, parts, store):
    store.save_recipe(_two_fields_and_submit())
    _set_screens(parts, _home_screen(), _login_page(), _login_page(), _login_page(), _login_page())
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.AUTOFILL_FAILED
    assert len(_taps(parts)) == 3                    # eseguiti comunque tutti gli step
    assert parts[0].read_screen.call_count == 6      # apertura + 3 step + verify con una rilettura


def test_wrong_credentials_stay_a_login_failure_not_an_autofill_failure(executor, parts, store):
    store.save_recipe(_two_fields_and_submit())
    _set_screens(parts, _home_screen(), _login_page(), _login_page(), _login_page(), LOGIN_FAILED_SCREEN)
    assert executor.perform_login(DEVICE, BOOK).data.status == LoginStatus.LOGIN_FAILED


def test_field_then_touch_to_fill_popup_button(executor, parts, store):
    store.save_recipe(_recipe([
        LoginStep("tap_field", target_label="Email / Username", trigger_autofill=True, wait_after_seconds=3),
        LoginStep("tap_button", target_text="Accedi", wait_after_seconds=5, description="Accedi del popup"),
    ]))
    _set_screens(parts, _home_screen(), _login_page(), _popup_page(), LOGGED_IN_SCREEN)
    res = executor.perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    # campo email, poi il Button "Accedi" del popup (l'ultimo), non quello del form
    assert _taps(parts) == [(540, 685), (540, 1750)]


def test_verify_login_form_visible_then_logged_in_is_a_success(executor, parts):
    _set_screens(parts, _login_page(), LOGGED_IN_SCREEN)      # la pagina post-login non aveva ancora finito di caricare
    res = executor.verify_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS and parts[0].read_screen.call_count == 2
    parts[0]._screenshot.assert_not_called()


def test_verify_login_form_visible_twice_is_an_autofill_failure(executor, parts):
    _set_screens(parts, _login_page())
    res = executor.verify_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.AUTOFILL_FAILED and res.data.details == {"form_still_visible": True}
    assert parts[0].read_screen.call_count == 2
