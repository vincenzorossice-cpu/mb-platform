import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.session import SlotSessionResult
from orchestrator.scripts import run_promo_complete as script


def _args(tmp_path, *extra):
    # --skip-login: i test del flusso navigazione + gioco restano quelli di prima del login automatico
    return script.parse_args(["--device", "D1", "--promo-name", "P", "--slot-name", "Slot X", "--target", "30",
                              "--demo", "--skip-login", "--log-dir", str(tmp_path), *extra])


class FakeClient:
    model = "gpt-6.1-sol"

    def __init__(self, usage=None):
        self._usage = usage or {}

    def usage_summary(self):
        return "GPT fake"


class FakeAutopilot:
    def __init__(self, screen_reader=None, touch_injector=None):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.gpt_client = FakeClient({"m": {"calls": 2, "in": 1, "out": 1, "cost": 0.02}})
        self.hitl_strategy = None
        self.shutdowns = 0

    def shutdown(self, device):
        self.shutdowns += 1
        return Result.ok(True)


@pytest.fixture
def env(monkeypatch, tmp_path):
    autopilot = FakeAutopilot()
    played = []
    monkeypatch.setattr(script.run_autopilot, "_configure_logging", lambda d, s: str(tmp_path / "x.log"))
    monkeypatch.setattr(script.run_autopilot, "build_autopilot_for", lambda **kw: Result.ok(autopilot))
    monkeypatch.setattr(script, "GPTDecisionClient", FakeClient, raising=False)
    import orchestrator.modules.decision_engine.gpt_client as gc
    monkeypatch.setattr(gc, "GPTDecisionClient", lambda model=None: FakeClient({"s": {"calls": 3, "in": 1, "out": 1, "cost": 0.1}}))
    monkeypatch.setattr(script.model_metrics, "log_run", lambda **kw: played.append(kw))
    monkeypatch.setattr(script.model_metrics, "start_run", lambda *a, **k: "run")
    exits = []
    monkeypatch.setattr(script, "exit_slot", lambda *a, **k: exits.append(1))
    from orchestrator.modules.slot_autopilot.slot_intro import IntroOutcome
    monkeypatch.setattr(script, "build_slot_intro", lambda autopilot: SimpleNamespace(
        run=lambda device, slot: Result.ok(IntroOutcome(rounds=1, taps=0, ready=True))))
    return SimpleNamespace(autopilot=autopilot, metrics=played, exits=exits, tmp=tmp_path)


def _session(monkeypatch, result, calls):
    import orchestrator.modules.slot_autopilot.session as session
    monkeypatch.setattr(session, "play_session", lambda *a, **k: calls.append(a) or result)


def test_main_refuses_without_demo(monkeypatch):
    monkeypatch.setattr(script, "run", lambda args: pytest.fail("non deve partire"))
    with pytest.raises(SystemExit) as info:
        script.main(["--device", "D", "--promo-name", "P", "--slot-name", "S", "--target", "1"])
    assert info.value.code == 2


def test_defaults_and_exclusive_flags(tmp_path):
    args = _args(tmp_path)
    assert (args.book, args.max_steps) == ("SISAL", 200)
    with pytest.raises(SystemExit):
        _args(tmp_path, "--already-active", "--not-active")


