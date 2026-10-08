"""Esplorazione di una slot nuova con Sol Vision: individua spin, menu puntata (e come si chiude) ed Esci, poi VERIFICA
ogni punto toccandolo (lo schermo deve cambiare) prima di metterlo nella mappa. Sol da' coordinate normalizzate
0-1000: nessun problema di scala dello screenshot ridotto. Nessun punto entra in mappa senza una verifica."""
import logging
import time
from decimal import Decimal
from typing import Callable, Optional

from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.slot_map import BET_MENU_TYPES, CLOSE_METHODS, Point, SlotMap, now_iso
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_ocr.vision_reader import _encode_reduced

logger = logging.getLogger(__name__)

_PURPOSE = "esplorazione"
_CHANGED_DIFF = 0.02          # schermo "cambiato" dopo un tocco
_CLOSED_DIFF = 0.06           # sotto questa differenza dallo schermo di partenza il menu e' chiuso
_SETTLE_SECONDS = 1.5
_STILL_MAX_SECONDS = 20.0
_MAX_TOKENS = 800

_EXPLORE_SYSTEM = (
    "You are exploring a slot machine game on an Android phone to map its UI elements. You see ONE screenshot. "
    "Answer ONLY with JSON: "
    '{"spin": {"x": int, "y": int, "description": str} | null, '
    '"bet_menu_open": {"x": int, "y": int, "description": str} | null, '
    '"bet_menu_type": "carousel|list|dropdown|plus_minus|other|unknown", '
    '"bet_minus": {"x": int, "y": int} | null, "bet_plus": {"x": int, "y": int} | null, '
    '"exit": {"x": int, "y": int, "description": str} | null, "bet_menu_is_open_now": bool, "notes": str}. '
    "ALL coordinates are NORMALIZED to a 0-1000 grid: x from the left edge (0) to the right edge (1000), y from the "
    "top (0) to the bottom (1000); give the CENTER of each control. spin = the main round button that starts a "
    "spin (circular arrows). bet_menu_open = the control that opens the panel to change the bet amount (often a "
    "stack-of-coins icon or the bet value itself); if the bet menu is closed, describe how to open it. "
    "bet_menu_type: carousel = a horizontal row of amounts that scrolls; list/dropdown = a vertical list; "
    "plus_minus = the bet is changed with separate minus and plus buttons (then give bet_minus and bet_plus and "
    "bet_menu_open=null); unknown if you cannot tell yet. exit = the Exit / Esci / Home button of the game, only if "
    "visible. Use null for anything you cannot see. NEVER guess a position you cannot see."
)
_MENU_SYSTEM = (
    "You are exploring a slot machine game to map its UI. The bet selection menu has just been opened. Answer ONLY "
    'with JSON: {"menu_type": "carousel|list|dropdown|plus_minus|other", "lowest_visible_bet": string | null, '
    '"cells_cut_at_edge": bool, "close": {"method": "tap_outside|x_button|toggle|other", "x": int, "y": int} | null, '
    '"notes": str}. Coordinates are NORMALIZED to a 0-1000 grid (x from the left, y from the top). '
    "carousel = a horizontal row of amounts that can be scrolled sideways (some may be cut at the screen edge); "
    "list/dropdown = amounts one under the other. lowest_visible_bet = the smallest amount you can read, copied as "
    "written. close = how to close this menu: an X button (give its center), tapping on the dimmed area outside the "
    "panel (give a point of the empty background, away from any control) or tapping the same icon again (toggle). "
    "NEVER suggest tapping the spin button."
)


