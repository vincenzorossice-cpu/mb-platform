import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import openai
import pytest
from PIL import Image

from orchestrator.modules.slot_ocr.vision_reader import ResilientOCRReader, VisionAmountReader


@pytest.fixture
def shot(tmp_path):
    path = tmp_path / "s.png"
    Image.new("RGB", (100, 200)).save(path)
    return str(path)


def _client(payload=None, error=None):
    client = MagicMock()
    if error is not None:
        client.client.chat.completions.create.side_effect = error
    else:
        content = payload if isinstance(payload, str) else json.dumps(payload)
        client.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )
    return client


class TestVisionAmountReader:
    def test_reads_credit_and_bet(self, shot):
        reader = VisionAmountReader(_client({"credit": 50000.0, "bet": 1000}))

        assert reader.read(shot) == {"saldo": 50000.0, "posta": 1000.0}

    def test_strings_with_separators_are_normalised(self, shot):
        reader = VisionAmountReader(_client({"credit": "€50.000,00", "bet": "1.000"}))

        assert reader.read(shot) == {"saldo": 50000.0, "posta": 1000.0}

    @pytest.mark.parametrize("payload", [
        {"credit": None, "bet": None}, {"credit": -5, "bet": True}, {"credit": "boh", "bet": []}, ["x"], "not json",
    ])
    def test_unreadable_answers_give_none(self, shot, payload):
        assert VisionAmountReader(_client(payload)).read(shot) == {"saldo": None, "posta": None}

    def test_openai_errors_never_raise(self, shot):
        error = openai.APIConnectionError(request=MagicMock())

        assert VisionAmountReader(_client(error=error)).read(shot) == {"saldo": None, "posta": None}

    def test_missing_screenshot_never_raises(self):
        assert VisionAmountReader(_client({"credit": 1})).read("/nonexistent.png") == {"saldo": None, "posta": None}

    def test_the_same_screenshot_is_read_once(self, shot):
        client = _client({"credit": 10, "bet": 1})
        reader = VisionAmountReader(client)
        reader.read(shot)
        reader.read(shot)

        assert client.client.chat.completions.create.call_count == 1


def _ocr(states):
    ocr = MagicMock()
    ocr.region = (0, 247, 1080, 2400)
    ocr.read_slot_state.side_effect = list(states)
    return ocr


def _vision(*reads):
    vision = MagicMock()
    vision.read.side_effect = [dict(read) for read in reads]
    return vision


class TestResilientOCRReader:
    def test_when_ocr_and_vision_agree_the_vision_is_not_called_again(self):
        vision = _vision({"saldo": 50000.0, "posta": 1000.0})
        reader = ResilientOCRReader(_ocr([{"saldo": 50000.0, "posta": 1000.0}] * 3), vision)

        for _ in range(3):
            assert reader.read_slot_state("/a.png")["saldo"] == 50000.0

        assert vision.read.call_count == 1                 # solo il controllo iniziale
        assert not reader.prefer_vision

    def test_a_misread_balance_switches_to_the_vision_for_the_whole_session(self, caplog):
        vision = _vision({"saldo": 50000.0, "posta": 1000.0}, {"saldo": 49000.0, "posta": 1000.0})
        ocr = _ocr([{"saldo": 50.0, "posta": 1000.0, "raw_texts": ["x"]}, {"saldo": 49.0, "posta": 1000.0}])
        reader = ResilientOCRReader(ocr, vision)

        with caplog.at_level("WARNING"):
            first = reader.read_slot_state("/a.png")
            second = reader.read_slot_state("/b.png")

        assert first["saldo"] == 50000.0 and first["raw_texts"] == ["x"]     # il resto resta dell'OCR
        assert second["saldo"] == 49000.0 and reader.prefer_vision
        assert any("uso la visione" in r.getMessage() for r in caplog.records)

    def test_an_unreadable_bet_is_filled_by_the_vision(self):
        vision = _vision({"saldo": 50000.0, "posta": 1000.0})
        reader = ResilientOCRReader(_ocr([{"saldo": 50000.0, "posta": None}]), vision)

        state = reader.read_slot_state("/a.png")

        assert state["posta"] == 1000.0 and reader.prefer_vision

    def test_tolerance_absorbs_small_differences(self):
        vision = _vision({"saldo": 100.0, "posta": 1.0})
        reader = ResilientOCRReader(_ocr([{"saldo": 105.0, "posta": 1.0}]), vision)

        assert reader.read_slot_state("/a.png")["saldo"] == 105.0 and not reader.prefer_vision

    def test_a_vision_that_reads_nothing_changes_nothing(self):
        vision = _vision({"saldo": None, "posta": None})
        reader = ResilientOCRReader(_ocr([{"saldo": 12.0, "posta": None}]), vision)

        assert reader.read_slot_state("/a.png") == {"saldo": 12.0, "posta": None}
        assert not reader.prefer_vision

    def test_without_a_vision_it_is_just_the_ocr(self):
        ocr = _ocr([{"saldo": 50.0, "posta": None}])
        assert ResilientOCRReader(ocr, None).read_slot_state("/a.png") == {"saldo": 50.0, "posta": None}

    def test_later_missing_fields_are_filled_when_the_reader_trusts_the_ocr(self):
        vision = _vision({"saldo": 100.0, "posta": 1.0}, {"saldo": 100.0, "posta": 1.0})
        ocr = _ocr([{"saldo": 100.0, "posta": 1.0}, {"saldo": 100.0, "posta": None}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/a.png")

        assert reader.read_slot_state("/b.png")["posta"] == 1.0
        assert not reader.prefer_vision

    def test_region_text_boxes_and_other_attributes_are_delegated(self):
        ocr = _ocr([])
        ocr.read_text_boxes.return_value = "boxes"
        ocr._SALDO_LABELS = ("SALDO",)
        reader = ResilientOCRReader(ocr, None)

        assert reader.region == (0, 247, 1080, 2400) and reader.read_text_boxes("/a.png") == "boxes"
        reader.set_region((1, 2, 3, 4))
        ocr.set_region.assert_called_once_with((1, 2, 3, 4))
        assert reader._SALDO_LABELS == ("SALDO",)
