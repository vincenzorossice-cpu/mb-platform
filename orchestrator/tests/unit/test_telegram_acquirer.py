from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.modules.telegram_acquirer.acquirer import TelegramAcquirer, _looks_like_signal
from orchestrator.modules.telegram_acquirer.notification_monitor import NotificationMonitor


def make_mock_message(id, text, date=None):
    msg = MagicMock()
    msg.id = id
    msg.text = text
    msg.date = date or datetime(2026, 9, 21, 10, 0, 0)
    return msg


MOCK_SIGNAL_MESSAGES = [
    make_mock_message(100, "📚 SISAL\n⏳RICORRENTE SLOT\n✅Gioca 5€ su slot\n⏰Scadenza: 22 Settembre 23:59"),
    make_mock_message(99, "📚 BETSSON\n💎RIMBORSO 50% FUN BONUS SLOT\n✅Consigliato\n⏰Scadenza: 23 Settembre"),
    make_mock_message(98, "Messaggio senza segnale"),
    make_mock_message(97, None),  # messaggio media
]


@pytest.fixture
def acquirer():
    return TelegramAcquirer()


def _patch_client(acquirer, messages):
    """Patcha _get_client() dell'acquirer per restituire un client fake il cui
    iter_messages ritorna la lista di messaggi data."""
    mock_client = MagicMock()
    mock_client.iter_messages.return_value = list(messages)
    acquirer._client = mock_client
    return mock_client


class TestCheckNotifications:
    def test_check_notifications_finds_signal(self, acquirer):
        _patch_client(acquirer, MOCK_SIGNAL_MESSAGES)

        result = acquirer.check_notifications()

        assert result.success
        assert len(result.data) == 2
        assert all(n["is_signal"] for n in result.data)
        assert "SISAL" in result.data[0]["text"]

    def test_check_notifications_no_signal(self, acquirer):
        _patch_client(acquirer, [make_mock_message(1, "Ciao"), make_mock_message(2, "Nessun segnale qui")])

        result = acquirer.check_notifications()

        assert result.success
        assert result.data == []

    def test_check_notifications_skips_non_text(self, acquirer):
        _patch_client(acquirer, [make_mock_message(1, None)])

        result = acquirer.check_notifications()

        assert result.success
        assert result.data == []


class TestCaptureSignal:
    def test_capture_signal_success(self, acquirer):
        _patch_client(acquirer, MOCK_SIGNAL_MESSAGES)

        result = acquirer.capture_signal()

        assert result.success
        signal = result.data
        assert signal.raw_text.startswith("📚 SISAL")
        assert signal.ocr_text == signal.raw_text
        assert signal.emoji_values == {}
        assert signal.source == "telegram_telethon"
        assert signal.device_id == "pc"
        assert signal.channel_name == "MB Evo Pro"

    def test_capture_signal_no_signal(self, acquirer):
        _patch_client(acquirer, [make_mock_message(i, "Nessun segnale") for i in range(50)])

        result = acquirer.capture_signal()

        assert not result.success
        assert result.error_code == "NO_SIGNAL_FOUND"

    def test_capture_signal_has_message_id(self, acquirer):
        _patch_client(acquirer, MOCK_SIGNAL_MESSAGES)

        result = acquirer.capture_signal()

        assert result.success
        assert result.data.message_id == "100"


class TestLooksLikeSignal:
    def test_looks_like_signal(self):
        assert _looks_like_signal("📚 STANLEYBET") is True
        assert _looks_like_signal("Ciao") is False
        assert _looks_like_signal("") is False
        assert _looks_like_signal(None) is False


class TestMergeTextAndValues:
    def test_merge_text_and_values(self, acquirer):
        merged = acquirer.merge_text_and_values("ricarica 🔡🔡€", {"importo_ricarica": 100})
        assert merged == "ricarica 100€"

    def test_merge_text_and_values_unknown(self, acquirer):
        merged = acquirer.merge_text_and_values("ricarica 🔡🔡€", {"importo_ricarica": None})
        assert merged == "ricarica 🔡🔡€"

    def test_merge_multiple_values(self, acquirer):
        text = "ricarica 🔡🔡€, bonus 🔡🔡€, PROFITTO MEDIO:🔡🔡€"
        merged = acquirer.merge_text_and_values(text, {
            "importo_ricarica": 100,
            "importo_bonus": 25,
            "profitto_atteso": 12.5,
        })
        assert merged == "ricarica 100€, bonus 25€, PROFITTO MEDIO: 12,5€"


class TestNotificationMonitor:
    def test_notification_monitor_new_signals(self):
        mock_acquirer = MagicMock()
        mock_acquirer.channel_id = -1003833700547
        mock_client = MagicMock()
        mock_client.iter_messages.return_value = MOCK_SIGNAL_MESSAGES[:3]
        mock_acquirer._get_client.return_value = mock_client

        monitor = NotificationMonitor(acquirer=mock_acquirer)
        signals = monitor.check_new_signals()

        assert len(signals) == 2
        assert all(s.message_id in ("100", "99") for s in signals)

    def test_notification_monitor_dedup(self):
        mock_acquirer = MagicMock()
        mock_acquirer.channel_id = -1003833700547
        mock_client = MagicMock()
        mock_client.iter_messages.return_value = MOCK_SIGNAL_MESSAGES[:2]
        mock_acquirer._get_client.return_value = mock_client

        monitor = NotificationMonitor(acquirer=mock_acquirer)
        first_pass = monitor.check_new_signals()
        assert len(first_pass) == 2

        second_pass = monitor.check_new_signals()
        assert second_pass == []
