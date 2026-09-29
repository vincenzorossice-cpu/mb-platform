import string
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

_MIN_OUTPUT_WIDTH = 800  # ritagli piccoli vengono ingranditi: etichette leggibili per la Vision


def _font(size: int):
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def cell_label(col: int, row: int) -> str:
    """Colonna a lettere, riga a numeri partendo da 1: (0,0) -> 'A1'."""
    return f"{string.ascii_uppercase[col]}{row + 1}"


def annotate_grid(
    image_path: str, box: tuple, cols: int, rows: int, out_path: str,
) -> dict:
    """Ritaglia `box` (x1,y1,x2,y2 in pixel assoluti), lo ingrandisce se
    stretto, ci disegna sopra una griglia cols x rows con etichette ('A1',
    'B3'...) e salva il PNG in out_path. Ritorna {etichetta: (x, y)} con il
    CENTRO di ogni cella in pixel ASSOLUTI dello schermo: chi sceglie
    un'etichetta ottiene subito una coordinata valida da tappare."""
    if not (1 <= cols <= 26 and rows >= 1):
        raise ValueError(f"griglia non valida: {cols}x{rows}")

    with Image.open(image_path) as img:
        width, height = img.size
        x1, y1, x2, y2 = box
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
        if x1 >= x2 or y1 >= y2:
            raise ValueError(f"riquadro vuoto: {box}")
        crop = img.convert("RGB").crop((x1, y1, x2, y2))

    scale = max(1.0, _MIN_OUTPUT_WIDTH / crop.width)
    if scale > 1.0:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)

    cell_w_px, cell_h_px = crop.width / cols, crop.height / rows
    draw = ImageDraw.Draw(crop, "RGBA")
    font = _font(max(14, int(min(cell_w_px, cell_h_px) * 0.28)))

    span_x, span_y = x2 - x1, y2 - y1
    centers = {}
    for col in range(cols):
        for row in range(rows):
            left, top = col * cell_w_px, row * cell_h_px
            draw.rectangle(
                (left, top, left + cell_w_px, top + cell_h_px), outline=(255, 0, 0, 200), width=2,
            )
            label = cell_label(col, row)
            draw.text((left + 4, top + 2), label, fill=(255, 255, 0, 255), font=font,
                      stroke_width=2, stroke_fill=(0, 0, 0, 255))
            centers[label] = (
                round(x1 + (col + 0.5) * span_x / cols),
                round(y1 + (row + 0.5) * span_y / rows),
            )

    crop.save(out_path, "PNG")
    return centers


def refinement_box(center: tuple, cell_w: float, cell_h: float, width: int, height: int,
                   margin: float = 0.5) -> tuple:
    """Riquadro attorno a una cella scelta, allargato di `margin` celle per
    lato (l'elemento puo' stare a cavallo del bordo), limitato allo schermo."""
    cx, cy = center
    half_w, half_h = cell_w * (0.5 + margin), cell_h * (0.5 + margin)
    return (
        max(0, round(cx - half_w)), max(0, round(cy - half_h)),
        min(width, round(cx + half_w)), min(height, round(cy + half_h)),
    )
