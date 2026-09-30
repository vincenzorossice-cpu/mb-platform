from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import precise_hitl
from orchestrator.modules.slot_autopilot.precise_hitl import handle_precise_hitl_escalation


@pytest.fixture
def shot(tmp_path):
    path = tmp_path / "s.png"
    Image.new("RGB", (1080, 2400)).save(path)
    return str(path)


def _run(monkeypatch, shot, inputs, states, describe, current_bet=2.0):
    """inputs: risposte di ask_human_help in ordine; states: read_ocr_state dopo ogni tap."""
    monkeypatch.setattr(precise_hitl, "ask_human_help", MagicMock(side_effect=inputs))
    monkeypatch.setattr(precise_hitl, "read_ocr_state", MagicMock(side_effect=states))
    monkeypatch.setattr(precise_hitl.time, "sleep", lambda s: None)
    screen_reader = MagicMock()
    screen_reader._screenshot.return_value = Result.ok(shot)
    ocr_reader = MagicMock()
    ocr_reader.read_slot_state.return_value = {"saldo": 100.0, "posta": current_bet, "raw_texts": []}
    touch = MagicMock()
    result = handle_precise_hitl_escalation(
        screen_reader, ocr_reader, touch, "dev", current_bet, None, describe,
    )
    return result, touch


def _taps(touch):
    return [call.args[1:3] for call in touch.tap_coordinates.call_args_list]


class TestDescriptionIsUnderstood:
    def test_phrase_is_located_with_the_describer_not_the_3x3_cell_center(self, monkeypatch, shot):
        describe = MagicMock(return_value=(544, 2090))
        result, touch = _run(
            monkeypatch, shot, [("text", "l'icona delle monete impilate in basso al centro")],
            [{"posta": 0.5, "saldo": 100.0, "raw_texts": []}], describe,
        )

        assert result[0] == "resolved" and result[3] == (544, 2090)
        assert _taps(touch) == [(544, 2090)]        # non (540, 2000), il centro della cella 3x3
        assert describe.call_args.args[0] == "l'icona delle monete impilate in basso al centro"
        assert describe.call_args.args[1].screenshot_path == shot

    def test_describer_without_result_falls_back_to_keyword_parser(self, monkeypatch, shot):
        result, touch = _run(
            monkeypatch, shot, [("text", "in basso al centro")],
            [{"posta": 0.5, "saldo": 100.0, "raw_texts": []}], MagicMock(return_value=None),
        )

        assert _taps(touch) == [(540, 2000)]
        assert result[0] == "resolved"

    def test_describer_crash_falls_back_to_keyword_parser(self, monkeypatch, shot):
        result, touch = _run(
            monkeypatch, shot, [("text", "in basso al centro")],
            [{"posta": 0.5, "saldo": 100.0, "raw_texts": []}], MagicMock(side_effect=RuntimeError("boom")),
        )

        assert _taps(touch) == [(540, 2000)]

    def test_unintelligible_phrase_asks_again_without_tapping(self, monkeypatch, shot):
        result, touch = _run(
            monkeypatch, shot, [("text", "boh"), ("stop",)], [], MagicMock(return_value=None),
        )

        assert result[0] == "stop"
        touch.tap_coordinates.assert_not_called()


class TestExplicitCoordinatesAndExits:
    def test_explicit_coordinates_are_tapped_exactly(self, monkeypatch, shot):
        describe = MagicMock()
        result, touch = _run(
            monkeypatch, shot, [("coords", 350, 1765)],
            [{"posta": 1.0, "saldo": 100.0, "raw_texts": []}], describe,
        )

        assert result[0] == "resolved" and _taps(touch) == [(350, 1765)]
        describe.assert_not_called()

    def test_coordinates_without_effect_ask_again(self, monkeypatch, shot):
        result, touch = _run(
            monkeypatch, shot, [("coords", 10, 10), ("skip",)],
            [{"posta": 2.0, "saldo": 100.0, "raw_texts": [("nuovo", (1, 1), 0.9)]}], MagicMock(),
        )

        assert result[0] == "skip"

    def test_stop_and_skip(self, monkeypatch, shot):
        assert _run(monkeypatch, shot, [("stop",)], [], MagicMock())[0][0] == "stop"
        assert _run(monkeypatch, shot, [("skip",)], [], MagicMock())[0][0] == "skip"


class TestOutcomeClassification:
    def test_new_texts_after_the_tap_are_a_hint(self, monkeypatch, shot):
        result, _ = _run(
            monkeypatch, shot, [("text", "monete")],
            [{"posta": 2.0, "saldo": 100.0, "raw_texts": [("0,10", (1, 1), 0.9)]}],
            MagicMock(return_value=(544, 2090)),
        )

        assert result[0] == "hint" and result[3] == (544, 2090)

    def test_balance_drop_with_spin_intent_is_spin_confirmed(self, monkeypatch, shot):
        result, _ = _run(
            monkeypatch, shot, [("text", "premi il tasto spin")],
            [{"posta": 2.0, "saldo": 98.0, "raw_texts": []}], MagicMock(return_value=(540, 1765)),
        )

        assert result[0] == "spin_confirmed"

    def test_balance_drop_without_spin_intent_asks_again(self, monkeypatch, shot):
        result, _ = _run(
            monkeypatch, shot, [("text", "il tasto meno"), ("skip",)],
            [{"posta": 2.0, "saldo": 98.0, "raw_texts": []}], MagicMock(return_value=(350, 1765)),
        )

        assert result[0] == "skip"

    def test_nothing_changes_asks_again(self, monkeypatch, shot):
        result, touch = _run(
            monkeypatch, shot, [("text", "monete"), ("stop",)],
            [{"posta": 2.0, "saldo": 100.0, "raw_texts": []}], MagicMock(return_value=(544, 2090)),
        )

        assert result[0] == "stop" and len(_taps(touch)) == 1

    def test_min_bet_confirmation_phrase_is_reported(self, monkeypatch, shot):
        result, _ = _run(
            monkeypatch, shot, [("text", "abbiamo gia la puntata minima corretta"), ("stop",)],
            [], MagicMock(return_value=None),
        )

        assert result[2] == 2.0
