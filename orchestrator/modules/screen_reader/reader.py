import subprocess
import time
import xml.etree.ElementTree as ET
import re
import os
import tempfile
from typing import Optional
from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import (
    ScreenReaderInterface,
    ScreenContent,
    UIElement,
    BalanceReading,
    BetStatus,
    MovementReading,
)

# FIX-1: etichette generiche di saldo, in ordine di priorita' di ricerca
_BALANCE_LABELS = ("saldo", "balance", "disponibile", "wallet", "conto", "fondo", "credito")

# FIX-1: euristiche per-book usate solo se nessuna etichetta generica viene trovata
_BOOK_LABEL_HINTS = {
    "betfair": ("wallet balance", "saldo"),
    "bet365": ("balance",),
}

# FIX-1/FIX-2: importo in formato italiano (con o senza separatore di migliaia) o inglese,
# con simbolo euro e segno/parentesi opzionali. Alternative ordinate dalla piu' alla meno specifica.
_AMOUNT_RE = re.compile(
    r"[-+]?\(?\s*€?\s*"
    r"(?:\d{1,3}(?:\.\d{3})+,\d{2}"   # italiano con migliaia: 1.234,56
    r"|\d+,\d{2}"                      # italiano senza migliaia: 150,75
    r"|\d+\.\d{2})"                    # inglese: 123.45
    r"\s*€?\)?"
)

# FIX-BETFAIR-STANDALONE: elemento che contiene SOLO un importo con simbolo euro
# (nessun'altra parola), usato come saldo quando non esiste un'etichetta associata
# (es. Betfair mostra il saldo come "â‚¬1,194.57" standalone). "â‚¬" e' la sequenza
# mojibake reale di "€" nei dump con encoding errato.
_STANDALONE_EURO_RE = re.compile(
    r"^\s*[-+]?\(?\s*(?:€|â‚¬|EUR|eur)\s*"
    r"(?:\d{1,3}(?:\.\d{3})+,\d{2}"   # italiano con migliaia: 1.234,56
    r"|\d{1,3}(?:,\d{3})+\.\d{2}"     # inglese con migliaia: 1,194.57
    r"|\d+,\d{2}"                      # solo virgola decimale: 150,75
    r"|\d+\.\d{2})"                    # solo punto decimale: 123.45
    r"\s*\)?\s*$"
)

# FIX-4: etichette per scommesse aperte, bonus reale (prelevabile) e wagering bonus
_OPEN_BETS_LABELS = ("scommesse aperte", "open bets", "scommesse in corso", "puntate aperte", "giocate aperte")
_REAL_BONUS_LABELS = ("bonus reale", "real bonus", "bonus saldo", "bonus disponibile")
_REAL_BONUS_EXCLUDE_LABELS = ("wagering", "rollover", "bloccato")
_WAGERING_BONUS_LABELS = ("wagering", "rollover", "bonus bloccato", "bonus in rollover")

# FIX-A: frasi che indicano assenza di scommesse aperte, da NON confondere con "unknown"
_NO_BETS_PHRASES = ("non sono presenti scommesse", "no open bets", "nessuna scommessa", "no bets found")


