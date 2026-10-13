"""Uscita dalla slot a fine sessione: pulsante con testo -> icona di chiusura -> tasto Back, verificando ogni volta.

Mai bloccante: se non si riesce a uscire si segnala e basta (il target e' gia' raggiunto). Gli elementi che
ScreenReader vede sono solo quelli con testo o resource_id: un'icona senza id e un gioco su canvas non compaiono,
per questo l'ultima risorsa e' il tasto Back di Android.
"""
import logging
import time
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.login_manager import adb_input, ui_helpers
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement

logger = logging.getLogger(__name__)

# Testi del pulsante di uscita, per priorita'. Mai "Menu" (apre un menu, non esce) ne' una "X" testuale (moltiplicatori).
EXIT_TEXTS = (
    "Esci", "Esci dalla slot", "Esci dal gioco", "Chiudi slot", "Chiudi gioco",
    "Torna alla lobby", "Lobby", "Home", "Torna", "Chiudi",
)
MAX_EXIT_TEXT_LENGTH = 30          # "Chiudi" dentro un paragrafo non e' un pulsante
# Icona di chiusura: testo o resource_id che la riconoscono; in alto a destra, piccola.
CLOSE_TEXTS = ("x", "×", "✕", "✖", "✗", "close", "chiudi")
CLOSE_ID_PARTS = ("close", "exit", "esci", "chiudi")
MAX_ICON_SIDE = 200
CORNER_X_FRACTION = 0.74           # equivale a x > 800 su 1080 px
CORNER_Y_FRACTION = 0.17           # equivale a y < 400 su 2400 px
MAX_ACTIONS = 3
UNCHANGED_SIMILARITY = 0.5         # testi in comune sopra questa soglia = stessa schermata


def leave_slot(
    screen_reader, touch_injector, device_id: str, sleep: Callable[[float], None] = time.sleep,
    wait_seconds: float = 3, press_back: Callable[[str], Result] = adb_input.press_back,
    max_actions: int = MAX_ACTIONS,
) -> Result[bool]:
    """Esce dalla slot. Fino a `max_actions` azioni (testo, icona, Back); dopo ognuna si rilegge lo schermo, si verifica
    di essere usciti e, se si e' ancora li' (un popup chiuso, un dialogo), si sceglie di nuovo sullo schermo nuovo."""
    try:
        first = screen_reader.read_screen(device_id)
        if not first.success:
            # Senza vedere lo schermo non si tocca nulla alla cieca.
            logger.warning("Esci: lettura dello schermo fallita (%s): non tocco nulla.", first.error)
            return Result.fail(first.error or "lettura dello schermo fallita", first.error_code or "EXIT_FAILED")
        screen = first.data
        tapped: set = set()
        back_used = False

        for _ in range(max_actions):
            action = _tap_text(screen, touch_injector, device_id, tapped)
            if action is None:
                action = _tap_icon(screen, touch_injector, device_id, tapped)
            if action is None:
                if back_used:
                    break                              # niente altro da provare
                logger.info("Esci: non trovato, uso tasto Back")
                back_used = True
                pressed = press_back(device_id)
                if not pressed.success:
                    logger.warning("Esci: tasto Back fallito: %s", pressed.error)
                    continue
            elif action is False:
                continue                               # tocco fallito: provo la strategia successiva
            sleep(wait_seconds)

            after = screen_reader.read_screen(device_id)
            if not after.success:
                logger.info("Esci: uscita non verificabile (lettura dello schermo fallita)")
                logger.info("Uscita dalla slot effettuata")
                return Result.ok(True)
            verdict = _left_slot(screen, after.data)
            if verdict is None:
                logger.info("Esci: uscita non verificabile (schermo senza elementi leggibili)")
            if verdict is not False:
                logger.info("Uscita dalla slot effettuata")
                return Result.ok(True)
            logger.info("Esci: lo schermo e' ancora quello della slot (o di un popup): riprovo")
            screen = after.data

        logger.warning("Uscita dalla slot non riuscita: resto nella slot (il target e' gia' raggiunto).")
        return Result.fail("Ancora nella slot dopo i tentativi di uscita", "EXIT_FAILED")
    except Exception as exc:
        logger.warning("Uscita dalla slot non riuscita: %s", exc)
        return Result.fail(str(exc), "EXIT_ERROR")


def _key(element: UIElement) -> tuple:
    return element.text, tuple(element.bounds or ())


