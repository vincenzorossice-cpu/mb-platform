import time
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image, ImageChops

from orchestrator.core.result import Result


def _load_gray(image_path: str):
    """Screenshot in scala di grigi (decodificato una volta sola), o l'eccezione di lettura."""
    try:
        with Image.open(image_path) as image:
            return image.convert("L")
    except (OSError, FileNotFoundError) as exc:
        return exc


def diff_score(image_path_a: str, image_path_b: str, region: Optional[tuple] = None) -> Result[float]:
    """Punteggio di differenza 0..1 tra due screenshot, solo PIL (nessuna
    dipendenza numpy/opencv). region, se dato, e' (x1,y1,x2,y2) e limita il
    confronto a quel rettangolo."""
    img_a, img_b = _load_gray(image_path_a), _load_gray(image_path_b)
    for loaded in (img_a, img_b):
        if isinstance(loaded, Exception):
            return Result.fail(f"Cannot read image: {loaded}", "IMAGE_READ_ERROR")
    return _gray_diff(img_a, img_b, region)


def _gray_diff(img_a, img_b, region: Optional[tuple] = None) -> Result[float]:
    if region is not None:
        img_a = img_a.crop(region)
        img_b = img_b.crop(region)

    if img_a.size != img_b.size:
        return Result.fail(
            f"Image size mismatch: {img_a.size} vs {img_b.size}", "IMAGE_SIZE_MISMATCH"
        )

    diff = ImageChops.difference(img_a, img_b)
    histogram = diff.histogram()
    total_pixels = img_a.size[0] * img_a.size[1]
    if total_pixels == 0:
        return Result.ok(0.0)
    weighted_sum = sum(value * count for value, count in enumerate(histogram))
    mean_diff = weighted_sum / total_pixels
    return Result.ok(mean_diff / 255.0)


@dataclass
class StabilityResult:
    screenshot_path: str
    stable: bool
    waited_seconds: float
    captures: int = 0   # screenshot scattati (il costo di ciascuno e' waited_seconds / captures)


def wait_until_stable(
    capture_fn: Callable[[], Result], max_wait_seconds: float, poll_interval: float,
    stable_threshold: float, region: Optional[tuple] = None,
) -> Result[StabilityResult]:
    """Chiama ripetutamente capture_fn (iniettato: non tocca mai ScreenReader
    direttamente) finche' due frame consecutivi non differiscono meno di
    stable_threshold, o scade max_wait_seconds. Il timeout senza convergenza
    e' un dato per il chiamante (stable=False), non un errore."""
    start = time.monotonic()
    first = capture_fn()
    if not first.success:
        return Result.fail(first.error, first.error_code)
    previous_path = first.data
    previous_gray = _load_gray(previous_path)
    if isinstance(previous_gray, Exception):
        return Result.fail(f"Cannot read image: {previous_gray}", "IMAGE_READ_ERROR")
    captures = 1

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= max_wait_seconds:
            return Result.ok(StabilityResult(previous_path, stable=False, waited_seconds=elapsed, captures=captures))

        time.sleep(poll_interval)
        next_result = capture_fn()
        if not next_result.success:
            return Result.fail(next_result.error, next_result.error_code)
        next_path = next_result.data
        captures += 1

        next_gray = _load_gray(next_path)        # ogni frame si decodifica una volta sola (poi diventa il "precedente")
        if isinstance(next_gray, Exception):
            return Result.fail(f"Cannot read image: {next_gray}", "IMAGE_READ_ERROR")
        score_result = _gray_diff(previous_gray, next_gray, region)
        if not score_result.success:
            return Result.fail(score_result.error, score_result.error_code)

        elapsed = time.monotonic() - start
        if score_result.data < stable_threshold:
            return Result.ok(StabilityResult(next_path, stable=True, waited_seconds=elapsed, captures=captures))

        if elapsed >= max_wait_seconds:
            return Result.ok(StabilityResult(next_path, stable=False, waited_seconds=elapsed, captures=captures))

        previous_path = next_path
        previous_gray = next_gray
