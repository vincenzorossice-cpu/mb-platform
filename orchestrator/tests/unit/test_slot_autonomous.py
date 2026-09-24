import os
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.tests.validation.test_slot_autonomous import (
    VISION_RESIZE_SCALE,
    descale_coords,
    determine_phase,
    resize_screenshot_for_vision,
    run_autonomous_session,
)


def _ocr_state(saldo=100.0, posta=0.10, spin_button_coords=None):
    return {
        "saldo": saldo,
        "posta": posta,
        "vincita": None,
        "spin_button_coords": spin_button_coords,
        "is_active": False,
        "raw_texts": [],
    }


def _answers(action, target=None, confidence=0.9):
    return {"answers": {
        "action": {"choice": action, "confidence": confidence},
        "target": {"choice": target, "confidence": confidence},
    }}


class TestResizeScreenshotForVision:

    def test_halves_dimensions_and_computes_scale(self, tmp_path):
        """Passa scale=0.5 esplicitamente: il test verifica la matematica della
        funzione, indipendentemente da quale sia il default VISION_RESIZE_SCALE
        attualmente configurato per la sessione reale."""
        from PIL import Image

        path = tmp_path / "shot.png"
        Image.new("RGB", (1080, 2400), color="white").save(path)

        resized_path, w, h, scale_x, scale_y = resize_screenshot_for_vision(str(path), scale=0.5)

        assert (w, h) == (540, 1200)
        assert scale_x == pytest.approx(2.0)
        assert scale_y == pytest.approx(2.0)
        assert os.path.exists(resized_path)
        assert Image.open(resized_path).size == (540, 1200)

        os.remove(resized_path)

    def test_descale_coords_rounds_to_int(self):
        assert descale_coords(270, 600, 2.0, 2.0) == (540, 1200)
        assert descale_coords(270.4, 600.6, 2.0, 2.0) == (541, 1201)


class TestDeterminePhase:

    def test_above_minimum_is_setup(self):
        assert determine_phase(10.0) == "setup"

    def test_at_minimum_is_playing(self):
        assert determine_phase(0.10) == "playing"

    def test_below_minimum_is_playing(self):
        assert determine_phase(0.05) == "playing"

    def test_unknown_bet_is_playing(self):
        assert determine_phase(None) == "playing"


@pytest.fixture
def mocks(tmp_path):
    from PIL import Image

    screenshot_path = str(tmp_path / "shot.png")
    Image.new("RGB", (1080, 2400), color="white").save(screenshot_path)

    with patch("orchestrator.tests.validation.test_slot_autonomous.GPTDecisionClient") as mock_gpt_cls, \
         patch("orchestrator.tests.validation.test_slot_autonomous.ScreenReader") as mock_screen_reader_cls, \
         patch("orchestrator.tests.validation.test_slot_autonomous.TouchInjector") as mock_touch_injector_cls, \
         patch("orchestrator.tests.validation.test_slot_autonomous.SlotOCRReader") as mock_ocr_reader_cls, \
         patch("builtins.input", return_value=""), \
         patch("orchestrator.tests.validation.test_slot_autonomous.time.sleep"):
        screen_reader = MagicMock()
        screen_reader._screenshot.return_value = Result.ok(screenshot_path)
        mock_screen_reader_cls.return_value = screen_reader

        touch_injector = MagicMock()
        touch_injector.tap_coordinates.return_value = Result.ok(True)
        mock_touch_injector_cls.return_value = touch_injector

        ocr_reader = MagicMock()
        mock_ocr_reader_cls.return_value = ocr_reader

        engine = MagicMock()
        mock_gpt_cls.return_value = engine

        yield {
            "screen_reader": screen_reader, "touch_injector": touch_injector,
            "ocr_reader": ocr_reader, "engine": engine, "screenshot_path": screenshot_path,
        }


