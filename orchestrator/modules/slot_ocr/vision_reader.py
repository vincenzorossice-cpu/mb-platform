import base64
import json
import logging
import re
from typing import Optional

import openai

from orchestrator.core.amount_format import normalize_number_run
from orchestrator.core.openai_retry import call_with_rate_limit_retry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You read numbers off a screenshot of a mobile slot-machine game. Respond ONLY with JSON: "
    '{"credit": <number or null>, "bet": <number or null>}. '
    "credit = the player's balance / credit shown on screen. bet = the current TOTAL bet, i.e. the amount "
    "staked on one spin (not the value of a single line or coin, unless that is the only bet shown). "
    "Write numbers as plain decimals with a dot as the decimal separator and NO thousands separators "
    "(for example 50000.00). Use null for anything you cannot read with certainty. Never guess."
)


def _to_amount(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if isinstance(value, str):
        match = re.search(r"\d[\d.,]*\d|\d", value)
        if match:
            try:
                return float(normalize_number_run(match.group(0)))
            except ValueError:
                return None
    return None


class VisionAmountReader:
    """Legge saldo e puntata guardando lo screenshot con GPT (visione), per le slot dove l'OCR non li
    legge o li legge male. Usa il client OpenAI diretto di GPTDecisionClient (come la ricerca a griglia)."""

    def __init__(self, gpt_client):
        self.gpt_client = gpt_client
        self._cache = {}

    def read(self, screenshot_path: str) -> dict:
        """{'saldo': float|None, 'posta': float|None}; mai eccezioni: in caso di errore tutto None."""
        if screenshot_path in self._cache:
            return dict(self._cache[screenshot_path])
        empty = {"saldo": None, "posta": None}
        try:
            with open(screenshot_path, "rb") as img_file:
                encoded = base64.b64encode(img_file.read()).decode("utf-8")
            response = call_with_rate_limit_retry(lambda: self.gpt_client.client.chat.completions.create(
                model=self.gpt_client.model, response_format={"type": "json_object"},
                temperature=0, max_tokens=80,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Read the credit and the current total bet."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                    ]},
                ],
            ))
            data = json.loads(response.choices[0].message.content)
        except OSError as exc:
            logger.warning("lettura per visione: screenshot illeggibile: %s", exc)
            return empty
        except openai.OpenAIError as exc:
            logger.warning("lettura per visione: errore OpenAI: %s", exc)
            return empty
        except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
            logger.warning("lettura per visione: risposta non valida: %s", exc)
            return empty
        if not isinstance(data, dict):
            return empty
        result = {"saldo": _to_amount(data.get("credit")), "posta": _to_amount(data.get("bet"))}
        logger.info("lettura per visione: saldo=%s puntata=%s", result["saldo"], result["posta"])
        if len(self._cache) >= 4:
            self._cache.clear()
        self._cache[screenshot_path] = dict(result)
        return result


class ResilientOCRReader:
    """Stessa interfaccia di SlotOCRReader, con la visione come rete di sicurezza per saldo e puntata.
    Alla PRIMA lettura confronta OCR e visione: se discordano oltre la tolleranza (o l'OCR non legge) usa
    la visione per tutta la sessione; altrimenti la chiama solo se mancano saldo o puntata. Dove l'OCR
    basta non costa nulla. La vincita resta quella dell'OCR."""

    def __init__(self, ocr, vision: Optional[VisionAmountReader], tolerance: float = 0.10):
        self.ocr = ocr
        self.vision = vision
        self.tolerance = tolerance
        self.prefer_vision = False
        self._self_checked = False

    @property
    def region(self):
        return self.ocr.region

    def set_region(self, region) -> None:
        self.ocr.set_region(region)

    def read_text_boxes(self, screenshot_path: str):
        return self.ocr.read_text_boxes(screenshot_path)

    def __getattr__(self, name):
        # Tutto il resto (attributi privati, costanti...) e' quello dell'OCR sottostante.
        return getattr(self.__dict__["ocr"], name)

    def read_slot_state(self, screenshot_path: str) -> dict:
        state = self.ocr.read_slot_state(screenshot_path)
        if self.vision is None:
            return state
        if not self._self_checked:
            self._self_checked = True
            seen = self.vision.read(screenshot_path)
            self.prefer_vision = self._disagree(state, seen)
            if self.prefer_vision:
                logger.warning(
                    "L'OCR legge saldo=%s puntata=%s, la visione saldo=%s puntata=%s: uso la visione per gli importi.",
                    state.get("saldo"), state.get("posta"), seen.get("saldo"), seen.get("posta"),
                )
                return self._merged(state, seen)
            return self._filled(state, seen)
        if self.prefer_vision:
            return self._merged(state, self.vision.read(screenshot_path))
        if state.get("saldo") is None or state.get("posta") is None:
            return self._filled(state, self.vision.read(screenshot_path))
        return state

    def _disagree(self, state: dict, seen: dict) -> bool:
        for key in ("saldo", "posta"):
            ocr_value, vision_value = state.get(key), seen.get(key)
            if vision_value is None:
                continue
            if ocr_value is None:
                return True
            if abs(ocr_value - vision_value) > self.tolerance * max(abs(vision_value), 1e-9):
                return True
        return False

    @staticmethod
    def _merged(state: dict, seen: dict) -> dict:
        merged = dict(state)
        for key in ("saldo", "posta"):
            if seen.get(key) is not None:
                merged[key] = seen[key]
        return merged

    @staticmethod
    def _filled(state: dict, seen: dict) -> dict:
        filled = dict(state)
        for key in ("saldo", "posta"):
            if filled.get(key) is None and seen.get(key) is not None:
                filled[key] = seen[key]
        return filled
