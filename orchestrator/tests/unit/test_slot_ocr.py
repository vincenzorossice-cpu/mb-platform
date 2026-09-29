from unittest.mock import MagicMock

import pytest

from orchestrator.flows.casino_flow import SlotOCRReader as ReExportedSlotOCRReader
from orchestrator.modules.slot_ocr.reader import OCRBox, SlotOCRReader


def _detection(text: str, x: int, y: int, conf: float = 0.9):
    """Riproduce il formato nativo di easyocr.Reader.readtext(): (bbox, text, conf)."""
    bbox = [[x, y], [x + 150, y], [x + 150, y + 30], [x, y + 30]]
    return (bbox, text, conf)


def _rotated_detection(text: str, x1, y1, x2, y2, x3, y3, x4, y4, conf: float = 0.9):
    """Un quadrilatero NON assiale (i 4 punti non formano un rettangolo dritto),
    per verificare che read_text_boxes calcoli davvero min/max su tutti e 4 i
    punti e non si limiti al primo angolo (come fa raw_texts in read_slot_state)."""
    bbox = [[x1, y1], [x2, y2], [x3, y3], [x4, y4]]
    return (bbox, text, conf)


def _ocr_reader_with(detections: list) -> SlotOCRReader:
    reader = SlotOCRReader()
    reader._ocr = MagicMock()
    reader._ocr.readtext.return_value = detections
    return reader


class TestReadSlotStateUnchanged:
    """Regression: il comportamento di read_slot_state non cambia dopo lo spostamento."""

    def test_saldo_posta_vincita_parsed(self):
        r = _ocr_reader_with([
            _detection("SALDO 114,36", 10, 10),
            _detection("POSTA 0,20", 10, 100),
            _detection("VINCITA 5,00", 10, 200),
        ])
        state = r.read_slot_state("/tmp/shot.png")
        assert state["saldo"] == 114.36
        assert state["posta"] == 0.20
        assert state["vincita"] == 5.00

    def test_confidence_below_threshold_dropped(self):
        r = _ocr_reader_with([_detection("SALDO 114,36", 10, 10, conf=0.1)])
        state = r.read_slot_state("/tmp/shot.png")
        assert state["saldo"] is None
        assert state["raw_texts"] == []


class TestReadTextBoxes:
    def test_bbox_is_true_min_max_of_all_four_points(self):
        """Un quad non assiale: bbox deve essere il min/max reale dei 4 punti,
        non solo il primo angolo (a differenza di raw_texts in read_slot_state)."""
        r = _ocr_reader_with([
            _rotated_detection("SPIN", 20, 5, 60, 0, 65, 30, 15, 35),
        ])
        result = r.read_text_boxes("/tmp/shot.png")
        assert result.success
        boxes = result.data
        assert len(boxes) == 1
        box = boxes[0]
        assert isinstance(box, OCRBox)
        assert box.text == "SPIN"
        assert box.bbox == (15, 0, 65, 35)
        assert box.confidence == 0.9

    def test_confidence_below_threshold_dropped(self):
        r = _ocr_reader_with([_detection("SPIN", 10, 10, conf=0.1)])
        result = r.read_text_boxes("/tmp/shot.png")
        assert result.success
        assert result.data == []

    def test_empty_readtext_result(self):
        r = _ocr_reader_with([])
        result = r.read_text_boxes("/tmp/shot.png")
        assert result.success
        assert result.data == []

    def test_ocr_exception_returns_fail(self):
        r = SlotOCRReader()
        r._ocr = MagicMock()
        r._ocr.readtext.side_effect = RuntimeError("boom")
        result = r.read_text_boxes("/tmp/shot.png")
        assert not result.success
        assert result.error_code == "OCR_ERROR"

    def test_shared_lazy_ocr_instance(self):
        r = SlotOCRReader()
        with pytest.MonkeyPatch.context() as mp:
            fake_reader_instance = MagicMock()
            fake_reader_instance.readtext.return_value = []
            fake_easyocr_module = MagicMock()
            fake_easyocr_module.Reader.return_value = fake_reader_instance
            import sys
            mp.setitem(sys.modules, "easyocr", fake_easyocr_module)
            r.read_slot_state("/tmp/shot.png")
            r.read_text_boxes("/tmp/shot.png")
            assert fake_easyocr_module.Reader.call_count == 1


class TestRawDetectionCache:
    """Fix velocita': read_slot_state + read_text_boxes sullo STESSO
    screenshot non devono rileggere l'immagine con l'OCR due volte."""

    def test_same_path_reuses_the_raw_ocr_result(self):
        r = _ocr_reader_with([_detection("SALDO 114,36", 10, 10)])
        r.read_slot_state("/tmp/shot.png")
        r.read_text_boxes("/tmp/shot.png")
        assert r._ocr.readtext.call_count == 1

    def test_different_path_triggers_a_fresh_ocr_call(self):
        r = _ocr_reader_with([_detection("SALDO 114,36", 10, 10)])
        r.read_slot_state("/tmp/shot_a.png")
        r.read_text_boxes("/tmp/shot_b.png")
        assert r._ocr.readtext.call_count == 2

    def test_cached_result_still_produces_correct_data(self):
        r = _ocr_reader_with([_detection("SALDO 114,36", 10, 10)])
        state = r.read_slot_state("/tmp/shot.png")
        boxes_result = r.read_text_boxes("/tmp/shot.png")
        assert state["saldo"] == 114.36
        assert boxes_result.data[0].text == "SALDO 114,36"


class TestReExport:
    def test_casino_flow_reexports_same_class(self):
        assert ReExportedSlotOCRReader is SlotOCRReader
