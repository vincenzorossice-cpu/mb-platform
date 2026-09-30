from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.profile import ProviderProfile


@dataclass
class LocateCandidate:
    x: int
    y: int
    confidence: float
    strategy_name: str
    verified: bool = False  # pre-impostato True solo da HITLStrategy


@dataclass
class LocateContext:
    device_id: str
    slot_name: str
    screenshot_path: str
    screen_width: int
    screen_height: int
    raw_texts: list = field(default_factory=list)
    text_boxes: list = field(default_factory=list)
    slot_ocr_state: dict = field(default_factory=dict)
    profile: Optional[ProviderProfile] = None
    # Punti (x, y) gia' tappati per questo elemento con un effetto sbagliato
    # (es. hanno aperto un'altra schermata): riempito dal locator.
    rejected_positions: list = field(default_factory=list)
    # Fascia (x1, y1, x2, y2) dove si leggono saldo e puntata, appresa dallo schermo:
    # permette letture OCR rapide quando servono solo quei due importi.
    amounts_band: Optional[tuple] = None


class LocatorStrategy(ABC):
    """Contratto comune per ogni anello della catena di ElementLocator."""
    name: str = "base"

    @abstractmethod
    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        """Prova a localizzare un elemento sullo schermo corrente."""