def test_navigation_failure_skips_game(env, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(script, "navigate_to_slot", lambda *a: Result.fail("boom", "X"))
    _session(monkeypatch, None, calls)
    assert script.run(_args(tmp_path)) == 1
    assert calls == [] and env.autopilot.shutdowns == 1 and env.exits == []
    assert env.metrics[0]["navigation_ok"] is False


def test_success_plays_then_exits_slot(env, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(script, "navigate_to_slot", lambda *a: Result.ok(7))
    _session(monkeypatch, SlotSessionResult(Decimal("30"), 100, failed=False, target=Decimal("30")), calls)
    assert script.run(_args(tmp_path)) == 0
    assert len(calls) == 1 and calls[0][2:5] == ("Slot X", Decimal("30.0"), 200)
    assert env.exits == [1] and env.autopilot.shutdowns == 1
    assert env.metrics[0]["cost_total"] == pytest.approx(0.12)


def test_failed_session_exit_code_and_critical_stop_does_not_leave_slot(env, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(script, "navigate_to_slot", lambda *a: Result.ok(7))
    _session(monkeypatch, SlotSessionResult(Decimal("0"), 0, failed=True, stop_reason="PREPARE_FAILED"), calls)
    assert script.run(_args(tmp_path)) == 1
    assert env.exits == []


def test_noncritical_failure_still_exits_slot(env, monkeypatch, tmp_path):
    monkeypatch.setattr(script, "navigate_to_slot", lambda *a: Result.ok(7))
    _session(monkeypatch, SlotSessionResult(Decimal("5"), 9, failed=True, stop_reason="NO_EFFECT"), [])
    assert script.run(_args(tmp_path)) == 1
    assert env.exits == [1]


def test_shutdown_on_keyboard_interrupt(env, monkeypatch, tmp_path):
    def interrupted(*a):
        raise KeyboardInterrupt
    monkeypatch.setattr(script, "navigate_to_slot", interrupted)
    assert script.run(_args(tmp_path)) == 1
    assert env.autopilot.shutdowns == 1


def test_total_cost_sums_and_flags_unknown_price():
    a = FakeClient({"m": {"calls": 1, "in": 0, "out": 0, "cost": 0.5}})
    b = FakeClient({"n": {"calls": 2, "in": 0, "out": 0, "cost": None}})
    assert script.total_cost() == {"calls": 0, "cost": 0.0, "complete": True}
    total = script.total_cost(a, b, None)
    assert total == {"calls": 3, "cost": 0.5, "complete": False}
    assert "parziale" in script.cost_line(a, b) and "COSTO AI TOTALE" in script.cost_line(a, b)


class FakeReader:
    def __init__(self, texts, ok=True):
        self.texts, self.ok = texts, ok

    def read_screen(self, device):
        if not self.ok:
            return Result.fail("x", "E")
        return Result.ok(SimpleNamespace(elements=[SimpleNamespace(text=t) for t in self.texts]))


class FakeInjector:
    def __init__(self, ok=True, raises=False):
        self.taps, self.ok, self.raises = [], ok, raises

    def tap_element(self, device, hint):
        if self.raises:
            raise RuntimeError("adb")
        self.taps.append(hint)
        return Result.ok(True) if self.ok else Result.fail("no", "TAP_MISSED")


def test_exit_slot_found(caplog):
    sleeps, inj = [], FakeInjector()
    with caplog.at_level(logging.INFO):
        result = script.exit_slot(FakeReader(["Gioca", "Esci"]), inj, "D", sleep=sleeps.append)
    assert result.success and inj.taps == ["Esci"] and sleeps == [3]
    assert "Uscita dalla slot effettuata" in caplog.text


@pytest.mark.parametrize("reader,inj", [
    (FakeReader(["Gioca"]), FakeInjector()), (FakeReader([], ok=False), FakeInjector()),
    (FakeReader(["Esci"]), FakeInjector(ok=False)), (FakeReader(["Esci"]), FakeInjector(raises=True)),
])
def test_exit_slot_never_fails_hard(reader, inj, caplog):
    sleeps = []
    with caplog.at_level(logging.WARNING):
        result = script.exit_slot(reader, inj, "D", sleep=sleeps.append)
    assert not result.success and sleeps == [] and caplog.records


# ======================== login automatico prima della navigazione ====================================================

from orchestrator.modules.login_manager.interface import LoginResult, LoginStatus  # noqa: E402


def _login_args(tmp_path, *extra):
    """Come _args ma SENZA --skip-login: il login e' attivo (e' il comportamento di default)."""
    return script.parse_args(["--device", "D1", "--promo-name", "P", "--slot-name", "Slot X", "--target", "30",
                              "--demo", "--log-dir", str(tmp_path), *extra])


def _login(status, message="", shot=None):
    return Result.ok(LoginResult(status, "SISAL", "D1", message, screenshot_path=shot))


class LoginState:
    def __init__(self):
        self.ensure, self.check, self.perform, self.map = [], [], [], []
        self.calls, self.executors, self.mappers = [], [], []
        self.recipe_known = True


@pytest.fixture
def login_env(monkeypatch):
    state = LoginState()

    class FakeExecutor:
        def __init__(self, screen_reader, touch_injector):
            self.screen_reader, self.touch_injector = screen_reader, touch_injector
            self.recipe_store = SimpleNamespace(has_recipe=lambda book: state.recipe_known)
            state.executors.append(self)

        def ensure_logged_in(self, device, book):
            state.calls.append("ensure")
            return state.ensure.pop(0)

        def check_session(self, device, book):
            state.calls.append("check")
            return state.check.pop(0)

        def perform_login(self, device, book):
            state.calls.append("perform")
            return state.perform.pop(0)

    class FakeMapper:
        def __init__(self, screen_reader, touch_injector):
            self.screen_reader, self.touch_injector = screen_reader, touch_injector
            state.mappers.append(self)

        def map_book_login(self, device, book):
            state.calls.append("map")
            return state.map.pop(0)

    monkeypatch.setattr(script, "LoginExecutor", FakeExecutor)
    monkeypatch.setattr(script, "LoginMapper", FakeMapper)
    return state


def _game(monkeypatch, state, session_calls=None):
    """Navigazione e gioco finti che registrano l'ordine delle chiamate insieme a quelle del login."""
    seen_args = []

    def navigate(args, client, reader, injector):
        state.calls.append("navigate")
        seen_args.append(args)
        return Result.ok(7)

    monkeypatch.setattr(script, "navigate_to_slot", navigate)
    _session(monkeypatch, SlotSessionResult(Decimal("30"), 100, failed=False, target=Decimal("30")),
             session_calls if session_calls is not None else [])
    return seen_args


class TestLoginBeforeNavigation:
    def test_active_session_skips_the_login_and_goes_on(self, env, login_env, monkeypatch, tmp_path, caplog):
        login_env.ensure = [_login(LoginStatus.LOGGED_IN)]
        _game(monkeypatch, login_env)
        with caplog.at_level(logging.INFO):
            assert script.run(_login_args(tmp_path)) == 0
        assert login_env.calls == ["ensure", "navigate"]
        assert "Login: controllo sessione per SISAL su D1" in caplog.text
        assert "Login: sessione attiva, skip login" in caplog.text

    def test_expired_session_with_a_recipe_logs_in_and_goes_on(self, env, login_env, monkeypatch, tmp_path, caplog):
        login_env.ensure = [_login(LoginStatus.LOGIN_SUCCESS)]          # ensure_logged_in esegue il recipe da solo
        _game(monkeypatch, login_env)
        with caplog.at_level(logging.INFO):
            assert script.run(_login_args(tmp_path)) == 0
        assert login_env.calls == ["ensure", "navigate"] and login_env.mappers == []
        assert "Login: serve login" in caplog.text and "recipe trovato" in caplog.text
        assert "Login: riuscito" in caplog.text

    def test_a_book_without_recipe_is_mapped_by_gpt_then_checked_again(self, env, login_env, monkeypatch, tmp_path,
                                                                        caplog):
        login_env.recipe_known = False
        login_env.ensure = [_login(LoginStatus.RECIPE_NOT_FOUND), _login(LoginStatus.LOGGED_IN)]
        login_env.map = [Result.ok(SimpleNamespace(steps=[1, 2, 3]))]
        _game(monkeypatch, login_env)
        with caplog.at_level(logging.INFO):
            assert script.run(_login_args(tmp_path)) == 0
        assert login_env.calls == ["ensure", "map", "ensure", "navigate"]
        assert "Login: nessun recipe, mappo con GPT" in caplog.text

    def test_failed_login_stops_before_navigation_and_still_shuts_down(self, env, login_env, monkeypatch, tmp_path,
                                                                        caplog):
        login_env.ensure = [_login(LoginStatus.LOGIN_FAILED)]
        calls = []
        _game(monkeypatch, login_env, calls)
        with caplog.at_level(logging.INFO):
            assert script.run(_login_args(tmp_path)) == 1
        assert "navigate" not in login_env.calls and calls == []          # niente navigazione, niente gioco
        assert env.autopilot.shutdowns == 1 and env.exits == []
        assert "Login: fallito (credenziali errate" in caplog.text and "non navigo e non gioco" in caplog.text

    def test_captcha_stops_and_asks_for_the_operator(self, env, login_env, monkeypatch, tmp_path, caplog):
        login_env.ensure = [_login(LoginStatus.CAPTCHA_DETECTED, shot="/tmp/captcha.png")]
        calls = []
        _game(monkeypatch, login_env, calls)
        with caplog.at_level(logging.INFO):
            assert script.run(_login_args(tmp_path)) == 1
        assert "navigate" not in login_env.calls and calls == []
        assert "Login: CAPTCHA rilevato, richiesto intervento operatore" in caplog.text
        assert "/tmp/captcha.png" in caplog.text                          # lo screenshot per l'operatore

    def test_skip_login_leaves_the_flow_exactly_as_before(self, env, login_env, monkeypatch, tmp_path):
        _game(monkeypatch, login_env)
        assert script.run(_args(tmp_path)) == 0                           # _args passa --skip-login
        assert login_env.executors == [] and login_env.mappers == [] and login_env.calls == ["navigate"]

    def test_the_executor_gets_the_autopilot_reader_and_injector(self, env, login_env, monkeypatch, tmp_path):
        env.autopilot.screen_reader, env.autopilot.touch_injector = object(), object()
        login_env.ensure = [_login(LoginStatus.LOGGED_IN)]
        _game(monkeypatch, login_env)
        script.run(_login_args(tmp_path))
        assert login_env.executors[0].screen_reader is env.autopilot.screen_reader
        assert login_env.executors[0].touch_injector is env.autopilot.touch_injector

    def test_navigation_receives_the_same_book_and_url_after_the_login(self, env, login_env, monkeypatch, tmp_path):
        login_env.ensure = [_login(LoginStatus.LOGIN_SUCCESS)]
        seen = _game(monkeypatch, login_env)
        script.run(_login_args(tmp_path, "--book", "SISAL", "--url", "https://www.sisal.it/bonus/promozioni"))
        assert seen[0].book == "SISAL" and seen[0].url == "https://www.sisal.it/bonus/promozioni"

    def test_keyboard_interrupt_during_the_login_is_handled(self, env, login_env, monkeypatch, tmp_path):
        def interrupted(*a, **k):
            raise KeyboardInterrupt
        monkeypatch.setattr(script, "login_before_navigation", interrupted)
        assert script.run(_login_args(tmp_path)) == 1
        assert env.autopilot.shutdowns == 1


class TestLoginOutcomes:
    """login_before_navigation direttamente: ogni stato che non e' 'loggato' ferma, con il messaggio giusto."""

    def _go(self, login_env, tmp_path, *extra, autopilot=None, allow_mapping=True):
        return script.login_before_navigation(_login_args(tmp_path, *extra), "SR", "TI", autopilot,
                                              allow_mapping=allow_mapping)

    @pytest.mark.parametrize("status, phrase", [
        (LoginStatus.LOGIN_FAILED, "Login: fallito (credenziali errate"),
        (LoginStatus.CAPTCHA_DETECTED, "CAPTCHA rilevato, richiesto intervento operatore"),
        (LoginStatus.TWO_FA_REQUIRED, "2FA richiesta"),
        (LoginStatus.AUTOFILL_FAILED, "Autofill Chrome non funzionante"),
        (LoginStatus.RECIPE_STEP_FAILED, "login_recipes.json"),
        (LoginStatus.AMBIGUOUS, "stato della sessione non determinabile"),
        (LoginStatus.NAVIGATION_FAILED, "apertura del book non riuscita"),
    ])
    def test_every_non_logged_in_state_stops(self, login_env, tmp_path, caplog, status, phrase):
        login_env.ensure = [_login(status, "dettaglio")]
        with caplog.at_level(logging.INFO):
            result = self._go(login_env, tmp_path)
        assert not result.success and result.error_code == status.name
        assert phrase in caplog.text and "dettaglio" in caplog.text

    def test_a_failed_call_is_propagated(self, login_env, tmp_path):
        login_env.ensure = [Result.fail("lettura fallita", "ADB_TIMEOUT")]
        result = self._go(login_env, tmp_path)
        assert not result.success and result.error_code == "ADB_TIMEOUT"

    def test_a_stale_recipe_tells_how_to_remap(self, login_env, tmp_path, caplog):
        login_env.ensure = [_login(LoginStatus.RECIPE_STEP_FAILED)]
        with caplog.at_level(logging.INFO):
            self._go(login_env, tmp_path)
        assert "elimina la voce SISAL" in caplog.text and "--login-only" in caplog.text

    def test_no_gpt_forbids_the_mapping(self, login_env, tmp_path, caplog):
        login_env.recipe_known = False
        login_env.ensure = [_login(LoginStatus.RECIPE_NOT_FOUND)]
        with caplog.at_level(logging.INFO):
            result = self._go(login_env, tmp_path, "--no-gpt")
        assert not result.success and result.error_code == "RECIPE_NOT_FOUND"
        assert login_env.mappers == [] and "--no-gpt" in caplog.text

    def test_a_failed_mapping_stops_with_its_code(self, login_env, tmp_path):
        login_env.recipe_known = False
        login_env.ensure = [_login(LoginStatus.RECIPE_NOT_FOUND)]
        login_env.map = [Result.fail("OPENAI_API_KEY non configurata", "OPENAI_API_KEY_MISSING")]
        result = self._go(login_env, tmp_path)
        assert not result.success and result.error_code == "OPENAI_API_KEY_MISSING"
        assert login_env.calls == ["ensure", "map"]                        # nessun secondo controllo

    def test_the_mapped_book_must_end_up_logged_in(self, login_env, tmp_path):
        login_env.recipe_known = False
        login_env.ensure = [_login(LoginStatus.RECIPE_NOT_FOUND), _login(LoginStatus.AMBIGUOUS)]
        login_env.map = [Result.ok(SimpleNamespace(steps=[1]))]
        result = self._go(login_env, tmp_path)
        assert not result.success and result.error_code == "AMBIGUOUS"

    def test_a_reentry_never_maps(self, login_env, tmp_path):
        login_env.ensure = [_login(LoginStatus.RECIPE_NOT_FOUND)]
        result = self._go(login_env, tmp_path, allow_mapping=False)
        assert not result.success and login_env.mappers == []

    def test_login_required_is_handled_defensively(self, login_env, tmp_path):
        login_env.ensure = [_login(LoginStatus.LOGIN_REQUIRED)]
        login_env.perform = [_login(LoginStatus.LOGIN_SUCCESS)]
        assert self._go(login_env, tmp_path).success and login_env.calls == ["ensure", "perform"]

        login_env.calls.clear()
        login_env.recipe_known = False
        login_env.ensure = [_login(LoginStatus.LOGIN_REQUIRED), _login(LoginStatus.LOGGED_IN)]
        login_env.map = [Result.ok(SimpleNamespace(steps=[1]))]
        assert self._go(login_env, tmp_path).success and login_env.calls == ["ensure", "map", "ensure"]

    def _operator(self, answers):
        asked = []

        def ask(device, reason):
            asked.append((device, reason))
            return answers.pop(0)

        return SimpleNamespace(hitl_strategy=SimpleNamespace(interventions=0, ask_for_help=ask)), asked

    def test_the_operator_fixes_it_and_the_session_is_checked_once_without_new_login(self, login_env, tmp_path):
        login_env.ensure = [_login(LoginStatus.CAPTCHA_DETECTED)]
        login_env.check = [_login(LoginStatus.LOGGED_IN)]
        autopilot, asked = self._operator(["resolved"])
        result = self._go(login_env, tmp_path, autopilot=autopilot)
        assert result.success and result.data.is_logged_in
        assert login_env.calls == ["ensure", "check"]                      # nessun perform_login / secondo ensure
        assert asked[0][0] == "D1" and "CAPTCHA" in asked[0][1]

    def test_the_operator_can_stop(self, login_env, tmp_path):
        login_env.ensure = [_login(LoginStatus.LOGIN_FAILED)]
        autopilot, _ = self._operator(["stop"])
        result = self._go(login_env, tmp_path, autopilot=autopilot)
        assert not result.success and login_env.calls == ["ensure"]

    def test_still_logged_out_after_the_operator_stops(self, login_env, tmp_path):
        login_env.ensure = [_login(LoginStatus.CAPTCHA_DETECTED)]
        login_env.check = [_login(LoginStatus.LOGIN_REQUIRED)]
        autopilot, _ = self._operator(["resolved"])
        result = self._go(login_env, tmp_path, autopilot=autopilot)
        assert not result.success and result.error_code == "CAPTCHA_DETECTED"

    def test_without_an_operator_nobody_is_asked(self, login_env, tmp_path):
        login_env.ensure = [_login(LoginStatus.CAPTCHA_DETECTED)]
        assert not self._go(login_env, tmp_path, autopilot=SimpleNamespace(hitl_strategy=None)).success
        assert login_env.calls == ["ensure"]

    def test_the_operator_is_not_asked_for_a_navigation_failure(self, login_env, tmp_path):
        login_env.ensure = [_login(LoginStatus.NAVIGATION_FAILED)]
        autopilot, asked = self._operator(["resolved"])
        assert not self._go(login_env, tmp_path, autopilot=autopilot).success and asked == []


class TestLoginOnly:
    @pytest.fixture
    def stack(self, monkeypatch, tmp_path):
        built = {}
        monkeypatch.setattr(script.run_autopilot, "_configure_logging", lambda d, s: built.setdefault("log", s))
        monkeypatch.setattr(script.run_autopilot, "ScreenReader", lambda **kw: built.setdefault("reader", object()))
        monkeypatch.setattr(script.run_autopilot, "TouchInjector",
                            lambda **kw: built.setdefault("injector", SimpleNamespace(**kw)))
        monkeypatch.setattr(script.run_autopilot, "build_autopilot_for",
                            lambda **kw: pytest.fail("--login-only non costruisce l'autopilot"))
        monkeypatch.setattr(script, "navigate_to_slot", lambda *a: pytest.fail("--login-only non naviga"))
        monkeypatch.setattr(script.model_metrics, "start_run", lambda *a, **k: pytest.fail("nessuna metrica di gioco"))
        return built

    def _args(self, tmp_path, *extra):
        return script.parse_args(["--device", "D1", "--login-only", "--log-dir", str(tmp_path), *extra])

    def test_only_logs_in_and_stops(self, stack, login_env, tmp_path, caplog):
        login_env.ensure = [_login(LoginStatus.LOGGED_IN)]
        with caplog.at_level(logging.INFO):
            assert script.run(self._args(tmp_path)) == 0
        assert login_env.calls == ["ensure"]
        assert login_env.executors[0].screen_reader is stack["reader"]
        assert login_env.executors[0].touch_injector is stack["injector"]
        assert "mi fermo qui" in caplog.text and stack["log"] == "login_SISAL"

    def test_maps_a_new_book_without_playing(self, stack, login_env, tmp_path):
        login_env.recipe_known = False
        login_env.ensure = [_login(LoginStatus.RECIPE_NOT_FOUND), _login(LoginStatus.LOGGED_IN)]
        login_env.map = [Result.ok(SimpleNamespace(steps=[1, 2]))]
        assert script.run(self._args(tmp_path, "--book", "SNAI")) == 0
        assert login_env.calls == ["ensure", "map", "ensure"]

    def test_a_failed_login_exits_with_1(self, stack, login_env, tmp_path):
        login_env.ensure = [_login(LoginStatus.LOGIN_FAILED)]
        assert script.run(self._args(tmp_path)) == 1

    def test_keyboard_interrupt_exits_with_1(self, stack, monkeypatch, tmp_path):
        def interrupted(*a, **k):
            raise KeyboardInterrupt
        monkeypatch.setattr(script, "login_before_navigation", interrupted)
        assert script.run(self._args(tmp_path)) == 1

    def test_needs_neither_promo_nor_slot_nor_target_nor_demo(self, monkeypatch, tmp_path):
        ran = []
        monkeypatch.setattr(script, "run", lambda args: ran.append(args) or 0)
        with pytest.raises(SystemExit) as info:
            script.main(["--device", "D1", "--login-only", "--log-dir", str(tmp_path)])
        assert info.value.code == 0 and ran and ran[0].login_only is True


class TestLoginFlags:
    def test_defaults_do_the_login(self, tmp_path):
        args = _login_args(tmp_path)
        assert args.skip_login is False and args.login_only is False

    def test_promo_slot_and_target_stay_required_without_login_only(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as info:
            script.parse_args(["--device", "D1", "--demo"])
        assert info.value.code == 2
        err = capsys.readouterr().err
        assert "--promo-name" in err and "--slot-name" in err and "--target" in err
        with pytest.raises(SystemExit):
            script.parse_args(["--device", "D1", "--demo", "--promo-name", "P", "--slot-name", "S"])

    def test_skip_login_and_login_only_exclude_each_other(self, tmp_path):
        with pytest.raises(SystemExit):
            script.parse_args(["--device", "D1", "--skip-login", "--login-only"])

    def test_demo_is_still_required_to_play(self, monkeypatch):
        monkeypatch.setattr(script, "run", lambda args: pytest.fail("non deve partire"))
        with pytest.raises(SystemExit) as info:
            script.main(["--device", "D", "--promo-name", "P", "--slot-name", "S", "--target", "1", "--skip-login"])
        assert info.value.code == 2


class TestLoginOnReentry:
    def _reenter(self, env, login_env, monkeypatch, tmp_path, *extra):
        monkeypatch.setattr(script, "navigate_to_slot",
                            lambda *a: login_env.calls.append("navigate") or Result.ok(5))
        return script.make_reenter(_login_args(tmp_path, *extra), object(), env.autopilot)

    def test_the_login_comes_before_the_navigation_and_never_maps(self, env, login_env, monkeypatch, tmp_path):
        login_env.ensure = [_login(LoginStatus.LOGIN_SUCCESS)]
        reenter = self._reenter(env, login_env, monkeypatch, tmp_path)
        assert reenter("D1").success
        assert login_env.calls == ["ensure", "navigate"]

    def test_without_recipe_the_reentry_fails_instead_of_mapping(self, env, login_env, monkeypatch, tmp_path):
        login_env.ensure = [_login(LoginStatus.RECIPE_NOT_FOUND)]
        reenter = self._reenter(env, login_env, monkeypatch, tmp_path)
        result = reenter("D1")
        assert not result.success and "login di rientro fallito" in result.error
        assert login_env.mappers == [] and "navigate" not in login_env.calls

    def test_skip_login_keeps_the_reentry_as_before(self, env, login_env, monkeypatch, tmp_path):
        reenter = self._reenter(env, login_env, monkeypatch, tmp_path, "--skip-login")
        assert reenter("D1").success
        assert login_env.executors == [] and login_env.calls == ["navigate"]
