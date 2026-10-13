import json
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
    def __init__(self, paths, advance_on_tap=True, advance_if=None):
        self.paths = list(paths)
        self.index = 0
        self.taps = []
        self.advance_on_tap = advance_on_tap
        self.advance_if = advance_if            # (x, y, indice_schermata) -> bool: quale tocco fa avanzare

    def _screenshot(self, device):
        return Result.ok(self.paths[min(self.index, len(self.paths) - 1)])

    def tap_coordinates(self, device, x, y):
        self.taps.append((x, y))
        if self.advance_if is not None:
            if self.advance_if(x, y, self.index):
                self.index += 1
        elif self.advance_on_tap:
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

    def only_gpt_point_works(x, y, index):
        return index == 0 or (x, y) == (150, 420)

    first = Phone([black, welcome, game], advance_if=only_gpt_point_works)
    assert _intro(first, tmp_path, describe=describe, supervisor=supervisor).run("d", "Slot").success
    # prima le strategie generiche (centro, basso), GPT solo alla fine e una volta sola
    assert describe.call_count == 1 and first.taps == [(150, 300), (150, 300), (150, 474), (150, 420)]

    describe.reset_mock()
    second = Phone([black, welcome, game], advance_if=only_gpt_point_works)
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


# ---- strategie generiche, OCR, memoria --------------------------------------------------------------------------------

class FakeOCR:
    """OCR semplice finto: `states` e `boxes` dipendono dal file dello screenshot."""
    def __init__(self, states=None, boxes=None):
        self.states = states or {}
        self.boxes = boxes or {}
        self.state_calls = []
        self.box_calls = []

    def read_slot_state(self, path):
        self.state_calls.append(path)
        return self.states.get(path, {"saldo": None, "posta": None, "spin_button_coords": None})

    def read_text_boxes(self, path):
        self.box_calls.append(path)
        return Result.ok(self.boxes.get(path, []))


def _box(text, x, y, w=200, h=60):
    return SimpleNamespace(text=text, bbox=(x - w // 2, y - h // 2, x + w // 2, y + h // 2), confidence=0.9)


READY = {"saldo": 99999.8, "posta": 1.0, "spin_button_coords": None}
SUPERVISOR_NEVER_READY = SimpleNamespace(observe=lambda path, goal: SimpleNamespace(screen_state="intro", spin_ready=False))


def _ocr_intro(phone, tmp_path, ocr, **kw):
    kw.setdefault("supervisor", SUPERVISOR_NEVER_READY)
    return _intro(phone, tmp_path, ocr=ocr, **kw)


def _scenes(tmp_path):
    return {
        "black": _img(tmp_path, "b.png", (0, 0, 0)),
        "text": _img(tmp_path, "t.png", (20, 90, 160), patch=((50, 200, 250, 260), (250, 250, 250))),
        "game": _img(tmp_path, "g.png", (180, 120, 60)),
    }


def test_black_overlay_then_center_then_balance_and_bet_readable_is_success(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["black"], scenes["game"]])
    ocr = FakeOCR(states={scenes["game"]: READY})
    describe = MagicMock()

    result = _ocr_intro(phone, tmp_path, ocr, describe=describe).run("d", "Slot")

    assert result.success and phone.taps == [(150, 300)]
    describe.assert_not_called()


def test_center_has_no_effect_then_continua_found_by_text(tmp_path, caplog):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (150, 540))
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: [_box("Continua", 150, 540)]})
    describe = MagicMock()

    with caplog.at_level("INFO"):
        result = _ocr_intro(phone, tmp_path, ocr, describe=describe).run("d", "Slot")

    assert result.success and phone.taps == [(150, 540)]
    describe.assert_not_called()
    assert "tentativo 1 - cercato pulsanti: trovato 'Continua' a (150,540) - schermo cambiato" in caplog.text


