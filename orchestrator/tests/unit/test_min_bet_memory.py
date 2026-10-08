from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_value, save_autopilot_value
from orchestrator.modules.slot_autopilot.bet_controller import BetController, BetResult
from orchestrator.modules.slot_autopilot.bet_menu import MenuSelection
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig


def _config(tmp_path, **kw):
    return AutoPilotConfig(device_id="d", slot_name="wrap", target_played_amount=Decimal("1"),
                           slot_memory_dir=str(tmp_path), **kw)


def _controller(tmp_path, **kw):
    return BetController(MagicMock(), MagicMock(), MagicMock(), _config(tmp_path, **kw), menu_selector=MagicMock())


def _value(tmp_path, key):
    return load_autopilot_value("wrap", key, str(tmp_path))


def test_the_remembered_minimum_never_rises(tmp_path):
    controller = _controller(tmp_path)
    controller._remember_min("wrap", Decimal("0.20"))
    controller._remember_min("wrap", Decimal("1.4"))

    assert Decimal(_value(tmp_path, "min_bet")) == Decimal("0.20")


def test_a_menu_or_plateau_value_is_only_a_hint_not_a_proof(tmp_path):
    controller = _controller(tmp_path)
    controller._remember_min("wrap", Decimal("1.4"))

    assert controller._remembered_min("wrap") == Decimal("1.4")
    assert controller._trusted_min("wrap") is None            # un suggerimento sporco non salta la verifica
    controller._remember_min("wrap", Decimal("0.20"), trusted=True)
    assert controller._trusted_min("wrap") == Decimal("0.20")
    assert controller._known_min(SimpleNamespace(slot_name="wrap", profile=None)) == Decimal("0.20")   # e' quello che arriva a Sol


def test_the_declared_minimum_repairs_a_dirty_memory(tmp_path):
    save_autopilot_value("wrap", "min_bet", "1.4", str(tmp_path))
    controller = _controller(tmp_path, known_min_bet=Decimal("0.20"))

    controller._apply_declared_min("wrap")

    assert Decimal(_value(tmp_path, "min_bet")) == Decimal("0.20")
    assert controller._trusted_min("wrap") == Decimal("0.20")


def test_a_bet_above_the_declared_minimum_is_not_verified(tmp_path):
    controller = _controller(tmp_path, known_min_bet=Decimal("0.20"))
    controller._set_min_bet = MagicMock(return_value=Result.ok(BetResult(
        slot_name="wrap", starting_bet=Decimal("1.4"), final_bet=Decimal("1.4"), min_bet_verified=True, attempts=0)))

    result = controller.set_min_bet("d", "wrap", "ep", MagicMock())

    assert not result.data.min_bet_verified and result.data.aborted_reason == "ABOVE_KNOWN_MIN"
    assert controller._session_min.get("wrap") is None            # e non entra nel minimo di sessione


def test_a_menu_value_above_what_the_slot_already_reached_is_not_accepted(tmp_path):
    controller = _controller(tmp_path)
    controller._remember_min("wrap", Decimal("0.20"))
    controller.menu_selector.select_lowest.return_value = Result.ok(
        MenuSelection(final_bet=Decimal("1.4"), lowest_listed=Decimal("1.4"), toggle_strategy="memory"))

    outcome, menu_open = controller._select_via_menu("d", "wrap", "ep", MagicMock(), MagicMock(), Decimal("1.4"), [], 1)

    assert outcome is None and menu_open is False                 # si prosegue fino all'operatore


def test_a_menu_value_equal_to_the_dirty_hint_is_verified_by_the_new_scroll_search(tmp_path):
    controller = _controller(tmp_path)
    controller._remember_min("wrap", Decimal("1.4"))              # suggerimento sporco
    controller.menu_selector.select_lowest.return_value = Result.ok(
        MenuSelection(final_bet=Decimal("0.20"), lowest_listed=Decimal("0.20"), toggle_strategy="memory", proven=True))

    outcome, _ = controller._select_via_menu("d", "wrap", "ep", MagicMock(), MagicMock(), Decimal("1.4"), [], 1)

    assert outcome.data.min_bet_verified and outcome.data.final_bet == Decimal("0.20")
    assert controller._trusted_min("wrap") == Decimal("0.20")      # trovata scorrendo su entrambi i lati


def test_the_bad_hint_does_not_skip_the_check_any_more(tmp_path):
    controller = _controller(tmp_path)
    controller._remember_min("wrap", Decimal("1.4"))
    context = SimpleNamespace(profile=None, slot_ocr_state={"posta": 1.4}, screen_width=1080, screen_height=2400,
                              text_boxes=[], slot_name="wrap")
    controller.menu_selector.select_lowest.return_value = Result.fail("menu", "MENU_TOGGLE_NO_EFFECT")
    controller.locator.locate.return_value = Result.fail("no", "ELEMENT_NOT_LOCATABLE")
    controller._locate_before_operator = MagicMock(return_value=Result.fail("stop", "HITL_STOP_REQUESTED"))

    result = controller._set_min_bet("d", "wrap", "ep", MagicMock(return_value=Result.ok(context)))

    # Non e' tornato "gia' al minimo": ha provato davvero (menu) e poi l'operatore
    assert controller._known_min(context) is None
    assert result.data.min_bet_verified is False
