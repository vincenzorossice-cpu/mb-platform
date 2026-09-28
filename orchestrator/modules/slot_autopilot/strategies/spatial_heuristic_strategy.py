from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.strategies.base import (
    LocateCandidate,
    LocateContext,
    LocatorStrategy,
)

# Griglia 3x3 generica (reimplementata localmente, non importata da
# test_slot_autonomous.py): nessuna coordinata specifica di slot.
_REGION_FRACTIONS = {
    "top-left": (1 / 6, 1 / 6), "top-center": (1 / 2, 1 / 6), "top-right": (5 / 6, 1 / 6),
    "middle-left": (1 / 6, 1 / 2), "center": (1 / 2, 1 / 2), "middle-right": (5 / 6, 1 / 2),
    "bottom-left": (1 / 6, 5 / 6), "bottom-center": (1 / 2, 5 / 6), "bottom-right": (5 / 6, 5 / 6),
}


class SpatialHeuristicStrategy(LocatorStrategy):
    """Quarta scelta, la piu' debole: una regione grossolana della griglia 3x3
    dichiarata nel profilo (region_hint), tradotta geometricamente."""
    name = "spatial_heuristic"

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        element = context.profile.elements.get(element_name) if context.profile else None
        region_hint = element.region_hint if element else None
        if not region_hint or region_hint not in _REGION_FRACTIONS:
            return Result.fail(
                f"No region_hint configured for '{element_name}'", "SPATIAL_HEURISTIC_NO_REGION_HINT",
            )
        fx, fy = _REGION_FRACTIONS[region_hint]
        x = int(round(context.screen_width * fx))
        y = int(round(context.screen_height * fy))
        return Result.ok(LocateCandidate(x=x, y=y, confidence=0.4, strategy_name=self.name))
