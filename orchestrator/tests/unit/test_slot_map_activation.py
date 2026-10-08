import argparse
import logging
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import save_autopilot_value
from orchestrator.modules.slot_autopilot import session
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.slot_intro import IntroOutcome
from orchestrator.modules.slot_autopilot.slot_map import Point, SlotMap, SlotMemory
from orchestrator.modules.slot_autopilot.slot_setup import EXPLORED, USED, SlotSetup
from orchestrator.modules.slot_autopilot.spin_controller import SpinResult
from orchestrator.scripts import reset_slot_memory, run_promo_complete as script
from orchestrator.tests.unit.test_slot_map_learning import FakePhone, SIZE, _good_map


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


def _ok_spin(i):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal("0.20"), balance_before=Decimal("100"),
                                balance_after=Decimal("99.8"), win_amount=None, verified=True, attempts=1,
                                outcome="success"))


def _autopilot(setup, spins=None):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(BetResult(
        slot_name="s", starting_bet=Decimal("1"), final_bet=Decimal("0.2"), min_bet_verified=True, attempts=1))
    autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
    autopilot.spin.side_effect = list(spins or [])
    autopilot.hitl_strategy = None
    autopilot.slot_setup = setup
    autopilot.gpt_client = SimpleNamespace(_usage={}, _usage_by_purpose={}, usage_summary=lambda: "GPT", model="m")
    return autopilot


def _failing_explorer():
    explorer = MagicMock()
    explorer.explore.return_value = Result.fail("non esplorabile", "EXPLORE_INCOMPLETE")
    return explorer


def _setup(tmp_path, explorer, **kw):
    phone = FakePhone(tmp_path)
    return SlotSetup(SlotMemory(str(tmp_path / "mem")), explorer, phone, gpt_client=SimpleNamespace(_usage_by_purpose={}), **kw)


# ---- run_promo_complete: la mappa si prepara PRIMA di play_session ------------------------------------------------

