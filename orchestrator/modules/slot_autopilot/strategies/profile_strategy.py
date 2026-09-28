from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.strategies.base import (
    LocateCandidate,
    LocateContext,
    LocatorStrategy,
)


class ProfileStrategy(LocatorStrategy):
    """Prima scelta della catena: coordinate curate a mano nel ProviderProfile.
    Confidenza massima (1.0) perche' sono state verificate da un umano, non
    scoperte a runtime."""
    name = "profile"

    def __init__(self, profile_loader):
        self.profile_loader = profile_loader

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        if context.profile is None:
            return Result.fail("No profile loaded", "PROFILE_NOT_LOADED")
        element = context.profile.elements.get(element_name)
        if element is None:
            return Result.fail(f"Unknown element in profile: {element_name}", "PROFILE_ELEMENT_UNKNOWN")

        abs_result = self.profile_loader.to_abs(
            element, context.screen_width, context.screen_height,
            canvas_bbox=context.profile.canvas_bbox,
        )
        if not abs_result.success:
            return Result.fail(abs_result.error, "PROFILE_ELEMENT_HAS_NO_COORDS")

        x, y = abs_result.data
        return Result.ok(LocateCandidate(x=x, y=y, confidence=1.0, strategy_name=self.name))
