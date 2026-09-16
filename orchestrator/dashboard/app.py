from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from orchestrator.dashboard.data_loader import load_errors, load_movimenti, load_rettifiche

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


@app.post("/approve/{item_id}")
async def approve_item(item_id: str):
    return {"status": "approved", "id": item_id}


@app.post("/reject/{item_id}")
async def reject_item(item_id: str):
    return {"status": "rejected", "id": item_id}
