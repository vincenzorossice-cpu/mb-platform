import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from orchestrator.dashboard.data_loader import DEFAULT_REPORT_DIR, load_errors, load_movimenti, load_rettifiche
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
    return templates.TemplateResponse(request, "index.html", {
        "rettifiche": rettifiche,
        "movimenti": movimenti,
        "errori": errori,
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
