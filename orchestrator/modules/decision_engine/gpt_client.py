import base64
import difflib
import json
import logging
import os

import openai

from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.interface import DecisionEngineClientInterface

logger = logging.getLogger(__name__)

_DEFAULT_VALID_ACTIONS = {"tap", "scroll", "input_text", "wait", "done"}

_SLOT_SPIN_ACTIONS = {"spin", "stop"}
# Vocabolario specifico della navigazione bookmaker: esclude "tap", che e' generico
# e condiviso anche dal flusso slot a tap libero (vedi _SLOT_TAP_ACTIONS).
_NAVIGATION_SIGNAL_ACTIONS = {"scroll", "done", "input_text", "wait"}
_SLOT_TAP_ACTIONS = {"tap", "stop"}

# Stati schermo di una slot online (da slot_anatomy.md, sez. 7): classificarli
# permette di sapere se i numeri letti (saldo/posta) sono attendibili, ed e' un
# segnale diretto e affidabile di "modale aperto" — molto piu' robusto di un
# confronto tra testi OCR letti in momenti diversi, che nella pratica sono
# leggermente diversi ad ogni lettura anche a schermo fermo (rumore OCR).
_SCREEN_STATES = (
    "loading", "intro", "idle", "spinning", "win_presentation",
    "feature", "gamble", "modal", "error", "other",
)
_TARGET_REGIONS = (
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
)


