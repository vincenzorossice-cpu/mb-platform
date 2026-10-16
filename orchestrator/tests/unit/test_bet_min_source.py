"""Il giocato da raggiungere (--target) non c'entra mai con la puntata minima; un "minimo" piu' alto di una puntata
che si legge davvero e' sbagliato e va ignorato, non preteso."""
import logging
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_value, save_autopilot_value
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.tests.unit.test_menu_close_outside import _cells, _ctx, _screen, _selector
from orchestrator.tests.unit.test_slot_autopilot_bet_controller import (
    _config, _context_provider, _controller, _locator_mock, _ok_candidate, _tap_outcome,
)
from orchestrator.tests.unit.test_slot_autopilot_bet_menu import _provider


def _select(tmp_path, values, target, read_after, rounds=1):
    menu, closed = _screen(tmp_path, "menu.png"), _screen(tmp_path, "closed.png", panel=None)
    selector, _, verifier = _selector(tmp_path)
    one_round = [_ctx(menu, _cells(values), 1.40), _ctx(menu, _cells(values), read_after),
                 _ctx(closed, [], read_after), _ctx(closed, [], read_after)]
    provider = _provider(*(one_round * rounds))
    return selector.select_lowest("d", "Wrap", _ctx(closed, [], 1.40), provider, target=target)


# ---- il menu: atteso = minimo noto o piu' bassa del menu, mai il giocato -----------------------------------------------

def test_target_4_declared_min_010_reads_010_is_ok_and_logs_the_expectation(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        result = _select(tmp_path, ("0,10", "0,20", "0,30", "0,40"), Decimal("0.10"), 0.10)

    assert result.success and result.data.final_bet == Decimal("0.10") and result.data.contradicted_min is None
    assert "Puntata letta: 0.10, attesa: 0.10 (minimo noto (dichiarato o ricordato)), OK" in caplog.text
    assert "expected 4" not in caplog.text


def test_unknown_min_accepts_the_lowest_of_the_menu(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        result = _select(tmp_path, ("0,10", "0,20", "0,30", "0,40"), None, 0.10)

    assert result.success and result.data.final_bet == Decimal("0.10")
    assert "attesa: 0.10 (la piu' bassa del menu), OK" in caplog.text


def test_target_30_min_020_reads_020_is_ok(tmp_path):
    result = _select(tmp_path, ("0,20", "0,40", "0,60", "1,00"), Decimal("0.20"), 0.20)

    assert result.success and result.data.final_bet == Decimal("0.20")


def test_a_wrong_high_min_is_overruled_by_the_lower_bet_that_is_actually_shown(tmp_path, caplog):
    """Il caso del log: atteso 4, letto 0.1. Nessuna puntata sta sotto il minimo: e' il minimo creduto a essere sbagliato."""
    with caplog.at_level(logging.WARNING):
        result = _select(tmp_path, ("0,10", "0,20", "4,00", "5,00"), Decimal("4"), 0.10)

    assert result.success and result.data.final_bet == Decimal("0.10")
    assert result.data.contradicted_min == Decimal("4") and result.data.proven is False
    assert "il minimo creduto era sbagliato" in caplog.text


def test_a_bet_higher_than_expected_is_still_a_failed_selection(tmp_path):
    result = _select(tmp_path, ("0,20", "0,40", "0,60", "1,00"), Decimal("0.20"), 0.40, rounds=3)

    assert result.error_code == "MENU_SELECTION_NOT_CONFIRMED" and "expected 0.20" in result.error
    assert "minimo noto" in result.error                           # l'errore dice da dove viene l'atteso


def test_a_lower_bet_above_one_euro_is_not_accepted(tmp_path):
    result = _select(tmp_path, ("2,00", "3,00", "4,00", "5,00"), Decimal("5"), 2.00, rounds=3)

    assert result.error_code == "MENU_SELECTION_NOT_CONFIRMED"


# ---- BetController: il minimo fidato smentito da una puntata piu' bassa si ignora ---------------------------------------

def _contradicted(tmp_path, declared="4"):
    locator = _locator_mock()
    locator.locate.return_value = _ok_candidate()
    verifier = MagicMock()
    verifier.verified_tap.return_value = Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "no_effect"))
    config = _config(target_played_amount=Decimal("4"), known_min_bet=Decimal(declared),
                     slot_memory_dir=str(tmp_path / "mem"), stable_reads_for_min=2)
    controller, _ = _controller(locator, verifier, config=config)
    return controller, verifier


