from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.telegram_acquirer.acquirer import TelegramAcquirer, _looks_like_signal
from orchestrator.modules.telegram_acquirer.notification_monitor import NotificationMonitor

MOCK_NOTIFICATION_OUTPUT = """
NotificationRecord{0x123: u0d123 com.telegram.messenger}
  android.app.Notification@456
  extras: Bundle[{android.title=Canale Segnali, android.text=📚 STANLEYBET 💎RICORRENTE SPORT}]
"""

MOCK_NO_TELEGRAM_OUTPUT = """
NotificationRecord{0x999: u0d999 com.whatsapp}
  extras: Bundle[{android.title=Chat, android.text=Ciao come stai}]
"""


def _fake_ocr_engine(lines: list[tuple[str, float]]):
    engine = MagicMock()
    engine.ocr.return_value = [
        [[[0, i * 10], [10, i * 10], [10, i * 10 + 10], [0, i * 10 + 10]], (text, conf)]
        for i, (text, conf) in enumerate(lines)
    ]
    return engine


@pytest.fixture
def screen_reader():
    return MagicMock()


@pytest.fixture
def touch_injector():
    injector = MagicMock()
    injector.tap_element.return_value = Result.ok(True)
    return injector


@pytest.fixture
def acquirer(screen_reader, touch_injector):
    return TelegramAcquirer(screen_reader=screen_reader, touch_injector=touch_injector)


class TestCheckNotifications:
    @patch("orchestrator.modules.telegram_acquirer.acquirer.subprocess.run")
    def test_check_notifications_finds_telegram(self, mock_run, acquirer):
        mock_run.return_value = MagicMock(returncode=0, stdout=MOCK_NOTIFICATION_OUTPUT, stderr="")

        result = acquirer.check_notifications("device_001")

        assert result.success
        assert len(result.data) == 1
        assert result.data[0]["title"] == "Canale Segnali"
        assert "STANLEYBET" in result.data[0]["text"]
        assert result.data[0]["is_signal"] is True

    @patch("orchestrator.modules.telegram_acquirer.acquirer.subprocess.run")
    def test_check_notifications_no_telegram(self, mock_run, acquirer):
        mock_run.return_value = MagicMock(returncode=0, stdout=MOCK_NO_TELEGRAM_OUTPUT, stderr="")

        result = acquirer.check_notifications("device_001")

        assert result.success
        assert result.data == []


class TestLooksLikeSignal:
    def test_looks_like_signal(self):
        assert _looks_like_signal("📚 STANLEYBET 💎RICORRENTE") is True
        assert _looks_like_signal("Ciao come stai") is False
        assert _looks_like_signal("") is False


class TestCaptureSignal:
    @patch("orchestrator.modules.telegram_acquirer.acquirer.time.sleep")
    @patch("orchestrator.modules.telegram_acquirer.acquirer.os.path.exists", return_value=True)
    @patch("orchestrator.modules.telegram_acquirer.acquirer.subprocess.run")
    def test_capture_signal_success(self, mock_run, mock_exists, mock_sleep, acquirer):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        acquirer._ocr_engine = _fake_ocr_engine([
            ("STANLEYBET", 0.95),
            ("ricarica 100€", 0.9),
        ])

        result = acquirer.capture_signal("device_001")

        assert result.success
        signal = result.data
        assert "STANLEYBET" in signal.raw_text
        assert "100€" in signal.raw_text
        assert signal.emoji_values == {}
        assert signal.device_id == "device_001"
        assert signal.source == "telegram_notification"
        assert 0.9 <= signal.confidence <= 0.95

    @patch("orchestrator.modules.telegram_acquirer.acquirer.time.sleep")
    @patch("orchestrator.modules.telegram_acquirer.acquirer.os.path.exists", return_value=True)
    @patch("orchestrator.modules.telegram_acquirer.acquirer.subprocess.run")
    def test_capture_signal_with_channel_navigation(self, mock_run, mock_exists, mock_sleep, acquirer, touch_injector):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        acquirer._ocr_engine = _fake_ocr_engine([("segnale", 0.9)])

        result = acquirer.capture_signal("device_001", channel_name="Canale Segnali")

        assert result.success
        touch_injector.tap_element.assert_called_once_with("device_001", "Canale Segnali")
        assert result.data.channel_name == "Canale Segnali"


