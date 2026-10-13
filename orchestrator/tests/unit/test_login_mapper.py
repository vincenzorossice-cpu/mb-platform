import base64
import json
import os
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from orchestrator.core.result import Result
from orchestrator.modules.login_manager.executor import LoginExecutor
from orchestrator.modules.login_manager.interface import LoginStatus
from orchestrator.modules.login_manager.mapper import LoginMapper
from orchestrator.modules.login_manager.recipe import LoginRecipe, LoginStep, RecipeStore
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement

DEVICE = "emulator-5554"
BOOK = "SISAL"
OPENAI_PATH = "orchestrator.modules.login_manager.mapper.openai.OpenAI"
ADB_RUN_PATH = "orchestrator.modules.login_manager.adb_input.subprocess.run"

EMAIL_BOUNDS = [100, 628, 980, 743]
PASSWORD_BOUNDS = [100, 798, 980, 913]
BUTTON_BOUNDS = [100, 1118, 980, 1213]
POPUP_BOUNDS = [0, 1700, 1080, 1800]
HOME_LINK_BOUNDS = [800, 50, 980, 120]
_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _screen(*elements):
    return ScreenContent(elements=list(elements), screen_type="login_page",
                         confidence=1.0, method="uiautomator")


def _el(text, cls="android.widget.TextView", bounds=None, resource_id=None):
    return UIElement(text=text, bounds=bounds or [0, 0, 0, 0], resource_id=resource_id, class_name=cls)


_LOGIN_FORM = [
    _el("", "android.widget.EditText", EMAIL_BOUNDS, "_r_0_-form-item"),
    _el("Email / Username", "android.view.View", [125, 658, 492, 713]),
    _el("", "android.widget.EditText", PASSWORD_BOUNDS, "password"),
    _el("Password", "android.view.View", [125, 828, 352, 883]),
    _el("Accedi", "android.widget.Button", BUTTON_BOUNDS),
]
HOME = _screen(_el("Bonus di benvenuto"), _el("Accedi", "android.view.View", HOME_LINK_BOUNDS))
LOGIN_PAGE = _screen(*_LOGIN_FORM)
POPUP_PAGE = _screen(*_LOGIN_FORM, _el("Accedi", "android.widget.Button", POPUP_BOUNDS))  # Touch to Fill
LOGGED_IN = _screen(_el("Logout", "android.widget.Button"), _el("Il mio conto"),
                    _el("Saldo disponibile: 125,50 €"), _el("Deposita"))
NEUTRAL = _screen(_el("Promozioni"), _el("Calcio"))


def _act(action, **kw):
    base = {"action": action, "target_text": None, "target_label": None, "x": None, "y": None,
            "description": f"azione {action}"}
    base.update(kw)
    return base


def _completion(content):
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = content
    return completion


def _client(*actions):
    client = MagicMock()
    client.chat.completions.create.side_effect = [_completion(json.dumps(a)) for a in actions]
    return client


def _reader(*screens):
    seq = list(screens)

    def _read(device_id, *args, **kwargs):
        return Result.ok(seq.pop(0) if len(seq) > 1 else seq[0])

    return _read


def _pngs(directory):
    return [f for f in os.listdir(directory) if f.endswith(".png")]


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("orchestrator.modules.login_manager.mapper.time.sleep"), \
         patch("orchestrator.modules.login_manager.executor.time.sleep"):
        yield


@pytest.fixture
def parts(tmp_path):
    counter = {"n": 0}

    def fake_screenshot(device_id):
        counter["n"] += 1
        path = tmp_path / f"shot{counter['n']}.png"
        path.write_bytes(b"\x89PNG fake")
        return Result.ok(str(path))

    screen_reader = MagicMock()
    screen_reader._screenshot.side_effect = fake_screenshot
    touch_injector = MagicMock()
    touch_injector.tap_coordinates.return_value = Result.ok(True)
    book_navigator = MagicMock()
    book_navigator.open_chrome.return_value = Result.ok(True)
    return screen_reader, touch_injector, book_navigator


@pytest.fixture
def store(tmp_path):
    return RecipeStore(str(tmp_path / "recipes.json"))