def test_the_target_never_reaches_the_expected_minimum(tmp_path):
    """--target 4 e minimo dichiarato 0.10: la puntata di partenza 0.10 e' gia' il minimo, senza toccare nulla."""
    locator = _locator_mock()
    verifier = MagicMock()
    config = _config(target_played_amount=Decimal("4"), known_min_bet=Decimal("0.10"),
                     slot_memory_dir=str(tmp_path / "mem"))
    controller, _ = _controller(locator, verifier, config=config)

    result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("0.10")))

    assert result.data.min_bet_verified and result.data.final_bet == Decimal("0.10") and result.data.attempts == 0
    assert controller._trusted_min("pirots5") == Decimal("0.10")
    verifier.verified_tap.assert_not_called()


def test_target_30_and_min_020_start_020_is_already_the_minimum(tmp_path):
    config = _config(target_played_amount=Decimal("30"), known_min_bet=Decimal("0.20"),
                     slot_memory_dir=str(tmp_path / "mem"))
    controller, _ = _controller(_locator_mock(), MagicMock(), config=config)

    result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("0.20")))

    assert result.data.min_bet_verified and result.data.final_bet == Decimal("0.20")


def test_a_declared_min_above_the_bet_on_screen_is_ignored_and_the_memory_is_repaired(tmp_path, caplog):
    controller, verifier = _contradicted(tmp_path)

    with caplog.at_level(logging.INFO):
        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("0.10")))

    assert "(da --min-bet)" in caplog.text and "non puo' essere il minimo" in caplog.text
    assert "hai passato --min-bet col valore sbagliato" in caplog.text
    assert controller._trusted_min("pirots5") is None                      # ignorato per la sessione
    assert load_autopilot_value("pirots5", "min_bet_trusted", str(tmp_path / "mem")) in ("", None)
    assert Decimal(load_autopilot_value("pirots5", "min_bet", str(tmp_path / "mem"))) == Decimal("0.10")
    assert verifier.verified_tap.called                                    # niente scorciatoia "gia' al minimo"
    assert not (result.success and result.data.attempts == 0 and result.data.outcome == "unchanged_already_min")


def test_a_remembered_wrong_min_is_repaired_the_same_way(tmp_path, caplog):
    controller, _ = _contradicted(tmp_path, declared="0.10")
    memory = str(tmp_path / "mem")
    controller.config.known_min_bet = None
    save_autopilot_value("pirots5", "min_bet_trusted", "4", memory)

    with caplog.at_level(logging.INFO):
        controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("0.10")))

    assert "(dalla memoria della slot)" in caplog.text and controller._trusted_min("pirots5") is None
    assert load_autopilot_value("pirots5", "min_bet_trusted", memory) in ("", None)


def test_the_menu_route_repairs_the_memory_when_the_min_was_contradicted(tmp_path):
    from orchestrator.modules.slot_autopilot.bet_menu import MenuSelection
    controller, _ = _contradicted(tmp_path)
    memory = str(tmp_path / "mem")
    save_autopilot_value("pirots5", "min_bet_trusted", "4", memory)

    controller._menu_result("dev", "pirots5", "ep", Decimal("5"), MenuSelection(
        final_bet=Decimal("0.10"), lowest_listed=Decimal("4"), toggle_strategy="memory", contradicted_min=Decimal("4")),
        ["menu"], 1)

    assert controller._trusted_min("pirots5") is None
    assert load_autopilot_value("pirots5", "min_bet_trusted", memory) in ("", None)
