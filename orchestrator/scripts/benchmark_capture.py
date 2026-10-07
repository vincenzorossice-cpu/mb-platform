"""Quanto costa uno screenshot ADB sul tuo telefono: PNG compresso dal telefono ('screencap -p', com'e' oggi) contro
grezzo ('screencap', senza compressione sul telefono), piu' la decodifica sul PC.

Uso:
    python -m orchestrator.scripts.benchmark_capture --device ZT322ZSJVS --n 5
"""
import argparse
import os
import statistics
import sys
import time
from typing import Optional

from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.slot_autopilot.frame_diff import diff_score


def measure_capture(reader: ScreenReader, device_id: str, n: int) -> dict:
    """Secondi per cattura (la prima, di riscaldamento, non conta), dimensione media del file, catture fallite."""
    reader._screenshot(device_id)
    seconds, sizes, failures = [], [], 0
    for _ in range(n):
        started = time.monotonic()
        result = reader._screenshot(device_id)
        elapsed = time.monotonic() - started
        if not result.success:
            failures += 1
            continue
        seconds.append(elapsed)
        sizes.append(os.path.getsize(result.data))
    return {
        "mean": statistics.mean(seconds) if seconds else None, "min": min(seconds) if seconds else None,
        "max": max(seconds) if seconds else None, "failures": failures,
        "mb": statistics.mean(sizes) / 1_000_000 if sizes else None,
        "raw_failures": reader.capture_stats["raw_failures"],
    }


def measure_diff(path: str, n: int) -> Optional[float]:
    """Secondi per confrontare due frame (decodifica + differenza) sul PC."""
    started = time.monotonic()
    for _ in range(n):
        if not diff_score(path, path).success:
            return None
    return (time.monotonic() - started) / n


def recommendation(png: dict, raw: dict) -> str:
    if png["mean"] is None:
        return "Nessuna cattura PNG riuscita: controlla il telefono (adb devices)."
    if raw["mean"] is None or raw["raw_failures"] > 0:
        return "Il formato grezzo non funziona su questo telefono: resta ADB_CAPTURE_MODE=png."
    if raw["mean"] < 0.8 * png["mean"]:
        return (f"Il grezzo e' piu' veloce ({raw['mean']:.2f} s contro {png['mean']:.2f} s a scatto): "
                f"imposta $env:ADB_CAPTURE_MODE = \"raw\" prima di lanciare l'autopilot.")
    return "Nessun vantaggio netto col grezzo: resta ADB_CAPTURE_MODE=png."


def _row(label: str, data: dict) -> str:
    if data["mean"] is None:
        return f"  {label:<10} nessuna cattura riuscita ({data['failures']} fallite)"
    return (f"  {label:<10} media {data['mean']:.2f} s  (min {data['min']:.2f}, max {data['max']:.2f})  "
            f"file {data['mb']:.1f} MB  fallite {data['failures']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Misura il costo degli screenshot ADB (png contro grezzo).")
    parser.add_argument("--device", required=True)
    parser.add_argument("--n", type=int, default=5, help="catture per metodo (default 5)")
    args = parser.parse_args(argv)

    png_reader = ScreenReader(capture_mode="png")
    raw_reader = ScreenReader(capture_mode="raw")
    png = measure_capture(png_reader, args.device, args.n)
    raw = measure_capture(raw_reader, args.device, args.n)
    print("Costo di uno screenshot sul tuo telefono:")
    print(_row("png (oggi)", png))
    print(_row("grezzo", raw))
    last = png_reader._screenshot(args.device)
    if last.success:
        diff = measure_diff(last.data, 3)
        if diff is not None:
            print(f"  decodifica + confronto di due frame sul PC: {diff:.2f} s")
    print(recommendation(png, raw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
