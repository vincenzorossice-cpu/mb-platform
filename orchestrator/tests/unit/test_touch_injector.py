import unittest
from unittest.mock import patch, MagicMock, call
from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import (
    UIElement,
    ScreenContent,
    BetStatus,
)
from orchestrator.modules.touch_injector.injector import TouchInjector
from orchestrator.modules.touch_injector.interface import LayBetResult


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


class TestTapCoordinates(unittest.TestCase):
    """test_tap_coordinates_calls_adb + test_tap_coordinates_humanization"""

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_coordinates_calls_adb(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        result = injector.tap_coordinates(DEVICE, 500, 600)

        self.assertTrue(result.success)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:5], ["adb", "-s", DEVICE, "shell", "input"])
        self.assertEqual(cmd[5], "tap")

    @patch("orchestrator.modules.touch_injector.injector.random.uniform", return_value=0.5)
    @patch("orchestrator.modules.touch_injector.injector.random.randint", return_value=3)
    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_coordinates_humanization(self, mock_run, mock_sleep, mock_randint, mock_uniform):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        result = injector.tap_coordinates(DEVICE, 500, 600)

        self.assertTrue(result.success)
        cmd = mock_run.call_args[0][0]
        # Con jitter +3, le coordinate devono essere 503, 603
        self.assertEqual(cmd[6], "503")
        self.assertEqual(cmd[7], "603")
        # Delay umanizzato deve essere stato chiamato
        mock_sleep.assert_called_once_with(0.5)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_coordinates_jitter_range(self, mock_run, mock_sleep):
        """Verifica che il jitter sia nell'intervallo ±5px."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        x_orig, y_orig = 500, 600
        for _ in range(50):
            injector.tap_coordinates(DEVICE, x_orig, y_orig)
            cmd = mock_run.call_args[0][0]
            tap_x = int(cmd[6])
            tap_y = int(cmd[7])
            self.assertGreaterEqual(tap_x, x_orig - 5)
            self.assertLessEqual(tap_x, x_orig + 5)
            self.assertGreaterEqual(tap_y, y_orig - 5)
            self.assertLessEqual(tap_y, y_orig + 5)


class TestTapElement(unittest.TestCase):

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_element_success(self, mock_run, mock_sleep):
        """Mock ScreenReader.find_element → tap sul centro bounds [100,200,300,400] → centro (200,300)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        reader = MagicMock()
        elem = _make_element("Scommetti", bounds=[100, 200, 300, 400])
        reader.find_element.return_value = Result.ok(elem)
        reader.read_screen.return_value = Result.ok(_make_screen())

        injector = TouchInjector(screen_reader=reader)
        result = injector.tap_element(DEVICE, "Scommetti")

        self.assertTrue(result.success)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[5], "tap")
        # Centro di [100,200,300,400] = (200,300) ± jitter 5px
        tap_x = int(cmd[6])
        tap_y = int(cmd[7])
        self.assertGreaterEqual(tap_x, 195)
        self.assertLessEqual(tap_x, 205)
        self.assertGreaterEqual(tap_y, 295)
        self.assertLessEqual(tap_y, 305)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_element_not_found(self, mock_run, mock_sleep):
        """find_element fallisce → TAP_MISSED dopo retry."""
        reader = MagicMock()
        reader.find_element.return_value = Result.fail("not found", "NO_ELEMENTS_FOUND")

        injector = TouchInjector(screen_reader=reader)
        result = injector.tap_element(DEVICE, "invisible_button")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TAP_MISSED")
        # find_element chiamato 3 volte (1 + 2 retry)
        self.assertEqual(reader.find_element.call_count, 3)