def _args(**kw):
    base = dict(device="D1", book="SISAL", promo_name="P", slot_name="Wrap", target=1.0, max_steps=20, url="u",
                demo=True, already_active=True, not_active=False, live=False, no_gpt=False, no_hitl=False,
                no_save_steps=True, log_dir="logs", min_bet=0.2, nav_direct=False, reset_slot_memory=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestRunPromoCompleteUsesTheMap:
    def _run(self, monkeypatch, tmp_path, setup, args=None):
        calls = []
        autopilot = _autopilot(setup, [_ok_spin(1), _ok_spin(2), _ok_spin(3), _ok_spin(4), _ok_spin(5)])
        autopilot.shutdown.return_value = Result.ok(True)
        built = {}

        def build(**kwargs):
            built.update(kwargs)
            return Result.ok(autopilot)

        monkeypatch.setattr(script.run_autopilot, "_configure_logging", lambda d, s: "x.log")
        monkeypatch.setattr(script.run_autopilot, "build_autopilot_for", build)
        import orchestrator.modules.decision_engine.gpt_client as gc
        monkeypatch.setattr(gc, "GPTDecisionClient", lambda model=None: SimpleNamespace(
            model="gpt-6.1-sol", usage_summary=lambda: "GPT", _usage={}))
        monkeypatch.setattr(script, "navigate_to_slot", lambda *a: calls.append("navigazione") or Result.ok(3))
        monkeypatch.setattr(script, "build_slot_intro", lambda ap: SimpleNamespace(
            run=lambda d, s: calls.append("intro") or Result.ok(IntroOutcome(rounds=1, taps=0, ready=True))))
        monkeypatch.setattr(script.model_metrics, "log_run", lambda **k: None)
        monkeypatch.setattr(script.model_metrics, "start_run", lambda *a, **k: "r")
        monkeypatch.setattr(script, "exit_slot", lambda *a, **k: None)
        if setup is not None:
            original = setup.prepare
            setup.prepare = lambda d, s: calls.append("slot_setup.prepare") or original(d, s)
        real_play = session.play_session

        def play(*a, **k):
            calls.append("play_session")
            return real_play(*a, **k)
        import orchestrator.modules.slot_autopilot.session as session_module
        monkeypatch.setattr(session_module, "play_session", play)
        code = script.run(args or _args())
        return code, calls, built, autopilot

    def test_prepare_runs_after_the_intro_and_before_play_session(self, monkeypatch, tmp_path):
        explorer = MagicMock()
        explorer.explore.return_value = Result.ok(_good_map())
        code, calls, _, _ = self._run(monkeypatch, tmp_path, _setup(tmp_path, explorer))

        assert code == 0
        assert calls.index("intro") < calls.index("slot_setup.prepare") < calls.index("play_session")

    def test_no_map_means_the_explorer_is_called_once_even_if_play_session_prepares_again(self, monkeypatch, tmp_path):
        explorer = MagicMock()
        explorer.explore.return_value = Result.ok(_good_map())
        setup = _setup(tmp_path, explorer)

        self._run(monkeypatch, tmp_path, setup)

        explorer.explore.assert_called_once()
        assert setup.status == EXPLORED and calls_count(setup) == 2     # script + play_session, ma una sola esplorazione

    def test_existing_map_means_saved_coordinates_and_zero_gpt(self, monkeypatch, tmp_path):
        explorer = MagicMock()
        setup = _setup(tmp_path, explorer)
        setup.memory.save(_good_map())

        self._run(monkeypatch, tmp_path, setup)

        explorer.explore.assert_not_called()
        assert setup.status == USED and sum(setup.gpt_client._usage_by_purpose.values()) == 0

    def test_min_bet_is_passed_to_the_autopilot_builder(self, monkeypatch, tmp_path):
        _, _, built, _ = self._run(monkeypatch, tmp_path, _setup(tmp_path, _failing_explorer()), _args(min_bet=0.2))

        assert built["known_min_bet"] == 0.2

    def test_without_a_setup_it_warns_and_goes_on(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            code, calls, _, _ = self._run(monkeypatch, tmp_path, None)

        assert code == 0 and "Slot memory NON attiva" in caplog.text

    def test_reset_flag_moves_the_slot_memory_before_building(self, monkeypatch, tmp_path):
        os.makedirs("slot_memory")
        open(os.path.join("slot_memory", "wrap.autopilot.json"), "w").write("{}")
        self._run(monkeypatch, tmp_path, _setup(tmp_path, _failing_explorer()), _args(reset_slot_memory=True))

        assert not os.path.exists(os.path.join("slot_memory", "wrap.autopilot.json"))
        assert any(name.startswith("2026") or name[0].isdigit() for name in os.listdir(os.path.join("slot_memory", "_backup")))


def calls_count(setup):
    return len(setup._outcomes) + 1


def test_min_bet_reaches_slot_setup_through_the_real_builder(tmp_path, monkeypatch):
    from orchestrator.scripts.run_autopilot import build_autopilot
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("orchestrator.scripts.run_autopilot._check_openai_key", lambda client: Result.ok(True))
    args = argparse.Namespace(device="D", slot="pirots5", target=30.0, max_steps=50, dry_run=False, live=False,
                              no_gpt=False, no_hitl=False, min_bet=0.2)

    autopilot = build_autopilot(args).data

    assert autopilot.slot_setup.known_min_bet == Decimal("0.2")


def test_the_map_loggers_reach_the_console():
    from orchestrator.scripts import run_autopilot
    for name in ("slot_setup", "slot_explorer", "slot_intro"):
        assert f"orchestrator.modules.slot_autopilot.{name}" in run_autopilot._CONSOLE_INFO_LOGGERS


# ---- soldi veri: spin in attesa, confermato dal primo spin vero ---------------------------------------------------

def test_real_money_pending_spin_is_confirmed_by_the_first_real_spin(tmp_path):
    pending = _good_map(spin=Point(100, 360, "spin", False), spin_pending=True)
    explorer = MagicMock()
    explorer.explore.return_value = Result.ok(pending)
    setup = _setup(tmp_path, explorer, demo=False)
    autopilot = _autopilot(setup, [_ok_spin(1), _ok_spin(2)])

    result = session.play_session(autopilot, "d", "Wrap", Decimal("0.40"), 20)

    assert not result.failed
    saved = setup.memory.load("Wrap", SIZE).data
    assert saved.spin.verified and not saved.spin_pending


def test_without_a_real_spin_the_map_stays_pending(tmp_path):
    pending = _good_map(spin=Point(100, 360, "spin", False), spin_pending=True)
    explorer = MagicMock()
    explorer.explore.return_value = Result.ok(pending)
    setup = _setup(tmp_path, explorer, demo=False)

    setup.prepare("d", "Wrap")

    saved = setup.memory.load("Wrap", SIZE).data
    assert saved.spin_pending and not saved.spin.verified


# ---- reset con backup -------------------------------------------------------------------------------------------

class TestResetSlotMemory:
    def _populate(self, memory_dir):
        os.makedirs(memory_dir, exist_ok=True)
        for name in ("wrap.slot_map.json", "wrap.autopilot.json", "wrap.json", "wrap.spin_button.json",
                     "wrap.intro.json", "other.autopilot.json"):
            open(os.path.join(memory_dir, name), "w").write("{}")

    def test_moves_only_that_slot_into_a_backup(self, tmp_path):
        memory_dir = str(tmp_path / "mem")
        self._populate(memory_dir)

        moved = SlotMemory(memory_dir).reset("Wrap")

        assert moved.success and len(moved.data) == 5
        assert os.listdir(memory_dir).count("other.autopilot.json") == 1          # le altre slot restano
        assert not any(name.startswith("wrap.") for name in os.listdir(memory_dir))
        backup_root = os.path.join(memory_dir, "_backup")
        assert len(os.listdir(os.path.join(backup_root, os.listdir(backup_root)[0]))) == 5

    def test_nothing_to_move_is_not_an_error(self, tmp_path):
        assert SlotMemory(str(tmp_path / "vuoto")).reset("Wrap").data == []

    def test_the_command_line_script(self, tmp_path, capsys):
        self._populate(str(tmp_path / "mem"))

        assert reset_slot_memory.main(["--slot", "Wrap", "--memory-dir", str(tmp_path / "mem")]) == 0
        assert "Spostati 5 file" in capsys.readouterr().out
