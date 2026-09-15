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

    def read_screen(self, device_id: str, screen_type: Optional[str] = None) -> Result[ScreenContent]:
        xml_result = self._uiautomator_dump(device_id)
        if xml_result.success:
            elements = self._parse_xml(xml_result.data)
            if elements:
                detected_type = self._classify_screen(elements) if not screen_type else screen_type
                return Result.ok(ScreenContent(
                    elements=elements,
                    screen_type=detected_type,
                    confidence=1.0,
                    method="uiautomator",
                    screenshot_path=None,
                ))
            else:
                return self._ocr_fallback(device_id, screen_type)

        if xml_result.error_code == "ADB_TIMEOUT":
            return Result.fail("ADB timeout during uiautomator dump", "ADB_TIMEOUT")
        if xml_result.error_code == "DEVICE_OFFLINE":
            return Result.fail("Device offline", "DEVICE_OFFLINE")

        return self._ocr_fallback(device_id, screen_type)

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
        screen = self.read_screen(device_id, screen_type="balance_page")
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
        screen = self.read_screen(device_id, screen_type="my_bets")
        if not screen.success:
            return Result.fail(f"Cannot read bet status: {screen.error}", screen.error_code)

        texts = [e.text for e in screen.data.elements]
        texts_lower = " ".join(texts).lower()

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
            status = "unsettled"

        lay_odds = None
        for t in texts:
            match = re.match(r"^(\d+\.\d{2})$", t.strip())
            if match:
                val = float(match.group(1))
                if 1.01 < val < 1000:
                    lay_odds = val
                    break

        return Result.ok(BetStatus(
            status=status,
            lay_odds=lay_odds,
            confidence=screen.data.confidence,
        ))

    def read_movements(self, device_id: str, app: str) -> Result[list[MovementReading]]:
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

            # Leggi il file con cat
            cat_result = subprocess.run(
                ["adb", "-s", device_id, "shell", "cat", "/sdcard/ui_dump.xml"],
                capture_output=True, text=True, timeout=self.adb_timeout,
            )
            result.stdout = cat_result.stdout
            
            if result.returncode != 0:
                stderr = result.stderr.lower()
                if "device not found" in stderr or "device offline" in stderr or "no devices" in stderr:
                    return Result.fail(f"Device offline: {device_id}", "DEVICE_OFFLINE")
                if "security exception" in stderr or "flag_secure" in stderr:
                    return Result.fail(f"uiautomator blocked (FLAG_SECURE): {device_id}", "UIAUTOMATOR_BLOCKED")
                return Result.fail(f"uiautomator failed: {result.stderr}", "UIAUTOMATOR_BLOCKED")

            xml = result.stdout.strip()
            if not xml or "<hierarchy" not in xml.lower():
                return Result.fail("uiautomator returned empty XML", "UIAUTOMATOR_BLOCKED")

            return Result.ok(xml)

        except subprocess.TimeoutExpired:
            return Result.fail(f"ADB timeout on {device_id}", "ADB_TIMEOUT")
        except FileNotFoundError:
            return Result.fail("ADB not found in PATH", "DEVICE_OFFLINE")
        except Exception as e:
            return Result.fail(f"Unexpected error: {e}", "ADB_TIMEOUT")

    def _screenshot(self, device_id: str) -> Result[str]:
        try:
            local_path = os.path.join(self.screenshot_dir, f"screen_{device_id}_{int(time.time())}.png")
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
            return Result.fail(f"Screenshot error: {e}", "ADB_TIMEOUT")

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
        except ET.ParseError:
            pass
        except Exception:
            pass
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
        return "unknown"

    # --------------------------------------------------------
    # INTERNAL: Balance extraction
    # --------------------------------------------------------

    def _extract_balance(self, texts: list[str], book: str) -> Optional[dict]:
        for t in texts:
            t_clean = t.replace("€", "").replace("EUR", "").strip()
            match = re.search(r"(\d{1,6}[.,]\d{2})", t_clean)
            if match:
                amount_str = match.group(1).replace(",", ".")
                try:
                    amount = float(amount_str)
                    if 0 <= amount <= 1000000:
                        return {"raw": amount, "open_bets": 0.0, "real_bonus": 0.0, "wagering_bonus": None}
                except ValueError:
                    continue
        return None

    def _extract_amount(self, text: str) -> Optional[float]:
        match = re.search(r"[-+]?\s*(\d{1,6}[.,]\d{2})", text.replace("€", "").replace("EUR", ""))
        if match:
            amount_str = match.group(1).replace(",", ".")
            try:
                return float(amount_str)
            except ValueError:
                pass
        return None
