from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import BalanceReading
from orchestrator.modules.rules_engine.interface import (
    Account,
    Rectification,
    ReconciliationResult,
    RulesEngineInterface,
)


class RulesEngine(RulesEngineInterface):
    """Motore regole riconciliazione P6 — sincrono, zero eccezioni."""

    MATERIALITY_THRESHOLD: float = 0.50    # EUR — sotto questa soglia: no rettifica
    ANOMALY_THRESHOLD: float = 500.0       # EUR — sopra questa soglia: HITL
    MIN_CONFIDENCE: float = 0.85           # sotto → lettura non affidabile

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def reconcile(
        self,
        balance_reading: BalanceReading,
        expected_balance: float,
        account: Account,
    ) -> Result[ReconciliationResult]:
        """Applica le regole di riconciliazione P6."""

        rules_applied: list[str] = []

        # 1 — confidence check
        if balance_reading.confidence < self.MIN_CONFIDENCE:
            rules_applied.append("low_confidence_hitl")
            return Result.ok(self._build_result(
                balance_reading=balance_reading,
                expected_balance=expected_balance,
                account=account,
                action="hitl_required",
                rules_applied=rules_applied,
            ))

        # 2 — calcola adjusted balance
        adjusted = self._calc_adjusted_balance(balance_reading)
        rules_applied.append("adjusted_balance_calc")

        # 3 — colonna
        column = self._determine_column(account)
        if account.is_betfair:
            rules_applied.append("betfair_banco_column")

        # 4 — differenza
        difference = round(adjusted - expected_balance, 2)

        # 5 — bonus note
        bonus_note = self._format_bonus_note(balance_reading.wagering_bonus)
        if bonus_note:
            rules_applied.append("wagering_bonus_noted")

        # 6 — action + rectification
        action, rectification = self._determine_action(
            difference=difference,
            column=column,
            bonus_note=bonus_note,
            rules_applied=rules_applied,
        )

        return Result.ok(ReconciliationResult(
            adjusted_balance=adjusted,
            expected_balance=expected_balance,
            difference=difference,
            column=column,
            bonus_note=bonus_note,
            action=action,
            rectification=rectification,
            rules_applied=rules_applied,
        ))

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _calc_adjusted_balance(self, br: BalanceReading) -> float:
        """raw_balance + open_bets + real_bonus (wagering escluso)."""
        return round(br.raw_balance + br.open_bets + br.real_bonus, 2)

    def _determine_column(self, account: Account) -> str:
        """Betfair → BANCO, tutti gli altri → BOOK."""
        return "BANCO" if account.is_betfair else "BOOK"

    def _format_bonus_note(self, wagering_bonus: Optional[float]) -> Optional[str]:
        """Genera nota bonus se wagering_bonus presente e > 0."""
        if wagering_bonus is not None and wagering_bonus > 0:
            return f"BONUS: {wagering_bonus:.2f}"
        return None

    def _determine_action(
        self,
        difference: float,
        column: str,
        bonus_note: Optional[str],
        rules_applied: list[str],
    ) -> tuple[str, Optional[Rectification]]:
        """Decide action e crea Rectification se serve."""

        abs_diff = abs(difference)

        if abs_diff > self.ANOMALY_THRESHOLD:
            rules_applied.append("anomalous_difference_hitl")
            return "hitl_required", None

        if abs_diff < self.MATERIALITY_THRESHOLD:
            rules_applied.append("below_materiality_threshold")
            return "no_adjustment", None

        # adjust
        rules_applied.append("adjustment_required")
        rectification = Rectification(
            amount=difference,
            column=column,
            reason=f"Differenza riconciliazione: {difference:+.2f} EUR",
            bonus_note=bonus_note,
        )
        return "adjust", rectification

    def _build_result(
        self,
        balance_reading: BalanceReading,
        expected_balance: float,
        account: Account,
        action: str,
        rules_applied: list[str],
    ) -> ReconciliationResult:
        """Helper per costruire un result senza rettifica (es. HITL pre-calcolo)."""
        adjusted = self._calc_adjusted_balance(balance_reading)
        column = self._determine_column(account)
        difference = round(adjusted - expected_balance, 2)
        bonus_note = self._format_bonus_note(balance_reading.wagering_bonus)
        if bonus_note:
            rules_applied.append("wagering_bonus_noted")
        return ReconciliationResult(
            adjusted_balance=adjusted,
            expected_balance=expected_balance,
            difference=difference,
            column=column,
            bonus_note=bonus_note,
            action=action,
            rectification=None,
            rules_applied=rules_applied,
        )
