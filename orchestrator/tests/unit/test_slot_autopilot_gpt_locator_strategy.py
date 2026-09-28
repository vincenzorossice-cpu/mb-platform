from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_autopilot.strategies.gpt_locator_strategy import GPTLocatorStrategy


def _context(raw_texts=None):
    return LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/shot.png",
        screen_width=1080, screen_height=2400, raw_texts=raw_texts or [], profile=None,
    )


def _answers(action="tap", target_text=None, target_region=None, confidence=0.8):
    return {
        "answers": {
            "action": {"choice": action, "confidence": confidence},
            "target": {"choice": {"text": target_text, "region": target_region}, "confidence": confidence},
        }
    }


class TestGPTLocatorStrategy:
    def test_tap_with_resolvable_text_target(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(target_text="SPIN", confidence=0.85))
        raw_texts = [("SPIN", (500, 2000), 0.9)]
        strategy = GPTLocatorStrategy(gpt_client)

        result = strategy.locate("spin", _context(raw_texts))
        assert result.success
        assert (result.data.x, result.data.y) == (500, 2000)
        assert result.data.confidence == 0.85

    def test_stop_action_fails(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(action="stop"))
        strategy = GPTLocatorStrategy(gpt_client)

        result = strategy.locate("spin", _context())
        assert not result.success
        assert result.error_code == "GPT_LOCATOR_NO_TAP"

    def test_underlying_gpt_failure_propagates(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.fail("API down", "GPT_API_ERROR")
        strategy = GPTLocatorStrategy(gpt_client)

        result = strategy.locate("spin", _context())
        assert not result.success
        assert result.error_code == "GPT_API_ERROR"

    def test_unresolved_target_fails(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(target_text="GHOST"))
        strategy = GPTLocatorStrategy(gpt_client)

        result = strategy.locate("spin", _context(raw_texts=[]))
        assert not result.success
        assert result.error_code == "GPT_LOCATOR_UNRESOLVED_TARGET"

    def test_gpt_client_is_injected_never_instantiated(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(target_region="bottom-center"))
        strategy = GPTLocatorStrategy(gpt_client)
        assert strategy.gpt_client is gpt_client

        result = strategy.locate("spin", _context())
        assert result.success
        gpt_client.ask_with_image.assert_called_once()
