import io
import sys
import types
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import ocr_server
from orchestrator.modules.slot_ocr.reader import SlotOCRReader


def _image_bytes(mode="RGB", size=(20, 10), fmt="PNG"):
    buffer = io.BytesIO()
    Image.new(mode, size).save(buffer, fmt)
    return buffer.getvalue()


def _post(client, content, name="screenshot.png", mime="image/png"):
    return client.post("/ocr", files={"file": (name, content, mime)})


# Quello che EasyOCR restituisce davvero: coordinate numpy.int32 e confidenza numpy.float32
# (i tipi che FastAPI non sa serializzare e mandavano in crash il server con un 500).
NUMPY_RESULTS = [
    ([[np.int32(1), np.int32(2)], [np.int32(30), np.int32(2)],
      [np.int32(30), np.int32(12)], [np.int32(1), np.int32(12)]], "99996.60", np.float32(0.91)),
    ([[np.int32(0), np.int32(0)], [np.int32(5), np.int32(0)],
      [np.int32(5), np.int32(5)], [np.int32(0), np.int32(5)]], "rumore", np.float32(0.2)),
]


class FakeReader:
    def __init__(self, results=None, error=None):
        self.results = NUMPY_RESULTS if results is None else results
        self.error = error
        self.received = []

    def readtext(self, image):
        self.received.append(image)
        if self.error:
            raise self.error
        return self.results


@pytest.fixture
def reader(monkeypatch):
    fake = FakeReader()
    monkeypatch.setattr(ocr_server, "_reader", fake)
    monkeypatch.setattr(ocr_server, "_use_gpu", False)
    return fake


@pytest.fixture
def client(reader):
    return TestClient(ocr_server.app)   # senza `with`: il lifespan (caricamento di EasyOCR) non parte


# ---------------- /ocr ----------------

def test_readtext_receives_a_numpy_array(client, reader):
    assert _post(client, _image_bytes()).status_code == 200
    image = reader.received[0]
    assert isinstance(image, np.ndarray) and image.shape == (10, 20, 3) and image.dtype == np.uint8


def test_numpy_types_are_serialized_as_native_numbers(client):
    response = _post(client, _image_bytes())
    assert response.status_code == 200
    texts = response.json()["texts"]
    assert len(texts) == 1                      # "rumore" (conf 0.2) filtrato come prima
    item = texts[0]
    assert set(item) == {"text", "coords", "conf"}
    assert item["text"] == "99996.60" and item["conf"] == pytest.approx(0.91, abs=1e-6)
    assert item["coords"] == [[1, 2], [30, 2], [30, 12], [1, 12]]
    assert all(isinstance(v, int) for point in item["coords"] for v in point)


@pytest.mark.parametrize("mode, fmt", [("RGBA", "PNG"), ("P", "PNG"), ("L", "PNG"), ("RGB", "JPEG")])
def test_any_image_mode_becomes_rgb(client, reader, mode, fmt):
    assert _post(client, _image_bytes(mode=mode, fmt=fmt)).status_code == 200
    assert reader.received[-1].shape == (10, 20, 3)


def test_invalid_or_empty_image_is_a_400_not_a_500(client, reader):
    bad = _post(client, b"questo non e' un'immagine")
    assert bad.status_code == 400 and bad.json()["detail"] == "Immagine non valida"
    empty = _post(client, b"")
    assert empty.status_code == 400 and empty.json()["detail"] == "Immagine vuota"
    truncated = _post(client, _image_bytes()[:20])
    assert truncated.status_code == 400
    assert reader.received == []                # nessuna chiamata a EasyOCR


def test_ocr_failure_is_a_500_with_a_clear_message(reader, monkeypatch):
    monkeypatch.setattr(ocr_server, "_reader", FakeReader(error=RuntimeError("CUDA out of memory")))
    response = _post(TestClient(ocr_server.app), _image_bytes())
    assert response.status_code == 500 and "RuntimeError" in response.json()["detail"]


# ---------------- /health ----------------

def test_health_reports_the_real_gpu_state(reader, monkeypatch):
    client = TestClient(ocr_server.app)
    monkeypatch.setattr(ocr_server, "_use_gpu", True)
    assert client.get("/health").json() == {"status": "ok", "gpu": True}
    monkeypatch.setattr(ocr_server, "_use_gpu", False)
    assert client.get("/health").json() == {"status": "ok", "gpu": False}


