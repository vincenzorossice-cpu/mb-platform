import io
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.amount_format import find_amount_run
from orchestrator.core.result import Result

logger = logging.getLogger(__name__)

# Oltre questo importo non e' una puntata: sono jackpot (milioni, migliaia) o saldi. Le puntate vanno da centesimi a
# decine di euro.
MAX_BET_VALUE = 1000.0
MIN_BET_VALUE = 0.01
MAX_BALANCE_VALUE = 100000.0   # in demo i saldi vanno da 0 a 100.000; sopra sono jackpot (milioni)
BALANCE_JUMP_RATIO = 10.0      # un saldo che salta a piu' di 10x il precedente non e' un saldo (e' un contatore jackpot)
_AMBIGUOUS_MIN_VALUES = 3   # tre o piu' importi plausibili vicino alle etichette: e' una griglia di scelte, non LA puntata


def choose_bet(candidates: list, label_count: int = 1, last_tapped_bet: Optional[float] = None) -> Optional[float]:
    """La puntata tra importi plausibili (MIN_BET_VALUE..MAX_BET_VALUE, gia' filtrati). Un solo valore distinto = quello.
    Piu' valori con piu' etichette, o una riga di 3+ valori, sono una GRIGLIA DI SCELTE (0.10 0.20 0.30...): non si
    sa quale e' selezionata e non si indovina; l'unica prova e' il valore appena toccato, se tra i candidati."""
    values = sorted({round(float(v), 2) for v in candidates if MIN_BET_VALUE <= float(v) <= MAX_BET_VALUE})
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ambiguous = label_count >= 2 or len(values) >= _AMBIGUOUS_MIN_VALUES
    if not ambiguous:
        return None if last_tapped_bet is None else (
            round(float(last_tapped_bet), 2) if round(float(last_tapped_bet), 2) in values else None)
    if last_tapped_bet is not None and round(float(last_tapped_bet), 2) in values:
        return round(float(last_tapped_bet), 2)
    return None


def is_jackpot_balance(value: float, last_saldo: Optional[float] = None) -> bool:
    """True se `value` non puo' essere il saldo: oltre MAX_BALANCE_VALUE quando il precedente era sotto (o non c'e'), o
    oltre 10x il saldo precedente."""
    if value is None:
        return False
    if last_saldo is not None and last_saldo > 0 and value > BALANCE_JUMP_RATIO * last_saldo:
        return True
    return value > MAX_BALANCE_VALUE and (last_saldo is None or last_saldo < MAX_BALANCE_VALUE)


def parse_saldo(ocr_texts: list, last_saldo: Optional[float] = None) -> Optional[float]:
    """Saldo dai soli testi OCR: ha la priorita' il primo importo DOPO l'etichetta (SALDO/BALANCE/CREDIT...); i jackpot
    (contatori enormi in alto) non sono mai il saldo. Senza etichetta, tra i candidati plausibili quello piu' vicino al
    saldo precedente (o l'unico). Solo jackpot e un saldo precedente noto: quello."""
    texts = [str(t) for t in (ocr_texts or [])]

    def amount(text: str) -> Optional[float]:
        run = find_amount_run(text)
        try:
            return float(run) if run is not None else None
        except ValueError:
            return None

    labels = ("SALDO", "BALANCE", "CREDIT")
    for index, text in enumerate(texts):
        if any(label in text.upper() for label in labels):
            inline = amount(text)
            if inline is not None and not is_jackpot_balance(inline, last_saldo):
                return inline
            for after in texts[index + 1:]:
                value = amount(after)
                if value is not None and value >= 0 and not is_jackpot_balance(value, last_saldo):
                    return value
            break
    candidates = []
    for text in texts:
        value = amount(text)
        if value is not None and value >= 0 and not is_jackpot_balance(value, last_saldo):
            candidates.append(value)
    if candidates:
        if last_saldo is not None:
            return min(candidates, key=lambda v: abs(v - last_saldo))
        return candidates[0] if len(set(candidates)) == 1 else None
    return last_saldo


