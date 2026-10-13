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


class TestCheapVisionCall:
    def test_the_image_is_reduced_and_marked_low_detail(self, tmp_path):
        import base64
        import io
        path = tmp_path / "big.png"
        Image.new("RGB", (1080, 2400), (10, 20, 30)).save(path)
        client = _client({"credit_text": "50 000.00", "bet_text": "0.10"})

        VisionAmountReader(client).read(str(path))

        call = client.client.chat.completions.create.call_args
        image = call.kwargs["messages"][1]["content"][1]["image_url"]
        assert image["detail"] == "low"
        sent = Image.open(io.BytesIO(base64.b64decode(image["url"].split(",", 1)[1])))
        assert max(sent.size) == 960 and sent.size[0] < sent.size[1]      # lato lungo ridotto, proporzioni uguali

    def test_a_small_image_is_never_enlarged(self, tmp_path):
        import base64
        import io
        path = tmp_path / "small.png"
        Image.new("RGB", (300, 500)).save(path)
        client = _client({"credit_text": "1", "bet_text": "1"})

        VisionAmountReader(client).read(str(path))

        url = client.client.chat.completions.create.call_args.kwargs["messages"][1]["content"][1]["image_url"]["url"]
        assert Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size == (300, 500)

    def test_the_prompt_says_the_highlighted_bet_is_the_current_one(self):
        from orchestrator.modules.slot_ocr import vision_reader
        assert "highlighted" in vision_reader._SYSTEM_PROMPT


class TestSpinModeSkipsAUselessOcr:
    def _reader(self, ocr_states, vision_reads):
        ocr = _ocr(ocr_states)
        return ResilientOCRReader(ocr, _vision(*vision_reads)), ocr

    def test_ocr_that_reads_neither_amount_is_skipped_during_spins(self):
        reader, ocr = self._reader(
            [{"saldo": None, "posta": None, "raw_texts": ["x"]}],
            [{"saldo": 50000.0, "posta": 0.3}, {"saldo": 49999.9, "posta": 0.1}],
        )
        reader.read_slot_state("/a.png")                       # primo controllo: OCR + visione
        reader.set_spin_mode(True)

        state = reader.read_slot_state("/b.png")

        assert state["saldo"] == 49999.9 and state["posta"] == 0.1
        assert state["raw_texts"] == [] and state["spin_button_coords"] is None
        assert ocr.read_slot_state.call_count == 1             # nessuna seconda lettura OCR

    def test_outside_spin_mode_the_ocr_is_still_read(self):
        reader, ocr = self._reader(
            [{"saldo": None, "posta": None}, {"saldo": None, "posta": None}],
            [{"saldo": 50000.0, "posta": 0.3}, {"saldo": 49999.9, "posta": 0.1}],
        )
        reader.read_slot_state("/a.png")
        reader.read_slot_state("/b.png")

        assert ocr.read_slot_state.call_count == 2

    def test_an_ocr_that_reads_the_amounts_is_never_skipped(self):
        reader, ocr = self._reader(
            [{"saldo": 100.0, "posta": 1.0}] * 3, [{"saldo": 100.0, "posta": 1.0}],
        )
        reader.read_slot_state("/a.png")
        reader.set_spin_mode(True)
        reader.read_slot_state("/b.png")
        reader.read_slot_state("/c.png")

        assert ocr.read_slot_state.call_count == 3


class TestReaderModel:
    def test_the_reader_uses_its_own_model_not_the_decision_one(self, shot):
        client = _client({"credit_text": "1", "bet_text": "1"})
        client.model = "gpt-6.1-sol"

        VisionAmountReader(client, model="gpt-4o-mini").read(shot)

        assert client.client.chat.completions.create.call_args.kwargs["model"] == "gpt-4o-mini"

    def test_without_a_reader_model_the_client_model_is_used(self, shot):
        client = _client({"credit_text": "1", "bet_text": "1"})
        client.model = "gpt-4o"

        VisionAmountReader(client).read(shot)

        assert client.client.chat.completions.create.call_args.kwargs["model"] == "gpt-4o"

    def test_usage_is_recorded_with_the_reader_model(self, shot):
        client = _client({"credit_text": "1", "bet_text": "1"})

        VisionAmountReader(client, model="gpt-4o-mini").read(shot)

        client.record_usage.assert_called_once()
        assert client.record_usage.call_args.args[2] == "gpt-4o-mini"


# ======================== importi: l'OCR prima, la visione (GPT) come ultima risorsa ===================================

