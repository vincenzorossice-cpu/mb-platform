from unittest.mock import MagicMock, patch

import pytest
import requests
from PIL import Image

from orchestrator.modules.slot_ocr.reader import SlotOCRReader

POST = "requests.post"


@pytest.fixture
def shot(tmp_path):
    path = tmp_path / "s.png"
    Image.new("RGB", (1080, 2400), "white").save(path)
    return str(path)


def _response(texts, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"texts": texts}
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status} error")
    return response


def _item(text, x, y, conf=0.9, w=150, h=30):
    return {"text": text, "coords": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], "conf": conf}


class TestLocalIsUnchangedWithoutUrl:
    def test_no_url_uses_easyocr_and_never_posts(self, shot):
        reader = SlotOCRReader()
        reader._ocr = MagicMock()
        reader._ocr.readtext.return_value = [([[10, 10], [160, 10], [160, 40], [10, 40]], "SALDO 114,36", 0.9)]

        with patch(POST) as post:
            state = reader.read_slot_state(shot)

        assert not reader.remote and state["saldo"] == 114.36
        post.assert_not_called()
        reader._ocr.readtext.assert_called_once()


class TestRemote:
    def test_with_a_url_the_screenshot_is_posted_and_easyocr_is_never_loaded(self, shot):
        reader = SlotOCRReader(ocr_url="https://pod.proxy.runpod.net", ocr_timeout=7)

        with patch(POST, return_value=_response([_item("SALDO 114,36", 10, 10)])) as post, \
                patch.object(SlotOCRReader, "_get_ocr") as local:
            state = reader.read_slot_state(shot)

        local.assert_not_called()
        post.assert_called_once()
        assert post.call_args.args[0] == "https://pod.proxy.runpod.net/ocr"
        assert post.call_args.kwargs["timeout"] == 7
        name, payload, mime = post.call_args.kwargs["files"]["file"]
        assert name == "screenshot.png" and mime == "image/png" and payload.startswith(b"\x89PNG")
        assert reader.remote and state["saldo"] == 114.36

    def test_the_url_can_come_from_the_environment_and_gets_the_ocr_path(self, monkeypatch):
        monkeypatch.setenv("OCR_URL", "https://pod.example/ocr/")
        assert SlotOCRReader()._ocr_url == "https://pod.example/ocr"
        monkeypatch.setenv("OCR_URL", "https://pod.example")
        assert SlotOCRReader()._ocr_url == "https://pod.example/ocr"

    def test_the_timeout_comes_from_the_environment_or_the_default(self, monkeypatch):
        monkeypatch.setenv("OCR_TIMEOUT", "12")
        assert SlotOCRReader(ocr_url="https://x")._ocr_timeout == 12.0
        monkeypatch.setenv("OCR_TIMEOUT", "boh")
        assert SlotOCRReader(ocr_url="https://x")._ocr_timeout == 20.0

    def test_the_remote_answer_gives_the_same_state_as_an_equivalent_easyocr_answer(self, shot):
        items = [_item("SALDO 114,36", 10, 10), _item("POSTA 0,20", 10, 100), _item("VINCITA 5,00", 10, 200)]
        local = SlotOCRReader()
        local._ocr = MagicMock()
        local._ocr.readtext.return_value = [(it["coords"], it["text"], it["conf"]) for it in items]
        remote = SlotOCRReader(ocr_url="https://x")

        with patch(POST, return_value=_response(items)):
            remote_state = remote.read_slot_state(shot)

        assert remote_state == local.read_slot_state(shot)

    def test_split_thousands_are_still_merged(self, shot):
        reader = SlotOCRReader(ocr_url="https://x")
        items = [_item("CREDITO", 10, 10, w=120), _item("50", 140, 10, w=40), _item("000.00", 190, 10, w=120)]

        with patch(POST, return_value=_response(items)):
            state = reader.read_slot_state(shot)

        assert state["saldo"] == 50000.0

    def test_with_a_region_only_the_crop_is_sent_and_coordinates_are_absolute(self, shot):
        reader = SlotOCRReader(ocr_url="https://x")
        reader.set_region((100, 2000, 1000, 2300))

        with patch(POST, return_value=_response([_item("SALDO 5,00", 20, 30)])) as post:
            state = reader.read_slot_state(shot)

        import io
        sent = Image.open(io.BytesIO(post.call_args.kwargs["files"]["file"][1]))
        assert sent.size == (900, 300)
        assert state["raw_texts"][0][1] == (120, 2030)          # 20 + 100, 30 + 2000

    def test_the_same_screenshot_is_posted_once(self, shot):
        reader = SlotOCRReader(ocr_url="https://x")

        with patch(POST, return_value=_response([_item("SALDO 5,00", 10, 10)])) as post:
            reader.read_slot_state(shot)
            reader.read_text_boxes(shot)

        assert post.call_count == 1

    @pytest.mark.parametrize("failure", [
        requests.Timeout("lento"), requests.ConnectionError("giu'"),
    ])
    def test_network_errors_never_raise_and_are_not_cached(self, shot, caplog, failure):
        reader = SlotOCRReader(ocr_url="https://x")
        good = _response([_item("SALDO 5,00", 10, 10)])

        with caplog.at_level("WARNING"), patch(POST, side_effect=[failure, good]) as post:
            first = reader.read_slot_state(shot)
            second = reader.read_slot_state(shot)

        assert first["saldo"] is None and first["raw_texts"] == []
        assert second["saldo"] == 5.0 and post.call_count == 2           # la lettura dopo riprova
        assert "OCR remoto non disponibile" in caplog.text

    def test_http_error_bad_json_and_missing_texts_give_an_empty_state(self, shot):
        reader = SlotOCRReader(ocr_url="https://x")
        bad_json = MagicMock()
        bad_json.json.side_effect = ValueError("non json")
        no_texts = MagicMock()
        no_texts.json.return_value = {"altro": 1}

        for response in (_response([], status=500), bad_json, no_texts):
            with patch(POST, return_value=response):
                assert reader.read_slot_state(shot)["raw_texts"] == []

    def test_malformed_items_are_skipped_and_good_ones_kept(self, shot):
        reader = SlotOCRReader(ocr_url="https://x")
        texts = [{"text": "senza coordinate"}, {"coords": [[1, 2]], "text": "ok?"}, "stringa",
                 {"text": "x", "coords": []}, _item("SALDO 9,00", 10, 10)]

        with patch(POST, return_value=_response(texts)):
            state = reader.read_slot_state(shot)

        assert state["saldo"] == 9.0

    def test_a_missing_screenshot_never_raises(self):
        reader = SlotOCRReader(ocr_url="https://x")

        with patch(POST) as post:
            state = reader.read_slot_state("/nonexistent.png")

        assert state["saldo"] is None
        post.assert_not_called()
