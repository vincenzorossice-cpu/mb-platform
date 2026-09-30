import base64
import json
import logging
import math
import os
import tempfile

import openai

from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.interface import choice_question
from orchestrator.modules.slot_autopilot.grid_locator import annotate_grid, refinement_box
from orchestrator.modules.slot_autopilot.strategies.base import (
    LocateCandidate,
    LocateContext,
    LocatorStrategy,
)
from orchestrator.tests.validation.test_slot_autonomous import resolve_target


logger = logging.getLogger(__name__)

# Griglie di affinamento per le icone senza testo: passo A su tutto lo schermo
# (celle ~270x300 px), passo B sulla cella scelta (celle ~90x100 px, circa la
# taglia di un pulsante). Una cella 3x3 (360x800) e' troppo grande per colpire
# un cerchietto da ~100 px.
_COARSE_GRID = (4, 8)
_FINE_GRID = (6, 6)
_AVOID_RADIUS_PX = 120  # stesso raggio di ElementLocator.REJECT_RADIUS_PX

# Descrizioni GENERICHE dei nomi elemento del progetto (valide per qualunque
# slot): senza, GPT riceve solo un identificatore come 'bet_minus'.
_ELEMENT_DESCRIPTIONS = {
    "bet_minus": (
        "the minus (-) button that lowers the bet, normally next to the round spin button or the bet amount; "
        "it may look dimmed when the minimum bet is reached. It is NEVER the 'i' (info) icon, the menu "
        "(three lines) icon or the stack-of-coins icon"
    ),
    "bet_plus": (
        "the plus (+) button that raises the bet, normally next to the round spin button or the bet amount. "
        "It is NEVER the 'i' (info) icon or the menu (three lines) icon"
    ),
    "bet_menu_toggle": "the icon (often a stack of coins) that opens or closes the bet selection menu",
    "spin": "the large round button that starts a spin (usually with circular arrows)",
    "close_button": (
        "the X (close) button of a panel or popup that is open on top of the game, usually in a top corner "
        "of that panel"
    ),
}


_GRID_SYSTEM_PROMPT = (
    "You look at a screenshot of a mobile slot-machine game with a labeled grid drawn on top: each cell "
    "has a yellow label (like A1, B3) at its top-left corner. Your only job is to say which ONE cell "
    "contains the CENTER of the requested element. Use only labels from the list you are given; never "
    "invent labels. If the element is not visible in the image, answer null. Respond ONLY with JSON: "
    '{"cell": "<label or null>", "confidence": <0.0-1.0>}'
)


def _describe(element_name: str) -> str:
    return _ELEMENT_DESCRIPTIONS.get(element_name, element_name)


def _avoid_note(rejected_positions: list, width: int, height: int) -> str:
    """Frase per la domanda: punti gia' rivelatisi sbagliati, in frazioni
    dello schermo (GPT ragiona meglio cosi' che in pixel)."""
    if not rejected_positions:
        return ""
    spots = ", ".join(f"({x / width:.0%} from left, {y / height:.0%} from top)" for x, y in rejected_positions)
    return (
        f" IMPORTANT: tapping the icon at {spots} was already tried and it was the WRONG element "
        "(it opened another screen): do NOT choose it."
    )


