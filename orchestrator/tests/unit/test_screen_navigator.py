import pytest
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import UIElement, ScreenContent
from orchestrator.modules.screen_navigator.navigator import ScreenNavigator


DEVICE = "emulator-5554"


@pytest.fixture(autouse=True)
def _no_tap_settle_delay(monkeypatch):
    monkeypatch.setattr(ScreenNavigator, "TAP_SETTLE_SECONDS", 0)


def _make_element(text="OK", bounds=None, resource_id=None):
    return UIElement(
        text=text,
        bounds=bounds or [100, 200, 300, 400],
        resource_id=resource_id,
        class_name=None,
        confidence=1.0,
    )


def _make_screen(elements=None, screen_type="unknown"):
    return ScreenContent(
        elements=elements or [],
        screen_type=screen_type,
        confidence=1.0,
        method="uiautomator",
        screenshot_path=None,
    )


def _answers(action, target=None, confidence=0.9, text_to_type=None):
    data = {
        "answers": {
            "action": {"choice": action, "confidence": confidence, "probabilities": {}},
            "target": {"choice": target, "confidence": confidence, "probabilities": {}},
        }
    }
    if text_to_type is not None:
        data["answers"]["text_to_type"] = {"choice": text_to_type, "confidence": confidence, "probabilities": {}}
    return data


def _make_navigator(screen_reader=None, touch_injector=None, decision_engine_client=None, max_steps=20):
    return ScreenNavigator(
        screen_reader=screen_reader or MagicMock(),
        touch_injector=touch_injector or MagicMock(),
        decision_engine_client=decision_engine_client or MagicMock(),
        max_steps=max_steps,
    )