class TestMergeTextAndValues:
    def test_merge_text_and_values(self, acquirer):
        merged = acquirer.merge_text_and_values("PROFITTO MEDIO: 🔡💶", {"profitto_atteso": 12.5})
        assert merged == "PROFITTO MEDIO: 12.5€"

    def test_merge_text_and_values_unknown(self, acquirer):
        merged = acquirer.merge_text_and_values("ricarica 🔡🔡€", {"importo_ricarica": None})
        assert merged == "ricarica 🔡🔡€"

    def test_merge_multiple_values(self, acquirer):
        text = "ricarica 🔡🔡€, riceverai 😳😃€ di bonus, PROFITTO MEDIO: 🔡💶"
        merged = acquirer.merge_text_and_values(text, {
            "importo_ricarica": 100,
            "importo_bonus": 25,
            "profitto_atteso": 12.5,
        })
        assert merged == "ricarica 100€, riceverai 25€ di bonus, PROFITTO MEDIO: 12.5€"


class TestOpenTelegram:
    @patch("orchestrator.modules.telegram_acquirer.acquirer.subprocess.run")
    def test_open_telegram(self, mock_run, acquirer):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = acquirer._open_telegram("device_001")

        assert result.success
        args = mock_run.call_args[0][0]
        assert args[:3] == ["adb", "-s", "device_001"]
        assert "am" in args and "start" in args
        assert "com.telegram.messenger/.TelegramLaunchActivity" in args


class TestScreenshot:
    @patch("orchestrator.modules.telegram_acquirer.acquirer.os.path.exists", return_value=True)
    @patch("orchestrator.modules.telegram_acquirer.acquirer.subprocess.run")
    def test_screenshot_taken(self, mock_run, mock_exists, acquirer):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = acquirer._take_screenshot("device_001")

        assert result.success
        assert mock_run.call_count == 2
        screencap_args = mock_run.call_args_list[0][0][0]
        pull_args = mock_run.call_args_list[1][0][0]
        assert "screencap" in screencap_args
        assert "pull" in pull_args
        assert result.data.endswith(".png")


class TestNotificationMonitor:
    def test_is_new_signal(self):
        monitor = NotificationMonitor(acquirer=MagicMock(), device_ids=["d1"])
        assert monitor._is_new_signal("d1", "📚 SEGNALE") is True
        monitor._last_notification_text["d1"] = "📚 SEGNALE"
        assert monitor._is_new_signal("d1", "📚 SEGNALE") is False

    def test_notification_monitor_check_all_dedup(self):
        mock_acquirer = MagicMock()
        same_text = "📚 STANLEYBET 💎RICORRENTE"
        mock_acquirer.check_notifications.side_effect = lambda device_id: Result.ok([
            {"title": "Canale Segnali", "text": same_text, "is_signal": True},
        ])
        mock_acquirer.capture_signal.return_value = Result.ok(MagicMock())

        monitor = NotificationMonitor(acquirer=mock_acquirer, device_ids=["d1", "d2", "d3"])
        acquired = monitor.check_all_devices()

        assert len(acquired) == 1
        mock_acquirer.capture_signal.assert_called_once()

    def test_notification_monitor_skips_non_signal(self):
        mock_acquirer = MagicMock()
        mock_acquirer.check_notifications.return_value = Result.ok([
            {"title": "Chat", "text": "Ciao come stai", "is_signal": False},
        ])

        monitor = NotificationMonitor(acquirer=mock_acquirer, device_ids=["d1"])
        acquired = monitor.check_all_devices()

        assert acquired == []
        mock_acquirer.capture_signal.assert_not_called()
