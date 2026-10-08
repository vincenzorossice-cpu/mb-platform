"""Punto diretto per la navigazione: UNA domanda a Sol ("dammi il centro di X, coordinate 0-1000") al posto della
ricerca a griglia (3 chiamate). Si usa come `region_refiner` di ScreenNavigator, che verifica gia' l'effetto del tocco:
se la stessa descrizione torna una seconda volta di fila il punto diretto non ha funzionato e si passa alla griglia."""
import base64
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You locate one control on an Android screenshot. Answer ONLY with JSON: "
    '{"x": int, "y": int} | {"found": false}. x and y are NORMALIZED to a 0-1000 grid (x from the left edge, y from '
    "the top) and must be the CENTER of the described control. If it is not visible answer {\"found\": false}. "
    "NEVER guess."
)


def build_direct_refiner(gpt_client, grid_refiner: Callable, stats: Optional[dict] = None) -> Callable:
    stats = stats if stats is not None else {}
    stats.setdefault("direct", 0)
    stats.setdefault("grid", 0)
    last = {"description": None}

    def refine(description: str, screenshot_path: str, width: int, height: int):
        repeated = last["description"] == description
        last["description"] = description
        point = None if repeated else _ask_point(gpt_client, description, screenshot_path, width, height)
        if point is None:
            stats["grid"] += 1
            return grid_refiner(description, screenshot_path, width, height)
        stats["direct"] += 1
        return point

    return refine


def _ask_point(gpt_client, description: str, screenshot_path: str, width: int, height: int):
    try:
        with open(screenshot_path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("utf-8")
    except OSError:
        return None
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": f"Find the center of: {description}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ]},
    ]
    result = gpt_client._create_completion(messages, max_tokens=400, purpose="punto diretto")
    if not result.success or not isinstance(result.data, dict):
        return None
    try:
        nx, ny = float(result.data["x"]), float(result.data["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
        return None
    return round(nx / 1000 * width), round(ny / 1000 * height)