@pytest.fixture
def mapper(parts, store):
    return LoginMapper(parts[0], parts[1], openai_api_key="sk-test",
                       book_navigator=parts[2], recipe_store=store)


def _screens(parts, *screens):
    parts[0].read_screen.side_effect = _reader(*screens)


def _taps(parts):
    return [c.args[1:] for c in parts[1].tap_coordinates.call_args_list]


# ---------------- map_book_login: loop interattivo ----------------

def test_map_book_login_interactive_success(mapper, parts, store, tmp_path):
    _screens(parts, HOME, LOGIN_PAGE, POPUP_PAGE, LOGGED_IN)
    client = _client(
        _act("tap_button", target_text="Accedi"),
        _act("tap_field", target_label="Email / Username"),
        _act("tap_button", target_text="Accedi", description="Tappo Accedi del popup Touch to Fill"),
        _act("done"))
    with patch(OPENAI_PATH, return_value=client) as openai_cls:
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.success
    recipe = res.data
    assert [s.action for s in recipe.steps] == ["tap_button", "tap_field", "tap_button"]
    assert recipe.steps[1].target_label == "Email / Username" and recipe.steps[1].trigger_autofill is False
    assert recipe.steps[2].target_text == "Accedi"
    # link Accedi della home, campo email, Button del popup (l'ULTIMO "Accedi")
    assert _taps(parts) == [(890, 85), (540, 685), (540, 1750)]
    assert client.chat.completions.create.call_count == 4
    assert store.load_recipe(BOOK).data == recipe
    assert recipe.mapped_by == "gpt_vision" and recipe.has_captcha is False
    assert openai_cls.call_args.kwargs["api_key"] == "sk-test"
    parts[2].open_chrome.assert_called_once_with(DEVICE, "https://www.sisal.it")
    assert _pngs(tmp_path) == []


def test_map_book_login_two_step_navigation(mapper, parts, store):
    _screens(parts, HOME, LOGIN_PAGE, LOGGED_IN)
    client = _client(_act("tap_button", target_text="Accedi"),
                     _act("tap_field", target_label="Email / Username"),
                     _act("done"))
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK, login_url="https://m.sisal.it")
    assert res.success
    assert [s.action for s in res.data.steps] == ["tap_button", "tap_field"]
    assert res.data.steps[0].target_text == "Accedi" and res.data.steps[0].wait_after_seconds == 5
    assert res.data.login_url == "https://m.sisal.it"
    assert _taps(parts)[0] == (890, 85)


def test_map_book_login_already_logged_in(mapper, parts, store, tmp_path):
    _screens(parts, LOGGED_IN)
    with patch(OPENAI_PATH) as openai_cls:
        res = mapper.map_book_login(DEVICE, BOOK)
    assert not res.success and res.error_code == "ALREADY_LOGGED_IN"
    openai_cls.assert_not_called()
    parts[0]._screenshot.assert_not_called()
    assert not store.has_recipe(BOOK)


def test_map_book_login_done_at_first_step_without_actions(mapper, parts):
    _screens(parts, NEUTRAL)  # non riconosciuta come loggata in locale, ma GPT dice done
    with patch(OPENAI_PATH, return_value=_client(_act("done"))):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "ALREADY_LOGGED_IN"


def test_map_book_login_max_steps(mapper, parts, store):
    _screens(parts, LOGIN_PAGE)
    alternating = [_act("tap_button", target_text="Accedi") if i % 2 == 0
                   else _act("tap_field", target_label="Email / Username") for i in range(10)]
    client = _client(*alternating)
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert not res.success and res.error_code == "MAP_MAX_STEPS"
    assert client.chat.completions.create.call_count == 10
    assert not store.has_recipe(BOOK)


def test_map_book_login_login_failed(mapper, parts, store):
    # login_failed va confermato: serve una seconda risposta login_failed consecutiva
    _screens(parts, HOME, LOGIN_PAGE)
    client = _client(_act("tap_button", target_text="Accedi"),
                     _act("login_failed", description="Credenziali errate per mario@example.it"),
                     _act("login_failed", description="Credenziali errate per mario@example.it"))
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_LOGIN_FAILED"
    assert "2 risposte consecutive" in res.error
    assert "mario@example.it" not in res.error
    assert client.chat.completions.create.call_count == 3
    assert not store.has_recipe(BOOK)


