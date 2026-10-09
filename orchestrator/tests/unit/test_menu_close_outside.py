import dataclasses
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import save_autopilot_flag
from orchestrator.modules.slot_autopilot import session
from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector
from orchestrator.modules.slot_autopilot.menu_geometry import is_inside, outside_point, panel_bounds
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_ocr.reader import OCRBox
from orchestrator.tests.unit.test_slot_autopilot_bet_menu import _provider, _tap
from orchestrator.tests.unit.test_slot_autopilot_spin_controller import _config

SIZE = (1080, 2400)


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.bet_menu.time.sleep", lambda s: None)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)


def _screen(tmp_path, name, panel=(1580, 2000), browser_bar=True):
    """Telefono finto come nello screenshot: barra del browser in alto, gioco sfocato, pannello nero, barra saldo."""
    path = str(tmp_path / name)
    image = Image.new("RGB", SIZE, (160, 90, 200))
    draw = ImageDraw.Draw(image)
    if browser_bar:
        draw.rectangle([0, 0, 1080, 300], fill=(240, 240, 240))
    if panel:
        draw.rectangle([100, panel[0], 980, panel[1]], fill=(15, 15, 15))
    draw.rectangle([0, 2030, 1080, 2200], fill=(25, 25, 25))
    draw.rectangle([0, 2230, 1080, 2400], fill=(10, 10, 10))
    image.save(path)
    return path


def _cells(values=("0,20", "0,40", "0,60", "1,00"), y=1860):
    return [OCRBox(text=v, bbox=(x - 40, y - 25, x + 40, y + 25), confidence=0.9)
            for v, x in zip(values, (300, 500, 700, 900))]


def _ctx(path, boxes=None, posta=None):
    state = {"posta": posta} if posta is not None else {}
    return LocateContext(device_id="d", slot_name="Wrap", screenshot_path=path, screen_width=SIZE[0],
                         screen_height=SIZE[1], text_boxes=boxes or [], slot_ocr_state=state)


class TestGeometry:
    def test_bounds_found_with_and_without_the_anchor(self, tmp_path):
        path = _screen(tmp_path, "a.png")
        assert panel_bounds(path, 1860) == (1580, 2000) and panel_bounds(path) == (1580, 2000)

    def test_outside_point_is_above_the_panel_and_below_the_browser_bar(self, tmp_path):
        x, y = outside_point((1580, 2000), SIZE)
        assert x == 540 and int(SIZE[1] * 0.20) <= y < 1580 and not is_inside((x, y), (1580, 2000))

    def test_a_panel_near_the_top_gets_a_tap_below_it(self):
        x, y = outside_point((600, 1000), SIZE)
        assert y > 1000 and not is_inside((x, y), (600, 1000))

    def test_the_coin_icon_position_is_inside_the_open_panel(self):
        assert is_inside((518, 1975), (1580, 2000)) and not is_inside((518, 1100), (1580, 2000))

    def test_no_panel_means_a_safe_fallback_height(self, tmp_path):
        assert panel_bounds(_screen(tmp_path, "n.png", panel=None)) is None
        assert outside_point(None, SIZE)[1] == int(SIZE[1] * 0.30)


def _selector(tmp_path, memory_point=None, toggle_xy=(518, 1975)):
    locator = MagicMock()
    locator.is_trusted_strategy.return_value = True
    locator.memory_dir = str(tmp_path)
    toggle = LocateCandidate(x=toggle_xy[0], y=toggle_xy[1], confidence=1, strategy_name="memory")

    def locate(name, ctx, exclude_strategies=()):
        if name == "bet_menu_close":
            if memory_point and "memory" not in exclude_strategies:
                return Result.ok(LocateCandidate(x=memory_point[0], y=memory_point[1], confidence=1, strategy_name="memory"))
            return Result.fail("no", "ELEMENT_NOT_LOCATABLE")
        return Result.ok(toggle)
    locator.locate.side_effect = locate
    verifier = MagicMock()
    verifier.verified_tap.return_value = _tap(True)
    return BetMenuSelector(locator, verifier), locator, verifier


def _taps(verifier):
    return [call.args[1:3] for call in verifier.verified_tap.call_args_list]