class GPTLocatorStrategy(LocatorStrategy):
    """Sesta scelta, fallback verso GPT-4o-mini Vision: usata solo se
    registrata nella catena (AutoPilotConfig.enable_gpt_fallback=False la
    esclude semplicemente dal dict di strategie passato a ElementLocator,
    nessun flag interno da controllare qui)."""
    name = "gpt"

    def __init__(self, gpt_client):
        self.gpt_client = gpt_client

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        candidate_texts = [text for text, _coords, _conf in context.raw_texts]
        questions = {
            "action": choice_question(
                f"Tap the UI element '{element_name}' ({_describe(element_name)}) if you can see it on screen."
                + _avoid_note(context.rejected_positions, context.screen_width, context.screen_height),
                {"tap": "Touch the element", "stop": "Element not visible / cannot proceed"},
            ),
            "target": choice_question(
                "Which element should I tap? Answer with its exact visible text, or leave it "
                "and use a region instead.",
                {text: text for text in candidate_texts},
            ),
        }
        result = self.gpt_client.ask_with_image(
            context.screenshot_path, f"Locate the element: {element_name}", questions,
            context.screen_width, context.screen_height,
        )
        if not result.success:
            return Result.fail(result.error, result.error_code)

        answers = result.data.get("answers", {})
        action = answers.get("action", {}).get("choice")
        if action != "tap":
            return Result.fail(f"GPT did not choose to tap for '{element_name}'", "GPT_LOCATOR_NO_TAP")

        target = answers.get("target", {}).get("choice")
        confidence = answers.get("target", {}).get("confidence", 0.5)
        coords = None
        if isinstance(target, dict) and target.get("text"):
            coords = resolve_target(
                {"text": target["text"], "region": None}, context.raw_texts,
                context.screen_width, context.screen_height,
            )
        if coords is None and isinstance(target, dict) and target.get("region"):
            # Icona senza testo: la cella 3x3 (360x800 px) non colpirebbe mai
            # un pulsante, si affina in due passi con griglie etichettate.
            coords = self._refine_icon(element_name, context)
        if coords is None:
            return Result.fail(
                f"GPT target could not be resolved for '{element_name}'", "GPT_LOCATOR_UNRESOLVED_TARGET",
            )

        x, y = coords
        return Result.ok(LocateCandidate(x=x, y=y, confidence=confidence, strategy_name=self.name))


    def _ask_cell(self, element_name: str, image_path: str, labels: list, stage: str, note: str = ""):
        """Domanda Vision con un prompt DEDICATO alla griglia etichettata:
        ritorna l'etichetta scelta o None. Non passa da ask_with_image, il cui
        prompt di sistema impone a GPT di rispondere con testo OCR o regione
        3x3 (mai con una cella della mia griglia)."""
        try:
            with open(image_path, "rb") as img_file:
                encoded = base64.b64encode(img_file.read()).decode("utf-8")
        except OSError as exc:
            logger.warning("'%s' (%s): immagine illeggibile: %s", element_name, stage, exc)
            return None

        user_text = (
            f"Element to find: '{element_name}' - {_describe(element_name)}.{note}\n"
            f"Valid cell labels: {', '.join(labels)}.\n"
            "Reply with the label of the ONE cell that contains the CENTER of the element, or null if it "
            "is not visible in this image."
        )
        messages = [
            {"role": "system", "content": _GRID_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ]},
        ]
        try:
            response = self.gpt_client.client.chat.completions.create(
                model=self.gpt_client.model, messages=messages,
                response_format={"type": "json_object"}, temperature=0.1, max_tokens=100,
            )
            data = json.loads(response.choices[0].message.content)
        except openai.OpenAIError as exc:
            logger.warning("'%s' (%s): errore OpenAI: %s", element_name, stage, exc)
            return None
        except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
            logger.warning("'%s' (%s): risposta GPT non valida: %s", element_name, stage, exc)
            return None

        cell = data.get("cell") if isinstance(data, dict) else None
        logger.info("'%s' (%s): GPT risponde cella=%r confidenza=%r", element_name, stage, cell,
                    data.get("confidence") if isinstance(data, dict) else None)
        if not isinstance(cell, str) or not cell.strip():
            return None
        return cell.strip().upper()

    def _refine_icon(self, element_name: str, context: LocateContext):
        """(x, y) assoluti del centro della cella fine scelta, o None."""
        width, height = context.screen_width, context.screen_height
        with tempfile.TemporaryDirectory(prefix="autopilot_grid_") as tmp:
            try:
                coarse_cols, coarse_rows = _COARSE_GRID
                coarse_path = os.path.join(tmp, "coarse.png")
                coarse = annotate_grid(context.screenshot_path, (0, 0, width, height),
                                       coarse_cols, coarse_rows, coarse_path)
                note = _avoid_note(context.rejected_positions, width, height)
                label = self._ask_cell(element_name, coarse_path, list(coarse), "coarse", note)
                if label not in coarse:
                    logger.warning("'%s': passo A senza cella valida (%r)", element_name, label)
                    return None

                fine_cols, fine_rows = _FINE_GRID
                box = refinement_box(coarse[label], width / coarse_cols, height / coarse_rows, width, height)
                fine_path = os.path.join(tmp, "fine.png")
                fine = annotate_grid(context.screenshot_path, box, fine_cols, fine_rows, fine_path)
                # Celle fini vicine a un punto gia' rivelatosi sbagliato: non
                # sono candidati validi.
                fine_labels = [
                    name for name, (cx, cy) in fine.items()
                    if not any(math.hypot(cx - rx, cy - ry) <= _AVOID_RADIUS_PX
                               for rx, ry in context.rejected_positions)
                ]
                if not fine_labels:
                    logger.warning("'%s': tutte le celle fini sono vicine a punti scartati", element_name)
                    return None
                label_b = self._ask_cell(element_name, fine_path, fine_labels, "fine", note)
                if label_b not in fine_labels:
                    logger.warning("'%s': passo B senza cella valida (%r)", element_name, label_b)
                    return None
                logger.info("'%s': cella %s -> %s, punto %s", element_name, label, label_b, fine[label_b])
                return fine[label_b]
            except (OSError, ValueError) as exc:
                logger.warning("'%s': affinamento icona fallito: %s", element_name, exc)
                return None
