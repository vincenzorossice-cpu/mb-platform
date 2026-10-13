import base64
import json
import os
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from orchestrator.core.result import Result
from orchestrator.modules.login_manager.mapper import LoginMapper
from orchestrator.modules.login_manager.recipe import RecipeStore
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement

DEVICE = "emulator-5554"
BOOK = "SISAL"
OPENAI_PATH = "orchestrator.modules.login_manager.mapper.openai.OpenAI"

EMAIL_BOUNDS = [100, 628, 980, 743]
PASSWORD_BOUNDS = [100, 798, 980, 913]
BUTTON_BOUNDS = [100, 1118, 980, 1213]
HOME_LINK_BOUNDS = [800, 50, 980, 120]
_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _screen(*elements):
    return ScreenContent(elements=list(elements), screen_type="login_page",
                         confidence=1.0, method="uiautomator")


def _el(text, cls="android.widget.TextView", bounds=None, resource_id=None):
    return UIElement(text=text, bounds=bounds or [0, 0, 0, 0], resource_id=resource_id, class_name=cls)


HOME = _screen(_el("Bonus di benvenuto"), _el("Accedi", "android.view.View", HOME_LINK_BOUNDS))
LOGIN_PAGE = _screen(
    _el("", "android.widget.EditText", EMAIL_BOUNDS, "_r_0_-form-item"),
    _el("Email / Username", "android.view.View", [125, 658, 492, 713]),
    _el("", "android.widget.EditText", PASSWORD_BOUNDS, "password"),
    _el("Password", "android.view.View", [125, 828, 352, 883]),
    _el("Accedi", "android.widget.Button", BUTTON_BOUNDS))
LOGGED_IN = _screen(_el("Logout"), _el("Il mio conto"), _el("Saldo disponibile: 10€"))

FORM_ANALYSIS = {
    "page_type": "login_form", "has_login_button": False, "login_button_text": None, "has_form": True,
    "form_fields": [{"label": "Email / Username", "type": "text"}, {"label": "Password", "type": "password"}],
    "submit_button_text": "Accedi", "has_captcha": True, "captcha_type": "reCAPTCHA",
    "has_2fa": False, "notes": "",
}
HOME_ANALYSIS = {
    "page_type": "home_not_logged_in", "has_login_button": True, "login_button_text": "Accedi",
    "has_form": False, "form_fields": [], "submit_button_text": None, "has_captcha": False,
    "captcha_type": None, "has_2fa": False, "notes": "",
}


def _completion(content):
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = content
    return completion


def _reader(*screens):
    seq = list(screens)

    def _read(device_id, *args, **kwargs):
        return Result.ok(seq.pop(0) if len(seq) > 1 else seq[0])

    return _read


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("orchestrator.modules.login_manager.mapper.time.sleep"):
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


def _gpt_replies(*analyses):
    client = MagicMock()
    client.chat.completions.create.side_effect = [_completion(json.dumps(a)) for a in analyses]
    return client


# ---------------- map_book_login ----------------

def test_map_book_login_success(mapper, parts, store, tmp_path):
    parts[0].read_screen.side_effect = _reader(LOGIN_PAGE)
    with patch(OPENAI_PATH, return_value=_gpt_replies(FORM_ANALYSIS)) as openai_cls:
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.success
    recipe = res.data
    assert [s.action for s in recipe.steps] == ["tap_field", "tap_field", "tap_button"]
    assert recipe.steps[0].target_label == "Email / Username" and recipe.steps[0].trigger_autofill
    assert recipe.steps[-1].target_text == "Accedi"
    assert recipe.has_captcha and recipe.captcha_type == "reCAPTCHA"
    assert recipe.mapped_by == "gpt_vision" and recipe.mapped_at
    assert store.load_recipe(BOOK).data == recipe
    parts[2].open_chrome.assert_called_once_with(DEVICE, "https://www.sisal.it")
    assert openai_cls.call_args.kwargs["api_key"] == "sk-test"
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".png")]  # screenshot cancellati