class TestInputText(unittest.TestCase):

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_input_text_success(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        result = injector.input_text(DEVICE, "10.00")

        self.assertTrue(result.success)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:5], ["adb", "-s", DEVICE, "shell", "input"])
        self.assertEqual(cmd[5], "text")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_input_text_with_field_hint(self, mock_run, mock_sleep):
        """Prima tap sul campo (via find_element), poi input text."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        reader = MagicMock()
        elem = _make_element("importo", bounds=[50, 100, 200, 150])
        reader.find_element.return_value = Result.ok(elem)
        reader.read_screen.return_value = Result.ok(_make_screen())

        injector = TouchInjector(screen_reader=reader)
        result = injector.input_text(DEVICE, "25.00", field_hint="importo")

        self.assertTrue(result.success)
        # subprocess.run chiamato almeno 2 volte: tap + input text
        self.assertGreaterEqual(mock_run.call_count, 2)
        # L'ultimo call deve essere input text
        last_cmd = mock_run.call_args_list[-1][0][0]
        self.assertEqual(last_cmd[5], "text")


class TestPlaceLayBetVerifyThenAct(unittest.TestCase):
    """Tests per il pattern verify-then-act su ADB timeout durante place_lay_bet."""

    def _make_injector_with_timeout(self):
        """Crea un injector dove il tap di conferma lay alza TimeoutExpired."""
        reader = MagicMock()
        elem = _make_element("place lay", bounds=[100, 500, 300, 550])
        reader.find_element.return_value = Result.ok(elem)
        reader.read_screen.return_value = Result.ok(_make_screen())
        injector = TouchInjector(screen_reader=reader, bet_confirm_timeout=2)
        return injector, reader

    @staticmethod
    def _lay_side_effect_factory():
        """Side effect che fa passare i tap di focus campo e alza timeout solo
        sul tap di conferma (il 3° tap: focus odds, focus stake, confirm)."""
        state = {"tap_count": 0}

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[5] == "text":
                return MagicMock(returncode=0, stderr="")
            if cmd[5] == "tap":
                state["tap_count"] += 1
                if state["tap_count"] <= 2:
                    # Tap di focus per i campi odds e stake → ok
                    return MagicMock(returncode=0, stderr="")
                # 3° tap = conferma lay → timeout
                import subprocess as sp
                raise sp.TimeoutExpired(cmd, 10)
            return MagicMock(returncode=0, stderr="")

        return side_effect

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_place_lay_bet_verify_matched(self, mock_run, mock_sleep):
        """ADB timeout durante conferma → read_bet_status='matched' → successo."""
        injector, reader = self._make_injector_with_timeout()
        mock_run.side_effect = self._lay_side_effect_factory()

        reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="matched", lay_odds=2.10, confidence=1.0)
        )

        result = injector.place_lay_bet(DEVICE, "match_odds", 2.10, 50.0)

        self.assertTrue(result.success)
        self.assertTrue(result.data.confirmed)
        self.assertEqual(result.data.actual_odds, 2.10)
        reader.read_bet_status.assert_called()

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_place_lay_bet_verify_not_found(self, mock_run, mock_sleep):
        """ADB timeout → read_bet_status='not_found' → LAY_NOT_PLACED_RETRY_SAFE."""
        injector, reader = self._make_injector_with_timeout()
        mock_run.side_effect = self._lay_side_effect_factory()

        reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="not_found", lay_odds=None, confidence=1.0)
        )

        result = injector.place_lay_bet(DEVICE, "match_odds", 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "LAY_NOT_PLACED_RETRY_SAFE")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_place_lay_bet_ambiguous_state(self, mock_run, mock_sleep):
        """ADB timeout → read_bet_status fallisce → AMBIGUOUS_STATE."""
        injector, reader = self._make_injector_with_timeout()
        mock_run.side_effect = self._lay_side_effect_factory()

        reader.read_bet_status.return_value = Result.fail("screen unreadable", "OCR_LOW_CONFIDENCE")

        result = injector.place_lay_bet(DEVICE, "match_odds", 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "AMBIGUOUS_STATE")


class TestHumanTapDelay(unittest.TestCase):

    @patch("orchestrator.modules.touch_injector.injector.random.uniform")
    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_human_tap_random_delay(self, mock_run, mock_sleep, mock_uniform):
        """Verifica che time.sleep sia chiamato con valore da random.uniform(0.3, 1.5)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_uniform.return_value = 0.87

        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)
        injector._human_tap(DEVICE, 100, 200)

        mock_uniform.assert_called_once_with(0.3, 1.5)
        mock_sleep.assert_called_once_with(0.87)


if __name__ == "__main__":
    unittest.main()