class SlotExplorer:
    """describe(frase, LocateContext) -> (x, y) | None (GPTLocatorStrategy.locate_described) e' il ripiego con la
    ricerca a griglia se un punto di Sol non apre/cambia nulla; ocr_reader.read_slot_state(path) legge saldo e puntata."""

    def __init__(self, gpt_client, screen_reader, touch_injector, ocr_reader=None,
                 describe: Optional[Callable] = None, sleep: Callable[[float], None] = time.sleep):
        self.gpt_client = gpt_client
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.ocr_reader = ocr_reader
        self.describe = describe
        self.sleep = sleep
        self.last_notes = []

    # ---- API ---------------------------------------------------------------------------------------------------
    def explore(self, device_id: str, slot_name: str, demo: bool = True, known_min_bet: Optional[Decimal] = None,
                base: Optional[SlotMap] = None, only: Optional[str] = None) -> Result[SlotMap]:
        """Mappa la slot. only=None: tutto; "spin" | "bet_menu": solo quella parte (ri-esplorazione), partendo da
        `base`. Result con la mappa se utilizzabile (spin verificato)."""
        self.last_notes = []
        shot = self._screenshot(device_id)
        if shot is None:
            return Result.fail("screenshot fallito", "EXPLORE_SCREENSHOT_FAILED")
        path, size = shot
        overview = self._ask(path, _EXPLORE_SYSTEM, f"Map the UI of the slot '{slot_name}'.")
        if overview is None:
            return Result.fail("nessuna risposta utilizzabile da Sol", "EXPLORE_NO_ANSWER")

        smap = base if base is not None and tuple(base.screen) == size else SlotMap(slot_name=slot_name, screen=size)
        smap.slot_name = slot_name
        if only in (None, "bet_menu"):
            self._map_bet_menu(device_id, smap, path, overview, known_min_bet)
        if only in (None, "spin"):
            self._map_spin(device_id, smap, overview, demo)
        if only is None:
            smap.exit_point = self._point(overview.get("exit"), size, verified=False)
        smap.explored_at = now_iso()
        smap.method = "sol"
        if not smap.is_usable():
            return Result.fail("mappa incompleta: " + "; ".join(self.last_notes or ["spin non verificato"]),
                               "EXPLORE_INCOMPLETE")
        return Result.ok(smap)

    # ---- bet menu ----------------------------------------------------------------------------------------------
    def _map_bet_menu(self, device_id: str, smap: SlotMap, closed_path: str, overview: dict,
                      known_min_bet: Optional[Decimal]) -> None:
        size = smap.screen
        bet_type = overview.get("bet_menu_type")
        if known_min_bet is not None:
            smap.min_bet, smap.min_bet_proven = str(known_min_bet), True

        if bet_type == "plus_minus":
            minus = self._point(overview.get("bet_minus"), size)
            plus = self._point(overview.get("bet_plus"), size)
            if minus is not None and self._tap_changes(device_id, minus)[0]:
                minus.verified = True
                smap.bet_menu_type, smap.bet_minus, smap.bet_plus = "plus_minus", minus, plus
                smap.bet_menu_open, smap.close_point, smap.close_method = None, None, "other"
                return
            self.last_notes.append("il tasto '-' indicato da Sol non cambia lo schermo")

        open_point = self._point(overview.get("bet_menu_open"), size)
        if overview.get("bet_menu_is_open_now") is True:
            self.last_notes.append("il menu puntata risulta gia' aperto: non lo mappo")
            return
        opened_path = None
        if open_point is not None:
            changed, _, opened_path = self._tap_changes(device_id, open_point)
            if not changed:
                open_point, opened_path = None, None
        if open_point is None and self.describe is not None:
            fallback = self._describe(closed_path, size,
                                      "the icon or control that opens the bet amount selection menu")
            if fallback is not None:
                candidate = Point(int(fallback[0]), int(fallback[1]), "trovato con la ricerca a griglia")
                changed, _, opened_path = self._tap_changes(device_id, candidate)
                if changed:
                    open_point = candidate
        if open_point is None or opened_path is None:
            self.last_notes.append("non riesco ad aprire il menu puntata")
            return
        open_point.verified = True
        smap.bet_menu_open = open_point

        menu = self._ask(opened_path, _MENU_SYSTEM, "Describe the open bet menu and how to close it.")
        menu_type = (menu or {}).get("menu_type")
        smap.bet_menu_type = menu_type if menu_type in BET_MENU_TYPES else "other"
        self._close_menu(device_id, smap, closed_path, (menu or {}).get("close"))

    def _close_menu(self, device_id: str, smap: SlotMap, closed_path: str, close: Optional[dict]) -> None:
        """Prova il metodo di chiusura indicato da Sol e poi il tocco sulla stessa icona; si salva solo cio' che
        porta davvero lo schermo come prima dell'apertura."""
        attempts = []
        method = (close or {}).get("method")
        close_point = self._point(close, smap.screen)
        if close_point is not None:
            attempts.append((method if method in CLOSE_METHODS else "other", close_point))
        if smap.bet_menu_open is not None and method != "toggle":
            attempts.append(("toggle", smap.bet_menu_open))
        for name, point in attempts:
            _, _, after_path = self._tap_changes(device_id, point)
            if after_path is None:
                continue
            diff = diff_score(closed_path, after_path)
            if diff.success and diff.data < _CLOSED_DIFF:
                smap.close_method = name
                smap.close_point = Point(point.x, point.y, point.description, verified=True)
                return
        smap.close_method, smap.close_point = "other", None
        self.last_notes.append("non ho verificato come si chiude il menu puntata")

    # ---- spin --------------------------------------------------------------------------------------------------
    def _map_spin(self, device_id: str, smap: SlotMap, overview: dict, demo: bool) -> None:
        if not demo:
            self.last_notes.append("soldi veri: non tocco lo spin per verificarlo")
            return
        point = self._point(overview.get("spin"), smap.screen)
        for attempt in range(2):
            if point is None:
                break
            before = self._screenshot(device_id)
            balance_before = self._balance(before[0]) if before else None
            self.touch_injector.tap_coordinates(device_id, point.x, point.y)
            self.sleep(_SETTLE_SECONDS * 2)
            after = self._screenshot(device_id)
            balance_after = self._balance(after[0]) if after else None
            changed = bool(before and after and self._changed(before[0], after[0]))
            spent = balance_before is not None and balance_after is not None and balance_after < balance_before
            if spent or (changed and (balance_before is None or balance_after is None)):
                point.verified = True
                point.description = (point.description or "spin") + (" (addebito visto)" if spent else " (schermo cambiato)")
                smap.spin = point
                self._wait_still(device_id)
                return
            point = None
            if attempt == 0 and self.describe is not None and before:
                found = self._describe(before[0], smap.screen,
                                       "the main SPIN button (round button with circular arrows) that starts a spin")
                if found is not None:
                    point = Point(int(found[0]), int(found[1]), "trovato con la ricerca a griglia")
        self.last_notes.append("lo spin indicato non ha addebitato")

    def _wait_still(self, device_id: str) -> None:
        """Aspetta che l'animazione dello spin finisca (due screenshot di fila uguali) prima di proseguire."""
        deadline = time.monotonic() + _STILL_MAX_SECONDS
        previous = self._screenshot(device_id)
        while time.monotonic() < deadline:
            self.sleep(1.0)
            current = self._screenshot(device_id)
            if previous and current:
                diff = diff_score(previous[0], current[0])
                if diff.success and diff.data < 0.005:
                    return
            previous = current

    # ---- strumenti ---------------------------------------------------------------------------------------------
    def _screenshot(self, device_id: str):
        shot = self.screen_reader._screenshot(device_id)
        if not shot.success:
            return None
        try:
            with Image.open(shot.data) as image:
                return shot.data, image.size
        except OSError:
            return None

    def _tap_changes(self, device_id: str, point: Point) -> tuple:
        """(cambiato, screenshot_prima, screenshot_dopo): tocca e confronta con lo schermo di prima."""
        before = self._screenshot(device_id)
        self.touch_injector.tap_coordinates(device_id, point.x, point.y)
        self.sleep(_SETTLE_SECONDS)
        after = self._screenshot(device_id)
        if not before or not after:
            return False, None, None
        return self._changed(before[0], after[0]), before[0], after[0]

    @staticmethod
    def _changed(before_path: str, after_path: str) -> bool:
        diff = diff_score(before_path, after_path)
        return bool(diff.success and diff.data >= _CHANGED_DIFF)

    def _balance(self, path: str) -> Optional[Decimal]:
        if self.ocr_reader is None:
            return None
        try:
            value = self.ocr_reader.read_slot_state(path).get("saldo")
            return Decimal(str(value)) if value is not None else None
        except Exception:
            return None

    def _describe(self, path: str, size: tuple, description: str):
        try:
            return self.describe(description, LocateContext(
                device_id="explorer", slot_name="explorer", screenshot_path=path,
                screen_width=size[0], screen_height=size[1],
            ))
        except Exception as exc:
            logger.warning("esplorazione: ricerca a griglia fallita: %s", exc)
            return None

    @staticmethod
    def _point(raw, size: tuple, verified: bool = False) -> Optional[Point]:
        """Coordinate normalizzate 0-1000 di Sol -> pixel; None se mancano o sono fuori dallo schermo."""
        try:
            nx, ny = float(raw["x"]), float(raw["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
            return None
        return Point(round(nx / 1000 * size[0]), round(ny / 1000 * size[1]),
                     str(raw.get("description", "")) if isinstance(raw, dict) else "", verified)

    def _ask(self, image_path: str, system_prompt: str, user_text: str) -> Optional[dict]:
        try:
            encoded = _encode_reduced(image_path)
        except OSError as exc:
            logger.warning("esplorazione: screenshot illeggibile: %s", exc)
            return None
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ]},
        ]
        result = self.gpt_client._create_completion(messages, max_tokens=_MAX_TOKENS, purpose=_PURPOSE)
        if not result.success or not isinstance(result.data, dict):
            logger.warning("esplorazione: nessuna risposta utilizzabile (%s).", getattr(result, "error", None))
            return None
        return result.data
