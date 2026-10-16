"""Shining Crown: la schermata di scelta della puntata e' una GRIGLIA (0.10 ... 0.50, un'etichetta PUNTATA per colonna)
con i jackpot in alto: la puntata non si indovina, i jackpot non sono mai puntate, e dopo il tocco ci si fida del tocco."""
import json
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector, visible_grid
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_ocr.reader import MAX_BET_VALUE, OCRBox, SlotOCRReader, choose_bet, parse_bet
from orchestrator.modules.slot_ocr.vision_reader import ResilientOCRReader, VisionAmountReader
from orchestrator.tests.unit.test_slot_autopilot_bet_controller import _config, _locator_mock, _tap_outcome
from orchestrator.tests.unit.test_slot_autopilot_bet_menu import _provider

SIZE = (1080, 2400)


# ---- parse_bet: i sei casi richiesti -----------------------------------------------------------------------------------

def test_a_grid_with_the_value_just_tapped_uses_it():
    assert parse_bet(["0.10", "0.20", "0.30", "PUNTATA"], last_tapped_bet=0.10) == 0.10


def test_a_grid_without_a_tapped_value_is_not_guessed():
    assert parse_bet(["0.10", "0.20", "0.30", "PUNTATA"]) is None


def test_a_jackpot_is_never_a_bet():
    assert parse_bet(["1132915.53", "0.10", "PUNTATA"]) == 0.10


def test_the_balance_is_never_a_bet():
    assert parse_bet(["50 000.00", "SALDO", "0.20", "PUNTATA"]) == 0.20


def test_a_single_candidate_is_the_bet():
    assert parse_bet(["0.10"]) == 0.10


def test_no_candidate_no_bet():
    assert parse_bet(["50000.00", "SALDO"]) is None
    assert parse_bet([]) is None and parse_bet(["FUN", "SHINING CROWN"]) is None


def test_the_tapped_value_must_be_among_the_candidates():
    assert parse_bet(["0.10", "0.20", "0.30", "PUNTATA"], last_tapped_bet=4.0) is None
    assert choose_bet([0.10, 0.20, 0.30], 1, None) is None
    assert choose_bet([2000.0, 0.005], 1) is None                               # fuori dai limiti plausibili


# ---- il lettore OCR con le posizioni: la griglia di Shining Crown -----------------------------------------------------

def _det(text, x, y, w=120, h=40, conf=0.9):
    return ([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], text, conf)


def _reader(detections):
    reader = SlotOCRReader()
    reader._get_raw_detections = lambda path: detections
    return reader


def _shining_crown_detections():
    jackpots = [_det("15 147 350.64", 100, 100), _det("1 132 912.60", 500, 100), _det("66 171.30", 100, 170),
                _det("19808.68", 500, 170)]
    balance = [_det("SHINING CROWN", 400, 400), _det("SALDO:", 100, 1900), _det("50 000.00", 300, 1900),
               _det("ULTIMA VINCITA:", 600, 1900), _det("FAI LA TUA PUNTATA", 300, 1100)]
    cells = [_det(v, 120 + 200 * i, 1200) for i, v in enumerate(("0.10", "0.20", "0.30", "0.40", "0.50"))]
    labels = [_det("PUNTATA", 100 + 200 * i, 1230) for i in range(5)]
    return jackpots + balance + cells + labels


def test_the_shining_crown_grid_gives_the_balance_and_no_guess_for_the_bet(caplog):
    with caplog.at_level(logging.INFO):
        state = _reader(_shining_crown_detections()).read_slot_state("x.png")

    assert state["saldo"] == 50000.0
    assert state["posta"] is None                       # ne' 0.50 (l'ultima colonna) ne' un jackpot
    assert "griglia di scelte, non indovino" in caplog.text


def test_a_jackpot_next_to_the_bet_label_is_ignored():
    detections = [_det("PUNTATA", 100, 500), _det("1 132 912.60", 400, 510), _det("0.10", 700, 505)]

    assert _reader(detections).read_slot_state("x.png")["posta"] == 0.10


