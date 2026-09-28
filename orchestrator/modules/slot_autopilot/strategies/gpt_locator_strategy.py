from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.interface import choice_question
from orchestrator.modules.slot_autopilot.strategies.base import (
    LocateCandidate,
    LocateContext,
    LocatorStrategy,
)
from orchestrator.tests.validation.test_slot_autonomous import resolve_target


class GPTLocatorStrategy(LocatorStrategy):
    """Sesta scelta, fallback verso GPT-4o-mini Vision: usata solo se
    registrata nella catena (AutoPilotConfig.enable_gpt_fallback=False la
    esclude semplicemente dal dict di strategie passato a ElementLocator,
    nessun flag interno da controllare qui)."""
    name = "gpt"

    def __init__(self, gpt_client):
        self.gpt_client = gpt_client

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        candidate_texts = [text for text, _coords, _conf in context.raw_texts]
        questions = {
            "action": choice_question(
                f"Tap the UI element named '{element_name}' if you can see it on screen.",
                {"tap": "Touch the element", "stop": "Element not visible / cannot proceed"},
            ),
            "target": choice_question(
                "Which element should I tap? Answer with its exact visible text, or leave it "
                "and use a region instead.",
                {text: text for text in candidate_texts},
            ),
        }
        result = self.gpt_client.ask_with_image(
            context.screenshot_path, f"Locate the element: {element_name}", questions,
            context.screen_width, context.screen_height,
        )
        if not result.success:
            return Result.fail(result.error, result.error_code)

        answers = result.data.get("answers", {})
        action = answers.get("action", {}).get("choice")
        if action != "tap":
            return Result.fail(f"GPT did not choose to tap for '{element_name}'", "GPT_LOCATOR_NO_TAP")

        target = answers.get("target", {}).get("choice")
        coords = resolve_target(target, context.raw_texts, context.screen_width, context.screen_height)
        if coords is None:
            return Result.fail(
                f"GPT target could not be resolved for '{element_name}'", "GPT_LOCATOR_UNRESOLVED_TARGET",
            )

        confidence = answers.get("target", {}).get("confidence", 0.5)
        x, y = coords
        return Result.ok(LocateCandidate(x=x, y=y, confidence=confidence, strategy_name=self.name))
