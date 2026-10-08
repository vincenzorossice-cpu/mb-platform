import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import forget_autopilot_element, load_autopilot_flag
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate
from orchestrator.modules.slot_autopilot.value_parser import parse_amount
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, Expectation

logger = logging.getLogger(__name__)

_MENU_TOGGLE = "bet_menu_toggle"
_MENU_CLOSE = "bet_menu_close"   # punto che chiude il menu (spesso: fuori dal riquadro), insegnato dall'operatore
_ALL_BUT_MEMORY = ("ocr_anchor", "spatial_heuristic", "template", "gpt", "hitl")
_ALL_BUT_OPERATOR = ("profile", "memory", "ocr_anchor", "spatial_heuristic", "template", "gpt")
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
# Un menu puntata mostra almeno 2 importi isolati (un pannello a righe 2-3, una lista 4+). Con meno, l'icona
# toccata ha aperto un'altra cosa (menu generale del gioco, regole...).
_MIN_BET_MENU_AMOUNTS = 2
_BET_VIA_MENU_FLAG = "bet_via_menu"
# Dopo la scelta di una cella: un menu che si chiude cambia molto lo schermo (diff sopra questa soglia),
# una cella che si evidenzia cambia poco. Serve a non scambiare per "menu aperto" una schermata di gioco
# che mostra anch'essa una riga di puntate.
_MENU_STILL_OPEN_DIFF = 0.08
_EDGE_FRACTION = 0.18   # una cella piu' bassa vicino al bordo sinistro: la fila puo' continuare fuori schermo
_MAX_REVEAL_SWIPES = 3
_SETTLE_SECONDS = 1.5   # pausa prima di rileggere una puntata non leggibile (animazione di chiusura)


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

    def __init__(self, locator: ElementLocator, verifier: ActionVerifier, screen_recovery=None, supervisor=None):
        self.locator = locator
        self.verifier = verifier
        # Opzionale: se la puntata riletta dopo la scelta non e' leggibile o non torna, Sol guarda lo schermo
        # intero prima di dichiarare il fallimento (l'OCR/visione economica puo' leggere un contatore jackpot).
        self.supervisor = supervisor
        # Opzionale: senza, un tocco sul bersaglio sbagliato non viene annullato.
        self.screen_recovery = screen_recovery

    def select_lowest(
        self, device_id: str, slot_name: str, context, context_provider: Callable[[], Result],
        allow_operator: bool = True,
    ) -> Result[MenuSelection]:
        # allow_operator=False: se l'icona non si trova da sola NON si chiama l'operatore (il
        # chiamante lo fara' dopo, con la sua domanda e non una sull'icona del menu).
        toggle_result = self.locator.locate(
            _MENU_TOGGLE, context, exclude_strategies=() if allow_operator else ("hitl",),
        )
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
        opened = self._fresh(context_provider)
        if opened is None:
            return Result.fail("Cannot read the screen after opening the menu", "MENU_CONTEXT_ERROR")
        if self._is_not_a_bet_menu(slot_name, toggle, opened):
            # Lo schermo e' cambiato ma non c'e' nessuna puntata da scegliere: quell'icona non e' il menu
            # puntata. Niente memoria, si annulla (come un tocco sbagliato) e non si riprova.
            logger.warning(
                "bet menu: l'icona (%d,%d) ha aperto qualcosa che non e' un menu puntata: la annullo.",
                toggle.x, toggle.y,
            )
            if toggle.strategy_name == "memory":
                forget_autopilot_element(slot_name, _MENU_TOGGLE, self._memory_dir())
            if not is_trusted:
                self.locator.record_failure(toggle.strategy_name, _MENU_TOGGLE)
            return self._undo_wrong_toggle(device_id, slot_name, toggle, toggle_tap.data, context_provider)
        self.locator.record_success(toggle.strategy_name, _MENU_TOGGLE)
        self.locator.save_verified_candidate(
            _MENU_TOGGLE, toggle, slot_name, context.screen_width, context.screen_height,
        )
        return self._select_from_open(device_id, opened, context_provider, toggle, slot_name, allow_operator)

    def _memory_dir(self) -> str:
        directory = getattr(self.locator, "memory_dir", None)
        return directory if isinstance(directory, str) and directory else "slot_memory"

    def _is_not_a_bet_menu(self, slot_name: str, toggle: LocateCandidate, opened) -> bool:
        if len({value for value, _ in menu_amounts(opened.text_boxes)}) >= _MIN_BET_MENU_AMOUNTS:
            return False
        if toggle.strategy_name == "profile":
            return False        # coordinata curata a mano: mai messa in dubbio da una lettura
        if toggle.strategy_name == "memory" and load_autopilot_flag(
            slot_name, _BET_VIA_MENU_FLAG, self._memory_dir(),
        ):
            return False        # gia' usata con successo in una sessione precedente
        return True

    def select_from_open_menu(
        self, device_id: str, slot_name: str, context, context_provider: Callable[[], Result],
        toggle_xy: Optional[tuple] = None,
    ) -> Result[MenuSelection]:
        """Il menu e' GIA' aperto (l'ha aperto l'operatore): sceglie la puntata piu' bassa della lista.
        `toggle_xy` e' il punto che l'operatore ha fatto tappare per aprirlo: se la selezione riesce
        viene ricordato come icona del menu, cosi' la volta dopo non serve nessun aiuto."""
        toggle = None
        if toggle_xy is not None:
            toggle = LocateCandidate(
                x=int(toggle_xy[0]), y=int(toggle_xy[1]), confidence=1.0, strategy_name="hitl", verified=True,
            )
        result = self._select_from_open(device_id, context, context_provider, toggle, slot_name, True)
        if result.success and toggle is not None:
            self.locator.save_verified_candidate(
                _MENU_TOGGLE, toggle, slot_name, context.screen_width, context.screen_height,
            )
        return result

    def _select_from_open(
        self, device_id: str, opened, context_provider: Callable[[], Result], toggle: Optional[LocateCandidate],
        slot_name: str = "", allow_operator: bool = True,
    ) -> Result[MenuSelection]:
        cells = menu_amounts(opened.text_boxes)
        if not _looks_like_menu(cells):
            # Non e' una lista di puntate (es. pannello a righe con -/+): il menu
            # resta APERTO, il chiamante cambiera' la puntata con i pulsanti meno.
            logger.info("bet menu: aperto, %d importi isolati: non e' una lista selezionabile, lo lascio aperto", len(cells))
            return Result.fail("The open menu is not a list of selectable bets", "MENU_OPEN_NOT_A_LIST")

        opened, cells = self._reveal_lower_cells(device_id, opened, cells, context_provider)
        lowest, (cell_x, cell_y) = min(cells, key=lambda item: item[0])
        logger.info("bet menu: valori letti %s, tocco il piu' basso %s a (%d,%d)",
                    sorted({v for v, _ in cells}), lowest, cell_x, cell_y)
        self.verifier.verified_tap(device_id, cell_x, cell_y, Expectation(kind="screen_changed"))

        after = self._fresh(context_provider)
        if after is None:
            return Result.fail("Cannot read the screen after selecting the bet", "MENU_CONTEXT_ERROR")
        if self._menu_still_open(opened, after):
            logger.info("bet menu: ancora aperto dopo la selezione, lo richiudo")
            closed = self._close_menu(device_id, slot_name, opened, after, toggle, context_provider, allow_operator)
            if not closed.success:
                return Result.fail(closed.error, closed.error_code)
            after = closed.data

        bet = after.slot_ocr_state.get("posta")
        if bet is None:
            # Non letta non vuol dire sbagliata: un'animazione di chiusura puo' coprirla. Una seconda lettura.
            time.sleep(_SETTLE_SECONDS)
            again = self._fresh(context_provider)
            if again is not None:
                after = again
                bet = after.slot_ocr_state.get("posta")
        if self.supervisor is not None and (bet is None or Decimal(str(bet)) != lowest):
            seen = self.supervisor.read_bet(device_id)
            if seen is not None:
                logger.info("bet menu: la lettura locale dice %s, il supervisore vede %s (atteso %s).", bet, seen, lowest)
                bet = seen
        if bet is None:
            return Result.fail(
                f"Bet after menu selection cannot be read (expected {lowest})", "MENU_SELECTION_UNREADABLE",
            )
        if Decimal(str(bet)) != lowest:
            return Result.fail(
                f"Bet after menu selection is {bet}, expected {lowest}", "MENU_SELECTION_NOT_CONFIRMED",
            )
        return Result.ok(MenuSelection(
            final_bet=Decimal(str(bet)), lowest_listed=lowest,
            toggle_strategy=toggle.strategy_name if toggle is not None else "hitl",
        ))

    def _reveal_lower_cells(self, device_id: str, opened, cells: list, context_provider) -> tuple:
        """La fila di celle puo' scorrere in orizzontale con le puntate piu' basse nascoste a sinistra. Se la cella
        piu' bassa visibile e' la prima a sinistra ed e' vicina al bordo, si scorre verso destra (il contenuto
        va a destra) e si rileggono le celle, finche' non compare niente di piu' basso (al massimo 3 volte).
        Una fila tutta visibile non viene toccata."""
        injector = getattr(self.verifier, "touch_injector", None)
        swipe = getattr(injector, "swipe", None)
        width = getattr(opened, "screen_width", None)
        if not callable(swipe) or not isinstance(width, int) or width <= 0:
            return opened, cells
        for _ in range(_MAX_REVEAL_SWIPES):
            lowest, (low_x, low_y) = min(cells, key=lambda item: item[0])
            leftmost_x = min(position[0] for _, position in cells)
            if low_x != leftmost_x or low_x > width * _EDGE_FRACTION:
                break
            logger.info("bet menu: la cella piu' bassa (%s) e' al bordo sinistro: scorro la fila per vedere se ce ne sono altre.", lowest)
            moved = swipe(device_id, int(width * 0.25), low_y, int(width * 0.85), low_y, 500)
            if not getattr(moved, "success", False):
                break
            time.sleep(_SETTLE_SECONDS)
            fresh = self._fresh(context_provider)
            if fresh is None:
                break
            fresh_cells = menu_amounts(fresh.text_boxes)
            if not _looks_like_menu(fresh_cells) or min(v for v, _ in fresh_cells) >= lowest:
                break          # niente di piu' basso: il minimo e' quello gia' visto
            opened, cells = fresh, fresh_cells
        return opened, cells

    def _close_menu(
        self, device_id: str, slot_name: str, opened, after, toggle, context_provider, allow_operator: bool,
    ) -> Result:
        """Chiude il menu rimasto aperto, in quest'ordine: il punto ricordato per la slot, l'icona che lo ha
        aperto, l'operatore (che indica dove toccare; il punto viene ricordato). Dopo ogni tentativo si
        RICONTROLLA che si sia chiuso. Result con lo schermo aggiornato."""
        memo = self.locator.locate(_MENU_CLOSE, after, exclude_strategies=_ALL_BUT_MEMORY)
        if memo.success:
            self.verifier.verified_tap(device_id, memo.data.x, memo.data.y, Expectation(kind="screen_changed"))
            fresh = self._fresh(context_provider)
            if fresh is None:
                return Result.fail("Cannot read the screen after closing the menu", "MENU_CONTEXT_ERROR")
            after = fresh
            if not self._menu_still_open(opened, after):
                return Result.ok(after)
            logger.warning("bet menu: il punto ricordato per chiudere il menu non e' servito: lo dimentico.")
            forget_autopilot_element(slot_name, _MENU_CLOSE, self._memory_dir())

        if toggle is None:
            located = self.locator.locate(_MENU_TOGGLE, after, exclude_strategies=("hitl",))
            toggle = located.data if located.success else None
        if toggle is not None:
            self._close(device_id, toggle)
            fresh = self._fresh(context_provider)
            if fresh is None:
                return Result.fail("Cannot read the screen after closing the menu", "MENU_CONTEXT_ERROR")
            after = fresh
            if not self._menu_still_open(opened, after):
                return Result.ok(after)
            logger.warning("bet menu: l'icona non ha chiuso il menu.")

        if allow_operator:
            helped = self.locator.locate(_MENU_CLOSE, after, exclude_strategies=_ALL_BUT_OPERATOR)
            if helped.error_code == "HITL_STOP_REQUESTED":
                return Result.fail("The operator asked to stop while the bet menu was open", "MENU_STUCK")
            tap = (helped.data.x, helped.data.y) if helped.success else self.locator.last_hitl_tap()
            if isinstance(tap, tuple):
                fresh = self._fresh(context_provider)
                if fresh is None:
                    return Result.fail("Cannot read the screen after closing the menu", "MENU_CONTEXT_ERROR")
                after = fresh
                if not self._menu_still_open(opened, after):
                    self.locator.save_verified_candidate(
                        _MENU_CLOSE,
                        LocateCandidate(x=int(tap[0]), y=int(tap[1]), confidence=1.0, strategy_name="hitl", verified=True),
                        slot_name, after.screen_width, after.screen_height,
                    )
                    logger.info("bet menu: l'operatore ha chiuso il menu toccando (%d,%d): lo ricordo.", tap[0], tap[1])
                    return Result.ok(after)
        if toggle is None:
            return Result.fail("The menu is still open and its icon is unknown", "MENU_CLOSE_UNKNOWN")
        return Result.ok(after)    # come prima: si prosegue e sara' la lettura della puntata a dire se va bene

    @staticmethod
    def _menu_still_open(opened, after) -> bool:
        """Dopo la scelta: 4+ importi isolati E lo schermo quasi uguale a quello col menu aperto. Se la
        differenza e' grande il menu si e' chiuso (e gli importi visti sono la riga di puntate del gioco)."""
        if not _looks_like_menu(menu_amounts(after.text_boxes)):
            return False
        diff = diff_score(opened.screenshot_path, after.screenshot_path)
        return not (diff.success and diff.data >= _MENU_STILL_OPEN_DIFF)

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