class TestNavigateHappyPath(unittest.TestCase):

    def test_tap_then_done(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Accedi")]))
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        decision_engine_client = MagicMock()
        decision_engine_client.ask.side_effect = [
            Result.ok(_answers("tap", target="Accedi")),
            Result.ok(_answers("done")),
        ]
        navigator = _make_navigator(screen_reader, touch_injector, decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Accedi al conto")

        self.assertTrue(result.success)
        self.assertTrue(result.data)
        touch_injector.tap_element.assert_called_once_with(DEVICE, "Accedi")
        self.assertEqual(decision_engine_client.ask.call_count, 2)


class TestInputTextAction(unittest.TestCase):

    def test_input_text_delegates_with_field_hint(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Username")]))
        touch_injector = MagicMock()
        touch_injector.input_text.return_value = Result.ok(True)
        decision_engine_client = MagicMock()
        decision_engine_client.ask.side_effect = [
            Result.ok(_answers("input_text", target="Username", text_to_type="mario.rossi")),
            Result.ok(_answers("done")),
        ]
        navigator = _make_navigator(screen_reader, touch_injector, decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Login")

        self.assertTrue(result.success)
        touch_injector.input_text.assert_called_once_with(DEVICE, "mario.rossi", field_hint="Username")


class TestScrollAction(unittest.TestCase):

    def test_scroll_delegates_direction(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Promo")]))
        touch_injector = MagicMock()
        touch_injector.scroll.return_value = Result.ok(True)
        decision_engine_client = MagicMock()
        decision_engine_client.ask.side_effect = [
            Result.ok(_answers("scroll", target="down")),
            Result.ok(_answers("done")),
        ]
        navigator = _make_navigator(screen_reader, touch_injector, decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Trova la promo")

        self.assertTrue(result.success)
        touch_injector.scroll.assert_called_once_with(DEVICE, "down", duration_ms=900, fraction=0.3)


class TestWaitAction(unittest.TestCase):

    @patch("orchestrator.modules.screen_navigator.navigator.time.sleep")
    def test_wait_then_done(self, mock_sleep):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen())
        decision_engine_client = MagicMock()
        decision_engine_client.ask.side_effect = [
            Result.ok(_answers("wait")),
            Result.ok(_answers("done")),
        ]
        navigator = _make_navigator(screen_reader, decision_engine_client=decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Attendi caricamento")

        self.assertTrue(result.success)
        mock_sleep.assert_called_once_with(3)


class TestLowConfidence(unittest.TestCase):

    def test_low_confidence_returns_hitl_failure(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Boh")]))
        decision_engine_client = MagicMock()
        decision_engine_client.ask.return_value = Result.ok(_answers("tap", target="Boh", confidence=0.1))
        navigator = _make_navigator(screen_reader, decision_engine_client=decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Obiettivo ambiguo")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "LOW_CONFIDENCE_HITL")


class TestUnrecognizedAction(unittest.TestCase):

    def test_unrecognized_action_does_not_crash_and_continues(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen())
        decision_engine_client = MagicMock()
        decision_engine_client.ask.side_effect = [
            Result.ok(_answers("fly")),
            Result.ok(_answers("done")),
        ]
        navigator = _make_navigator(screen_reader, decision_engine_client=decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Obiettivo qualsiasi")

        self.assertTrue(result.success)
        self.assertEqual(decision_engine_client.ask.call_count, 2)


class TestDecisionEngineUnreachable(unittest.TestCase):

    def test_ask_failure_propagates_immediately(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen())
        decision_engine_client = MagicMock()
        decision_engine_client.ask.return_value = Result.fail("down", "DECISION_ENGINE_UNREACHABLE")
        navigator = _make_navigator(screen_reader, decision_engine_client=decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Obiettivo qualsiasi")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "DECISION_ENGINE_UNREACHABLE")
        self.assertEqual(decision_engine_client.ask.call_count, 1)


class TestScreenReadFailure(unittest.TestCase):

    def test_screen_read_failure_propagates(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.fail("adb down", "ADB_TIMEOUT")
        decision_engine_client = MagicMock()
        navigator = _make_navigator(screen_reader, decision_engine_client=decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Obiettivo qualsiasi")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SCREEN_READ_FAILED")
        decision_engine_client.ask.assert_not_called()


class TestMaxStepsExceeded(unittest.TestCase):

    def test_never_done_within_max_steps(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Loop")]))
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        decision_engine_client = MagicMock()
        decision_engine_client.ask.return_value = Result.ok(_answers("tap", target="Loop"))
        navigator = _make_navigator(screen_reader, touch_injector, decision_engine_client, max_steps=2)

        result = navigator.navigate(DEVICE, goal="Obiettivo irraggiungibile")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "GOAL_NOT_REACHED")
        self.assertEqual(decision_engine_client.ask.call_count, 2)


class TestActionFailureDoesNotAbortLoop(unittest.TestCase):

    def test_failed_tap_is_logged_and_loop_continues(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Accedi")]))
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.fail("not found", "TAP_MISSED")
        decision_engine_client = MagicMock()
        decision_engine_client.ask.side_effect = [
            Result.ok(_answers("tap", target="Accedi")),
            Result.ok(_answers("done")),
        ]
        navigator = _make_navigator(screen_reader, touch_injector, decision_engine_client)

        result = navigator.navigate(DEVICE, goal="Accedi al conto")

        self.assertTrue(result.success)
        self.assertEqual(decision_engine_client.ask.call_count, 2)


class TestInvisibleElementsFiltered(unittest.TestCase):

    def test_zero_bounds_element_excluded_from_target_candidates(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[
            _make_element(text="Menu nascosto", bounds=[0, 0, 0, 0]),
            _make_element(text="Promozioni", bounds=[10, 20, 100, 60]),
        ]))
        decision_engine_client = MagicMock()
        decision_engine_client.ask.return_value = Result.ok(_answers("done"))
        navigator = _make_navigator(screen_reader, decision_engine_client=decision_engine_client)

        navigator.navigate(DEVICE, goal="Vai alle promozioni")

        _, questions = decision_engine_client.ask.call_args.args
        target_criteria = questions["target"]["criteria"]
        self.assertIn("Promozioni", target_criteria)
        self.assertNotIn("Menu nascosto", target_criteria)


class TestAntiLoopRecovery(unittest.TestCase):

    def test_third_consecutive_same_action_triggers_recovery_scroll(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Promo")]))
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        touch_injector.scroll.return_value = Result.ok(True)
        decision_engine_client = MagicMock()
        decision_engine_client.ask.side_effect = [
            Result.ok(_answers("tap", target="Promo")),
            Result.ok(_answers("tap", target="Promo")),
            Result.ok(_answers("tap", target="Promo")),
            Result.ok(_answers("done")),
        ]
        navigator = _make_navigator(screen_reader, touch_injector, decision_engine_client, max_steps=10)

        result = navigator.navigate(DEVICE, goal="Attiva la promo")

        self.assertTrue(result.success)
        # Il 3° tentativo consecutivo di "tap 'Promo'" scatena lo scroll di recovery
        # invece di un altro tap.
        touch_injector.scroll.assert_called_once_with(DEVICE, "down")
        self.assertEqual(touch_injector.tap_element.call_count, 2)

    def test_stuck_loop_after_recovery_returns_navigation_stuck(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Promo")]))
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        touch_injector.scroll.return_value = Result.ok(True)
        decision_engine_client = MagicMock()
        # 3 tap identici -> recovery scroll -> altri 3 tap identici -> stuck
        decision_engine_client.ask.return_value = Result.ok(_answers("tap", target="Promo"))
        navigator = _make_navigator(screen_reader, touch_injector, decision_engine_client, max_steps=20)

        result = navigator.navigate(DEVICE, goal="Attiva la promo")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "NAVIGATION_STUCK")
        touch_injector.scroll.assert_called_once_with(DEVICE, "down")

    def test_different_action_resets_loop_counter(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[
            _make_element(text="Promo"), _make_element(text="Altro"),
        ]))
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        touch_injector.scroll.return_value = Result.ok(True)
        decision_engine_client = MagicMock()
        decision_engine_client.ask.side_effect = [
            Result.ok(_answers("tap", target="Promo")),
            Result.ok(_answers("tap", target="Altro")),
            Result.ok(_answers("tap", target="Promo")),
            Result.ok(_answers("done")),
        ]
        navigator = _make_navigator(screen_reader, touch_injector, decision_engine_client, max_steps=10)

        result = navigator.navigate(DEVICE, goal="Attiva la promo")

        self.assertTrue(result.success)
        touch_injector.scroll.assert_not_called()


class TestDemoMode(unittest.TestCase):

    def test_demo_mode_rewrites_goal_sent_to_decision_engine(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Prova")]))
        decision_engine_client = MagicMock()
        decision_engine_client.ask.return_value = Result.ok(_answers("done"))
        navigator = _make_navigator(screen_reader, decision_engine_client=decision_engine_client)

        navigator.navigate(DEVICE, goal="Gioca alla slot X", demo_mode=True)

        state, _ = decision_engine_client.ask.call_args.args
        self.assertIn("demo", state.lower())
        self.assertIn("Prova", state)

    def test_demo_mode_off_leaves_goal_untouched(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text="Gioca")]))
        decision_engine_client = MagicMock()
        decision_engine_client.ask.return_value = Result.ok(_answers("done"))
        navigator = _make_navigator(screen_reader, decision_engine_client=decision_engine_client)

        navigator.navigate(DEVICE, goal="Gioca alla slot X", demo_mode=False)

        state, _ = decision_engine_client.ask.call_args.args
        self.assertIn("Goal: Gioca alla slot X\n", state)


class TestDecisionEngineProviderSelection(unittest.TestCase):
    """Quando decision_engine_client non e' iniettato esplicitamente, ScreenNavigator
    sceglie l'implementazione in base a DECISION_ENGINE_PROVIDER."""

    def test_default_provider_uses_openjev_client(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("DECISION_ENGINE_PROVIDER", None)
            with patch("orchestrator.modules.decision_engine.client.DecisionEngineClient") as mock_cls:
                navigator = ScreenNavigator(screen_reader=MagicMock(), touch_injector=MagicMock())
        mock_cls.assert_called_once()
        self.assertIs(navigator.decision_engine_client, mock_cls.return_value)

    def test_openjev_provider_uses_openjev_client(self):
        with patch.dict("os.environ", {"DECISION_ENGINE_PROVIDER": "openjev"}):
            with patch("orchestrator.modules.decision_engine.client.DecisionEngineClient") as mock_cls:
                navigator = ScreenNavigator(screen_reader=MagicMock(), touch_injector=MagicMock())
        mock_cls.assert_called_once()
        self.assertIs(navigator.decision_engine_client, mock_cls.return_value)

    def test_gpt4o_provider_uses_gpt_client(self):
        with patch.dict("os.environ", {"DECISION_ENGINE_PROVIDER": "gpt4o"}):
            with patch("orchestrator.modules.decision_engine.gpt_client.GPTDecisionClient") as mock_cls:
                navigator = ScreenNavigator(screen_reader=MagicMock(), touch_injector=MagicMock())
        mock_cls.assert_called_once()
        self.assertIs(navigator.decision_engine_client, mock_cls.return_value)

    def test_explicit_client_bypasses_provider_selection(self):
        explicit_client = MagicMock()
        with patch.dict("os.environ", {"DECISION_ENGINE_PROVIDER": "gpt4o"}):
            navigator = ScreenNavigator(
                screen_reader=MagicMock(), touch_injector=MagicMock(), decision_engine_client=explicit_client
            )
        self.assertIs(navigator.decision_engine_client, explicit_client)


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------
# Vision
# ----------------------------------------------------------------------

import pytest
from PIL import Image


@pytest.fixture
def screenshot(tmp_path):
    path = tmp_path / "screen.png"
    Image.new("RGB", (1000, 2000), "white").save(path)
    return str(path)


def _vision_answers(action="tap", text=None, region=None, description=None, confidence=0.9, text_to_type=""):
    target = {"text": text, "region": region} if (text or region) else None
    return Result.ok({"answers": {
        "action": {"choice": action, "confidence": confidence},
        "target": {"choice": target, "confidence": confidence},
        "screen_state": {"choice": "other", "confidence": confidence},
        "target_description": {"choice": description, "confidence": confidence},
        "text_to_type": {"choice": text_to_type, "confidence": confidence},
    }})


def _vision_setup(screenshot, texts=("Accedi",)):
    screen_reader = MagicMock()
    screen_reader.read_screen.return_value = Result.ok(_make_screen(elements=[_make_element(text=t) for t in texts]))
    screen_reader._screenshot.return_value = Result.ok(screenshot)
    touch = MagicMock()
    touch.tap_element.return_value = Result.ok(True)
    touch.tap_coordinates.return_value = Result.ok(True)
    client = MagicMock()
    return screen_reader, touch, client


class TestVisionMode:
    def test_vision_mode_uses_the_screenshot_and_taps_the_chosen_text(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(text="Accedi"), _vision_answers("done")]
        navigator = _make_navigator(reader, touch, client)

        result = navigator.navigate(DEVICE, goal="Accedi", vision_mode=True)

        assert result.success
        touch.tap_element.assert_called_once_with(DEVICE, "Accedi")
        client.ask.assert_not_called()
        assert client.ask_with_image.call_args.args[0] == screenshot
        assert client.ask_with_image.call_args.args[3:] == (1000, 2000)

    def test_a_region_without_text_taps_the_centre_of_the_region(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(region="top-left"), _vision_answers("done")]

        _make_navigator(reader, touch, client).navigate(DEVICE, goal="Apri il menu", vision_mode=True)

        touch.tap_coordinates.assert_called_once_with(DEVICE, 167, 333)

    def test_the_region_is_refined_with_the_description_when_a_refiner_is_given(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [
            _vision_answers(region="bottom-center", description="coin stack icon"), _vision_answers("done"),
        ]
        refiner = MagicMock(return_value=(512, 1777))
        navigator = ScreenNavigator(reader, touch, client, region_refiner=refiner)

        navigator.navigate(DEVICE, goal="Apri la puntata", vision_mode=True)

        refiner.assert_called_once_with("coin stack icon", screenshot, 1000, 2000)
        touch.tap_coordinates.assert_called_once_with(DEVICE, 512, 1777)

    def test_a_failing_refiner_drops_the_tap_instead_of_hitting_the_region_centre(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [
            _vision_answers(region="center", description="x"), _vision_answers("done"),
        ]
        navigator = ScreenNavigator(reader, touch, client, region_refiner=MagicMock(side_effect=RuntimeError("boom")))

        navigator.navigate(DEVICE, goal="g", vision_mode=True)

        touch.tap_coordinates.assert_not_called()
        assert navigator.last_history[0]["action"] == "tap scartato"
        assert "punto non precisato" in navigator.last_history[0]["target"]

    def test_without_a_description_the_region_centre_is_still_used(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(region="center"), _vision_answers("done")]

        ScreenNavigator(reader, touch, client, region_refiner=MagicMock(return_value=None)).navigate(
            DEVICE, goal="g", vision_mode=True)

        touch.tap_coordinates.assert_called_once_with(DEVICE, 500, 1000)

    def test_a_client_that_cannot_see_stays_on_text(self, screenshot):
        reader, touch, _ = _vision_setup(screenshot)
        client = MagicMock(spec=["ask"])
        client.ask.side_effect = [Result.ok(_answers("tap", target="Accedi")), Result.ok(_answers("done"))]

        result = _make_navigator(reader, touch, client).navigate(DEVICE, goal="Accedi", vision_mode=True)

        assert result.success and client.ask.call_count == 2

    def test_input_text_in_vision_mode_uses_the_text_the_engine_returns(self, screenshot):
        reader, touch, client = _vision_setup(screenshot, texts=("Username",))
        touch.input_text.return_value = Result.ok(True)
        client.ask_with_image.side_effect = [
            _vision_answers("input_text", text="Username", text_to_type="mario"), _vision_answers("done"),
        ]

        _make_navigator(reader, touch, client).navigate(DEVICE, goal="Login", vision_mode=True)

        touch.input_text.assert_called_once_with(DEVICE, "mario", field_hint="Username")

    def test_the_default_is_still_text_only(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask.side_effect = [Result.ok(_answers("tap", target="Accedi")), Result.ok(_answers("done"))]

        _make_navigator(reader, touch, client).navigate(DEVICE, goal="Accedi")

        client.ask_with_image.assert_not_called()

    def test_the_cache_is_invalidated_after_every_action(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(text="Accedi"), _vision_answers("done")]

        _make_navigator(reader, touch, client).navigate(DEVICE, goal="Accedi", vision_mode=True)

        assert client.invalidate_cache.call_count == 1

    def test_a_screenshot_failure_is_reported(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        reader._screenshot.return_value = Result.fail("adb down", "ADB_TIMEOUT")

        result = _make_navigator(reader, touch, client).navigate(DEVICE, goal="g", vision_mode=True)

        assert not result.success and result.error_code == "ADB_TIMEOUT"


class TestVisionFallback:
    def _navigator(self, reader, touch, client, **kwargs):
        return ScreenNavigator(reader, touch, client, vision_fallback=True, **kwargs)

    def test_low_confidence_at_text_only_repeats_the_step_with_vision(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask.side_effect = [Result.ok(_answers("tap", target="Accedi", confidence=0.05)), Result.ok(_answers("done"))]
        client.ask_with_image.side_effect = [_vision_answers(text="Accedi")]

        result = self._navigator(reader, touch, client).navigate(DEVICE, goal="Accedi")

        assert result.success
        touch.tap_element.assert_called_once_with(DEVICE, "Accedi")
        assert client.ask_with_image.call_count == 1

    def test_without_the_fallback_a_low_confidence_still_fails_as_before(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask.return_value = Result.ok(_answers("tap", target="Accedi", confidence=0.05))

        result = _make_navigator(reader, touch, client).navigate(DEVICE, goal="Accedi")

        assert result.error_code == "LOW_CONFIDENCE_HITL"
        client.ask_with_image.assert_not_called()

    def test_a_screen_without_readable_text_goes_to_vision(self, screenshot):
        reader, touch, client = _vision_setup(screenshot, texts=())
        client.ask_with_image.side_effect = [_vision_answers(region="center"), _vision_answers("done")]
        client.ask.side_effect = [Result.ok(_answers("done"))]

        result = self._navigator(reader, touch, client).navigate(DEVICE, goal="g")

        assert result.success
        touch.tap_coordinates.assert_called_once_with(DEVICE, 500, 1000)

    def test_a_failed_screen_read_goes_to_vision_instead_of_failing(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        reader.read_screen.return_value = Result.fail("uiautomator blocked", "SCREEN_READ_FAILED")
        client.ask_with_image.side_effect = [_vision_answers(region="center"), _vision_answers("done")]

        result = self._navigator(reader, touch, client).navigate(DEVICE, goal="g")

        assert result.success

    def test_the_vision_budget_is_capped(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask.return_value = Result.ok(_answers("tap", target="Accedi", confidence=0.05))
        client.ask_with_image.return_value = _vision_answers("wait")

        result = self._navigator(reader, touch, client, max_vision_steps=1).navigate(DEVICE, goal="g")

        assert result.error_code == "LOW_CONFIDENCE_HITL"      # budget finito: torna il comportamento di sempre
        assert client.ask_with_image.call_count == 1

    def test_a_text_loop_gets_one_vision_step_before_the_scroll_recovery(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        touch.scroll.return_value = Result.ok(True)
        client.ask.return_value = Result.ok(_answers("tap", target="Accedi"))
        client.ask_with_image.side_effect = [_vision_answers(region="top-right"), _vision_answers("done")]

        result = self._navigator(reader, touch, client).navigate(DEVICE, goal="g")

        assert result.success
        assert client.ask_with_image.call_count == 2
        touch.scroll.assert_not_called()


class TestLastHistory:
    def test_the_full_history_is_readable_after_a_failure(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(text="Promo"), _vision_answers(text="Attiva")] + [
            _vision_answers("wait") for _ in range(5)
        ]
        navigator = _make_navigator(reader, touch, client, max_steps=2)

        result = navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert not result.success
        assert [(h["action"], h["target"].split(" (")[0]) for h in navigator.last_history] == [
            ("tap", "Promo"), ("tap", "Attiva")]


class TestGentleScroll:
    def test_scroll_up_is_executed_up_slowly(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        touch.scroll.return_value = Result.ok(True)
        client.ask_with_image.side_effect = [_vision_answers("scroll", text="up"), _vision_answers("done")]

        with patch("orchestrator.modules.screen_navigator.navigator.time.sleep"):
            _make_navigator(reader, touch, client).navigate(DEVICE, goal="g", vision_mode=True)

        touch.scroll.assert_called_once_with(DEVICE, "up", duration_ms=900, fraction=0.3)

    def test_an_unchanged_screen_after_the_scroll_is_reported_as_end_of_page(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)   # sempre lo stesso screenshot
        touch.scroll.return_value = Result.ok(True)
        client.ask_with_image.side_effect = [_vision_answers("scroll", text="down"), _vision_answers("done")]
        navigator = _make_navigator(reader, touch, client)

        with patch("orchestrator.modules.screen_navigator.navigator.time.sleep"):
            navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert "fine pagina" in navigator.last_history[0]["target"]

    def test_a_moved_page_has_no_end_of_page_note(self, screenshot, tmp_path):
        reader, touch, client = _vision_setup(screenshot)
        other = tmp_path / "moved.png"
        Image.effect_noise((1000, 2000), 80).save(other)
        reader._screenshot.side_effect = [Result.ok(screenshot), Result.ok(screenshot), Result.ok(str(other)),
                                          Result.ok(str(other))]
        touch.scroll.return_value = Result.ok(True)
        client.ask_with_image.side_effect = [_vision_answers("scroll", text="down"), _vision_answers("done")]
        navigator = _make_navigator(reader, touch, client)

        with patch("orchestrator.modules.screen_navigator.navigator.time.sleep"):
            navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert "fine pagina" not in str(navigator.last_history[0]["target"])


class TestSystemBarGuard:
    def test_a_tap_on_the_system_bar_is_dropped_and_recorded(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(region="bottom-center", description="x"),
                                             _vision_answers("done")]
        refiner = MagicMock(return_value=(500, 1980))   # 99% dell'altezza (2000)
        navigator = ScreenNavigator(reader, touch, client, region_refiner=refiner)

        result = navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert result.success
        touch.tap_coordinates.assert_not_called()
        assert navigator.last_history[0]["action"] == "tap scartato"

    def test_a_tap_above_the_bar_is_executed(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(region="bottom-center", description="x"),
                                             _vision_answers("done")]
        navigator = ScreenNavigator(reader, touch, client, region_refiner=MagicMock(return_value=(500, 1800)))

        navigator.navigate(DEVICE, goal="g", vision_mode=True)

        touch.tap_coordinates.assert_called_once_with(DEVICE, 500, 1800)


class TestStepObserverAndReasoning:
    def test_the_observer_gets_every_screenshot_seen(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(text="Accedi"), _vision_answers("done")]
        seen = []
        navigator = ScreenNavigator(reader, touch, client, step_observer=lambda step, path: seen.append((step, path)))

        navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert seen == [(1, screenshot), (2, screenshot)]

    def test_the_reasoning_is_logged(self, screenshot, caplog):
        import logging
        reader, touch, client = _vision_setup(screenshot)
        answers = _vision_answers("done").data
        answers["answers"]["reasoning"] = {"choice": "sono dentro la slot", "confidence": 0.9}
        client.ask_with_image.return_value = Result.ok(answers)

        with caplog.at_level(logging.INFO):
            _make_navigator(reader, touch, client).navigate(DEVICE, goal="g", vision_mode=True)

        assert "sono dentro la slot" in caplog.text


class TestTapEffectNote:
    def test_an_unchanged_screen_after_a_tap_is_reported(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers(text="Slot"), _vision_answers("done")]
        navigator = _make_navigator(reader, touch, client)

        navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert "schermo invariato" in navigator.last_history[0]["target"]

    def test_a_changed_screen_has_no_note_even_if_tap_element_says_no_effect(self, screenshot, tmp_path):
        reader, touch, client = _vision_setup(screenshot)
        touch.tap_element.return_value = Result.fail("Tap had no effect", "TAP_NO_EFFECT")
        other = tmp_path / "changed.png"
        Image.effect_noise((1000, 2000), 80).save(other)
        reader._screenshot.side_effect = [Result.ok(screenshot), Result.ok(screenshot), Result.ok(str(other)),
                                          Result.ok(str(other))]
        client.ask_with_image.side_effect = [_vision_answers(text="Slot"), _vision_answers("done")]
        navigator = _make_navigator(reader, touch, client)

        navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert navigator.last_history[0]["target"] == "Slot"


class TestAppGuard:
    def test_a_lost_app_is_restored_without_asking_the_model(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers("done")]
        guard = MagicMock(side_effect=[False, True])
        restore = MagicMock()
        navigator = ScreenNavigator(reader, touch, client, app_guard=guard, app_restore=restore)

        with patch("orchestrator.modules.screen_navigator.navigator.time.sleep"):
            result = navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert result.success
        restore.assert_called_once_with(DEVICE)
        assert navigator.last_history[0]["action"] == "rientro nell'app"
        assert client.ask_with_image.call_count == 1

    def test_restores_are_capped(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers("wait")] * 6 + [_vision_answers("done")]
        restore = MagicMock()
        navigator = ScreenNavigator(reader, touch, client, app_guard=lambda d: False, app_restore=restore, max_steps=6)

        with patch("orchestrator.modules.screen_navigator.navigator.time.sleep"):
            navigator.navigate(DEVICE, goal="g", vision_mode=True)

        assert restore.call_count == 3

    def test_a_failing_guard_is_treated_as_fine(self, screenshot):
        reader, touch, client = _vision_setup(screenshot)
        client.ask_with_image.side_effect = [_vision_answers("done")]
        restore = MagicMock()
        navigator = ScreenNavigator(reader, touch, client, app_guard=MagicMock(side_effect=OSError), app_restore=restore)

        assert navigator.navigate(DEVICE, goal="g", vision_mode=True).success
        restore.assert_not_called()


class TestBookHints:
    def test_sisal_hints_reach_the_state_and_forbid_the_game_tile(self):
        state = _make_navigator()._build_state("SISAL", "g", 1, [], ["x"])
        assert "Notes about SISAL" in state and "NEVER tap the game's box" in state and "'i'" in state

    def test_unknown_book_has_no_notes(self):
        assert "Notes about" not in _make_navigator()._build_state("ALTROBOOK", "g", 1, [], ["x"])

    def test_missing_or_broken_file_is_tolerated(self, tmp_path):
        from orchestrator.modules.screen_navigator.navigator import load_book_hints
        assert load_book_hints("SISAL", str(tmp_path / "nope.json")) == []
        broken = tmp_path / "b.json"
        broken.write_text("{not json")
        assert load_book_hints("SISAL", str(broken)) == []
