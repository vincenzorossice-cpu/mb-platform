import pytest


@pytest.fixture(autouse=True)
def _metrics_file_in_tmp(tmp_path, monkeypatch):
    """I test non scrivono mai nel file di metriche vero (logs/model_metrics.jsonl)."""
    monkeypatch.setenv("MODEL_METRICS_FILE", str(tmp_path / "model_metrics.jsonl"))


@pytest.fixture(autouse=True)
def _no_remote_ocr_from_the_environment(monkeypatch):
    """Un OCR_URL impostato sul PC di chi lancia i test non deve cambiarne il comportamento."""
    monkeypatch.delenv("OCR_URL", raising=False)
    monkeypatch.delenv("OCR_TIMEOUT", raising=False)


@pytest.fixture(autouse=True)
def _no_capture_mode_from_the_environment(monkeypatch):
    """SCREENSHOT_MODE / ADB_CAPTURE_MODE impostati sul PC di chi lancia i test non devono cambiare il comando di
    cattura che i test si aspettano (png)."""
    monkeypatch.delenv("ADB_CAPTURE_MODE", raising=False)
    monkeypatch.delenv("SCREENSHOT_MODE", raising=False)
