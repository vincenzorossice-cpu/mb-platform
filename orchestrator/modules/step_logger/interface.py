from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result


@dataclass
class StepRecord:
    """Una riga del dataset: un'azione, un momento nel tempo. requested_coords
    e' la coordinata RICHIESTA dal sistema, non quella con jitter di
    TouchInjector (che non la restituisce mai - vedi Fase 0 del piano)."""
    episode_id: str
    step_index: int
    timestamp: str  # ISO8601 UTC
    slot_name: str
    device_id: str
    phase: str  # "bet_setup" | "spin" | "verification" | "hitl"
    action_type: str  # "tap" | "spin" | "wait" | "hitl_prompt"
    element_name: Optional[str]
    strategy_used: Optional[str]
    requested_coords: Optional[tuple]
    balance_before: Optional[str]  # Decimal come stringa, mai float
    balance_after: Optional[str]
    bet_before: Optional[str]
    bet_after: Optional[str]
    win_amount: Optional[str]
    verified: bool
    diff_score: Optional[float]
    outcome: str
    error_code: Optional[str]
    notes: Optional[str]


@dataclass
class EpisodeSummary:
    episode_id: str
    slot_name: str
    device_id: str
    started_at: str
    ended_at: Optional[str]
    total_steps: int
    total_spins: int
    total_played: Optional[str]
    final_outcome: str


class StepLoggerInterface(ABC):
    """Contratto per il logging append-only di ogni azione del sistema."""

    @abstractmethod
    def start_episode(self, slot_name: str, device_id: str) -> Result[str]:
        """Apre un nuovo episodio e ritorna il suo episode_id."""

    @abstractmethod
    def log_step(self, episode_id: str, record: StepRecord) -> Result[bool]:
        """Appende una riga al dataset dell'episodio. Mai riscrive righe precedenti."""

    @abstractmethod
    def end_episode(self, episode_id: str, summary: EpisodeSummary) -> Result[bool]:
        """Chiude l'episodio e scrive il riepilogo."""