def parse_bet(ocr_texts: list, last_tapped_bet: Optional[float] = None) -> Optional[float]:
    """Puntata dai soli testi OCR (senza posizioni): esclude i jackpot e i saldi (> MAX_BET_VALUE) e non indovina tra
    le celle di una griglia. `label_count` = quante volte compare un'etichetta di puntata."""
    labels = 0
    candidates = []
    for text in ocr_texts or []:
        upper = str(text).upper()
        if any(word in upper for word in SlotOCRReader._POSTA_LABELS):
            labels += 1
        run = find_amount_run(str(text))
        if run is None:
            continue
        try:
            value = float(run)
        except ValueError:
            continue
        if MIN_BET_VALUE <= value <= MAX_BET_VALUE and not any(
                word in upper for word in SlotOCRReader._SALDO_LABELS + SlotOCRReader._VINCITA_LABELS):
            candidates.append(value)
    return choose_bet(candidates, labels, last_tapped_bet)


@dataclass
class OCRBox:
    """Rilevazione OCR con bounding box completo (non solo il punto top-left
    di read_slot_state's raw_texts)."""
    text: str
    bbox: tuple  # (x1, y1, x2, y2)
    confidence: float


_LEFT_GROUP = re.compile(r"^\d{1,3}(?:[ .,]\d{3})*$")          # "50", "6 405"
_RIGHT_GROUP = re.compile(r"^\d{3}(?:[.,]\d{1,2})?$")           # "000.00", "921.88"


def _merge_grouped_numbers(detections: list) -> list:
    """EasyOCR spesso divide "50 000.00" in due caselle ("50" e "000.00"): quando una casella e' l'inizio di
    un importo con le migliaia separate da uno spazio e la successiva, sulla stessa riga e subito accanto,
    e' un gruppo di 3 cifre (con eventuale decimale), le si unisce in "50 000.00". Cosi' l'importo si legge
    intero senza chiedere aiuto alla visione. Formato dei risultati invariato."""
    merged = []
    for bbox, text, conf in sorted(detections, key=lambda d: (round(min(p[1] for p in d[0]) / 20), min(p[0] for p in d[0]))):
        if merged:
            prev_bbox, prev_text, prev_conf = merged[-1]
            px1, py1 = min(p[0] for p in prev_bbox), min(p[1] for p in prev_bbox)
            px2, py2 = max(p[0] for p in prev_bbox), max(p[1] for p in prev_bbox)
            x1, y1 = min(p[0] for p in bbox), min(p[1] for p in bbox)
            x2, y2 = max(p[0] for p in bbox), max(p[1] for p in bbox)
            height = max(py2 - py1, y2 - y1, 1)
            same_row = abs((py1 + py2) / 2 - (y1 + y2) / 2) < height * 0.5
            close = 0 <= x1 - px2 <= height * 1.2
            if (
                same_row and close and _LEFT_GROUP.match(prev_text.strip()) and _RIGHT_GROUP.match(text.strip())
            ):
                joined = [[min(px1, x1), min(py1, y1)], [max(px2, x2), min(py1, y1)],
                          [max(px2, x2), max(py2, y2)], [min(px1, x1), max(py2, y2)]]
                merged[-1] = (joined, f"{prev_text.strip()} {text.strip()}", min(prev_conf, conf))
                continue
        merged.append((bbox, text, conf))
    return merged


