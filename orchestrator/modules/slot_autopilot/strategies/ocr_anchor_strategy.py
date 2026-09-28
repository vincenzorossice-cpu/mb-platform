import difflib

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.strategies.base import (
    LocateCandidate,
    LocateContext,
    LocatorStrategy,
)


def _find_text_coords(query: str, raw_texts: list):
    """Reimplementata localmente (non importata da test_slot_autonomous.py,
    che resta off-limits oltre ai simboli esplicitamente riusati): match
    esatto, poi case-insensitive, poi fuzzy (difflib)."""
    for text, (x, y), _conf in raw_texts:
        if text == query:
            return (x, y)
    query_lower = query.lower()
    for text, (x, y), _conf in raw_texts:
        if text.lower() == query_lower:
            return (x, y)
    texts = [text for text, _coords, _conf in raw_texts]
    match = difflib.get_close_matches(query_lower, [t.lower() for t in texts], n=1, cutoff=0.6)
    if match:
        for text, (x, y), _conf in raw_texts:
            if text.lower() == match[0]:
                return (x, y)
    return None


class OCRAnchorStrategy(LocatorStrategy):
    """Terza scelta: cerca una delle etichette OCR configurate nel profilo tra
    i testi letti sullo schermo corrente."""
    name = "ocr_anchor"

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        if context.profile is None:
            return Result.fail("No profile loaded", "OCR_ANCHOR_NO_LABELS")
        element = context.profile.elements.get(element_name)
        labels = element.ocr_labels if element else []
        if not labels:
            return Result.fail(f"No ocr_labels configured for '{element_name}'", "OCR_ANCHOR_NO_LABELS")

        for label in labels:
            coords = _find_text_coords(label, context.raw_texts)
            if coords is not None:
                x, y = coords
                return Result.ok(LocateCandidate(x=x, y=y, confidence=0.75, strategy_name=self.name))

        return Result.fail(f"None of {labels} found in raw_texts", "OCR_ANCHOR_NOT_FOUND")
