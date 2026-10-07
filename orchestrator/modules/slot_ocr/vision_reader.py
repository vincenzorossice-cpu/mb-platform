import base64
import io
import json
import logging
import math
import re
import time
from typing import Optional

import openai
from typing import Callable
from PIL import Image

from orchestrator.core.amount_format import find_amount_run
from orchestrator.core.openai_compat import create_with_param_fallback
from orchestrator.core.openai_retry import call_with_rate_limit_retry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You read numbers off a screenshot of a mobile slot-machine game. Respond ONLY with JSON: "
    '{"credit_text": <string or null>, "bet_text": <string or null>}. '
    "credit_text = the player's balance / credit shown on screen. bet_text = the current TOTAL bet, i.e. the "
    "amount staked on one spin (not the value of a single line or coin, unless that is the only bet shown). "
    "COPY each amount EXACTLY as it is written on screen, character by character, including spaces, dots and "
    "commas used as separators (for example \"49 999.90\" or \"1.250,00\"); leave out only the currency "
    "symbol. Do NOT convert, round or reformat the numbers. If several amounts are shown as selectable "
    "options (a row of bet buttons), the current bet is the one that is highlighted/selected; if you cannot "
    "tell which, use null. Use null for anything you cannot read with certainty. Never guess. IGNORE jackpot and "
    "progressive meters (labels such as JACKPOT, MINI, MIDI, MAXI, GRAND, or any large number that keeps "
    "changing while the game is idle): they are neither the credit nor the bet. The credit and the bet are "
    "never the same number."
)
# Per leggere un importo non serve l'immagine intera: lato lungo ridotto e detail "low" (circa 2.800 token
# invece di decine di migliaia: risposta piu' veloce e lontani dal limite di token al minuto).
_MAX_IMAGE_SIDE_PX = 960


def _encode_reduced(screenshot_path: str) -> str:
    """PNG in base64 con il lato lungo ridotto a _MAX_IMAGE_SIDE_PX (mai ingrandito)."""
    with Image.open(screenshot_path) as image:
        image = image.convert("RGB")
        longest = max(image.size)
        if longest > _MAX_IMAGE_SIDE_PX:
            scale = _MAX_IMAGE_SIDE_PX / longest
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _to_amount(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if isinstance(value, str):
        run = find_amount_run(value, allow_single_digit=True)
        if run is not None:
            try:
                return float(run)
            except ValueError:
                return None
    return None


class VisionAmountReader:
    """Legge saldo e puntata guardando lo screenshot con GPT (visione), per le slot dove l'OCR non li
    legge o li legge male. Usa il client OpenAI diretto di GPTDecisionClient (come la ricerca a griglia)."""

    def __init__(self, gpt_client, model: str = None):
        self.gpt_client = gpt_client
        # Modello per leggere gli importi: di norma uno economico (GPT_READER_MODEL), non quello decisionale.
        self.model = model
        self._cache = {}

    def read(self, screenshot_path: str) -> dict:
        """{'saldo': float|None, 'posta': float|None}; mai eccezioni: in caso di errore tutto None."""
        if screenshot_path in self._cache:
            return dict(self._cache[screenshot_path])
        empty = {"saldo": None, "posta": None}
        try:
            encoded = _encode_reduced(screenshot_path)
            started = time.monotonic()
            model = self.model or self.gpt_client.model
            response = call_with_rate_limit_retry(lambda: create_with_param_fallback(
                self.gpt_client.client.chat.completions.create,
                dict(
                    model=model, response_format={"type": "json_object"}, temperature=0, max_tokens=100,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": [
                            {"type": "text", "text": "Read the credit and the current total bet."},
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": "low"}},
                        ]},
                    ],
                ),
            ))
            call_seconds = time.monotonic() - started
            record = getattr(self.gpt_client, "record_usage", None)
            if callable(record):
                record(response, "importi", model, seconds=call_seconds)
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
        # Si interpreta il TESTO copiato (il modello sbaglia a convertire "49 999.90" in un numero); le vecchie
        # chiavi numeriche restano accettate.
        credit = data.get("credit_text", data.get("credit"))
        bet = data.get("bet_text", data.get("bet"))
        result = {"saldo": _to_amount(credit), "posta": _to_amount(bet)}
        logger.info("lettura per visione: saldo=%s (testo %r) puntata=%s (testo %r) [%.1f s]",
                    result["saldo"], credit, result["posta"], bet, call_seconds)
        if len(self._cache) >= 4:
            self._cache.clear()
        self._cache[screenshot_path] = dict(result)
        return result


