from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result


@dataclass
class AcquiredSignal:
    """Segnale acquisito da uno screenshot di una notifica/chat Telegram."""
    raw_text: str                      # testo pronto per SignalParser (eventuali marker non risolti restano)
    screenshot_path: str
    ocr_text: str                      # testo OCR grezzo, prima di un eventuale merge con valori manuali
    emoji_values: dict                 # valori inseriti manualmente per i marker che l'OCR non ha risolto
    confidence: float                  # confidence media OCR
    source: str                        # "telegram_notification"
    device_id: str
    detected_at: str                   # ISO 8601
    channel_name: Optional[str] = None


class TelegramAcquirerInterface(ABC):
    """Contratto per l'acquisizione di segnali dalle notifiche Telegram via ADB."""

    @abstractmethod
    def check_notifications(self, device_id: str) -> Result[list[dict]]:
        """Controlla le notifiche Telegram attive per nuovi segnali."""

    @abstractmethod
    def capture_signal(self, device_id: str, channel_name: str = None) -> Result[AcquiredSignal]:
        """Acquisisce un segnale: apri Telegram -> screenshot -> OCR."""

    @abstractmethod
    def merge_text_and_values(self, ocr_text: str, emoji_values: dict) -> str:
        """Merge del testo OCR con valori inseriti manualmente per i marker non risolti."""