class TestRunAutonomousSession:

    def test_stops_immediately_when_engine_says_stop(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, spin_button_coords=(270, 1900))
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("stop"))

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_not_called()

    def test_auto_stop_on_insufficient_balance_never_calls_engine(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=0.05)

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["engine"].ask_with_image.assert_not_called()
        mocks["touch_injector"].tap_coordinates.assert_not_called()

    def test_tap_coordinates_are_scaled_back_to_real_screen(self, mocks):
        """Lo screenshot mockato e' 1080x2400: le coordinate [270,600]
        restituite da GPT (nello spazio dell'immagine ridimensionata) vanno
        riscalate in base a VISION_RESIZE_SCALE per tornare allo schermo reale.
        Il fattore atteso e' calcolato dalla costante corrente, cosi' il test
        non dipende da quale valore specifico e' configurato in produzione."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, spin_button_coords=(540, 1200))
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", (270, 600))),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        expected_x = round(270 / VISION_RESIZE_SCALE)
        expected_y = round(600 / VISION_RESIZE_SCALE)
        mocks["touch_injector"].tap_coordinates.assert_called_once_with("dev", expected_x, expected_y)

    def test_ask_with_image_receives_resized_dimensions(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0)
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("stop"))

        run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        call_args = mocks["engine"].ask_with_image.call_args
        # (screenshot_path, state_text, questions, image_width, image_height)
        assert call_args.args[3] == round(1080 * VISION_RESIZE_SCALE)
        assert call_args.args[4] == round(2400 * VISION_RESIZE_SCALE)
        assert call_args.args[0].endswith("_small.png")

    def test_total_played_is_sum_of_bets_not_balance_diff(self, mocks, capsys):
        """3 spin da 0.10 EUR (posta al minimo, fase playing) -> total_played
        deve essere 0.30 EUR, ANCHE SE il saldo nel frattempo sale (vincita):
        non deve mai essere calcolato come saldo_iniziale - saldo_attuale."""
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, posta=0.10, spin_button_coords=(540, 1200)),  # initial
            _ocr_state(saldo=105.0, posta=0.10, spin_button_coords=(540, 1200)),  # refresh dopo 3 step: vincita netta
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", (540, 1200))),
            Result.ok(_answers("tap", (540, 1200))),
            Result.ok(_answers("tap", (540, 1200))),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        out = capsys.readouterr().out
        assert "Totale giocato: 0.30 EUR" in out
        assert "Net profit/loss: +5.00 EUR" in out

    def test_setup_phase_taps_do_not_count_toward_total_played(self, mocks, capsys):
        """Con la posta sopra il minimo (fase 'setup'), un tap serve ad
        aggiustare il menu posta, non a giocare: non deve incrementare
        total_played."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200),
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", (300, 400))),  # tap sul menu posta, non sullo spin
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        out = capsys.readouterr().out
        assert "Totale giocato: 0.00 EUR" in out

    def test_stops_automatically_once_target_reached_without_asking_again(self, mocks):
        """Il controllo su total_played >= target deve avvenire PRIMA di
        interpellare l'engine: appena il target e' raggiunto, l'engine non va
        piu' chiamato (niente overshoot come nel bug reale 35 invece di 30)."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=0.10, spin_button_coords=(540, 1200),
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", (540, 1200))),  # total_played -> 0.10
            Result.ok(_answers("tap", (540, 1200))),  # total_played -> 0.20 == target
        ]

        ok = run_autonomous_session("dev", target_amount=0.20, max_steps=10)

        assert ok is True
        assert mocks["engine"].ask_with_image.call_count == 2

    def test_missing_target_forces_ocr_refresh_without_tapping(self, mocks):
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, spin_button_coords=(540, 1200)),
            _ocr_state(saldo=100.0, spin_button_coords=(540, 1200)),
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", None)),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_not_called()
        assert mocks["ocr_reader"].read_slot_state.call_count == 2

    def test_non_spin_tap_forces_ocr_refresh_next_step(self, mocks):
        """Se il tap non e' vicino al tasto spin noto, l'OCR va riletto al
        prossimo step invece di riusare la cadenza ogni 3 step."""
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, spin_button_coords=(540, 1200)),  # initial
            _ocr_state(saldo=100.0, spin_button_coords=(540, 1200)),  # forced refresh (menu posta lontano dallo spin)
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", (10, 10))),  # lontano dallo spin (540,1200)
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        assert mocks["ocr_reader"].read_slot_state.call_count == 2

    def test_engine_failure_returns_false(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0)
        mocks["engine"].ask_with_image.return_value = Result.fail("boom", "GPT_DECISION_TIMEOUT")

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is False

    def test_screenshot_failure_returns_false(self, mocks):
        mocks["screen_reader"]._screenshot.return_value = Result.fail("adb error", "ADB_TIMEOUT")

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is False
        mocks["engine"].ask_with_image.assert_not_called()

    def test_save_screenshots_writes_files_when_enabled(self, mocks, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, spin_button_coords=(540, 1200))
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("stop"))

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, save_screenshots=True)

        assert ok is True
        saved = tmp_path / "screenshots" / "step_0001.png"
        assert saved.exists()

    def test_no_screenshots_saved_by_default(self, mocks, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, spin_button_coords=(540, 1200))
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("stop"))

        run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert not (tmp_path / "screenshots").exists()
