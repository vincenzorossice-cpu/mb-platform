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
_MIN_MENU_CELLS = 3
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
    """Cambia la puntata dal MENU della slot invece che con un tasto meno:
    apre il menu (icona tipo monete), legge con l'OCR i valori elencati,
    tocca il piu' basso, richiude il menu se e' rimasto aperto e conferma che
    la puntata letta sia quel valore. Il valore piu' basso elencato dal gioco
    stesso e' per definizione la puntata minima consentita."""

    def __init__(self, locator: ElementLocator, verifier: ActionVerifier):
        self.locator = locator
        self.verifier = verifier

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
            logger.warning("bet menu: aperto ma non leggibile (%d importi isolati): lo richiudo", len(cells))
            self._close(device_id, toggle)
            return Result.fail("Bet menu values not readable", "MENU_NOT_READABLE")

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

    def _close(self, device_id: str, toggle) -> None:
        self.verifier.verified_tap(device_id, toggle.x, toggle.y, Expectation(kind="screen_changed"))

    @staticmethod
    def _fresh(context_provider: Callable[[], Result]):
        result = context_provider()
        return result.data if result.success else None
