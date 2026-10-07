import json
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image, ImageDraw

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import session as session_module
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.session import play_session
from orchestrator.modules.slot_autopilot.spin_button import SpinButtonState
from orchestrator.modules.slot_autopilot.spin_controller import SpinController
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate
from orchestrator.modules.slot_autopilot.verifier import CalmResult, TapOutcome
from orchestrator.scripts import benchmark_capture, spin_button_probe

W, H = 1080, 2400
BX, BY = 518, 1775


def _screen(path, arrow=True, color=(255, 255, 255), background=(30, 90, 40)):
    image = Image.new("RGB", (W, H), background)
    draw = ImageDraw.Draw(image)
    if arrow:
        draw.ellipse((BX - 80, BY - 80, BX + 80, BY + 80), outline=color, width=14)
        draw.arc((BX - 40, BY - 40, BX + 40, BY + 40), 30, 300, fill=color, width=14)
    image.save(path)
    return str(path)


@pytest.fixture
def button(tmp_path):
    return SpinButtonState(memory_dir=str(tmp_path / "mem"), debug_dir=str(tmp_path / "dbg"))


class TestAdaptiveThreshold:
    def test_a_translucent_grey_arrow_is_learned_with_a_lower_threshold(self, button, tmp_path):
        idle = _screen(tmp_path / "i.png", color=(150, 160, 165))           # freccia grigia, non bianca pura
        busy = _screen(tmp_path / "b.png", arrow=False)

        assert button.learn("Pirots 5", (W, H), BX, BY, idle, busy) == "learned"
        reference = button.reference("Pirots 5", (W, H))
        assert reference.luma < 200 and reference.spread_max >= 60
        assert button.classify("Pirots 5", (W, H), _screen(tmp_path / "r.png", color=(150, 160, 165)), BX, BY) is True
        assert button.classify("Pirots 5", (W, H), _screen(tmp_path / "o.png", arrow=False), BX, BY) is False

    def test_the_chosen_parameters_are_saved_and_read_back(self, button, tmp_path):
        idle = _screen(tmp_path / "i.png", color=(150, 160, 165))
        button.learn("Pirots 5", (W, H), BX, BY, idle, _screen(tmp_path / "b.png", arrow=False))
        again = SpinButtonState(memory_dir=button.memory_dir, debug_dir=button.debug_dir)

        assert again.reference("Pirots 5", (W, H)).luma == button.reference("Pirots 5", (W, H)).luma

    def test_a_very_dark_button_has_no_recognisable_arrow_and_says_so(self, button, tmp_path, caplog):
        idle = _screen(tmp_path / "i.png", color=(40, 100, 50))             # freccia quasi come il fondo
        busy = _screen(tmp_path / "b.png", arrow=False)

        with caplog.at_level("WARNING"):
            outcome = button.learn("Pirots 5", (W, H), BX, BY, idle, busy)

        assert outcome == "retry" and "nessuna freccia chiara" in button.status("Pirots 5")
        assert "NON appreso" in caplog.text and "riprovo" in caplog.text

    def test_identical_samples_explain_why_in_the_status_and_give_up_after_three_attempts(self, button, tmp_path):
        idle = _screen(tmp_path / "i.png")
        same = _screen(tmp_path / "s.png")

        for _ in range(3):
            outcome = button.learn("Pirots 5", (W, H), BX, BY, idle, same)

        assert outcome == "unusable"
        assert "troppo simili" in button.status("Pirots 5")

    def test_an_unreadable_crop_is_reported_not_silent(self, button, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            outcome = button.learn("Pirots 5", (W, H), BX, BY, str(tmp_path / "x.png"), str(tmp_path / "y.png"))

        assert outcome == "retry" and "illeggibile" in button.status("Pirots 5")
        assert "ritaglio del pulsante illeggibile" in caplog.text


class TestRelearnAndSwitches:
    def test_relearn_deletes_the_saved_reference_once(self, tmp_path, monkeypatch):
        first = SpinButtonState(memory_dir=str(tmp_path / "mem"), debug_dir=str(tmp_path / "dbg"))
        assert first.learn("Pirots 5", (W, H), BX, BY, _screen(tmp_path / "i.png"),
                           _screen(tmp_path / "b.png", arrow=False)) == "learned"
        monkeypatch.setenv("SPIN_BUTTON_RELEARN", "1")
        again = SpinButtonState(memory_dir=str(tmp_path / "mem"), debug_dir=str(tmp_path / "dbg"))

        assert again.reference("Pirots 5", (W, H)) is None and again.needs_learning("Pirots 5", (W, H))
        again.learn("Pirots 5", (W, H), BX, BY, _screen(tmp_path / "i2.png"), _screen(tmp_path / "b2.png", arrow=False))
        assert again.reference("Pirots 5", (W, H)) is not None          # il riapprendimento non si ripete a ogni lettura


def _config(**kw):
    return AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"),
                           frame_diff_poll_interval=0.001, **kw)


