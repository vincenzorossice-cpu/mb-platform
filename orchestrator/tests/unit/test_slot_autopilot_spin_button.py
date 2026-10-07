import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image, ImageDraw

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import session as session_module
from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.frame_diff import StabilityResult
from orchestrator.modules.slot_autopilot.session import play_session
from orchestrator.modules.slot_autopilot.spin_button import SpinButtonState, button_mask, similarity
from orchestrator.modules.slot_autopilot.spin_controller import SpinController
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, CalmResult, TapOutcome

W, H = 1080, 2400
BX, BY = 540, 1800


def _screen(path, arrow=True, background=(30, 90, 40), noise=False, overlay=False):
    """Schermo con il pulsante spin: un anello e una freccia bianchi (se arrow) su un fondo che puo' muoversi."""
    image = Image.new("RGB", (W, H), background)
    draw = ImageDraw.Draw(image)
    if overlay:                                  # overlay scuro con numero dorato, lontano dal pulsante
        draw.rectangle((100, 600, 980, 1100), fill=(10, 10, 10))
        draw.text((400, 800), "1.11", fill=(230, 180, 20))
    if noise:                                    # fondo animato: macchie colorate (non bianche)
        rng = np.random.default_rng(abs(hash(str(path))) % 1000)
        patch = rng.integers(0, 140, (200, 200, 3), dtype=np.uint8)
        image.paste(Image.fromarray(patch), (BX - 100, BY - 100))
    if arrow:
        draw.ellipse((BX - 80, BY - 80, BX + 80, BY + 80), outline=(255, 255, 255), width=14)
        draw.arc((BX - 40, BY - 40, BX + 40, BY + 40), 30, 300, fill=(255, 255, 255), width=14)
    image.save(path)
    return str(path)


@pytest.fixture
def button(tmp_path):
    return SpinButtonState(memory_dir=str(tmp_path / "mem"), debug_dir=str(tmp_path / "dbg"))


def _learned(button, tmp_path):
    idle = _screen(tmp_path / "idle.png")
    busy = _screen(tmp_path / "busy.png", arrow=False)
    assert button.learn("Pirots 5", (W, H), BX, BY, idle, busy) == "learned"
    return button


class TestMaskAndSimilarity:
    def test_the_arrow_gives_a_mask_and_its_absence_does_not(self, tmp_path):
        with_arrow = button_mask(_screen(tmp_path / "a.png"), BX, BY)
        without = button_mask(_screen(tmp_path / "b.png", arrow=False), BX, BY)

        assert with_arrow.sum() >= 15 and without.sum() == 0
        assert similarity(with_arrow, with_arrow) == 1.0 and similarity(with_arrow, without) == 0.0

    def test_two_empty_masks_are_identical_and_a_missing_file_gives_none(self, tmp_path):
        empty = np.zeros((32, 32), dtype=bool)
        assert similarity(empty, empty) == 1.0
        assert button_mask(str(tmp_path / "nope.png"), BX, BY) is None


class TestLearningAndClassification:
    def test_learns_from_a_ready_and_a_busy_sample_and_classifies(self, button, tmp_path):
        _learned(button, tmp_path)

        assert button.classify("Pirots 5", (W, H), _screen(tmp_path / "r.png"), BX, BY) is True
        assert button.classify("Pirots 5", (W, H), _screen(tmp_path / "o.png", arrow=False), BX, BY) is False

    def test_a_moving_background_and_an_overlay_do_not_hide_the_arrow(self, button, tmp_path):
        _learned(button, tmp_path)

        ready = _screen(tmp_path / "r.png", noise=True, overlay=True, background=(60, 20, 90))

        assert button.classify("Pirots 5", (W, H), ready, BX, BY) is True

    def test_without_a_reference_the_answer_is_unknown(self, button, tmp_path):
        assert button.classify("Pirots 5", (W, H), _screen(tmp_path / "r.png"), BX, BY) is None

    def test_the_reference_is_saved_and_read_back_by_a_new_instance(self, button, tmp_path):
        _learned(button, tmp_path)
        again = SpinButtonState(memory_dir=button.memory_dir, debug_dir=button.debug_dir)

        assert again.classify("Pirots 5", (W, H), _screen(tmp_path / "r.png"), BX, BY) is True
        assert not again.needs_learning("Pirots 5", (W, H))

    def test_a_different_screen_size_ignores_the_saved_reference(self, button, tmp_path):
        _learned(button, tmp_path)

        assert button.classify("Pirots 5", (720, 1600), _screen(tmp_path / "r.png"), BX, BY) is None
        assert button.needs_learning("Pirots 5", (720, 1600))

    def test_identical_samples_are_retried_and_then_given_up(self, button, tmp_path):
        idle = _screen(tmp_path / "i.png")
        same = _screen(tmp_path / "s.png")                       # la freccia non e' sparita

        assert button.learn("Pirots 5", (W, H), BX, BY, idle, same) == "retry"
        assert button.needs_learning("Pirots 5", (W, H))
        assert button.learn("Pirots 5", (W, H), BX, BY, idle, same) == "retry"
        assert button.learn("Pirots 5", (W, H), BX, BY, idle, same) == "unusable"
        assert not button.needs_learning("Pirots 5", (W, H))
        assert button.classify("Pirots 5", (W, H), idle, BX, BY) is None

    def test_a_ready_sample_without_an_arrow_is_not_usable(self, button, tmp_path):
        none = _screen(tmp_path / "n.png", arrow=False)

        assert button.learn("Pirots 5", (W, H), BX, BY, none, none) == "retry"

    def test_unreadable_samples_are_retried_and_a_broken_file_is_ignored(self, button, tmp_path):
        assert button.learn("Pirots 5", (W, H), BX, BY, str(tmp_path / "x.png"), str(tmp_path / "y.png")) == "retry"
        import os
        os.makedirs(button.memory_dir, exist_ok=True)
        with open(os.path.join(button.memory_dir, "pirots_5.spin_button.json"), "w") as broken:
            broken.write("{no json")
        assert button.reference("Pirots 5", (W, H)) is None

    def test_the_two_crops_are_saved_for_a_visual_check(self, button, tmp_path):
        import os
        _learned(button, tmp_path)

        assert os.path.exists(os.path.join(button.debug_dir, "spin_button_pronto.png"))
        assert os.path.exists(os.path.join(button.debug_dir, "spin_button_occupato.png"))


