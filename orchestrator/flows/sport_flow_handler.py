import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.flows.sport_flow import SignalFlowResult
from orchestrator.modules.multiplier_engine.interface import CascadeCalculation, CascadeEventCalc
from orchestrator.modules.rag_system.interface import TCVerification
from orchestrator.modules.signal_parser.interface import Signal
from orchestrator.modules.telegram_notifier.notifier import TelegramNotifier

DEFAULT_REPORT_DIR = "orchestrator/tests/validation/reports"
DEFAULT_DASHBOARD_BASE_URL = "http://localhost:8000"

logger = logging.getLogger(__name__)


class SportFlowHandler:
    """Salva e carica i flow P5 Sport pending per la HITL Dashboard. Stesso
    pattern di MovementImporter.save_pending_movements / RulesEngine.save_unregistered_alerts:
    un file JSON per flow, con status mutabile per l'approvazione."""

    def save_pending_flow(
        self, flow_result: SignalFlowResult, report_dir: str = DEFAULT_REPORT_DIR,
    ) -> Result[str]:
        """Salva il flow result in sport_flow_pending_{telegram_message_id}_{timestamp}.json
        e assegna l'hitl_item_id."""
        now = datetime.now(timezone.utc)
        directory = Path(report_dir)
        directory.mkdir(parents=True, exist_ok=True)

        telegram_message_id = flow_result.signal.telegram_message_id
        epoch = int(now.timestamp())
        item_id = f"sport_{telegram_message_id}_{epoch}"
        file_path = directory / f"sport_flow_pending_{telegram_message_id}_{now:%Y%m%d_%H%M%S}.json"

        flow_data = {
            "item_id": item_id,
            "signal": asdict(flow_result.signal),
            "tc_verification": asdict(flow_result.tc_verification),
            "cascade_calc": asdict(flow_result.cascade_calc) if flow_result.cascade_calc else None,
            "identity_ids_available": flow_result.identity_ids_available,
            "status": flow_result.status,
            "error": flow_result.error,
        }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump({
                "created_at": now.isoformat(),
                "flows": [flow_data],
            }, f, ensure_ascii=False, indent=2)

        self._notify_sport_flow(flow_result.signal, item_id, len(flow_result.identity_ids_available))

        return Result.ok(item_id)

    def _notify_sport_flow(self, signal: Signal, item_id: str, identities_ready: int) -> None:
        """Notifica Raffaele via Telegram che un flow sport e' pronto per conferma.
        Un fallimento della notifica non deve far fallire il salvataggio del flow."""
        dashboard_base_url = os.environ.get("DASHBOARD_BASE_URL", DEFAULT_DASHBOARD_BASE_URL)
        dashboard_url = f"{dashboard_base_url}/confirm_sport_flow/{item_id}"

        result = TelegramNotifier().notify_sport_flow(
            book=signal.book,
            bonus_type=f"{signal.bonus_type.capitalize()} {signal.category.capitalize()}",
            va=signal.expected_profit,
            identities_ready=identities_ready,
            deadline=signal.deadline,
            dashboard_url=dashboard_url,
        )
        if not result.success:
            logger.warning("Notifica Telegram fallita per flow %s: %s", item_id, result.error)

    TERMINAL_STATUSES = ("executed", "rejected")

    def load_pending_flows(self, report_dir: str = DEFAULT_REPORT_DIR) -> list[dict]:
        """Legge tutti i flow non ancora risolti (pending_hitl, tc_discrepancy,
        calc_error) per la dashboard: anche le discrepanze T&C e gli errori di
        calcolo vanno mostrati a Raffaele, non solo i flow puliti."""
        directory = Path(report_dir)
        if not directory.exists():
            return []

        flows = []
        for path in sorted(directory.glob("sport_flow_pending_*.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    report = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            for flow_data in report.get("flows", []):
                if flow_data.get("status") in self.TERMINAL_STATUSES:
                    continue
                signal = flow_data.get("signal", {})
                flows.append({
                    "item_id": flow_data.get("item_id"),
                    "book": signal.get("book"),
                    "category": signal.get("category"),
                    "tc_status": "verificato" if flow_data.get("tc_verification", {}).get("verified") else "discrepanze",
                    "expected_profit": signal.get("expected_profit"),
                    "identity_ids_available": flow_data.get("identity_ids_available", []),
                    "status": flow_data.get("status"),
                })
        return flows

    def get_pending_flow(self, hitl_item_id: str, report_dir: str = DEFAULT_REPORT_DIR) -> Optional[SignalFlowResult]:
        """Recupera un flow pending per esecuzione, ricostruendo le dataclass."""
        found = self._find_flow(hitl_item_id, report_dir)
        if found is None:
            return None
        _path, flow_data, _index = found
        return self._flow_from_dict(flow_data)

    def update_flow_status(self, hitl_item_id: str, status: str, report_dir: str = DEFAULT_REPORT_DIR) -> bool:
        """Aggiorna lo status di un flow dopo conferma/esecuzione."""
        found = self._find_flow(hitl_item_id, report_dir)
        if found is None:
            return False
        file_path, _flow_data, index = found
        try:
            with open(file_path, encoding="utf-8") as f:
                report = json.load(f)
            report["flows"][index]["status"] = status
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            return True
        except (json.JSONDecodeError, OSError, IndexError, KeyError):
            return False

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _find_flow(
        self, hitl_item_id: str, report_dir: str = DEFAULT_REPORT_DIR,
    ) -> Optional[tuple[Path, dict, int]]:
        directory = Path(report_dir)
        if not directory.exists():
            return None
        for path in sorted(directory.glob("sport_flow_pending_*.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    report = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            for index, flow_data in enumerate(report.get("flows", [])):
                if flow_data.get("item_id") == hitl_item_id:
                    return path, flow_data, index
        return None

    def _flow_from_dict(self, flow_data: dict) -> SignalFlowResult:
        signal = Signal(**flow_data["signal"])
        tc_verification = TCVerification(**flow_data["tc_verification"])

        cascade_calc = None
        cascade_calc_data = flow_data.get("cascade_calc")
        if cascade_calc_data:
            events = [CascadeEventCalc(**event) for event in cascade_calc_data["events"]]
            cascade_calc = CascadeCalculation(**{**cascade_calc_data, "events": events})

        return SignalFlowResult(
            signal=signal,
            tc_verification=tc_verification,
            cascade_calc=cascade_calc,
            identity_ids_available=flow_data.get("identity_ids_available", []),
            status=flow_data.get("status", "pending_hitl"),
            hitl_item_id=flow_data.get("item_id"),
            error=flow_data.get("error"),
        )
