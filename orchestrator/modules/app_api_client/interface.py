from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.rules_engine.interface import Rectification


@dataclass
class ExpectedBalance:
    """Saldo atteso per un singolo collaboratore × bookmaker."""
    collaboratore_id: str
    bookmaker_id: int
    bookmaker_nome: str
    saldo_atteso: float
    importo_impegnato: float
    is_exchange: bool


@dataclass
class WalletBalance:
    """Saldi Hype/PayPal di un collaboratore."""
    collaboratore_id: str
    saldo_hype: float
    saldo_paypal: float


@dataclass
class BookStatus:
    """Stato di un bookmaker per un collaboratore."""
    collaboratore_id: str
    bookmaker_id: int
    stato: str
    limitazioni: Optional[str]


@dataclass
class MovementRecord:
    """Singolo movimento (deposito/prelievo) da Netcap."""
    id: int
    tipo: str
    importo: float
    bookmaker_id: Optional[int]
    bookmaker_nome: Optional[str]
    collaboratore_id: str
    data: str


@dataclass
class WriteResult:
    """Esito di una scrittura su Netcap."""
    success: bool
    netcap_id: Optional[int]
    message: Optional[str]


class AppApiClientInterface(ABC):
    """Contratto per comunicazione con l'API Netcap."""

    @abstractmethod
    def read_expected_balance(self, collaboratore_id: str) -> Result[list[ExpectedBalance]]:
        """Legge i saldi attesi da Netcap per un collaboratore."""

    @abstractmethod
    def read_wallet_balance(self, collaboratore_id: str) -> Result[WalletBalance]:
        """Legge i saldi Hype/PayPal da Netcap."""

    @abstractmethod
    def read_book_status(self, collaboratore_id: str) -> Result[list[BookStatus]]:
        """Legge lo stato dei book per un collaboratore."""

    @abstractmethod
    def read_movements(self, collaboratore_id: str, date_from: str = None) -> Result[list[MovementRecord]]:
        """Legge i movimenti registrati su Netcap per un collaboratore."""

    @abstractmethod
    def write_rettifica(self, rectification: Rectification, collaboratore_id: str, bookmaker_id: int) -> Result[WriteResult]:
        """Scrive una rettifica su Netcap via POST /operazioni."""

    @abstractmethod
    def write_operazione(self, operazione: dict) -> Result[WriteResult]:
        """Scrive una nuova operazione generica su Netcap via POST /operazioni."""