def _config(**kw):
    return AutoPilotConfig(device_id="d", slot_name="Pirots 5", target_played_amount=Decimal("1"),
                           frame_diff_poll_interval=0.001, frame_diff_max_wait_seconds=1.0,
                           spin_calm_max_wait_seconds=1.0, **kw)


def _verifier(frames):
    """screen_reader finto che consegna gli screenshot di `frames` in ordine (l'ultimo si ripete)."""
    reader = MagicMock()
    paths = list(frames)
    counter = {"n": 0}

    def shot(device_id):
        index = min(counter["n"], len(paths) - 1)
        counter["n"] += 1
        return Result.ok(paths[index])

    reader._screenshot.side_effect = shot
    ocr = MagicMock()
    ocr.read_slot_state.return_value = {"saldo": 9.8, "posta": 0.2}
    injector = MagicMock()
    injector.tap_coordinates.return_value = Result.ok(True)
    return ActionVerifier(injector, reader, ocr, _config()), counter


class TestVerifierUsesTheArrow:
    def test_after_the_tap_the_wait_ends_when_the_arrow_is_back_twice_not_before_the_minimum_time(
        self, button, tmp_path, monkeypatch,
    ):
        _learned(button, tmp_path)
        frames = [_screen(tmp_path / f"f{i}.png", arrow=(i >= 3)) for i in range(8)]   # freccia assente 3 frame
        verifier, counter = _verifier(frames)
        verifier.config.spin_calm_max_wait_seconds = 30.0
        clock = {"now": 100.0}
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.verifier.time.monotonic", lambda: clock["now"])
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.verifier.time.sleep",
                            lambda s: clock.__setitem__("now", clock["now"] + 0.5))
        check = lambda path: button.classify("Pirots 5", (W, H), path, BX, BY)

        wait, ended_ready = verifier._wait_after_tap("d", SimpleNamespace(region=None), check, 100.0)

        assert ended_ready and wait.data.stable
        assert counter["n"] == 4                      # 3 occupati + 1 pronto: la transizione vista e' gia' la prova
        assert wait.data.waited_seconds >= 1.2

    def test_without_ever_seeing_the_busy_state_two_ready_frames_are_needed(self, button, tmp_path, monkeypatch):
        _learned(button, tmp_path)
        frames = [_screen(tmp_path / f"r{i}.png") for i in range(8)]          # sempre pronto
        verifier, counter = _verifier(frames)
        verifier.config.spin_calm_max_wait_seconds = 30.0
        clock = {"now": 100.0}
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.verifier.time.monotonic", lambda: clock["now"])
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.verifier.time.sleep",
                            lambda s: clock.__setitem__("now", clock["now"] + 0.5))
        check = lambda path: button.classify("Pirots 5", (W, H), path, BX, BY)

        wait, ended_ready = verifier._wait_after_tap("d", SimpleNamespace(region=None), check, 100.0)

        assert ended_ready and counter["n"] == 5      # 3 frame prima dei 1,2 s minimi + 2 pronti di fila

    def test_the_initial_wait_skips_the_first_seconds_without_screenshots(self, button, tmp_path, monkeypatch):
        _learned(button, tmp_path)
        frames = [_screen(tmp_path / "r.png")]
        verifier, counter = _verifier(frames)
        slept = []
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.verifier.time.sleep", slept.append)
        check = lambda path: button.classify("Pirots 5", (W, H), path, BX, BY)

        verifier._wait_after_tap("d", SimpleNamespace(region=None), check, 0.0, initial_wait=3.5)

        assert slept[0] == 3.5

    def test_an_unclassifiable_first_frame_falls_back_to_the_stable_screen(self, tmp_path):
        frames = [_screen(tmp_path / "a.png")] * 5
        verifier, _ = _verifier(frames)

        wait, ended_ready = verifier._wait_after_tap("d", SimpleNamespace(region=None), lambda p: None, 0.0)

        assert not ended_ready and wait.data.stable

    def test_the_before_tap_check_is_one_screenshot_when_the_arrow_is_visible(self, button, tmp_path):
        _learned(button, tmp_path)
        verifier, counter = _verifier([_screen(tmp_path / "r.png")])
        check = lambda path: button.classify("Pirots 5", (W, H), path, BX, BY)

        result = verifier.wait_until_button_ready("d", check, 1.0)

        assert result.data.calm and not result.data.waited and counter["n"] == 1

    def test_the_before_tap_check_waits_for_the_arrow_and_times_out_when_it_never_returns(self, button, tmp_path):
        _learned(button, tmp_path)
        busy = _screen(tmp_path / "b.png", arrow=False)
        verifier, _ = _verifier([busy])
        check = lambda path: button.classify("Pirots 5", (W, H), path, BX, BY)

        result = verifier.wait_until_button_ready("d", check, 0.05)

        assert result.success and not result.data.calm

    def test_the_before_tap_check_returns_none_when_unclassifiable(self, tmp_path):
        verifier, _ = _verifier([_screen(tmp_path / "r.png")])

        assert verifier.wait_until_button_ready("d", lambda p: None, 1.0) is None


