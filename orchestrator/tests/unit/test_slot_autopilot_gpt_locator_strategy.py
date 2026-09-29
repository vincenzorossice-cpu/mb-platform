import os
from unittest.mock import MagicMock

import pytest
from PIL import Image

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
        strategy = GPTLocatorStrategy(gpt_client)
        assert strategy.gpt_client is gpt_client
        gpt_client.ask_with_image.assert_not_called()


@pytest.fixture
def real_context(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (1080, 2400), (10, 20, 30)).save(path)
    return LocateContext(
        device_id="dev", slot_name="bigbass", screenshot_path=str(path),
        screen_width=1080, screen_height=2400, raw_texts=[], profile=None,
    )


def _cell_answer(label, action="tap"):
    return Result.ok({"answers": {
        "action": {"choice": action, "confidence": 0.8},
        "target": {"choice": {"text": label, "region": None}, "confidence": 0.8},
    }})


class TestIconRefinement:
    def test_icon_is_located_in_two_grid_steps_never_at_the_3x3_cell_center(self, real_context):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.side_effect = [
            Result.ok(_answers(target_region="middle-left")),
            _cell_answer("B6"),   # passo A: colonna B (270-540), riga 6 (1500-1800)
            _cell_answer("C4"),   # passo B
        ]
        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert result.success
        assert gpt_client.ask_with_image.call_count == 3
        assert (result.data.x, result.data.y) != (180, 1200)   # centro 3x3 "middle-left"
        # cella A: centro (405,1650); riquadro (135,1350)-(675,1950); cella B "C4" di una 6x6:
        assert (result.data.x, result.data.y) == (135 + round(2.5 * 540 / 6), 1350 + round(3.5 * 600 / 6))

    def test_stage_calls_use_labeled_grid_candidates_and_the_element_description(self, real_context):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.side_effect = [
            Result.ok(_answers(target_region="middle-left")), _cell_answer("A1"), _cell_answer("A1"),
        ]
        GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        first_questions = gpt_client.ask_with_image.call_args_list[0].args[2]
        assert "minus" in first_questions["action"]["instructions"].lower()
        coarse_questions = gpt_client.ask_with_image.call_args_list[1].args[2]
        assert "A1" in coarse_questions["target"]["criteria"] and "D8" in coarse_questions["target"]["criteria"]

    def test_text_target_resolvable_needs_no_grid_steps(self, real_context):
        real_context.raw_texts = [("SPIN", (500, 2000), 0.9)]
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(target_text="SPIN"))

        result = GPTLocatorStrategy(gpt_client).locate("spin", real_context)

        assert result.success and (result.data.x, result.data.y) == (500, 2000)
        gpt_client.ask_with_image.assert_called_once()

    @pytest.mark.parametrize("second_answer", [_cell_answer("ZZ9"), _cell_answer("A1", action="stop"),
                                               Result.fail("api", "GPT_API_ERROR")])
    def test_invalid_first_grid_step_fails_without_falling_back_to_the_coarse_cell(self, real_context, second_answer):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.side_effect = [Result.ok(_answers(target_region="middle-left")), second_answer]

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert not result.success
        assert result.error_code == "GPT_LOCATOR_UNRESOLVED_TARGET"

    def test_invalid_second_step_fails(self, real_context):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.side_effect = [
            Result.ok(_answers(target_region="middle-left")), _cell_answer("B2"), _cell_answer("Z9"),
        ]
        assert not GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context).success

    def test_temporary_grid_images_are_removed(self, real_context):
        seen = []
        gpt_client = MagicMock()

        def ask(image_path, *args, **kwargs):
            if "autopilot_grid_" in image_path:
                seen.append(image_path)
                assert os.path.exists(image_path)
                return _cell_answer("A1")
            return Result.ok(_answers(target_region="middle-left"))

        gpt_client.ask_with_image.side_effect = ask
        GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert len(seen) == 2
        assert not any(os.path.exists(path) for path in seen)
