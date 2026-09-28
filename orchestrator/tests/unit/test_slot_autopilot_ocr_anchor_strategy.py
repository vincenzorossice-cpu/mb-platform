from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_autopilot.strategies.ocr_anchor_strategy import OCRAnchorStrategy


def _profile(labels):
    return ProviderProfile(
        slot_name="pirots5", provider="elk", screen_width=1080, screen_height=2400,
        elements={"spin": ElementSpec(name="spin", ocr_labels=labels)},
    )


def _context(raw_texts, profile):
    return LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/shot.png",
        screen_width=1080, screen_height=2400, raw_texts=raw_texts, profile=profile,
    )


class TestOCRAnchorStrategy:
    def test_exact_match(self):
        raw_texts = [("SPIN", (500, 2000), 0.9)]
        strategy = OCRAnchorStrategy()
        result = strategy.locate("spin", _context(raw_texts, _profile(["SPIN"])))
        assert result.success
        assert (result.data.x, result.data.y) == (500, 2000)
        assert result.data.confidence == 0.75

    def test_case_insensitive_match(self):
        raw_texts = [("spin", (500, 2000), 0.9)]
        strategy = OCRAnchorStrategy()
        result = strategy.locate("spin", _context(raw_texts, _profile(["SPIN"])))
        assert result.success

    def test_fuzzy_match(self):
        raw_texts = [("SPI N", (500, 2000), 0.9)]
        strategy = OCRAnchorStrategy()
        result = strategy.locate("spin", _context(raw_texts, _profile(["SPIN"])))
        assert result.success

    def test_no_labels_configured(self):
        raw_texts = [("SPIN", (500, 2000), 0.9)]
        strategy = OCRAnchorStrategy()
        result = strategy.locate("spin", _context(raw_texts, _profile([])))
        assert not result.success
        assert result.error_code == "OCR_ANCHOR_NO_LABELS"

    def test_label_absent_from_raw_texts(self):
        raw_texts = [("SALDO", (10, 10), 0.9)]
        strategy = OCRAnchorStrategy()
        result = strategy.locate("spin", _context(raw_texts, _profile(["SPIN"])))
        assert not result.success
        assert result.error_code == "OCR_ANCHOR_NOT_FOUND"
