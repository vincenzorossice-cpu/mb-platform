import json
from unittest.mock import MagicMock, patch

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.profile import ProviderProfile
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext


def _context(profile=None):
    return LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/shot.png",
        screen_width=1080, screen_height=2400, profile=profile,
    )


def _profile_with_canvas(canvas_bbox=(0, 247, 1080, 2400)):
    return ProviderProfile(
        slot_name="pirots5", provider="hacksaw", screen_width=1080, screen_height=2400,
        canvas_bbox=canvas_bbox,
    )


def _ok_strategy(name, x=1, y=1, confidence=0.5):
    strategy = MagicMock()
    strategy.locate.return_value = Result.ok(LocateCandidate(x=x, y=y, confidence=confidence, strategy_name=name))
    return strategy


def _fail_strategy(name, code="NOT_FOUND"):
    strategy = MagicMock()
    strategy.locate.return_value = Result.fail(f"{name} failed", code)
    return strategy


class TestLocateOrder:
    def test_fixed_order_first_success_wins(self):
        profile = _ok_strategy("profile")
        memory = _ok_strategy("memory")
        locator = ElementLocator({"profile": profile, "memory": memory})

        result = locator.locate("spin", _context())
        assert result.success
        assert result.data.strategy_name == "profile"
        memory.locate.assert_not_called()

    def test_all_fail_returns_element_not_locatable(self):
        strategies = {name: _fail_strategy(name) for name in ("profile", "memory", "hitl")}
        locator = ElementLocator(strategies)
        result = locator.locate("spin", _context())
        assert not result.success
        assert result.error_code == "ELEMENT_NOT_LOCATABLE"

    def test_unregistered_strategy_is_skipped(self):
        memory = _ok_strategy("memory")
        locator = ElementLocator({"memory": memory})
        result = locator.locate("spin", _context())
        assert result.success
        assert result.data.strategy_name == "memory"

    def test_strategy_disabled_after_max_consecutive_failures_for_that_element(self):
        template = _fail_strategy("template")
        gpt = _ok_strategy("gpt")
        locator = ElementLocator({"template": template, "gpt": gpt}, max_consecutive_failures_per_strategy=3)

        for _ in range(3):
            locator.locate("bet_minus", _context())
        assert template.locate.call_count == 3

        locator.locate("bet_minus", _context())
        assert template.locate.call_count == 3  # non richiamato: soglia raggiunta

        # Un elemento DIVERSO deve ancora provare template.
        locator.locate("spin", _context())
        assert template.locate.call_count == 4

    def test_success_resets_failure_counter(self):
        flaky = MagicMock()
        flaky.locate.side_effect = [
            Result.fail("no", "NOT_FOUND"),
            Result.fail("no", "NOT_FOUND"),
            Result.ok(LocateCandidate(x=1, y=1, confidence=0.5, strategy_name="template")),
            Result.fail("no", "NOT_FOUND"),
        ]
        locator = ElementLocator({"template": flaky}, max_consecutive_failures_per_strategy=3)
        locator.locate("bet_minus", _context())
        locator.locate("bet_minus", _context())
        result = locator.locate("bet_minus", _context())
        assert result.success
        # dopo il successo il contatore e' azzerato: un nuovo fallimento non disabilita subito
        locator.locate("bet_minus", _context())
        assert flaky.locate.call_count == 4

    def test_save_verified_candidate_noop_for_profile_and_memory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        locator = ElementLocator({})
        for strategy_name in ("profile", "memory"):
            candidate = LocateCandidate(x=1, y=1, confidence=1.0, strategy_name=strategy_name)
            result = locator.save_verified_candidate("bet_minus", candidate, "pirots5", 1080, 2400)
            assert result.success
            assert result.data is False

    def test_save_verified_candidate_for_spin_uses_legacy_memory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        locator = ElementLocator({})
        candidate = LocateCandidate(x=540, y=2000, confidence=0.9, strategy_name="ocr_anchor")
        with patch("orchestrator.modules.slot_autopilot.locator._persist_slot_memory") as mocked:
            result = locator.save_verified_candidate("spin", candidate, "pirots5", 1080, 2400)
        assert result.success
        mocked.assert_called_once_with("pirots5", known_spin_coords=(540, 2000))

    def test_save_verified_candidate_for_other_element_uses_autopilot_memory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        locator = ElementLocator({})
        candidate = LocateCandidate(x=200, y=2200, confidence=0.9, strategy_name="ocr_anchor")
        result = locator.save_verified_candidate("bet_minus", candidate, "pirots5", 1080, 2400)
        assert result.success

        path = tmp_path / "slot_memory" / "pirots5.autopilot.json"
        with open(path) as f:
            data = json.load(f)
        assert data["elements"]["bet_minus"]["x"] == 200
        assert data["elements"]["bet_minus"]["y"] == 2200

    def test_failure_counters_are_independent_per_element(self):
        template = _fail_strategy("template")
        gpt = _ok_strategy("gpt")
        locator = ElementLocator({"template": template, "gpt": gpt}, max_consecutive_failures_per_strategy=1)

        locator.locate("bet_minus", _context())
        assert template.locate.call_count == 1
        locator.locate("spin", _context())
        assert template.locate.call_count == 2  # elemento diverso: contatore indipendente
        locator.locate("bet_minus", _context())
        assert template.locate.call_count == 2  # bet_minus: gia' disabilitato


