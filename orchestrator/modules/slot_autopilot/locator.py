import logging

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import save_autopilot_element
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_autopilot.strategies.memory_strategy import _LEGACY_MEMORY_FIELDS
from orchestrator.tests.validation.test_slot_autonomous import _persist_slot_memory

logger = logging.getLogger(__name__)

# Fonti gia' curate/verificate a monte: un candidato da qui non viene mai
# scartato dal controllo canvas, esattamente come nel vecchio script un
# comando dell'operatore o una coordinata memorizzata restavano sempre fidati.
_TRUSTED_STRATEGIES = {"profile", "memory", "hitl"}


def _within_canvas(x: int, y: int, canvas_bbox: tuple) -> bool:
    x1, y1, x2, y2 = canvas_bbox
    return x1 <= x <= x2 and y1 <= y <= y2


class ElementLocator:
    """Prova le strategie in un ordine fisso; la prima che ha successo vince.
    Localizzare e avere-effetto sono passi distinti per design: questo
    componente risponde solo a "dov'e'", non "ha funzionato" (quello e'
    ActionVerifier)."""

    _STRATEGY_ORDER = ("profile", "memory", "ocr_anchor", "spatial_heuristic", "template", "gpt", "hitl")

    def __init__(self, strategies: dict, max_consecutive_failures_per_strategy: int = 3):
        self.strategies = strategies
        self.max_consecutive_failures_per_strategy = max_consecutive_failures_per_strategy
        self._failure_counts = {}  # (strategy_name, element_name) -> int consecutivi

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        collected = []
        for strategy_name in self._STRATEGY_ORDER:
            strategy = self.strategies.get(strategy_name)
            if strategy is None:
                continue
            key = (strategy_name, element_name)
            if self._failure_counts.get(key, 0) >= self.max_consecutive_failures_per_strategy:
                continue

            result = strategy.locate(element_name, context)
            if result.success:
                candidate = result.data
                if (
                    strategy_name not in _TRUSTED_STRATEGIES
                    and context.profile is not None
                    and context.profile.canvas_bbox is not None
                    and not _within_canvas(candidate.x, candidate.y, context.profile.canvas_bbox)
                ):
                    logger.warning(
                        "'%s' via '%s' a (%d,%d) e' fuori dal canvas %s: scartato, non tappato.",
                        element_name, strategy_name, candidate.x, candidate.y, context.profile.canvas_bbox,
                    )
                    collected.append(f"{strategy_name}:TARGET_OUTSIDE_CANVAS")
                    self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
                    continue

                self._failure_counts[key] = 0
                logger.info(
                    "'%s' localizzato via '%s' a (%d,%d)", element_name, strategy_name, candidate.x, candidate.y,
                )
                return result

            collected.append(f"{strategy_name}:{result.error_code}")
            self._failure_counts[key] = self._failure_counts.get(key, 0) + 1

        logger.warning("Impossibile localizzare '%s' con nessuna strategia: %s", element_name, collected)
        return Result.fail(
            f"Could not locate '{element_name}' with any strategy: {collected}",
            "ELEMENT_NOT_LOCATABLE",
        )

    def record_failure(self, strategy_name: str, element_name: str) -> None:
        key = (strategy_name, element_name)
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1

    def reset_failures(self, element_name: str = None) -> None:
        if element_name is None:
            self._failure_counts = {}
            return
        self._failure_counts = {
            key: count for key, count in self._failure_counts.items() if key[1] != element_name
        }

    def save_verified_candidate(
        self, element_name: str, candidate: LocateCandidate, slot_name: str,
        screen_width: int, screen_height: int,
    ) -> Result[bool]:
        """Salva una coordinata verificata solo se NON viene gia' da una
        fonte curata/memorizzata (Profile/Memory): altrimenti sarebbe un
        riscrivere cio' che era gia' noto."""
        if candidate.strategy_name in ("profile", "memory"):
            return Result.ok(False)

        if element_name in _LEGACY_MEMORY_FIELDS:
            kwargs = {_LEGACY_MEMORY_FIELDS[element_name]: (candidate.x, candidate.y)}
            _persist_slot_memory(slot_name, **kwargs)
            return Result.ok(True)

        norm_x = candidate.x / screen_width if screen_width else 0.0
        norm_y = candidate.y / screen_height if screen_height else 0.0
        return save_autopilot_element(slot_name, element_name, candidate.x, candidate.y, norm_x, norm_y)