def _controller(button, **config_kw):
    locator = MagicMock()
    locator.locate.return_value = Result.ok(LocateCandidate(x=BX, y=BY, confidence=1.0, strategy_name="profile"))
    locator.is_trusted_strategy.return_value = True
    verifier = MagicMock()
    verifier.wait_until_calm.return_value = Result.ok(CalmResult("p.png", calm=True, waited=False, seconds=0.1))
    verifier.wait_until_button_ready.return_value = Result.ok(CalmResult("p.png", calm=True, waited=False, seconds=0.1))
    verifier.screen_reader._screenshot.return_value = Result.ok("busy.png")
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    return SpinController(locator, verifier, step_logger, _config(**config_kw), button=button), verifier


def _outcome(ended_ready=True, before="10", after="9.8"):
    return Result.ok(TapOutcome(
        tapped_coords=(BX, BY), verified=True, screenshot_before="a.png", screenshot_after="b.png", diff_score=0.4,
        balance_before=Decimal(before), balance_after=Decimal(after), bet_before=Decimal("0.2"), bet_after=Decimal("0.2"),
        screen_state_before="idle", screen_state_after="idle", outcome="success", state_after={"saldo": 1},
        ended_ready=ended_ready,
    ))


def _context(path):
    return SimpleNamespace(slot_ocr_state={"saldo": 10.0, "posta": 0.2}, screenshot_path=path, screen_width=W,
                           screen_height=H, profile=None)


def _spin(controller, path, index=1):
    return controller.spin("Pirots 5", "Pirots 5", "ep", index, MagicMock(return_value=Result.ok(_context(path))),
                           True, Decimal("0.2"))


