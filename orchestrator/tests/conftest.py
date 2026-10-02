import pytest


@pytest.fixture(autouse=True)
def _metrics_file_in_tmp(tmp_path, monkeypatch):
    """I test non scrivono mai nel file di metriche vero (logs/model_metrics.jsonl)."""
    monkeypatch.setenv("MODEL_METRICS_FILE", str(tmp_path / "model_metrics.jsonl"))
