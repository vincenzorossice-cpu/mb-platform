import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_ocr.vision_reader import _encode_reduced, _to_amount

logger = logging.getLogger(__name__)

_PURPOSE = "supervisore"
_SCREEN_STATES = ("idle", "modal", "spinning", "win_presentation", "feature", "gamble", "loading", "intro", "error", "other")
# Il supervisore non gioca mai: una descrizione che parla di spin non viene toccata.
_SPIN_WORDS = ("spin", "gira", "play", "start", "auto")
_SETTLE_SECONDS = 1.5
_SCREEN_CHANGED_DIFF = 0.02

_SYSTEM_PROMPT = (
    "You supervise an automatic player of a slot machine game on an Android phone. You see ONE screenshot. "
    "Answer ONLY with JSON: "
    '{"screen_state": "' + "|".join(_SCREEN_STATES) + '", "credit_text": <string|null>, "bet_text": <string|null>, '
    '"action": "none|close", "target_description": <string|null>, "spin_ready": <true|false>, "in_game": <true|false>, '
    '"reasoning": <short string>}. '
    "screen_state: idle = reels stopped and nothing covers the game; modal = a menu/popup/panel covers part of the "
    "game (bet selection menu, settings, info, paytable); spinning; win_presentation; loading; intro; error; other. "
    "credit_text = the player's balance/credit; bet_text = the current TOTAL bet of one spin. COPY each amount exactly "
    "as written on screen (keep spaces, dots and commas), without the currency symbol; null if you cannot read it with "
    "certainty. IGNORE jackpot and progressive meters (labels such as JACKPOT, MINI, MIDI, MAXI, GRAND or any large "
    "number that keeps changing): they are NOT the credit and NOT the bet. The bet and the credit are never the same "
    "number. action: 'close' ONLY if a menu or popup is open that the player did not ask for and is hiding the game; "
    "then target_description = the close control (for example 'the X at the top right of the panel'). Otherwise "
    "'none'. spin_ready = true if the spin button is visible and the game is waiting for the player, EVEN IF a win "
    "overlay, a big win number or an animation is shown over the reels; false during a spin in progress, a bonus / "
    "free-spins feature, a loading screen or an open menu. in_game = true if the slot game itself (reels, spin "
    "button) is on screen, even with a menu or overlay over it; false if the screen shows instead a 'session "
    "expired' / disconnected / timeout message, the casino lobby or any other website page, a login page or a "
    "browser error page. NEVER suggest spinning or tapping the spin button."
)


@dataclass
class Observation:
    screen_state: str
    bet: Optional[Decimal]
    credit: Optional[Decimal]
    action: str
    target_description: Optional[str]
    reasoning: str
    spin_ready: Optional[bool] = None
    in_game: Optional[bool] = None


@dataclass
class Assist:
    """outcome: 'at_min' (la puntata letta e' uguale al minimo noto), 'closed' (chiuso un menu non voluto),
    'nothing' (niente da fare per il supervisore: tocca all'operatore)."""
    outcome: str
    bet: Optional[Decimal] = None


