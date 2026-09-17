from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from orchestrator.core.result import Result


@dataclass
class CorrelationCheck:
    """Esito della verifica di rischio correlation su un batch di operazioni."""
    risk_level: str                  # "low" | "medium" | "high"
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False            # True se risk_level="high" -> operazioni bloccate


class OpSecInterface(ABC):
    """Contratto per la prevenzione della correlation detection multi-account."""

    @abstractmethod
    def schedule_operation(self, identity_id: str, operation_type: str,
                            book: str, urgency: str = "normal") -> Result[str]:
        """Calcola quando un'operazione può essere eseguita, applicando temporal diversity.
        Ritorna una stringa ISO datetime."""

    @abstractmethod
    def check_correlation_risk(self, batch: list[dict]) -> Result[CorrelationCheck]:
        """Verifica se un batch di operazioni ha rischio di correlation."""
