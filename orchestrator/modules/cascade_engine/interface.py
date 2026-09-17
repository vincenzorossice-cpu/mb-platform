from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from orchestrator.core.result import Result
from orchestrator.modules.multiplier_engine.interface import CascadeCalculation


@dataclass
class CascadeState:
    """Stato persistente di una cascata (dict in-memory al posto di Redis per ora)."""
    cascade_id: str
    operation_id: str
    identity_id: str
    device_id: str
    markets: list[str]              # market id Betfair per evento
    current_event_index: int
    state: str
    back_stake: float
    back_odds: list[float]
    lay_odds: list[float]
    lay_stakes: list[float]
    liabilities: list[float]
    commission_rate: float
    betfair_bet_ids: list[str]
    retry_count: int
    max_retries: int
    next_check_at: str              # ISO datetime
    last_check_at: str              # ISO datetime

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CascadeState":
        return cls(**d)


@dataclass
class CascadeEvent:
    """Vista pubblica di un evento della cascata (contratto Cap 4.6/11.2)."""
    index: int
    betfair_market_id: str
    lay_odds: float
    lay_stake: float
    profit_if_won: float
    profit_if_all_lost: float


class CascadeEngineInterface(ABC):
    """Contratto del motore cascata di copertura Betfair (Cap 4.6)."""

    @abstractmethod
    def start_cascade(self, operation: dict, calculation: CascadeCalculation) -> Result[str]:
        """Avvia la cascata: piazza la prima bancata su Betfair e registra lo stato."""

    @abstractmethod
    def check_and_advance(self, cascade_id: str) -> Result[CascadeState]:
        """Controlla l'esito dell'evento corrente su Betfair e avanza la cascata se risolto."""

    @abstractmethod
    def recover_cascade(self, cascade_id: str) -> Result[CascadeState]:
        """Recovery di una cascata in stato error confrontando con lo stato reale su Betfair."""
