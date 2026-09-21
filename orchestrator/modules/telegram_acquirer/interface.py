from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result


@dataclass
class AcquiredSignal:
    """Segnale acquisito da un messaggio del canale Telegram via Telethon."""
    raw_text: str                      # testo completo del messaggio, pronto per SignalParser
    ocr_text: str                      # storicamente il testo OCR; con Telethon coincide con raw_text
    emoji_values: dict                 # valori inseriti manualmente per i placeholder (emoji animate)
    source: str                        # "telegram_telethon"
    device_id: str                     # "pc" (Telethon non usa telefoni)
    detected_at: str                   # ISO 8601
    channel_name: Optional[str] = None
    message_id: Optional[str] = None   # ID messaggio Telegram, per deduplicazione


class TelegramAcquirerInterface(ABC):
    """Contratto per l'acquisizione di segnali dal canale Telegram via Telethon."""

    @abstractmethod
    def check_notifications(self, device_id: str = None) -> Result[list[dict]]:
        """Controlla gli ultimi messaggi del canale per nuovi segnali."""

    @abstractmethod
    def capture_signal(self, device_id: str = None, channel_name: str = None) -> Result[AcquiredSignal]:
        """Acquisisce l'ultimo segnale disponibile dal canale."""

    @abstractmethod
    def merge_text_and_values(self, ocr_text: str, emoji_values: dict) -> str:
        """Merge del testo con valori inseriti manualmente per i placeholder non risolti."""