class GPTDecisionClient(DecisionEngineClientInterface):
    """Client per il Decision Engine basato su GPT-4o-mini (OpenAI Chat Completions)."""

    GPT_DECISION_UNREACHABLE = "GPT_DECISION_UNREACHABLE"
    GPT_DECISION_TIMEOUT = "GPT_DECISION_TIMEOUT"
    GPT_DECISION_HTTP_ERROR = "GPT_DECISION_HTTP_ERROR"
    GPT_DECISION_PARSE_ERROR = "GPT_DECISION_PARSE_ERROR"
    GPT_DECISION_IMAGE_READ_ERROR = "GPT_DECISION_IMAGE_READ_ERROR"

    _VISION_MAX_TOKENS = 300

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
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        result = self._create_completion(messages)
        if not result.success:
            return result
        data = result.data

        action = self._resolve_action(data.get("action"), valid_actions)
        confidence = data.get("confidence", 0.0)
        answers = {
            "action": {"choice": action, "confidence": confidence},
            "target": {"choice": data.get("target"), "confidence": confidence},
            "text_to_type": {"choice": data.get("text_to_type", ""), "confidence": confidence},
        }
        return Result.ok({"answers": answers})

    def ask_with_image(
        self, screenshot_path: str, state: str, questions: dict, image_width: int, image_height: int,
    ) -> Result[dict]:
        """Come ask(), ma invia anche lo screenshot (Vision API): GPT vede il layout
        reale invece di un dump di testi OCR. NON restituisce coordinate [x,y]
        dirette (gpt-4o-mini e' inaffidabile nel grounding spaziale, vedi
        slot_anatomy.md): sceglie invece un elemento tra i candidati OCR
        (target_text, risolto con precisione dal chiamante) o, in mancanza di
        un'etichetta, una regione grossolana su griglia 3x3 (target_region),
        oltre a classificare lo stato dello schermo (screen_state)."""
        valid_actions = set(questions.get("action", {}).get("criteria") or {}) or _SLOT_TAP_ACTIONS
        system_prompt = self._build_vision_system_prompt(valid_actions, image_width, image_height)
        user_text = self._build_user_prompt(state, questions)

        try:
            with open(screenshot_path, "rb") as img_file:
                base64_image = base64.b64encode(img_file.read()).decode("utf-8")
        except OSError as exc:
            return Result.fail(
                f"Impossibile leggere lo screenshot {screenshot_path}: {exc}", self.GPT_DECISION_IMAGE_READ_ERROR,
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
            ]},
        ]

        result = self._create_completion(messages, max_tokens=self._VISION_MAX_TOKENS)
        if not result.success:
            return result
        data = result.data

        action = self._resolve_action(data.get("action"), valid_actions)
        confidence = data.get("confidence", 0.0)
        screen_state = self._resolve_screen_state(data.get("screen_state"))

        target_text = data.get("target_text") or None
        target_region = data.get("target_region") if data.get("target_region") in _TARGET_REGIONS else None
        target = {"text": target_text, "region": target_region} if (target_text or target_region) else None

        answers = {
            "action": {"choice": action, "confidence": confidence},
            "target": {"choice": target, "confidence": confidence},
            "screen_state": {"choice": screen_state, "confidence": confidence},
        }
        return Result.ok({"answers": answers})

    def _create_completion(self, messages: list, max_tokens: int = None) -> Result[dict]:
        """Chiama Chat Completions in JSON mode e ritorna il JSON gia' parsato.
        Fattorizza la gestione errori condivisa tra ask() e ask_with_image()."""
        kwargs = {"max_tokens": max_tokens} if max_tokens is not None else {}
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.1,
                **kwargs,
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

        return Result.ok(data)

    def _build_system_prompt(self, valid_actions: set) -> str:
        lines = [
            "You are an AI agent controlling an Android phone via ADB. "
            "You MUST choose an action from the provided options only. Do not invent actions. "
            "Respond ONLY with the JSON object described below, with no extra text.",
        ]
        if "spin" in valid_actions:
            lines.append(
                "You are inside a slot machine game. Choose 'spin' to play another round or 'stop' to finish."
            )
        elif valid_actions & _NAVIGATION_SIGNAL_ACTIONS:
            lines.append(
                "You are navigating a bookmaker website. Choose 'tap' to click an element, "
                "'scroll' to see more, or 'done' when the goal is reached."
            )
        elif valid_actions == _SLOT_TAP_ACTIONS:
            lines.append(
                "You are inside a slot machine game and can tap any element visible on screen. "
                "Choose 'tap' to touch the element named in 'target' (e.g. to adjust the bet or spin), "
                "or 'stop' to finish playing."
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

    def _build_vision_system_prompt(self, valid_actions: set, image_width: int, image_height: int) -> str:
        actions_list = "|".join(sorted(valid_actions))
        states_list = "|".join(_SCREEN_STATES)
        regions_list = "|".join(_TARGET_REGIONS)
        return "\n\n".join([
            "You are an AI agent controlling a slot machine on an Android phone via ADB. "
            "You can SEE the screen through the attached screenshot image.",

            "Your goal is to: 1) set the bet to the minimum amount possible, 2) play spins until "
            "the total played reaches the target, 3) stop when done.",

            "FIRST, classify the screen state (see 'screen_state' in the schema below): "
            "loading (splash/progress bar), intro (welcome/'PLAY' screen), idle (reels stopped, "
            "nothing overlaying, safe to act), spinning (reels blurred/moving, spin button turned "
            "into stop), win_presentation (highlighted lines, a counter going up), feature (free "
            "spins / bonus game running), gamble (a red/black double-or-nothing card screen — "
            "avoid it), modal (a popup/menu covering part of the game), error (connection lost, "
            "insufficient funds), other (anything else). Numbers you read (balance, bet) are only "
            "reliable when screen_state is 'idle' or 'modal' (the modal's own text is fine to read) "
            "— during loading/spinning/win_presentation they may be mid-transition.",

            "CRITICAL: Before spinning, you MUST set the bet to the minimum amount. Common places to "
            "look for the bet selector (slot machines vary, but these patterns are widespread): a "
            "+/- pair next to the bet amount, a coin/token icon, or a small settings panel — tapping "
            "the bet AMOUNT TEXT ITSELF (e.g. 'PUNTATA €10.00', 'BET', 'STAKE') is usually the most "
            "reliable way to open bet selection. A coin icon with a '+' can ALSO mean 'add demo "
            "credits' on some slots (unrelated to the bet) — if tapping it does not lead to "
            "screen_state 'modal' and does not lower the bet, it was probably the wrong icon: try "
            "the bet amount text instead. Only after the bet is at minimum, find and tap the SPIN "
            "button (usually a large circular icon, often at the bottom of the screen — it may turn "
            "into a STOP icon while spinning). You MUST NOT spin until the bet is at minimum.",

            "TO CLOSE A BET MENU once you've selected the lowest amount: look for a CLOSE/CONFIRM/OK "
            "control, or tap outside the menu area. A menu covering the screen will hide the SPIN "
            "button, so you must close it before you can spin.",

            "If you tap something and the screen state does not change at all across turns, do not "
            "just repeat the exact same choice forever. The state message below will tell you "
            "specifically whether to try something near your last attempt (your estimate may be "
            "close but not exact) or a COMPLETELY DIFFERENT element (when that area seems unrelated "
            "to your goal). Follow that specific guidance rather than a fixed rule.",

            "You do NOT provide pixel coordinates — you are not reliable at that. Instead, choose "
            "'target_text': the EXACT text of one of the candidate elements listed in the message "
            "below (preferred whenever the element you want has a visible label). If the element you "
            "want has no text (a plain icon/button), choose 'target_region' instead: a rough location "
            f"on a 3x3 grid ({regions_list}). Set the other target field to null. The image is "
            f"{image_width}x{image_height} pixels, only for your own spatial reasoning about regions.",

            "Respond ONLY with a JSON object matching this exact schema, with no extra text:\n"
            "{\n"
            f'  "action": "{actions_list}",\n'
            f'  "screen_state": "{states_list}",\n'
            '  "target_text": "exact_text_of_a_candidate_element_or_null",\n'
            f'  "target_region": "{regions_list}_or_null",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "reasoning": "brief_explanation"\n'
            "}\n\n"
            f'"action" must be one of: {", ".join(sorted(valid_actions))}. Exactly one of "target_text"/'
            '"target_region" should be non-null when action is "tap" (never both, never neither).',
        ])

    def _resolve_screen_state(self, raw_state) -> str:
        """'other' se GPT restituisce uno stato fuori dal vocabolario noto
        (mai propagare un valore arbitrario al chiamante)."""
        if raw_state in _SCREEN_STATES:
            return raw_state
        return "other"

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