class TestSpinControllerWithTheArrow:
    def test_with_a_reference_the_arrow_check_replaces_the_calm_check_and_is_passed_to_the_tap(self, button, tmp_path):
        _learned(button, tmp_path)
        controller, verifier = _controller(button)
        verifier.verified_tap.return_value = _outcome(ended_ready=False)

        _spin(controller, _screen(tmp_path / "c.png"))

        verifier.wait_until_button_ready.assert_called_once()
        verifier.wait_until_calm.assert_not_called()
        assert callable(verifier.verified_tap.call_args.kwargs["ready_check"])

    def test_an_unclassifiable_button_falls_back_to_the_calm_check(self, button, tmp_path):
        _learned(button, tmp_path)
        controller, verifier = _controller(button)
        verifier.wait_until_button_ready.return_value = None
        verifier.verified_tap.return_value = _outcome(ended_ready=False)

        _spin(controller, _screen(tmp_path / "c.png"))

        verifier.wait_until_calm.assert_called_once()

    def test_after_a_spin_that_ended_with_the_arrow_the_next_spin_skips_every_check_even_after_a_win(self, button, tmp_path):
        _learned(button, tmp_path)
        controller, verifier = _controller(button)
        verifier.verified_tap.return_value = _outcome(ended_ready=True, after="11.0")     # vincita 1.2

        _spin(controller, _screen(tmp_path / "c.png"), 1)
        _spin(controller, _screen(tmp_path / "c2.png"), 2)

        assert verifier.wait_until_button_ready.call_count == 1                            # solo il primo

    def test_without_a_button_object_nothing_changes(self, tmp_path):
        controller, verifier = _controller(None)
        verifier.verified_tap.return_value = _outcome(ended_ready=False)

        _spin(controller, _screen(tmp_path / "c.png"))

        verifier.wait_until_calm.assert_called_once()
        assert "ready_check" not in verifier.verified_tap.call_args.kwargs

    def test_detection_can_be_switched_off(self, button, tmp_path):
        _learned(button, tmp_path)
        controller, verifier = _controller(button, spin_button_detection=False)
        verifier.verified_tap.return_value = _outcome(ended_ready=False)

        _spin(controller, _screen(tmp_path / "c.png"))

        verifier.wait_until_button_ready.assert_not_called()

    def test_the_first_spin_learns_the_button_and_the_second_uses_it(self, button, tmp_path, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.spin_controller._BUSY_SAMPLE_DELAY_SECONDS", 0)
        controller, verifier = _controller(button, spin_skip_calm_after_loss=False)
        idle = _screen(tmp_path / "idle.png")
        busy = _screen(tmp_path / "busy.png", arrow=False)
        verifier.screen_reader._screenshot.return_value = Result.ok(busy)

        def tap(*args, **kwargs):
            kwargs["on_after_tap"]()                       # il verificatore chiama il campionatore dopo il tocco
            return _outcome(ended_ready=False)

        verifier.verified_tap.side_effect = tap
        _spin(controller, idle, 1)

        assert button.reference("Pirots 5", (W, H)) is not None          # appreso al primo spin
        verifier.verified_tap.side_effect = None
        verifier.verified_tap.return_value = _outcome(ended_ready=False)
        _spin(controller, idle, 2)
        verifier.wait_until_button_ready.assert_called_once()              # usato al secondo


class TestPhaseReports:
    def _autopilot(self):
        ocr = MagicMock()
        ocr.stats = {"calls": 5, "seconds": 41.0, "remote_calls": 2}
        verifier = MagicMock()
        verifier.snapshot.return_value = {"tap_s": 1.5, "taps": 2, "wait_s": 12.0, "captures": 15, "read_s": 3.0,
                                          "reads": 2}
        bet_controller = MagicMock()
        bet_controller.verifier = verifier
        autopilot = SlotAutoPilot(
            config=_config(), screen_reader=MagicMock(), touch_injector=MagicMock(), ocr_reader=ocr,
            profile_loader=MagicMock(), locator=MagicMock(), bet_controller=bet_controller,
            spin_controller=MagicMock(), step_logger=MagicMock(),
        )
        return autopilot, ocr, verifier

    def test_the_report_gives_ocr_waits_and_taps_since_the_last_report(self):
        autopilot, ocr, verifier = self._autopilot()

        first = autopilot.phase_report("Preparazione", 16)
        ocr.stats = {"calls": 9, "seconds": 75.0, "remote_calls": 2}
        verifier.snapshot.return_value = {"tap_s": 2.5, "taps": 3, "wait_s": 20.0, "captures": 25, "read_s": 3.0,
                                          "reads": 2}
        second = autopilot.phase_report("Puntata minima", 45)

        assert "letture OCR 5 (41.0 s" in first and "2 dal server remoto" in first
        assert "Puntata minima: 45 s = letture OCR 4 (34.0 s" in second and "tocchi 1 (1.0 s)" in second
        assert "attese schermo fermo 8.0 s in 10 screenshot" in second

    def test_a_missing_counter_gives_an_empty_report_not_an_error(self):
        autopilot, ocr, _ = self._autopilot()
        ocr.stats = None

        assert autopilot.phase_report("X", 1) == ""

    def test_the_session_logs_both_phases(self, caplog):
        autopilot = MagicMock()
        autopilot.prepare.return_value = Result.ok(True)
        autopilot.set_min_bet.return_value = Result.ok(SimpleNamespace(
            min_bet_verified=False, final_bet=None, outcome="x", attempts=1, aborted_reason="X"))
        autopilot.phase_report.side_effect = lambda label, seconds: f"{label}: {seconds:.0f} s = misura"

        with caplog.at_level("INFO"):
            play_session(autopilot, "d", "s", Decimal("1"), 5)

        assert "Preparazione: " in caplog.text and "Puntata minima: " in caplog.text