class TestClosingNeverTouchesACell:
    def test_closing_taps_outside_the_panel_and_the_bet_stays(self, tmp_path):
        menu, closed = _screen(tmp_path, "menu.png"), _screen(tmp_path, "closed.png", panel=None)
        selector, _, verifier = _selector(tmp_path)
        provider = _provider(_ctx(menu, _cells(), 1.40), _ctx(menu, _cells(), 0.20), _ctx(closed, [], 0.20))

        result = selector.select_lowest("d", "Wrap", _ctx(closed, [], 1.40), provider, target=Decimal("0.20"))

        assert result.success and result.data.final_bet == Decimal("0.20")
        taps = _taps(verifier)
        assert taps[0] == (518, 1975) and taps[1] == (300, 1860)                  # icona, poi la cella 0.20
        assert not is_inside(taps[2], (1580, 2000)) and taps[2][1] >= int(SIZE[1] * 0.20)   # chiusura: fuori e sotto la barra
        assert not any(is_inside(tap, (1580, 2000)) for tap in taps[2:])

    def test_the_coin_icon_is_not_tapped_to_close_without_a_verified_toggle(self, tmp_path):
        menu = _screen(tmp_path, "menu.png")
        selector, _, verifier = _selector(tmp_path)
        provider = _provider(_ctx(menu, _cells(), 1.40), _ctx(menu, _cells(), 0.20), _ctx(menu, _cells(), 0.20),
                             _ctx(menu, _cells(), 0.20))

        selector.select_lowest("d", "Wrap", _ctx(menu, [], 1.40), provider, allow_operator=False, target=Decimal("0.20"))

        assert _taps(verifier).count((518, 1975)) == 1                  # solo per APRIRE il menu

    def test_a_remembered_close_point_inside_the_panel_is_discarded_and_forgotten(self, tmp_path):
        menu, closed = _screen(tmp_path, "menu.png"), _screen(tmp_path, "closed.png", panel=None)
        selector, _, verifier = _selector(tmp_path, memory_point=(507, 1900))     # sporco: cade sulla cella 0.40
        provider = _provider(_ctx(menu, _cells(), 1.40), _ctx(menu, _cells(), 0.20), _ctx(closed, [], 0.20))

        with patch("orchestrator.modules.slot_autopilot.bet_menu.forget_autopilot_element") as forget:
            result = selector.select_lowest("d", "Wrap", _ctx(closed, [], 1.40), provider, target=Decimal("0.20"))

        assert result.success and (507, 1900) not in _taps(verifier)
        forget.assert_called_once()

    def test_the_toggle_is_used_to_close_only_when_the_map_verified_it_and_it_is_outside_the_panel(self, tmp_path):
        menu, closed = _screen(tmp_path, "menu.png"), _screen(tmp_path, "closed.png", panel=None)
        save_autopilot_flag("Wrap", "bet_menu_close_via_toggle", True, str(tmp_path))
        selector, _, verifier = _selector(tmp_path, toggle_xy=(100, 700))
        provider = _provider(_ctx(menu, _cells(), 1.40), _ctx(menu, _cells(), 0.20), _ctx(menu, _cells(), 0.20),
                             _ctx(closed, [], 0.20))

        selector.select_lowest("d", "Wrap", _ctx(closed, [], 1.40), provider, target=Decimal("0.20"))

        assert _taps(verifier).count((100, 700)) == 2                    # aprire + (dopo il tocco fuori che non chiude) chiudere

    def test_if_the_closing_tap_changes_the_bet_it_reopens_and_reselects(self, tmp_path):
        menu, closed = _screen(tmp_path, "menu.png"), _screen(tmp_path, "closed.png", panel=None)
        selector, _, verifier = _selector(tmp_path)
        provider = _provider(
            _ctx(menu, _cells(), 1.40), _ctx(menu, _cells(), 0.20), _ctx(closed, [], 0.40),      # 1o: chiusura ha cambiato a 0.40
            _ctx(closed, [], 0.40),                                                              # _close_if_left_open: gia' chiuso
            _ctx(menu, _cells(), 0.40), _ctx(menu, _cells(), 0.20), _ctx(closed, [], 0.20),      # 2o tentativo ok
        )

        result = selector.select_lowest("d", "Wrap", _ctx(closed, [], 1.40), provider, target=Decimal("0.20"))

        assert result.success and result.data.final_bet == Decimal("0.20")
        assert _taps(verifier).count((518, 1975)) == 2                  # il menu e' stato riaperto una volta

    def test_three_failed_attempts_end_with_the_not_confirmed_error_for_the_operator(self, tmp_path):
        menu, closed = _screen(tmp_path, "menu.png"), _screen(tmp_path, "closed.png", panel=None)
        selector, _, verifier = _selector(tmp_path)
        one_round = [_ctx(menu, _cells(), 1.40), _ctx(menu, _cells(), 0.20), _ctx(closed, [], 0.40), _ctx(closed, [], 0.40)]

        result = selector.select_lowest("d", "Wrap", _ctx(closed, [], 1.40), _provider(*(one_round * 3)),
                                        target=Decimal("0.20"))

        assert result.error_code == "MENU_SELECTION_NOT_CONFIRMED"


