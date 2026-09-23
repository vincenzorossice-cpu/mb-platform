import os, logging, torch
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
from transformers import AutoTokenizer, AutoModelForCausalLM

os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

class Question(BaseModel):
    type: str
    instructions: str = ""
    criteria: Optional[dict] = None
    levels: Optional[list] = None

class SystemOneRequest(BaseModel):
    state: str
    questions: dict

_model = None
_tokenizer = None

def load_model():
    global _model, _tokenizer
    if _model is not None: return
    logger.info("Caricamento Open-Jev 2B...")
    model_name = "Qwen/Qwen2.5-2B-Instruct"
    _tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    _model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=torch.float16, device_map="auto", trust_remote_code=True)
    _model.eval()
    logger.info("Modello caricato!")

def generate(prompt, max_tokens=30):
    load_model()
    inputs = _tokenizer(prompt, return_tensors="pt").to(_model.device)
    with torch.no_grad():
        outputs = _model.generate(**inputs, max_new_tokens=max_tokens, temperature=0.1, do_sample=True, pad_token_id=_tokenizer.eos_token_id)
    return _tokenizer.decode(outputs[0][inputs['input_ids'].shape[1]:], skip_special_tokens=True).strip()

@app.post("/v1/systemone")
async def system_one(request: SystemOneRequest):
    answers = {}
    for key, q_raw in request.questions.items():
        q = Question(**q_raw) if isinstance(q_raw, dict) else q_raw
        prompt = f"Context: {request.state}\n\nQuestion: {q.instructions}\n"
        if q.type == "choice" and q.criteria:
            prompt += "Options:\n"
            for opt, desc in q.criteria.items(): prompt += f"- {opt}: {desc}\n"
            prompt += "Answer with ONLY the option name:"
        elif q.type == "noul":
            prompt += "Answer with a number from 0 to 1:"
        elif q.type == "score" and q.levels:
            prompt += f"Levels: {', '.join(q.levels)}\nAnswer with ONLY the level name:"
        
        try:
            raw = generate(prompt, max_tokens=20)
            first_line = raw.strip().lower().split('\n')[0].strip('.')
            if q.type == "choice":
                choice = first_line
                if q.criteria:
                    for opt in q.criteria:
                        if opt.lower() in choice: choice = opt; break
                answers[key] = {"choice": choice, "confidence": 0.75}
            elif q.type == "noul":
                try: noul_val = max(0.0, min(1.0, float(first_line)))
                except: noul_val = 0.5
                answers[key] = {"noul": noul_val, "confidence": 0.65}
            elif q.type == "score":
                answers[key] = {"score": first_line, "confidence": 0.65}
        except Exception as e:
            logger.error(f"Error: {e}")
            answers[key] = {"choice": None, "confidence": 0.0}
    return {"answers": answers}

@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": _model is not None}

if __name__ == "__main__":
    import uvicorn
    logger.info("Avvio server Open-Jev su 0.0.0.0:8001")
    uvicorn.run(app, host="0.0.0.0", port=8001)
