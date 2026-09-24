import json
import os

import openai

from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.interface import DecisionEngineClientInterface

_VALID_ACTIONS = {"tap", "scroll", "input_text", "wait", "done"}

_SYSTEM_PROMPT = """Sei un agente che naviga interfacce di app di bookmaker su Android tramite ADB.
Ad ogni turno ricevi lo stato corrente dello schermo (testi degli elementi visibili, obiettivo,
storico delle azioni) e la lista degli elementi su cui puoi agire. Devi decidere una singola
azione per avvicinarti all'obiettivo.

Rispondi SOLO con un oggetto JSON con questo schema esatto, senza testo aggiuntivo:
{
  "action": "tap|scroll|input_text|wait|done",
  "target": "testo_esatto_dell_elemento",
  "text_to_type": "testo_da_inserire_opzionale",
  "confidence": 0.0-1.0,
  "reasoning": "breve_spiegazione"
}

Regole:
- "action" deve essere una tra: tap, scroll, input_text, wait, done.
- "target" deve essere il testo esatto di uno degli elementi candidati elencati, oppure una
  direzione (up/down/left/right) se action è scroll.
- Usa "done" solo quando l'obiettivo e' stato raggiunto.
- "confidence" riflette quanto sei sicuro della scelta (0.0 = nessuna sicurezza, 1.0 = massima)."""


class GPTDecisionClient(DecisionEngineClientInterface):
    """Client per il Decision Engine basato su GPT-4o-mini (OpenAI Chat Completions)."""

    GPT_DECISION_UNREACHABLE = "GPT_DECISION_UNREACHABLE"
    GPT_DECISION_TIMEOUT = "GPT_DECISION_TIMEOUT"
    GPT_DECISION_HTTP_ERROR = "GPT_DECISION_HTTP_ERROR"
    GPT_DECISION_PARSE_ERROR = "GPT_DECISION_PARSE_ERROR"
    GPT_DECISION_INVALID_ACTION = "GPT_DECISION_INVALID_ACTION"

    def __init__(self, api_key: str = None, model: str = "gpt-4o-mini", timeout: int = 30):
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY non impostata: richiesta una API key per usare GPTDecisionClient."
            )
        self.model = model
        self.client = openai.OpenAI(api_key=api_key, timeout=timeout)

    def ask(self, state: str, questions: dict) -> Result[dict]:
        """Chiama Chat Completions in JSON mode e mappa la risposta nel formato answers atteso."""
        user_prompt = self._build_user_prompt(state, questions)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
        except openai.APITimeoutError as exc:
            return Result.fail(f"Timeout GPT-4o-mini: {exc}", self.GPT_DECISION_TIMEOUT)
        except openai.APIConnectionError as exc:
            return Result.fail(f"GPT-4o-mini non raggiungibile: {exc}", self.GPT_DECISION_UNREACHABLE)
        except openai.APIStatusError as exc:
            return Result.fail(f"HTTP {exc.status_code} da OpenAI: {exc}", self.GPT_DECISION_HTTP_ERROR)
        except openai.OpenAIError as exc:
            return Result.fail(f"Errore OpenAI: {exc}", self.GPT_DECISION_UNREACHABLE)

        try:
            content = response.choices[0].message.content
            data = json.loads(content)
        except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
            return Result.fail(f"JSON non valido da GPT-4o-mini: {exc}", self.GPT_DECISION_PARSE_ERROR)

        action = data.get("action")
        if action not in _VALID_ACTIONS:
            return Result.fail(
                f"Azione non valida restituita da GPT-4o-mini: {action!r}",
                self.GPT_DECISION_INVALID_ACTION,
            )

        confidence = data.get("confidence", 0.0)
        answers = {
            "action": {"choice": action, "confidence": confidence},
            "target": {"choice": data.get("target"), "confidence": confidence},
            "text_to_type": {"choice": data.get("text_to_type", ""), "confidence": confidence},
        }
        return Result.ok({"answers": answers})

    def _build_user_prompt(self, state: str, questions: dict) -> str:
        target_question = questions.get("target", {})
        candidates = list(target_question.get("criteria", {}).keys())
        parts = [state]
        if candidates:
            parts.append("Elementi cliccabili disponibili (target validi):\n" + "\n".join(f"- {c}" for c in candidates))
        return "\n\n".join(parts)