def test_map_book_login_login_failed_then_done_continues(mapper, parts, store):
    # falso positivo di GPT: la pagina e' gia' loggata ma al primo giro dice login_failed
    _screens(parts, HOME, LOGGED_IN, LOGGED_IN)
    client = _client(_act("tap_button", target_text="Accedi"), _act("login_failed"), _act("done"))
    with patch("orchestrator.modules.login_manager.mapper.time.sleep") as sleep, \
         patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.success
    assert [s.action for s in res.data.steps] == ["tap_button"]
    assert store.has_recipe(BOOK)
    sleep.assert_any_call(5)  # attesa prima della nuova richiesta a GPT
    assert client.chat.completions.create.call_count == 3
    # la schermata del giro login_failed non entra in seen_texts: gli indicatori finali restano
    assert "Deposita" in res.data.logged_in_indicators


def test_map_book_login_login_failed_executes_nothing(mapper, parts):
    _screens(parts, HOME)
    client = _client(_act("login_failed"), _act("login_failed"))
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_LOGIN_FAILED"
    parts[1].tap_coordinates.assert_not_called()  # nessun nuovo submit sui giri login_failed
    parts[0]._screenshot.assert_called()
    assert parts[0]._screenshot.call_count == 2  # nuovo screenshot a ogni giro


def test_map_book_login_login_failed_not_consecutive(mapper, parts, store):
    _screens(parts, HOME, HOME, LOGIN_PAGE, LOGGED_IN)
    client = _client(_act("login_failed"),                      # 1a (streak 1)
                     _act("tap_button", target_text="Accedi"),  # azione: azzera il contatore
                     _act("login_failed"),                      # di nuovo la 1a, non si conferma
                     _act("done"))
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.success and store.has_recipe(BOOK)


def test_map_book_login_max_login_failed_retries_configurable(parts, store):
    _screens(parts, HOME)
    immediate = LoginMapper(parts[0], parts[1], openai_api_key="sk-test", book_navigator=parts[2],
                            recipe_store=store, max_login_failed_retries=1)
    client = _client(_act("login_failed"))
    with patch(OPENAI_PATH, return_value=client):
        res = immediate.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_LOGIN_FAILED" and client.chat.completions.create.call_count == 1

    patient = LoginMapper(parts[0], parts[1], openai_api_key="sk-test", book_navigator=parts[2],
                          recipe_store=store, max_login_failed_retries=3)
    client = _client(*[_act("login_failed")] * 3)
    with patch(OPENAI_PATH, return_value=client):
        res = patient.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_LOGIN_FAILED" and client.chat.completions.create.call_count == 3
    assert "3 risposte consecutive" in res.error


def test_map_book_login_login_failed_retries_count_in_max_steps(parts, store):
    _screens(parts, HOME)
    mapper = LoginMapper(parts[0], parts[1], openai_api_key="sk-test", book_navigator=parts[2],
                         recipe_store=store, max_login_failed_retries=50)
    client = _client(*[_act("login_failed")] * 10)
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_MAX_STEPS"
    assert client.chat.completions.create.call_count == 10


def test_map_book_login_stuck_stops_hammering_submit(mapper, parts):
    _screens(parts, HOME)
    client = _client(*[_act("tap_button", target_text="Accedi")] * 5)
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_STUCK"
    assert len(_taps(parts)) == 3  # il quarto tentativo identico non viene eseguito


def test_map_book_login_failed_action_is_reported_to_gpt(mapper, parts):
    _screens(parts, HOME, HOME, LOGIN_PAGE)
    client = _client(_act("tap_button", target_text="Entra"),
                     _act("tap_button", target_text="Accedi"),
                     _act("done"))
    with patch(OPENAI_PATH, return_value=client):
        mapper.map_book_login(DEVICE, BOOK)
    second_prompt = client.chat.completions.create.call_args_list[1].kwargs["messages"][0]["content"][0]["text"]
    assert "Azioni già eseguite" in second_prompt and "fallita" in second_prompt
    first_prompt = client.chat.completions.create.call_args_list[0].kwargs["messages"][0]["content"][0]["text"]
    assert "Azioni già eseguite" not in first_prompt


