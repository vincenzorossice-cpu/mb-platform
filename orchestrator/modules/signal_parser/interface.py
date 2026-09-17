from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from orchestrator.core.result import Result


@dataclass
class Signal:
    """Segnale di matched betting parsato e strutturato."""
    telegram_message_id: str
    book: str
    category: str                    # "sport" | "casino"
    bonus_type: str                  # "ricorrente" | "benvenuto" | "esclusivo" | "rimborso_slot"
    difficulty: str                  # "facile" | "medio" | "avanzato"
    strategy: dict
    bonus: dict
    deadline: str                    # ISO 8601
    priority: str                    # "alta" | "media" | "bassa"
    expected_profit: str             # "alto" | "medio" | "basso" o importo
    tc_verified: bool = False
    tc_discrepancies: list[str] = field(default_factory=list)
    status: str = "pending"          # "pending" | "tc_verified" | "tc_discrepancy"


class SignalParserInterface(ABC):
    """Contratto per il parsing dei segnali Telegram P5 Sport."""

    @abstractmethod
    def parse(self, raw_text: str, telegram_message_id: str) -> Result[Signal]:
        """Parsea un segnale Telegram grezzo in un oggetto Signal strutturato."""

    @abstractmethod
    def parse_and_verify(self, raw_text: str, telegram_message_id: str) -> Result[Signal]:
        """Parse + T&C verification via RAGSystem."""