# ---- rete di sicurezza sul saldo ---------------------------------------------------------------------------------

class TestUnderstatedBetThreshold:
    def _controller(self):
        return SpinController(MagicMock(), MagicMock(), MagicMock(), _config())

    def test_a_drop_of_double_the_believed_bet_is_now_caught(self):
        assert self._controller()._bet_understated(Decimal("100"), Decimal("99.60"), Decimal("0.20"))

    def test_a_normal_loss_and_partial_wins_are_not_flagged(self):
        controller = self._controller()
        assert not controller._bet_understated(Decimal("100"), Decimal("99.80"), Decimal("0.20"))
        assert not controller._bet_understated(Decimal("100"), Decimal("99.95"), Decimal("0.20"))


def _spin(i, understated=False, before="100", after="99.8", bet="0.20"):
    return Result.ok(SpinResult(slot_name="s", spin_index=i, bet_amount=Decimal(bet), balance_before=Decimal(before),
                                balance_after=Decimal(after), win_amount=None, verified=True, attempts=1,
                                outcome="success", bet_understated=understated))


def _autopilot(spins):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(BetResult(
        slot_name="s", starting_bet=Decimal("0.4"), final_bet=Decimal("0.2"), min_bet_verified=True, attempts=1))
    autopilot.get_balance_summary.return_value = (Decimal("100"), Decimal("99"))
    autopilot.spin.side_effect = list(spins)
    autopilot.hitl_strategy = None
    autopilot.slot_setup = None
    return autopilot


class TestSessionRecoversFromAWrongBet:
    def test_the_first_understated_spin_rereads_the_bet_and_the_session_goes_on(self):
        autopilot = _autopilot([_spin(1), _spin(2, understated=True, after="99.60"), _spin(3), _spin(4)])

        result = session.play_session(autopilot, "d", "slot", Decimal("0.80"), 30)

        assert not result.failed
        assert autopilot.set_min_bet.call_count == 2                     # iniziale + riverifica
        # spin 1 = 0.20, spin 2 = il calo reale 0.40, spin 3 = 0.20: target 0.80 raggiunto a 3 spin
        assert result.total_played == Decimal("0.80") and result.spins == 3

    def test_after_two_rechecks_the_third_wrong_bet_stops(self):
        autopilot = _autopilot([_spin(i, understated=True, after="99.60") for i in range(1, 6)])

        result = session.play_session(autopilot, "d", "slot", Decimal("50"), 30)

        assert result.stop_reason == "BET_UNDERSTATED" and autopilot.set_min_bet.call_count == 3

    def test_a_recheck_that_cannot_verify_the_bet_stops(self):
        autopilot = _autopilot([_spin(1, understated=True, after="99.60")])
        autopilot.set_min_bet.side_effect = [
            autopilot.set_min_bet.return_value,
            Result.ok(BetResult(slot_name="s", starting_bet=Decimal("0.4"), final_bet=None, min_bet_verified=False, attempts=1)),
        ]

        result = session.play_session(autopilot, "d", "slot", Decimal("50"), 30)

        assert result.stop_reason == "BET_UNDERSTATED"
