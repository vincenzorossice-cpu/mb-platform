from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_autopilot.strategies.spatial_heuristic_strategy import (
    SpatialHeuristicStrategy,
)


def _context(profile):
    return LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/shot.png",
        screen_width=1080, screen_height=2400, profile=profile,
    )


class TestSpatialHeuristicStrategy:
    def test_bottom_center_region(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="elk", screen_width=1080, screen_height=2400,
            elements={"spin": ElementSpec(name="spin", region_hint="bottom-center")},
        )
        strategy = SpatialHeuristicStrategy()
        result = strategy.locate("spin", _context(profile))
        assert result.success
        assert (result.data.x, result.data.y) == (540, 2000)
        assert result.data.confidence == 0.4

    def test_missing_region_hint(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="elk", screen_width=1080, screen_height=2400,
            elements={"spin": ElementSpec(name="spin", ocr_labels=["SPIN"])},
        )
        strategy = SpatialHeuristicStrategy()
        result = strategy.locate("spin", _context(profile))
        assert not result.success
        assert result.error_code == "SPATIAL_HEURISTIC_NO_REGION_HINT"
