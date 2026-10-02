from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from orchestrator.core.result import Result


@dataclass
class BackBetResult:
    confirmed: bool
    bet_id: Optional[str]
    actual_stake: float


@dataclass
class LayBetResult:
    confirmed: bool
    bet_id: Optional[str]
    actual_odds: float
    actual_stake: float


class TouchInjectorInterface(ABC):
    @abstractmethod
    def tap_element(self, device_id: str, hint: str, field: str = "text", verify: bool = True) -> Result[bool]:
        """Tap su un elemento identificato dal testo o resource-id.
        Se verify=True (default), verifica che il tap abbia avuto un effetto reale."""

    @abstractmethod
    def tap_coordinates(self, device_id: str, x: int, y: int) -> Result[bool]:
        """Tap su coordinate fisse. Jitter spaziale ±5px, delay 0.3-1.5s."""

    @abstractmethod
    def input_text(self, device_id: str, text: str, field_hint: Optional[str] = None) -> Result[bool]:
        """Inserisce testo in un campo."""

    @abstractmethod
    def scroll(
        self, device_id: str, direction: str = "down", duration_ms: int = 300, fraction: float = 0.5,
    ) -> Result[bool]:
        """Swipe di scroll (up/down/left/right) centrato sullo schermo."""

    @abstractmethod
    def navigate_to(self, device_id: str, destination: str, book: str) -> Result[bool]:
        """Naviga a una schermata specifica."""

    @abstractmethod
    def place_back_bet(
        self, device_id: str, book: str, stake: float, selections: list[dict],
    ) -> Result[BackBetResult]:
        """Piazza la scommessa back sul book."""

    @abstractmethod
    def place_lay_bet(
        self, device_id: str, market: str, odds: float, stake: float,
    ) -> Result[LayBetResult]:
        """Piazza la bancata su Betfair (web mobile)."""