def test_map_book_login_ignores_the_informational_recaptcha_badge(mapper, parts, store):
    # Sisal: in fondo alla pagina di login c'e' solo il badge informativo, non un CAPTCHA attivo
    badge_page = _screen(*_LOGIN_FORM, _el("This site is protected by reCAPTCHA", "android.view.View",
                                           [100, 1300, 980, 1360]))
    _screens(parts, HOME, badge_page, LOGGED_IN)
    client = _client(_act("tap_button", target_text="Accedi"),
                     _act("tap_field", target_label="Email / Username"),
                     _act("done"))
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.success and res.data.has_captcha is False and res.data.captcha_type is None


def test_map_book_login_detects_captcha(mapper, parts):
    captcha_page = _screen(*_LOGIN_FORM, _el("Non sono un robot", "android.widget.CheckBox", [100, 1250, 400, 1330]))
    _screens(parts, HOME, captcha_page, LOGGED_IN)
    client = _client(_act("tap_button", target_text="Accedi"),
                     _act("tap_field", target_label="Email / Username"),
                     _act("done"))
    with patch(OPENAI_PATH, return_value=client):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.data.has_captcha is True and res.data.captcha_type == "reCAPTCHA"


def test_map_book_login_preconditions(mapper, parts, store, monkeypatch):
    assert mapper.map_book_login(DEVICE, "BOOK-SCONOSCIUTO").error_code == "NO_LOGIN_URL"
    parts[2].open_chrome.return_value = Result.fail("adb down", "CHROME_OPEN_FAILED")
    assert mapper.map_book_login(DEVICE, BOOK).error_code == "NAVIGATION_FAILED"
    parts[0].read_screen.assert_not_called()

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    no_key = LoginMapper(parts[0], parts[1], book_navigator=parts[2], recipe_store=store)
    res = no_key.map_book_login(DEVICE, BOOK)
    assert not res.success and res.error == "OPENAI_API_KEY non configurata"


# ---------------- _gpt_guided_step ----------------

def test_gpt_guided_step(mapper, parts, tmp_path):
    _screens(parts, LOGIN_PAGE)
    client = _client(_act("tap_field", target_label="Email / Username", description="Tap sul campo email"))
    with patch(OPENAI_PATH, return_value=client):
        res = mapper._gpt_guided_step(DEVICE, 2)
    assert res.success
    data = res.data
    assert data["action"] == "tap_field" and data["target_label"] == "Email / Username"
    assert data["status"] == "continue" and data["screen_content"] is LOGIN_PAGE
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o"
    prompt = kwargs["messages"][0]["content"][0]["text"]
    assert "Elementi UI visibili:" in prompt and "text='Email / Username'" in prompt
    assert "EditText" in prompt and "resource_id='password'" in prompt
    assert kwargs["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert _pngs(tmp_path) == []


def test_gpt_guided_step_status_mapping(mapper, parts):
    _screens(parts, LOGIN_PAGE)
    with patch(OPENAI_PATH, return_value=_client(_act("done"), _act("login_failed"))):
        assert mapper._gpt_guided_step(DEVICE, 2).data["status"] == "done"
        assert mapper._gpt_guided_step(DEVICE, 3).data["status"] == "failed"


def test_gpt_guided_step_invalid_reply(mapper, parts):
    _screens(parts, LOGIN_PAGE)
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _completion('{"action": "vola"}'), _completion("non json"),
        _completion('```json\n{"action": "press_enter", "x": "540.0"}\n```')]
    with patch(OPENAI_PATH, return_value=client):
        assert mapper._gpt_guided_step(DEVICE, 2).error_code == "GPT_VISION_PARSE_ERROR"
        assert mapper._gpt_guided_step(DEVICE, 2).error_code == "GPT_VISION_PARSE_ERROR"
        ok = mapper._gpt_guided_step(DEVICE, 2)
    assert ok.success and ok.data["action"] == "press_enter" and ok.data["x"] == 540


def test_gpt_guided_step_first_step_logged_in_sends_nothing(mapper, parts):
    _screens(parts, LOGGED_IN)
    with patch(OPENAI_PATH) as openai_cls:
        res = mapper._gpt_guided_step(DEVICE, 1)
    assert res.error_code == "ALREADY_LOGGED_IN"
    openai_cls.assert_not_called()


