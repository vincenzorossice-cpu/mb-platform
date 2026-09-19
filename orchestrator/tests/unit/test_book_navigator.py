import unittest
from unittest.mock import patch, MagicMock
from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import UIElement, ScreenContent
from orchestrator.modules.book_navigator.interface import NavigationResult
from orchestrator.modules.book_navigator.navigator import BookNavigator


DEVICE = "emulator-5554"
BETFAIR_URL = "https://mobile.betfair.com"


def _make_element(text="OK", bounds=None, resource_id=None, class_name=None):
    return UIElement(
        text=text,
        bounds=bounds or [100, 200, 300, 400],
        resource_id=resource_id,
        class_name=class_name,
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
    def test_navigate_to_balance_already_on_balance_page(self, mock_run, mock_sleep):
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
        self.assertEqual(screen_reader.read_screen.call_count, 1)

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_tap_to_balance(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Account")],
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
        touch_injector.tap_element.assert_called_once_with(DEVICE, "Account")

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_3_levels(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Menu")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[_make_element(text="Impostazioni")],
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
        self.assertEqual(screen_reader.read_screen.call_count, 3)
        self.assertEqual(touch_injector.tap_element.call_count, 2)

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_not_found(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(
            elements=[_make_element(text="Menu")],
            screen_type="unknown",
        ))
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertFalse(result.data.success)
        self.assertFalse(result.data.needs_login)
        self.assertEqual(screen_reader.read_screen.call_count, 3)

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
        self.assertEqual(screen_reader.read_screen.call_count, 1)

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_unknown_book(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        navigator = BookNavigator()

        result = navigator.navigate_to_balance(DEVICE, "UNKNOWN")

        self.assertTrue(result.success)
        self.assertEqual(result.data.url, "https://m.unknown.it")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[-1], "https://m.unknown.it")

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_guest_keywords_excluded(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Privacy"), _make_element(text="Account")],
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

        self.assertTrue(result.data.success)
        touch_injector.tap_element.assert_called_once_with(DEVICE, "Account")

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_no_screen_reader(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        navigator = BookNavigator()

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertTrue(result.data.success)
        self.assertIn("non disponibile", result.data.message)

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_auto_login_success(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Accedi")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[_make_element(text="€ 0.68")],
                screen_type="balance_page",
            )),
        ]
        navigator = BookNavigator(screen_reader=screen_reader)
        navigator.auto_login = MagicMock(return_value=Result.ok(NavigationResult(
            success=True, current_screen_type="balance_page",
            url="https://www.betfair.it/exchange/plus/", needs_login=False,
            message="Auto-login riuscito per BETFAIR",
        )))

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertTrue(result.data.success)
        self.assertEqual(result.data.current_screen_type, "balance_page")
        self.assertFalse(result.data.needs_login)
        navigator.auto_login.assert_called_once_with(DEVICE, "BETFAIR")

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    @patch("orchestrator.modules.book_navigator.navigator.subprocess.run")
    def test_navigate_to_balance_auto_login_fails(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        screen_reader = MagicMock()
        screen_reader.read_screen.return_value = Result.ok(_make_screen(
            elements=[_make_element(text="Accedi")],
            screen_type="unknown",
        ))
        navigator = BookNavigator(screen_reader=screen_reader)
        navigator.auto_login = MagicMock(return_value=Result.ok(NavigationResult(
            success=False, current_screen_type="unknown",
            url="https://www.betfair.it/exchange/plus/", needs_login=True,
            message="Credenziali non salvate, login manuale richiesto",
        )))

        result = navigator.navigate_to_balance(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertFalse(result.data.success)
        self.assertTrue(result.data.needs_login)
        self.assertEqual(result.data.message, "Credenziali non salvate, login manuale richiesto")
        navigator.auto_login.assert_called_once_with(DEVICE, "BETFAIR")


class TestAutoLogin(unittest.TestCase):

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    def test_auto_login_success(self, mock_sleep):
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Accedi")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[
                    _make_element(text="user@test.com", class_name="android.widget.EditText"),
                    _make_element(text="pass123", class_name="android.widget.EditText"),
                    _make_element(text="Accedi"),
                ],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[_make_element(text="€ 12.34")],
                screen_type="balance_page",
            )),
        ]
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)

        result = navigator.auto_login(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertTrue(result.data.success)
        self.assertFalse(result.data.needs_login)
        self.assertEqual(result.data.current_screen_type, "balance_page")
        self.assertEqual(touch_injector.tap_element.call_count, 2)
        mock_sleep.assert_any_call(3)
        mock_sleep.assert_any_call(5)

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    def test_auto_login_empty_form(self, mock_sleep):
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Accedi")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[
                    _make_element(text="", class_name="android.widget.EditText"),
                    _make_element(text="", class_name="android.widget.EditText"),
                ],
                screen_type="unknown",
            )),
        ]
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)

        result = navigator.auto_login(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertFalse(result.data.success)
        self.assertTrue(result.data.needs_login)
        self.assertEqual(result.data.message, "Credenziali non salvate, login manuale richiesto")
        self.assertEqual(touch_injector.tap_element.call_count, 1)

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    def test_auto_login_2fa(self, mock_sleep):
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Accedi")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[_make_element(text="Inserisci il codice ricevuto via SMS")],
                screen_type="unknown",
            )),
        ]
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)

        result = navigator.auto_login(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertFalse(result.data.success)
        self.assertTrue(result.data.needs_login)
        self.assertEqual(result.data.message, "2FA richiesto, login manuale richiesto")
        self.assertEqual(touch_injector.tap_element.call_count, 1)

    @patch("orchestrator.modules.book_navigator.navigator.time.sleep")
    def test_auto_login_no_login_button(self, mock_sleep):
        screen_reader = MagicMock()
        screen_reader.read_screen.side_effect = [
            Result.ok(_make_screen(
                elements=[_make_element(text="Accedi")],
                screen_type="unknown",
            )),
            Result.ok(_make_screen(
                elements=[
                    _make_element(text="user@test.com", class_name="android.widget.EditText"),
                    _make_element(text="pass123", class_name="android.widget.EditText"),
                ],
                screen_type="unknown",
            )),
        ]
        touch_injector = MagicMock()
        touch_injector.tap_element.return_value = Result.ok(True)
        navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)

        result = navigator.auto_login(DEVICE, "BETFAIR")

        self.assertTrue(result.success)
        self.assertFalse(result.data.success)
        self.assertTrue(result.data.needs_login)
        self.assertEqual(result.data.message, "Login fallito, login manuale richiesto")
        self.assertEqual(touch_injector.tap_element.call_count, 1)


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
    def test_navigate_to_my_bets_generic(self, mock_run, mock_sleep):
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
