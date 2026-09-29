from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.screen_recovery import ScreenRecovery
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_autopilot.verifier import TapOutcome
from orchestrator.modules.slot_ocr.reader import OCRBox


@pytest.fixture
def shots(tmp_path):
    """original = schermo di gioco, other = schermata aperta per errore."""
    paths = {}
    for name, color in (("original", (20, 40, 200)), ("same", (22, 41, 200)), ("other", (240, 240, 240))):
        path = tmp_path / f"{name}.png"
        Image.new("RGB", (200, 400), color).save(path)
        paths[name] = str(path)
    return paths


def _box(text, x, y):
    return OCRBox(text=text, bbox=(x - 20, y - 20, x + 20, y + 20), confidence=0.9)


def _context(boxes=()):
    return LocateContext(
        device_id="dev", slot_name="bigbass", screenshot_path="/tmp/x.png", screen_width=1080,
        screen_height=2400, text_boxes=list(boxes),
    )


def _tap_result(after):
    return Result.ok(TapOutcome(
        tapped_coords=(0, 0), verified=True, screenshot_before="b", screenshot_after=after, diff_score=0.5,
        balance_before=None, balance_after=None, bet_before=None, bet_after=None,
        screen_state_before="idle", screen_state_after="idle", outcome="success",
    ))


def _recovery(current_shot, tap_afters=(), locator_result=None, config=None):
    verifier = MagicMock()
    verifier.screen_reader._screenshot.return_value = Result.ok(current_shot)
    verifier.verified_tap.side_effect = [_tap_result(after) for after in tap_afters]
    locator = MagicMock()
    locator.locate.return_value = locator_result or Result.fail("none", "ELEMENT_NOT_LOCATABLE")
    config = config or AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"))
    return ScreenRecovery(locator, verifier, config), locator, verifier


def _provider(boxes=()):
    return MagicMock(side_effect=lambda: Result.ok(_context(boxes)))


def _taps(verifier):
    return [call.args[1:3] for call in verifier.verified_tap.call_args_list]


class TestRecover:
    def test_screen_already_original_taps_nothing(self, shots):
        recovery, _, verifier = _recovery(shots["same"])

        result = recovery.recover("dev", shots["original"], (46, 2048), _provider())

        assert result.data.method == "already_original"
        verifier.verified_tap.assert_not_called()

    def test_close_word_found_by_ocr_is_tapped_at_its_center(self, shots):
        recovery, locator, verifier = _recovery(shots["other"], tap_afters=[shots["same"]])

        result = recovery.recover("dev", shots["original"], (46, 2048), _provider([_box("X", 1000, 300)]))

        assert result.data.method == "ocr_close"
        assert _taps(verifier) == [(1000, 300)]
        locator.locate.assert_not_called()

    def test_close_word_is_preferred_over_exit_word(self, shots):
        recovery, _, verifier = _recovery(shots["other"], tap_afters=[shots["same"]])

        recovery.recover("dev", shots["original"], (0, 0), _provider([_box("ESCI", 500, 100), _box("CHIUDI", 900, 300)]))

        assert _taps(verifier)[0] == (900, 300)

    def test_falls_back_to_gpt_close_button_without_ever_using_hitl(self, shots):
        found = Result.ok(LocateCandidate(x=980, y=290, confidence=0.7, strategy_name="gpt"))
        recovery, locator, verifier = _recovery(shots["other"], tap_afters=[shots["same"]], locator_result=found)

        result = recovery.recover("dev", shots["original"], (46, 2048), _provider())

        assert result.data.method == "gpt_close"
        assert _taps(verifier) == [(980, 290)]
        assert locator.locate.call_args.args[0] == "close_button"
        assert locator.locate.call_args.kwargs["exclude_strategies"] == ("hitl",)

    def test_last_resort_is_retapping_the_icon_that_opened_the_screen(self, shots):
        recovery, _, verifier = _recovery(shots["other"], tap_afters=[shots["same"]])

        result = recovery.recover("dev", shots["original"], (46, 2048), _provider())

        assert result.data.method == "retap"
        assert _taps(verifier) == [(46, 2048)]

    def test_a_tap_that_does_not_restore_the_original_screen_moves_on(self, shots):
        recovery, _, verifier = _recovery(shots["other"], tap_afters=[shots["other"], shots["same"]])

        result = recovery.recover("dev", shots["original"], (46, 2048), _provider([_box("X", 1000, 300)]))

        assert result.data.method == "retap"
        assert _taps(verifier) == [(1000, 300), (46, 2048)]

    def test_fails_when_nothing_restores_the_original_screen(self, shots):
        recovery, _, verifier = _recovery(shots["other"], tap_afters=[shots["other"]] * 2)

        result = recovery.recover("dev", shots["original"], (46, 2048), _provider([_box("X", 1000, 300)]))

        assert not result.success and result.error_code == "RECOVERY_FAILED"

    def test_attempts_are_capped_by_config(self, shots):
        config = AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"), max_recovery_attempts=1)
        recovery, _, verifier = _recovery(shots["other"], tap_afters=[shots["other"]] * 3, config=config)

        result = recovery.recover("dev", shots["original"], (46, 2048), _provider([_box("X", 1000, 300), _box("CLOSE", 800, 300)]))

        assert result.error_code == "RECOVERY_FAILED"
        assert verifier.verified_tap.call_count == 1

    def test_the_same_point_is_not_tapped_twice(self, shots):
        recovery, _, verifier = _recovery(shots["other"], tap_afters=[shots["other"]])

        recovery.recover("dev", shots["original"], (1000, 300), _provider([_box("X", 1000, 300)]))

        assert _taps(verifier) == [(1000, 300)]