class SlotOCRReader:
    """Legge saldo e stato della slot via EasyOCR su screenshot (Canvas/WebGL,
    illeggibile da uiautomator). Lazy init: il primo readtext e' lento perche'
    carica i pesi del modello, poi il Reader resta cache in self._ocr."""

    _SALDO_LABELS = ("SALDO", "DEMO", "MODALIT", "BALANCE", "CREDITO", "CREDIT")
    _POSTA_LABELS = ("POSTA", "PUNTATA", "BET", "STAKE")
    _VINCITA_LABELS = ("VINCITA", "WIN")
    # "XITER" rimosso: e' il nome del bottone feature/bonus-buy di Wazdan
    # ("X-iter"), un elemento distinto dal tasto spin, non un suo sinonimo.
    _SPIN_LABELS = ("SPIN", "GIRA")
    _ALL_AMOUNT_LABELS = _SALDO_LABELS + _POSTA_LABELS + _VINCITA_LABELS
    _LABEL_Y_TOLERANCE = 50
    _MIN_OCR_CONFIDENCE = 0.3

    _DEFAULT_REMOTE_TIMEOUT = 20.0

    _DEFAULT_REMOTE_MIN_AREA = 0.3

    def __init__(
        self, ocr_url: Optional[str] = None, ocr_timeout: Optional[float] = None,
        remote_min_area: Optional[float] = None,
    ):
        """ocr_url (o la variabile OCR_URL): server OCR remoto (GPU, es. RunPod) con POST /ocr. Se impostato
        EasyOCR non viene mai caricato in locale; senza, comportamento storico (EasyOCR su CPU)."""
        url = (ocr_url or os.environ.get("OCR_URL") or "").strip().rstrip("/")
        if url and not url.endswith("/ocr"):
            url += "/ocr"
        self._ocr_url = url or None
        self.last_known_bet: Optional[float] = None    # ultima puntata letta (o impostata) in questa sessione
        self._bet_fallback_logs = 0
        try:
            self._ocr_timeout = float(ocr_timeout or os.environ.get("OCR_TIMEOUT") or self._DEFAULT_REMOTE_TIMEOUT)
        except ValueError:
            self._ocr_timeout = self._DEFAULT_REMOTE_TIMEOUT
        # Sotto questa frazione dello schermo si legge in locale (EasyOCR CPU), sopra si usa il server. Con OCR_URL il
        # default e' 0: tutte le letture vanno al server, anche in un singolo telefono (la fascia di saldo/puntata in
        # locale costava 1-2 s e teneva la CPU occupata). Una soglia esplicita (argomento o OCR_REMOTE_MIN_AREA, es. 0.3
        # per il vecchio comportamento) vale sempre. Senza OCR_URL e' tutto locale e la soglia non ha effetto.
        default_area = 0.0 if self._ocr_url is not None else self._DEFAULT_REMOTE_MIN_AREA
        try:
            self._remote_min_area = float(
                remote_min_area if remote_min_area is not None
                else os.environ.get("OCR_REMOTE_MIN_AREA") or default_area
            )
        except ValueError:
            self._remote_min_area = default_area
        self.stats = {"calls": 0, "seconds": 0.0, "remote_calls": 0}   # letture fatte e secondi spesi
        if self._ocr_url is None:
            logger.info("SlotOCRReader: OCR locale (EasyOCR CPU)")
        elif self._remote_min_area <= 0:
            logger.info("SlotOCRReader: OCR remoto attivo (RunPod), tutte le letture remote")
        else:
            logger.info("SlotOCRReader: OCR remoto attivo (RunPod); letture piccole (< %.0f%% dello schermo) in locale: "
                        "la CPU locale resta in uso", self._remote_min_area * 100)
        self._ocr = None
        self._last_screenshot_path = None
        self._last_raw_results = None
        self._region = None

    @property
    def region(self) -> Optional[tuple]:
        """La regione corrente (x1, y1, x2, y2) o None = schermo intero."""
        return self._region

    def set_region(self, region: Optional[tuple]) -> None:
        """Limita l'OCR a (x1, y1, x2, y2) in pixel assoluti (None = schermo
        intero). Le coordinate restituite restano assolute."""
        self._region = tuple(region) if region else None
        self._last_screenshot_path = None
        self._last_raw_results = None

    @property
    def remote(self) -> bool:
        """True se l'OCR passa dal server remoto."""
        return self._ocr_url is not None

    def _get_ocr(self):
        if self._ocr is None:
            import easyocr
            self._ocr = easyocr.Reader(['it', 'en'], gpu=False)
        return self._ocr

    def _get_raw_detections(self, screenshot_path: str):
        """Cache di un solo elemento: read_slot_state e read_text_boxes sono
        tipicamente chiamati in sequenza sullo STESSO screenshot (costruzione
        di un contesto) - senza questa cache l'OCR (il vero collo di
        bottiglia su CPU) verrebbe eseguito due volte sulla stessa immagine."""
        if screenshot_path == self._last_screenshot_path and self._last_raw_results is not None:
            return self._last_raw_results
        started = time.monotonic()
        results = None
        remote = False
        if self._ocr_url is not None and self._use_remote(screenshot_path):
            results = self._readtext_remote(screenshot_path)
            remote = results is not None
            if results is None:
                logger.warning("OCR remoto non disponibile: per questa lettura uso l'EasyOCR locale.")
        if results is None:
            try:
                results = self._readtext_local(screenshot_path)
            except ImportError as exc:
                if self._ocr_url is None:
                    raise
                logger.warning("EasyOCR locale non disponibile (%s): lettura vuota.", exc)
                return []           # nessuna cache: la lettura dopo riprova
        elapsed = time.monotonic() - started
        self.stats["calls"] += 1
        self.stats["seconds"] += elapsed
        self.stats["remote_calls"] += 1 if remote else 0
        if not remote:
            logger.info("OCR locale: %d testi in %.1f s", len(results), elapsed)
        self._last_screenshot_path = screenshot_path
        self._last_raw_results = results
        return results

    def _use_remote(self, screenshot_path: str) -> bool:
        """Remoto per le letture a schermo intero o comunque grandi; locale per la fascia di saldo e puntata."""
        if self._region is None:
            return True
        try:
            from PIL import Image
            with Image.open(screenshot_path) as image:
                total = image.width * image.height
        except OSError:
            return True
        x1, y1, x2, y2 = self._region
        return total > 0 and max(0, x2 - x1) * max(0, y2 - y1) / total >= self._remote_min_area

    def _readtext_local(self, screenshot_path: str):
        ocr = self._get_ocr()
        if self._region is None:
            return ocr.readtext(screenshot_path)
        return self._readtext_in_region(ocr, screenshot_path)

    def _readtext_remote(self, screenshot_path: str) -> Optional[list]:
        """Rilevazioni dal server OCR remoto nello stesso formato di EasyOCR: [(bbox, testo, conf)], con bbox =
        4 punti [x, y] in pixel assoluti. Con una regione si invia solo il ritaglio e si sommano gli offset.
        None se la chiamata fallisce (mai eccezioni: lo stato torna vuoto e chi legge ripiega sulla visione)."""
        import requests
        from PIL import Image

        started = time.monotonic()
        try:
            offset_x = offset_y = 0
            if self._region is None:
                with open(screenshot_path, "rb") as screenshot_file:
                    payload = screenshot_file.read()          # lo screenshot e' gia' un PNG: nessuna ricodifica
            else:
                with Image.open(screenshot_path) as image:
                    x1, y1, x2, y2 = self._region
                    box = (max(0, x1), max(0, y1), min(image.width, x2), min(image.height, y2))
                    buffer = io.BytesIO()
                    image.convert("RGB").crop(box).save(buffer, format="PNG")
                    payload = buffer.getvalue()
                    offset_x, offset_y = box[0], box[1]
            response = requests.post(
                self._ocr_url, files={"file": ("screenshot.png", payload, "image/png")},
                timeout=self._ocr_timeout,
            )
            response.raise_for_status()
            texts = response.json()["texts"]
        except (OSError, ValueError, KeyError, TypeError, requests.RequestException) as exc:
            logger.warning("OCR remoto non disponibile (%s): %s", self._ocr_url, exc)
            return None

        results = []
        for item in texts if isinstance(texts, list) else []:
            try:
                coords = [[int(px) + offset_x, int(py) + offset_y] for px, py in item["coords"]]
                if not coords:
                    continue
                results.append((coords, str(item["text"]), float(item.get("conf", 0.0))))
            except (KeyError, TypeError, ValueError):
                continue          # voce malformata: si salta
        logger.info("OCR remoto: %d testi in %.1f s", len(results), time.monotonic() - started)
        return results

    def _readtext_in_region(self, ocr, screenshot_path: str):
        import numpy as np
        from PIL import Image

        x1, y1, x2, y2 = self._region
        with Image.open(screenshot_path) as img:
            width, height = img.size
            box = (max(0, x1), max(0, y1), min(width, x2), min(height, y2))
            cropped = np.array(img.convert("RGB").crop(box))
        offset_x, offset_y = box[0], box[1]

        results = []
        for bbox, text, conf in ocr.readtext(cropped):
            shifted = [[int(px) + offset_x, int(py) + offset_y] for px, py in bbox]
            results.append((shifted, text, conf))
        return results

    def read_slot_state(self, screenshot_path: str) -> dict:
        """Legge lo stato della slot da uno screenshot.

        Returns:
        {
            "saldo": float or None,
            "posta": float or None,
            "vincita": float or None,
            "spin_button_coords": (x, y) or None,
            "is_active": bool,  # True se "SCOMMETTI ATTIVO"
            "raw_texts": [(text, coords, confidence), ...]
        }
        """
        results = _merge_grouped_numbers(self._get_raw_detections(screenshot_path))

        state = {
            "saldo": None,
            "posta": None,
            "vincita": None,
            "spin_button_coords": None,
            "is_active": False,
            "raw_texts": [],
        }

        label_coords = {"saldo": None, "posta": None, "vincita": None}
        posta_labels = []   # TUTTE le etichette di puntata: una griglia di scelte ne ha una per colonna
        resolved = {"saldo": False, "posta": False, "vincita": False}
        detections = []  # (text_upper, x, y) per la passata di fallback

        for detection in results:
            bbox = detection[0]
            text = detection[1].strip()
            conf = detection[2]

            if conf < self._MIN_OCR_CONFIDENCE:
                continue

            x = int(bbox[0][0])
            y = int(bbox[0][1])
            state["raw_texts"].append((text, (x, y), conf))

            text_upper = text.upper()
            detections.append((text_upper, x, y))

            if any(kw in text_upper for kw in self._SALDO_LABELS):
                label_coords["saldo"] = (x, y)
            if any(kw in text_upper for kw in self._POSTA_LABELS):
                label_coords["posta"] = (x, y)
                posta_labels.append((x, y))
            if any(kw in text_upper for kw in self._VINCITA_LABELS):
                label_coords["vincita"] = (x, y)

            if any(kw in text_upper for kw in self._SPIN_LABELS) and "ATTIVO" not in text_upper:
                state["spin_button_coords"] = (x + 50, y + 20)  # centro approssimativo

            if "ATTIVO" in text_upper and "SCOMMETTI" in text_upper:
                state["is_active"] = "NON" not in text_upper

            # Importo nella STESSA stringa della label: risolvi subito, cosi'
            # un'altra label sulla stessa riga (y simile) non puo' piu' rubarlo.
            amount = self._extract_amount(text)
            if amount is not None:
                if any(kw in text_upper for kw in self._SALDO_LABELS):
                    state["saldo"] = amount
                    resolved["saldo"] = True
                elif any(kw in text_upper for kw in self._POSTA_LABELS):
                    if MIN_BET_VALUE <= amount <= MAX_BET_VALUE:      # un jackpot non e' mai una puntata
                        state["posta"] = amount
                    resolved["posta"] = True
                elif any(kw in text_upper for kw in self._VINCITA_LABELS):
                    state["vincita"] = amount
                    resolved["vincita"] = True

        # Fallback: label senza importo nella propria stringa -> cerca
        # l'elemento numerico piu' vicino (stessa riga, y +-50px; a parita'
        # di riga vince quello piu' vicino in x).
        used = set()  # numeri gia' assegnati a un campo: lo stesso numero non puo' essere anche un altro campo
        for key in ("saldo", "posta", "vincita"):
            if resolved[key] or label_coords[key] is None:
                continue
            labels = posta_labels if key == "posta" else [label_coords[key]]
            found = []   # (importo, x, y, distanza_in_x_dalla_etichetta_piu_vicina)
            for text_upper, x, y in detections:
                if any(kw in text_upper for kw in self._ALL_AMOUNT_LABELS):
                    continue  # non riusare il testo di un'altra label come importo
                if (x, y) in used:
                    continue
                near = [abs(x - lx) for lx, ly in labels if abs(y - ly) < self._LABEL_Y_TOLERANCE]
                if not near:
                    continue
                amount = self._extract_amount(text_upper)
                if amount is None or (key == "posta" and not MIN_BET_VALUE <= amount <= MAX_BET_VALUE):
                    continue  # un jackpot o un saldo non sono mai la puntata
                found.append((amount, x, y, min(near)))
            if not found:
                continue
            if key == "posta":
                values = {round(item[0], 2) for item in found}
                if len(values) >= 2 and (len(posta_labels) >= 2 or len(values) >= _AMBIGUOUS_MIN_VALUES):
                    # Una griglia di scelte (0.10 0.20 0.30... con un'etichetta per colonna): non si sa quale e'
                    # selezionata, e scegliere "la piu' vicina all'ultima etichetta" sarebbe a caso.
                    logger.info("OCR: %d valori di puntata (%s) e %d etichette: griglia di scelte, non indovino.",
                                len(values), sorted(values), len(posta_labels))
                    continue
            amount, x, y, _ = min(found, key=lambda item: item[3])
            state[key] = amount
            used.add((x, y))

        if state["posta"] is not None:
            self.last_known_bet = state["posta"]
        elif posta_labels and self.last_known_bet is not None:
            # Griglia di scelte (o cella non distinguibile): l'OCR non sa quale e' selezionata, la puntata e' quella che
            # si sa. Solo se c'e' un'etichetta di puntata a schermo: senza, non e' una schermata di gioco.
            state["posta"] = self.last_known_bet
            self._bet_fallback_logs += 1
            if self._bet_fallback_logs % 20 == 1:
                logger.warning("Puntata non leggibile via OCR, uso ultimo valore noto: %.2f", self.last_known_bet)
        return state

    def note_bet(self, value) -> None:
        """La puntata e' stata impostata/verificata da chi chiama: vale come ultimo valore noto."""
        try:
            self.last_known_bet = float(value) if value is not None else self.last_known_bet
        except (TypeError, ValueError):
            pass

    def read_text_boxes(self, screenshot_path: str) -> Result[list]:
        """Come read_slot_state, ma espone il bounding box completo (non solo
        il punto top-left) di ogni rilevazione OCR, per i locator che devono
        ragionare sull'area occupata da un elemento (es. SpatialHeuristicStrategy)."""
        try:
            results = self._get_raw_detections(screenshot_path)
        except Exception as exc:
            return Result.fail(f"OCR error: {exc}", "OCR_ERROR")

        boxes = []
        for detection in results:
            bbox = detection[0]
            text = detection[1].strip()
            conf = detection[2]
            if conf < self._MIN_OCR_CONFIDENCE:
                continue
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            boxes.append(OCRBox(
                text=text,
                bbox=(int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))),
                confidence=conf,
            ))
        return Result.ok(boxes)

    def _extract_amount(self, text: str) -> Optional[float]:
        """Estrae un importo numerico da un testo OCR (es. '114,36 €', '5,000.00',
        '50.000' = cinquantamila). Le regole dei separatori stanno in
        find_amount_run (anche le migliaia separate da uno spazio, '49 999.90')."""
        run = find_amount_run(text)
        if run is None:
            return None
        return float(run)
