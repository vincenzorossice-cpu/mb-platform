import json
from pathlib import Path

DEFAULT_REPORT_DIR = "orchestrator/tests/validation/reports"

ANOMALY_THRESHOLD = 500.0      # EUR — sopra questa soglia: differenza critica
MIN_CONFIDENCE = 0.85          # sotto questa soglia: warning (HITL)
CRITICAL_CONFIDENCE = 0.5      # sotto questa soglia: critical

LEVEL_TO_SEVERITY = {"L1": "info", "L2": "warning", "L3": "critical"}


def _load_json_files(report_dir: str, pattern: str) -> list[dict]:
    directory = Path(report_dir)
    if not directory.exists():
        return []
    files = []
    for path in sorted(directory.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as f:
                files.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return files


def load_rettifiche(report_dir: str = DEFAULT_REPORT_DIR) -> list[dict]:
    """Legge i report E2E e estrae le rettifiche con action == 'hitl_required'."""
    rettifiche = []
    for report in _load_json_files(report_dir, "test_e2e_results_*.json"):
        collaboratore = report.get("run", {}).get("collaboratore_id")
        for i, result in enumerate(report.get("results", [])):
            if result.get("action") != "hitl_required":
                continue
            if result.get("betfair_open_bets_candidate"):
                # gestita dalla sezione dedicata "Operazioni non registrate", non
                # come rettifica generica ambigua
                continue
            confidence = result.get("real_balance", {}).get("confidence")
            differenza = result.get("difference", 0.0)
            severity = "critical"
            if abs(differenza) <= ANOMALY_THRESHOLD and (
                confidence is None or confidence >= CRITICAL_CONFIDENCE
            ):
                severity = "warning"
            rettifiche.append({
                "id": f"rettifica-{collaboratore}-{result.get('book')}-{i}",
                "collaboratore": collaboratore,
                "book": result.get("book"),
                "saldo_reale": result.get("adjusted_balance"),
                "saldo_atteso": result.get("expected_balance"),
                "differenza": differenza,
                "colonna": result.get("column"),
                "confidence": confidence,
                "action": result.get("action"),
                "severity": severity,
            })
    return rettifiche


def load_movimenti(report_dir: str = DEFAULT_REPORT_DIR) -> list[dict]:
    """Legge tutti i movimenti pending salvati da MovementImporter.save_pending_movements."""
    movimenti = []
    for report in _load_json_files(report_dir, "movimenti_pending_*.json"):
        collaboratore_id = report.get("collaboratore_id")
        for movimento in report.get("movements", []):
            if movimento.get("status") in ("approved", "rejected"):
                continue
            confidence = movimento.get("confidence")
            severity = "ok" if (confidence is not None and confidence >= MIN_CONFIDENCE) else "warning"
            movimenti.append({
                "item_id": movimento.get("item_id"),
                "collaboratore_id": movimento.get("collaboratore_id", collaboratore_id),
                "data": movimento.get("data"),
                "fonte": movimento.get("fonte"),
                "tipo": movimento.get("tipo"),
                "importo": movimento.get("importo"),
                "descrizione": movimento.get("descrizione"),
                "confidence": confidence,
                "severity": severity,
            })
    return movimenti


def load_unregistered_operations(report_dir: str = DEFAULT_REPORT_DIR) -> list[dict]:
    """Legge gli alert 'operazione non registrata' pending salvati da
    RulesEngine.save_unregistered_alerts."""
    operazioni = []
    for report in _load_json_files(report_dir, "unregistered_ops_*.json"):
        collaboratore_id = report.get("collaboratore_id")
        for alert in report.get("alerts", []):
            if alert.get("status") != "pending":
                continue
            bet = alert.get("betfair_open_bet", {})
            operazioni.append({
                "item_id": alert.get("item_id"),
                "collaboratore_id": alert.get("collaboratore_id", collaboratore_id),
                "event": bet.get("event"),
                "selection": bet.get("selection"),
                "bet_id": bet.get("bet_id"),
                "lay_stake": bet.get("lay_stake"),
                "lay_odds": bet.get("lay_odds"),
                "liability": bet.get("liability"),
                "placed_at": bet.get("placed_at"),
                "detected_at": alert.get("detected_at"),
                "severity": "critical",  # bancata reale non registrata = movimento di denaro reale non tracciato
            })
    return operazioni


def load_bookmaker_options(config_path: str = None) -> dict:
    """Merge deduplicato bookmaker_id -> nome da tutte le identity in
    config.json, per la dropdown book della sezione 'Operazioni non registrate'."""
    from orchestrator.core.config import load_config
    config = load_config(config_path)
    options: dict[int, str] = {}
    for identity in config.get("identities", []):
        for book in identity.get("books", []):
            bk_id, name = book.get("bookmaker_id"), book.get("name")
            if bk_id is not None and name:
                options[int(bk_id)] = name
    return dict(sorted(options.items(), key=lambda kv: kv[1]))


def load_pending_sport_flows(report_dir: str = DEFAULT_REPORT_DIR) -> list[dict]:
    """Legge i flow P5 Sport pending salvati da SportFlowHandler.save_pending_flow,
    aggiungendo severity per il badge della dashboard."""
    from orchestrator.flows.sport_flow_handler import SportFlowHandler

    flows = SportFlowHandler().load_pending_flows(report_dir)
    for flow in flows:
        if flow.get("status") == "calc_error":
            flow["severity"] = "critical"
        elif flow.get("status") == "tc_discrepancy":
            flow["severity"] = "warning"
        else:
            flow["severity"] = "ok"
    return flows


def load_errors(report_dir: str = DEFAULT_REPORT_DIR) -> list[dict]:
    """Legge gli errori di sistema (device offline, OCR fallito, Netcap irraggiungibile)."""
    errori = []
    for report in _load_json_files(report_dir, "errors_*.json"):
        items = report if isinstance(report, list) else report.get("errors", [])
        for i, error in enumerate(items):
            level = error.get("level", "L2")
            errori.append({
                "id": f"error-{i}-{error.get('code')}",
                "code": error.get("code"),
                "level": level,
                "message": error.get("message"),
                "device_id": error.get("device_id"),
                "timestamp": error.get("timestamp"),
                "severity": LEVEL_TO_SEVERITY.get(level, "warning"),
            })
    return errori