# ---------------- _execute_gpt_action ----------------

def test_execute_gpt_action_tap_button_last(mapper, parts):
    screen = _screen(_el("Accedi", "android.widget.Button", [0, 50, 300, 120]),
                     _el("Accedi", "android.widget.Button", BUTTON_BOUNDS),
                     _el("Accedi", "android.widget.Button", POPUP_BOUNDS))
    data = _act("tap_button", target_text="Accedi")
    res = mapper._execute_gpt_action(DEVICE, data, screen)
    assert res.success and _taps(parts) == [(540, 1750)]
    assert (data["tapped_x"], data["tapped_y"]) == (540, 1750)


def test_execute_gpt_action_tap_button_falls_back_to_link(mapper, parts):
    res = mapper._execute_gpt_action(DEVICE, _act("tap_button", target_text="Accedi"), HOME)
    assert res.success and _taps(parts) == [(890, 85)]
    missing = mapper._execute_gpt_action(DEVICE, _act("tap_button", target_text="Entra"), HOME)
    assert missing.error_code == "ELEMENT_NOT_FOUND"


def test_execute_gpt_action_tap_field(mapper, parts):
    res = mapper._execute_gpt_action(DEVICE, _act("tap_field", target_label="Email / Username"), LOGIN_PAGE)
    assert res.success and _taps(parts) == [(540, 685)]
    missing = mapper._execute_gpt_action(DEVICE, _act("tap_field", target_label="Codice fiscale"), LOGIN_PAGE)
    assert missing.error_code == "ELEMENT_NOT_FOUND"


def test_execute_gpt_action_tap_coordinates(mapper, parts):
    inside = _act("tap_coordinates", x=540, y=1165)
    assert mapper._execute_gpt_action(DEVICE, inside, LOGIN_PAGE).success
    assert _taps(parts) == [(540, 1165)]
    assert inside["resolved_text"] == "Accedi"  # cade dentro un Button risolvibile in modo identico

    nowhere = _act("tap_coordinates", x=10, y=1500)
    assert mapper._execute_gpt_action(DEVICE, nowhere, LOGIN_PAGE).success
    assert "resolved_text" not in nowhere

    assert mapper._execute_gpt_action(DEVICE, _act("tap_coordinates"), LOGIN_PAGE).error_code == "INVALID_ACTION"


def test_execute_gpt_action_press_enter(mapper):
    with patch(ADB_RUN_PATH, return_value=MagicMock(returncode=0, stderr="")) as run:
        res = mapper._execute_gpt_action(DEVICE, _act("press_enter"), LOGIN_PAGE)
    assert res.success
    run.assert_called_once()
    assert run.call_args.args[0] == ["adb", "-s", DEVICE, "shell", "input", "keyevent", "66"]
    with patch(ADB_RUN_PATH, return_value=MagicMock(returncode=1, stderr="no device")):
        assert mapper._execute_gpt_action(DEVICE, _act("press_enter"), LOGIN_PAGE).error_code == "ADB_ERROR"


def test_find_last_button_by_text(mapper):
    b1, b2, b3 = ([0, 0, 10, 10], [0, 20, 10, 30], [0, 40, 10, 50])
    screen = _screen(_el("Accedi", "android.widget.Button", b1), _el("Accedi", "android.widget.Button", b2),
                     _el("Accedi", "android.widget.Button", b3), _el("Accedi", "android.view.View", [0, 60, 10, 70]))
    assert mapper._find_last_button_by_text(screen, "Accedi").bounds == b3
    assert mapper._find_last_button_by_text(screen, "Registrati") is None


# ---------------- _generate_recipe_from_steps ----------------

