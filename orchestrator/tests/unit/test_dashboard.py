import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from orchestrator.core.result import Result
from orchestrator.dashboard.app import _find_movement_in_files, app
from orchestrator.dashboard.data_loader import (
    DEFAULT_REPORT_DIR,
    load_movimenti,
    load_pending_sport_flows,
    load_rettifiche,
)

REAL_REPORT_DIR = Path(DEFAULT_REPORT_DIR)


def _write_report(report_dir, filename, data):
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _pending_report(collaboratore_id="01", movements=None):
    return {
        "collaboratore_id": collaboratore_id,
        "imported_at": "2026-09-16T15:30:00",
        "movements": movements or [],
    }


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


def test_load_movimenti_with_pending(tmp_path):
    report_dir = tmp_path / "reports"
    _write_report(report_dir, "movimenti_pending_01_20260916_153000.json", _pending_report(
        collaboratore_id="01",
        movements=[
            {
                "item_id": "mov_01_0_1694872200",
                "tipo": "deposito",
                "importo": 40.0,
                "conto_id": None,
                "bookmaker_id": None,
                "bookmaker_nome": "TSG Italy S.R.L",
                "data": "2026-09-13",
                "collaboratore_id": "01",
                "descrizione": "TSG Italy S.R.L",
                "fonte": "paypal_ocr",
                "confidence": 0.7,
                "status": "pending",
            },
            {
                "item_id": "mov_01_1_1694872200",
                "tipo": "prelievo",
                "importo": 15.0,
                "conto_id": None,
                "bookmaker_id": None,
                "bookmaker_nome": "Marathonbet",
                "data": "2026-09-13",
                "collaboratore_id": "01",
                "descrizione": "Marathonbet",
                "fonte": "paypal_ocr",
                "confidence": 1.0,
                "status": "pending",
            },
            {
                "item_id": "mov_01_2_1694872200",
                "tipo": "deposito",
                "importo": 10.0,
                "conto_id": None,
                "bookmaker_id": None,
                "bookmaker_nome": "Sisal",
                "data": "2026-09-13",
                "collaboratore_id": "01",
                "descrizione": "Sisal",
                "fonte": "paypal_ocr",
                "confidence": 0.9,
                "status": "approved",
            },
        ],
    ))

    movimenti = load_movimenti(report_dir=str(report_dir))

    assert len(movimenti) == 2  # l'item "approved" è escluso
    item_ids = {m["item_id"] for m in movimenti}
    assert item_ids == {"mov_01_0_1694872200", "mov_01_1_1694872200"}
    pending_low = next(m for m in movimenti if m["item_id"] == "mov_01_0_1694872200")
    assert pending_low["severity"] == "warning"  # confidence < 0.85
    pending_high = next(m for m in movimenti if m["item_id"] == "mov_01_1_1694872200")
    assert pending_high["severity"] == "ok"  # confidence >= 0.85


def test_find_movement_in_files(tmp_path):
    report_dir = tmp_path / "reports"
    _write_report(report_dir, "movimenti_pending_01_20260916_153000.json", _pending_report(
        movements=[
            {"item_id": "mov_01_0_1694872200", "tipo": "deposito", "importo": 40.0,
             "conto_id": None, "bookmaker_id": None, "bookmaker_nome": "TSG",
             "data": "2026-09-13", "collaboratore_id": "01", "descrizione": "TSG",
             "fonte": "paypal_ocr", "confidence": 0.7, "status": "pending"},
        ],
    ))

    found = _find_movement_in_files("mov_01_0_1694872200", report_dir=str(report_dir))
    assert found is not None
    path, movimento, index = found
    assert path == report_dir / "movimenti_pending_01_20260916_153000.json"
    assert movimento["item_id"] == "mov_01_0_1694872200"
    assert index == 0

    assert _find_movement_in_files("does-not-exist", report_dir=str(report_dir)) is None


def test_dashboard_route():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "HITL Dashboard" in response.text


def test_dashboard_mobile_render():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert 'name="viewport"' in response.text
    assert "width=device-width" in response.text


def test_dashboard_terminology():
    filename = "unregistered_ops_test_terminology.json"
    path = _write_report(REAL_REPORT_DIR, filename, {
        "collaboratore_id": "01",
        "detected_at": "2026-09-16T15:30:00",
        "alerts": [
            {
                "item_id": "unreg_test_terminology",
                "collaboratore_id": "01",
                "detected_at": "2026-09-16T15:30:00",
                "status": "pending",
                "betfair_open_bet": {
                    "event": "Gil Vicente - Maritimo",
                    "selection": "Maritimo",
                    "bet_id": "B1",
                    "lay_stake": 48.0,
                    "lay_odds": 2.1,
                    "liability": 52.0,
                    "placed_at": "2026-09-16T15:00:00",
                },
            },
        ],
    })
    try:
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "Punta" in response.text
        assert "Banca" in response.text
        assert "Back" not in response.text
        assert "Lay" not in response.text
    finally:
        path.unlink(missing_ok=True)


