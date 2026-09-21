import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import ScreenReaderInterface
from orchestrator.modules.telegram_acquirer.interface import AcquiredSignal, TelegramAcquirerInterface
from orchestrator.modules.touch_injector.interface import TouchInjectorInterface

logger = logging.getLogger(__name__)

TELEGRAM_PACKAGE = "com.telegram.messenger"

# Emoji delimitatore usate dai canali segnali: la loro presenza in una notifica
# è un'euristica per distinguere un segnale promo da una notifica generica.
SIGNAL_EMOJI = ("📚", "💎", "⏳", "🚨", "✅")

# Placeholder noti per i valori nascosti dietro emoji animate (vedi
# SignalParser.OBSCURED_MARKERS): mappa placeholder -> chiave in emoji_values,
# compilata manualmente da chi legge lo screenshot quando l'OCR non risolve il valore.
PLACEHOLDER_MAP = {
    "🔡🔡€": "importo_ricarica",
    "🔡💶": "profitto_atteso",
    "😳😃€": "importo_bonus",
}

_EXTRAS_RE = re.compile(r"android\.title=(.*?),\s*android\.text=(.*?)\}\]")
_TEXT_ONLY_RE = re.compile(r"android\.text=(.*?)\}\]")


def _looks_like_signal(text: str) -> bool:
    """Euristica: il testo contiene emoji delimitatori tipiche dei segnali promo?"""
    if not text:
        return False
    return any(emoji in text for emoji in SIGNAL_EMOJI)