def _controller(button, **kw):
    locator = MagicMock()
    locator.locate.return_value = Result.ok(LocateCandidate(x=BX, y=BY, confidence=1.0, strategy_name="profile"))
    locator.is_trusted_strategy.return_value = True
    verifier = MagicMock()
    verifier.wait_until_calm.return_value = Result.ok(CalmResult("p.png", calm=True, waited=False, seconds=0.1))
    verifier.screen_reader._screenshot.return_value = Result.ok("busy.png")
    verifier.screen_reader.capture_stats = {"calls": 10, "seconds": 13.5}
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    return SpinController(locator, verifier, step_logger, _config(**kw), button=button), verifier


def _outcome(wait_seconds=6.5, ended_ready=False, after="9.8"):
    return Result.ok(TapOutcome(
        tapped_coords=(BX, BY), verified=True, screenshot_before="a.png", screenshot_after="b.png", diff_score=0.4,
        balance_before=Decimal("10"), balance_after=Decimal(after), bet_before=Decimal("0.2"), bet_after=Decimal("0.2"),
        screen_state_before="idle", screen_state_after="idle", outcome="success", state_after={"saldo": 1},
        wait_seconds=wait_seconds, ended_ready=ended_ready,
    ))


def _context(path):
    return SimpleNamespace(slot_ocr_state={"saldo": 10.0, "posta": 0.2}, screenshot_path=path, screen_width=W,
                           screen_height=H, profile=None)


def _spin(controller, path, index=1):
    return controller.spin("Pirots 5", "Pirots 5", "ep", index, MagicMock(return_value=Result.ok(_context(path))),
                           True, Decimal("0.2"))


