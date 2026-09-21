import logging
import os

import requests

from orchestrator.core.result import Result
from orchestrator.modules.telegram_notifier.interface import TelegramNotifierInterface

logger = logging.getLogger(__name__)


class TelegramNotifier(TelegramNotifierInterface):
    """Invia notifiche push a Raffaele via Telegram bot.

    Setup: creare un bot con @BotFather, ottenere token, ottenere chat_id di Raffaele.
    Configurazione via env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    """

    def __init__(self, bot_token: str = None, chat_id: str = None):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_notification(self, message: str, parse_mode: str = "HTML") -> Result[bool]:
        """Invia un messaggio a Raffaele via Telegram."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram non configurato: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID mancanti")
            return Result.ok(False)

        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode},
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            return Result.fail(f"Errore invio notifica Telegram: {e}", "TELEGRAM_SEND_ERROR")

        return Result.ok(True)

    def notify_sport_flow(
        self, book: str, bonus_type: str, va: str,
        identities_ready: int, deadline: str, dashboard_url: str,
    ) -> Result[bool]:
        """Notifica una promo sport pronta per conferma."""
        message = (
            f"⚡ <b>{book}</b> | {bonus_type}\n"
            f"💰 Valore atteso: {va}\n"
            f"👥 {identities_ready} identità pronte\n"
            f"⏰ Scade: {deadline}\n"
            f"👉 Conferma: {dashboard_url}"
        )
        return self.send_notification(message)

    def notify_unregistered_op(
        self, collaboratore_id: str, event: str, liability: float, dashboard_url: str,
    ) -> Result[bool]:
        """Notifica un'operazione non registrata su Betfair."""
        message = (
            "⚠️ <b>Operazione non registrata</b>\n"
            f"{collaboratore_id} | Betfair | {event}\n"
            f"Responsabilità: {liability:.2f}€\n"
            f"👉 Completa: {dashboard_url}"
        )
        return self.send_notification(message)

    def notify_rettifica(
        self, collaboratore_id: str, book: str, difference: float, dashboard_url: str,
    ) -> Result[bool]:
        """Notifica una riconciliazione con differenza anomala."""
        message = (
            "🔄 <b>Rettifica riconciliazione</b>\n"
            f"{collaboratore_id} | {book}\n"
            f"Differenza: {difference:.2f}€\n"
            f"👉 Verifica: {dashboard_url}"
        )
        return self.send_notification(message)
