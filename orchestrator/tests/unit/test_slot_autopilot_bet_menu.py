from decimal import Decimal
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector, menu_amounts
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_autopilot.verifier import TapOutcome
from orchestrator.modules.slot_ocr.reader import OCRBox


def _box(text, x, y):
    return OCRBox(text=text, bbox=(x - 30, y - 15, x + 30, y + 15), confidence=0.9)


MENU_BOXES = [
    _box("0,10", 200, 1000), _box("0,20", 400, 1000), _box("0,50", 600, 1000),
    _box("1,00", 200, 1200), _box("2,00", 400, 1200),
]
# Quadro principale: importi sulla stessa riga delle etichette.
MAIN_BOXES = [_box("CREDITO", 150, 2200), _box("€99.998,00", 350, 2200),
              _box("PUNTATA", 600, 2200), _box("€0,10", 800, 2200)]


def _context(boxes, posta=None):
    state = {"posta": posta} if posta is not None else {}
    return LocateContext(
        device_id="dev", slot_name="bigbass", screenshot_path="/tmp/x.png", screen_width=1080,
        screen_height=2400, text_boxes=boxes, slot_ocr_state=state,
    )


def _tap(verified=True, outcome=None):
    return Result.ok(TapOutcome(
        tapped_coords=(0, 0), verified=verified, screenshot_before="a", screenshot_after="b",
        diff_score=0.3, balance_before=None, balance_after=None, bet_before=None, bet_after=None,
        screen_state_before="idle", screen_state_after="idle",
        outcome=outcome or ("success" if verified else "no_effect"),
    ))


class TestMenuAmounts:
    def test_standalone_amounts_are_cells_but_main_panel_values_are_not(self):
        cells = menu_amounts(MENU_BOXES + MAIN_BOXES)
        assert sorted(value for value, _ in cells) == [Decimal("0.10"), Decimal("0.20"), Decimal("0.50"),
                                                        Decimal("1.00"), Decimal("2.00")]

    def test_cell_coordinates_are_box_centers(self):
        cells = dict((value, xy) for value, xy in menu_amounts(MENU_BOXES))
        assert cells[Decimal("0.20")] == (400, 1000)

    def test_text_that_is_not_only_an_amount_is_ignored(self):
        assert menu_amounts([_box("PUNTATA TOTALE 0,10", 300, 1000), _box("SPIN 3", 300, 1200)]) == []


def _selector(strategy="memory", trusted=True):
    locator = MagicMock()
    locator.locate.return_value = Result.ok(
        LocateCandidate(x=540, y=2088, confidence=0.9, strategy_name=strategy))
    locator.is_trusted_strategy.return_value = trusted
    verifier = MagicMock()
    verifier.verified_tap.return_value = _tap(True)
    return BetMenuSelector(locator, verifier), locator, verifier


def _provider(*contexts):
    return MagicMock(side_effect=[Result.ok(c) for c in contexts])