class TestCanvasExclusion:
    def test_untrusted_candidate_outside_canvas_is_discarded_falls_through(self):
        # GPT propone un punto nella barra Chrome (y=163 < canvas y1=247);
        # deve essere scartato e la catena deve scendere a hitl.
        gpt = _ok_strategy("gpt", x=891, y=163)
        hitl = _ok_strategy("hitl", x=540, y=2000)
        locator = ElementLocator({"gpt": gpt, "hitl": hitl})

        result = locator.locate("bet_minus", _context(profile=_profile_with_canvas()))
        assert result.success
        assert result.data.strategy_name == "hitl"
        gpt.locate.assert_called_once()

    def test_trusted_strategy_outside_canvas_is_not_discarded(self):
        # Una coordinata da "memory" (fidata a monte) non viene mai scartata,
        # anche se ricade fuori dal canvas dichiarato.
        memory = _ok_strategy("memory", x=891, y=163)
        locator = ElementLocator({"memory": memory})

        result = locator.locate("bet_minus", _context(profile=_profile_with_canvas()))
        assert result.success
        assert result.data.strategy_name == "memory"
        assert (result.data.x, result.data.y) == (891, 163)

    def test_no_canvas_bbox_means_no_check(self):
        profile = _profile_with_canvas(canvas_bbox=None)
        gpt = _ok_strategy("gpt", x=891, y=163)
        locator = ElementLocator({"gpt": gpt})

        result = locator.locate("bet_minus", _context(profile=profile))
        assert result.success
        assert result.data.strategy_name == "gpt"

    def test_no_profile_means_no_check(self):
        gpt = _ok_strategy("gpt", x=891, y=163)
        locator = ElementLocator({"gpt": gpt})

        result = locator.locate("bet_minus", _context(profile=None))
        assert result.success
        assert result.data.strategy_name == "gpt"

    def test_discarded_candidate_is_logged(self, caplog):
        gpt = _ok_strategy("gpt", x=891, y=163)
        hitl = _ok_strategy("hitl", x=540, y=2000)
        locator = ElementLocator({"gpt": gpt, "hitl": hitl})

        with caplog.at_level("WARNING"):
            locator.locate("bet_minus", _context(profile=_profile_with_canvas()))
        assert any("fuori dal canvas" in record.getMessage() for record in caplog.records)

    def test_discarded_candidate_counts_as_a_failure(self):
        gpt = _ok_strategy("gpt", x=891, y=163)  # sempre fuori canvas
        locator = ElementLocator({"gpt": gpt}, max_consecutive_failures_per_strategy=2)
        context = _context(profile=_profile_with_canvas())

        locator.locate("bet_minus", context)
        locator.locate("bet_minus", context)
        assert gpt.locate.call_count == 2

        result = locator.locate("bet_minus", context)
        assert not result.success
        assert gpt.locate.call_count == 2  # soglia raggiunta, non richiamato una terza volta