class TelegramAcquirer(TelegramAcquirerInterface):
    """Acquisisce segnali di matched betting dalle notifiche Telegram via ADB:
    rileva la notifica, apre Telegram, fa uno screenshot della chat e ne estrae
    il testo via OCR. I valori nascosti dietro emoji animate che l'OCR non
    risolve restano come placeholder, da completare a mano (vedi merge_text_and_values)."""

    def __init__(
        self,
        screen_reader: ScreenReaderInterface,
        touch_injector: TouchInjectorInterface,
        ocr_engine=None,
        screenshot_dir: str = None,
        adb_timeout: int = 15,
    ):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self._ocr_engine = ocr_engine
        self.screenshot_dir = screenshot_dir or tempfile.gettempdir()
        self.adb_timeout = adb_timeout

    # --------------------------------------------------------
    # NOTIFICATION DETECTION
    # --------------------------------------------------------

    def check_notifications(self, device_id: str) -> Result[list[dict]]:
        """Legge le notifiche Android via ADB e filtra quelle di Telegram."""
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "dumpsys", "notification", "--noredact"],
                capture_output=True, text=True, timeout=self.adb_timeout,
            )
        except subprocess.TimeoutExpired:
            return Result.fail(f"ADB timeout on {device_id}", "ADB_TIMEOUT")
        except FileNotFoundError:
            return Result.fail("ADB not found in PATH", "DEVICE_OFFLINE")

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "device not found" in stderr or "device offline" in stderr or "no devices" in stderr:
                return Result.fail(f"Device offline: {device_id}", "DEVICE_OFFLINE")
            return Result.fail(f"dumpsys notification failed: {result.stderr}", "ADB_ERROR")

        notifications = []
        for block in result.stdout.split("NotificationRecord{")[1:]:
            if TELEGRAM_PACKAGE not in block:
                continue

            match = _EXTRAS_RE.search(block)
            if match:
                title, text = match.group(1).strip(), match.group(2).strip()
            else:
                text_match = _TEXT_ONLY_RE.search(block)
                title, text = None, (text_match.group(1).strip() if text_match else "")

            notifications.append({"title": title, "text": text, "is_signal": _looks_like_signal(text)})

        return Result.ok(notifications)

    # --------------------------------------------------------
    # SCREENSHOT + OCR
    # --------------------------------------------------------

    def capture_signal(self, device_id: str, channel_name: str = None) -> Result[AcquiredSignal]:
        """Pipeline: apri Telegram -> (naviga al canale) -> screenshot -> OCR."""
        open_result = self._open_telegram(device_id)
        if not open_result.success:
            return Result.fail(open_result.error, open_result.error_code)

        if channel_name:
            tap_result = self.touch_injector.tap_element(device_id, channel_name)
            if not tap_result.success:
                logger.warning(
                    "Impossibile aprire il canale '%s' su %s: %s", channel_name, device_id, tap_result.error,
                )

        time.sleep(3)

        screenshot_result = self._take_screenshot(device_id)
        if not screenshot_result.success:
            return Result.fail(screenshot_result.error, screenshot_result.error_code)
        screenshot_path = screenshot_result.data

        try:
            ocr_text, confidence = self._run_ocr(screenshot_path)
        except Exception as e:
            return Result.fail(f"OCR failed: {e}", "OCR_LOW_CONFIDENCE")

        return Result.ok(AcquiredSignal(
            raw_text=ocr_text,
            screenshot_path=screenshot_path,
            ocr_text=ocr_text,
            emoji_values={},
            confidence=confidence,
            source="telegram_notification",
            device_id=device_id,
            detected_at=datetime.now(timezone.utc).isoformat(),
            channel_name=channel_name,
        ))

    def _take_screenshot(self, device_id: str) -> Result[str]:
        try:
            local_path = os.path.join(self.screenshot_dir, f"telegram_{device_id}_{int(time.time() * 1000)}.png")
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
            return Result.fail(f"Screenshot error: {e}", "ADB_ERROR")

    def _run_ocr(self, image_path: str) -> tuple[str, float]:
        if self._ocr_engine is None:
            from paddleocr import PaddleOCR
            self._ocr_engine = PaddleOCR(use_angle_cls=True, lang="it")

        raw_result = self._ocr_engine.ocr(image_path, cls=True)
        raw_result = raw_result if raw_result else []

        lines, confidences = [], []
        for item in raw_result:
            if len(item) >= 2:
                text = item[1][0] if isinstance(item[1], tuple) else str(item[1])
                conf = item[1][1] if isinstance(item[1], tuple) else 0.5
                lines.append(text)
                confidences.append(conf)

        ocr_text = "\n".join(lines)
        avg_conf = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
        return ocr_text, avg_conf

    # --------------------------------------------------------
    # MERGE
    # --------------------------------------------------------

    def merge_text_and_values(self, ocr_text: str, emoji_values: dict) -> str:
        """Sostituisce i placeholder noti con i valori inseriti manualmente.
        Un placeholder senza valore corrispondente (assente o None) resta intatto:
        SignalParser.parse fa comunque da rete di sicurezza (OBSCURED_VALUES)."""
        merged = ocr_text
        for placeholder, key in PLACEHOLDER_MAP.items():
            if placeholder not in merged:
                continue
            value = emoji_values.get(key)
            if value is None:
                continue
            merged = merged.replace(placeholder, f"{value}€")
        return merged

    # --------------------------------------------------------
    # NAVIGATION
    # --------------------------------------------------------

    def _open_telegram(self, device_id: str) -> Result[bool]:
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "am", "start", "-n",
                 "com.telegram.messenger/.TelegramLaunchActivity"],
                capture_output=True, text=True, timeout=self.adb_timeout,
            )
        except subprocess.TimeoutExpired:
            return Result.fail(f"ADB timeout on {device_id}", "ADB_TIMEOUT")
        except FileNotFoundError:
            return Result.fail("ADB not found in PATH", "DEVICE_OFFLINE")

        if result.returncode != 0:
            return Result.fail(f"Impossibile aprire Telegram: {result.stderr}", "ADB_ERROR")
        return Result.ok(True)

    def _scroll_to_latest(self, device_id: str) -> Result[bool]:
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "input", "swipe", "540", "1800", "540", "600"],
                capture_output=True, text=True, timeout=self.adb_timeout,
            )
        except subprocess.TimeoutExpired:
            return Result.fail(f"ADB timeout on {device_id}", "ADB_TIMEOUT")
        except FileNotFoundError:
            return Result.fail("ADB not found in PATH", "DEVICE_OFFLINE")

        if result.returncode != 0:
            return Result.fail(f"Swipe fallito: {result.stderr}", "ADB_ERROR")
        return Result.ok(True)