def _tap(touch_injector, device_id: str, element: UIElement, tapped: set) -> bool:
    center = ui_helpers.element_center(element.bounds)
    tapped.add(_key(element))
    if center is None:
        return False
    result = touch_injector.tap_coordinates(device_id, center[0], center[1])
    if not result.success:
        logger.warning("Esci: tocco su '%s' fallito: %s", element.text.strip(), result.error)
        return False
    return True


def _tap_text(screen: ScreenContent, touch_injector, device_id: str, tapped: set) -> Optional[bool]:
    """Tocca il pulsante di uscita col testo. None = non c'e'; True = toccato; False = tocco fallito."""
    shown = ScreenContent(
        elements=[e for e in screen.elements
                  if _key(e) not in tapped and len(e.text.strip()) <= MAX_EXIT_TEXT_LENGTH],
        screen_type=screen.screen_type, confidence=screen.confidence, method=screen.method)
    for text in EXIT_TEXTS:
        element = ui_helpers.find_element_by_text(shown, text)
        if element is None:
            continue
        cx, cy = ui_helpers.element_center(element.bounds)
        logger.info("Esci: trovato pulsante '%s' a (%d,%d)", element.text.strip(), cx, cy)
        return _tap(touch_injector, device_id, element, tapped)
    return None


def _tap_icon(screen: ScreenContent, touch_injector, device_id: str, tapped: set) -> Optional[bool]:
    """Tocca un'icona di chiusura in alto a destra. None = non c'e' una scelta sicura; True/False come _tap_text."""
    icon = _close_icon(screen, tapped)
    if icon is None:
        return None
    cx, cy = ui_helpers.element_center(icon.bounds)
    label = icon.text.strip() or icon.resource_id or icon.class_name
    logger.info("Esci: nessun pulsante con testo, tocco l'icona di chiusura '%s' a (%d,%d)", label, cx, cy)
    return _tap(touch_injector, device_id, icon, tapped)


def _looks_like_close(element: UIElement) -> bool:
    return (element.text.strip().casefold() in CLOSE_TEXTS
            or any(part in (element.resource_id or "").lower() for part in CLOSE_ID_PARTS))


def _close_icon(screen: ScreenContent, tapped: set) -> Optional[UIElement]:
    visible = [e for e in screen.elements if ui_helpers.valid_bounds(e.bounds)]
    if not visible:
        return None
    max_x = max(e.bounds[2] for e in visible)
    max_y = max(e.bounds[3] for e in visible)
    width = max_x if max_x >= 600 else 1080          # schermate con pochi elementi: misure tipiche di un telefono
    height = max_y if max_y >= 1000 else 2400

    def in_corner(e: UIElement) -> bool:
        return e.bounds[0] >= CORNER_X_FRACTION * width and e.bounds[3] <= CORNER_Y_FRACTION * height

    def small(e: UIElement) -> bool:
        return e.bounds[2] - e.bounds[0] <= MAX_ICON_SIDE and e.bounds[3] - e.bounds[1] <= MAX_ICON_SIDE

    corner = [e for e in visible if _key(e) not in tapped and e.class_name != ui_helpers.EDIT_TEXT_CLASS
              and small(e) and in_corner(e)]
    marked = [e for e in corner if _looks_like_close(e)]
    if marked:
        return min(marked, key=lambda e: (e.bounds[1], -e.bounds[0]))          # il piu' in alto e a destra
    # Senza un segno che la riconosca, un'icona si tocca solo se e' l'unica scelta possibile in quell'angolo
    # (Menu, audio e impostazioni stanno nello stesso punto).
    # Un elemento con un'etichetta ("Menu", "Info") non e' un'icona anonima: ha detto cos'e', e non e' una chiusura.
    plain = [e for e in corner if not e.text.strip()
             and any(part in (e.class_name or "") for part in ("Button", "ImageView"))]
    return plain[0] if len(plain) == 1 else None


def _texts(screen: ScreenContent) -> set:
    return {e.text.strip() for e in screen.elements if e.text.strip()}


def _left_slot(before: ScreenContent, after: ScreenContent) -> Optional[bool]:
    """True = lo schermo e' un altro (URL cambiato o quasi nessun testo in comune), False = e' ancora lo stesso (un popup
    o un menu aggiungono elementi ma non sostituiscono la pagina), None = non determinabile (schermate quasi vuote,
    es. un gioco su canvas)."""
    url_before, url_after = ui_helpers.current_chrome_url(before), ui_helpers.current_chrome_url(after)
    if url_before and url_after and url_before != url_after:
        return True
    a, b = _texts(before), _texts(after)
    if len(a | b) < 3:
        return None
    return len(a & b) / len(a | b) <= UNCHANGED_SIMILARITY
