from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import BalanceReading


@dataclass
class Account:
    """Identità + bookmaker da riconciliare."""
    account_id: str
    identity_id: str
    book: str
    is_betfair: bool


@dataclass
class Rectification:
    """Rettifica da registrare sull'app gestionale."""
    amount: float
    column: str       # "BOOK" | "BANCO"
    reason: str
    bonus_note: Optional[str] = None


@dataclass
class ReconciliationResult:
    """Output della riconciliazione di un singolo account."""
    adjusted_balance: float
    expected_balance: float
    difference: float
    column: str                            # "BOOK" | "BANCO"
    bonus_note: Optional[str]
    action: str                            # "no_adjustment" | "adjust" | "hitl_required"
    rectification: Optional[Rectification]
    rules_applied: list[str]


@dataclass
class UnregisteredOperationAlert:
    """Bancata Betfair aperta senza operazione corrispondente su Netcap."""
    collaboratore_id: str
    betfair_open_bet: dict
    detected_at: str
    status: str = "pending"     # "pending" | "completed" | "ignored"
    bookmaker_id: Optional[int] = None
    back_stake: Optional[float] = None
    back_odds: Optional[float] = None
    tipologia: str = "Ricorrenti"


class RulesEngineInterface(ABC):
    """Contratto per il motore regole di riconciliazione P6."""

    @abstractmethod
    def reconcile(
        self,
        balance_reading: BalanceReading,
        expected_balance: float,
        account: Account,
        impegnato_netcap: float = 0.0,
    ) -> Result[ReconciliationResult]:
        """Applica le regole di riconciliazione P6."""
