import unittest
from unittest.mock import patch, MagicMock
from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import UIElement, ScreenContent
from orchestrator.modules.book_navigator.navigator import BookNavigator


DEVICE = "emulator-5554"
BETFAIR_URL = "https://mobile.betfair.com"


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


class TestOpenChrome(unittest.TestCase):

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_open_chrome_success(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        navigator = BookNavigator()

        result = navigator.open_chrome(DEVICE, BETFAIR_URL)

        self.assertTrue(result.success)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(
            cmd,
            ["adb", "-s", DEVICE, "shell", "am", "start", "-a",
             "android.intent.action.VIEW", "-d", BETFAIR_URL],
        )
        mock_sleep.assert_called_once_with(5)


class TestNavigateToBalance(unittest.TestCase):

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_already_logged_in(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(
            elements=[_make_element(text="€ 0.68")],
            screen_type="balance_page",
        ))
        navigator = BookNavigator(screen_reader=screen_reader)

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertTrue(result.data.success)
        self.assertEqual(result.data.current_screen_type, "balance_page")
        self.assertFalse(result.data.needs_login)

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_needs_login(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(
            elements=[_make_element(text="Login")],
            screen_type="unknown",
        ))
        navigator = BookNavigator(screen_reader=screen_reader)

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertFalse(result.data.success)
        self.assertTrue(result.data.needs_login)
        self.assertEqual(result.data.current_screen_type, "login_page")

    def test_navigate_to_balance_not_configured(self):
        navigator = BookNavigator()

        result = navigator.navigate_to_balance(DEVICE, "UNKNOWN")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "BOOK_NOT_CONFIGURED")

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_tap_to_balance(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Il mio conto")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[_make_element(text="€ 0.68")],
                screen_type="balance_page",
            )),
        ]
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertTrue(result.data.success)
        self.assertEqual(result.data.current_screen_type, "balance_page")
        touch_injector.tap_element.assert_called_once_with(DEVICE, "Il mio conto")

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_tap_my_bets(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Scommesse")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[_make_element(text="Le mie scommesse")],
                screen_type="my_bets",
            )),
        ]
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertTrue(result.data.success)
        self.assertEqual(result.data.current_screen_type, "my_bets")
        touch_injector.tap_element.assert_called_once_with(DEVICE, "Scommesse")

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_no_screen_reader(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        navigator = BookNavigator()

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertTrue(result.data.success)
        self.assertIn("non disponibile", result.data.message)


class TestCheckLoginStatus(unittest.TestCase):

    def test_check_login_status_logged_in(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(
            elements=[_make_element(text="€ 0.68")],
            screen_type="balance_page",
        ))
        navigator = BookNavigator(screen_reader=screen_reader)

        result = navigator.check_login_status(DEVICE)

        self.assertTrue(result.success)
        self.assertTrue(result.data)

    def test_check_login_status_logged_out(self):
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(
            elements=[_make_element(text="Login")],
            screen_type="unknown",
        ))
        navigator = BookNavigator(screen_reader=screen_reader)

        result = navigator.check_login_status(DEVICE)

        self.assertTrue(result.success)
        self.assertFalse(result.data)


class TestNavigateToMyBets(unittest.TestCase):

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_my_bets_success(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="My Bets")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[_make_element(text="Le mie scommesse")],
                screen_type="my_bets",
            )),
        ]
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)

        result = navigator.navigate_to_my_bets(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertTrue(result.data.success)
        self.assertEqual(result.data.current_screen_type, "my_bets")
        touch_injector.tap_element.assert_called_once_with(DEVICE, "My Bets")


if __name__ == "__main__":
    unittest.main()