def test_generate_recipe_from_steps(mapper):
    steps = [{"action": "tap_button", "target_text": "Accedi", "description": "Naviga al login"},
             {"action": "tap_field", "target_label": "Email / Username"},
             {"action": "tap_button", "target_text": "Accedi"}]
    recipe = mapper._generate_recipe_from_steps(BOOK, "https://m.sisal.it", steps, LOGGED_IN)
    assert isinstance(recipe, LoginRecipe)
    assert [(s.action, s.target_text or s.target_label) for s in recipe.steps] == [
        ("tap_button", "Accedi"), ("tap_field", "Email / Username"), ("tap_button", "Accedi")]
    assert recipe.steps[0].wait_after_seconds == 5 and recipe.steps[1].wait_after_seconds == 3
    assert recipe.steps[0].description == "Naviga al login"
    assert recipe.login_url == "https://m.sisal.it" and recipe.book == BOOK
    assert recipe.mapped_by == "gpt_vision" and recipe.mapped_at


def test_generate_recipe_cleans_redundant(mapper):
    steps = [{"action": "tap_button", "target_text": "Accedi"},
             {"action": "tap_field", "target_label": "Email / Username"},
             {"action": "tap_field", "target_label": "email / username"},  # ripetuto
             {"action": "tap_button", "target_text": "Accedi"},
             {"action": "tap_button", "target_text": "Accedi"}]            # ripetuto
    recipe = mapper._generate_recipe_from_steps(BOOK, "u", steps, LOGGED_IN)
    assert [s.action for s in recipe.steps] == ["tap_button", "tap_field", "tap_button"]


def test_generate_recipe_drops_steps_without_effect(mapper):
    steps = [{"action": "tap_button", "target_text": "Accedi", "changed": False},
             {"action": "tap_field", "target_label": "Email / Username", "changed": True}]
    recipe = mapper._generate_recipe_from_steps(BOOK, "u", steps, LOGGED_IN)
    assert [s.action for s in recipe.steps] == ["tap_field"]


def test_generate_recipe_keeps_email_out_of_the_recipe(mapper):
    steps = [{"action": "tap_button", "target_text": "mario.rossi@gmail.com", "tapped_x": 540, "tapped_y": 1500,
              "description": "Tappo il suggerimento mario.rossi@gmail.com"},
             {"action": "tap_button", "target_text": "mario", "resolved_text": "mario.rossi@gmail.com",
              "tapped_x": 540, "tapped_y": 1400}]
    recipe = mapper._generate_recipe_from_steps(BOOK, "u", steps, LOGGED_IN)
    assert [(s.action, s.x, s.y) for s in recipe.steps] == [("tap_coordinates", 540, 1500),
                                                             ("tap_coordinates", 540, 1400)]
    assert "@" not in json.dumps([vars(s) for s in recipe.steps])


def test_generate_recipe_converts_resolved_coordinates_and_press_enter(mapper):
    steps = [{"action": "tap_coordinates", "x": 540, "y": 1165, "resolved_text": "Accedi"},
             {"action": "press_enter"},
             {"action": "tap_coordinates", "x": 10, "y": 1500}]
    recipe = mapper._generate_recipe_from_steps(BOOK, "u", steps, LOGGED_IN)
    assert [s.action for s in recipe.steps] == ["tap_button", "press_enter", "tap_coordinates"]
    assert recipe.steps[0].target_text == "Accedi"
    assert (recipe.steps[2].x, recipe.steps[2].y) == (10, 1500)


def test_generate_recipe_logged_in_indicators(mapper):
    final = _screen(_el("Logout"), _el("Il mio conto"), _el("Saldo disponibile: 125,50 €"),
                    _el("mario@example.it"), _el("Deposita"), _el("Promozioni"), _el("Mario Rossi 1990"))
    recipe = mapper._generate_recipe_from_steps(
        BOOK, "u", [{"action": "press_enter"}], final, exclude_texts={"Promozioni"})
    assert recipe.logged_in_indicators == ["Logout", "Il mio conto", "Saldo disponibile", "Deposita"]


def test_generate_recipe_indicators_fall_back_to_defaults(mapper):
    recipe = mapper._generate_recipe_from_steps(BOOK, "u", [{"action": "press_enter"}], _screen(_el("12")))
    assert "Logout" in recipe.logged_in_indicators


# ---------------- privacy ----------------