class TestBranchesAreLogged:
    def test_no_button_object_disabled_by_config_or_env_and_operator_point_each_say_so(self, button, tmp_path, caplog,
                                                                                       monkeypatch):
        path = _screen(tmp_path / "c.png")
        with caplog.at_level("INFO"):
            controller, verifier = _controller(None)
            verifier.verified_tap.return_value = _outcome()
            _spin(controller, path)
            controller2, verifier2 = _controller(button, spin_button_detection=False)
            verifier2.verified_tap.return_value = _outcome()
            _spin(controller2, path)
            monkeypatch.setenv("SPIN_BUTTON_DETECTION", "0")
            controller3, verifier3 = _controller(button)
            verifier3.verified_tap.return_value = _outcome()
            _spin(controller3, path)

        assert "nessun rilevatore collegato" in caplog.text
        assert caplog.text.count("disattivato (spin_button_detection=False o SPIN_BUTTON_DETECTION=0)") == 2

    def test_a_missing_reference_announces_the_learning_and_a_spent_one_warns(self, button, tmp_path, caplog):
        path = _screen(tmp_path / "c.png")
        controller, verifier = _controller(button)
        verifier.verified_tap.return_value = _outcome()

        with caplog.at_level("INFO"):
            _spin(controller, path)
        assert "nessun riferimento: lo apprendo in questo spin" in caplog.text

        same = _screen(tmp_path / "same.png")
        for _ in range(3):                                    # tre campioni identici: rinuncia
            button.learn("Pirots 5", (W, H), BX, BY, path, same)
        caplog.clear()
        controller2, verifier2 = _controller(button)
        verifier2.verified_tap.return_value = _outcome()
        with caplog.at_level("WARNING"):
            _spin(controller2, path)
        assert "tentativi gia' esauriti" in caplog.text and "SPIN_BUTTON_RELEARN=1" in caplog.text

    def test_a_missing_busy_sample_is_reported_in_the_log_and_the_result(self, button, tmp_path, caplog):
        controller, verifier = _controller(button)
        verifier.verified_tap.return_value = _outcome()          # la callback dopo il tocco non viene mai chiamata

        with caplog.at_level("WARNING"):
            result = _spin(controller, _screen(tmp_path / "c.png"))

        assert "occupato" in result.data.button_status and "non e' stato raccolto" in result.data.button_status
        assert "non e' stato raccolto" in caplog.text

    def test_the_learning_spin_returns_the_status_and_the_session_prints_it_once(self, button, tmp_path, monkeypatch,
                                                                                 caplog):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.spin_controller._BUSY_SAMPLE_DELAY_SECONDS", 0)
        idle = _screen(tmp_path / "idle.png")
        busy = _screen(tmp_path / "busy.png", arrow=False)
        controller, verifier = _controller(button)
        verifier.screen_reader._screenshot.return_value = Result.ok(busy)

        def tap(*args, **kwargs):
            kwargs["on_after_tap"]()
            return _outcome()

        verifier.verified_tap.side_effect = tap
        result = _spin(controller, idle)

        assert result.data.button_status.startswith("appreso")

        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.return_value = Result.ok(SimpleNamespace(
            min_bet_verified=True, final_bet=Decimal("0.2"), outcome="x", attempts=1, aborted_reason=None))
        autopilot.spin.return_value = result
        autopilot.get_balance_summary.return_value = (None, None)
        with caplog.at_level("INFO"):
            play_session(autopilot, "d", "s", Decimal("0.4"), 10, log=session_module.logger)
        assert caplog.text.count("Pulsante spin: appreso") == 1


class TestSmartInitialWait:
    def test_nothing_is_skipped_before_two_normal_spins_are_known(self, button):
        controller, verifier = _controller(button)
        verifier.verified_tap.return_value = _outcome(wait_seconds=6.5)

        _spin(controller, "x.png", 1)

        assert "initial_wait" not in verifier.verified_tap.call_args.kwargs

    def test_after_two_spins_the_first_seconds_are_skipped_conservatively(self, button):
        controller, verifier = _controller(button)
        verifier.verified_tap.return_value = _outcome(wait_seconds=6.5)

        for index in (1, 2, 3):
            _spin(controller, "x.png", index)

        # 70% di 6,5 s meno uno scatto da 1,35 s = 3,2 s
        assert verifier.verified_tap.call_args.kwargs["initial_wait"] == pytest.approx(3.2, abs=0.05)

    def test_a_bonus_does_not_lengthen_the_typical_duration(self, button):
        controller, verifier = _controller(button)
        verifier.verified_tap.return_value = _outcome(wait_seconds=6.5)
        for index in (1, 2):
            _spin(controller, "x.png", index)
        verifier.verified_tap.return_value = _outcome(wait_seconds=34.0)       # bonus
        _spin(controller, "x.png", 3)
        verifier.verified_tap.return_value = _outcome(wait_seconds=6.5)
        _spin(controller, "x.png", 4)

        assert controller._durations["Pirots 5"] == [6.5, 6.5, 6.5]
        assert verifier.verified_tap.call_args.kwargs["initial_wait"] == pytest.approx(3.2, abs=0.05)

    def test_the_wait_is_capped_and_can_be_switched_off(self, button):
        controller, verifier = _controller(button, spin_smart_wait=False)
        verifier.verified_tap.return_value = _outcome(wait_seconds=6.5)
        for index in (1, 2, 3):
            _spin(controller, "x.png", index)

        assert "initial_wait" not in verifier.verified_tap.call_args.kwargs
        capped, _ = _controller(button)
        capped._durations["Pirots 5"] = [19.0, 19.0, 19.0]
        assert capped._initial_wait("Pirots 5") == 8.0


