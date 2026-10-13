"""Server OCR (EasyOCR) per i telefoni: POST /ocr (multipart, campo "file") e GET /health.

Avvio: `python ocr_server.py` oppure `uvicorn ocr_server:app --port <porta>`.
Variabili d'ambiente:
  OCR_PORT  porta del server con `python ocr_server.py` (default 8001; la 8001 e' anche quella del
            Decision Engine: se girano sulla stessa macchina se ne imposta una diversa).

GPU se c'e', altrimenti CPU. EasyOCR viene importato e caricato all'avvio dell'app (non
all'import del modulo), cosi' il file si puo' importare e testare senza GPU.
"""
import io
import os
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

DEFAULT_PORT = 8001
MIN_CONFIDENCE = 0.3          # stessa soglia del client (orchestrator/modules/slot_ocr/reader.py)
LANGUAGES = ["it", "en"]

_reader = None
_use_gpu = False


def port_from_env() -> int:
    """Porta da OCR_PORT; assente o non valida -> DEFAULT_PORT."""
    raw = os.environ.get("OCR_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        print(f"OCR_PORT non valida ({raw!r}): uso {DEFAULT_PORT}")
        return DEFAULT_PORT
    return port


def cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def load_reader():
    """Carica EasyOCR: GPU se disponibile; se la GPU non parte, riprova su CPU."""
    global _reader, _use_gpu
    import easyocr

    gpu = cuda_available()
    try:
        _reader = easyocr.Reader(LANGUAGES, gpu=gpu)
        _use_gpu = gpu
    except Exception as exc:
        if not gpu:
            raise
        print(f"EasyOCR su GPU non riuscito ({type(exc).__name__}: {exc}): riprovo su CPU")
        _reader = easyocr.Reader(LANGUAGES, gpu=False)
        _use_gpu = False
    return _reader


def get_reader():
    return _reader if _reader is not None else load_reader()


@asynccontextmanager
async def lifespan(_app):
    if _reader is None:
        print("Inizializzazione EasyOCR...")
        load_reader()
        print(f"EasyOCR pronto! (GPU: {_use_gpu})")
    yield


app = FastAPI(lifespan=lifespan)


@app.post("/ocr")
async def ocr_endpoint(file: UploadFile = File(...)):
    # `async def` di proposito: le richieste dei telefoni si mettono in fila e usano la GPU una alla volta.
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Immagine vuota")
    try:
        # convert("RGB") forza anche la decodifica e normalizza PNG RGBA / palette
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise HTTPException(status_code=400, detail="Immagine non valida")

    try:
        # EasyOCR vuole un array numpy, non una PIL Image
        results = get_reader().readtext(np.array(img))
    except Exception as exc:
        print(f"OCR fallito: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"OCR fallito ({type(exc).__name__})")

    # Tipi nativi: le coordinate di EasyOCR sono numpy.int32 e la confidenza numpy.float32,
    # che FastAPI non sa serializzare in JSON.
    return {
        "texts": [
            {
                "text": str(text),
                "coords": [[int(pt[0]), int(pt[1])] for pt in bbox],
                "conf": float(conf),
            }
            for bbox, text, conf in results if float(conf) > MIN_CONFIDENCE
        ]
    }


@app.get("/health")
async def health():
    gpu = _use_gpu if _reader is not None else cuda_available()
    return {"status": "ok", "gpu": bool(gpu)}


if __name__ == "__main__":
    import uvicorn

    port = port_from_env()
    print(f"Avvio server Uvicorn su http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