class TestSelectLowest:
    def test_opens_menu_taps_lowest_and_confirms_the_read_bet(self):
        selector, locator, verifier = _selector()
        provider = _provider(_context(MENU_BOXES), _context(MAIN_BOXES, posta=0.10))

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES, posta=2.0), provider)

        assert result.success
        assert result.data.final_bet == Decimal("0.10")
        taps = [call.args[1:3] for call in verifier.verified_tap.call_args_list]
        assert taps == [(540, 2088), (200, 1000)]   # toggle, poi la cella 0,10
        locator.save_verified_candidate.assert_called_once()

    def test_menu_left_open_after_selection_is_closed_with_the_same_toggle(self):
        selector, _, verifier = _selector()
        provider = _provider(_context(MENU_BOXES), _context(MENU_BOXES, posta=0.10), _context(MAIN_BOXES, posta=0.10))

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES, posta=2.0), provider)

        assert result.success
        taps = [call.args[1:3] for call in verifier.verified_tap.call_args_list]
        assert taps == [(540, 2088), (200, 1000), (540, 2088)]

    def test_toggle_without_effect_fails_and_records_the_failure_for_untrusted_sources(self):
        selector, locator, verifier = _selector(strategy="gpt", trusted=False)
        verifier.verified_tap.return_value = _tap(False)

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES), _provider())

        assert result.error_code == "MENU_TOGGLE_NO_EFFECT"
        locator.record_failure.assert_called_once_with("gpt", "bet_menu_toggle")
        locator.save_verified_candidate.assert_not_called()

    def test_menu_that_is_not_a_list_is_left_open_for_the_caller(self):
        selector, _, verifier = _selector()

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES), _provider(_context(MAIN_BOXES)))

        assert result.error_code == "MENU_OPEN_NOT_A_LIST"
        assert verifier.verified_tap.call_count == 1   # solo l'apertura: nessuna richiusura

    def test_stepper_panel_with_three_amounts_is_not_treated_as_a_list(self):
        # pannello a righe: "10" monete per riga, "€0,02" valore moneta, "€2,00" puntata totale
        stepper = [_box("10", 500, 800), _box("€0,02", 500, 1000), _box("€2,00", 500, 1200)] + MAIN_BOXES
        selector, _, verifier = _selector()

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES), _provider(_context(stepper)))

        assert result.error_code == "MENU_OPEN_NOT_A_LIST"
        assert verifier.verified_tap.call_count == 1   # nessun tocco sui campi dei valori

    def test_bet_not_matching_the_selected_value_is_not_confirmed(self):
        selector, _, _ = _selector()
        provider = _provider(_context(MENU_BOXES), _context(MAIN_BOXES, posta=2.0))

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES, posta=2.0), provider)

        assert result.error_code == "MENU_SELECTION_NOT_CONFIRMED"

    def test_toggle_not_locatable_fails(self):
        selector, locator, _ = _selector()
        locator.locate.return_value = Result.fail("nope", "ELEMENT_NOT_LOCATABLE")

        assert not selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES), _provider()).success


class TestWrongToggleTarget:
    def _wrong_toggle(self, recovery_result=None, with_recovery=True):
        selector, locator, verifier = _selector(strategy="gpt", trusted=False)
        verifier.verified_tap.return_value = Result.ok(TapOutcome(
            tapped_coords=(599, 2115), verified=False, screenshot_before="/tmp/before.png",
            screenshot_after="/tmp/after.png", diff_score=0.003, balance_before=Decimal("100000"),
            balance_after=Decimal("500000"), bet_before=Decimal("2.00"), bet_after=Decimal("10.00"),
            screen_state_before="idle", screen_state_after="idle", outcome="unexpected_change",
        ))
        recovery = MagicMock()
        recovery.recover.return_value = recovery_result or Result.ok(MagicMock(method="retap"))
        if with_recovery:
            selector.screen_recovery = recovery
        return selector, locator, recovery

    def test_wrong_target_is_rejected_undone_and_remembered(self):
        selector, locator, recovery = self._wrong_toggle()

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES, posta=2.0), _provider())

        assert result.error_code == "MENU_TOGGLE_WRONG_TARGET"
        locator.reject_position.assert_called_once_with("bet_menu_toggle", 540, 2088)
        args = recovery.recover.call_args.args
        assert args[1] == "/tmp/before.png" and args[2] == (540, 2088)
        assert args[4] == {"posta": Decimal("2.00"), "saldo": Decimal("100000")}
        locator.remember_rejected.assert_called_once_with("bigbass", "bet_menu_toggle", 540, 2088)

    def test_nothing_is_remembered_permanently_when_the_screen_was_already_original(self):
        selector, locator, _ = self._wrong_toggle(Result.ok(MagicMock(method="already_original")))

        selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES, posta=2.0), _provider())

        locator.remember_rejected.assert_not_called()

    def test_failed_recovery_reports_stuck(self):
        selector, locator, _ = self._wrong_toggle(Result.fail("no way back", "RECOVERY_FAILED"))

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES, posta=2.0), _provider())

        assert result.error_code == "MENU_STUCK"
        locator.remember_rejected.assert_not_called()

    def test_without_a_recovery_the_point_is_only_rejected_for_the_session(self):
        selector, locator, _ = self._wrong_toggle(with_recovery=False)

        result = selector.select_lowest("dev", "bigbass", _context(MAIN_BOXES, posta=2.0), _provider())

        assert result.error_code == "MENU_TOGGLE_WRONG_TARGET"
        locator.reject_position.assert_called_once()
        locator.remember_rejected.assert_not_called()
