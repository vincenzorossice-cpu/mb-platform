from unittest.mock import MagicMock, patch

from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_autopilot.strategies.hitl_strategy import HITLStrategy


def _context():
    return LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/shot.png",
        screen_width=1080, screen_height=2400, slot_ocr_state={"posta": 1.0}, profile=None,
    )


def _strategy():
    return HITLStrategy(MagicMock(), MagicMock(), MagicMock())


_PATCH_TARGET = "orchestrator.modules.slot_autopilot.strategies.hitl_strategy.handle_hitl_escalation"


class TestHITLStrategy:
    def test_resolved_returns_verified_candidate(self):
        with patch(_PATCH_TARGET, return_value=("resolved", {"posta": 0.5}, None, (300, 2100))):
            result = _strategy().locate("bet_minus", _context())
        assert result.success
        assert result.data.verified is True
        assert (result.data.x, result.data.y) == (300, 2100)

    def test_stop(self):
        with patch(_PATCH_TARGET, return_value=("stop", None, None, None)):
            result = _strategy().locate("bet_minus", _context())
        assert not result.success
        assert result.error_code == "HITL_STOP_REQUESTED"

    def test_skip(self):
        with patch(_PATCH_TARGET, return_value=("skip", None, None, None)):
            result = _strategy().locate("bet_minus", _context())
        assert not result.success
        assert result.error_code == "HITL_SKIPPED"

    def test_hint_only(self):
        with patch(_PATCH_TARGET, return_value=("hint", "look lower", None, (300, 2100))):
            result = _strategy().locate("bet_minus", _context())
        assert not result.success
        assert result.error_code == "HITL_HINT_ONLY"
        assert "look lower" in result.error

    def test_resolved_without_coords_fails(self):
        with patch(_PATCH_TARGET, return_value=("resolved", {"posta": 0.5}, None, None)):
            result = _strategy().locate("bet_minus", _context())
        assert not result.success
        assert result.error_code == "HITL_NO_COORDS"


_PRECISE_TARGET = "orchestrator.modules.slot_autopilot.strategies.hitl_strategy.handle_precise_hitl_escalation"


class TestPreciseFlowSelection:
    def test_with_a_describer_the_precise_flow_is_used(self):
        describer = MagicMock()
        strategy = HITLStrategy(MagicMock(), MagicMock(), MagicMock(), describer=describer)
        with patch(_PRECISE_TARGET, return_value=("resolved", {}, None, (544, 2090))) as precise, \
                patch(_PATCH_TARGET) as legacy:
            result = strategy.locate("bet_menu_toggle", _context())

        assert result.success and (result.data.x, result.data.y) == (544, 2090)
        legacy.assert_not_called()
        assert precise.call_args.args[-1] is describer.locate_described

    def test_without_a_describer_the_legacy_flow_is_used(self):
        with patch(_PRECISE_TARGET) as precise, \
                patch(_PATCH_TARGET, return_value=("stop", None, None, None)):
            result = _strategy().locate("bet_minus", _context())

        assert result.error_code == "HITL_STOP_REQUESTED"
        precise.assert_not_called()


class TestLastTap:
    def test_the_tap_that_changed_the_screen_is_remembered(self):
        strategy = _strategy()
        with patch(_PATCH_TARGET, return_value=("hint", "look", None, (788, 1775))):
            strategy.locate("bet_minus", _context())
        assert strategy.last_tap == (788, 1775)

    def test_no_tap_after_a_stop(self):
        strategy = _strategy()
        with patch(_PATCH_TARGET, return_value=("stop", None, None, None)):
            strategy.locate("bet_minus", _context())
        assert strategy.last_tap is None