def test_a_jackpot_alone_near_the_label_leaves_the_bet_unknown():
    detections = [_det("PUNTATA", 100, 500), _det("1 132 912.60", 400, 510)]

    assert _reader(detections).read_slot_state("x.png")["posta"] is None


def test_a_normal_screen_still_reads_the_bet_next_to_its_label():
    detections = [_det("SALDO", 100, 2000), _det("114,36", 300, 2000), _det("PUNTATA", 600, 2000),
                  _det("0,20", 800, 2000)]
    state = _reader(detections).read_slot_state("x.png")

    assert state["saldo"] == 114.36 and state["posta"] == 0.20


def test_a_bet_written_in_the_label_string_above_the_cap_is_dropped():
    detections = [_det("PUNTATA 1 132 912.60", 100, 500)]

    assert _reader(detections).read_slot_state("x.png")["posta"] is None


# ---- la visione non scambia un jackpot per la puntata -------------------------------------------------------------------

def _vision_with(tmp_path, bet_text):
    path = tmp_path / "shot.png"
    Image.new("RGB", (200, 400), "black").save(path)
    client = MagicMock()
    client.model = "m"
    client.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"credit_text": "50 000.00", "bet_text": bet_text})))])
    return VisionAmountReader(client).read(str(path))


def test_vision_reading_a_jackpot_as_the_bet_is_discarded(tmp_path):
    result = _vision_with(tmp_path, "1 132 915.53")

    assert result["saldo"] == 50000.0 and result["posta"] is None


def test_vision_reading_a_small_bet_is_kept(tmp_path):
    assert _vision_with(tmp_path, "0.10")["posta"] == 0.10


def test_the_sanity_check_drops_any_bet_above_the_cap_even_without_a_balance():
    assert ResilientOCRReader._sane({"saldo": None, "posta": 1132915.53})["posta"] is None
    assert ResilientOCRReader._sane({"saldo": None, "posta": 0.10})["posta"] == 0.10
    assert MAX_BET_VALUE == 1000.0


# ---- la griglia visibile e il tocco di cui ci si fida -------------------------------------------------------------------

def _boxes(with_labels=True, values=("0.10", "0.20", "0.30", "0.40", "0.50")):
    boxes = [OCRBox(text=v, bbox=(120 + 200 * i - 60, 1180, 120 + 200 * i + 60, 1220), confidence=0.9)
             for i, v in enumerate(values)]
    if with_labels:
        boxes += [OCRBox(text="PUNTATA", bbox=(100 + 200 * i - 60, 1230, 100 + 200 * i + 60, 1270), confidence=0.9)
                  for i in range(5)]
    boxes += [OCRBox(text="1 132 912.60", bbox=(440, 80, 560, 120), confidence=0.9),
              OCRBox(text="50 000.00", bbox=(240, 1880, 360, 1920), confidence=0.9)]
    return boxes


def _context(boxes, posta=None):
    state = {"saldo": 50000.0}
    if posta is not None:
        state["posta"] = posta
    return LocateContext(device_id="d", slot_name="Shining", screenshot_path="/nonexistent/x.png",
                         screen_width=SIZE[0], screen_height=SIZE[1], text_boxes=boxes, slot_ocr_state=state)


def test_the_visible_grid_is_the_row_of_choices_not_the_jackpots_or_the_balance():
    cells = visible_grid(_boxes())

    assert [v for v, _ in cells] == [Decimal("0.10"), Decimal("0.20"), Decimal("0.30"), Decimal("0.40"), Decimal("0.50")]
    assert visible_grid(_boxes(with_labels=False)) == []                      # senza etichetta di puntata non e' una griglia
    assert visible_grid(_boxes(values=("0.10", "0.20", "0.30"))) == []        # troppo corta


def _selector():
    locator = MagicMock()
    verifier = MagicMock()
    verifier.verified_tap.return_value = Result.ok(_tap_outcome(None, None, True, "success"))
    return BetMenuSelector(locator, verifier), verifier