import logging  # noqa: E402

from orchestrator.modules.slot_autopilot.value_parser import parse_amount  # noqa: E402
from orchestrator.modules.slot_ocr.reader import SlotOCRReader  # noqa: E402

AMOUNT_FORMATS = [
    ("99999.8", 99999.8), ("99999,8", 99999.8), ("€ 99999.8", 99999.8), ("Saldo: 99999.8", 99999.8),
    ("99999.8€", 99999.8), ("50.00", 50.0), ("50,00€", 50.0), ("50,00 €", 50.0), ("1.234,56", 1234.56),
]


class TestAmountFormats:
    """Il parser non e' la causa delle chiamate GPT: questi formati si leggono gia'. I test lo fissano."""

    @pytest.mark.parametrize("text, expected", AMOUNT_FORMATS)
    def test_both_parsers_read_the_listed_formats(self, text, expected):
        assert float(parse_amount(text).data) == pytest.approx(expected)        # parse_amount da' un Decimal
        assert SlotOCRReader(ocr_url=None)._extract_amount(text) == pytest.approx(expected)

    @pytest.mark.parametrize("balance_text, bet_text", [
        ("99999.8", "0.10"), ("99999,8", "0,10"), ("€ 99999.8", "€ 0,10"), ("99999.8€", "0.10€"),
    ])
    def test_a_whole_screen_with_labels_gives_saldo_and_posta(self, shot, balance_text, bet_text):
        box = lambda x, y: [[x, y], [x + 150, y], [x + 150, y + 30], [x, y + 30]]    # noqa: E731
        detections = [(box(0, 10), "SALDO", 0.95), (box(200, 10), balance_text, 0.95),
                      (box(0, 100), "PUNTATA", 0.95), (box(200, 100), bet_text, 0.95)]
        reader = SlotOCRReader(ocr_url=None)
        reader._get_raw_detections = lambda path: detections

        state = reader.read_slot_state(shot)

        assert state["saldo"] == pytest.approx(99999.8) and state["posta"] == pytest.approx(0.10)

    @pytest.mark.parametrize("text", ["SALDO: 99999.8", "Saldo 99999,8", "CREDITO € 99999.8"])
    def test_label_and_amount_in_the_same_text(self, shot, text):
        box = [[0, 10], [300, 10], [300, 40], [0, 40]]
        reader = SlotOCRReader(ocr_url=None)
        reader._get_raw_detections = lambda path: [(box, text, 0.95)]
        assert reader.read_slot_state(shot)["saldo"] == pytest.approx(99999.8)


def _ready(ocr_states, vision_reads, capture=None, **kwargs):
    """Lettore con la prima lettura (confronto OCR-visione) gia' fatta e concorde: da qui in poi la visione e' la rete
    di sicurezza."""
    first = {"saldo": 100.0, "posta": 1.0}
    ocr = _ocr([first] + list(ocr_states))
    vision = _vision(first, *vision_reads)
    reader = ResilientOCRReader(ocr, vision, capture=capture, **kwargs)
    reader.read_slot_state("/first.png")
    return reader, ocr, vision


class TestRawOcrIsLogged:
    def test_unreadable_text_is_logged_raw_and_then_the_vision_answers(self, caplog):
        garbled = {"saldo": None, "posta": None, "raw_texts": [("¿?", (10, 10), 0.9), ("###", (50, 10), 0.8)]}
        reader, ocr, vision = _ready([garbled], [{"saldo": 99.0, "posta": 1.0}])

        with caplog.at_level(logging.WARNING):
            state = reader.read_slot_state("/b.png")

        assert "OCR testo grezzo: ['¿?', '###']" in caplog.text
        assert "saldo=None puntata=None: parsing saldo e puntata fallito" in caplog.text
        assert state["saldo"] == 99.0 and vision.read.call_count == 2         # fallback GPT

    def test_the_log_names_only_the_missing_field(self, caplog):
        reader, _, _ = _ready([{"saldo": 99.0, "posta": None, "raw_texts": [("99.0", (1, 1), 0.9)]}],
                              [{"saldo": 99.0, "posta": 1.0}])
        with caplog.at_level(logging.WARNING):
            reader.read_slot_state("/b.png")
        assert "saldo=99.0 puntata=None: parsing puntata fallito" in caplog.text

    def test_the_raw_text_is_logged_the_first_time_and_then_once_every_ten(self, caplog):
        miss = {"saldo": None, "posta": None, "raw_texts": [("?", (1, 1), 0.9)]}
        reader, _, _ = _ready([miss] * 11, [{"saldo": 99.0, "posta": 1.0}] * 11)
        with caplog.at_level(logging.WARNING):
            for i in range(11):
                reader.read_slot_state(f"/{i}.png")
        assert caplog.text.count("OCR testo grezzo") == 2                    # 1a lettura e 11a

    def test_nothing_is_logged_when_the_ocr_reads_both(self, caplog):
        reader, _, _ = _ready([{"saldo": 99.0, "posta": 1.0}], [])
        with caplog.at_level(logging.WARNING):
            reader.read_slot_state("/b.png")
        assert "OCR testo grezzo" not in caplog.text


