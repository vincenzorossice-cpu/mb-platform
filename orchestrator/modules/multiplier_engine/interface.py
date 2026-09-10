from abc import ABC, abstractmethod
from orchestrator.core.result import Result
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CascadeEventCalc:
    index: int
    lay_stake: float
    liability: float
    lay_odds: float
    profit_if_won: float
    profit_if_all_lost: float


@dataclass
class CascadeCalculation:
    events: list[CascadeEventCalc]
    profit_per_scenario: dict
    total_liability: float
    is_valid: bool
    warnings: list[str] = field(default_factory=list)
    back_stake: float = 0.0
    back_odds: list[float] = field(default_factory=list)
    lay_odds: list[float] = field(default_factory=list)
    commission: float = 0.045


@dataclass
class SingleCalculation:
    lay_stake: float
    liability: float
    profit_if_won: float
    profit_if_lost: float
    is_valid: bool
    warnings: list[str] = field(default_factory=list)


class MultiplierEngineInterface(ABC):
    @abstractmethod
    def calculate_cascade(self, back_stake: float, back_odds: list[float], lay_odds: list[float], commission: float = 0.045, strategy: str = "equal_profit") -> Result[CascadeCalculation]:
        pass

    @abstractmethod
    def calculate_single(self, back_stake: float, back_odds: float, lay_odds: float, commission: float = 0.045) -> Result[SingleCalculation]:
        pass

    @abstractmethod
    def recalculate_from_index(self, back_stake: float, back_odds: list[float], lay_odds: list[float], commission: float, from_index: int) -> Result[CascadeCalculation]:
        pass
