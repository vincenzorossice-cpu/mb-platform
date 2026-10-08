from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import slot_intro
from orchestrator.modules.slot_autopilot.slot_intro import SlotIntro, is_dark_overlay, same_screen, signature


def _img(tmp_path, name, color, size=(300, 600), patch=None):
    path = str(tmp_path / name)
    image = Image.new("RGB", size, color)
    if patch:
        box, patch_color = patch
        image.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), patch_color), box[:2])
    image.save(path)
    return path


class Phone:
    """Telefono finto: ogni tocco fa avanzare la sequenza di schermate."""
    def __init__(self, paths, advance_on_tap=True):
        self.paths = list(paths)
        self.index = 0
        self.taps = []
        self.advance_on_tap = advance_on_tap

    def _screenshot(self, device):
        return Result.ok(self.paths[min(self.index, len(self.paths) - 1)])

    def tap_coordinates(self, device, x, y):
        self.taps.append((x, y))
        if self.advance_on_tap:
            self.index += 1
        return Result.ok(True)


def _intro(phone, tmp_path, **kw):
    return SlotIntro(phone, phone, memory_dir=str(tmp_path / "mem"), sleep=lambda s: None, **kw)


def test_dark_overlay_and_signature_helpers(tmp_path):
    black = _img(tmp_path, "b.png", (0, 0, 0), patch=((100, 250, 200, 280), (255, 255, 255)))
    game = _img(tmp_path, "g.png", (180, 120, 60))
    assert is_dark_overlay(black) and not is_dark_overlay(game)
    assert same_screen(signature(game), signature(game)) and not same_screen(signature(game), signature(black))


def test_black_overlay_is_closed_by_tapping_the_center_without_any_call(tmp_path):
    black = _img(tmp_path, "b.png", (0, 0, 0))
    game = _img(tmp_path, "g.png", (180, 120, 60))
    phone = Phone([black, game])
    describe = MagicMock()
    supervisor = MagicMock()
    supervisor.observe.return_value = SimpleNamespace(screen_state="idle", spin_ready=True)

    result = _intro(phone, tmp_path, describe=describe, supervisor=supervisor).run("d", "Slot")

    assert result.success and phone.taps == [(150, 300)]
    describe.assert_not_called()


def test_new_screen_costs_one_call_and_is_remembered_for_the_next_run(tmp_path):
    black = _img(tmp_path, "b.png", (0, 0, 0))
    welcome = _img(tmp_path, "w.png", (20, 90, 160), patch=((100, 400, 200, 440), (250, 250, 250)))
    game = _img(tmp_path, "g.png", (180, 120, 60))
    describe = MagicMock(return_value=(150, 420))
    supervisor = MagicMock()
    supervisor.observe.side_effect = lambda path, goal: SimpleNamespace(
        screen_state="idle" if path == game else "intro", spin_ready=path == game)

    first = Phone([black, welcome, game])
    assert _intro(first, tmp_path, describe=describe, supervisor=supervisor).run("d", "Slot").success
    assert describe.call_count == 1 and first.taps == [(150, 300), (150, 420)]

    describe.reset_mock()
    second = Phone([black, welcome, game])
    result = _intro(second, tmp_path, describe=describe, supervisor=supervisor).run("d", "Slot")

    assert result.success and result.data.learned == 0
    describe.assert_not_called()                                   # la schermata nota si ripete dalla memoria
    assert second.taps == [(150, 300), (150, 420)]


def test_ready_is_recognised_by_the_learned_arrow_without_asking_sol(tmp_path):
    game = _img(tmp_path, "g.png", (180, 120, 60))
    button = MagicMock()
    button.reference.return_value = object()
    button.classify.return_value = True
    supervisor = MagicMock()

    result = _intro(Phone([game]), tmp_path, button=button, spin_point=lambda slot: (150, 500),
                    supervisor=supervisor).run("d", "Slot")

    assert result.success and result.data.taps == 0
    supervisor.observe.assert_not_called()


def test_screen_that_never_changes_asks_for_help(tmp_path):
    welcome = _img(tmp_path, "w.png", (20, 90, 160))
    phone = Phone([welcome], advance_on_tap=False)
    supervisor = MagicMock()
    supervisor.observe.return_value = SimpleNamespace(screen_state="intro", spin_ready=False)

    result = _intro(phone, tmp_path, describe=MagicMock(return_value=(150, 420)), supervisor=supervisor).run("d", "Slot")

    assert not result.success and result.error_code == "INTRO_STUCK"


def test_without_any_way_to_judge_a_screen_with_nothing_to_tap_is_considered_ready(tmp_path):
    game = _img(tmp_path, "g.png", (180, 120, 60))

    result = _intro(Phone([game]), tmp_path, describe=MagicMock(return_value=None)).run("d", "Slot")

    assert result.success and result.data.taps == 0


def test_describer_failure_never_crashes(tmp_path):
    game = _img(tmp_path, "g.png", (180, 120, 60))
    describe = MagicMock(side_effect=RuntimeError("boom"))

    result = _intro(Phone([game]), tmp_path, describe=describe).run("d", "Slot")

    assert result.success
