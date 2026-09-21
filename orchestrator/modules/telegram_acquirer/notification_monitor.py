from datetime import datetime, timezone

from orchestrator.modules.telegram_acquirer.acquirer import TelegramAcquirer, _looks_like_signal
from orchestrator.modules.telegram_acquirer.interface import AcquiredSignal


class NotificationMonitor:
    """Monitora il canale Telegram "MB Evo Pro" per nuovi segnali usando Telethon.

    Progettata per essere richiamata a intervalli regolari (es. 30 secondi)
    dall'orchestratore: non implementa un loop proprio. Deduplica confrontando
    l'id dell'ultimo messaggio processato."""

    def __init__(self, acquirer: TelegramAcquirer, poll_interval: int = 30):
        self.acquirer = acquirer
        self.poll_interval = poll_interval
        self._last_message_id: int = 0

    def check_new_signals(self) -> list[AcquiredSignal]:
        """Controlla il canale per nuovi segnali dall'ultimo check.

        Legge i messaggi più recenti e si ferma appena trova un id già
        processato (i messaggi arrivano in ordine decrescente)."""
        client = self.acquirer._get_client()

        new_signals = []
        highest_seen = self._last_message_id
        for msg in client.iter_messages(self.acquirer.channel_id, limit=20):
            if msg.id <= self._last_message_id:
                break

            highest_seen = max(highest_seen, msg.id)

            if msg.text and self._looks_like_signal(msg.text):
                new_signals.append(AcquiredSignal(
                    raw_text=msg.text,
                    ocr_text=msg.text,
                    emoji_values={},
                    source="telegram_telethon",
                    device_id="pc",
                    detected_at=datetime.now(timezone.utc).isoformat(),
                    channel_name="MB Evo Pro",
                    message_id=str(msg.id),
                ))

        self._last_message_id = highest_seen
        return new_signals

    def _looks_like_signal(self, text: str) -> bool:
        return _looks_like_signal(text)
