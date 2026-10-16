import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import (
    forget_autopilot_element, load_autopilot_flag, load_autopilot_value, save_autopilot_flag, save_autopilot_value,
)
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.menu_geometry import (
    bounds_from_fractions, is_inside, outside_point, panel_bounds,
)
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
_MAX_REVEAL_SWIPES = 3   # passaggi per direzione
_ROW_STATIC_FLAG = "bet_row_static"   # la fila di celle non scorre: quello che si vede e' tutto l'elenco
_SWIPE_MS = 700
_PANEL_VALUE = "bet_menu_panel"                 # "alto,basso" come frazioni dell'altezza (dalla mappa)
_CLOSE_VIA_TOGGLE_FLAG = "bet_menu_close_via_toggle"   # la mappa ha verificato che la stessa icona chiude il menu
_MAX_CLOSE_RETRIES = 3
_ROW_DIRECTION_VALUE = "bet_row_direction"   # verso dello scorrimento che ha abbassato la minima (appreso)
_MAX_PICK_ATTEMPTS = 3   # tocco -> verifica -> correzione: in un carosello la fila si ricentra dopo il tocco
_SETTLE_SECONDS = 1.5   # pausa prima di rileggere una puntata non leggibile (animazione di chiusura)
_MAX_ACCEPTED_LOWER_BET = Decimal("1")   # una puntata letta PIU' BASSA dell'attesa si accetta solo se e' un importo piccolo


@dataclass
class MenuSelection:
    final_bet: Decimal
    lowest_listed: Decimal
    toggle_strategy: str
    proven: bool = False   # nessuna puntata piu' bassa su nessun lato: e' davvero il minimo, non solo il piu' basso visibile
    # Il minimo noto (dichiarato o ricordato) era PIU' ALTO della puntata letta: non poteva essere il minimo. Chi chiama
    # ripara la memoria.
    contradicted_min: Optional[Decimal] = None


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


_BET_LABEL_WORDS = ("POSTA", "PUNTATA", "BET", "STAKE")
_GRID_ROW_TOLERANCE_PX = 40
_GRID_MIN_CELLS = 4
_RECENT_TAP_SECONDS = 15.0     # un valore appena toccato vale come puntata attuale solo per poco