class TestSecondOcrReadBeforeTheVision:
    def test_a_field_lost_once_is_recovered_by_a_second_ocr_read_without_gpt(self):
        capture = MagicMock(return_value="/new.png")
        reader, ocr, vision = _ready(
            [{"saldo": 99.0, "posta": None}, {"saldo": 98.0, "posta": 1.0}], [], capture=capture)

        state = reader.read_slot_state("/b.png")

        assert state["saldo"] == 98.0 and state["posta"] == 1.0              # la seconda lettura
        assert vision.read.call_count == 1                                   # solo il controllo iniziale: niente GPT
        capture.assert_called_once_with()
        assert [c.args[0] for c in ocr.read_slot_state.call_args_list] == ["/first.png", "/b.png", "/new.png"]

    def test_two_failures_in_a_row_call_the_vision(self):
        capture = MagicMock(return_value="/new.png")
        reader, ocr, vision = _ready(
            [{"saldo": 99.0, "posta": None}, {"saldo": 98.0, "posta": None}], [{"saldo": 99.0, "posta": 1.0}],
            capture=capture)

        state = reader.read_slot_state("/b.png")

        assert vision.read.call_count == 2 and state["posta"] == 1.0
        assert reader.vision_reasons == {"prima_lettura": 1, "campo_mancante": 1}

    def test_without_a_capture_function_the_vision_is_called_at_once(self):
        reader, ocr, vision = _ready([{"saldo": 99.0, "posta": None}], [{"saldo": 99.0, "posta": 1.0}])
        reader.read_slot_state("/b.png")
        assert vision.read.call_count == 2 and ocr.read_slot_state.call_count == 2

    def test_a_capture_that_fails_falls_back_to_the_vision(self):
        capture = MagicMock(side_effect=RuntimeError("adb offline"))
        reader, _, vision = _ready([{"saldo": 99.0, "posta": None}], [{"saldo": 99.0, "posta": 1.0}], capture=capture)
        assert reader.read_slot_state("/b.png")["posta"] == 1.0 and vision.read.call_count == 2

    def test_after_two_useless_retries_it_goes_straight_to_the_vision(self):
        capture = MagicMock(return_value="/new.png")
        miss = {"saldo": 99.0, "posta": None}
        reader, _, vision = _ready([miss, miss] * 3, [{"saldo": 99.0, "posta": 1.0}] * 3, capture=capture)

        for i in range(3):
            reader.read_slot_state(f"/{i}.png")

        assert capture.call_count == 2                                       # la terza lettura non riprova
        assert vision.read.call_count == 1 + 3

    def test_a_complete_read_resets_the_retry_counter(self):
        capture = MagicMock(return_value="/new.png")
        miss, ok = {"saldo": 99.0, "posta": None}, {"saldo": 99.0, "posta": 1.0}
        reader, _, _ = _ready([miss, miss, ok, miss, ok], [{"saldo": 99.0, "posta": 1.0}] * 2, capture=capture)
        for i in range(3):
            reader.read_slot_state(f"/{i}.png")      # fallita+retry fallito, poi OK
        assert reader._retry_failures == 0


