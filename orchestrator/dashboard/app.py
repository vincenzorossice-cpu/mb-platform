import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from orchestrator.dashboard.data_loader import (
    DEFAULT_REPORT_DIR,
    load_bookmaker_options,
    load_errors,
    load_movimenti,
    load_rettifiche,
    load_unregistered_operations,
)
from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.movement_importer.importer import MovementImporter
from orchestrator.modules.movement_importer.interface import Movement

app = FastAPI(title="MB Platform HITL Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    rettifiche = load_rettifiche()
    movimenti = load_movimenti()
    errori = load_errors()
    operazioni_non_registrate = load_unregistered_operations()
    bookmaker_options = load_bookmaker_options()
    return templates.TemplateResponse(request, "index.html", {
        "rettifiche": rettifiche,
        "movimenti": movimenti,
        "errori": errori,
        "operazioni_non_registrate": operazioni_non_registrate,
        "bookmaker_options": bookmaker_options,
    })


def _find_movement_in_files(
    item_id: str, report_dir: str = DEFAULT_REPORT_DIR
) -> Optional[tuple[Path, dict, int]]:
    directory = Path(report_dir)
    if not directory.exists():
        return None
    for path in sorted(directory.glob("movimenti_pending_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for index, movimento in enumerate(report.get("movements", [])):
            if movimento.get("item_id") == item_id:
                return path, movimento, index
    return None


def _update_movement_status(file_path: Path, index: int, status: str) -> bool:
    try:
        with open(file_path, encoding="utf-8") as f:
            report = json.load(f)
        report["movements"][index]["status"] = status
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return True
    except (json.JSONDecodeError, OSError, IndexError, KeyError):
        return False


def _movement_from_dict(data: dict) -> Movement:
    return Movement(
        tipo=data["tipo"],
        importo=data["importo"],
        conto_id=data.get("conto_id"),
        bookmaker_id=data.get("bookmaker_id"),
        bookmaker_nome=data.get("bookmaker_nome"),
        data=data["data"],
        collaboratore_id=data["collaboratore_id"],
        descrizione=data.get("descrizione") or data.get("descrizzione"),
        fonte=data.get("fonte"),
        confidence=data.get("confidence"),
    )


@app.post("/approve/{item_id}")
async def approve_item(item_id: str):
    found = _find_movement_in_files(item_id)
    if found is None:
        return JSONResponse(status_code=404, content={"status": "error", "id": item_id, "error": "movimento non trovato"})
    file_path, movimento_dict, index = found

    movement = _movement_from_dict(movimento_dict)
    app_api_client = AppApiClient()
    importer = MovementImporter(app_api_client, screen_reader=None)
    result = importer.write_movements_to_netcap([movement])

    if not result.success or not result.data[0].success:
        error = result.error if not result.success else result.data[0].message
        return JSONResponse(status_code=502, content={"status": "error", "id": item_id, "error": error})

    _update_movement_status(file_path, index, "approved")
    return {"status": "approved", "id": item_id, "netcap_result": asdict(result.data[0])}


@app.post("/reject/{item_id}")
async def reject_item(item_id: str):
    found = _find_movement_in_files(item_id)
    if found is None:
        return JSONResponse(status_code=404, content={"status": "error", "id": item_id, "error": "movimento non trovato"})
    file_path, _movimento_dict, index = found

    _update_movement_status(file_path, index, "rejected")
    return {"status": "rejected", "id": item_id}


def _find_unregistered_alert_in_files(
    item_id: str, report_dir: str = DEFAULT_REPORT_DIR
) -> Optional[tuple[Path, dict, int]]:
    directory = Path(report_dir)
    if not directory.exists():
        return None
    for path in sorted(directory.glob("unregistered_ops_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for index, alert in enumerate(report.get("alerts", [])):
            if alert.get("item_id") == item_id:
                return path, alert, index
    return None


def _update_unregistered_alert(file_path: Path, index: int, status: str) -> bool:
    try:
        with open(file_path, encoding="utf-8") as f:
            report = json.load(f)
        report["alerts"][index]["status"] = status
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return True
    except (json.JSONDecodeError, OSError, IndexError, KeyError):
        return False


@app.post("/complete_op/{item_id}")
async def complete_operation(item_id: str, request: Request):
    try:
        payload = await request.json()
        bookmaker_id = int(payload["bookmaker_id"])
        back_stake = float(payload["back_stake"])
        back_odds = float(payload["back_odds"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse(status_code=400, content={
            "status": "error", "id": item_id,
            "error": "body JSON invalido: bookmaker_id/back_stake/back_odds richiesti",
        })

    found = _find_unregistered_alert_in_files(item_id)
    if found is None:
        return JSONResponse(status_code=404, content={"status": "error", "id": item_id, "error": "alert non trovato"})
    file_path, alert_dict, index = found

    bet = alert_dict.get("betfair_open_bet", {})
    collaboratore_id = alert_dict.get("collaboratore_id")
    tipologia = alert_dict.get("tipologia", "Ricorrenti")

    app_api_client = AppApiClient()

    op_result = app_api_client.write_operazione({
        "nome": f"Bancata Betfair - {bet.get('event', 'evento')}",
        "tipologia": tipologia,
        "categoria": "Sport",
        "bookmaker_id": bookmaker_id,
        "collaboratori_ids": collaboratore_id,
        "stato": "APERTO",
        "data_apertura": date.today().isoformat(),
        "note": f"Bancata Betfair non registrata - ID scommessa {bet.get('bet_id', 'n/d')}",
    })
    if not op_result.success or not op_result.data.success:
        error = op_result.error if not op_result.success else op_result.data.message
        return JSONResponse(status_code=502, content={"status": "error", "id": item_id, "error": error})

    operazione_id = op_result.data.netcap_id
    if operazione_id is None:
        return JSONResponse(status_code=502, content={"status": "error", "id": item_id, "error": "operazione creata senza id"})

    riga_result = app_api_client.add_riga_giocata_sport(
        operazione_id=operazione_id,
        collaboratore_id=collaboratore_id,
        bookmaker_id=bookmaker_id,
        exchange_id=AppApiClient.BETFAIR_EXCHANGE_ID,
        punta=back_stake,
        contropuntata=bet.get("lay_stake"),
        quota_punta=back_odds,
        quota_contropuntata=bet.get("lay_odds"),
        profitto_book=0.0,
        profitto_banco=0.0,
    )
    if not riga_result.success:
        return JSONResponse(status_code=502, content={
            "status": "error", "id": item_id,
            "error": f"Operazione creata (id={operazione_id}) ma riga fallita: {riga_result.error}",
        })

    _update_unregistered_alert(file_path, index, "completed")
    return {"status": "completed", "id": item_id, "operazione_id": operazione_id, "riga_id": riga_result.data}
