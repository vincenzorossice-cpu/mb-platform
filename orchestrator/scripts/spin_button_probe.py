"""Sonda del pulsante spin: come appare la freccia e quanto dura davvero l'animazione sul tuo telefono.

Premi SPIN a mano sul telefono e poi Invio: lo script scatta frame al massimo ritmo per alcuni secondi, salva il
ritaglio del pulsante di ogni frame in logs/spin_probe/<ora>/ e stampa, per ogni frame, quante celle chiare ha il
pulsante e quanto somiglia al frame di partenza. Alla fine riassume: da quando a quando la freccia non c'e' (= animazione).
Serve anche per il test del turbo: stessa procedura con il turbo acceso e spento, e si confrontano i secondi.

Uso:
    python -m orchestrator.scripts.spin_button_probe --device ZT322ZSJVS --slot pirots5 [--seconds 14]
    (--x/--y per indicare il punto dello spin se la slot non ha un profilo)
"""
import argparse
import json
import os
import sys
import time
from typing import Optional

from PIL import Image

from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.slot_autopilot.spin_button import HALF_PX, button_mask, similarity

_SAME_AS_START = 0.6        # somiglianza minima col frame di partenza per dire "la freccia c'e' ancora"


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip().lower()) or "unknown"


def spin_point_from_profile(slot_name: str, screen_size: tuple, profiles_dir: str = "provider_profiles") -> Optional[tuple]:
    """(x, y) dello spin dal profilo curato della slot (coordinate normalizzate), o None."""
    try:
        with open(os.path.join(profiles_dir, f"{_slug(slot_name)}.json"), encoding="utf-8") as profile_file:
            spin = json.load(profile_file)["elements"]["spin"]
        return round(float(spin["norm_x"]) * screen_size[0]), round(float(spin["norm_y"]) * screen_size[1])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def analyse(rows: list) -> dict:
    """Riassunto dai frame: rows = [(secondi, somiglianza col primo frame)]. 'assente' = freccia non riconosciuta."""
    absent = [seconds for seconds, similarity_value in rows if similarity_value < _SAME_AS_START]
    if not absent:
        return {"absent_from": None, "absent_to": None, "duration": None, "back_at": None}
    last_absent = absent[-1]
    later = [seconds for seconds, similarity_value in rows if seconds > last_absent and similarity_value >= _SAME_AS_START]
    return {"absent_from": absent[0], "absent_to": last_absent, "duration": last_absent - absent[0],
            "back_at": later[0] if later else None}


def _crop(path: str, x: int, y: int, target: str) -> None:
    with Image.open(path) as image:
        box = (max(0, x - HALF_PX), max(0, y - HALF_PX), min(image.width, x + HALF_PX), min(image.height, y + HALF_PX))
        image.convert("RGB").crop(box).save(target)


def main(argv=None, input_fn=input) -> int:
    parser = argparse.ArgumentParser(description="Sonda del pulsante spin (freccia) e durata dell'animazione.")
    parser.add_argument("--device", required=True)
    parser.add_argument("--slot", default="")
    parser.add_argument("--seconds", type=float, default=14.0)
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--capture-mode", default=None, help="png | raw | rawgz | auto (default: ADB_CAPTURE_MODE)")
    args = parser.parse_args(argv)

    reader = ScreenReader(capture_mode=args.capture_mode)
    first = reader._screenshot(args.device)
    if not first.success:
        print(f"Screenshot fallito: {first.error}")
        return 1
    with Image.open(first.data) as image:
        size = image.size
    point = (args.x, args.y) if args.x is not None and args.y is not None else spin_point_from_profile(args.slot, size)
    if point is None:
        print("Punto dello spin sconosciuto: indica --slot di una slot con profilo oppure --x e --y.")
        return 1
    print(f"Punto dello spin: {point} (schermo {size[0]}x{size[1]}). Il telefono deve mostrare la slot ferma, con la freccia.")
    input_fn("Premi SPIN sul telefono e SUBITO Invio qui... ")

    started = time.monotonic()
    out_dir = os.path.join("logs", "spin_probe", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    reference = button_mask(first.data, *point)
    rows = []
    index = 0
    print(f"{'t (s)':>6} {'celle chiare':>13} {'somiglianza col via':>20}")
    while time.monotonic() - started < args.seconds:
        shot = reader._screenshot(args.device)
        elapsed = time.monotonic() - started
        if not shot.success:
            continue
        mask = button_mask(shot.data, *point)
        similarity_value = similarity(reference, mask) if reference is not None and mask is not None else 0.0
        rows.append((elapsed, similarity_value))
        print(f"{elapsed:6.1f} {int(mask.sum()) if mask is not None else -1:13d} {similarity_value:20.2f}")
        _crop(shot.data, point[0], point[1], os.path.join(out_dir, f"crop_{index:02d}_t{elapsed:04.1f}.png"))
        index += 1

    summary = analyse(rows)
    print()
    if summary["duration"] is None:
        print("La freccia e' sempre rimasta riconoscibile: o lo spin non e' partito o la freccia non sparisce "
              "durante l'animazione (in quel caso la freccia non si puo' usare su questa slot).")
    else:
        back = f", torna a {summary['back_at']:.1f} s" if summary["back_at"] is not None else ", non e' tornata nel tempo di misura"
        print(f"Freccia assente da {summary['absent_from']:.1f} s a {summary['absent_to']:.1f} s "
              f"(animazione ~{summary['duration']:.1f} s{back}).")
    print(f"Ritagli salvati in {out_dir}. Catture: {len(rows)} in {args.seconds:.0f} s "
          f"({args.seconds / max(1, len(rows)):.2f} s a scatto, modo {reader.last_method}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