class TestVisionFieldsAreNotForever:
    def _mismatched(self, ocr_after, vision_after):
        first_ocr = {"saldo": 50.0, "posta": 1.0}                            # l'OCR legge male il saldo (50 contro 50000)
        reader = ResilientOCRReader(_ocr([first_ocr] + ocr_after), _vision({"saldo": 50000.0, "posta": 1.0},
                                                                          *vision_after))
        reader.read_slot_state("/first.png")
        assert reader.prefer_vision
        return reader

    def test_a_field_returns_to_the_ocr_after_five_agreeing_reads(self, caplog):
        values = [49999.9, 49999.8, 49999.7, 49999.6, 49999.5]
        ocr_after = [{"saldo": v, "posta": 1.0} for v in values]
        vision_after = [{"saldo": v, "posta": 1.0} for v in values]
        reader = self._mismatched(ocr_after + [{"saldo": 49999.4, "posta": 1.0}], vision_after)

        with caplog.at_level(logging.INFO):
            for i, _ in enumerate(values):
                reader.read_slot_state(f"/{i}.png")
        assert not reader.prefer_vision and "torno all'OCR per questo campo" in caplog.text

        vision = reader.vision
        calls = vision.read.call_count
        assert reader.read_slot_state("/last.png")["saldo"] == 49999.4
        assert vision.read.call_count == calls                               # niente piu' GPT per questo campo

    def test_a_disagreement_resets_the_count(self):
        agree = lambda v: ({"saldo": v, "posta": 1.0}, {"saldo": v, "posta": 1.0})    # noqa: E731
        pairs = [agree(49999.9 - i * 0.1) for i in range(4)]
        pairs.append(({"saldo": 12.0, "posta": 1.0}, {"saldo": 49999.4, "posta": 1.0}))   # l'OCR sbaglia ancora
        pairs += [agree(49999.3 - i * 0.1) for i in range(4)]
        reader = self._mismatched([o for o, _ in pairs], [v for _, v in pairs])
        for i in range(len(pairs)):
            reader.read_slot_state(f"/{i}.png")
        assert reader.prefer_vision                                          # 4 + 4 concordanze, mai 5 di fila


class TestUselessOcrIsRechecked:
    GOOD = {"saldo": 100.0, "posta": 1.0}

    def _useless(self, ocr_after):
        ocr = _ocr([{"saldo": None, "posta": None, "raw_texts": []}] + ocr_after)
        vision = MagicMock()
        vision.read.return_value = dict(self.GOOD)
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/first.png")          # la prima lettura cade su una schermata senza importi
        reader.set_spin_mode(True)
        assert reader._ocr_useless_for_amounts
        return reader, ocr, vision

    def test_two_agreeing_checks_bring_the_ocr_back(self, caplog):
        reader, ocr, vision = self._useless([dict(self.GOOD), dict(self.GOOD), dict(self.GOOD)])

        with caplog.at_level(logging.INFO):
            for i in range(20):
                reader.read_slot_state(f"/{i}.png")
        assert not reader._ocr_useless_for_amounts and not reader.prefer_vision
        assert "OCR di nuovo affidabile" in caplog.text
        assert ocr.read_slot_state.call_count == 1 + 2                      # il primo + i due controlli (10a e 20a)

        calls = vision.read.call_count
        assert reader.read_slot_state("/after.png")["saldo"] == 100.0
        assert vision.read.call_count == calls and ocr.read_slot_state.call_count == 4

    def test_an_ocr_that_still_cannot_read_stays_skipped(self):
        blank = {"saldo": None, "posta": None, "raw_texts": []}
        reader, ocr, _ = self._useless([blank, blank])
        for i in range(25):
            reader.read_slot_state(f"/{i}.png")
        assert reader._ocr_useless_for_amounts and ocr.read_slot_state.call_count == 1 + 2   # solo i controlli

    def test_one_failed_check_resets_the_streak(self):
        blank = {"saldo": None, "posta": None, "raw_texts": []}
        reader, _, _ = self._useless([dict(self.GOOD), blank, dict(self.GOOD)])
        for i in range(30):
            reader.read_slot_state(f"/{i}.png")
        assert reader._ocr_useless_for_amounts                               # controlli: ok, no, ok: mai due di fila


class TestWhyTheVisionWasCalled:
    def test_reasons_are_counted_and_summarised_every_twenty_calls(self, caplog):
        reader = ResilientOCRReader(_ocr([]), None)
        with caplog.at_level(logging.WARNING):
            for _ in range(15):
                reader._count_vision("ocr_inutile")
            for _ in range(5):
                reader._count_vision("campo_mancante")
        assert reader.vision_reasons == {"ocr_inutile": 15, "campo_mancante": 5}
        assert "Importi per visione: 20 chiamate su 0 letture (motivi: ocr_inutile 15, campo_mancante 5)" in caplog.text

    def test_reads_and_first_reason_are_tracked(self):
        reader, _, _ = _ready([{"saldo": 99.0, "posta": 1.0}], [])
        reader.read_slot_state("/b.png")
        assert reader.reads == 2 and reader.vision_reads == 1 and reader.vision_reasons == {"prima_lettura": 1}
