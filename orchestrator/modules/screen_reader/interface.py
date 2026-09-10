from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from orchestrator.core.result import Result


@dataclass
class UIElement:
    text: str
    bounds: list[int]
    resource_id: Optional[str] = None
    class_name: Optional[str] = None
    confidence: float = 1.0


@dataclass
class ScreenContent:
    elements: list[UIElement]
    screen_type: str
    confidence: float
    method: str
    screenshot_path: Optional[str] = None


@dataclass
class BalanceReading:
    raw_balance: float
    open_bets: float
    real_bonus: float
    wagering_bonus: Optional[float]
    read_method: str
    confidence: float


@dataclass
class BetStatus:
    status: str
    lay_odds: Optional[float]
    confidence: float


@dataclass
class MovementReading:
    amount: float
    description: str
    date: str
    direction: str
    confidence: float


class ScreenReaderInterface(ABC):
    @abstractmethod
    def read_screen(self, device_id: str, screen_type: Optional[str] = None) -> Result[ScreenContent]:
        pass

    @abstractmethod
    def find_element(self, device_id: str, hint: str, field: str = "text") -> Result[UIElement]:
        pass

    @abstractmethod
    def read_balance(self, device_id: str, book: str) -> Result[BalanceReading]:
        pass

    @abstractmethod
    def read_bet_status(self, device_id: str, betfair: bool = False) -> Result[BetStatus]:
        pass

    @abstractmethod
    def read_movements(self, device_id: str, app: str) -> Result[list[MovementReading]]:
        pass
