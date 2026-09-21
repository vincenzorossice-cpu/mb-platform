from dataclasses import dataclass
from datetime import datetime, timedelta

from orchestrator.core.result import Result
from orchestrator.modules.signal_parser.interface import Signal


@dataclass
class PromoFilterResult:
    action: str             # "proceed" | "skip" | "warning"
    priority: str            # "flash" | "alta" | "media" | "bassa"
    reason: str
    capital_required: float
    capital_available: float


class PromoFilter:
    """Filtra le promo in base alle regole di capitale/VA di Raffaele."""

    MIN_VA_EUR = 1.0          # VA < 1€ -> skip (troppo tempo)
    LOW_VA_THRESHOLD = 10.0    # VA < 10€ -> warning se capitale alto
    FLASH_PROMO_HOURS = 2      # promo con scadenza < 2h -> priorità massima

    def filter_promo(
        self, signal: Signal, expected_value_eur: float,
        capital_required: float, available_capital: float,
    ) -> Result[PromoFilterResult]:
        """Applica le regole di filtro di Raffaele.

        Regole:
        1. VA < 1€ -> skip (motivo: "Valore atteso troppo basso")
        2. VA < 10€ AND capitale richiesto > disponibile -> skip (motivo: "Capitale insufficiente per VA basso")
        3. VA < 10€ AND capitale ok -> warning (motivo: "VA basso ma possibile")
        4. VA >= 10€ AND capitale ok -> proceed (notifica normale)
        """
        priority = self.determine_priority(signal, expected_value_eur)

        if expected_value_eur < self.MIN_VA_EUR:
            action, reason = "skip", "Valore atteso troppo basso"
        elif expected_value_eur < self.LOW_VA_THRESHOLD:
            if capital_required > available_capital:
                action, reason = "skip", "Capitale insufficiente per VA basso"
            else:
                action, reason = "warning", "VA basso ma possibile"
        else:
            action, reason = "proceed", "VA sufficiente e capitale disponibile"

        return Result.ok(PromoFilterResult(
            action=action,
            priority=priority,
            reason=reason,
            capital_required=capital_required,
            capital_available=available_capital,
        ))

    def determine_priority(self, signal: Signal, expected_value_eur: float) -> str:
        """Determina priorità: 'flash' | 'alta' | 'media' | 'bassa'

        - flash: scadenza < 2h
        - alta: VA > 20€ o priorità segnale = ALTA
        - media: VA 10-20€ o priorità segnale = MEDIA
        - bassa: VA < 10€
        """
        if self._is_flash(signal.deadline):
            return "flash"
        if expected_value_eur > 20.0 or signal.priority == "alta":
            return "alta"
        if expected_value_eur >= 10.0 or signal.priority == "media":
            return "media"
        return "bassa"

    def _is_flash(self, deadline: str) -> bool:
        try:
            deadline_dt = datetime.fromisoformat(deadline)
        except (TypeError, ValueError):
            return False
        now = datetime.now(deadline_dt.tzinfo) if deadline_dt.tzinfo else datetime.now()
        return deadline_dt - now < timedelta(hours=self.FLASH_PROMO_HOURS)
