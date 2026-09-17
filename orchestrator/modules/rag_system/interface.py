from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.signal_parser.interface import Signal


@dataclass
class RAGResponse:
    """Esito di una query semantica sulla knowledge base."""
    recommendation: str
    confidence: float
    sources: list[str]
    action: str                    # "auto_execute" | "hitl_confirm" | "no_match"


@dataclass
class TCVerification:
    """Esito della verifica termini & condizioni di un segnale contro la KB."""
    verified: bool
    discrepancies: list[str]
    matched_docs: list[str]


class RAGSystemInterface(ABC):
    """Contratto per il sistema RAG (Cap 4.10) — query semantica KB + verifica T&C."""

    @abstractmethod
    def query(self, question: str, context: dict,
              process: Optional[int] = None,
              book: Optional[str] = None) -> Result[RAGResponse]:
        """Interroga la KB e restituisce una raccomandazione con confidence e sorgenti."""

    @abstractmethod
    def verify_tc(self, signal: Signal) -> Result[TCVerification]:
        """Verifica i parametri del segnale contro i T&C indicizzati per il book."""

    @abstractmethod
    def index_document(self, file_path: str) -> Result[bool]:
        """Indicizza (o rimuove se deprecated) un documento markdown nella KB."""
