"""Metriche dei modelli (performance, costo, tempo): ogni chiamata e ogni run finiscono in un file JSONL, e il
rapporto (scripts/model_report.py) le aggrega per modello. Mai eccezioni verso chi chiama: misurare non deve
rompere un run."""
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_METRICS_PATH = os.path.join("logs", "model_metrics.jsonl")
PRICES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "model_prices.json")

_run = {"id": None, "task": ""}


def metrics_path() -> str:
    return os.environ.get("MODEL_METRICS_FILE") or DEFAULT_METRICS_PATH


def start_run(task: str, label: str = "") -> str:
    """Da qui in avanti le chiamate registrate appartengono a questo run."""
    _run["id"] = f"{time.strftime('%Y%m%d_%H%M%S')}_{task}" + (f"_{label}" if label else "")
    _run["task"] = task
    return _run["id"]


def current_run() -> tuple:
    return _run["id"], _run["task"]


def _write(record: dict) -> None:
    run_id, task = current_run()
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run_id": run_id, "task": task, **record}
    try:
        path = metrics_path()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as metrics_file:
            metrics_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        logger.debug("metriche non scritte: %s", exc)


def log_call(
    model: str, purpose: str, seconds: Optional[float], tokens_in: int, tokens_out: int,
    cached_tokens: int = 0, outcome: str = "ok",
) -> None:
    _write({
        "type": "call", "model": model, "purpose": purpose,
        "seconds": None if seconds is None else round(float(seconds), 3),
        "tokens_in": int(tokens_in), "tokens_out": int(tokens_out), "cached_tokens": int(cached_tokens),
        "outcome": outcome,
    })


def log_event(kind: str, model: str = "", purpose: str = "") -> None:
    """Un fatto che interessa alla performance: risposta vuota, cella non valida, tocco scartato, rientro..."""
    _write({"type": "event", "kind": kind, "model": model, "purpose": purpose})


def log_run(**fields) -> None:
    """Esito di un intero run (navigazione o sessione di gioco)."""
    _write({"type": "run", **fields})


def load_prices(path: Optional[str] = None) -> dict:
    """{modello: {"input", "cached_input", "output"}}; vuoto se il file manca o e' rotto."""
    try:
        with open(path or PRICES_PATH, encoding="utf-8") as prices_file:
            data = json.load(prices_file)
    except (OSError, ValueError):
        return {}
    return {name: value for name, value in data.items() if isinstance(value, dict) and not name.startswith("_")}


def price_for(model: str, prices: Optional[dict] = None) -> Optional[dict]:
    """Prezzi del modello: file prezzi, altrimenti le variabili GPT_PRICE_INPUT_PER_M / GPT_PRICE_OUTPUT_PER_M."""
    entry = (load_prices() if prices is None else prices).get(model)
    if entry and entry.get("input") is not None and entry.get("output") is not None:
        return entry
    try:
        return {"input": float(os.environ["GPT_PRICE_INPUT_PER_M"]), "output": float(os.environ["GPT_PRICE_OUTPUT_PER_M"])}
    except (KeyError, ValueError):
        return None


def compute_cost(
    model: str, tokens_in: int, tokens_out: int, cached_tokens: int = 0, prices: Optional[dict] = None,
) -> Optional[float]:
    """Costo in dollari, None se il prezzo del modello e' ignoto (mai un prezzo inventato)."""
    price = price_for(model, prices)
    if price is None:
        return None
    cached = max(0, min(int(cached_tokens), int(tokens_in)))
    cached_price = price.get("cached_input")
    cached_price = price["input"] if cached_price is None else cached_price
    return ((tokens_in - cached) * price["input"] + cached * cached_price + tokens_out * price["output"]) / 1_000_000
