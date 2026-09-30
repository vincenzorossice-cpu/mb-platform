import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.value_parser import parse_amount
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, Expectation

logger = logging.getLogger(__name__)

_MENU_TOGGLE = "bet_menu_toggle"
# Un testo che e' SOLO un importo (opz. con simbolo di valuta): la cella di
# un menu puntata. Le etichette come "PUNTATA €0,10" non lo sono.
_STANDALONE_AMOUNT = re.compile(r"^\s*(€|\$)?\s*\d[\d.,]*\s*(€|\$|EUR)?\s*$", re.IGNORECASE)
# Etichette del quadro principale (saldo/puntata/vincita, multilingua): un
# importo sulla loro riga e' il valore mostrato, non una cella del menu.
_LABEL_WORDS = (
    "POSTA", "PUNTATA", "BET", "STAKE", "SALDO", "CREDITO", "CREDIT", "BALANCE",
    "VINCITA", "WIN", "DEMO", "TOTALE", "TOTAL",
)
_ROW_TOLERANCE_PX = 50
# Un menu a LISTA di puntate elenca piu' valori selezionabili. Con meno (es. un
# pannello a righe con pulsanti -/+ che mostra 2-3 importi) non e' una lista:
# si cambia la puntata con i pulsanti meno/piu'.
_MIN_MENU_CELLS = 4
_MAX_PLAUSIBLE_BET = Decimal("100000")


@dataclass
class MenuSelection:
    final_bet: Decimal
    lowest_listed: Decimal
    toggle_strategy: str


def menu_amounts(text_boxes: list) -> list:
    """[(valore, (cx, cy))] delle celle-importo di un menu puntata aperto:
    testi che sono solo un importo e non stanno sulla riga di un'etichetta
    del quadro principale. Generico: nessuna posizione o valore fissi."""
    label_rows = [
        (box.bbox[1] + box.bbox[3]) / 2 for box in text_boxes
        if any(word in box.text.upper() for word in _LABEL_WORDS)
    ]
    cells = []
    for box in text_boxes:
        if not _STANDALONE_AMOUNT.match(box.text):
            continue
        cy = (box.bbox[1] + box.bbox[3]) / 2
        if any(abs(cy - row) < _ROW_TOLERANCE_PX for row in label_rows):
            continue
        parsed = parse_amount(box.text)
        if not parsed.success or parsed.data <= 0 or parsed.data > _MAX_PLAUSIBLE_BET:
            continue
        cells.append((parsed.data, (round((box.bbox[0] + box.bbox[2]) / 2), round(cy))))
    return cells


def _looks_like_menu(cells: list) -> bool:
    return len({value for value, _ in cells}) >= _MIN_MENU_CELLS


