from unittest.mock import MagicMock, patch

from orchestrator.modules.telegram_notifier.notifier import TelegramNotifier


class TestSendNotification:
    def test_telegram_notifier_send(self):
        notifier = TelegramNotifier(bot_token="tok123", chat_id="chat456")
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None

        with patch("orchestrator.modules.telegram_notifier.notifier.requests.post", return_value=mock_response) as mock_post:
            result = notifier.send_notification("Ciao Raffaele")

        assert result.success
        assert result.data is True
        mock_post.assert_called_once()
        _args, kwargs = mock_post.call_args
        assert kwargs["json"]["chat_id"] == "chat456"
        assert kwargs["json"]["text"] == "Ciao Raffaele"
        assert kwargs["json"]["parse_mode"] == "HTML"

    def test_not_configured_returns_ok_false(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        notifier = TelegramNotifier()

        with patch("orchestrator.modules.telegram_notifier.notifier.requests.post") as mock_post:
            result = notifier.send_notification("Ciao")

        assert result.success
        assert result.data is False
        mock_post.assert_not_called()

    def test_network_error_returns_fail(self):
        import requests

        notifier = TelegramNotifier(bot_token="tok123", chat_id="chat456")
        with patch(
            "orchestrator.modules.telegram_notifier.notifier.requests.post",
            side_effect=requests.RequestException("boom"),
        ):
            result = notifier.send_notification("Ciao")

        assert not result.success
        assert result.error_code == "TELEGRAM_SEND_ERROR"


class TestNotifySportFlow:
    def test_notify_sport_flow_format(self):
        notifier = TelegramNotifier(bot_token="tok123", chat_id="chat456")

        with patch.object(notifier, "send_notification", return_value=None) as mock_send:
            notifier.notify_sport_flow(
                book="STANLEYBET",
                bonus_type="Ricorrente Sport",
                va="+12.50€",
                identities_ready=13,
                deadline="23:59",
                dashboard_url="http://localhost:8000/confirm_sport_flow/sport_tm1_1",
            )

        mock_send.assert_called_once()
        message = mock_send.call_args[0][0]
        assert "STANLEYBET" in message
        assert "Ricorrente Sport" in message
        assert "+12.50€" in message
        assert "13" in message
        assert "23:59" in message
        assert "http://localhost:8000/confirm_sport_flow/sport_tm1_1" in message


class TestNotifyUnregisteredOp:
    def test_notify_unregistered_op_format(self):
        notifier = TelegramNotifier(bot_token="tok123", chat_id="chat456")

        with patch.object(notifier, "send_notification", return_value=None) as mock_send:
            notifier.notify_unregistered_op(
                collaboratore_id="ID-08",
                event="Gil Vicente - Maritimo",
                liability=1154.15,
                dashboard_url="http://localhost:8000/some/path",
            )

        message = mock_send.call_args[0][0]
        assert "ID-08" in message
        assert "Betfair" in message
        assert "Gil Vicente - Maritimo" in message
        assert "1154.15" in message


class TestNotifyRettifica:
    def test_notify_rettifica_format(self):
        notifier = TelegramNotifier(bot_token="tok123", chat_id="chat456")

        with patch.object(notifier, "send_notification", return_value=None) as mock_send:
            notifier.notify_rettifica(
                collaboratore_id="01",
                book="Sisal",
                difference=600.0,
                dashboard_url="http://localhost:8000/some/path",
            )

        message = mock_send.call_args[0][0]
        assert "01" in message
        assert "Sisal" in message
        assert "600.00" in message
