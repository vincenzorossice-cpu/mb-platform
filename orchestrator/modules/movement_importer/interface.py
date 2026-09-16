from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.app_api_client.interface import WriteResult


@dataclass
class Movement:
    """Movimento da importare (Hype/PayPal/GoCardless) verso Netcap."""
    tipo: str  # "deposito" | "prelievo"
    importo: float
    conto_id: Optional[int]
    bookmaker_id: Optional[int]
    bookmaker_nome: Optional[str]
    data: str
    collaboratore_id: str
    descrizione: str
    fonte: str  # "hype_email" | "paypal_ocr" | "hype_gocardless"
    confidence: float


@dataclass
class ConsentStatus:
    """Stato del consenso GoCardless per un collaboratore."""
    collaboratore_id: str
    is_valid: bool
    expires_at: Optional[str]
    days_remaining: Optional[int]


class MovementImporterInterface(ABC):
    """Contratto per l'importazione di movimenti da fonti esterne verso Netcap."""

    @abstractmethod
    def import_hype(self, collaboratore_id: str, date_from: str = None, date_to: str = None) -> Result[list[Movement]]:
        """Importa movimenti Hype via email parsing."""

    @abstractmethod
    def import_paypal(self, device_id: str, collaboratore_id: str, date_from: str = None) -> Result[list[Movement]]:
        """Importa movimenti PayPal via ScreenReader/OCR sull'app PayPal."""

    @abstractmethod
    def check_consent_expiry(self, collaboratore_id: str) -> Result[ConsentStatus]:
        """Verifica se il consenso PSD2 per Hype è valido."""

    @abstractmethod
    def write_movements_to_netcap(self, movements: list[Movement], session_token: str = None) -> Result[list[WriteResult]]:
        """Scrive i movimenti su Netcap via POST /movimentazioni, con deduplicazione."""

    @abstractmethod
    def save_pending_movements(
        self, movements: list[Movement], collaboratore_id: str, report_dir: str = "orchestrator/tests/validation/reports"
    ) -> Result[str]:
        """Salva movimenti pending in un file JSON per la HITL Dashboard."""