_KEYS = ("saldo", "posta")
# Un singolo spin non puo' cambiare il saldo di oltre la meta': una lettura piu' lontana e' un errore di lettura.
_BALANCE_CONTINUITY = 0.5
_BET_RECHECK_EVERY = 10   # letture in modo spin tra due riletture di controllo della puntata con la visione


def _within(value: float, reference: float, tolerance: float) -> bool:
    return abs(value - reference) <= tolerance * max(abs(reference), 1e-9)


def _powers_of_ten_apart(a: float, b: float) -> bool:
    """True se i due valori differiscono di una potenza di dieci (49999.9 contro 49.99): il segno di cifre
    delle migliaia perse da uno dei due lettori."""
    if a <= 0 or b <= 0:
        return False
    ratio = max(a, b) / min(a, b)
    exponent = round(math.log10(ratio))
    return exponent >= 1 and abs(ratio - 10 ** exponent) <= 0.01 * 10 ** exponent


class ResilientOCRReader:
    """Stessa interfaccia di SlotOCRReader, con la visione come rete di sicurezza per saldo e puntata.
    Alla PRIMA lettura confronta OCR e visione CAMPO PER CAMPO: la visione prende solo i campi che l'OCR non
    legge o contraddice. Dopo, per i campi affidati alla visione, vale il valore su cui i due concordano,
    altrimenti (per il saldo) quello coerente con l'ultimo saldo fidato; se nessuno lo e', il campo resta
    sconosciuto. Dove l'OCR basta non costa nulla. La vincita resta quella dell'OCR."""

    def __init__(
        self, ocr, vision: Optional[VisionAmountReader], tolerance: float = 0.10,
        capture: Optional[Callable[[], Optional[str]]] = None, confirm_delay: float = 1.5,
    ):
        self.ocr = ocr
        self.vision = vision
        self.tolerance = tolerance
        # capture() -> percorso di uno screenshot NUOVO: serve a smascherare i contatori (jackpot) che la visione
        # scambia per saldo/puntata: un numero che cambia tra due letture ravvicinate non e' il credito.
        self._capture = capture
        self._confirm_delay = confirm_delay
        self._warn_counts = {}
        self._self_checked = False
        self._vision_fields = set()
        # Alla prima lettura l'OCR non ha letto NE' saldo NE' puntata: durante gli spin e' tempo perso.
        self._ocr_useless_for_amounts = False
        self._spin_mode = False
        self._bet_skips = 0       # letture consecutive in cui la puntata fidata ha sostituito la visione
        # Ultimo valore fidato per campo (concordato tra i due lettori o coerente con la storia).
        self._trusted = {"saldo": None, "posta": None}

    def set_spin_mode(self, active: bool) -> None:
        """Durante gli spin, se l'OCR ha dimostrato di non leggere gli importi di questa slot, la lettura
        di saldo/puntata salta l'OCR a schermo intero (~7 s su CPU) e usa solo la visione. Le letture
        dei testi con le caselle (menu, icone) restano all'OCR."""
        self._spin_mode = bool(active)
        if not active:
            self._bet_skips = 0

    @property
    def prefer_vision(self) -> bool:
        return bool(self._vision_fields)

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
        if self.vision is not None and self._spin_mode and self._ocr_useless_for_amounts and self._self_checked:
            empty = {"saldo": None, "posta": None, "vincita": None, "spin_button_coords": None,
                     "is_active": False, "raw_texts": []}
            started = time.monotonic()
            merged = self._combine(empty, self.vision.read(screenshot_path))
            logger.info("lettura importi (solo visione, OCR saltato): %.1f s", time.monotonic() - started)
            return merged
        ocr_started = time.monotonic()
        state = self.ocr.read_slot_state(screenshot_path)
        ocr_seconds = time.monotonic() - ocr_started
        if self.vision is None:
            return state
        if not self._self_checked:
            self._self_checked = True
            self._ocr_useless_for_amounts = state.get("saldo") is None and state.get("posta") is None
            return self._first_read(state, self.vision.read(screenshot_path))
        if self._can_reuse_trusted_bet(state):
            # La puntata non cambia durante gli spin e l'OCR non la legge: la lettura verificata vale, senza una
            # chiamata di visione a ogni spin (~1 s e un costo). Ogni _BET_RECHECK_EVERY letture si ricontrolla.
            self._bet_skips += 1
            merged = dict(state)
            merged["posta"] = self._trusted["posta"]
            return self._sane(merged)
        if self._vision_fields or any(state.get(key) is None for key in _KEYS) or self._balance_is_suspicious(state):
            self._bet_skips = 0
            vision_started = time.monotonic()
            merged = self._combine(state, self.vision.read(screenshot_path))
            logger.info("lettura importi: OCR %.1f s, visione %.1f s", ocr_seconds, time.monotonic() - vision_started)
            return merged
        for key in _KEYS:
            self._trusted[key] = state[key]
        return state

    def _can_reuse_trusted_bet(self, state: dict) -> bool:
        return (
            self._spin_mode and self._vision_fields == {"posta"} and self._trusted["posta"] is not None
            and state.get("posta") is None and state.get("saldo") is not None
            and not self._balance_is_suspicious(state) and self._bet_skips < _BET_RECHECK_EVERY
        )

    def _balance_is_suspicious(self, state: dict) -> bool:
        balance, trusted = state.get("saldo"), self._trusted["saldo"]
        return balance is not None and trusted is not None and not _within(balance, trusted, _BALANCE_CONTINUITY)

    def _first_read(self, state: dict, seen: dict) -> dict:
        merged = dict(state)
        second = {}                           # seconda lettura (screenshot nuovo), fatta al piu' una volta
        for key in _KEYS:
            ocr_value, vision_value = state.get(key), seen.get(key)
            if vision_value is None:
                continue                      # la visione non dice nulla: resta l'OCR
            if ocr_value is not None and _within(ocr_value, vision_value, self.tolerance):
                merged[key] = self._trusted[key] = ocr_value
            elif ocr_value is not None and _powers_of_ten_apart(ocr_value, vision_value) and ocr_value > vision_value:
                # Perdere le cifre delle migliaia e' molto piu' comune che inventarle: vince il valore piu' grande.
                logger.warning("'%s': OCR %s contro visione %s (differiscono di una potenza di dieci): "
                               "tengo il piu' grande, l'OCR.", key, ocr_value, vision_value)
                merged[key] = self._trusted[key] = ocr_value
            elif self._is_stable(key, vision_value, second) is False:
                # La visione ha letto un numero che cambia da una lettura all'altra: un contatore (jackpot), non
                # il credito. Non e' un ancoraggio: resta l'OCR (o sconosciuto) e la visione non lo ridefinisce.
                logger.warning("'%s': la visione ha letto %s ma il valore cambia tra due letture (contatore?): "
                               "lo scarto.", key, vision_value)
                merged[key] = ocr_value
                if ocr_value is not None:
                    # L'OCR legge il campo: la visione e' quella inaffidabile e NON entra nel ciclo (altrimenti a ogni
                    # spin partirebbe una chiamata di visione inutile, e un suo errore scarterebbe l'OCR giusto).
                    self._trusted[key] = ocr_value
                else:
                    self._vision_fields.add(key)   # l'OCR non legge nulla: la visione resta l'unica fonte (non confermata)
            else:
                self._vision_fields.add(key)
                merged[key] = self._trusted[key] = vision_value
        if self._vision_fields:
            logger.warning(
                "L'OCR legge saldo=%s puntata=%s, la visione saldo=%s puntata=%s: uso la visione per: %s.",
                state.get("saldo"), state.get("posta"), seen.get("saldo"), seen.get("posta"),
                ", ".join(sorted(self._vision_fields)),
            )
        return self._sane(merged)

    def _is_stable(self, key: str, first_value: float, second: dict) -> Optional[bool]:
        """True se una seconda lettura ravvicinata (screenshot nuovo) da' lo stesso valore, False se cambia o non
        c'e' piu', None se non si puo' controllare (nessuna cattura disponibile)."""
        if self._capture is None or self.vision is None:
            return None
        if "read" not in second:
            time.sleep(self._confirm_delay)
            path = self._capture()
            second["read"] = self.vision.read(path) if path else None
        if second["read"] is None:
            return None
        again = second["read"].get(key)
        # Identico: un contatore che sale di pochi centesimi e' 'quasi uguale' ma non e' il credito.
        return again is not None and abs(again - first_value) < 0.005

    @staticmethod
    def _sane(merged: dict) -> dict:
        """Una puntata che non e' minore del saldo e' un errore di lettura (credito letto come puntata)."""
        bet, balance = merged.get("posta"), merged.get("saldo")
        if bet is not None and balance is not None and bet >= balance:
            logger.warning("puntata %s non inferiore al saldo %s: la scarto.", bet, balance)
            merged = dict(merged)
            merged["posta"] = None
        return merged

    def _warn(self, kind: str, message: str, *args) -> None:
        """Lo stesso avviso ripetuto a ogni lettura copre il resto del log: il primo e poi uno ogni 10."""
        count = self._warn_counts.get(kind, 0)
        self._warn_counts[kind] = count + 1
        if count % 10 == 0:
            logger.warning(message + ("" if count == 0 else f" [ripetuto {count} volte]"), *args)

    def _combine(self, state: dict, seen: dict) -> dict:
        merged = dict(state)
        for key in _KEYS:
            ocr_value, vision_value = state.get(key), seen.get(key)
            if key in self._vision_fields or (
                key == "saldo" and ocr_value is not None and self._balance_is_suspicious(state)
            ):
                merged[key] = self._arbitrate(key, ocr_value, vision_value)
            elif ocr_value is None and vision_value is not None:
                merged[key] = self._trusted[key] = vision_value
            elif ocr_value is not None:
                self._trusted[key] = ocr_value
        return self._sane(merged)

    def _arbitrate(self, key: str, ocr_value: Optional[float], vision_value: Optional[float]) -> Optional[float]:
        trusted = self._trusted[key]

        def accept(value):
            self._trusted[key] = value
            return value

        if vision_value is None:
            # La visione non risponde (errore, limite di richieste): l'OCR vale solo se concorda col fidato.
            if ocr_value is not None and trusted is not None and _within(ocr_value, trusted, self.tolerance):
                return accept(ocr_value)
            if ocr_value is not None:
                self._warn("no_vision_" + key, "visione non disponibile: scarto l'importo dell'OCR per '%s' (%s contro %s).",
                           key, ocr_value, trusted)
            return None
        if ocr_value is not None and _within(ocr_value, vision_value, self.tolerance):
            return accept(vision_value)
        if trusted is None or key != "saldo":
            return accept(vision_value)
        if _within(vision_value, trusted, _BALANCE_CONTINUITY):
            return accept(vision_value)
        if ocr_value is not None and _within(ocr_value, trusted, _BALANCE_CONTINUITY):
            self._warn("discard_vision", "saldo: scarto la visione (%s) perche' incoerente con l'ultimo saldo (%s): tengo l'OCR (%s).",
                       vision_value, trusted, ocr_value)
            return accept(ocr_value)
        self._warn("ignore_balance", "saldo: ne' l'OCR (%s) ne' la visione (%s) sono coerenti con l'ultimo saldo (%s): lo ignoro.",
                   ocr_value, vision_value, trusted)
        return None
