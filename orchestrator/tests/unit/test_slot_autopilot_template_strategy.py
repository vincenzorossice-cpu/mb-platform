import sys
from unittest.mock import MagicMock, patch

from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_autopilot.strategies.template_strategy import TemplateStrategy


def _profile(template_path):
    return ProviderProfile(
        slot_name="pirots5", provider="elk", screen_width=1080, screen_height=2400,
        elements={"spin": ElementSpec(name="spin", template_path=template_path)},
    )


def _context(profile):
    return LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/shot.png",
        screen_width=1080, screen_height=2400, profile=profile,
    )


def _fake_cv2(imread_return, match_template_return=None, min_max_loc_return=None, raise_on_match=False):
    fake = MagicMock()
    fake.IMREAD_GRAYSCALE = 0
    fake.TM_CCOEFF_NORMED = 5
    fake.imread.return_value = imread_return
    if raise_on_match:
        fake.matchTemplate.side_effect = RuntimeError("boom")
    else:
        fake.matchTemplate.return_value = match_template_return
    fake.minMaxLoc.return_value = min_max_loc_return
    return fake


class TestTemplateStrategy:
    def test_match_above_threshold(self):
        screen_img = MagicMock()
        template_img = MagicMock(shape=(30, 60))
        fake_cv2 = _fake_cv2(
            imread_return=screen_img, match_template_return="result",
            min_max_loc_return=(0.1, 0.92, (0, 0), (100, 200)),
        )
        fake_cv2.imread.side_effect = [screen_img, template_img]

        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            strategy = TemplateStrategy(min_match_score=0.75)
            result = strategy.locate("spin", _context(_profile("spin.png")))

        assert result.success
        assert (result.data.x, result.data.y) == (100 + 30, 200 + 15)
        assert result.data.confidence == 0.92

    def test_match_below_threshold(self):
        screen_img = MagicMock()
        template_img = MagicMock(shape=(30, 60))
        fake_cv2 = _fake_cv2(
            imread_return=screen_img, match_template_return="result",
            min_max_loc_return=(0.1, 0.5, (0, 0), (100, 200)),
        )
        fake_cv2.imread.side_effect = [screen_img, template_img]

        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            strategy = TemplateStrategy(min_match_score=0.75)
            result = strategy.locate("spin", _context(_profile("spin.png")))

        assert not result.success
        assert result.error_code == "TEMPLATE_MATCH_BELOW_THRESHOLD"

    def test_missing_template_path(self):
        strategy = TemplateStrategy()
        result = strategy.locate("spin", _context(_profile(None)))
        assert not result.success
        assert result.error_code == "TEMPLATE_NOT_CONFIGURED"

    def test_image_read_error(self):
        fake_cv2 = _fake_cv2(imread_return=None)
        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            strategy = TemplateStrategy()
            result = strategy.locate("spin", _context(_profile("spin.png")))
        assert not result.success
        assert result.error_code == "TEMPLATE_IMAGE_READ_ERROR"

    def test_match_template_raises(self):
        screen_img = MagicMock()
        template_img = MagicMock(shape=(30, 60))
        fake_cv2 = _fake_cv2(imread_return=screen_img, raise_on_match=True)
        fake_cv2.imread.side_effect = [screen_img, template_img]

        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            strategy = TemplateStrategy()
            result = strategy.locate("spin", _context(_profile("spin.png")))
        assert not result.success
        assert result.error_code == "TEMPLATE_ERROR"
