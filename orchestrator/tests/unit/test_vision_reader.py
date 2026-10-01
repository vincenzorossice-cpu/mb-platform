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


class TestRateLimit:
    def _limit(self):
        import httpx
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
        return openai.RateLimitError("Please try again in 100ms.", response=response, body=None)

    def test_a_temporary_limit_is_waited_out(self, shot, monkeypatch):
        monkeypatch.setattr("orchestrator.core.openai_retry.time.sleep", lambda seconds: None)
        ok = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"credit": 50000, "bet": 0.3})))])
        client = MagicMock()
        client.client.chat.completions.create.side_effect = [self._limit(), ok]

        assert VisionAmountReader(client).read(shot) == {"saldo": 50000.0, "posta": 0.3}

    def test_a_persistent_limit_reads_nothing_without_raising(self, shot, monkeypatch):
        monkeypatch.setattr("orchestrator.core.openai_retry.time.sleep", lambda seconds: None)
        client = MagicMock()
        client.client.chat.completions.create.side_effect = self._limit()

        assert VisionAmountReader(client).read(shot) == {"saldo": None, "posta": None}


class TestVisionUnavailable:
    def test_amount_strings_with_spaced_thousands_are_read(self, shot):
        client = _client({"credit": "49 999.90", "bet": "0.10"})
        assert VisionAmountReader(client).read(shot) == {"saldo": 49999.9, "posta": 0.1}

    def test_an_ocr_value_that_contradicts_the_last_trusted_one_is_discarded(self, caplog):
        vision = _vision({"saldo": 50000.0, "posta": 0.3}, {"saldo": None, "posta": None})
        ocr = _ocr([{"saldo": 50.0, "posta": None}, {"saldo": 49.99, "posta": None}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/a.png")                        # prima lettura: OCR 50 contro visione 50000

        with caplog.at_level("WARNING"):
            state = reader.read_slot_state("/b.png")            # la visione non risponde

        assert state["saldo"] is None and state["posta"] is None
        assert any("visione non disponibile" in r.getMessage() for r in caplog.records)

    def test_an_ocr_value_that_agrees_with_the_last_trusted_one_is_kept(self):
        vision = _vision({"saldo": 50000.0, "posta": 0.3}, {"saldo": None, "posta": None})
        ocr = _ocr([{"saldo": 50.0, "posta": None}, {"saldo": 49999.9, "posta": 0.3}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/a.png")

        state = reader.read_slot_state("/b.png")

        assert state["saldo"] == 49999.9 and state["posta"] == 0.3

    def test_a_field_the_vision_says_nothing_about_stays_with_the_ocr(self):
        vision = _vision({"saldo": None, "posta": 0.3})
        reader = ResilientOCRReader(_ocr([{"saldo": 50.0, "posta": None}]), vision)

        state = reader.read_slot_state("/a.png")

        assert reader.prefer_vision and state["saldo"] == 50.0 and state["posta"] == 0.3


class TestVisionTranscription:
    def test_the_copied_text_is_interpreted_by_us(self, shot):
        client = _client({"credit_text": "49 999.90", "bet_text": "0.10"})
        assert VisionAmountReader(client).read(shot) == {"saldo": 49999.9, "posta": 0.1}

    def test_non_breaking_spaces_and_currency_text_are_fine(self, shot):
        client = _client({"credit_text": "49\u00a0999,90", "bet_text": "\u20ac0,10"})
        assert VisionAmountReader(client).read(shot) == {"saldo": 49999.9, "posta": 0.1}

    def test_the_old_numeric_keys_are_still_accepted(self, shot):
        client = _client({"credit": 50000, "bet": 1})
        assert VisionAmountReader(client).read(shot) == {"saldo": 50000.0, "posta": 1.0}

    def test_text_that_is_not_a_number_gives_none(self, shot):
        client = _client({"credit_text": "n/a", "bet_text": None})
        assert VisionAmountReader(client).read(shot) == {"saldo": None, "posta": None}

    def test_the_prompt_asks_for_a_literal_copy(self):
        from orchestrator.modules.slot_ocr import vision_reader
        assert "COPY" in vision_reader._SYSTEM_PROMPT and "credit_text" in vision_reader._SYSTEM_PROMPT


class TestPerFieldArbitration:
    def test_the_ocr_balance_survives_when_only_the_bet_is_unreadable(self):
        vision = _vision({"saldo": 49.99, "posta": 0.1}, {"saldo": 49.99, "posta": 0.1})
        ocr = _ocr([{"saldo": 49999.9, "posta": None}, {"saldo": 49999.8, "posta": None}])
        reader = ResilientOCRReader(ocr, vision)

        first = reader.read_slot_state("/a.png")
        second = reader.read_slot_state("/b.png")

        assert first["saldo"] == 49999.9 and first["posta"] == 0.1          # saldo all'OCR, puntata alla visione
        assert second["saldo"] == 49999.8 and second["posta"] == 0.1
        assert reader._vision_fields == {"posta"}

    def test_ocr_dropping_the_thousands_loses_to_the_vision(self):
        reader = ResilientOCRReader(_ocr([{"saldo": 50.0, "posta": 1.0}]), _vision({"saldo": 50000.0, "posta": 1.0}))
        assert reader.read_slot_state("/a.png")["saldo"] == 50000.0

    def test_a_disagreement_that_is_not_a_power_of_ten_goes_to_the_vision(self):
        reader = ResilientOCRReader(_ocr([{"saldo": 120.0, "posta": 1.0}]), _vision({"saldo": 100.0, "posta": 1.0}))
        assert reader.read_slot_state("/a.png")["saldo"] == 100.0

    def test_a_vision_misread_is_discarded_in_favour_of_a_coherent_ocr(self):
        vision = _vision({"saldo": 50000.0, "posta": None}, {"saldo": 49.99, "posta": None})
        ocr = _ocr([{"saldo": 50.0, "posta": 1.0}, {"saldo": 49999.9, "posta": 1.0}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/a.png")                       # OCR 50 contro visione 50000: visione

        state = reader.read_slot_state("/b.png")               # visione 49.99, OCR 49999.9

        assert state["saldo"] == 49999.9

    def test_neither_reading_coherent_with_the_history_gives_unknown(self):
        vision = _vision({"saldo": 50000.0, "posta": None}, {"saldo": 49.99, "posta": None})
        ocr = _ocr([{"saldo": 50.0, "posta": 1.0}, {"saldo": 4.9, "posta": 1.0}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/a.png")

        assert reader.read_slot_state("/b.png")["saldo"] is None

    def test_both_readers_agreeing_on_a_big_jump_are_believed(self):
        vision = _vision({"saldo": 50000.0, "posta": None}, {"saldo": 20000.0, "posta": None})
        ocr = _ocr([{"saldo": 50.0, "posta": 1.0}, {"saldo": 20000.0, "posta": 1.0}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/a.png")

        assert reader.read_slot_state("/b.png")["saldo"] == 20000.0

    def test_the_bet_is_not_subject_to_the_continuity_check(self):
        vision = _vision({"saldo": 100.0, "posta": 0.3}, {"saldo": 100.0, "posta": 0.1})
        ocr = _ocr([{"saldo": 100.0, "posta": None}, {"saldo": 100.0, "posta": None}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/a.png")

        assert reader.read_slot_state("/b.png")["posta"] == 0.1

    def test_an_ocr_only_balance_that_jumps_is_checked_with_the_vision(self):
        vision = _vision({"saldo": 50000.0, "posta": 1.0}, {"saldo": 49999.0, "posta": 1.0})
        ocr = _ocr([{"saldo": 50000.0, "posta": 1.0}, {"saldo": 49.9, "posta": 1.0}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/a.png")

        assert reader.read_slot_state("/b.png")["saldo"] == 49999.0
        assert vision.read.call_count == 2
