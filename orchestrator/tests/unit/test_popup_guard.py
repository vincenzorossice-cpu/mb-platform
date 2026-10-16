import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.popup_guard import PopupGuard
from orchestrator.modules.slot_autopilot.slot_warden import SlotWarden

W, H = 400, 800
DOUBLE = (100, 600)
COLLECT = (300, 600)


def _box(text, xy, w=120, h=40):
    return SimpleNamespace(text=text, bbox=(xy[0] - w // 2, xy[1] - h // 2, xy[0] + w // 2, xy[1] + h // 2), confidence=0.9)


class PopupWorld:
    """kind: "it" (Raddoppia/Raccogli), "en" (Gamble/Collect), "en2" (Double up/Take win), "icons" (nessun testo),
    "win" (CONGRATULAZIONI), "none", "trap" (un solo testo con 'Raccogli e raddoppia'), "scommetti" (solo Scommetti)."""

    TEXTS = {"it": ("RADDOPPIA", "RACCOGLI"), "en": ("GAMBLE", "COLLECT"), "en2": ("DOUBLE UP", "TAKE WIN")}

    def __init__(self, tmp_path, kind="it", sticky=False, bet_after_close="0.10"):
        self.tmp, self.kind, self.sticky = tmp_path, kind, sticky
        self.open = kind not in ("none", "scommetti")
        self.taps, self.shots, self.states = [], 0, {}
        self.bet, self.bet_after_close = Decimal("0.10"), Decimal(bet_after_close)
        self.screen_reader = SimpleNamespace(_screenshot=self._screenshot)
        self.touch_injector = SimpleNamespace(tap_coordinates=self._tap)
        self.ocr = SimpleNamespace(read_text_boxes=self._boxes)

    def _screenshot(self, device):
        self.shots += 1
        color = (200, 30, 30) if self.open else (30, 30, 30)
        path = str(self.tmp / f"p{self.shots}.png")
        Image.new("RGB", (W, H), color).save(path)
        self.states[path] = self.open
        return Result.ok(path)

    def _near(self, xy, target, r=60):
        return abs(xy[0] - target[0]) <= r and abs(xy[1] - target[1]) <= r

    def _tap(self, device, x, y):
        self.taps.append((x, y))
        if self.open and not self.sticky and (
                (self.kind in self.TEXTS or self.kind == "icons") and self._near((x, y), COLLECT)
                or self.kind == "win" and self._near((x, y), (W // 2, H // 2), 200)):
            self.open = False
            self.bet = self.bet_after_close
        return Result.ok(True)

    def _boxes(self, path):
        if not self.states[path]:
            return Result.ok([_box("SCOMMETTI ATTIVO", (200, 750))] if self.kind == "scommetti" else [])
        if self.kind in self.TEXTS:
            double, collect = self.TEXTS[self.kind]
            return Result.ok([_box(double, DOUBLE), _box(collect, COLLECT)])
        if self.kind == "win":
            return Result.ok([_box("CONGRATULAZIONI! PREMI OVUNQUE PER CONTINUARE", (200, 300), 360)])
        if self.kind == "trap":
            return Result.ok([_box("RACCOGLI E RADDOPPIA", COLLECT, 200)])
        return Result.ok([])


def _guard(world, **kw):
    return PopupGuard(world.screen_reader, world.touch_injector, world.ocr, sleep=lambda s: None, **kw)


@pytest.mark.parametrize("kind", ["it", "en", "en2"])
def test_only_the_collect_button_is_tapped_in_any_language(tmp_path, kind, caplog):
    world = PopupWorld(tmp_path, kind)

    with caplog.at_level(logging.INFO):
        result = _guard(world).clear("d", "Slot")

    assert result.success and result.data.closed and result.data.kind == "gamble"
    assert world.taps == [COLLECT] and not world.open                       # mai "Raddoppia"
    assert "popup raddoppio vincita rilevato, raccolgo vincita" in caplog.text


def test_without_a_popup_nothing_is_touched(tmp_path):
    world = PopupWorld(tmp_path, "none")

    result = _guard(world).clear("d", "Slot", suspect=True)

    assert result.success and not result.data.closed and world.taps == []


def test_a_bare_scommetti_label_is_not_a_gamble_popup(tmp_path):
    world = PopupWorld(tmp_path, "scommetti")

    assert _guard(world).inspect("d", "Slot", suspect=False).kind is None
    assert _guard(world).clear("d", "Slot").data.closed is False and world.taps == []


def test_a_single_text_with_collect_and_double_is_never_tapped(tmp_path):
    world = PopupWorld(tmp_path, "trap")

    result = _guard(world).clear("d", "Slot")

    assert world.taps == [] and (not result.success or not result.data.closed)


def test_the_win_popup_without_buttons_is_closed_with_a_center_tap(tmp_path):
    world = PopupWorld(tmp_path, "win")

    result = _guard(world).clear("d", "Slot")

    assert result.data.closed and result.data.kind == "win" and world.taps == [(W // 2, H // 2)]


def test_icon_only_popup_asks_sol_and_taps_the_collect_icon_then_remembers_it(tmp_path):
    world = PopupWorld(tmp_path, "icons")
    ask = MagicMock(return_value={"gamble_popup": True, "collect": {"x": 750, "y": 750}, "double": {"x": 250, "y": 750}})
    saved = []

    result = _guard(world, ask=ask, save=lambda slot, coords, sig: saved.append((slot, coords, sig))).clear(
        "d", "Slot", suspect=True)

    assert result.data.closed and world.taps == [COLLECT] and ask.call_count == 1
    assert saved and saved[0][0] == "Slot" and saved[0][1] == [300, 600] and saved[0][2]


def test_a_remembered_popup_is_recognised_by_its_fingerprint_without_asking_sol(tmp_path):
    first = PopupWorld(tmp_path, "icons")
    memory = {}
    _guard(first, ask=MagicMock(return_value={"gamble_popup": True, "collect": {"x": 750, "y": 750}, "double": None}),
           save=lambda slot, coords, sig: memory.update(value=(tuple(coords), sig))).clear("d", "Slot", suspect=True)

    second = PopupWorld(tmp_path / "..", "icons") if False else PopupWorld(tmp_path, "icons")
    ask = MagicMock()
    result = _guard(second, ask=ask, saved=lambda slot: memory["value"]).clear("d", "Slot", suspect=True)

    assert result.data.closed and result.data.source == "memory" and second.taps == [COLLECT]
    ask.assert_not_called()


def test_sol_putting_collect_next_to_double_is_not_trusted(tmp_path):
    world = PopupWorld(tmp_path, "icons")
    ask = MagicMock(return_value={"gamble_popup": True, "collect": {"x": 500, "y": 750}, "double": {"x": 520, "y": 750}})

    result = _guard(world, ask=ask).clear("d", "Slot", suspect=True)

    assert world.taps == [] and not result.data.closed


def test_sol_is_only_asked_when_the_caller_suspects_a_popup_and_a_few_times_at_most(tmp_path):
    world = PopupWorld(tmp_path, "icons")
    ask = MagicMock(return_value=None)
    guard = _guard(world, ask=ask)

    guard.clear("d", "Slot", suspect=False)
    assert ask.call_count == 0
    for _ in range(6):
        guard.clear("d", "Slot", suspect=True)
    assert ask.call_count == 3


def test_a_popup_that_does_not_close_fails_after_tapping_only_collect(tmp_path):
    world = PopupWorld(tmp_path, "it", sticky=True)

    result = _guard(world).clear("d", "Slot")

    assert not result.success and result.error_code == "POPUP_NOT_CLOSED"
    assert set(world.taps) == {COLLECT} and len(world.taps) <= 3


def test_the_collect_button_must_not_sit_on_the_double_button(tmp_path):
    world = PopupWorld(tmp_path, "it")
    world.ocr.read_text_boxes = lambda path: Result.ok([_box("RADDOPPIA", (200, 600)), _box("RACCOGLI", (220, 600))]
                                                       if world.states[path] else [])

    result = _guard(world).clear("d", "Slot")

    assert world.taps == [] and result.error_code == "GAMBLE_NO_COLLECT"


# ---- il Vigile chiude il raddoppio prima di ogni altra logica -----------------------------------------------------------

def test_the_warden_collects_first_and_never_calls_sol(tmp_path):
    world = PopupWorld(tmp_path, "it")
    gpt = MagicMock()
    warden = SlotWarden(gpt, world.screen_reader, world.touch_injector, spin_point=lambda slot: (200, 700),
                        sleep=lambda s: None)
    warden.gamble_guard = _guard(world)

    verdict = warden.handle("d", "Slot", "il tocco sullo spin ha cambiato lo schermo senza addebito")

    assert verdict == "resume" and warden.gamble_collected and world.taps == [COLLECT]
    gpt._create_completion.assert_not_called()


def test_the_warden_without_a_gamble_popup_keeps_its_normal_logic(tmp_path):
    world = PopupWorld(tmp_path, "none")
    gpt = MagicMock()
    gpt._create_completion.return_value = Result.ok({"situation": "normal", "action": "resume", "confidence": 0.9,
                                                      "explanation": "ok"})
    warden = SlotWarden(gpt, world.screen_reader, world.touch_injector, spin_point=lambda slot: (200, 700),
                        sleep=lambda s: None)
    warden.gamble_guard = _guard(world)

    assert warden.handle("d", "Slot", "test") == "resume" and not warden.gamble_collected
    assert gpt._create_completion.call_count == 1