def test_selecting_from_a_visible_grid_taps_the_lowest_cell_once_and_never_closes_anything(caplog):
    selector, verifier = _selector()
    provider = _provider(_context(_boxes(), posta=0.10))

    with caplog.at_level(logging.INFO):
        result = selector.select_from_visible_grid("d", "Shining", _context(_boxes()), provider)

    assert result.success and result.data.final_bet == Decimal("0.10")
    assert [c.args[1:3] for c in verifier.verified_tap.call_args_list] == [(120, 1200)]
    assert "Puntata letta: 0.10, attesa: 0.10" in caplog.text


def test_if_the_bet_stays_unreadable_the_tap_just_made_is_trusted(caplog):
    selector, _ = _selector()
    provider = _provider(_context(_boxes()), _context(_boxes()))

    with caplog.at_level(logging.WARNING):
        result = selector.select_from_visible_grid("d", "Shining", _context(_boxes()), provider)

    assert result.success and result.data.final_bet == Decimal("0.10")
    assert "Puntata non leggibile via OCR, uso valore appena impostato: 0.10" in caplog.text
    assert selector.recent_tapped_bet("Shining") == Decimal("0.10") and selector.recent_tapped_bet("Altra") is None


def test_a_different_bet_that_is_read_is_still_an_error():
    selector, _ = _selector()

    result = selector.select_from_visible_grid("d", "Shining", _context(_boxes()), _provider(_context(_boxes(), posta=0.40)))

    assert result.error_code == "MENU_SELECTION_NOT_CONFIRMED"


def test_a_failed_tap_is_not_trusted():
    selector, verifier = _selector()
    verifier.verified_tap.return_value = Result.fail("x", "TAP_FAILED")

    result = selector.select_from_visible_grid("d", "Shining", _context(_boxes()), _provider(_context(_boxes())))

    assert result.error_code == "MENU_TAP_FAILED"


def test_a_screen_without_a_grid_is_refused_without_tapping():
    selector, verifier = _selector()

    result = selector.select_from_visible_grid("d", "Shining", _context(_boxes(with_labels=False)), _provider())

    assert result.error_code == "BET_GRID_NOT_FOUND" and not verifier.verified_tap.called


# ---- set_min_bet: la puntata illeggibile con una griglia visibile non e' piu' un vicolo cieco --------------------------

def _controller(tmp_path):
    selector, selector_verifier = _selector()
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    controller = BetController(_locator_mock(), MagicMock(), step_logger,
                               _config(slot_memory_dir=str(tmp_path / "mem")), menu_selector=selector)
    return controller, selector_verifier


def test_set_min_bet_with_an_unreadable_bet_and_a_visible_grid_taps_the_lowest_cell(tmp_path):
    controller, taps = _controller(tmp_path)
    provider = MagicMock(return_value=Result.ok(_context(_boxes())))

    result = controller.set_min_bet("d", "Shining", "ep", provider)

    assert result.success and result.data.min_bet_verified and result.data.final_bet == Decimal("0.10")
    assert [c.args[1:3] for c in taps.verified_tap.call_args_list] == [(120, 1200)]


def test_set_min_bet_with_an_unreadable_bet_and_no_grid_touches_nothing(tmp_path):
    controller, taps = _controller(tmp_path)
    provider = MagicMock(return_value=Result.ok(_context(_boxes(with_labels=False)[:0])))

    result = controller.set_min_bet("d", "Shining", "ep", provider)

    assert result.data.aborted_reason == "BET_UNREADABLE" and not taps.verified_tap.called


def test_a_bet_that_cannot_be_read_right_after_the_tap_uses_the_value_just_set(tmp_path, caplog):
    controller, _ = _controller(tmp_path)
    controller.menu_selector.select_from_visible_grid(
        "d", "Shining", _context(_boxes()), MagicMock(return_value=Result.ok(_context(_boxes()))))

    with caplog.at_level(logging.WARNING):
        value = controller._read_bet(_context(_boxes()))

    assert value == Decimal("0.10") and "uso valore appena impostato: 0.10" in caplog.text
    assert controller._read_bet(_context(_boxes(with_labels=False))) is None               # niente griglia: nessuna fiducia
    assert controller._read_bet(_context(_boxes(), posta=0.30)) == Decimal("0.30")         # se si legge, vale la lettura
