import json
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import BalanceReading
from orchestrator.modules.screen_reader.betfair_open_bets import BetfairOpenBet
from orchestrator.modules.rules_engine.interface import (
    Account,
    Rectification,
    ReconciliationResult,
    RulesEngineInterface,
    UnregisteredOperationAlert,
)


class RulesEngine(RulesEngineInterface):
    """Motore regole riconciliazione P6 — sincrono, zero eccezioni."""

    MATERIALITY_THRESHOLD: float = 0.50           # EUR — sotto questa soglia: no rettifica
    ANOMALY_THRESHOLD: float = 500.0              # EUR — sopra questa soglia: HITL
    MIN_CONFIDENCE: float = 0.85                  # sotto → lettura non affidabile
    OPEN_BETS_MISMATCH_THRESHOLD: float = 5.0     # EUR — |open_bets telefono - impegnato Netcap| oltre: HITL

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def reconcile(
        self,
        balance_reading: BalanceReading,
        expected_balance: float,
        account: Account,
        impegnato_netcap: float = 0.0,
    ) -> Result[ReconciliationResult]:
        """Applica le regole di riconciliazione P6.

        expected_balance e' il saldo totale Netcap (matrix); impegnato_netcap e'
        l'importo delle scommesse aperte secondo Netcap. Il confronto avviene
        sempre disponibile-vs-disponibile: raw_balance+real_bonus (telefono) contro
        expected_balance-impegnato_netcap (Netcap)."""

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

        # 2 — cross-check open_bets (telefono) vs impegnato (Netcap): solo quando il
        # telefono ha effettivamente rilevato un valore (open_bets > 0) — nella
        # maggior parte dei casi lo screen reader non trova le scommesse aperte nella
        # pagina saldo e open_bets resta a 0 "per assenza di lettura", non perche' non
        # ce ne siano; in quel caso non c'e' nulla da confrontare, si procede con
        # impegnato_netcap come unica fonte. Se invece open_bets e' stato letto e
        # diverge oltre soglia da impegnato, le due fonti non sono allineate -> HITL
        # invece di rettifica automatica.
        open_bets_diff = abs(round(balance_reading.open_bets - impegnato_netcap, 2))
        if balance_reading.open_bets > 0 and open_bets_diff > self.OPEN_BETS_MISMATCH_THRESHOLD:
            rules_applied.append("open_bets_netcap_mismatch")
            return Result.ok(self._build_result(
                balance_reading=balance_reading,
                expected_balance=expected_balance,
                account=account,
                action="hitl_required",
                rules_applied=rules_applied,
            ))

        # 3 — disponibile telefono (open_bets escluso: usato solo per il cross-check sopra)
        adjusted = self._calc_adjusted_balance(balance_reading)
        rules_applied.append("adjusted_balance_calc")

        # 4 — colonna
        column = self._determine_column(account)
        if account.is_betfair:
            rules_applied.append("betfair_banco_column")

        # 5 — disponibile Netcap = totale (matrix) - impegnato
        expected_disponibile = round(expected_balance - impegnato_netcap, 2)

        # 6 — differenza (disponibile vs disponibile)
        difference = round(adjusted - expected_disponibile, 2)

        # 7 — bonus note
        bonus_note = self._format_bonus_note(balance_reading.wagering_bonus)
        if bonus_note:
            rules_applied.append("wagering_bonus_noted")

        # 8 — action + rectification
        action, rectification = self._determine_action(
            difference=difference,
            column=column,
            bonus_note=bonus_note,
            rules_applied=rules_applied,
        )

        return Result.ok(ReconciliationResult(
            adjusted_balance=adjusted,
            expected_balance=expected_disponibile,
            difference=difference,
            column=column,
            bonus_note=bonus_note,
            action=action,
            rectification=rectification,
            rules_applied=rules_applied,
        ))

    def detect_unregistered_operations(
        self,
        open_bets: list[BetfairOpenBet],
        collaboratore_id: str,
        netcap_operations: list[dict],
    ) -> Result[list[UnregisteredOperationAlert]]:
        """Confronta le bancate aperte su Betfair con le operazioni registrate
        su Netcap. Le bancate senza operazione corrispondente (tolleranza ±1
        EUR su lay_stake/liability contro qualunque campo stake-like trovato
        sull'operazione) diventano alert 'operazione non registrata'."""
        now = datetime.now(timezone.utc).isoformat()
        alerts: list[UnregisteredOperationAlert] = []
        for bet in open_bets:
            if any(self._matches_netcap(bet, op) for op in netcap_operations):
                continue
            alerts.append(UnregisteredOperationAlert(
                collaboratore_id=collaboratore_id,
                betfair_open_bet=asdict(bet),
                detected_at=now,
            ))
        return Result.ok(alerts)

    def save_unregistered_alerts(
        self,
        alerts: list[UnregisteredOperationAlert],
        collaboratore_id: str,
        report_dir: str = "orchestrator/tests/validation/reports",
    ) -> Result[str]:
        """Salva gli alert 'operazione non registrata' in un file JSON per la
        HITL Dashboard (stesso pattern di MovementImporter.save_pending_movements)."""
        now = datetime.now(timezone.utc)
        directory = Path(report_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_path = directory / f"unregistered_ops_{collaboratore_id}_{now:%Y%m%d_%H%M%S}.json"

        epoch = int(now.timestamp())
        alerts_data = [{
            "item_id": f"unreg_{collaboratore_id}_{index}_{epoch}",
            "collaboratore_id": alert.collaboratore_id,
            "betfair_open_bet": alert.betfair_open_bet,
            "detected_at": alert.detected_at,
            "status": alert.status,
            "bookmaker_id": alert.bookmaker_id,
            "back_stake": alert.back_stake,
            "back_odds": alert.back_odds,
            "tipologia": alert.tipologia,
        } for index, alert in enumerate(alerts)]

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump({
                "collaboratore_id": collaboratore_id,
                "detected_at": now.isoformat(),
                "alerts": alerts_data,
            }, f, ensure_ascii=False, indent=2)

        return Result.ok(str(file_path))

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    # campi Netcap potenzialmente stake-like; forma reale della risposta
    # GET /operazioni non confermata — estrazione defensiva, mai eccezione
    _NETCAP_STAKE_LIKE_KEYS = ("contropuntata", "punta", "importo", "importo_impegnato", "stake", "liability")

    def _matches_netcap(self, bet: BetfairOpenBet, operazione: dict) -> bool:
        """±1 EUR di tolleranza tra lay_stake/liability della bancata e
        qualunque campo stake-like trovato sull'operazione Netcap."""
        for value in self._extract_netcap_stake_fields(operazione):
            if abs(round(bet.lay_stake - value, 2)) <= 1.0 or abs(round(bet.liability - value, 2)) <= 1.0:
                return True
        return False

    def _extract_netcap_stake_fields(self, operazione: dict) -> list[float]:
        """Estrazione defensiva: top-level + righe annidate (se presenti),
        ignora tutto cio' che non e' un numero. Un'operazione senza campi
        comparabili conta come 'non corrispondente', mai eccezione."""
        values: list[float] = []
        if not isinstance(operazione, dict):
            return values
        for key in self._NETCAP_STAKE_LIKE_KEYS:
            v = operazione.get(key)
            if isinstance(v, (int, float)):
                values.append(float(v))
        righe = operazione.get("righe")
        if isinstance(righe, list):
            for riga in righe:
                if isinstance(riga, dict):
                    for key in self._NETCAP_STAKE_LIKE_KEYS:
                        v = riga.get(key)
                        if isinstance(v, (int, float)):
                            values.append(float(v))
        return values

    def _calc_adjusted_balance(self, br: BalanceReading) -> float:
        """raw_balance + real_bonus (wagering escluso; open_bets escluso: usato solo
        come cross-check contro impegnato_netcap in reconcile, non sommato al saldo)."""
        return round(br.raw_balance + br.real_bonus, 2)

    def _determine_column(self, account: Account) -> str:
        """In riconciliazione (P6) tutto va in colonna BOOK (anche Betfair).
        Il differenziale in BANCO farebbe applicare la fee Netcap corrompendo il saldo."""
        return "BOOK"

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

        # FIX-BASSA-6: confini inclusivi verso "adjust" (">" non ">=", "<" non "<=") —
        # abs_diff == ANOMALY_THRESHOLD (500.00 esatto) NON e' hitl_required
        if abs_diff > self.ANOMALY_THRESHOLD:
            rules_applied.append("anomalous_difference_hitl")
            return "hitl_required", None

        # FIX-BASSA-6: abs_diff == MATERIALITY_THRESHOLD (0.50 esatto) NON e' no_adjustment
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
