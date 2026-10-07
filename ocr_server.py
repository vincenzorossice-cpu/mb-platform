from fastapi import FastAPI, UploadFile, File
import easyocr
import io
from PIL import Image

print("Inizializzazione EasyOCR...")
app = FastAPI()
reader = easyocr.Reader(['it', 'en'], gpu=True)
print("EasyOCR pronto!")

@app.post("/ocr")
async def ocr_endpoint(file: UploadFile = File(...)):
    image_bytes = await file.read()
    img = Image.open(io.BytesIO(image_bytes))
    results = reader.readtext(img)
    return {
        "texts": [
            {"text": text, "coords": bbox, "conf": conf}
            for bbox, text, conf in results if conf > 0.3
        ]
    }

@app.get("/health")
async def health():
    return {"status": "ok", "gpu": True}

if __name__ == "__main__":
    import uvicorn
    print("Avvio server Uvicorn su http://0.0.0.0:8001")
    uvicorn.run(app, host="0.0.0.0", port=8001)
