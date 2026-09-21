from orchestrator.modules.telegram_acquirer.acquirer import TelegramAcquirer, _looks_like_signal
from orchestrator.modules.telegram_acquirer.interface import AcquiredSignal


class NotificationMonitor:
    """Monitora le notifiche Telegram su tutti i telefoni.

    Progettata per essere richiamata a intervalli regolari (es. 30 secondi)
    dall'orchestratore: non implementa un loop proprio. Quando rileva un nuovo
    segnale lo acquisisce via TelegramAcquirer.capture_signal."""

    def __init__(self, acquirer: TelegramAcquirer, device_ids: list[str], poll_interval: int = 30):
        self.acquirer = acquirer
        self.device_ids = device_ids
        self.poll_interval = poll_interval
        self._last_notification_text: dict[str, str] = {}

    def check_all_devices(self) -> list[AcquiredSignal]:
        """Controlla tutti i telefoni per nuovi segnali, deduplicando sia per
        device (stesso testo di prima non riacquisito) sia tra device diversi
        (lo stesso segnale arrivato su più telefoni viene acquisito una sola volta)."""
        acquired = []
        seen_this_round = set()

        for device_id in self.device_ids:
            result = self.acquirer.check_notifications(device_id)
            if not result.success:
                continue

            for notification in result.data:
                text = notification.get("text", "")
                if not notification.get("is_signal") or not text:
                    continue
                if not self._is_new_signal(device_id, text):
                    continue
                self._last_notification_text[device_id] = text
                if text in seen_this_round:
                    continue
                seen_this_round.add(text)

                capture_result = self.acquirer.capture_signal(device_id)
                if capture_result.success:
                    acquired.append(capture_result.data)

        return acquired

    def _is_new_signal(self, device_id: str, notification_text: str) -> bool:
        """Verifica se la notifica è nuova (non uguale all'ultima vista su questo device)."""
        return self._last_notification_text.get(device_id) != notification_text

    def _looks_like_signal(self, text: str) -> bool:
        return _looks_like_signal(text)