class ScreenReader(ScreenReaderInterface):
    def __init__(
        self,
        ocr_confidence_threshold: float = 0.85,
        adb_timeout: int = 10,
        screenshot_dir: str = None,
    ):
        self.ocr_confidence_threshold = ocr_confidence_threshold
        self.adb_timeout = adb_timeout
        self.screenshot_dir = screenshot_dir or tempfile.gettempdir()
        self._ocr_engine = None
        self._screen_cache = {}  # FIX-B: {(device_id, screen_type): (Result, timestamp)}

    def read_screen(
        self, device_id: str, screen_type: Optional[str] = None,
        use_cache: bool = False, cache_ttl: int = 5,
    ) -> Result[ScreenContent]:
        cache_key = (device_id, screen_type)  # FIX-B: chiave per device+tipo, mai uno slot globale
        if use_cache:
            cached = self._screen_cache.get(cache_key)
            if cached is not None:
                cached_result, cached_at = cached
                if time.time() - cached_at < cache_ttl:
                    return cached_result

        xml_result = self._uiautomator_dump(device_id)
        if xml_result.success:
            elements = self._parse_xml(xml_result.data)
            if elements:
                detected_type = self._classify_screen(elements) if not screen_type else screen_type
                confidence = self._validate_screen_confidence(elements, detected_type)  # FIX-3
                result = Result.ok(ScreenContent(
                    elements=elements,
                    screen_type=detected_type,
                    confidence=confidence,
                    method="uiautomator",
                    screenshot_path=None,
                ))
            else:
                result = self._ocr_fallback(device_id, screen_type)
        elif xml_result.error_code == "ADB_TIMEOUT":
            result = Result.fail("ADB timeout during uiautomator dump", "ADB_TIMEOUT")
        elif xml_result.error_code == "DEVICE_OFFLINE":
            result = Result.fail("Device offline", "DEVICE_OFFLINE")
        else:
            result = self._ocr_fallback(device_id, screen_type)

        if use_cache and result.success:  # FIX-B: non cachare i fallimenti (potrebbero essere transitori)
            self._screen_cache[cache_key] = (result, time.time())

        return result

    def find_element(self, device_id: str, hint: str, field: str = "text") -> Result[UIElement]:
        screen = self.read_screen(device_id)
        if not screen.success:
            return Result.fail(f"Cannot read screen: {screen.error}", screen.error_code)

        hint_lower = hint.lower()
        for elem in screen.data.elements:
            if field == "text":
                if hint_lower in elem.text.lower():
                    return Result.ok(elem)
            elif field == "resource_id":
                if elem.resource_id and hint_lower in elem.resource_id.lower():
                    return Result.ok(elem)

        return Result.fail(f"Element '{hint}' not found", "NO_ELEMENTS_FOUND")

    def read_balance(self, device_id: str, book: str) -> Result[BalanceReading]:
        screen = self.read_screen(device_id, screen_type="balance_page", use_cache=True)  # FIX-B
        if not screen.success:
            return Result.fail(f"Cannot read balance: {screen.error}", screen.error_code)

        elements = screen.data.elements
        texts = [e.text for e in elements]

        balance = self._extract_balance(texts, book)

        if balance is None:
            if screen.data.method == "uiautomator":
                ocr_screen = self._ocr_fallback(device_id, "balance_page")
                if ocr_screen.success:
                    texts = [e.text for e in ocr_screen.data.elements]
                    balance = self._extract_balance(texts, book)

        if balance is None:
            return Result.fail(f"Balance not found for {book}", "SCREEN_NOT_RECOGNIZED")

        return Result.ok(BalanceReading(
            raw_balance=balance["raw"],
            open_bets=balance.get("open_bets", 0.0),
            real_bonus=balance.get("real_bonus", 0.0),
            wagering_bonus=balance.get("wagering_bonus"),
            read_method=screen.data.method,
            confidence=screen.data.confidence,
        ))

    def read_bet_status(self, device_id: str, betfair: bool = False) -> Result[BetStatus]:
        screen = self.read_screen(device_id, screen_type="my_bets", use_cache=True)  # FIX-B
        if not screen.success:
            return Result.fail(f"Cannot read bet status: {screen.error}", screen.error_code)

        texts = [e.text for e in screen.data.elements]
        texts_lower = " ".join(texts).lower()

        if any(p in texts_lower for p in _NO_BETS_PHRASES):  # FIX-A
            return Result.ok(BetStatus(status="not_found", lay_odds=None, confidence=1.0))

        if "matched" in texts_lower:
            status = "matched"
        elif "unsettled" in texts_lower or "in corso" in texts_lower or "pending" in texts_lower:
            status = "unsettled"
        elif "vinta" in texts_lower or "won" in texts_lower:
            status = "settled_won"
        elif "persa" in texts_lower or "lost" in texts_lower:
            status = "settled_lost"
        elif not texts:
            status = "not_found"
        else:
            status = "unknown"  # FIX-5: schermata non riconosciuta, mai da trattare come "piazzata"

        lay_odds = None
        for t in texts:
            match = re.match(r"^(\d+[.,]\d{2})$", t.strip())  # FIX-20: accetta anche il locale italiano
            if match:
                val = float(match.group(1).replace(",", "."))
                if 1.01 < val < 1000:
                    lay_odds = val
                    break

        return Result.ok(BetStatus(
            status=status,
            lay_odds=lay_odds,
            confidence=screen.data.confidence,
        ))

    def read_movements(self, device_id: str) -> Result[list[MovementReading]]:  # FIX-BASSA-2: rimosso 'app' inutilizzato
        screen = self.read_screen(device_id, screen_type="movements")
        if not screen.success:
            return Result.fail(f"Cannot read movements: {screen.error}", screen.error_code)

        movements = []
        for elem in screen.data.elements:
            amount = self._extract_amount(elem.text)
            if amount is not None:
                direction = "in" if "+" in elem.text else "out"
                movements.append(MovementReading(
                    amount=amount,
                    description=elem.text,
                    date="",
                    direction=direction,
                    confidence=screen.data.confidence,
                ))

        if not movements:
            return Result.fail("No movements found", "NO_ELEMENTS_FOUND")

        return Result.ok(movements)

    # --------------------------------------------------------
    # INTERNAL: ADB + uiautomator
    # --------------------------------------------------------

    def _uiautomator_dump(self, device_id: str) -> Result[str]:
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "uiautomator", "dump", "/sdcard/ui_dump.xml"],
                capture_output=True,
                text=True,
                timeout=self.adb_timeout,
            )

            # FIX-15: controlla il returncode del dump PRIMA di eseguire il cat,
            # invece di sovrascrivere result.stdout e continuare a leggere
            # result.returncode/result.stderr (che restano quelli del dump anche
            # dopo la sovrascrittura, mascherando un eventuale fallimento del cat)
            if result.returncode != 0:
                stderr = result.stderr.lower()
                if "device not found" in stderr or "device offline" in stderr or "no devices" in stderr:
                    return Result.fail(f"Device offline: {device_id}", "DEVICE_OFFLINE")
                if "security exception" in stderr or "flag_secure" in stderr:
                    return Result.fail(f"uiautomator blocked (FLAG_SECURE): {device_id}", "UIAUTOMATOR_BLOCKED")
                return Result.fail(f"uiautomator failed: {result.stderr}", "UIAUTOMATOR_BLOCKED")

            # Leggi il file con cat
            cat_result = subprocess.run(
                ["adb", "-s", device_id, "shell", "cat", "/sdcard/ui_dump.xml"],
                capture_output=True, text=True, timeout=self.adb_timeout,
            )
            if cat_result.returncode != 0:
                # FIX-15: dump ok ma cat fallito, ritenta una volta prima di arrendersi
                cat_result = subprocess.run(
                    ["adb", "-s", device_id, "shell", "cat", "/sdcard/ui_dump.xml"],
                    capture_output=True, text=True, timeout=self.adb_timeout,
                )
                if cat_result.returncode != 0:
                    return Result.fail(f"cat fallito dopo retry: {cat_result.stderr}", "ADB_TIMEOUT")

            xml = cat_result.stdout.strip()
            if not xml or "<hierarchy" not in xml.lower():
                return Result.fail("uiautomator returned empty XML", "UIAUTOMATOR_BLOCKED")

            return Result.ok(xml)

        except subprocess.TimeoutExpired:
            return Result.fail(f"ADB timeout on {device_id}", "ADB_TIMEOUT")
        except FileNotFoundError:
            return Result.fail("ADB not found in PATH", "DEVICE_OFFLINE")
        except Exception as e:
            return Result.fail(f"Unexpected error: {e}", "ADB_ERROR")  # FIX-16

    def _screenshot(self, device_id: str) -> Result[str]:
        try:
            local_path = os.path.join(self.screenshot_dir, f"screen_{device_id}_{int(time.time() * 1000)}.png")  # FIX-BASSA-4
            subprocess.run(
                ["adb", "-s", device_id, "shell", "screencap", "-p", "/sdcard/screen.png"],
                capture_output=True, timeout=self.adb_timeout,
            )
            subprocess.run(
                ["adb", "-s", device_id, "pull", "/sdcard/screen.png", local_path],
                capture_output=True, timeout=self.adb_timeout,
            )
            if not os.path.exists(local_path):
                return Result.fail("Screenshot file not created", "ADB_TIMEOUT")
            return Result.ok(local_path)
        except subprocess.TimeoutExpired:
            return Result.fail("Screenshot timeout", "ADB_TIMEOUT")
        except Exception as e:
            return Result.fail(f"Screenshot error: {e}", "ADB_ERROR")  # FIX-16

    # --------------------------------------------------------
    # INTERNAL: OCR fallback
    # --------------------------------------------------------

    def _ocr_fallback(self, device_id: str, screen_type: Optional[str] = None) -> Result[ScreenContent]:
        screenshot = self._screenshot(device_id)
        if not screenshot.success:
            return Result.fail(f"Screenshot failed: {screenshot.error}", screenshot.error_code)

        try:
            ocr_result = self._run_paddleocr(screenshot.data)
        except Exception as e:
            return Result.fail(f"OCR failed: {e}", "OCR_LOW_CONFIDENCE")

        if not ocr_result:
            return Result.fail("OCR returned no text", "OCR_LOW_CONFIDENCE")

        elements = []
        for item in ocr_result:
            if len(item) >= 2:
                bbox = item[0]
                text = item[1][0] if isinstance(item[1], tuple) else str(item[1])
                conf = item[1][1] if isinstance(item[1], tuple) else 0.5
                bounds = [
                    int(min(p[0] for p in bbox)),
                    int(min(p[1] for p in bbox)),
                    int(max(p[0] for p in bbox)),
                    int(max(p[1] for p in bbox)),
                ]
                elements.append(UIElement(
                    text=text, bounds=bounds, resource_id=None, class_name=None, confidence=round(conf, 3),
                ))

        if not elements:
            return Result.fail("OCR found no text elements", "OCR_LOW_CONFIDENCE")

        avg_conf = sum(e.confidence for e in elements) / len(elements)

        if avg_conf < self.ocr_confidence_threshold:
            return Result.fail(f"OCR confidence too low: {avg_conf:.2f}", "OCR_LOW_CONFIDENCE")

        detected_type = self._classify_screen(elements) if not screen_type else screen_type
        return Result.ok(ScreenContent(
            elements=elements, screen_type=detected_type, confidence=round(avg_conf, 3), method="ocr", screenshot_path=None,
        ))

    def _run_paddleocr(self, image_path: str):
        if self._ocr_engine is None:
            from paddleocr import PaddleOCR
            self._ocr_engine = PaddleOCR(use_angle_cls=True, lang="it")
        result = self._ocr_engine.ocr(image_path, cls=True)
        return result if result else []

    # --------------------------------------------------------
    # INTERNAL: XML parsing
    # --------------------------------------------------------

    def _parse_xml(self, xml_str: str) -> list[UIElement]:
        elements = []
        try:
            xml_clean = xml_str
            idx = xml_clean.find("<?xml")
            if idx == -1:
                idx = xml_clean.find("<hierarchy")
            if idx > 0:
                xml_clean = xml_clean[idx:]

            root = ET.fromstring(xml_clean)
            for node in root.iter():
                text = node.get("text", "").strip()
                resource_id = node.get("resource-id", "").strip()
                bounds_str = node.get("bounds", "")
                if not text and not resource_id:
                    continue
                bounds = self._parse_bounds(bounds_str)
                elements.append(UIElement(
                    text=text, bounds=bounds,
                    resource_id=resource_id if resource_id else None,
                    class_name=node.get("class", "").strip() or None, confidence=1.0,
                ))
        except ET.ParseError as e:
            print(f"Warning: XML parse error: {e}")  # FIX-17: logga invece di ingoiare in silenzio
        except Exception as e:
            print(f"Warning: XML parse error: {e}")  # FIX-17
        return elements

    def _parse_bounds(self, bounds_str: str) -> list[int]:
        if not bounds_str:
            return [0, 0, 0, 0]
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if match:
            return [int(match.group(i)) for i in range(1, 5)]
        return [0, 0, 0, 0]

    # --------------------------------------------------------
    # INTERNAL: Screen classification
    # --------------------------------------------------------

    def _classify_screen(self, elements: list[UIElement]) -> str:
        texts = " ".join(e.text.lower() for e in elements if e.text)
        if any(kw in texts for kw in ["saldo", "balance", "conto", "wallet", "disponibile"]):
            return "balance_page"
        if any(kw in texts for kw in ["le mie scommesse", "my bets", "scommesse aperte", "open bets", "bet history"]):
            return "my_bets"
        if any(kw in texts for kw in ["mercati", "markets", "quota", "odds", "1x2", "risultato"]):
            return "market_page"
        if any(kw in texts for kw in ["movimenti", "transactions", "storico", "history", "bonifico", "deposito"]):
            return "movements"
        # FIX-CLASSIFY-STANDALONE: nessuna etichetta trovata, ma un elemento contiene
        # SOLO un importo con simbolo euro (es. Betfair "â‚¬1,194.57" senza label "Saldo") ->
        # trattalo comunque come balance_page invece di "unknown". Check per-elemento sul
        # testo originale (non sul blob lowercased) perché la regex è ancorata ^...$.
        for e in elements:
            if e.text and _STANDALONE_EURO_RE.match(e.text):
                return "balance_page"
        return "unknown"

    def _validate_screen_confidence(self, elements: list[UIElement], screen_type: str) -> float:
        # FIX-3: il percorso uiautomator non validava mai il contenuto letto — una
        # schermata non riconoscibile veniva comunque etichettata confidence=1.0,
        # bypassando il gate RulesEngine.MIN_CONFIDENCE (0.85)
        texts = " ".join(e.text.lower() for e in elements if e.text)

        if screen_type == "balance_page":
            if any(kw in texts for kw in ("saldo", "balance", "disponibile", "wallet", "conto")):
                if not _AMOUNT_RE.search(texts):
                    return 0.3
                return 1.0
            # FIX-CONFIDENCE-STANDALONE: nessuna etichetta di saldo trovata. Se un elemento
            # è un importo standalone con simbolo euro (es. Betfair "â‚¬1,194.57" senza label
            # "Saldo"), il balance è comunque stato estratto in modo affidabile da
            # _extract_balance -> confidence sufficiente a superare il gate
            # RulesEngine.MIN_CONFIDENCE (0.85), evitando un HITL inutile.
            if any(e.text and _STANDALONE_EURO_RE.match(e.text) for e in elements):
                return 0.85
            return 0.3

        if screen_type == "my_bets":
            if not any(kw in texts for kw in ("scommesse", "bets", "matched", "unsettled")):
                return 0.5
            return 1.0

        return 1.0  # altri screen_type: non validabili

    # --------------------------------------------------------
    # INTERNAL: Balance extraction
    # --------------------------------------------------------

    def _extract_balance(self, texts: list[str], book: str) -> Optional[dict]:
        # FIX-4: estrae raw_balance, open_bets, real_bonus e wagering_bonus in un solo
        # passaggio sugli elementi, invece di restituire open_bets/real_bonus hardcoded a 0
        raw_balance = None
        open_bets = 0.0
        real_bonus = 0.0
        wagering_bonus = None

        for i, t in enumerate(texts):
            t_lower = t.lower()

            # Saldo reale (FIX-1/FIX-2: ancorato a etichetta, mai il primo decimale della pagina)
            if raw_balance is None and any(kw in t_lower for kw in _BALANCE_LABELS):
                raw_balance = self._extract_amount_near(texts, i)

            # Scommesse aperte
            if any(kw in t_lower for kw in _OPEN_BETS_LABELS):
                val = self._extract_amount_near(texts, i)
                if val is not None:
                    open_bets = val

            # Bonus reale (esclude wagering/rollover/bloccato: non e' prelevabile)
            if any(kw in t_lower for kw in _REAL_BONUS_LABELS) and not any(
                kw in t_lower for kw in _REAL_BONUS_EXCLUDE_LABELS
            ):
                val = self._extract_amount_near(texts, i)
                if val is not None:
                    real_bonus = val

            # Wagering bonus
            if any(kw in t_lower for kw in _WAGERING_BONUS_LABELS):
                val = self._extract_amount_near(texts, i)
                if val is not None:
                    wagering_bonus = val

        if raw_balance is None and book:
            # FIX-1: fallback alle euristiche per-book solo se la scansione per
            # etichetta generica non ha trovato nulla (invariato da FIX-1/2)
            book_lower = book.lower()
            full_text = " ".join(texts)
            for book_key, hints in _BOOK_LABEL_HINTS.items():
                if book_key in book_lower:
                    raw_balance = self._find_labeled_amount(full_text, hints)
                    if raw_balance is not None:
                        break

        if raw_balance is None:
            # FIX-BETFAIR-STANDALONE: nessuna etichetta trovata (ne' generica ne' per-book) ->
            # cerca un elemento che sia SOLO un importo con simbolo euro (es. Betfair "â‚¬1,194.57")
            for t in texts:
                match = _STANDALONE_EURO_RE.match(t)
                if match:
                    val = self._clean_italian_amount(match.group(0))
                    if val is not None:
                        raw_balance = val
                        break

        if raw_balance is None:
            # FIX-1: nessuna etichetta trovata -> non indovinare, ritorna None
            return None

        if not (-1_000_000 <= raw_balance <= 1_000_000):
            return None

        return {
            "raw": raw_balance,
            "open_bets": open_bets,
            "real_bonus": real_bonus,
            "wagering_bonus": wagering_bonus,
        }

    def _extract_amount_near(self, texts: list[str], index: int) -> Optional[float]:
        # FIX-4: l'etichetta e il valore sono spesso su elementi UI separati —
        # cerca un importo nell'elemento index, poi index+1, poi index+2
        for i in (index, index + 1, index + 2):
            if i >= len(texts):
                break
            match = _AMOUNT_RE.search(texts[i])
            if match:
                value = self._clean_italian_amount(match.group(0))
                if value is not None:
                    return value
        return None

    def _find_labeled_amount(self, text: str, labels: tuple) -> Optional[float]:
        # FIX-1: cerca ogni etichetta (in ordine di priorita') e prende il primo importo
        # trovato entro 50 caratteri dopo di essa, cioe' il piu' vicino all'etichetta
        text_lower = text.lower()
        for label in labels:
            idx = text_lower.find(label.lower())
            if idx == -1:
                continue
            window_start = idx + len(label)
            window = text[window_start:window_start + 50]
            match = _AMOUNT_RE.search(window)
            if match:
                value = self._clean_italian_amount(match.group(0))
                if value is not None:
                    return value
        return None

    def _clean_italian_amount(self, amount_str: str) -> Optional[float]:
        # FIX-2: normalizza un importo in formato italiano o inglese, con simbolo euro,
        # segno negativo o parentesi, in un float
        s = amount_str.strip()
        s = s.replace("€", "").replace("â‚¬", "").replace("EUR", "").replace("eur", "").strip()

        negative = False
        if s.startswith("(") and s.endswith(")"):
            negative = True
            s = s[1:-1].strip()
        if s.startswith("-"):
            negative = True
            s = s[1:].strip()
        elif s.startswith("+"):
            s = s[1:].strip()

        has_dot = "." in s
        has_comma = "," in s

        if has_dot and has_comma:
            # FIX-BETFAIR-STANDALONE: il separatore che compare per ultimo e' il decimale
            # (euristica standard) - gestisce sia "1.234,56" (IT) che "1,194.57" (EN)
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif has_comma:
            # solo virgola: e' il decimale
            s = s.replace(",", ".")
        elif has_dot:
            # solo punto: se ci sono 2+ gruppi da 3 cifre e' separatore di migliaia,
            # altrimenti (un solo punto con 2 cifre finali) e' gia' decimale
            parts = s.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                s = s.replace(".", "")

        try:
            value = float(s)
        except ValueError:
            return None

        return -value if negative else value

    def _extract_amount(self, text: str) -> Optional[float]:
        match = re.search(r"[-+]?\s*(\d{1,6}[.,]\d{2})", text.replace("€", "").replace("EUR", ""))
        if match:
            amount_str = match.group(1).replace(",", ".")
            try:
                return float(amount_str)
            except ValueError:
                pass
        return None
