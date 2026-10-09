import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.precise_hitl import _READY_STATEMENT


def _config(tmp_path, **kw):
    return AutoPilotConfig(device_id="d", slot_name="wrap", target_played_amount=Decimal("1"),
                           slot_memory_dir=str(tmp_path), **kw)


def _context(bet, profile_min=None):
    profile = SimpleNamespace(verified=False, known_min_bet=profile_min, elements={}) if profile_min else None
    return SimpleNamespace(slot_name="wrap", device_id="d", profile=profile, slot_ocr_state={"posta": bet},
                           text_boxes=[], screen_width=1080, screen_height=2400)


def _controller(tmp_path, locate_codes, **kw):
    locator = MagicMock()
    locator.locate.side_effect = [Result.fail("x", code) for code in locate_codes] * 5
    locator.is_chain_exhausted.return_value = False
    controller = BetController(locator, MagicMock(), MagicMock(), _config(tmp_path, **kw))
    return controller, locator


def _provider(*bets, profile_min=None):
    contexts = [Result.ok(_context(bet, profile_min)) for bet in bets]
    return MagicMock(side_effect=contexts + [contexts[-1]] * 20)


def test_a_proven_minimum_skips_everything(tmp_path):
    controller, locator = _controller(tmp_path, [], known_min_bet=Decimal("0.20"))

    result = controller.set_min_bet("d", "wrap", "ep", _provider(0.20))

    assert result.data.min_bet_verified and result.data.attempts == 0
    locator.locate.assert_not_called()


def test_a_bet_above_the_minimum_goes_on_lowering(tmp_path):
    controller, locator = _controller(tmp_path, ["ELEMENT_NOT_LOCATABLE"], known_min_bet=Decimal("0.20"))
    locator.is_chain_exhausted.return_value = True

    result = controller.set_min_bet("d", "wrap", "ep", _provider(0.40))

    assert locator.locate.called and not result.data.min_bet_verified


def test_ready_statement_rereads_the_fresh_bet_and_resumes(tmp_path):
    controller, locator = _controller(tmp_path, ["HITL_READY_STATEMENT"])

    # il primo contesto e' vecchio (1.00), la rilettura dopo "puoi spinnare" dice 0.20
    result = controller.set_min_bet("d", "wrap", "ep", _provider(1.00, 0.20, profile_min=0.20))

    assert result.data.min_bet_verified and result.data.final_bet == Decimal("0.2")
    controller.verifier.verified_tap.assert_not_called()


def test_same_ask_twice_with_bet_at_minimum_forces_skip(tmp_path, caplog):
    controller, locator = _controller(tmp_path, ["HITL_SKIPPED"])

    with caplog.at_level(logging.WARNING):
        result = controller.set_min_bet("d", "wrap", "ep", _provider(0.20, profile_min=0.20))

    assert result.data.min_bet_verified and locator.locate.call_count == 2
    assert "forzo skip" in caplog.text
    assert controller._trusted_min("wrap") is None          # non diventa un minimo "provato"


def test_one_skip_is_not_enough_and_a_bet_above_the_hint_never_forces(tmp_path):
    controller, locator = _controller(tmp_path, ["HITL_SKIPPED", "ELEMENT_NOT_LOCATABLE"])
    locator.is_chain_exhausted.side_effect = [False, True]

    result = controller.set_min_bet("d", "wrap", "ep", _provider(1.40, profile_min=0.20))

    assert not result.data.min_bet_verified


def test_ready_phrases_are_states_not_taps():
    for phrase in ("è già chiuso puoi spinnare", "gia' chiuso", "puoi spinnare", "vai"):
        assert _READY_STATEMENT.search(phrase), phrase
    assert not _READY_STATEMENT.search("premi il meno in basso a sinistra")
