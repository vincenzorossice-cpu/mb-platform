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
    """Legge i movimenti importati con confidence < 0.85 o dati mancanti."""
    movimenti = []
    for report in _load_json_files(report_dir, "movimenti_*.json"):
        items = report if isinstance(report, list) else report.get("movimenti", [])
        for i, movimento in enumerate(items):
            confidence = movimento.get("confidence")
            mancante = confidence is None or movimento.get("importo") is None
            if not mancante and confidence >= MIN_CONFIDENCE:
                continue
            severity = "critical" if (confidence is not None and confidence < CRITICAL_CONFIDENCE) else "warning"
            movimenti.append({
                "id": f"movimento-{i}-{movimento.get('data')}",
                "data": movimento.get("data"),
                "fonte": movimento.get("fonte"),
                "tipo": movimento.get("tipo"),
                "importo": movimento.get("importo"),
                "descrizione": movimento.get("descrizione"),
                "confidence": confidence,
                "severity": severity,
            })
    return movimenti


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
