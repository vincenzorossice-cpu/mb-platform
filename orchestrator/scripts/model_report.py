"""Rapporto su performance, costo e tempo dei modelli, dal file di metriche (logs/model_metrics.jsonl).

Uso:
    python -m orchestrator.scripts.model_report [--model gpt-6.1-sol] [--task navigazione_casino]
        [--since 2026-10-02] [--file logs/model_metrics.jsonl] [--csv rapporto.csv]
"""
import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from typing import Optional

from orchestrator.core import model_metrics


def load_records(path: str, since: Optional[str] = None) -> list:
    records = []
    try:
        with open(path, encoding="utf-8") as metrics_file:
            for line in metrics_file:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and (since is None or str(record.get("ts", "")) >= since):
                    records.append(record)
    except OSError:
        pass
    return records


def percentile(values: list, fraction: float) -> Optional[float]:
    """Nearest-rank (es. 0.95)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def _cost(call: dict, prices: Optional[dict]) -> Optional[float]:
    return model_metrics.compute_cost(
        call.get("model", ""), call.get("tokens_in", 0), call.get("tokens_out", 0), call.get("cached_tokens", 0), prices,
    )


def _sum_costs(costs: list) -> Optional[float]:
    return None if not costs or any(cost is None for cost in costs) else sum(costs)


def aggregate(records: list, model: Optional[str] = None, task: Optional[str] = None, prices: Optional[dict] = None) -> dict:
    """{'calls': {(modello, scopo): {...}}, 'models': {modello: {...}}, 'events': {(modello, tipo): n},
    'runs': {(compito, modello): {...}}}. Il filtro per modello vale per le chiamate/eventi e per i run."""
    prices = model_metrics.load_prices() if prices is None else prices
    calls = [r for r in records if r.get("type") == "call" and (task is None or r.get("task") == task)]
    events = [r for r in records if r.get("type") == "event" and (task is None or r.get("task") == task)]
    runs = [r for r in records if r.get("type") == "run" and (task is None or r.get("task") == task)]
    if model:
        calls = [r for r in calls if r.get("model") == model]
        events = [r for r in events if r.get("model") == model]
        runs = [r for r in runs if r.get("model") == model]

    by_purpose = defaultdict(list)
    by_model = defaultdict(list)
    for call in calls:
        by_purpose[(call.get("model"), call.get("purpose"))].append(call)
        by_model[call.get("model")].append(call)

    purposes = {}
    for key, items in by_purpose.items():
        seconds = [c["seconds"] for c in items if c.get("seconds") is not None and c.get("outcome", "ok") == "ok"]
        purposes[key] = {
            "calls": len(items), "failures": sum(1 for c in items if c.get("outcome", "ok") != "ok"),
            "tokens_in": sum(c.get("tokens_in", 0) for c in items), "tokens_out": sum(c.get("tokens_out", 0) for c in items),
            "cost": _sum_costs([_cost(c, prices) for c in items if c.get("outcome", "ok") == "ok"]),
            "sec_avg": statistics.mean(seconds) if seconds else None,
            "sec_median": statistics.median(seconds) if seconds else None,
            "sec_p95": percentile(seconds, 0.95),
        }

    models = {}
    for name, items in by_model.items():
        ok = [c for c in items if c.get("outcome", "ok") == "ok"]
        cost = _sum_costs([_cost(c, prices) for c in ok])
        models[name] = {
            "calls": len(items), "failures": len(items) - len(ok),
            "tokens_in": sum(c.get("tokens_in", 0) for c in items), "tokens_out": sum(c.get("tokens_out", 0) for c in items),
            "cached": sum(c.get("cached_tokens", 0) for c in items), "cost": cost,
            "cost_per_call": None if cost is None or not ok else cost / len(ok),
        }

    event_counts = defaultdict(int)
    for event in events:
        event_counts[(event.get("model"), event.get("kind"))] += 1

    cost_by_run = defaultdict(list)
    for call in [r for r in records if r.get("type") == "call"]:
        if call.get("outcome", "ok") == "ok":
            cost_by_run[call.get("run_id")].append(_cost(call, prices))
    grouped_runs = defaultdict(list)
    for run in runs:
        grouped_runs[(run.get("task"), run.get("model"))].append(run)
    run_stats = {}
    for key, items in grouped_runs.items():
        verified = [r.get("verified_by_user") for r in items if r.get("verified_by_user") is not None]
        # un run senza chiamate non ha speso nulla (0), un run con un prezzo mancante ha costo ignoto (None)
        run_costs = [
            _sum_costs(cost_by_run[r.get("run_id")]) if cost_by_run.get(r.get("run_id")) else 0.0 for r in items
        ]
        entry = {
            "runs": len(items),
            "seconds_avg": statistics.mean([r["seconds"] for r in items if r.get("seconds") is not None] or [0]) or None,
            "cost_avg": None if not run_costs or any(c is None for c in run_costs) else statistics.mean(run_costs),
            "operator_interventions": sum(r.get("operator_interventions", 0) or 0 for r in items),
            "supervisor_calls": sum(r.get("supervisor_calls", 0) or 0 for r in items),
        }
        if items and items[0].get("type_of_run") == "navigazione":
            entry["done_declared"] = sum(1 for r in items if r.get("done_declared"))
            entry["verified_ok"] = sum(1 for v in verified if v)
            entry["verified_total"] = len(verified)
            entry["steps_avg"] = statistics.mean([r.get("steps", 0) for r in items])
        else:
            entry["failed"] = sum(1 for r in items if r.get("failed"))
            entry["spins_avg"] = statistics.mean([r.get("spins", 0) or 0 for r in items])
            per_spin = [r["seconds_per_spin"] for r in items if r.get("seconds_per_spin") is not None]
            entry["seconds_per_spin_avg"] = statistics.mean(per_spin) if per_spin else None
        run_stats[key] = entry
    return {"calls": purposes, "models": models, "events": dict(event_counts), "runs": run_stats}


def _fmt(value, spec: str = ".1f", missing: str = "n/d") -> str:
    return missing if value is None else format(value, spec)


def format_report(result: dict) -> str:
    lines = []
    lines.append("=== PERFORMANCE (esito dei run) ===")
    if not result["runs"]:
        lines.append("  nessun run registrato")
    for (task, model), run in sorted(result["runs"].items(), key=lambda item: str(item[0])):
        if "done_declared" in run:
            verified = (f"verificati dall'utente {run['verified_ok']}/{run['verified_total']}"
                        if run["verified_total"] else "nessuna verifica dell'utente")
            detail = f"'done' dichiarato {run['done_declared']}/{run['runs']}, {verified}, passi medi {run['steps_avg']:.1f}"
        else:
            detail = (f"sessioni fallite {run['failed']}/{run['runs']}, spin medi {run['spins_avg']:.1f}, "
                      f"secondi per spin {_fmt(run['seconds_per_spin_avg'])}")
        lines.append(f"  {task} | {model}: {run['runs']} run, {detail}, interventi operatore {run['operator_interventions']}, "
                     f"chiamate supervisore {run['supervisor_calls']}")
    if result["events"]:
        lines.append("  eventi: " + ", ".join(f"{model}/{kind} x{count}" for (model, kind), count in sorted(result["events"].items(), key=str)))

    lines.append("")
    lines.append("=== COSTO ===")
    for model, entry in sorted(result["models"].items(), key=str):
        cost_text = "n/d (prezzo mancante)" if entry["cost"] is None else f"${entry['cost']:.4f} (${_fmt(entry['cost_per_call'], '.4f')} a chiamata)"
        lines.append(f"  {model}: {entry['calls']} chiamate, token in {entry['tokens_in']} (di cui in cache {entry['cached']}), "
                     f"out {entry['tokens_out']}, costo {cost_text}")
    for (task, model), run in sorted(result["runs"].items(), key=lambda item: str(item[0])):
        lines.append(f"  costo medio per run ({task} | {model}, tutti i modelli del run): "
                     + ("n/d" if run["cost_avg"] is None else f"${run['cost_avg']:.4f}"))

    lines.append("")
    lines.append("=== TEMPO (secondi per chiamata) ===")
    for (model, purpose), entry in sorted(result["calls"].items(), key=str):
        lines.append(f"  {model} | {purpose}: {entry['calls']} chiamate, media {_fmt(entry['sec_avg'])} s, mediana "
                     f"{_fmt(entry['sec_median'])} s, 95o percentile {_fmt(entry['sec_p95'])} s"
                     + (f", FALLITE {entry['failures']}" if entry["failures"] else ""))
    for (task, model), run in sorted(result["runs"].items(), key=lambda item: str(item[0])):
        lines.append(f"  durata media di un run ({task} | {model}): {_fmt(run['seconds_avg'])} s")
    return "\n".join(lines)


def write_csv(result: dict, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["modello", "scopo", "chiamate", "fallite", "token_in", "token_out", "costo_usd",
                         "sec_media", "sec_mediana", "sec_p95"])
        for (model, purpose), entry in sorted(result["calls"].items(), key=str):
            writer.writerow([model, purpose, entry["calls"], entry["failures"], entry["tokens_in"], entry["tokens_out"],
                             entry["cost"], entry["sec_avg"], entry["sec_median"], entry["sec_p95"]])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Performance, costo e tempo dei modelli dal file di metriche.")
    parser.add_argument("--file", default=None, help="File JSONL (default: logs/model_metrics.jsonl o MODEL_METRICS_FILE).")
    parser.add_argument("--model", default=None)
    parser.add_argument("--task", default=None, help="navigazione_casino | slot_autopilot")
    parser.add_argument("--since", default=None, help="Solo righe dalla data AAAA-MM-GG.")
    parser.add_argument("--csv", default=None, help="Scrive anche il dettaglio per modello e scopo in un CSV.")
    args = parser.parse_args(argv)
    path = args.file or model_metrics.metrics_path()
    records = load_records(path, args.since)
    if not records:
        print(f"Nessun dato in {path}.")
        return 1
    result = aggregate(records, args.model, args.task)
    print(format_report(result))
    if args.csv:
        write_csv(result, args.csv)
        print(f"\nCSV scritto in {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