def test_privacy_screenshots_deleted(mapper, parts, tmp_path):
    seen_during_call = []

    def create(**kwargs):
        seen_during_call.append(_pngs(tmp_path))  # il PNG e' gia' stato cancellato quando si chiama GPT
        return _completion(json.dumps(_act("done")))

    client = MagicMock()
    client.chat.completions.create.side_effect = create
    _screens(parts, NEUTRAL)
    with patch(OPENAI_PATH, return_value=client):
        mapper.map_book_login(DEVICE, BOOK)
    assert seen_during_call == [[]]
    assert _pngs(tmp_path) == []

    failing = MagicMock()
    failing.chat.completions.create.side_effect = openai.APIConnectionError(request=_REQUEST)
    with patch(OPENAI_PATH, return_value=failing):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "GPT_VISION_UNREACHABLE"
    assert _pngs(tmp_path) == []


# ---------------- Executor: recipe prodotti dal Mapper ----------------

def _executor(parts, store):
    return LoginExecutor(parts[0], parts[1], book_navigator=parts[2], recipe_store=store)


def test_recipe_step_failed_in_executor(parts, store):
    store.save_recipe(LoginRecipe(
        book=BOOK, login_url="https://m.sisal.it",
        steps=[LoginStep("tap_button", target_text="Accedi", description="Naviga al login"),
               LoginStep("tap_button", target_text="Entra", description="Pulsante cambiato")]))
    _screens(parts, HOME, HOME, HOME)
    res = _executor(parts, store).perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.RECIPE_STEP_FAILED
    assert "ri-mappare" in res.data.message and "LoginMapper" in res.data.message
    assert res.data.details["step_index"] == 1 and res.data.details["action_required"] == "remap_book"


def test_executor_replays_press_enter_and_last_button(parts, store):
    store.save_recipe(LoginRecipe(
        book=BOOK, login_url="https://m.sisal.it",
        steps=[LoginStep("tap_button", target_text="Accedi"), LoginStep("press_enter")]))
    assert store.load_recipe(BOOK).success  # press_enter e' un'azione valida
    # header e popup hanno entrambi un Button "Accedi": si tocca l'ultimo, come in mapping
    _screens(parts, HOME, POPUP_PAGE, LOGGED_IN)
    with patch(ADB_RUN_PATH, return_value=MagicMock(returncode=0, stderr="")) as run:
        res = _executor(parts, store).perform_login(DEVICE, BOOK)
    assert res.data.status == LoginStatus.LOGIN_SUCCESS
    assert _taps(parts) == [(540, 1750)]
    assert run.call_args.args[0][-3:] == ["input", "keyevent", "66"]


# ---------------- GPT Vision: chiamata e utility ----------------

def test_image_to_base64(mapper, tmp_path):
    image = tmp_path / "s.png"
    image.write_bytes(b"\x89PNG\r\nbytes")
    assert base64.b64decode(mapper._image_to_base64(str(image))) == b"\x89PNG\r\nbytes"


def test_call_gpt_vision_success(mapper):
    client = MagicMock()
    client.chat.completions.create.return_value = _completion('{"action": "done"}')
    with patch(OPENAI_PATH, return_value=client):
        res = mapper._call_gpt_vision("QUJD", "decidi")
    assert res.success and res.data == '{"action": "done"}'
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o" and kwargs["response_format"] == {"type": "json_object"}
    content = kwargs["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "decidi"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


@pytest.mark.parametrize("error, code", [
    (openai.APITimeoutError(request=_REQUEST), "GPT_VISION_TIMEOUT"),
    (openai.APIConnectionError(request=_REQUEST), "GPT_VISION_UNREACHABLE"),
    (openai.InternalServerError("sk-segreta-1234 boom", response=httpx.Response(500, request=_REQUEST), body=None),
     "GPT_VISION_HTTP_ERROR"),
])
def test_call_gpt_vision_errors(mapper, error, code):
    client = MagicMock()
    client.chat.completions.create.side_effect = error
    with patch(OPENAI_PATH, return_value=client):
        res = mapper._call_gpt_vision("QUJD", "decidi")
    assert not res.success and res.error_code == code
    assert "sk-segreta" not in res.error  # il messaggio dell'API puo' citare la chiave


def test_call_gpt_vision_empty_reply(mapper):
    client = MagicMock()
    client.chat.completions.create.return_value = _completion("  ")
    with patch(OPENAI_PATH, return_value=client):
        assert mapper._call_gpt_vision("QUJD", "x").error_code == "GPT_VISION_EMPTY"
