from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from orchestrator.core.result import Result


@dataclass
class NavigationStep:
    step: int
    action: str
    target: Optional[str]
    confidence: float


class ScreenNavigatorInterface(ABC):
    @abstractmethod
    def navigate(
        self, device_id: str, goal: str, book: str = "", demo_mode: bool = False, vision_mode: bool = False,
    ) -> Result[bool]:
        """Loop read -> decide (Decision Engine) -> act, fino a 'done' o max_steps.
        Se demo_mode=True, cerca 'Prova'/'For fun' invece di 'Gioca' con soldi veri.
        Se vision_mode=True il Decision Engine VEDE lo screenshot (se il motore lo supporta)."""
