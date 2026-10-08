import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.session import SlotSessionResult
from orchestrator.scripts import run_promo_complete as script


def _args(tmp_path, *extra):
    return script.parse_args(["--device", "D1", "--promo-name", "P", "--slot-name", "Slot X", "--target", "30",
                              "--demo", "--log-dir", str(tmp_path), *extra])


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