class TestProbeAndBenchmark:
    def test_the_probe_finds_the_animation_window_from_the_frames(self):
        rows = [(0.0, 1.0), (1.4, 0.1), (2.8, 0.0), (4.1, 0.05), (5.5, 0.2), (6.9, 0.95), (8.3, 1.0)]

        summary = spin_button_probe.analyse(rows)

        assert summary["absent_from"] == 1.4 and summary["absent_to"] == 5.5
        assert summary["duration"] == pytest.approx(4.1) and summary["back_at"] == 6.9

    def test_an_arrow_that_never_disappears_is_reported(self):
        assert spin_button_probe.analyse([(0.0, 1.0), (1.4, 0.9), (2.8, 0.95)])["duration"] is None

    def test_the_spin_point_comes_from_the_profile(self, tmp_path):
        profile = tmp_path / "pirots5.json"
        profile.write_text(json.dumps({"elements": {"spin": {"norm_x": 0.5, "norm_y": 0.74}}}))

        assert spin_button_probe.spin_point_from_profile("Pirots5", (1080, 2400), str(tmp_path)) == (540, 1776)
        assert spin_button_probe.spin_point_from_profile("Altra", (1080, 2400), str(tmp_path)) is None

    def test_the_probe_runs_end_to_end_with_a_fake_phone(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        frames = [_screen(tmp_path / "f0.png"), _screen(tmp_path / "f1.png", arrow=False),
                  _screen(tmp_path / "f2.png", arrow=False), _screen(tmp_path / "f3.png")]
        counter = {"n": 0}

        class FakeReader:
            last_method = "raw"

            def __init__(self, capture_mode=None):
                pass

            def _screenshot(self, device_id):
                index = min(counter["n"], len(frames) - 1)
                counter["n"] += 1
                return Result.ok(frames[index])

        monkeypatch.setattr(spin_button_probe, "ScreenReader", FakeReader)
        clock = {"now": 0.0}
        monkeypatch.setattr(spin_button_probe.time, "monotonic", lambda: clock.__setitem__("now", clock["now"] + 1.0) or clock["now"])

        code = spin_button_probe.main(["--device", "d", "--x", str(BX), "--y", str(BY), "--seconds", "6"],
                                      input_fn=lambda prompt: "")

        out = capsys.readouterr().out
        assert code == 0 and "Freccia assente da" in out and "Ritagli salvati in" in out
        assert any(name.startswith("crop_") for _, _, files in os.walk("logs/spin_probe") for name in files)

    def test_the_benchmark_recommends_the_fastest_clean_method(self):
        png = {"mean": 2.2, "raw_failures": 0}
        raw = {"mean": 1.35, "raw_failures": 0}
        rawgz = {"mean": 0.8, "raw_failures": 0}

        assert "'rawgz'" in benchmark_capture.recommendation(png, raw, rawgz)
        assert "'raw'" in benchmark_capture.recommendation(png, raw, {"mean": None, "raw_failures": 1})
        assert "'raw'" in benchmark_capture.recommendation(png, raw, {"mean": 0.5, "raw_failures": 2})
        assert "resta ADB_CAPTURE_MODE=png" in benchmark_capture.recommendation(png, {"mean": 2.1, "raw_failures": 0}, None)