def test_dashboard_splits_sport_and_casino_flows():
    filename = "sport_flow_pending_test_split.json"
    path = _write_report(REAL_REPORT_DIR, filename, {
        "flows": [
            {
                "item_id": "flow_sport_split",
                "status": "pending_hitl",
                "signal": {"book": "SISAL", "category": "sport", "expected_profit": 10.0},
                "tc_verification": {"verified": True},
                "identity_ids_available": ["id1"],
            },
            {
                "item_id": "flow_casino_split",
                "status": "pending_hitl",
                "signal": {"book": "STARCASINO", "category": "casino", "expected_profit": 5.0},
                "tc_verification": {"verified": True},
                "identity_ids_available": ["id2"],
            },
        ],
    })
    try:
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "Operazioni Sport Pending" in response.text
        assert "Operazioni Casino Pending" in response.text
        assert "flow_sport_split" in response.text
        assert "flow_casino_split" in response.text
    finally:
        path.unlink(missing_ok=True)


def test_load_pending_sport_flows_preserves_category(tmp_path):
    report_dir = tmp_path / "reports"
    _write_report(report_dir, "sport_flow_pending_test.json", {
        "flows": [
            {
                "item_id": "f1",
                "status": "pending_hitl",
                "signal": {"book": "SISAL", "category": "sport", "expected_profit": 1.0},
                "tc_verification": {"verified": True},
                "identity_ids_available": [],
            },
            {
                "item_id": "f2",
                "status": "pending_hitl",
                "signal": {"book": "STARCASINO", "category": "casino", "expected_profit": 1.0},
                "tc_verification": {"verified": True},
                "identity_ids_available": [],
            },
        ],
    })
    flows = load_pending_sport_flows(report_dir=str(report_dir))
    categories = {f["item_id"]: f["category"] for f in flows}
    assert categories == {"f1": "sport", "f2": "casino"}


def test_reject_updates_status():
    filename = "movimenti_pending_test_reject.json"
    path = _write_report(REAL_REPORT_DIR, filename, _pending_report(
        collaboratore_id="test",
        movements=[
            {"item_id": "mov_test_reject", "tipo": "deposito", "importo": 40.0,
             "conto_id": None, "bookmaker_id": None, "bookmaker_nome": "TSG",
             "data": "2026-09-13", "collaboratore_id": "test", "descrizione": "TSG",
             "fonte": "paypal_ocr", "confidence": 0.7, "status": "pending"},
        ],
    ))
    try:
        with patch("orchestrator.dashboard.app.AppApiClient") as mock_client_cls:
            client = TestClient(app)
            response = client.post("/reject/mov_test_reject")

        assert response.status_code == 200
        assert response.json() == {"status": "rejected", "id": "mov_test_reject"}
        mock_client_cls.assert_not_called()

        updated = json.loads(path.read_text(encoding="utf-8"))
        assert updated["movements"][0]["status"] == "rejected"
    finally:
        path.unlink(missing_ok=True)


def test_approve_writes_to_netcap():
    filename = "movimenti_pending_test_approve.json"
    path = _write_report(REAL_REPORT_DIR, filename, _pending_report(
        collaboratore_id="test",
        movements=[
            {"item_id": "mov_test_approve", "tipo": "deposito", "importo": 40.0,
             "conto_id": None, "bookmaker_id": None, "bookmaker_nome": "TSG",
             "data": "2026-09-13", "collaboratore_id": "test", "descrizione": "TSG",
             "fonte": "paypal_ocr", "confidence": 0.7, "status": "pending"},
        ],
    ))
    try:
        with patch("orchestrator.dashboard.app.AppApiClient") as mock_client_cls:
            mock_instance = MagicMock()
            mock_instance._get_json.return_value = Result.ok([])
            mock_instance._post_json.return_value = Result.ok({"id": 501})
            mock_client_cls.return_value = mock_instance

            client = TestClient(app)
            response = client.post("/approve/mov_test_approve")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["id"] == "mov_test_approve"
        assert body["netcap_result"]["netcap_id"] == 501
        mock_instance._post_json.assert_called_once()

        updated = json.loads(path.read_text(encoding="utf-8"))
        assert updated["movements"][0]["status"] == "approved"
    finally:
        path.unlink(missing_ok=True)
