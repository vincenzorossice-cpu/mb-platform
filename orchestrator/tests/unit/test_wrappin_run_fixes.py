from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai
import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import precise_hitl
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.spin_controller import SpinController
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import (
    _candidate, _config, _context_result, _tap_outcome,
)


# ---- 1. il punto di spin ricordato che non addebita viene dimenticato --------------------------------------------

def _dead_spin_controller(tmp_path):
    locator = MagicMock()
    locator.locate.return_value = _candidate(strategy_name="memory")
    locator.is_trusted_strategy.return_value = True
    unexpected = _tap_outcome(False)
    unexpected.data.outcome = "unexpected_change"
    verifier = MagicMock()
    verifier.verified_tap.return_value = unexpected
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    controller = SpinController(locator, verifier, step_logger, _config(spin_calm_check=True, slot_memory_dir=str(tmp_path)))
    controller._settle_after_unexpected = lambda *a, **k: None
    return controller, locator


def _spin(controller, index):
    provider = MagicMock(return_value=_context_result(Decimal("10.00")))
    return controller.spin("dev", "wrappin", "ep", index, provider, True, Decimal("0.10"))


def test_a_remembered_spin_point_without_debit_is_forgotten_the_second_time(tmp_path):
    controller, locator = _dead_spin_controller(tmp_path)
    with patch("orchestrator.modules.slot_autopilot.spin_controller.forget_autopilot_element") as forget:
        _spin(controller, 1)
        forget.assert_not_called()
        locator.reject_position.assert_not_called()
        _spin(controller, 2)

    forget.assert_called_once()
    assert forget.call_args.args[:2] == ("wrappin", "spin")
    locator.reject_position.assert_called_once_with("spin", 540, 2000)


def test_a_point_that_is_not_from_memory_is_not_forgotten(tmp_path):
    controller, locator = _dead_spin_controller(tmp_path)
    locator.locate.return_value = _candidate(strategy_name="profile")
    with patch("orchestrator.modules.slot_autopilot.spin_controller.forget_autopilot_element") as forget:
        _spin(controller, 1)
        _spin(controller, 2)
    forget.assert_not_called()


# ---- 2. "tocca fuori dal riquadro" ------------------------------------------------------------------------------

def test_outside_phrases_are_recognised():
    for text in ("devi cliccare in un qualsiasi punto fuori dal menu", "si basta cliccare fuori dal riquadro",
                 "tocca ovunque", "click anywhere"):
        assert precise_hitl._OUTSIDE_TAP.search(text)
    assert not precise_hitl._OUTSIDE_TAP.search("tocca l'icona delle monete in basso")


def test_outside_phrase_uses_a_background_description_then_top_points(monkeypatch):
    described = []

    def describe(text, ctx):
        described.append(text)
        return None

    monkeypatch.setattr(precise_hitl, "ask_human_help", MagicMock(side_effect=[
        ("text", "si basta cliccare fuori dal riquadro")]))
    monkeypatch.setattr(precise_hitl, "get_image_dimensions", lambda p: (1000, 2000))
    reader = MagicMock()
    reader._screenshot.return_value = Result.ok("/tmp/x.png")
    ocr = MagicMock()
    ocr.read_slot_state.return_value = {"posta": 0.2, "saldo": 5.0, "raw_texts": [("a", (0, 0), 0.9)]}
    touch = MagicMock()
    monkeypatch.setattr(precise_hitl, "read_ocr_state", lambda *a, **k: {"posta": 0.2, "saldo": 5.0, "raw_texts": [("a", (0, 0), 0.9), ("b", (1, 1), 0.9)]})

    result = precise_hitl.handle_precise_hitl_escalation(reader, ocr, touch, "dev", 0.2, 0.2, describe, element="bet_menu_close")

    assert all(text == precise_hitl._OUTSIDE_DESCRIPTION for text in described)
    taps = [call.args[1:3] for call in touch.tap_coordinates.call_args_list]
    assert taps == [(500, 300)]                                  # primo punto in alto; lo schermo cambia -> esito "hint"
    assert result[0] == "hint" and result[3] == (500, 300)


# ---- 3. mai riaprire il menu se la puntata e' gia' al minimo verificato ------------------------------------------

def _bet_controller(tmp_path, bet):
    from orchestrator.modules.slot_autopilot.bet_controller import BetResult
    controller = BetController(MagicMock(), MagicMock(), MagicMock(), _config(slot_memory_dir=str(tmp_path)),
                               menu_selector=MagicMock())
    context = SimpleNamespace(profile=None, slot_ocr_state={"posta": bet}, screen_width=1080, screen_height=2400,
                              text_boxes=[], slot_name="wrappin")
    controller._read_bet = lambda ctx: Decimal(str(bet))
    return controller, context, BetResult


def test_second_check_in_the_same_session_does_not_touch_anything(tmp_path):
    controller, context, BetResult = _bet_controller(tmp_path, "0.20")
    controller._set_min_bet = MagicMock(return_value=Result.ok(BetResult(
        slot_name="wrappin", starting_bet=Decimal("1"), final_bet=Decimal("0.20"), min_bet_verified=True, attempts=1)))
    provider = MagicMock(return_value=Result.ok(context))

    first = controller.set_min_bet("dev", "wrappin", "ep", provider)
    controller._set_min_bet = BetController._set_min_bet.__get__(controller)
    second = controller.set_min_bet("dev", "wrappin", "ep", provider)

    assert first.data.min_bet_verified
    assert second.data.outcome == "unchanged_already_min" and second.data.attempts == 0
    controller.locator.locate.assert_not_called()
    controller.verifier.verified_tap.assert_not_called()


# ---- 4. token insufficienti: un secondo tentativo, API non dichiarata irraggiungibile --------------------------

def test_grid_question_retries_with_more_tokens_instead_of_giving_up(tmp_path):
    from orchestrator.core import openai_compat
    openai_compat._REJECTED["gpt-6.1-sol"] = {"max_tokens", "temperature"}   # come in produzione dopo la prima chiamata
    from orchestrator.modules.slot_autopilot.strategies.gpt_locator_strategy import GPTLocatorStrategy
    image = tmp_path / "x.png"
    from PIL import Image
    Image.new("RGB", (50, 50)).save(image)
    client = MagicMock()
    client.model = "gpt-6.1-sol"
    err = openai.BadRequestError("max_tokens or model output limit was reached", response=MagicMock(status_code=400), body=None)
    ok = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"cell": "A1", "confidence": 0.9}'))],
                         usage=None)
    client.client.chat.completions.create.side_effect = [err, ok]
    strategy = GPTLocatorStrategy(client)

    cell = strategy._ask_cell("operator_target", str(image), ["A1", "A2"], "fine")

    assert cell == "A1" and not strategy._api_unavailable
    tokens = [call.kwargs.get("max_tokens") or call.kwargs["max_completion_tokens"] for call in client.client.chat.completions.create.call_args_list]
    assert tokens[1] > tokens[0]
    openai_compat.reset_known_rejections()