def test_black_overlay_center_first_then_buttons_with_attempt_logs(tmp_path, caplog):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["black"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (150, 540))
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["black"]: [_box("Continua", 150, 540)]})

    with caplog.at_level("INFO"):
        result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and phone.taps == [(150, 300), (150, 540)]       # il centro per primo sull'overlay nero
    assert "tentativo 1 - tap centro (150,300) - schermo non cambiato" in caplog.text
    assert "tentativo 2 - cercato pulsanti: trovato 'Continua' a (150,540) - schermo cambiato" in caplog.text


def test_no_buttons_then_spin_found_by_ocr(tmp_path, caplog):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (160, 520))
    ocr = FakeOCR(states={scenes["text"]: {"saldo": None, "posta": None, "spin_button_coords": (160, 520)},
                          scenes["game"]: READY})

    with caplog.at_level("INFO"):
        result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and phone.taps == [(150, 300), (160, 520)]
    assert "cercato SPIN: trovato a (160,520) - schermo cambiato" in caplog.text


def test_spin_point_from_memory_is_preferred_to_ocr(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (111, 222))
    ocr = FakeOCR(states={scenes["text"]: {"saldo": None, "posta": None, "spin_button_coords": (160, 520)},
                          scenes["game"]: READY})

    result = _ocr_intro(phone, tmp_path, ocr, spin_point=lambda slot: (111, 222)).run("d", "Slot")

    assert result.success and phone.taps == [(150, 300), (111, 222)]


def test_gpt_is_the_last_resort_after_center_spin_and_bottom(tmp_path):
    scenes = _scenes(tmp_path)
    describe = MagicMock(return_value=(30, 40))
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (30, 40))
    ocr = FakeOCR(states={scenes["game"]: READY})

    result = _ocr_intro(phone, tmp_path, ocr, describe=describe).run("d", "Slot")

    assert result.success and phone.taps == [(150, 300), (150, 474), (30, 40)]    # centro, basso (0.79*600), GPT
    assert describe.call_count == 1


def test_saved_intro_steps_are_used_before_any_strategy(tmp_path):
    scenes = _scenes(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    sig = signature(scenes["text"])
    (mem / "slot.intro.json").write_text(json.dumps(
        {"screens": {"300x600": [{"sig": sig, "x": 77, "y": 88, "kind": "buttons", "text": "Continua"}]}}))
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (77, 88))
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: [_box("Gioca", 150, 540)]})
    describe = MagicMock()

    result = _ocr_intro(phone, tmp_path, ocr, describe=describe).run("d", "Slot")

    assert result.success and phone.taps == [(77, 88)]
    describe.assert_not_called()
    assert ocr.box_calls == []                                      # nessuna ricerca di pulsanti


def test_balance_readable_immediately_skips_the_intro(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["game"]])
    ocr = FakeOCR(states={scenes["game"]: READY})
    supervisor = MagicMock()

    result = _ocr_intro(phone, tmp_path, ocr, supervisor=supervisor).run("d", "Slot")

    assert result.success and phone.taps == [] and result.data.rounds == 1
    supervisor.observe.assert_not_called()                           # niente chiamata a Sol


def test_balance_alone_is_not_enough_to_be_ready(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"]], advance_on_tap=False)
    ocr = FakeOCR(states={scenes["text"]: {"saldo": 50.0, "posta": None, "spin_button_coords": None}})

    result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert not result.success and result.error_code == "INTRO_STUCK"


def test_non_dark_screen_tries_buttons_before_center_and_dark_tries_center_first(tmp_path):
    scenes = _scenes(tmp_path)
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: [_box("Continua", 150, 540)]})
    phone = Phone([scenes["text"], scenes["game"]])

    _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert phone.taps[0] == (150, 540)                              # niente tocco cieco al centro prima del pulsante


def test_center_is_tapped_only_once_per_screen_and_stuck_only_after_all_strategies(tmp_path, caplog):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["black"]], advance_on_tap=False)
    describe = MagicMock(return_value=None)

    with caplog.at_level("INFO"):
        result = _ocr_intro(phone, tmp_path, FakeOCR(), describe=describe).run("d", "Slot")

    assert not result.success and result.error_code == "INTRO_STUCK"
    assert phone.taps == [(150, 300), (150, 474)]                   # centro e basso, una volta ciascuno
    assert describe.call_count == 1
    assert "tentativo 1 - tap centro (150,300) - schermo non cambiato" in caplog.text
    assert "tentativo 2 - tap in basso al centro (150,474) - schermo non cambiato" in caplog.text


