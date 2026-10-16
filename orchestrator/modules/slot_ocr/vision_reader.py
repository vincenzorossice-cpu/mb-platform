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
from orchestrator.modules.slot_ocr.reader import (
    BALANCE_JUMP_RATIO as BALANCE_RATIO, MAX_BALANCE_VALUE as MAX_BALANCE, MAX_BET_VALUE, is_jackpot_balance,
)       # oltre: jackpot o saldo, mai una puntata

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
        if result["posta"] is not None and result["posta"] > MAX_BET_VALUE:
            logger.warning("lettura per visione: puntata %s oltre %s (un jackpot?): la scarto.", result["posta"], MAX_BET_VALUE)
            result["posta"] = None
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
# La visione (GPT) e' l'ultima risorsa: costa ~1 s e un po' di denaro a lettura. Le decisioni prese alla prima lettura
# (campo affidato alla visione, "OCR inutile") non valgono per sempre: si rivedono se l'OCR dimostra di leggere.
_RELEASE_AFTER_AGREEMENTS = 5   # letture di fila in cui OCR e visione concordano su un campo: torna all'OCR
_REPROBE_EVERY = 10             # in modo "OCR inutile": ogni quante letture si controlla se l'OCR ora legge
_REPROBE_NEEDED = 2             # controlli concordi di fila per tornare all'OCR
_RETRY_FAILURES_LIMIT = 2       # seconde letture OCR fallite di fila dopo le quali non si riprova (si va alla visione)
_RAW_TEXT_LIMIT = 30            # testi OCR grezzi riportati nel log quando saldo o puntata non si leggono
_SUMMARY_EVERY = 20             # ogni quante chiamate di visione si riassume il perche'


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
        self._agreements = {"saldo": 0, "posta": 0}   # letture di fila in cui OCR e visione concordano (campi a visione)
        self._useless_reads = 0   # letture fatte in modo "OCR inutile" (per il controllo periodico dell'OCR)
        self._probe_agreements = 0
        self._retry_failures = 0  # seconde letture OCR fallite di fila
        self.reads = 0            # letture di importi in totale
        self.vision_reads = 0     # letture che hanno chiamato la visione (le chiamate GPT "importi")
        self.vision_reasons = {}  # motivo -> chiamate di visione
        # Ultimo valore fidato per campo (concordato tra i due lettori o coerente con la storia).
        self._trusted = {"saldo": None, "posta": None}
        self._balance_discarded = False
        self._balance_source = None

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
        self._balance_discarded = False
        state = self._read_inner(screenshot_path)
        if state.get("saldo") is None and self._balance_discarded and self._trusted.get("saldo") is not None:
            # Il saldo letto era un jackpot e nessuna altra fonte lo ha dato: l'ultimo saldo fidato, non un buco.
            logger.warning("Saldo non leggibile, uso ultimo saldo noto: %s", self._trusted["saldo"])
            state = dict(state)
            state["saldo"] = self._trusted["saldo"]
            self._note_balance_source("ultimo noto", state["saldo"])
        elif state.get("saldo") is not None:
            self._note_balance_source("per visione" if "saldo" in self._vision_fields else "dopo etichetta SALDO:",
                                      state["saldo"])
        return state

    def _note_balance_source(self, source: str, value) -> None:
        """Una riga solo quando cambia DA DOVE arriva il saldo (non a ogni spin)."""
        if source != self._balance_source:
            self._balance_source = source
            logger.info("Saldo letto: %s (%s)", value, source)

    def _clean_balance(self, value, where: str):
        """Un saldo che e' un jackpot (oltre 100.000, o oltre 10x l'ultimo saldo fidato) non entra mai nel lettore:
        ne' come lettura, ne' come riferimento fidato, ne' per accordo di OCR e visione."""
        if value is None:
            return None
        trusted = self._trusted.get("saldo")
        if is_jackpot_balance(value, trusted):
            reason = "> 10× precedente" if trusted and value > BALANCE_RATIO * trusted else f"> {int(MAX_BALANCE)}"
            logger.warning("Saldo scartato: %.2f (jackpot, %s) [%s]", value, reason, where)
            self._balance_discarded = True
            return None
        return value

    def _ocr_state(self, path: str) -> dict:
        state = self.ocr.read_slot_state(path)
        if isinstance(state, dict) and state.get("saldo") is not None:
            state = dict(state)
            state["saldo"] = self._clean_balance(state["saldo"], "OCR")
        return state

    def _vision_read(self, path: str) -> dict:
        seen = self.vision.read(path)
        if isinstance(seen, dict) and seen.get("saldo") is not None:
            seen = dict(seen)
            seen["saldo"] = self._clean_balance(seen["saldo"], "visione")
        return seen

    def note_bet(self, value) -> None:
        note = getattr(self.ocr, "note_bet", None)
        if callable(note):
            note(value)
        if value is not None:
            try:
                self._trusted["posta"] = float(value)
            except (TypeError, ValueError):
                pass

    def _read_inner(self, screenshot_path: str) -> dict:
        self.reads += 1
        if self.vision is not None and self._spin_mode and self._ocr_useless_for_amounts and self._self_checked:
            self._useless_reads += 1
            if self._useless_reads % _REPROBE_EVERY == 0:
                return self._reprobe_ocr(screenshot_path)    # ogni tanto si controlla se l'OCR ora legge
            empty = {"saldo": None, "posta": None, "vincita": None, "spin_button_coords": None,
                     "is_active": False, "raw_texts": []}
            started = time.monotonic()
            self._count_vision("ocr_inutile")
            merged = self._combine(empty, self._vision_read(screenshot_path))
            logger.info("lettura importi (solo visione, OCR saltato): %.1f s", time.monotonic() - started)
            return merged
        ocr_started = time.monotonic()
        state = self._ocr_state(screenshot_path)
        ocr_seconds = time.monotonic() - ocr_started
        if self.vision is None:
            return state
        if not self._self_checked:
            self._self_checked = True
            self._ocr_useless_for_amounts = state.get("saldo") is None and state.get("posta") is None
            self._log_raw_ocr(state)
            self._count_vision("prima_lettura")
            return self._first_read(state, self._vision_read(screenshot_path))
        self._log_raw_ocr(state)
        if self._can_reuse_trusted_bet(state):
            # La puntata non cambia durante gli spin e l'OCR non la legge: la lettura verificata vale, senza una
            # chiamata di visione a ogni spin (~1 s e un costo). Ogni _BET_RECHECK_EVERY letture si ricontrolla.
            self._bet_skips += 1
            merged = dict(state)
            merged["posta"] = self._trusted["posta"]
            return self._sane(merged)
        if self._incomplete(state) and not self._vision_fields and self._retry_failures < _RETRY_FAILURES_LIMIT:
            # L'OCR ha perso un campo UNA volta (animazione, overlay): prima di pagare la visione si rilegge con uno
            # screenshot nuovo. La visione parte solo se anche la seconda lettura fallisce (due volte di fila).
            retried = self._retry_ocr()
            if retried is not None:
                logger.info("OCR: campo mancante alla prima lettura, la seconda e' completa: niente visione.")
                state, self._retry_failures = retried, 0
            else:
                self._retry_failures += 1
        elif not self._incomplete(state):
            self._retry_failures = 0
        reason = self._vision_reason(state)
        if reason is not None:
            self._bet_skips = 0
            self._count_vision(reason)
            vision_started = time.monotonic()
            merged = self._combine(state, self._vision_read(screenshot_path))
            logger.info("lettura importi: OCR %.1f s, visione %.1f s", ocr_seconds, time.monotonic() - vision_started)
            return merged
        for key in _KEYS:
            self._trusted[key] = state[key]
        return state

    @staticmethod
    def _incomplete(state: dict) -> bool:
        return any(state.get(key) is None for key in _KEYS)

    def _vision_reason(self, state: dict) -> Optional[str]:
        """Perche' questa lettura chiede la visione (None se l'OCR basta)."""
        if self._vision_fields:
            return "campo_a_visione:" + "+".join(sorted(self._vision_fields))
        if self._incomplete(state):
            return "campo_mancante"
        if self._balance_is_suspicious(state):
            return "saldo_sospetto"
        return None

    def _count_vision(self, reason: str) -> None:
        self.vision_reads += 1
        self.vision_reasons[reason] = self.vision_reasons.get(reason, 0) + 1
        if self.vision_reads % _SUMMARY_EVERY == 0:
            reasons = ", ".join(f"{r} {n}" for r, n in sorted(self.vision_reasons.items(), key=lambda item: -item[1]))
            logger.warning("Importi per visione: %d chiamate su %d letture (motivi: %s).",
                           self.vision_reads, self.reads, reasons)

    def _log_raw_ocr(self, state: dict) -> None:
        """Saldo o puntata non letti dall'OCR: si riportano i testi grezzi, per capire se manca l'etichetta, se il
        numero e' scritto in modo strano o se il testo non c'e' proprio. Il primo avviso e poi uno ogni 10."""
        missing = [key for key in _KEYS if state.get(key) is None]
        if not missing:
            return
        texts = [(item[0] if isinstance(item, (tuple, list)) and item else item)
                 for item in (state.get("raw_texts") or [])][:_RAW_TEXT_LIMIT]
        names = {"saldo": "saldo", "posta": "puntata"}
        self._warn("ocr_raw", "OCR testo grezzo: %s -> saldo=%s puntata=%s: parsing %s fallito",
                   texts, state.get("saldo"), state.get("posta"), " e ".join(names[key] for key in missing))

    def _retry_ocr(self) -> Optional[dict]:
        """Seconda lettura OCR su uno screenshot NUOVO; lo stato se e' completo, altrimenti None."""
        if self._capture is None:
            return None
        try:
            path = self._capture()
        except Exception as exc:
            logger.warning("seconda lettura OCR: cattura non riuscita (%s).", exc)
            return None
        if not path:
            return None
        second = self._ocr_state(path)
        return second if not self._incomplete(second) else None

    def _reprobe_ocr(self, screenshot_path: str) -> dict:
        """In modo 'OCR inutile': la prima lettura puo' essere caduta su una schermata senza importi (caricamento).
        Ogni _REPROBE_EVERY letture si legge anche con l'OCR; dopo _REPROBE_NEEDED controlli in cui legge saldo e
        puntata concordi con la visione, si torna all'OCR."""
        state = self._ocr_state(screenshot_path)
        self._log_raw_ocr(state)
        self._count_vision("ocr_inutile")
        seen = self._vision_read(screenshot_path)
        agrees = all(state.get(key) is not None and seen.get(key) is not None
                     and _within(state[key], seen[key], self.tolerance) for key in _KEYS)
        self._probe_agreements = self._probe_agreements + 1 if agrees else 0
        if self._probe_agreements >= _REPROBE_NEEDED:
            self._ocr_useless_for_amounts = False
            self._vision_fields.clear()
            self._probe_agreements = 0
            logger.info("OCR di nuovo affidabile: legge saldo e puntata e concorda con la visione da %d controlli: "
                        "torno all'OCR (niente piu' chiamate di visione).", _REPROBE_NEEDED)
        return self._combine(state, seen)

    def _track_agreement(self, key: str, ocr_value: Optional[float], vision_value: Optional[float]) -> None:
        """Campo affidato alla visione: se l'OCR lo legge e concorda per _RELEASE_AFTER_AGREEMENTS letture di fila,
        l'OCR basta (la decisione presa alla prima lettura non vale per sempre)."""
        if key not in self._vision_fields:
            return
        if ocr_value is not None and vision_value is not None and _within(ocr_value, vision_value, self.tolerance):
            self._agreements[key] += 1
        else:
            self._agreements[key] = 0
        if self._agreements[key] >= _RELEASE_AFTER_AGREEMENTS:
            self._vision_fields.discard(key)
            self._agreements[key] = 0
            logger.info("'%s': OCR e visione concordano da %d letture: torno all'OCR per questo campo "
                        "(niente piu' chiamate di visione).", key, _RELEASE_AFTER_AGREEMENTS)

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
                if ocr_value is not None:
                    self._trusted[key] = ocr_value   # unica fonte plausibile (la visione e' muta o era un jackpot)
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
            second["read"] = self._vision_read(path) if path else None
        if second["read"] is None:
            return None
        again = second["read"].get(key)
        # Identico: un contatore che sale di pochi centesimi e' 'quasi uguale' ma non e' il credito.
        return again is not None and abs(again - first_value) < 0.005

    @staticmethod
    def _sane(merged: dict) -> dict:
        """Una puntata che non e' minore del saldo e' un errore di lettura (credito letto come puntata)."""
        bet, balance = merged.get("posta"), merged.get("saldo")
        if bet is not None and bet > MAX_BET_VALUE:
            logger.warning("puntata %s oltre %s: e' un jackpot o un saldo, non una puntata: la scarto.", bet, MAX_BET_VALUE)
            merged = dict(merged)
            merged["posta"] = bet = None
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
                self._track_agreement(key, ocr_value, vision_value)
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
