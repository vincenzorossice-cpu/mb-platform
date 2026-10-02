import base64
import copy
import difflib
import hashlib
import json
import logging
import os

import openai

from orchestrator.core.openai_compat import create_with_param_fallback
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


_DEFAULT_MODEL = "gpt-4o-mini"
# Modello associato a ciascun valore di DECISION_ENGINE_PROVIDER (openjev e' un altro motore, non OpenAI).
_PROVIDER_MODELS = {"gpt4o": "gpt-4o-mini", "gpt6sol": "gpt-6.1-sol"}
# Prezzi noti in dollari per milione di token (input, output). Per gli altri modelli si usano
# GPT_PRICE_INPUT_PER_M / GPT_PRICE_OUTPUT_PER_M, altrimenti il costo risulta "n/d": nessun prezzo inventato.
_KNOWN_PRICES = {"gpt-4o-mini": (0.15, 0.60)}
_VISION_MAX_TOKENS_COMPACT = 150   # modelli gpt-6.*: risposte piu' concise


def resolve_model(explicit: str = None) -> str:
    """Precedenza: argomento esplicito > GPT_MODEL > DECISION_ENGINE_PROVIDER > gpt-4o-mini."""
    if explicit:
        return explicit
    from_env = os.environ.get("GPT_MODEL", "").strip()
    if from_env:
        return from_env
    provider = os.environ.get("DECISION_ENGINE_PROVIDER", "").strip().lower()
    return _PROVIDER_MODELS.get(provider, _DEFAULT_MODEL)


def resolve_reader_model() -> str:
    """Modello (economico) per leggere saldo e puntata: GPT_READER_MODEL, default gpt-4o-mini."""
    return os.environ.get("GPT_READER_MODEL", "").strip() or _DEFAULT_MODEL


def _number(value) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _price_per_million(model: str):
    if model in _KNOWN_PRICES:
        return _KNOWN_PRICES[model]
    try:
        return (float(os.environ["GPT_PRICE_INPUT_PER_M"]), float(os.environ["GPT_PRICE_OUTPUT_PER_M"]))
    except (KeyError, ValueError):
        return None


