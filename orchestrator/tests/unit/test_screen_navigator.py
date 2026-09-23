import unittest
from unittest.mock import MagicMock, patch

from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import UIElement, ScreenContent
from orchestrator.modules.screen_navigator.navigator import ScreenNavigator


DEVICE = "emulator-5554"


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
        touch_injector.scroll.assert_called_once_with(DEVICE, "down")


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


if __name__ == "__main__":
    unittest.main()
