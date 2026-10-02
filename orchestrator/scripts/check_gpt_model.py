"""Prova rapida del modello configurato (GPT_MODEL / DECISION_ENGINE_PROVIDER): accetta i nostri parametri?
Quanti token e quanto costa una chiamata? Una chiamata di testo e una con un'immagine piccola (pochi centesimi).

    python -m orchestrator.scripts.check_gpt_model
"""
import base64
import io
import json
import sys

from PIL import Image, ImageDraw

from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient
from orchestrator.core.openai_compat import create_with_param_fallback


def _small_image_base64() -> str:
    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).text((20, 40), "SALDO 49 999.90", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def main() -> int:
    try:
        client = GPTDecisionClient()
    except ValueError as exc:
        print(f"ERRORE: {exc}")
        return 1
    print(f"Modello in uso: {client.model}")

    checks = {
        "testo": [{"role": "user", "content": 'Rispondi solo con JSON: {"ok": true}'}],
        "immagine": [{"role": "user", "content": [
            {"type": "text", "text": 'Copia il testo scritto nell\'immagine. Rispondi solo con JSON: {"text": "..."}'},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_small_image_base64()}", "detail": "low"}},
        ]}],
    }
    failed = False
    for name, messages in checks.items():
        try:
            response = create_with_param_fallback(
                client.client.chat.completions.create,
                dict(model=client.model, messages=messages, response_format={"type": "json_object"},
                     temperature=0.1, max_tokens=60),
            )
            client.record_usage(response, f"prova {name}")
            print(f"[OK] {name}: {json.dumps(json.loads(response.choices[0].message.content), ensure_ascii=False)}")
        except Exception as exc:  # qualunque errore e' un dato per chi lancia la prova
            failed = True
            print(f"[ERRORE] {name}: {exc}")
    print(client.usage_summary())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
