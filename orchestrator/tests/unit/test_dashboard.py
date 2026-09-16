import json

from fastapi.testclient import TestClient

from orchestrator.dashboard.app import app
from orchestrator.dashboard.data_loader import load_movimenti, load_rettifiche


def _write_report(report_dir, filename, data):
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_rettifiche_empty(tmp_path):
    rettifiche = load_rettifiche(report_dir=str(tmp_path / "reports"))
    assert rettifiche == []


def test_load_rettifiche_with_data(tmp_path):
    report_dir = tmp_path / "reports"
    _write_report(report_dir, "test_e2e_results_20260101_120000.json", {
        "run": {"collaboratore_id": "raffaele"},
        "results": [
            {
                "book": "Sisal",
                "expected_balance": 100.0,
                "adjusted_balance": 700.0,
                "difference": 600.0,
                "column": "BOOK",
                "action": "hitl_required",
                "real_balance": {"confidence": 0.9},
            },
            {
                "book": "Eurobet",
                "expected_balance": 50.0,
                "adjusted_balance": 50.0,
                "difference": 0.0,
                "column": "BANCO",
                "action": "no_adjustment",
                "real_balance": {"confidence": 0.95},
            },
            {
                "book": "Betfair",
                "expected_balance": 20.0,
                "adjusted_balance": 25.0,
                "difference": 5.0,
                "column": "BOOK",
                "action": "hitl_required",
                "real_balance": {"confidence": 0.4},
            },
        ],
    })

    rettifiche = load_rettifiche(report_dir=str(report_dir))

    assert len(rettifiche) == 2
    books = {r["book"] for r in rettifiche}
    assert books == {"Sisal", "Betfair"}
    sisal = next(r for r in rettifiche if r["book"] == "Sisal")
    assert sisal["severity"] == "critical"  # differenza > 500
    betfair = next(r for r in rettifiche if r["book"] == "Betfair")
    assert betfair["severity"] == "critical"  # confidence < 0.5


def test_load_movimenti_empty(tmp_path):
    movimenti = load_movimenti(report_dir=str(tmp_path / "reports"))
    assert movimenti == []


def test_load_movimenti_low_confidence(tmp_path):
    report_dir = tmp_path / "reports"
    _write_report(report_dir, "movimenti_20260101.json", [
        {
            "data": "2026-01-01",
            "fonte": "hype_email",
            "tipo": "deposito",
            "importo": 50.0,
            "descrizione": "Pagamento EUR 50,00",
            "confidence": 0.7,
        },
        {
            "data": "2026-01-02",
            "fonte": "paypal_ocr",
            "tipo": "prelievo",
            "importo": 30.0,
            "descrizione": "Addebito EUR 30,00",
            "confidence": 0.95,
        },
    ])

    movimenti = load_movimenti(report_dir=str(report_dir))

    assert len(movimenti) == 1
    assert movimenti[0]["confidence"] == 0.7
    assert movimenti[0]["severity"] == "warning"


def test_dashboard_route():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "HITL Dashboard" in response.text
