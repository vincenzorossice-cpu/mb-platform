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
# (celle ~270x300 px), passo B sulla cella scelta (celle ~90x100 px), passo C
# sulla cella B (celle ~34x38 px, sotto la taglia di un pulsante). Una cella 3x3 (360x800) e' troppo grande per colpire
# un cerchietto da ~100 px.
_COARSE_GRID = (4, 8)
_FINE_GRID = (6, 6)
# Passo C: zoom sulla cella fine scelta, celle ~34x38 px (un tasto "-" misura ~60 px).
_FINEST_GRID = (4, 4)
_AVOID_RADIUS_PX = 50  # stesso raggio di ElementLocator.REJECT_RADIUS_PX
# Risposta ESPLICITA "l'elemento non e' in questa immagine" (cell: null): diversa da una
# risposta illeggibile. Al passo C significa che il passo B aveva sbagliato: nessun tocco.
_NOT_VISIBLE = "__NOT_VISIBLE__"

# Descrizioni GENERICHE dei nomi elemento del progetto (valide per qualunque
# slot): senza, GPT riceve solo un identificatore come 'bet_minus'.
_ELEMENT_DESCRIPTIONS = {
    "bet_minus": (
        "the button that lowers the bet: a minus (-) sign, a left-pointing arrow or chevron, or a 'BET -' button, "
        "normally next to the spin button or the bet amount; it may look dimmed when the minimum bet is reached. "
        "It is NEVER the spin button itself, the 'i' (info) icon, the menu (three lines) icon or the "
        "stack-of-coins icon"
    ),
    "bet_plus": (
        "the button that raises the bet: a plus (+) sign, a right-pointing arrow or chevron, or a 'BET +' button, "
        "normally next to the spin button or the bet amount. It is NEVER the spin button itself, the 'i' (info) "
        "icon or the menu (three lines) icon"
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


def _text_center(coords: tuple, context: LocateContext) -> tuple:
    """L'OCR restituisce l'angolo in alto a sinistra del testo: se il riquadro
    corrispondente e' noto si tocca il suo CENTRO (l'angolo di un testo piccolo
    e' spesso fuori dal pulsante)."""
    for box in context.text_boxes:
        if abs(box.bbox[0] - coords[0]) <= 3 and abs(box.bbox[1] - coords[1]) <= 3:
            return (round((box.bbox[0] + box.bbox[2]) / 2), round((box.bbox[1] + box.bbox[3]) / 2))
    return coords


def _bet_hint(element_name: str, context: LocateContext) -> str:
    """Per i pulsanti -/+ della puntata: se e' aperto un pannello a piu' righe
    (es. monete per riga, valore moneta, puntata totale) GPT deve scegliere la
    riga della puntata TOTALE, riconoscibile dal fatto che mostra l'importo
    della puntata corrente letto dallo schermo. Generico, nessun nome di slot."""
    if element_name not in ("bet_minus", "bet_plus"):
        return ""
    posta = context.slot_ocr_state.get("posta") if context.slot_ocr_state else None
    if posta is None:
        return ""
    return (
        f" The current total bet shown on screen is {float(posta):.2f}. If an open panel has several rows "
        "with their own -/+ buttons, choose the button of the row whose field shows that TOTAL bet amount, "
        "not the buttons of the other rows."
    )


def _avoid_note(rejected_positions: list, width: int, height: int, avoid_zones: list = ()) -> str:
    """Frase per la domanda: punti gia' rivelatisi sbagliati e zone da non scegliere (es. il
    pulsante di spin), in frazioni dello schermo (GPT ragiona meglio cosi' che in pixel)."""
    note = ""
    if rejected_positions:
        spots = ", ".join(f"({x / width:.0%} from left, {y / height:.0%} from top)" for x, y in rejected_positions)
        note += (
            f" IMPORTANT: tapping the icon at {spots} was already tried and it was the WRONG element "
            "(it opened another screen): do NOT choose it."
        )
    for x, y, _radius, label in avoid_zones:
        note += (
            f" IMPORTANT: the {label} is around ({x / width:.0%} from left, {y / height:.0%} from top): "
            "that is NOT the element to find, do NOT choose it or anything touching it."
        )
    return note


class GPTLocatorStrategy(LocatorStrategy):
    """Sesta scelta, fallback verso GPT-4o-mini Vision: usata solo se
    registrata nella catena (AutoPilotConfig.enable_gpt_fallback=False la
    esclude semplicemente dal dict di strategie passato a ElementLocator,
    nessun flag interno da controllare qui)."""
    name = "gpt"

    def __init__(self, gpt_client):
        self.gpt_client = gpt_client

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        if element_name in _ELEMENT_DESCRIPTIONS:
            # Elemento "icona" (spin, -, +, menu, chiudi): un testo OCR li' vicino
            # non e' mai il pulsante (e tappare una scritta, magari sull'angolo,
            # e' come non fare nulla): si va direttamente alla visione a griglia.
            coords = self._refine_icon(element_name, context)
            if coords is None:
                return Result.fail(
                    f"GPT could not locate the icon '{element_name}' on the grid", "GPT_LOCATOR_UNRESOLVED_TARGET",
                )
            return Result.ok(LocateCandidate(x=coords[0], y=coords[1], confidence=0.8, strategy_name=self.name))

        candidate_texts = [text for text, _coords, _conf in context.raw_texts]
        questions = {
            "action": choice_question(
                f"Tap the UI element '{element_name}' ({_describe(element_name)}) if you can see it on screen."
                + _avoid_note(context.rejected_positions, context.screen_width, context.screen_height, context.avoid_zones)
                + _bet_hint(element_name, context),
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

        x, y = _text_center(coords, context)
        return Result.ok(LocateCandidate(x=x, y=y, confidence=confidence, strategy_name=self.name))

    def locate_described(self, description: str, context: LocateContext):
        """(x, y) di un elemento descritto a parole dall'operatore (es. "l'icona
        delle monete impilate in basso al centro"), con lo stesso affinamento a
        griglia delle icone: la frase diventa la descrizione dell'elemento. None
        se GPT non lo trova."""
        return self._refine_icon("operator_target", context, description=description)

    def _ask_cell(self, element_name: str, image_path: str, labels: list, stage: str, note: str = "",
                  description: str = None):
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

        if description:
            what = f"The operator describes the element to find as: \"{description}\"."
        else:
            what = f"Element to find: '{element_name}' - {_describe(element_name)}."
        user_text = (
            f"{what}{note}\n"
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
        if isinstance(data, dict) and "cell" in data and data["cell"] is None:
            return _NOT_VISIBLE
        if not isinstance(cell, str) or not cell.strip():
            return None
        return cell.strip().upper()

    def _refine_icon(self, element_name: str, context: LocateContext, description: str = None):
        """(x, y) assoluti del centro della cella fine scelta, o None."""
        width, height = context.screen_width, context.screen_height
        with tempfile.TemporaryDirectory(prefix="autopilot_grid_") as tmp:
            try:
                coarse_cols, coarse_rows = _COARSE_GRID
                coarse_path = os.path.join(tmp, "coarse.png")
                coarse = annotate_grid(context.screenshot_path, (0, 0, width, height),
                                       coarse_cols, coarse_rows, coarse_path)
                note = _avoid_note(context.rejected_positions, width, height, context.avoid_zones) + _bet_hint(
                    element_name, context,
                )
                label = self._ask_cell(element_name, coarse_path, list(coarse), "coarse", note, description)
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
                    and not any(math.hypot(cx - zx, cy - zy) <= radius
                                for zx, zy, radius, _label in context.avoid_zones)
                ]
                if not fine_labels:
                    logger.warning("'%s': tutte le celle fini sono vicine a punti scartati", element_name)
                    return None
                label_b = self._ask_cell(element_name, fine_path, fine_labels, "fine", note, description)
                if label_b not in fine_labels:
                    logger.warning("'%s': passo B senza cella valida (%r)", element_name, label_b)
                    return None
                point = fine[label_b]

                # Passo C: zoom sulla cella B scelta. Se non da' una risposta
                # valida si tiene il centro della cella B (mai quello della cella grande).
                finest_cols, finest_rows = _FINEST_GRID
                fine_cell_w, fine_cell_h = (box[2] - box[0]) / fine_cols, (box[3] - box[1]) / fine_rows
                box_c = refinement_box(point, fine_cell_w, fine_cell_h, width, height)
                finest_path = os.path.join(tmp, "finest.png")
                finest = annotate_grid(context.screenshot_path, box_c, finest_cols, finest_rows, finest_path)
                label_c = self._ask_cell(element_name, finest_path, list(finest), "finest", note, description)
                if label_c == _NOT_VISIBLE:
                    # Nel ritaglio zoomato GPT NON vede l'elemento: il passo B aveva sbagliato.
                    # Tappare comunque il centro della cella B e' tirare a indovinare (e ha gia'
                    # colpito uno spin): nessun punto.
                    logger.warning("'%s': al passo C GPT non vede l'elemento nella cella %s: nessun tocco.",
                                   element_name, label_b)
                    return None
                if label_c in finest and not any(
                    math.hypot(finest[label_c][0] - rx, finest[label_c][1] - ry) <= _AVOID_RADIUS_PX
                    for rx, ry in context.rejected_positions
                ) and not any(
                    math.hypot(finest[label_c][0] - zx, finest[label_c][1] - zy) <= radius
                    for zx, zy, radius, _label in context.avoid_zones
                ):
                    point = finest[label_c]
                else:
                    logger.warning("'%s': passo C senza cella valida (%r), tengo il punto del passo B",
                                   element_name, label_c)
                logger.info("'%s': cella %s -> %s -> %s, punto %s", element_name, label, label_b, label_c, point)
                return point
            except (OSError, ValueError) as exc:
                logger.warning("'%s': affinamento icona fallito: %s", element_name, exc)
                return None
