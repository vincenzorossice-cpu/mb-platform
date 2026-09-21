import re
import asyncio
import threading
from datetime import datetime, timezone

from telethon import TelegramClient

from orchestrator.core.result import Result
from orchestrator.modules.telegram_acquirer.interface import AcquiredSignal, TelegramAcquirerInterface

# Un segnale di interesse inizia sempre con l'emoji 📚 (book).
SIGNAL_EMOJI = "📚"

# Regex condivise per i valori nascosti dietro emoji animate: con Telethon
# arrivano sempre come sequenze di 🔡 (a differenza dell'OCR, che leggeva
# glifi diversi e imprevedibili per ogni sticker). La dashboard riusa queste
# stesse regex per decidere quali campi manuali mostrare (vedi
# orchestrator/dashboard/app.py::_find_placeholders).
PLACEHOLDER_PATTERNS: dict[str, re.Pattern] = {
    "importo_ricarica": re.compile(r'(ricarica|deposita|gioca)\s+🔡+\s*€?', re.IGNORECASE),
    "importo_bonus": re.compile(r'(bonus|rcevi)\s+🔡+\s*€?', re.IGNORECASE),
    "profitto_atteso": re.compile(r'(PROFITTO|profitto)\s*(MEDIO|atteso)?\s*:?\s*🔡+\s*€?', re.IGNORECASE),
    "valore_atteso": re.compile(r'(VALORE\s+ATTESO|VA)\s*:?\s*🔡+\s*€?', re.IGNORECASE),
}


def _looks_like_signal(text: str) -> bool:
    """Un segnale inizia sempre con l'emoji 📚 (book)."""
    if not text:
        return False
    return text.strip().startswith(SIGNAL_EMOJI)


def _replacement_for(key: str, match: "re.Match", value_str: str) -> str:
    if key == "profitto_atteso":
        label = " ".join(g for g in (match.group(1), match.group(2)) if g)
        return f"{label}: {value_str}€"
    if key == "valore_atteso":
        return f"{match.group(1)}: {value_str}€"
    return f"{match.group(1)} {value_str}€"


class TelegramAcquirer(TelegramAcquirerInterface):
    """Acquisisce segnali di matched betting leggendo direttamente il canale
    Telegram "MB Evo Pro" via Telethon: niente telefoni, screenshot o OCR.
    I valori nascosti dietro emoji animate arrivano come placeholder 🔡, da
    completare a mano (vedi merge_text_and_values)."""

    def __init__(
        self,
        api_id: int = 30241084,
        api_hash: str = "691c8b667128597541ca1785bbb7007d",
        session_name: str = "telegram_session",
        channel_id: int = -1003833700547,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.channel_id = channel_id
        self._client = None  # lazy init
        self._loop = None
        self._thread = None

    def _get_client(self) -> TelegramClient:
        if self._client is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()
            self._client = TelegramClient(self.session_name, self.api_id, self.api_hash, loop=self._loop)
            future = asyncio.run_coroutine_threadsafe(self._client.connect(), self._loop)
            future.result(timeout=30)
        return self._client

    def _run_async(self, coro):
        """Run a coroutine in the dedicated thread loop. Handles mocks that return non-coroutines."""
        if asyncio.iscoroutine(coro):
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result(timeout=30)
        return coro  # Mock returns non-coroutine directly

    # --------------------------------------------------------
    # NOTIFICATION DETECTION
    # --------------------------------------------------------

    def check_notifications(self, device_id: str = None) -> Result[list[dict]]:
        """Legge gli ultimi messaggi del canale e identifica quelli che sono segnali."""
        try:
            client = self._get_client()
            notifications = []
            for msg in client.get_messages(self.channel_id, limit=20):
                if not msg.text:
                    continue
                text = msg.text
                if not _looks_like_signal(text):
                    continue
                notifications.append({
                    "title": "MB Evo Pro",
                    "text": text,
                    "is_signal": True,
                    "message_id": str(msg.id),
                    "date": msg.date.isoformat() if msg.date else None,
                })
            return Result.ok(notifications)
        except Exception as e:
            return Result.fail(f"Errore Telegram: {e}", "TELEGRAM_ERROR")

    # --------------------------------------------------------
    # CAPTURE
    # --------------------------------------------------------

    def capture_signal(self, device_id: str = None, channel_name: str = None) -> Result[AcquiredSignal]:
        """Acquisisce l'ultimo segnale (testo che inizia con 📚) dal canale."""
        try:
            client = self._get_client()
            messages = self._run_async(client.get_messages(self.channel_id, limit=50))
            for msg in messages:
                if not msg.text:
                    continue
                # Filtro topic: solo topic 2 (Ricorrenti) e 1 (Benvenuto)
                reply_to = None
                if msg.reply_to:
                    reply_to = msg.reply_to.reply_to_msg_id if hasattr(msg.reply_to, 'reply_to_msg_id') else None
                if reply_to not in (1, 2):
                    continue
                if _looks_like_signal(msg.text):
                    raw_text = msg.text
                    return Result.ok(AcquiredSignal(
                        raw_text=raw_text,
                        ocr_text=raw_text,
                        emoji_values={},
                        source="telegram_telethon",
                        device_id="pc",
                        detected_at=datetime.now(timezone.utc).isoformat(),
                        channel_name="MB Evo Pro",
                        message_id=str(msg.id),
                    ))
            return Result.fail("Nessun segnale trovato negli ultimi 50 messaggi", "NO_SIGNAL_FOUND")
        except Exception as e:
            return Result.fail(f"Errore Telegram: {e}", "TELEGRAM_ERROR")

        return Result.fail("Nessun segnale trovato negli ultimi 50 messaggi", "NO_SIGNAL_FOUND")

    def extract_emoji_values(self, screenshot_path: str) -> Result[dict]:
        """Non più necessario con Telethon — i valori vengono inseriti manualmente."""
        return Result.ok({})

    # --------------------------------------------------------
    # MERGE
    # --------------------------------------------------------

    def merge_text_and_values(self, ocr_text: str, emoji_values: dict) -> str:
        """Sostituisce i placeholder 🔡 con i valori inseriti manualmente.
        Un placeholder senza valore corrispondente (assente, None o 0) resta
        intatto: SignalParser.parse fa comunque da rete di sicurezza (OBSCURED_VALUES)."""
        result = ocr_text
        for key, value in emoji_values.items():
            if not value:
                continue
            pattern = PLACEHOLDER_PATTERNS.get(key)
            if pattern is None:
                continue
            value_str = str(value).replace('.', ',')
            result = pattern.sub(lambda m: _replacement_for(key, m, value_str), result)
        return result