def test_health_before_the_model_is_loaded_detects_cuda(monkeypatch):
    monkeypatch.setattr(ocr_server, "_reader", None)
    monkeypatch.setattr(ocr_server, "cuda_available", lambda: True)
    assert TestClient(ocr_server.app).get("/health").json() == {"status": "ok", "gpu": True}


# ---------------- porta ----------------

@pytest.mark.parametrize("value, expected", [
    (None, 8001), ("", 8001), ("9000", 9000), (" 8123 ", 8123),
    ("abc", 8001), ("0", 8001), ("70000", 8001), ("-5", 8001),
])
def test_port_from_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("OCR_PORT", raising=False)
    else:
        monkeypatch.setenv("OCR_PORT", value)
    assert ocr_server.port_from_env() == expected


# ---------------- caricamento di EasyOCR: GPU con fallback CPU ----------------

class _FakeEasyOCR(types.ModuleType):
    def __init__(self, fail_on_gpu=False, fail_always=False):
        super().__init__("easyocr")
        self.calls = []
        self._fail_on_gpu, self._fail_always = fail_on_gpu, fail_always

    def Reader(self, languages, gpu):
        self.calls.append((tuple(languages), gpu))
        if self._fail_always or (gpu and self._fail_on_gpu):
            raise RuntimeError("init fallita")
        return FakeReader()


@pytest.fixture
def blank_server(monkeypatch):
    monkeypatch.setattr(ocr_server, "_reader", None)
    monkeypatch.setattr(ocr_server, "_use_gpu", False)


def test_load_reader_uses_the_gpu_when_available(blank_server, monkeypatch):
    fake = _FakeEasyOCR()
    monkeypatch.setitem(sys.modules, "easyocr", fake)
    monkeypatch.setattr(ocr_server, "cuda_available", lambda: True)
    ocr_server.load_reader()
    assert fake.calls == [(("it", "en"), True)] and ocr_server._use_gpu is True


def test_load_reader_falls_back_to_cpu_without_cuda(blank_server, monkeypatch):
    fake = _FakeEasyOCR()
    monkeypatch.setitem(sys.modules, "easyocr", fake)
    monkeypatch.setattr(ocr_server, "cuda_available", lambda: False)
    ocr_server.load_reader()
    assert fake.calls == [(("it", "en"), False)] and ocr_server._use_gpu is False


def test_load_reader_falls_back_to_cpu_when_the_gpu_fails(blank_server, monkeypatch):
    fake = _FakeEasyOCR(fail_on_gpu=True)
    monkeypatch.setitem(sys.modules, "easyocr", fake)
    monkeypatch.setattr(ocr_server, "cuda_available", lambda: True)
    ocr_server.load_reader()
    assert fake.calls == [(("it", "en"), True), (("it", "en"), False)]
    assert ocr_server._use_gpu is False and ocr_server._reader is not None


def test_load_reader_cpu_failure_is_not_hidden(blank_server, monkeypatch):
    monkeypatch.setitem(sys.modules, "easyocr", _FakeEasyOCR(fail_always=True))
    monkeypatch.setattr(ocr_server, "cuda_available", lambda: False)
    with pytest.raises(RuntimeError):
        ocr_server.load_reader()


def test_the_model_is_loaded_lazily_on_the_first_request(blank_server, monkeypatch):
    fake = _FakeEasyOCR()
    monkeypatch.setitem(sys.modules, "easyocr", fake)
    monkeypatch.setattr(ocr_server, "cuda_available", lambda: False)
    assert _post(TestClient(ocr_server.app), _image_bytes()).status_code == 200
    assert len(fake.calls) == 1


# ---------------- contratto con il client reale ----------------

def test_the_real_client_parses_the_server_response(client, tmp_path):
    shot = tmp_path / "s.png"
    Image.new("RGB", (200, 100), "white").save(shot)

    def forward(url, files, timeout):
        return client.post("/ocr", files=files)   # il client parla davvero col server (Reader finto)

    ocr = SlotOCRReader(ocr_url="http://ocr.test/ocr")
    with patch("requests.post", side_effect=forward):
        results = ocr._readtext_remote(str(shot))
    assert results == [([[1, 2], [30, 2], [30, 12], [1, 12]], "99996.60", pytest.approx(0.91, abs=1e-6))]
