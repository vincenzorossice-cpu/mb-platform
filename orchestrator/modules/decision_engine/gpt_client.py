import difflib
import json
import logging
import os

import openai

from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.interface import DecisionEngineClientInterface

logger = logging.getLogger(__name__)

_DEFAULT_VALID_ACTIONS = {"tap", "scroll", "input_text", "wait", "done"}

_SLOT_ACTIONS = {"spin", "stop"}
_NAVIGATION_ACTIONS = {"tap", "scroll", "done"}


class GPTDecisionClient(DecisionEngineClientInterface):
    """Client per il Decision Engine basato su GPT-4o-mini (OpenAI Chat Completions)."""

    GPT_DECISION_UNREACHABLE = "GPT_DECISION_UNREACHABLE"
    GPT_DECISION_TIMEOUT = "GPT_DECISION_TIMEOUT"
    GPT_DECISION_HTTP_ERROR = "GPT_DECISION_HTTP_ERROR"
    GPT_DECISION_PARSE_ERROR = "GPT_DECISION_PARSE_ERROR"

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
        valid_actions = set(questions.get("action", {}).get("criteria") or {}) or _DEFAULT_VALID_ACTIONS
        system_prompt = self._build_system_prompt(valid_actions)
        user_prompt = self._build_user_prompt(state, questions)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
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

        action = self._resolve_action(data.get("action"), valid_actions)
        confidence = data.get("confidence", 0.0)
        answers = {
            "action": {"choice": action, "confidence": confidence},
            "target": {"choice": data.get("target"), "confidence": confidence},
            "text_to_type": {"choice": data.get("text_to_type", ""), "confidence": confidence},
        }
        return Result.ok({"answers": answers})

    def _build_system_prompt(self, valid_actions: set) -> str:
        lines = [
            "You are an AI agent controlling an Android phone via ADB. "
            "You MUST choose an action from the provided options only. Do not invent actions. "
            "Respond ONLY with the JSON object described below, with no extra text.",
        ]
        if valid_actions & _SLOT_ACTIONS:
            lines.append(
                "You are inside a slot machine game. Choose 'spin' to play another round or 'stop' to finish."
            )
        if valid_actions & _NAVIGATION_ACTIONS:
            lines.append(
                "You are navigating a bookmaker website. Choose 'tap' to click an element, "
                "'scroll' to see more, or 'done' when the goal is reached."
            )

        actions_list = "|".join(sorted(valid_actions))
        lines.append(
            "Respond SOLELY with a JSON object matching this exact schema:\n"
            "{\n"
            f'  "action": "{actions_list}",\n'
            '  "target": "exact_text_of_the_element",\n'
            '  "text_to_type": "optional_text_to_enter",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "reasoning": "brief_explanation"\n'
            "}\n\n"
            f"Rules:\n"
            f'- "action" must be one of: {", ".join(sorted(valid_actions))}.\n'
            '- "target" must be the exact text of one of the listed candidate elements, or a '
            "direction (up/down/left/right) if action is scroll.\n"
            '- "confidence" reflects how sure you are of the choice (0.0 = no confidence, 1.0 = fully confident).'
        )
        return "\n\n".join(lines)

    def _resolve_action(self, raw_action, valid_actions: set) -> str:
        """Se l'azione di GPT non e' tra quelle valide, prova un fuzzy match e poi
        ricade su un fallback sicuro (mai un'azione inventata al chiamante)."""
        if raw_action in valid_actions:
            return raw_action

        candidates = sorted(valid_actions)
        match = difflib.get_close_matches(str(raw_action).lower(), [a.lower() for a in candidates], n=1, cutoff=0.6)
        if match:
            resolved = next(a for a in candidates if a.lower() == match[0])
        elif "wait" in valid_actions:
            resolved = "wait"
        elif "stop" in valid_actions:
            resolved = "stop"
        else:
            resolved = candidates[0]

        logger.warning(
            "GPT-4o-mini ha restituito un'azione non valida (%r): rimappata a %r (opzioni valide: %s)",
            raw_action, resolved, candidates,
        )
        return resolved

    def _build_user_prompt(self, state: str, questions: dict) -> str:
        target_question = questions.get("target", {})
        candidates = list(target_question.get("criteria", {}).keys())
        parts = [state]
        if candidates:
            parts.append("Elementi cliccabili disponibili (target validi):\n" + "\n".join(f"- {c}" for c in candidates))
        return "\n\n".join(parts)