class GPTDecisionClient(DecisionEngineClientInterface):
    """Client per il Decision Engine basato su GPT (OpenAI Chat Completions). Il modello e' configurabile
    (vedi resolve_model); di default gpt-4o-mini."""

    GPT_DECISION_UNREACHABLE = "GPT_DECISION_UNREACHABLE"
    GPT_DECISION_TIMEOUT = "GPT_DECISION_TIMEOUT"
    GPT_DECISION_HTTP_ERROR = "GPT_DECISION_HTTP_ERROR"
    GPT_DECISION_PARSE_ERROR = "GPT_DECISION_PARSE_ERROR"
    GPT_DECISION_IMAGE_READ_ERROR = "GPT_DECISION_IMAGE_READ_ERROR"

    _VISION_MAX_TOKENS = 300

    def __init__(self, api_key: str = None, model: str = None, timeout: int = 30):
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY non impostata: richiesta una API key per usare GPTDecisionClient."
            )
        self.model = resolve_model(model)
        self.client = openai.OpenAI(api_key=api_key, timeout=timeout)
        # Ultima risposta riuscita di ask / ask_with_image: stessa richiesta identica -> niente nuova chiamata.
        self._cached_text = None
        self._cached_image = None
        self._usage = {}   # modello -> {"calls", "in", "out", "cost"} (cost None = prezzo ignoto)

    def invalidate_cache(self) -> None:
        """Da chiamare dopo un'azione che cambia lo schermo: la prossima domanda e' sempre nuova."""
        self._cached_text = None
        self._cached_image = None

    def record_usage(self, response, purpose: str = "", model: str = None) -> None:
        """Registra token e costo stimato di una chiamata (anche fatta da chi usa direttamente
        `client`); mai eccezioni."""
        try:
            model = model or self.model
            usage = getattr(response, "usage", None)
            tokens_in = _number(getattr(usage, "prompt_tokens", 0))
            tokens_out = _number(getattr(usage, "completion_tokens", 0))
            prices = _price_per_million(model)
            cost = None if prices is None else (tokens_in * prices[0] + tokens_out * prices[1]) / 1_000_000
            entry = self._usage.setdefault(model, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
            entry["calls"] += 1
            entry["in"] += tokens_in
            entry["out"] += tokens_out
            entry["cost"] = None if (cost is None or entry["cost"] is None) else entry["cost"] + cost
            logger.info(
                "Chiamata GPT%s: modello=%s token in=%d out=%d costo=%s",
                f" ({purpose})" if purpose else "", model, tokens_in, tokens_out,
                "n/d (imposta GPT_PRICE_INPUT_PER_M e GPT_PRICE_OUTPUT_PER_M)" if cost is None else f"~${cost:.4f}",
            )
        except Exception as exc:  # il conteggio non deve mai rompere una chiamata
            logger.debug("record_usage fallito: %s", exc)

    def usage_summary(self) -> str:
        if not self._usage:
            return "GPT: nessuna chiamata."
        parts = []
        for model, entry in sorted(self._usage.items()):
            cost = "costo n/d" if entry["cost"] is None else f"costo stimato ~${entry['cost']:.4f}"
            parts.append(f"{model}: {entry['calls']} chiamate, {entry['in']} token in, {entry['out']} token out, {cost}")
        return "GPT - " + "; ".join(parts)

    def _vision_max_tokens(self) -> int:
        return _VISION_MAX_TOKENS_COMPACT if self.model.startswith("gpt-6") else self._VISION_MAX_TOKENS

    def ask(self, state: str, questions: dict) -> Result[dict]:
        key = hashlib.sha256(f"{self.model}|{state}|{json.dumps(questions, sort_keys=True, default=str)}".encode()).hexdigest()
        if self._cached_text is not None and self._cached_text[0] == key:
            logger.info("Chiamata GPT evitata: stessa domanda di prima (cache).")
            return copy.deepcopy(self._cached_text[1])
        result = self._ask_uncached(state, questions)
        if result.success:
            self._cached_text = (key, copy.deepcopy(result))
        return result

    def _ask_uncached(self, state: str, questions: dict) -> Result[dict]:
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
        key = None
        try:
            with open(screenshot_path, "rb") as img_file:
                digest = hashlib.sha256(img_file.read()).hexdigest()
            key = hashlib.sha256(
                f"{self.model}|{digest}|{state}|{json.dumps(questions, sort_keys=True, default=str)}|"
                f"{image_width}x{image_height}".encode()
            ).hexdigest()
        except OSError:
            pass   # l'errore di lettura lo riporta _ask_with_image_uncached come prima
        if key is not None and self._cached_image is not None and self._cached_image[0] == key:
            logger.info("Chiamata GPT evitata: stesso screenshot e stessa domanda di prima (cache).")
            return copy.deepcopy(self._cached_image[1])
        result = self._ask_with_image_uncached(screenshot_path, state, questions, image_width, image_height)
        if key is not None and result.success:
            self._cached_image = (key, copy.deepcopy(result))
        return result

    def _ask_with_image_uncached(
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
        # Azioni di navigazione di un book (scroll/done/input_text/wait): prompt dedicato, non quello delle slot.
        navigation = bool(valid_actions & _NAVIGATION_SIGNAL_ACTIONS)
        if navigation:
            system_prompt = self._build_navigation_vision_prompt(valid_actions, image_width, image_height)
        else:
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

        result = self._create_completion(messages, max_tokens=self._vision_max_tokens(), purpose="visione")
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
        if navigation:
            answers["text_to_type"] = {"choice": data.get("text_to_type") or "", "confidence": confidence}
            answers["target_description"] = {"choice": data.get("target_description") or None, "confidence": confidence}
        return Result.ok({"answers": answers})

    def _create_completion(self, messages: list, max_tokens: int = None, purpose: str = "decisione") -> Result[dict]:
        """Chiama Chat Completions in JSON mode e ritorna il JSON gia' parsato.
        Fattorizza la gestione errori condivisa tra ask() e ask_with_image()."""
        kwargs = {"max_tokens": max_tokens} if max_tokens is not None else {}
        try:
            response = create_with_param_fallback(
                self.client.chat.completions.create,
                dict(model=self.model, messages=messages, response_format={"type": "json_object"},
                     temperature=0.1, **kwargs),
            )
        except openai.APITimeoutError as exc:
            return Result.fail(f"Timeout {self.model}: {exc}", self.GPT_DECISION_TIMEOUT)
        except openai.APIConnectionError as exc:
            return Result.fail(f"{self.model} non raggiungibile: {exc}", self.GPT_DECISION_UNREACHABLE)
        except openai.APIStatusError as exc:
            return Result.fail(f"HTTP {exc.status_code} da OpenAI: {exc}", self.GPT_DECISION_HTTP_ERROR)
        except openai.OpenAIError as exc:
            return Result.fail(f"Errore OpenAI: {exc}", self.GPT_DECISION_UNREACHABLE)

        self.record_usage(response, purpose)
        try:
            content = response.choices[0].message.content
            data = json.loads(content)
        except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
            return Result.fail(f"JSON non valido da {self.model}: {exc}", self.GPT_DECISION_PARSE_ERROR)

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

    def _build_navigation_vision_prompt(self, valid_actions: set, image_width: int, image_height: int) -> str:
        """Prompt di visione per NAVIGARE un'app di book (menu, promozioni, attivazioni): non e' una slot."""
        actions_list = "|".join(sorted(valid_actions))
        states_list = "|".join(_SCREEN_STATES)
        regions_list = "|".join(_TARGET_REGIONS)
        return "\n\n".join([
            "You are an AI agent navigating a bookmaker's app or website on an Android phone via ADB. "
            "You can SEE the screen through the attached screenshot image. You are NOT playing a slot "
            "right now: your job is to reach the goal written in the message below by tapping the right "
            "menus, tabs and buttons (for example Home, Promozioni, Attiva/Aderisci, the game's name, "
            "Gioca / Prova / For fun).",

            "Look at the whole screen before choosing: popups, cookie banners or notices that cover the "
            "page must be closed first (an X or a 'Chiudi/OK' button). Never choose a button that would "
            "spend real money unless the goal explicitly says so; if the goal mentions demo/for fun, pick "
            "'Prova' or 'For fun'. If the goal is already reached, answer 'done'. If the page needs to be "
            "scrolled to find the element, answer 'scroll'. If the page is still loading, answer 'wait'.",

            "You do NOT provide pixel coordinates. Choose 'target_text': the EXACT text of one of the "
            "candidate elements listed in the message below (preferred whenever the element has a visible "
            "label). If the element you want has no text (a plain icon), choose 'target_region' instead "
            f"(a rough 3x3 location: {regions_list}) AND describe the element in 'target_description' "
            "in a few words (for example 'the hamburger menu icon, top left'). Set the unused target "
            f"fields to null. The image is {image_width}x{image_height} pixels, only for your own spatial "
            "reasoning about regions.",

            "Respond ONLY with a JSON object matching this exact schema, with no extra text:\n"
            "{\n"
            f'  "action": "{actions_list}",\n'
            f'  "screen_state": "{states_list}",\n'
            '  "target_text": "exact_text_of_a_candidate_element_or_null",\n'
            f'  "target_region": "{regions_list}_or_null",\n'
            '  "target_description": "short_description_or_null",\n'
            '  "text_to_type": "text_only_if_action_is_input_text_else_empty",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "reasoning": "brief_explanation"\n'
            "}\n\n"
            f'"action" must be one of: {", ".join(sorted(valid_actions))}. When action is "tap", exactly one of '
            '"target_text"/"target_region" must be non-null.',
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