class BetMenuSelector:
    """Cambia la puntata dal MENU della slot invece che con un tasto meno
    sulla schermata principale: apre il menu (icona tipo monete) e, se elenca
    una LISTA di puntate (4+ importi), legge i valori con l'OCR, tocca il piu'
    basso, richiude il menu e conferma che la puntata letta sia quel valore (il
    piu' basso elencato e' per definizione il minimo consentito). Se il menu non
    e' una lista (es. righe con pulsanti -/+) lo lascia aperto e fallisce con
    MENU_OPEN_NOT_A_LIST: lo gestisce il chiamante con i pulsanti meno."""

    def __init__(self, locator: ElementLocator, verifier: ActionVerifier, screen_recovery=None):
        self.locator = locator
        self.verifier = verifier
        # Opzionale: senza, un tocco sul bersaglio sbagliato non viene annullato.
        self.screen_recovery = screen_recovery

    def select_lowest(
        self, device_id: str, slot_name: str, context, context_provider: Callable[[], Result],
    ) -> Result[MenuSelection]:
        toggle_result = self.locator.locate(_MENU_TOGGLE, context)
        if not toggle_result.success:
            return Result.fail(toggle_result.error, toggle_result.error_code)
        toggle = toggle_result.data
        is_trusted = self.locator.is_trusted_strategy(toggle.strategy_name)

        toggle_tap = self.verifier.verified_tap(device_id, toggle.x, toggle.y, Expectation(kind="screen_changed"))
        if not (toggle_tap.success and toggle_tap.data.verified):
            if not is_trusted:
                self.locator.record_failure(toggle.strategy_name, _MENU_TOGGLE)
            if toggle_tap.success and toggle_tap.data.outcome == "unexpected_change":
                return self._undo_wrong_toggle(device_id, slot_name, toggle, toggle_tap.data, context_provider)
            return Result.fail("Opening the bet menu had no visible effect", "MENU_TOGGLE_NO_EFFECT")
        self.locator.record_success(toggle.strategy_name, _MENU_TOGGLE)
        self.locator.save_verified_candidate(
            _MENU_TOGGLE, toggle, slot_name, context.screen_width, context.screen_height,
        )

        opened = self._fresh(context_provider)
        if opened is None:
            return Result.fail("Cannot read the screen after opening the menu", "MENU_CONTEXT_ERROR")
        cells = menu_amounts(opened.text_boxes)
        if not _looks_like_menu(cells):
            # Non e' una lista di puntate (es. pannello a righe con -/+): il menu
            # resta APERTO, il chiamante cambiera' la puntata con i pulsanti meno.
            logger.info("bet menu: aperto, %d importi isolati: non e' una lista selezionabile, lo lascio aperto", len(cells))
            return Result.fail("The open menu is not a list of selectable bets", "MENU_OPEN_NOT_A_LIST")

        lowest, (cell_x, cell_y) = min(cells, key=lambda item: item[0])
        logger.info("bet menu: valori letti %s, tocco il piu' basso %s a (%d,%d)",
                    sorted({v for v, _ in cells}), lowest, cell_x, cell_y)
        self.verifier.verified_tap(device_id, cell_x, cell_y, Expectation(kind="screen_changed"))

        after = self._fresh(context_provider)
        if after is None:
            return Result.fail("Cannot read the screen after selecting the bet", "MENU_CONTEXT_ERROR")
        if _looks_like_menu(menu_amounts(after.text_boxes)):
            logger.info("bet menu: ancora aperto dopo la selezione, lo richiudo")
            self._close(device_id, toggle)
            after = self._fresh(context_provider)
            if after is None:
                return Result.fail("Cannot read the screen after closing the menu", "MENU_CONTEXT_ERROR")

        bet = after.slot_ocr_state.get("posta")
        if bet is None or Decimal(str(bet)) != lowest:
            return Result.fail(
                f"Bet after menu selection is {bet}, expected {lowest}", "MENU_SELECTION_NOT_CONFIRMED",
            )
        return Result.ok(MenuSelection(
            final_bet=Decimal(str(bet)), lowest_listed=lowest, toggle_strategy=toggle.strategy_name,
        ))

    def _undo_wrong_toggle(self, device_id, slot_name, toggle, outcome, context_provider) -> Result[MenuSelection]:
        """Il tocco ha cambiato qualcosa che non e' l'apertura del menu (es. una
        schermata info o la modalita' di valuta): si scarta il punto e, se
        possibile, si annulla il cambiamento."""
        self.locator.reject_position(_MENU_TOGGLE, toggle.x, toggle.y)
        if self.screen_recovery is None:
            return Result.fail("The tap changed the screen but did not open the menu", "MENU_TOGGLE_WRONG_TARGET")
        original_state = {"posta": outcome.bet_before, "saldo": outcome.balance_before}
        recovery = self.screen_recovery.recover(
            device_id, outcome.screenshot_before, (toggle.x, toggle.y), context_provider, original_state,
        )
        if not recovery.success:
            return Result.fail(
                f"The tap hit the wrong element and the screen could not be restored: {recovery.error}",
                "MENU_STUCK",
            )
        if recovery.data.method != "already_original":
            # Provato: questo tocco aveva cambiato lo schermo e abbiamo dovuto annullarlo.
            self.locator.remember_rejected(slot_name, _MENU_TOGGLE, toggle.x, toggle.y)
        return Result.fail("The tap changed the screen but did not open the menu (undone)", "MENU_TOGGLE_WRONG_TARGET")

    def _close(self, device_id: str, toggle) -> None:
        self.verifier.verified_tap(device_id, toggle.x, toggle.y, Expectation(kind="screen_changed"))

    @staticmethod
    def _fresh(context_provider: Callable[[], Result]):
        result = context_provider()
        return result.data if result.success else None
