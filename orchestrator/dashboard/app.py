import os

os.environ["HF_HUB_OFFLINE"] = "1"

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from orchestrator.core.device_manager import DeviceManager
from orchestrator.dashboard.data_loader import (
    DEFAULT_REPORT_DIR,
    load_bookmaker_options,
    load_errors,
    load_movimenti,
    load_pending_sport_flows,
    load_rettifiche,
    load_unregistered_operations,
)
from orchestrator.flows.sport_flow import SportFlow
from orchestrator.flows.sport_flow_handler import SportFlowHandler
from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.cascade_engine.engine import CascadeEngine
from orchestrator.modules.movement_importer.importer import MovementImporter
from orchestrator.modules.movement_importer.interface import Movement
from orchestrator.modules.multiplier_engine.calculator import MultiplierEngine
from orchestrator.modules.rag_system.rag import RAGSystem
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.signal_parser.parser import SignalParser
from orchestrator.modules.signal_parser.promo_filter import PromoFilter
from orchestrator.modules.telegram_acquirer.acquirer import PLACEHOLDER_PATTERNS, TelegramAcquirer
from orchestrator.modules.touch_injector.injector import TouchInjector

app = FastAPI(title="MB Platform HITL Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_rag_system: Optional[RAGSystem] = None


def _get_rag_system() -> RAGSystem:
    """Costruisce RAGSystem una sola volta (carica ChromaDB + embedding model,
    operazione lenta) e la cachea a livello di modulo — a differenza degli altri
    oggetti di questo file, non va ricostruita ad ogni richiesta."""
    global _rag_system
    if _rag_system is None:
        _rag_system = RAGSystem()
    return _rag_system


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    rettifiche = load_rettifiche()
    movimenti = load_movimenti()
    errori = load_errors()
    operazioni_non_registrate = load_unregistered_operations()
    bookmaker_options = load_bookmaker_options()
    pending_flows = load_pending_sport_flows()
    sport_flows = [f for f in pending_flows if f.get("category") == "sport"]
    casino_flows = [f for f in pending_flows if f.get("category") == "casino"]
    return templates.TemplateResponse(request, "index.html", {
        "rettifiche": rettifiche,
        "movimenti": movimenti,
        "errori": errori,
        "operazioni_non_registrate": operazioni_non_registrate,
        "bookmaker_options": bookmaker_options,
        "sport_flows": sport_flows,
        "casino_flows": casino_flows,
    })


def _build_sport_flow() -> SportFlow:
    """Istanzia SportFlow con i 7 moduli P5 Sport, stesso stile inline di
    AppApiClient()/MovementImporter(...) gia' usato dalle altre route. rag_system
    resta None: qui viene usato solo execute_operation, non process_signal."""
    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    multiplier_engine = MultiplierEngine()
    cascade_engine = CascadeEngine(
        screen_reader=screen_reader, touch_injector=touch_injector, multiplier_engine=multiplier_engine,
    )
    return SportFlow(
        signal_parser=SignalParser(),
        rag_system=None,
        multiplier_engine=multiplier_engine,
        touch_injector=touch_injector,
        cascade_engine=cascade_engine,
        app_api_client=AppApiClient(),
        screen_reader=screen_reader,
        device_manager=DeviceManager(),
    )


def _build_sport_flow_for_acquisition() -> SportFlow:
    """Come _build_sport_flow(), ma con RAGSystem reale (lazy-cached): a
    differenza di execute_operation, process_signal chiama sempre
    rag_system.verify_tc(), quindi qui rag_system=None non è utilizzabile."""
    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    multiplier_engine = MultiplierEngine()
    cascade_engine = CascadeEngine(
        screen_reader=screen_reader, touch_injector=touch_injector, multiplier_engine=multiplier_engine,
    )
    return SportFlow(
        signal_parser=SignalParser(),
        rag_system=_get_rag_system(),
        multiplier_engine=multiplier_engine,
        touch_injector=touch_injector,
        cascade_engine=cascade_engine,
        app_api_client=AppApiClient(),
        screen_reader=screen_reader,
        device_manager=DeviceManager(),
    )


def _build_telegram_acquirer() -> TelegramAcquirer:
    return TelegramAcquirer()


_PLACEHOLDER_LABELS = {
    "importo_ricarica": ("Importo ricarica (€)", "100"),
    "importo_bonus": ("Importo bonus (€)", "25"),
    "profitto_atteso": ("Profitto atteso (€)", "12.50"),
    "valore_atteso": ("Valore atteso (€)", "15.00"),
}


def _find_placeholders(ocr_text: str) -> list[dict]:
    """Scansiona il testo del messaggio per i placeholder noti (le stesse
    regex usate da telegram_acquirer.acquirer.merge_text_and_values, così
    rilevamento e sostituzione condividono un'unica fonte di verità) e
    produce la lista che la dashboard usa per generare gli input manuali."""
    found = []
    for key, pattern in PLACEHOLDER_PATTERNS.items():
        match = pattern.search(ocr_text)
        if not match:
            continue
        label, example = _PLACEHOLDER_LABELS[key]
        context = ocr_text[max(0, match.start() - 20): match.end() + 20].replace("\n", " ").strip()
        found.append({"key": key, "placeholder": match.group(0), "label": label, "example": example, "context": context})
    return found


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


@app.get("/open_operations/{collaboratore_id}")
async def get_open_operations(collaboratore_id: str):
    """Operazioni APERTO per un collaboratore, per popolare il dropdown
    'Collega a operazione esistente' della sezione Operazioni non registrate."""
    app_api_client = AppApiClient()
    result = app_api_client.get_open_operations(collaboratore_id)
    if not result.success:
        return JSONResponse(status_code=502, content={"status": "error", "error": result.error})
    return result.data


@app.post("/complete_op/{item_id}")
async def complete_operation(item_id: str, request: Request):
    try:
        payload = await request.json()
        bookmaker_id = int(payload["bookmaker_id"])
        back_stake = float(payload["back_stake"])
        back_odds = float(payload["back_odds"])
        op_type = payload.get("op_type", "singola")
        total_events = int(payload.get("total_events", 1))
        current_event = int(payload.get("current_event", 1))
        existing_op_id = payload.get("existing_op_id")
        existing_op_id = int(existing_op_id) if existing_op_id not in (None, "") else None
    except (ValueError, KeyError, TypeError):
        return JSONResponse(status_code=400, content={
            "status": "error", "id": item_id,
            "error": "body JSON invalido: bookmaker_id/back_stake/back_odds richiesti",
        })

    if op_type == "cascata" and not (total_events >= 2 and 1 <= current_event <= total_events):
        return JSONResponse(status_code=400, content={
            "status": "error", "id": item_id,
            "error": "cascata non valida: total_events >= 2 e 1 <= current_event <= total_events richiesti",
        })

    found = _find_unregistered_alert_in_files(item_id)
    if found is None:
        return JSONResponse(status_code=404, content={"status": "error", "id": item_id, "error": "alert non trovato"})
    file_path, alert_dict, index = found

    bet = alert_dict.get("betfair_open_bet", {})
    collaboratore_id = alert_dict.get("collaboratore_id")
    tipologia = alert_dict.get("tipologia", "Ricorrenti")

    app_api_client = AppApiClient()

    note = f"Cascata evento {current_event}/{total_events}" if op_type == "cascata" else None

    if existing_op_id is not None:
        open_ops_result = app_api_client.get_open_operations(collaboratore_id)
        if not open_ops_result.success:
            return JSONResponse(status_code=502, content={"status": "error", "id": item_id, "error": open_ops_result.error})
        matching_op = next((op for op in open_ops_result.data if op.get("id") == existing_op_id), None)
        if matching_op is None:
            return JSONResponse(status_code=400, content={
                "status": "error", "id": item_id,
                "error": f"existing_op_id {existing_op_id} non trovato tra le operazioni APERTO del collaboratore",
            })
        operazione_id = existing_op_id
        bet_gruppo_id = matching_op.get("bet_gruppo_id")
    else:
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

        bet_gruppo_id = f"cascade_{operazione_id}_{int(datetime.now().timestamp())}" if op_type == "cascata" else None

    riga_result = app_api_client.add_riga_cascata(
        operazione_id=operazione_id,
        collaboratore_id=collaboratore_id,
        bookmaker_id=bookmaker_id,
        back_stake=back_stake,
        back_odds=back_odds,
        liability=bet.get("liability"),
        lay_odds=bet.get("lay_odds"),
        bet_gruppo_id=bet_gruppo_id,
        note=note,
    )
    if not riga_result.success or not riga_result.data.success:
        error = riga_result.error if not riga_result.success else riga_result.data.message
        return JSONResponse(status_code=502, content={
            "status": "error", "id": item_id,
            "error": f"Operazione (id={operazione_id}) ma riga fallita: {error}",
        })

    _update_unregistered_alert(file_path, index, "completed")
    return {
        "status": "completed",
        "id": item_id,
        "operazione_id": operazione_id,
        "riga_id": riga_result.data.netcap_id,
        "linked_existing": existing_op_id is not None,
    }


@app.get("/pending_sport_flows")
async def get_pending_sport_flows():
    """Restituisce i flow P5 Sport pending per la dashboard."""
    return SportFlowHandler().load_pending_flows()


@app.post("/confirm_sport_flow/{hitl_item_id}")
async def confirm_sport_flow(hitl_item_id: str, request: Request):
    """Raffaele conferma -> execute_operation su tutte le identita' selezionate."""
    try:
        payload = await request.json()
        identity_ids = list(payload["identity_ids"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse(status_code=400, content={
            "status": "error", "id": hitl_item_id,
            "error": "body JSON invalido: identity_ids richiesto",
        })

    sport_flow = _build_sport_flow()
    result = sport_flow.execute_operation(hitl_item_id, identity_ids)
    if not result.success:
        return JSONResponse(status_code=502, content={"status": "error", "id": hitl_item_id, "error": result.error})

    SportFlowHandler().update_flow_status(hitl_item_id, "executed")
    return {"status": "executed", "id": hitl_item_id, "result": asdict(result.data)}


@app.post("/acquire_signal")
async def acquire_signal():
    """Acquisisce l'ultimo segnale dal canale Telegram "MB Evo Pro" via Telethon
    e identifica i placeholder (emoji animate) da far risolvere manualmente a Raffaele."""
    acquirer = _build_telegram_acquirer()
    result = acquirer.capture_signal()
    if not result.success:
        return JSONResponse(status_code=502, content={
            "success": False, "error": result.error, "error_code": result.error_code,
        })

    signal = result.data

    return {
        "success": True,
        "ocr_text": signal.ocr_text,
        "telegram_message_id": signal.message_id,
        "placeholders": _find_placeholders(signal.ocr_text),
    }


@app.post("/submit_signal")
async def submit_signal(request: Request):
    """Riceve testo OCR + valori manuali, li fonde e invia il segnale risolto
    a SignalParser -> PromoFilter -> SportFlow -> SportFlowHandler (HITL)."""
    try:
        payload = await request.json()
        ocr_text = payload["ocr_text"]
        manual_values = dict(payload.get("manual_values") or {})
        telegram_message_id = payload["telegram_message_id"]
        expected_value_eur = float(payload["expected_value_eur"])
        capital_required = float(payload["capital_required"])
        available_capital = float(payload["available_capital"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": "body JSON invalido: ocr_text/telegram_message_id/expected_value_eur/"
                     "capital_required/available_capital richiesti",
        })

    acquirer = _build_telegram_acquirer()
    resolved_text = acquirer.merge_text_and_values(ocr_text, manual_values)

    parse_result = SignalParser().parse(resolved_text, telegram_message_id)
    if not parse_result.success:
        if parse_result.error_code == "OBSCURED_VALUES":
            return JSONResponse(status_code=400, content={
                "success": False,
                "error": "Alcuni valori non sono stati risolti",
                "error_code": "OBSCURED_VALUES",
            })
        return JSONResponse(status_code=400, content={
            "success": False, "error": parse_result.error, "error_code": parse_result.error_code,
        })
    signal = parse_result.data

    filter_result = PromoFilter().filter_promo(
        signal, expected_value_eur, capital_required, available_capital,
    )
    promo = filter_result.data

    if promo.action == "skip":
        return {
            "success": True,
            "message": "Segnale scartato dal filtro promo",
            "filter_action": promo.action,
            "filter_reason": promo.reason,
        }

    sport_flow = _build_sport_flow_for_acquisition()
    flow_result = sport_flow.process_signal(resolved_text, telegram_message_id)
    if not flow_result.success:
        return JSONResponse(status_code=502, content={
            "success": False, "error": flow_result.error, "error_code": flow_result.error_code,
        })

    save_result = SportFlowHandler().save_pending_flow(flow_result.data)
    if not save_result.success:
        return JSONResponse(status_code=502, content={
            "success": False, "error": save_result.error, "error_code": save_result.error_code,
        })

    return {
        "success": True,
        "message": "Segnale inviato al flusso P5 Sport",
        "filter_action": promo.action,
        "filter_reason": promo.reason,
        "signal_book": flow_result.data.signal.book,
        "signal_type": flow_result.data.signal.bonus_type,
        "hitl_item_id": save_result.data,
        "flow_status": flow_result.data.status,
    }