class SlotSupervisor:
    """Sol guarda lo schermo intero quando i controlli locali si bloccano, PRIMA di chiamare l'operatore:
    dice in che stato e', legge puntata e saldo ignorando i contatori jackpot e, se un menu non voluto copre il
    gioco, indica la X da toccare. Non gioca mai e non decide la puntata: aiuta solo a verificare e a uscire dai
    menu. Un numero massimo di chiamate per sessione tiene sotto controllo il costo."""

    def __init__(
        self, gpt_client, screen_reader, touch_injector,
        describe: Optional[Callable[[str, LocateContext], Optional[tuple]]] = None,
        max_calls: int = 6, max_interventions: int = 3, settle_seconds: float = _SETTLE_SECONDS,
        max_lost_checks: int = 6, max_menu_checks: int = 4,
    ):
        self.gpt_client = gpt_client
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.describe = describe
        self.max_calls = max_calls
        self.max_interventions = max_interventions
        self.settle_seconds = settle_seconds
        self.calls = 0
        self.interventions = 0
        self.max_lost_checks = max_lost_checks
        self.lost_checks = 0
        self.max_menu_checks = max_menu_checks
        self.menu_checks = 0

    def reset(self) -> None:
        self.calls = 0
        self.interventions = 0
        self.lost_checks = 0
        self.menu_checks = 0

    def observe(self, screenshot_path: str, goal: str = "", use_budget: bool = True) -> Optional[Observation]:
        """Una chiamata al modello decisionale sullo screenshot; None se il budget e' finito o la risposta manca.
        use_budget=False: la chiamata ha un budget proprio (vedi check_in_game)."""
        if use_budget:
            if self.calls >= self.max_calls:
                logger.warning("supervisore: budget di %d chiamate esaurito.", self.max_calls)
                return None
            self.calls += 1
        try:
            encoded = _encode_reduced(screenshot_path)
        except OSError as exc:
            logger.warning("supervisore: screenshot illeggibile: %s", exc)
            return None
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"Context: {goal or 'check the screen'}."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ]},
        ]
        max_tokens = max(getattr(self.gpt_client, "_VISION_MAX_TOKENS", 300), 300)
        result = self.gpt_client._create_completion(messages, max_tokens=max_tokens, purpose=_PURPOSE)
        if not result.success or not isinstance(result.data, dict):
            logger.warning("supervisore: nessuna risposta utilizzabile (%s).", getattr(result, "error", None))
            return None
        data = result.data
        credit = _decimal(_to_amount(data.get("credit_text")))
        bet = _decimal(_to_amount(data.get("bet_text")))
        if bet is not None and credit is not None and bet >= credit:
            bet = None      # puntata e credito non sono lo stesso numero, e la puntata non supera il credito
        state = data.get("screen_state") if data.get("screen_state") in _SCREEN_STATES else "other"
        action = "close" if data.get("action") == "close" else "none"
        observation = Observation(
            screen_state=state, bet=bet, credit=credit, action=action,
            target_description=data.get("target_description") or None, reasoning=str(data.get("reasoning") or ""),
            spin_ready=data.get("spin_ready") if isinstance(data.get("spin_ready"), bool) else None,
            in_game=data.get("in_game") if isinstance(data.get("in_game"), bool) else None,
        )
        logger.info("supervisore: stato=%s puntata=%s saldo=%s pronto_allo_spin=%s azione=%s (%s)", state, bet, credit,
                    observation.spin_ready, action, observation.reasoning)
        return observation

    def read_bet(self, device_id: str) -> Optional[Decimal]:
        """La puntata attuale vista da Sol sullo schermo (None se non la legge)."""
        path = self._screenshot(device_id)
        if path is None:
            return None
        observation = self.observe(path, "verify the current total bet after selecting a bet in the menu")
        return observation.bet if observation is not None else None

    def assist(self, device_id: str, min_bet: Optional[Decimal], goal: str = "lower the bet to the minimum") -> Assist:
        """Prima dell'operatore: guarda lo schermo; se la puntata e' gia' il minimo noto lo dice, se c'e' un menu
        non voluto prova a chiuderlo."""
        if self.interventions >= self.max_interventions:
            return Assist("nothing")
        self.interventions += 1
        path = self._screenshot(device_id)
        if path is None:
            return Assist("nothing")
        observation = self.observe(path, goal)
        if observation is None:
            return Assist("nothing")

        at_min = (
            observation.bet is not None and min_bet is not None and observation.bet == min_bet
            and observation.screen_state in ("idle", "modal", "other")
        )
        if observation.action == "close" and observation.target_description:
            if self._close(device_id, observation.target_description, path):
                return Assist("at_min" if at_min else "closed", observation.bet)
            return Assist("at_min", observation.bet) if at_min and observation.screen_state != "modal" else Assist("nothing")
        if at_min and observation.screen_state != "modal":
            return Assist("at_min", observation.bet)
        return Assist("nothing", observation.bet)

    def check_in_game(self, device_id: str) -> str:
        """La slot e' ancora sullo schermo? 'in_game', 'lost' (sessione scaduta, lobby, pagina di errore o del sito:
        bisogna rientrare) o 'unknown' (nessuna risposta o budget proprio finito). Non consuma il budget di osserva."""
        if self.lost_checks >= self.max_lost_checks:
            return "unknown"
        self.lost_checks += 1
        path = self._screenshot(device_id)
        if path is None:
            return "unknown"
        observation = self.observe(
            path, "is the slot game still on screen, or has the session expired / has the player been thrown out "
                  "(error page, lobby, website page, login)?", use_budget=False,
        )
        if observation is None or observation.in_game is None:
            return "unknown"
        return "in_game" if observation.in_game else "lost"

    def check_menu_open(self, device_id: str) -> str:
        """Un menu/pannello (puntata, impostazioni) copre ancora il gioco? 'open' | 'closed' | 'unknown'. Ha un budget
        proprio e non consuma quello delle altre osservazioni."""
        if self.menu_checks >= self.max_menu_checks:
            return "unknown"
        self.menu_checks += 1
        path = self._screenshot(device_id)
        if path is None:
            return "unknown"
        observation = self.observe(
            path, "the player just changed the bet: is a bet-selection menu or any other panel still open over the "
                  "game?", use_budget=False,
        )
        if observation is None:
            return "unknown"
        return "open" if observation.screen_state == "modal" else "closed"

    def check_ready(self, device_id: str) -> str:
        """Lo schermo risulta 'occupato' al controllo locale: Sol dice se il gioco e' in realta' pronto.
        'ready' = pulsante spin visibile e gioco in attesa (anche con overlay di vincita); 'closed' = chiuso un
        menu non voluto; 'busy' = bonus/spin/caricamento in corso; 'unknown' = nessuna risposta o budget finito."""
        if self.interventions >= self.max_interventions:
            return "unknown"
        self.interventions += 1
        path = self._screenshot(device_id)
        if path is None:
            return "unknown"
        observation = self.observe(
            path, "the automatic player believes the screen is busy (win or bonus in progress): is the game ready "
                  "for a new spin?",
        )
        if observation is None:
            return "unknown"
        if observation.in_game is False:
            return "lost"
        if observation.action == "close" and observation.target_description:
            return "closed" if self._close(device_id, observation.target_description, path) else "busy"
        if observation.spin_ready is True and observation.screen_state in ("idle", "win_presentation", "other"):
            return "ready"
        return "busy"

    def _close(self, device_id: str, description: str, before_path: str) -> bool:
        if self.describe is None or any(word in description.lower() for word in _SPIN_WORDS):
            return False
        width, height = _image_size(before_path)
        try:
            point = self.describe(description, LocateContext(
                device_id=device_id, slot_name="supervisor", screenshot_path=before_path,
                screen_width=width, screen_height=height,
            ))
        except Exception as exc:
            logger.warning("supervisore: ricerca di '%s' fallita: %s", description, exc)
            return False
        if point is None:
            logger.warning("supervisore: non trovo '%s' con precisione: non tocco.", description)
            return False
        logger.info("supervisore: chiudo il menu non voluto toccando '%s' a (%d,%d).", description, point[0], point[1])
        self.touch_injector.tap_coordinates(device_id, int(point[0]), int(point[1]))
        time.sleep(self.settle_seconds)
        after = self._screenshot(device_id)
        if after is None:
            return False
        diff = diff_score(before_path, after)
        return bool(diff.success and diff.data >= _SCREEN_CHANGED_DIFF)

    def _screenshot(self, device_id: str) -> Optional[str]:
        shot = self.screen_reader._screenshot(device_id)
        if not shot.success:
            logger.warning("supervisore: screenshot fallito: %s", shot.error)
            return None
        return shot.data


def _decimal(value) -> Optional[Decimal]:
    return Decimal(str(value)) if value is not None else None


def _image_size(path: str) -> tuple:
    from PIL import Image
    with Image.open(path) as image:
        return image.size
