from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.tests.validation.test_slot_autonomous import (
    resolve_tap_coords,
    run_autonomous_session,
)


def _raw_texts(*entries):
    """entries: (text, x, y, conf)."""
    return [(text, (x, y), conf) for text, x, y, conf in entries]


def _ocr_state(saldo=100.0, raw_texts=None, spin_button_coords=None):
    return {
        "saldo": saldo,
        "posta": None,
        "vincita": None,
        "spin_button_coords": spin_button_coords,
        "is_active": False,
        "raw_texts": raw_texts or [],
    }


def _answers(action, target=None, confidence=0.9):
    return {"answers": {
        "action": {"choice": action, "confidence": confidence},
        "target": {"choice": target, "confidence": confidence},
    }}


class TestResolveTapCoords:

    def test_exact_match(self):
        raw_texts = _raw_texts(("PUNTATA € 1.00", 729, 2225, 0.9), ("Xiter", 6, 1794, 0.9))
        assert resolve_tap_coords("Xiter", raw_texts) == (6, 1794)

    def test_case_insensitive_match(self):
        raw_texts = _raw_texts(("Xiter", 6, 1794, 0.9),)
        assert resolve_tap_coords("XITER", raw_texts) == (6, 1794)

    def test_fuzzy_match(self):
        raw_texts = _raw_texts(("PUNTATA € 1.00", 729, 2225, 0.9),)
        assert resolve_tap_coords("PUNTATA  1.00", raw_texts) == (729, 2225)

    def test_no_match_returns_none(self):
        raw_texts = _raw_texts(("PUNTATA € 1.00", 729, 2225, 0.9),)
        assert resolve_tap_coords("totalmente un altro testo", raw_texts) is None

    def test_none_target_returns_none(self):
        raw_texts = _raw_texts(("PUNTATA € 1.00", 729, 2225, 0.9),)
        assert resolve_tap_coords(None, raw_texts) is None


@pytest.fixture
def mocks():
    with patch("orchestrator.tests.validation.test_slot_autonomous.build_engine") as mock_build_engine, \
         patch("orchestrator.tests.validation.test_slot_autonomous.ScreenReader") as mock_screen_reader_cls, \
         patch("orchestrator.tests.validation.test_slot_autonomous.TouchInjector") as mock_touch_injector_cls, \
         patch("orchestrator.tests.validation.test_slot_autonomous.SlotOCRReader") as mock_ocr_reader_cls, \
         patch("builtins.input", return_value=""), \
         patch("orchestrator.tests.validation.test_slot_autonomous.time.sleep"):
        screen_reader = MagicMock()
        screen_reader._screenshot.return_value = Result.ok("/tmp/fake.png")
        mock_screen_reader_cls.return_value = screen_reader

        touch_injector = MagicMock()
        touch_injector.tap_coordinates.return_value = Result.ok(True)
        mock_touch_injector_cls.return_value = touch_injector

        ocr_reader = MagicMock()
        mock_ocr_reader_cls.return_value = ocr_reader

        engine = MagicMock()
        mock_build_engine.return_value = engine

        yield {"screen_reader": screen_reader, "touch_injector": touch_injector, "ocr_reader": ocr_reader, "engine": engine}


class TestRunAutonomousSession:

    def test_stops_immediately_when_engine_says_stop(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, raw_texts=_raw_texts(("Xiter", 6, 1794, 0.9)), spin_button_coords=(56, 1814),
        )
        mocks["engine"].ask.return_value = Result.ok(_answers("stop"))

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, engine_url=None, provider="gpt4o")

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_not_called()

    def test_auto_stop_on_insufficient_balance_never_calls_engine(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=0.05, raw_texts=[])

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, engine_url=None, provider="gpt4o")

        assert ok is True
        mocks["engine"].ask.assert_not_called()
        mocks["touch_injector"].tap_coordinates.assert_not_called()

    def test_taps_resolved_coordinates_and_tracks_total_played(self, mocks):
        raw_texts = _raw_texts(("Xiter", 6, 1794, 0.9))
        # step1: tap Xiter (=spin) -> no forced refresh; step2: OCR still cached
        # (steps_since_ocr < 3) so saldo/candidates come from the same read;
        # engine says stop on the 2nd call.
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, raw_texts=raw_texts, spin_button_coords=(6, 1794),
        )
        mocks["engine"].ask.side_effect = [
            Result.ok(_answers("tap", "Xiter")),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, engine_url=None, provider="gpt4o")

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_called_once_with("dev", 6, 1794)

    def test_total_played_reflects_balance_drop_on_refresh(self, mocks, capsys):
        raw_texts = _raw_texts(("Xiter", 6, 1794, 0.9))
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, raw_texts=raw_texts, spin_button_coords=(6, 1794)),  # initial read
            _ocr_state(saldo=97.0, raw_texts=raw_texts, spin_button_coords=(6, 1794)),   # refresh after 3 steps
        ]
        # 3 taps (steps 1-3 reuse the first OCR read), then stop on step 4
        # right after the refresh triggered by steps_since_ocr hitting the limit.
        mocks["engine"].ask.side_effect = [
            Result.ok(_answers("tap", "Xiter")),
            Result.ok(_answers("tap", "Xiter")),
            Result.ok(_answers("tap", "Xiter")),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, engine_url=None, provider="gpt4o")

        assert ok is True
        out = capsys.readouterr().out
        assert "Totale giocato: 3.00 EUR" in out

    def test_total_played_can_be_negative_on_net_win(self, mocks, capsys):
        raw_texts = _raw_texts(("Xiter", 6, 1794, 0.9))
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, raw_texts=raw_texts, spin_button_coords=(6, 1794)),
            _ocr_state(saldo=105.0, raw_texts=raw_texts, spin_button_coords=(6, 1794)),
        ]
        mocks["engine"].ask.side_effect = [
            Result.ok(_answers("tap", "Xiter")),
            Result.ok(_answers("tap", "Xiter")),
            Result.ok(_answers("tap", "Xiter")),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, engine_url=None, provider="gpt4o")

        assert ok is True
        out = capsys.readouterr().out
        assert "Totale giocato: -5.00 EUR" in out

    def test_unresolvable_target_forces_ocr_refresh_without_tapping(self, mocks):
        raw_texts = _raw_texts(("Xiter", 6, 1794, 0.9))
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, raw_texts=raw_texts, spin_button_coords=(6, 1794)),
            _ocr_state(saldo=100.0, raw_texts=raw_texts, spin_button_coords=(6, 1794)),
        ]
        mocks["engine"].ask.side_effect = [
            Result.ok(_answers("tap", "elemento inesistente")),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, engine_url=None, provider="gpt4o")

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_not_called()
        assert mocks["ocr_reader"].read_slot_state.call_count == 2

    def test_engine_failure_returns_false(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, raw_texts=_raw_texts(("Xiter", 6, 1794, 0.9)),
        )
        mocks["engine"].ask.return_value = Result.fail("boom", "GPT_DECISION_TIMEOUT")

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, engine_url=None, provider="gpt4o")

        assert ok is False

    def test_screenshot_failure_returns_false(self, mocks):
        mocks["screen_reader"]._screenshot.return_value = Result.fail("adb error", "ADB_TIMEOUT")

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, engine_url=None, provider="gpt4o")

        assert ok is False
        mocks["engine"].ask.assert_not_called()