def visible_grid(text_boxes: list) -> list:
    """[(valore, (cx, cy))] di una GRIGLIA DI SCELTE sempre visibile (Shining Crown: 0.10 0.20 0.30 0.40 0.50 con
    un'etichetta PUNTATA sotto ogni cella): almeno 4 importi distinti, solo-importo, plausibili come puntata
    (0.01-1000), sulla stessa riga. A differenza di `menu_amounts` NON scarta le celle vicine alle etichette (qui
    stanno proprio li'). Vuota se non c'e' un'etichetta di puntata o la riga e' troppo corta."""
    from orchestrator.modules.slot_ocr.reader import MAX_BET_VALUE, MIN_BET_VALUE
    if not any(any(word in box.text.upper() for word in _BET_LABEL_WORDS) for box in text_boxes):
        return []
    cells = []
    for box in text_boxes:
        if not _STANDALONE_AMOUNT.match(box.text):
            continue
        parsed = parse_amount(box.text)
        if not parsed.success or not Decimal(str(MIN_BET_VALUE)) <= parsed.data <= Decimal(str(MAX_BET_VALUE)):
            continue
        cells.append((parsed.data, (round((box.bbox[0] + box.bbox[2]) / 2), round((box.bbox[1] + box.bbox[3]) / 2))))
    best = []
    for _, (_, cy) in cells:
        row = [item for item in cells if abs(item[1][1] - cy) <= _GRID_ROW_TOLERANCE_PX]
        if len(row) > len(best):
            best = row
    return best if len({value for value, _ in best}) >= _GRID_MIN_CELLS else []


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

    def __init__(self, locator: ElementLocator, verifier: ActionVerifier, screen_recovery=None, supervisor=None,
                 scroll_probe: bool = False):
        # scroll_probe: prima di scegliere si prova a scorrere la fila di celle (le puntate basse possono essere fuori
        # schermo). Spento di default: lo accende la radice di composizione.
        self.scroll_probe = scroll_probe
        self._dead_toggles = {}
        self.last_tap: Optional[tuple] = None      # (slot, valore, istante) dell'ultima cella toccata
        self.locator = locator
        self.verifier = verifier
        # Opzionale: se la puntata riletta dopo la scelta non e' leggibile o non torna, Sol guarda lo schermo
        # intero prima di dichiarare il fallimento (l'OCR/visione economica puo' leggere un contatore jackpot).
        self.supervisor = supervisor
        # Opzionale: senza, un tocco sul bersaglio sbagliato non viene annullato.
        self.screen_recovery = screen_recovery

    def select_lowest(
        self, device_id: str, slot_name: str, context, context_provider: Callable[[], Result],
        allow_operator: bool = True, target: Optional[Decimal] = None,
    ) -> Result[MenuSelection]:
        """Sceglie la puntata dal menu e chiude. Se dopo la chiusura la puntata e' cambiata (un tocco ha colpito una
        cella) richiude il pannello se serve, lo riapre e riseleziona: al massimo 3 tentativi, poi l'errore passa a
        chi chiama (operatore)."""
        result = None
        for attempt in range(1, _MAX_CLOSE_RETRIES + 1):
            result = self._select_lowest_once(device_id, slot_name, context, context_provider, allow_operator, target)
            if result.success or result.error_code != "MENU_SELECTION_NOT_CONFIRMED":
                return result
            logger.warning("bet menu: dopo la chiusura la puntata e' cambiata (%s): tentativo %d/%d, richiudo se serve, "
                           "riapro e riseleziono.", result.error, attempt, _MAX_CLOSE_RETRIES)
            self._close_if_left_open(device_id, slot_name, context_provider)
        return result

    def _close_if_left_open(self, device_id: str, slot_name: str, context_provider: Callable[[], Result]) -> None:
        """Prima di riaprire: se il pannello e' ancora aperto lo si chiude toccando FUORI (mai l'icona, coperta)."""
        current = self._fresh(context_provider)
        if current is None or self._menu_bounds(current, slot_name, live_only=True) is None:
            return
        self._close_menu(device_id, slot_name, current, current, None, context_provider, False)

    def _select_lowest_once(
        self, device_id: str, slot_name: str, context, context_provider: Callable[[], Result],
        allow_operator: bool = True, target: Optional[Decimal] = None,
    ) -> Result[MenuSelection]:
        # target: la puntata cercata (minimo dichiarato/dimostrato); senza, la piu' bassa letta.
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
            self._note_dead_toggle(slot_name, toggle)
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
        self._dead_toggles.pop(slot_name, None)
        self.locator.save_verified_candidate(
            _MENU_TOGGLE, toggle, slot_name, context.screen_width, context.screen_height,
        )
        return self._select_from_open(device_id, opened, context_provider, toggle, slot_name, allow_operator, target)

    def _note_dead_toggle(self, slot_name: str, toggle) -> None:
        """Un'icona ricordata che non apre il menu per la seconda volta di fila e' un punto sbagliato: si dimentica."""
        if toggle.strategy_name != "memory":
            return
        previous, count = self._dead_toggles.get(slot_name, (None, 0))
        count = count + 1 if previous == (toggle.x, toggle.y) else 1
        self._dead_toggles[slot_name] = ((toggle.x, toggle.y), count)
        if count >= 2:
            logger.warning("bet menu: l'icona ricordata (%d,%d) non apre il menu per %d volte di fila: la dimentico.",
                           toggle.x, toggle.y, count)
            forget_autopilot_element(slot_name, _MENU_TOGGLE, self._memory_dir())
            self.locator.reject_position(_MENU_TOGGLE, toggle.x, toggle.y)
            self._dead_toggles.pop(slot_name, None)

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

    def recent_tapped_bet(self, slot_name: str, max_age: float = _RECENT_TAP_SECONDS) -> Optional[Decimal]:
        """Il valore della cella toccata da pochi secondi (per una lettura illeggibile subito dopo), o None."""
        if self.last_tap is None or self.last_tap[0] != slot_name:
            return None
        return self.last_tap[1] if time.monotonic() - self.last_tap[2] <= max_age else None

    def select_from_visible_grid(
        self, device_id: str, slot_name: str, context, context_provider: Callable[[], Result],
        target: Optional[Decimal] = None,
    ) -> Result[MenuSelection]:
        """Griglia di scelte SEMPRE visibile (nessun menu da aprire o chiudere): un solo tocco sulla cella piu' bassa (o
        su quella del minimo noto), poi si legge la puntata; se l'OCR non sa dire quale cella e' selezionata ci si
        fida del tocco appena fatto. Mai scorrimenti ne' tocchi "fuori dal pannello"."""
        cells = visible_grid(getattr(context, "text_boxes", []) or [])
        if not cells:
            return Result.fail("No visible grid of selectable bets", "BET_GRID_NOT_FOUND")
        wanted = [item for item in cells if target is not None and item[0] == target]
        lowest, (cell_x, cell_y) = wanted[0] if wanted else min(cells, key=lambda item: item[0])
        logger.info("bet menu: griglia di puntate visibile %s, tocco %s a (%d,%d)",
                    sorted({v for v, _ in cells}), lowest, cell_x, cell_y)
        tapped = self.verifier.verified_tap(device_id, cell_x, cell_y, Expectation(kind="screen_changed"))
        if not getattr(tapped, "success", False):
            return Result.fail("The tap on the bet cell failed", "MENU_TAP_FAILED")
        self.last_tap = (slot_name, lowest, time.monotonic())
        after = self._fresh(context_provider)
        seen = after.slot_ocr_state.get("posta") if after is not None else None
        if seen is None and self.supervisor is not None:
            seen = self.supervisor.read_bet(device_id)
        if seen is not None:
            read_bet = Decimal(str(seen))
            if read_bet == lowest:
                logger.info("Puntata letta: %.2f, attesa: %.2f (griglia visibile), OK", read_bet, lowest)
                return Result.ok(MenuSelection(final_bet=read_bet, lowest_listed=lowest, toggle_strategy="grid"))
            if read_bet < lowest and read_bet <= _MAX_ACCEPTED_LOWER_BET:
                logger.warning("bet menu: letta %s piu' bassa dell'attesa %s: tengo la letta.", read_bet, lowest)
                return Result.ok(MenuSelection(final_bet=read_bet, lowest_listed=lowest, toggle_strategy="grid"))
            return Result.fail(f"Bet after grid selection is {read_bet}, expected {lowest}", "MENU_SELECTION_NOT_CONFIRMED")
        if lowest > _MAX_ACCEPTED_LOWER_BET:
            return Result.fail(f"Bet after grid selection cannot be read (expected {lowest})", "MENU_SELECTION_UNREADABLE")
        logger.warning("Puntata non leggibile via OCR, uso valore appena impostato: %.2f", lowest)
        return Result.ok(MenuSelection(final_bet=lowest, lowest_listed=lowest, toggle_strategy="grid"))

    def select_from_open_menu(
        self, device_id: str, slot_name: str, context, context_provider: Callable[[], Result],
        toggle_xy: Optional[tuple] = None, target: Optional[Decimal] = None,
    ) -> Result[MenuSelection]:
        """Il menu e' GIA' aperto (l'ha aperto l'operatore): sceglie la puntata piu' bassa della lista.
        `toggle_xy` e' il punto che l'operatore ha fatto tappare per aprirlo: se la selezione riesce
        viene ricordato come icona del menu, cosi' la volta dopo non serve nessun aiuto."""
        toggle = None
        if toggle_xy is not None:
            toggle = LocateCandidate(
                x=int(toggle_xy[0]), y=int(toggle_xy[1]), confidence=1.0, strategy_name="hitl", verified=True,
            )
        result = self._select_from_open(device_id, context, context_provider, toggle, slot_name, True, target)
        if result.success and toggle is not None:
            self.locator.save_verified_candidate(
                _MENU_TOGGLE, toggle, slot_name, context.screen_width, context.screen_height,
            )
        return result

    def _select_from_open(
        self, device_id: str, opened, context_provider: Callable[[], Result], toggle: Optional[LocateCandidate],
        slot_name: str = "", allow_operator: bool = True, target: Optional[Decimal] = None,
    ) -> Result[MenuSelection]:
        cells = menu_amounts(opened.text_boxes)
        if not _looks_like_menu(cells):
            # Non e' una lista di puntate (es. pannello a righe con -/+): il menu
            # resta APERTO, il chiamante cambiera' la puntata con i pulsanti meno.
            logger.info("bet menu: aperto, %d importi isolati: non e' una lista selezionabile, lo lascio aperto", len(cells))
            return Result.fail("The open menu is not a list of selectable bets", "MENU_OPEN_NOT_A_LIST")

        opened, cells, proven = self._reveal_lower_cells(device_id, slot_name, opened, cells, context_provider)
        wanted_cells = [item for item in cells if target is not None and item[0] == target]
        lowest, (cell_x, cell_y) = wanted_cells[0] if wanted_cells else min(cells, key=lambda item: item[0])
        logger.info("bet menu: valori letti %s, cerco %s%s: tocco a (%d,%d)", sorted({v for v, _ in cells}), lowest,
                    " (puntata dichiarata)" if wanted_cells else " (la piu' bassa letta)", cell_x, cell_y)
        after = None
        tap_ok = False
        for attempt in range(_MAX_PICK_ATTEMPTS):
            tapped = self.verifier.verified_tap(device_id, cell_x, cell_y, Expectation(kind="screen_changed"))
            tap_ok = bool(getattr(tapped, "success", False))
            self.last_tap = (slot_name, lowest, time.monotonic())
            after = self._fresh(context_provider)
            if after is None:
                return Result.fail("Cannot read the screen after selecting the bet", "MENU_CONTEXT_ERROR")
            seen = after.slot_ocr_state.get("posta")
            if seen is not None and Decimal(str(seen)) == lowest:
                break
            # In un carosello la fila si ricentra dopo il tocco e le celle si spostano: si rilegge la fila e si
            # tocca la cella che mostra il valore cercato (non un punto vecchio).
            moved_cells = menu_amounts(after.text_boxes)
            again = [item for item in moved_cells if item[0] == lowest]
            if not _looks_like_menu(moved_cells) or not again:
                break
            new_x, new_y = again[0][1]
            if seen is None and abs(new_x - cell_x) < 10 and abs(new_y - cell_y) < 10:
                break                                   # stessa cella e puntata illeggibile: ritoccare non serve
            logger.info("bet menu: dopo il tocco la puntata e' %s e non %s (la fila si e' spostata): ritocco la cella "
                        "giusta a (%d,%d), tentativo %d/%d.", seen, lowest, new_x, new_y, attempt + 2, _MAX_PICK_ATTEMPTS)
            cell_x, cell_y = new_x, new_y

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
        if bet is None and tap_ok and lowest <= _MAX_ACCEPTED_LOWER_BET:
            # Appena toccata la cella `lowest`: se l'OCR non sa dire quale e' selezionata ci si fida del tocco. Restano
            # Bet Watch e il calo del saldo a ogni spin come controlli indipendenti.
            logger.warning("Puntata non leggibile via OCR, uso valore appena impostato: %.2f", lowest)
            return Result.ok(MenuSelection(
                final_bet=lowest, lowest_listed=lowest, proven=False,
                toggle_strategy=toggle.strategy_name if toggle is not None else "hitl",
            ))
        if bet is None:
            return Result.fail(
                f"Bet after menu selection cannot be read (expected {lowest})", "MENU_SELECTION_UNREADABLE",
            )
        source = "minimo noto (dichiarato o ricordato)" if wanted_cells else "la piu' bassa del menu"
        read_bet = Decimal(str(bet))
        if read_bet != lowest:
            if read_bet < lowest and read_bet <= _MAX_ACCEPTED_LOWER_BET:
                # Nessuna puntata sta sotto il minimo: se se ne legge una piu' bassa, e' il minimo atteso a essere
                # sbagliato (non la selezione). Si accetta, ma il minimo NON si da' per dimostrato.
                logger.warning(
                    "bet menu: il minimo atteso %s (%s) e' maggiore della puntata letta %s: il minimo creduto era "
                    "sbagliato (--min-bet o memoria?), lo ignoro e tengo %s.", lowest, source, read_bet, read_bet,
                )
                return Result.ok(MenuSelection(
                    final_bet=read_bet, lowest_listed=lowest, proven=False,
                    toggle_strategy=toggle.strategy_name if toggle is not None else "hitl",
                    contradicted_min=lowest if wanted_cells else None,
                ))
            return Result.fail(
                f"Bet after menu selection is {bet}, expected {lowest} ({source})", "MENU_SELECTION_NOT_CONFIRMED",
            )
        logger.info("Puntata letta: %.2f, attesa: %.2f (%s), OK", read_bet, lowest, source)
        return Result.ok(MenuSelection(
            final_bet=Decimal(str(bet)), lowest_listed=lowest,
            toggle_strategy=toggle.strategy_name if toggle is not None else "hitl", proven=proven,
        ))

    def _reveal_lower_cells(self, device_id: str, slot_name: str, opened, cells: list, context_provider) -> tuple:
        """(contesto, celle, dimostrato). La fila di celle puo' scorrere in orizzontale con le puntate piu' basse fuori
        schermo (o tagliate al bordo, e allora l'OCR non le legge). Si prova a scorrere a SINISTRA e, se la minima
        non scende, a DESTRA, fino a 3 passaggi per direzione con una rilettura dopo ognuno; si continua nella
        direzione che abbassa la minima. 'Dimostrato' = in nessuna direzione compare qualcosa di piu' basso (o la
        fila non si muove affatto): solo allora il piu' basso visto e' il minimo vero. Una fila che non scorre si
        ricorda (`bet_row_static`) e non viene piu' provata."""
        injector = getattr(self.verifier, "touch_injector", None)
        swipe = getattr(injector, "swipe", None)
        width = getattr(opened, "screen_width", None)
        if not self.scroll_probe or not callable(swipe) or not isinstance(width, int) or width <= 0:
            return opened, cells, False
        if load_autopilot_flag(slot_name, _ROW_STATIC_FLAG, self._memory_dir()):
            return opened, cells, True

        row_y = int(sorted(position[1] for _, position in cells)[len(cells) // 2])
        best_min = min(value for value, _ in cells)
        shown = (opened, cells)            # cosa mostra lo schermo adesso
        origin_values = {value for value, _ in cells}
        progressed_any = changed_any = False
        last_back = "right"
        remembered = load_autopilot_value(slot_name, _ROW_DIRECTION_VALUE, self._memory_dir())
        order = (("right", "left"), ("left", "right")) if remembered == "right" else (("left", "right"), ("right", "left"))
        for direction, back in order:
            steps = 0
            progressed = False
            last_back = back
            while steps < _MAX_REVEAL_SWIPES:
                a, b = (0.85, 0.25) if direction == "left" else (0.25, 0.85)
                moved = swipe(device_id, int(width * a), row_y, int(width * b), row_y, _SWIPE_MS)
                if not getattr(moved, "success", False):
                    break
                time.sleep(_SETTLE_SECONDS)
                fresh = self._fresh(context_provider)
                if fresh is None:
                    break
                steps += 1
                fresh_cells = menu_amounts(fresh.text_boxes)
                if not _looks_like_menu(fresh_cells):
                    break
                values = {value for value, _ in fresh_cells}
                logger.info("bet menu: scorro a %s (passaggio %d): valori %s (minima finora %s)",
                            direction, steps, sorted(values), best_min)
                changed_any = changed_any or values != origin_values
                shown = (fresh, fresh_cells)
                if min(values) < best_min:
                    best_min = min(values)
                    progressed = progressed_any = True
                    continue
                break
            if progressed:
                save_autopilot_value(slot_name, _ROW_DIRECTION_VALUE, direction, self._memory_dir())
                break                       # trovata una minima piu' bassa: si ferma qui
            if steps and {v for v, _ in shown[1]} != origin_values:
                for _ in range(steps):      # si torna dove si era, poi si prova l'altra direzione
                    swipe(device_id, int(width * (0.25 if back == "right" else 0.85)), row_y,
                          int(width * (0.85 if back == "right" else 0.25)), row_y, _SWIPE_MS)
                time.sleep(_SETTLE_SECONDS)
        if not changed_any:
            shown = (opened, cells)          # la fila non si e' mossa: lo schermo e' quello di partenza
        elif min(value for value, _ in shown[1]) > best_min:
            # L'ultimo passaggio ha superato la minima trovata: un passo indietro e si rilegge.
            swipe(device_id, int(width * (0.25 if last_back == "right" else 0.85)), row_y,
                  int(width * (0.85 if last_back == "right" else 0.25)), row_y, _SWIPE_MS)
            time.sleep(_SETTLE_SECONDS)
            fresh = self._fresh(context_provider)
            if fresh is not None and _looks_like_menu(menu_amounts(fresh.text_boxes)):
                shown = (fresh, menu_amounts(fresh.text_boxes))
        elif not progressed_any and shown[0] is not opened:
            fresh = self._fresh(context_provider)    # tornati all'origine: si rilegge la fila
            if fresh is not None and _looks_like_menu(menu_amounts(fresh.text_boxes)):
                shown = (fresh, menu_amounts(fresh.text_boxes))
        if not changed_any:
            save_autopilot_flag(slot_name, _ROW_STATIC_FLAG, True, self._memory_dir())
            logger.info("bet menu: la fila di celle non scorre: quello che si vede e' tutto l'elenco.")
        proven = not progressed_any
        return shown[0], shown[1], proven

    def ensure_closed(self, device_id: str, slot_name: str, context_provider: Callable[[], Result]) -> bool:
        """Dopo aver cambiato la puntata: se Sol vede ancora un pannello aperto lo si chiude (punto ricordato ->
        icona -> operatore, e il punto che chiude viene ricordato). True se c'era da chiudere e si e' chiuso."""
        check = getattr(self.supervisor, "check_menu_open", None)
        if not callable(check):
            return False
        verdict = check(device_id)
        if verdict != "open":
            return False
        logger.info("bet menu: Sol vede ancora un pannello aperto dopo la scelta della puntata: lo chiudo.")
        current = self._fresh(context_provider)
        if current is None:
            return False
        closed = self._close_menu(device_id, slot_name, current, current, None, context_provider, True)
        if not closed.success:
            logger.warning("bet menu: pannello rimasto aperto (%s).", closed.error_code)
        return closed.success

    def _menu_bounds(self, context, slot_name: str, live_only: bool = False) -> Optional[tuple]:
        """(alto, basso) in pixel del pannello. Dallo screenshot con il menu aperto, ancorato alla riga delle celle
        lette dall'OCR; altrimenti (se non e' live_only) dai bounds salvati nella mappa."""
        cells = menu_amounts(getattr(context, "text_boxes", []) or [])
        path = getattr(context, "screenshot_path", None)
        if cells and path:
            ys = sorted(position[1] for _, position in cells)
            bounds = panel_bounds(path, ys[len(ys) // 2])
            if bounds is not None:
                return bounds
        if live_only:
            return None
        raw = load_autopilot_value(slot_name, _PANEL_VALUE, self._memory_dir())
        try:
            top, bottom = (float(v) for v in str(raw).split(","))
            return bounds_from_fractions((top, bottom), int(context.screen_height))
        except (TypeError, ValueError, AttributeError):
            return None

    def _tap_and_check_closed(self, device_id: str, point: tuple, opened, context_provider):
        """Tocca e ritorna (chiuso, schermo_dopo); schermo None se non si legge."""
        self.verifier.verified_tap(device_id, int(point[0]), int(point[1]), Expectation(kind="screen_changed"))
        fresh = self._fresh(context_provider)
        if fresh is None:
            return False, None
        return not self._menu_still_open(opened, fresh), fresh

    def _close_menu(
        self, device_id: str, slot_name: str, opened, after, toggle, context_provider, allow_operator: bool,
    ) -> Result:
        """Chiude il menu rimasto aperto. Ogni tocco di chiusura cade FUORI dal pannello (un tocco dentro seleziona
        una cella e cambia la puntata). Ordine: punto ricordato (se fuori dal pannello), punto calcolato fuori dal
        pannello, icona SOLO se la mappa ha verificato che chiude, operatore. Dopo ogni tentativo si ricontrolla."""
        size = (getattr(opened, "screen_width", 0), getattr(opened, "screen_height", 0))
        bounds = self._menu_bounds(opened, slot_name) or self._menu_bounds(after, slot_name)

        memo = self.locator.locate(_MENU_CLOSE, after, exclude_strategies=_ALL_BUT_MEMORY)
        if memo.success:
            if is_inside((memo.data.x, memo.data.y), bounds):
                logger.warning("bet menu: il punto ricordato per chiudere (%d,%d) cade DENTRO il pannello: lo dimentico.",
                               memo.data.x, memo.data.y)
                forget_autopilot_element(slot_name, _MENU_CLOSE, self._memory_dir())
            else:
                closed, fresh = self._tap_and_check_closed(device_id, (memo.data.x, memo.data.y), opened, context_provider)
                if fresh is None:
                    return Result.fail("Cannot read the screen after closing the menu", "MENU_CONTEXT_ERROR")
                after = fresh
                if closed:
                    return Result.ok(after)
                logger.warning("bet menu: il punto ricordato per chiudere il menu non e' servito: lo dimentico.")
                forget_autopilot_element(slot_name, _MENU_CLOSE, self._memory_dir())

        if size[0] and size[1]:
            point = outside_point(bounds, size)
            logger.info("bet menu: chiudo toccando fuori dal pannello a (%d,%d) (pannello %s).", point[0], point[1],
                        bounds if bounds is not None else "non trovato")
            closed, fresh = self._tap_and_check_closed(device_id, point, opened, context_provider)
            if fresh is None:
                return Result.fail("Cannot read the screen after closing the menu", "MENU_CONTEXT_ERROR")
            after = fresh
            if closed:
                self.locator.save_verified_candidate(
                    _MENU_CLOSE, LocateCandidate(x=point[0], y=point[1], confidence=1.0, strategy_name="computed", verified=True),
                    slot_name, size[0], size[1],
                )
                return Result.ok(after)
            logger.warning("bet menu: il tocco fuori dal pannello non ha chiuso il menu.")

        if toggle is None:
            located = self.locator.locate(_MENU_TOGGLE, after, exclude_strategies=("hitl",))
            toggle = located.data if located.success else None
        if toggle is not None and load_autopilot_flag(slot_name, _CLOSE_VIA_TOGGLE_FLAG, self._memory_dir()) \
                and not is_inside((toggle.x, toggle.y), bounds):
            closed, fresh = self._tap_and_check_closed(device_id, (toggle.x, toggle.y), opened, context_provider)
            if fresh is None:
                return Result.fail("Cannot read the screen after closing the menu", "MENU_CONTEXT_ERROR")
            after = fresh
            if closed:
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
                    if is_inside(tap, bounds):
                        logger.warning("bet menu: il tocco dell'operatore (%d,%d) e' dentro il pannello: non lo ricordo "
                                       "come punto di chiusura.", tap[0], tap[1])
                    else:
                        self.locator.save_verified_candidate(
                            _MENU_CLOSE,
                            LocateCandidate(x=int(tap[0]), y=int(tap[1]), confidence=1.0, strategy_name="hitl", verified=True),
                            slot_name, after.screen_width, after.screen_height,
                        )
                        logger.info("bet menu: l'operatore ha chiuso il menu toccando (%d,%d): lo ricordo.", tap[0], tap[1])
                    return Result.ok(after)
        return Result.ok(after)    # si prosegue: sara' la rilettura della puntata a dire se va bene

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
