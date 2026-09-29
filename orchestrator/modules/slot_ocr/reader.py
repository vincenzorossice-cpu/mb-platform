import logging
import re
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result

logger = logging.getLogger(__name__)


@dataclass
class OCRBox:
    """Rilevazione OCR con bounding box completo (non solo il punto top-left
    di read_slot_state's raw_texts)."""
    text: str
    bbox: tuple  # (x1, y1, x2, y2)
    confidence: float


class SlotOCRReader:
    """Legge saldo e stato della slot via EasyOCR su screenshot (Canvas/WebGL,
    illeggibile da uiautomator). Lazy init: il primo readtext e' lento perche'
    carica i pesi del modello, poi il Reader resta cache in self._ocr."""

    _SALDO_LABELS = ("SALDO", "DEMO", "MODALIT", "BALANCE")
    _POSTA_LABELS = ("POSTA", "PUNTATA", "BET", "STAKE")
    _VINCITA_LABELS = ("VINCITA", "WIN")
    # "XITER" rimosso: e' il nome del bottone feature/bonus-buy di Wazdan
    # ("X-iter"), un elemento distinto dal tasto spin, non un suo sinonimo.
    _SPIN_LABELS = ("SPIN", "GIRA")
    _ALL_AMOUNT_LABELS = _SALDO_LABELS + _POSTA_LABELS + _VINCITA_LABELS
    _LABEL_Y_TOLERANCE = 50
    _MIN_OCR_CONFIDENCE = 0.3

    def __init__(self):
        self._ocr = None
        self._last_screenshot_path = None
        self._last_raw_results = None
        self._region = None

    def set_region(self, region: Optional[tuple]) -> None:
        """Limita l'OCR a (x1, y1, x2, y2) in pixel assoluti (None = schermo
        intero). Le coordinate restituite restano assolute."""
        self._region = tuple(region) if region else None
        self._last_screenshot_path = None
        self._last_raw_results = None

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
        ocr = self._get_ocr()
        if self._region is None:
            results = ocr.readtext(screenshot_path)
        else:
            results = self._readtext_in_region(ocr, screenshot_path)
        self._last_screenshot_path = screenshot_path
        self._last_raw_results = results
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
        results = self._get_raw_detections(screenshot_path)

        state = {
            "saldo": None,
            "posta": None,
            "vincita": None,
            "spin_button_coords": None,
            "is_active": False,
            "raw_texts": [],
        }

        label_coords = {"saldo": None, "posta": None, "vincita": None}
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
                    state["posta"] = amount
                    resolved["posta"] = True
                elif any(kw in text_upper for kw in self._VINCITA_LABELS):
                    state["vincita"] = amount
                    resolved["vincita"] = True

        # Fallback: label senza importo nella propria stringa -> cerca
        # l'elemento numerico piu' vicino (stessa riga, y +-50px; a parita'
        # di riga vince quello piu' vicino in x).
        for key in ("saldo", "posta", "vincita"):
            if resolved[key] or label_coords[key] is None:
                continue
            label_x, label_y = label_coords[key]
            best_amount, best_dist = None, None
            for text_upper, x, y in detections:
                if abs(y - label_y) >= self._LABEL_Y_TOLERANCE:
                    continue
                if any(kw in text_upper for kw in self._ALL_AMOUNT_LABELS):
                    continue  # non riusare il testo di un'altra label come importo
                amount = self._extract_amount(text_upper)
                if amount is None:
                    continue
                dist = abs(x - label_x)
                if best_dist is None or dist < best_dist:
                    best_amount, best_dist = amount, dist
            if best_amount is not None:
                state[key] = best_amount

        return state

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
        """Estrae un importo numerico da un testo OCR (es. '114,36 €', '5,000.00')."""
        match = re.search(r'(\d[\d.,]*\d)', text)
        if not match: return None
        clean = match.group(1)

        # Determina formato in base all'ultimo separatore
        last_comma = clean.rfind(',')
        last_dot = clean.rfind('.')

        if last_comma > last_dot:
            # Formato europeo: 1.234,56 o 114,36
            return float(clean.replace('.', '').replace(',', '.'))
        elif last_dot > last_comma:
            # Formato americano: 5,000.00 o 1.50
            return float(clean.replace(',', ''))
        elif ',' in clean:
            # Solo virgola: europeo 114,36
            return float(clean.replace(',', '.'))

        # Solo punto o nessuno: intero o americano 1.50
        return float(clean)