def test_map_book_login_two_step(mapper, parts, store):
    parts[0].read_screen.side_effect = _reader(HOME, LOGIN_PAGE)
    with patch(OPENAI_PATH, return_value=_gpt_replies(HOME_ANALYSIS, FORM_ANALYSIS)):
        res = mapper.map_book_login(DEVICE, BOOK, login_url="https://m.sisal.it")
    assert res.success
    steps = res.data.steps
    assert [s.action for s in steps] == ["tap_button", "tap_field", "tap_field", "tap_button"]
    assert steps[0].target_text == "Accedi" and steps[0].wait_after_seconds == 5
    parts[1].tap_coordinates.assert_called_once_with(DEVICE, 890, 85)
    assert res.data.login_url == "https://m.sisal.it"
    assert store.has_recipe(BOOK)


def test_map_book_login_no_api_key(parts, store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    mapper = LoginMapper(parts[0], parts[1], book_navigator=parts[2], recipe_store=store)
    res = mapper.map_book_login(DEVICE, BOOK)
    assert not res.success and res.error == "OPENAI_API_KEY non configurata"
    parts[2].open_chrome.assert_not_called()


def test_map_book_login_already_logged_in_sends_nothing(mapper, parts, store):
    parts[0].read_screen.side_effect = _reader(LOGGED_IN)
    with patch(OPENAI_PATH) as openai_cls:
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "ALREADY_LOGGED_IN"
    openai_cls.assert_not_called()
    parts[0]._screenshot.assert_not_called()
    assert not store.has_recipe(BOOK)


def test_map_book_login_snaps_labels_to_real_text(mapper, parts):
    analysis = dict(FORM_ANALYSIS, form_fields=[{"label": "Email", "type": "text"},
                                                {"label": "password", "type": "password"}],
                    submit_button_text="Accedi ora")
    parts[0].read_screen.side_effect = _reader(LOGIN_PAGE)
    with patch(OPENAI_PATH, return_value=_gpt_replies(analysis)):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.success
    assert [s.target_label for s in res.data.steps[:2]] == ["Email / Username", "Password"]
    assert res.data.steps[-1].target_text == "Accedi"


def test_map_book_login_label_not_on_screen(mapper, parts, store):
    analysis = dict(FORM_ANALYSIS, form_fields=[{"label": "Codice fiscale", "type": "text"}])
    parts[0].read_screen.side_effect = _reader(LOGIN_PAGE)
    with patch(OPENAI_PATH, return_value=_gpt_replies(analysis)):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_LABEL_NOT_FOUND"
    assert not store.has_recipe(BOOK)


def test_map_book_login_unknown_page(mapper, parts):
    parts[0].read_screen.side_effect = _reader(LOGIN_PAGE)
    with patch(OPENAI_PATH, return_value=_gpt_replies(dict(FORM_ANALYSIS, page_type="unknown"))):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_FAILED"


def test_map_book_login_second_page_not_a_form(mapper, parts):
    parts[0].read_screen.side_effect = _reader(HOME, HOME)
    with patch(OPENAI_PATH, return_value=_gpt_replies(HOME_ANALYSIS, HOME_ANALYSIS)):
        res = mapper.map_book_login(DEVICE, BOOK)
    assert res.error_code == "MAP_FAILED"


def test_map_book_login_no_url(mapper, parts):
    res = mapper.map_book_login(DEVICE, "BOOK-SCONOSCIUTO")
    assert res.error_code == "NO_LOGIN_URL"


# ---------------- _analyze_screenshot / _call_gpt_vision ----------------

def test_analyze_screenshot_success(mapper, tmp_path):
    image = tmp_path / "s.png"
    image.write_bytes(b"png")
    reply = json.dumps({"page_type": "login_form", "form_fields": [{"label": " Password ", "type": "password"}, {"x": 1}],
                        "submit_button_text": "Accedi"})
    with patch(OPENAI_PATH, return_value=_gpt_replies(json.loads(reply))):
        res = mapper._analyze_screenshot(str(image), BOOK)
    assert res.success
    assert res.data["page_type"] == "login_form"
    assert res.data["form_fields"] == [{"label": "Password", "type": "password"}]
    assert res.data["has_captcha"] is False and res.data["login_button_text"] is None


def test_analyze_screenshot_no_api_key(parts, store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    mapper = LoginMapper(parts[0], parts[1], recipe_store=store)
    res = mapper._analyze_screenshot("/tmp/x.png", BOOK)
    assert not res.success and res.error == "OPENAI_API_KEY non configurata"


def test_analyze_screenshot_invalid_values(mapper, tmp_path):
    image = tmp_path / "s.png"
    image.write_bytes(b"png")
    client = MagicMock()
    client.chat.completions.create.return_value = _completion('```json\n{"page_type": "boh"}\n```')
    with patch(OPENAI_PATH, return_value=client):
        assert mapper._analyze_screenshot(str(image), BOOK).data["page_type"] == "unknown"
    client.chat.completions.create.return_value = _completion("non json")
    with patch(OPENAI_PATH, return_value=client):
        assert mapper._analyze_screenshot(str(image), BOOK).error_code == "GPT_VISION_PARSE_ERROR"
    assert mapper._analyze_screenshot(str(tmp_path / "manca.png"), BOOK).error_code == "GPT_VISION_IMAGE_READ_ERROR"


def test_image_to_base64(mapper, tmp_path):
    image = tmp_path / "s.png"
    image.write_bytes(b"\x89PNG\r\nbytes")
    assert base64.b64decode(mapper._image_to_base64(str(image))) == b"\x89PNG\r\nbytes"


def test_call_gpt_vision_success(mapper):
    client = MagicMock()
    client.chat.completions.create.return_value = _completion('{"page_type": "login_form"}')
    with patch(OPENAI_PATH, return_value=client):
        res = mapper._call_gpt_vision("QUJD", "analizza")
    assert res.success and res.data == '{"page_type": "login_form"}'
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["response_format"] == {"type": "json_object"}
    content = kwargs["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "analizza"}
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
        res = mapper._call_gpt_vision("QUJD", "analizza")
    assert not res.success and res.error_code == code
    assert "sk-segreta" not in res.error  # il messaggio dell'API puo' citare la chiave


def test_call_gpt_vision_empty_reply(mapper):
    client = MagicMock()
    client.chat.completions.create.return_value = _completion("  ")
    with patch(OPENAI_PATH, return_value=client):
        assert mapper._call_gpt_vision("QUJD", "x").error_code == "GPT_VISION_EMPTY"


# ---------------- _generate_recipe ----------------

def test_generate_recipe_from_form_page(mapper):
    res = mapper._generate_recipe([FORM_ANALYSIS], BOOK, "https://m.sisal.it")
    assert res.success
    assert [(s.action, s.target_label or s.target_text) for s in res.data.steps] == [
        ("tap_field", "Email / Username"), ("tap_field", "Password"), ("tap_button", "Accedi")]
    assert all(s.trigger_autofill for s in res.data.steps[:2])
    assert "Logout" in res.data.logged_in_indicators and res.data.login_failed_indicators


def test_generate_recipe_from_home_page(mapper):
    res = mapper._generate_recipe([HOME_ANALYSIS, FORM_ANALYSIS], BOOK, "https://m.sisal.it")
    assert res.success
    assert [s.action for s in res.data.steps] == ["tap_button", "tap_field", "tap_field", "tap_button"]
    assert res.data.steps[0].target_text == "Accedi"
    assert res.data.login_url == "https://m.sisal.it"


def test_generate_recipe_incomplete_analysis(mapper):
    no_fields = dict(FORM_ANALYSIS, form_fields=[])
    no_submit = dict(FORM_ANALYSIS, submit_button_text=None)
    home_no_button = dict(HOME_ANALYSIS, login_button_text=None)
    assert mapper._generate_recipe([], BOOK, "u").error_code == "MAP_FAILED"
    assert mapper._generate_recipe([no_fields], BOOK, "u").error_code == "MAP_FAILED"
    assert mapper._generate_recipe([no_submit], BOOK, "u").error_code == "MAP_FAILED"
    assert mapper._generate_recipe([home_no_button, FORM_ANALYSIS], BOOK, "u").error_code == "MAP_FAILED"
    assert mapper._generate_recipe([HOME_ANALYSIS], BOOK, "u").error_code == "MAP_FAILED"