def test_forbidden_and_non_button_texts_are_never_tapped():
    from orchestrator.modules.slot_autopilot.slot_intro import _button_rank
    for text in ("Acquista", "Esci", "Menu", "Gioca con denaro reale", "Buy bonus", "Bet", "X2", "Continua a leggere le regole del gioco qui"):
        assert _button_rank(text) is None, text
    assert _button_rank("X") is not None and _button_rank("CONTINUA") is not None
    assert _button_rank("Tocca per continuare") is not None


def test_button_priority_and_next_candidate_after_a_failure(tmp_path):
    scenes = _scenes(tmp_path)
    boxes = [_box("X", 270, 40), _box("Gioca", 150, 400), _box("Continua", 150, 540)]
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (150, 400))
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: boxes})

    result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and phone.taps == [(150, 540), (150, 400)]    # prima Continua, poi Gioca; la X non serve


def test_successful_tap_is_saved_with_its_kind_and_replayed_without_strategies(tmp_path):
    scenes = _scenes(tmp_path)
    boxes = {scenes["text"]: [_box("Continua", 150, 540)]}
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes=boxes)
    first = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (150, 540))
    outcome = _ocr_intro(first, tmp_path, ocr).run("d", "Slot")
    saved = json.loads((tmp_path / "mem" / "slot.intro.json").read_text())["screens"]["300x600"]
    assert outcome.data.learned == 1 and saved[0]["kind"] == "buttons" and saved[0]["text"] == "Continua"

    ocr2 = FakeOCR(states={scenes["game"]: READY}, boxes=boxes)
    second = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (150, 540))
    again = _ocr_intro(second, tmp_path, ocr2).run("d", "Slot")
    assert again.success and again.data.learned == 0 and second.taps == [(150, 540)]
    assert ocr2.box_calls == []


def test_remembered_step_that_stops_working_is_forgotten(tmp_path):
    scenes = _scenes(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "slot.intro.json").write_text(json.dumps(
        {"screens": {"300x600": [{"sig": signature(scenes["text"]), "x": 5, "y": 6, "kind": "gpt"}]}}))
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (150, 540))
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: [_box("Continua", 150, 540)]})

    result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and phone.taps == [(5, 6), (150, 540)]
    saved = json.loads((mem / "slot.intro.json").read_text())["screens"]["300x600"]
    assert [(s["x"], s["y"]) for s in saved] == [(150, 540)]         # il vecchio passo e' sparito, quello nuovo c'e'


def test_two_resolutions_share_one_file_and_old_format_is_still_read(tmp_path):
    scenes = _scenes(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    sig = signature(scenes["text"])
    (mem / "slot.intro.json").write_text(json.dumps({"screen": [300, 600], "steps": [{"sig": sig, "x": 7, "y": 8}]}))
    intro = _intro(Phone([scenes["text"]]), tmp_path)
    assert intro._load("Slot", (300, 600))[0]["x"] == 7 and intro._load("Slot", (400, 800)) == []

    intro._save("Slot", (400, 800), [{"sig": sig, "x": 1, "y": 2, "kind": "center"}])
    assert intro._load("Slot", (300, 600))[0]["x"] == 7 and intro._load("Slot", (400, 800))[0]["x"] == 1


def test_build_slot_intro_uses_the_plain_ocr_not_the_gpt_wrapper():
    from orchestrator.modules.slot_autopilot.slot_intro import build_slot_intro
    plain = object()
    autopilot = SimpleNamespace(screen_reader=object(), touch_injector=object(), locator=None,
                                ocr_reader=SimpleNamespace(ocr=plain))
    assert build_slot_intro(autopilot).ocr is plain
    bare = object()
    assert build_slot_intro(SimpleNamespace(screen_reader=object(), touch_injector=object(), locator=None,
                                            ocr_reader=bare)).ocr is bare
