from abc import ABC, abstractmethod

from orchestrator.core.result import Result


class TelegramNotifierInterface(ABC):
    """Contratto per l'invio di notifiche push a Raffaele via Telegram bot."""

    @abstractmethod
    def send_notification(self, message: str, parse_mode: str = "HTML") -> Result[bool]:
        """Invia un messaggio a Raffaele via Telegram."""

    @abstractmethod
    def notify_sport_flow(
        self, book: str, bonus_type: str, va: str,
        identities_ready: int, deadline: str, dashboard_url: str,
    ) -> Result[bool]:
        """Notifica una promo sport pronta per conferma."""

    @abstractmethod
    def notify_unregistered_op(
        self, collaboratore_id: str, event: str, liability: float, dashboard_url: str,
    ) -> Result[bool]:
        """Notifica un'operazione non registrata su Betfair."""

    @abstractmethod
    def notify_rettifica(
        self, collaboratore_id: str, book: str, difference: float, dashboard_url: str,
    ) -> Result[bool]:
        """Notifica una riconciliazione con differenza anomala."""
