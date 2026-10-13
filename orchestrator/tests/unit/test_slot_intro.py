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
        self.swipes = []
        self.swipe_ok = None                    # (x1, x2, indice_schermata) -> bool: quale swipe fa avanzare
        self.advance_on_tap = advance_on_tap
        self.advance_if = advance_if            # (x, y, indice_schermata) -> bool: quale tocco fa avanzare

    def _screenshot(self, device):
        return Result.ok(self.paths[min(self.index, len(self.paths) - 1)])

    def swipe(self, device, x1, y1, x2, y2, duration_ms=400):
        self.swipes.append((x1, y1, x2, y2))
        if self.swipe_ok is not None and self.swipe_ok(x1, x2, self.index):
            self.index += 1
        return Result.ok(True)

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


def test_balance_alone_is_enough_even_if_the_bet_is_unreadable(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"]], advance_on_tap=False)
    ocr = FakeOCR(states={scenes["text"]: {"saldo": 50.0, "posta": None, "spin_button_coords": (160, 520)}})
    supervisor = MagicMock()

    result = _ocr_intro(phone, tmp_path, ocr, supervisor=supervisor).run("d", "Slot")

    assert result.success and phone.taps == [] and result.data.rounds == 1      # niente tocchi, SPIN compreso
    supervisor.observe.assert_not_called()


def test_bet_alone_is_enough_even_if_the_balance_is_unreadable(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"]], advance_on_tap=False)
    ocr = FakeOCR(states={scenes["text"]: {"saldo": None, "posta": 0.2, "spin_button_coords": (160, 520)}})

    result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and phone.taps == []


def test_spin_tap_waits_for_the_round_then_balance_ends_the_intro_and_the_spin_is_not_remembered(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (160, 520))
    ocr = FakeOCR(states={scenes["text"]: {"saldo": None, "posta": None, "spin_button_coords": (160, 520)},
                          scenes["game"]: {"saldo": 99.0, "posta": None, "spin_button_coords": None}})
    pauses = []
    intro = _ocr_intro(phone, tmp_path, ocr)
    intro.sleep = pauses.append

    result = intro.run("d", "Slot")

    assert result.success and phone.taps == [(150, 300), (160, 520)]
    assert slot_intro._SPIN_SETTLE_SECONDS in pauses                            # attesa della fine del giro
    assert result.data.learned == 0 and not (tmp_path / "mem" / "slot.intro.json").exists()


def _gray_scenes(tmp_path, n):
    return [_img(tmp_path, f"s{i}.png", (v, v, v)) for i, v in enumerate(range(70, 70 + 55 * n, 55))]


def test_three_remembered_steps_that_changed_the_screen_end_the_intro(tmp_path, caplog):
    screens = _gray_scenes(tmp_path, 4)
    mem = tmp_path / "mem"
    mem.mkdir()
    steps = [{"sig": signature(path), "x": 10 + i, "y": 20, "kind": "gpt"} for i, path in enumerate(screens[:3])]
    (mem / "slot.intro.json").write_text(json.dumps({"screens": {"300x600": steps}}))
    phone = Phone(screens)                                       # nessun OCR leggibile, Sol dice "ancora intro"

    with caplog.at_level("INFO"):
        result = _ocr_intro(phone, tmp_path, FakeOCR()).run("d", "Slot")

    assert result.success and phone.taps == [(10, 20), (11, 20), (12, 20)]
    assert "3 passi ricordati eseguiti, schermo cambiato 3 volte - intro superate" in caplog.text


def test_three_changes_by_strategies_without_readable_amounts_stay_in_intro(tmp_path):
    screens = _gray_scenes(tmp_path, 4)
    phone = Phone(screens)                                       # ogni tocco cambia schermo, ma nulla e' leggibile
    ocr = FakeOCR(boxes={path: [_box(f"testo {i}", 150, 100 + 50 * i) for i in range(8)] for path in screens})

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


# ---- caricamento e banner ---------------------------------------------------------------------------------------------

def _texts(n, y0=100):
    return [_box(f"testo {i}", 150, y0 + 40 * i) for i in range(n)]


def _banner_boxes():
    """Una schermata informativa con un banner: testi in alto e uno in basso (y > 70% di 600)."""
    return _texts(4) + [_box("Promo", 150, 520)]


def _swipe_phone(paths, works, **kw):
    phone = Phone(paths, advance_on_tap=False, **kw)
    phone.swipe_ok = lambda x1, x2, index: works(x1, x2)
    return phone


def test_loading_screen_waits_instead_of_tapping_and_then_continues(tmp_path, caplog):
    scenes = _scenes(tmp_path)
    loading = _img(tmp_path, "l.png", (90, 90, 140), patch=((100, 100, 200, 140), (200, 200, 200)))
    loaded = _img(tmp_path, "ld.png", (160, 100, 60), patch=((50, 300, 250, 340), (200, 200, 200)))
    phone = Phone([scenes["black"], loading, loaded])
    phone.advance_if = lambda x, y, i: i == 0                      # solo il tocco sull'overlay nero fa avanzare
    seen = []
    ocr = FakeOCR(states={loaded: READY}, boxes={loading: _texts(3), loaded: _texts(12)})
    intro = _ocr_intro(phone, tmp_path, ocr)
    intro.sleep = seen.append
    shots = []
    original = phone._screenshot

    def timed(device):
        shots.append(phone.index)
        if len(shots) >= 4:                                         # dopo due attese il caricamento e' finito
            phone.index = 2
        return original(device)

    phone._screenshot = timed
    with caplog.at_level("INFO"):
        result = intro.run("d", "Slot")

    assert result.success and phone.taps == [(150, 300)]            # nessun tocco durante il caricamento
    assert seen.count(slot_intro._LOADING_SECONDS) >= 1
    assert "schermata di caricamento rilevata (3 elementi), attendo" in caplog.text


def test_loading_wait_is_capped_then_normal_strategies_run(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (150, 300))
    seen = []
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: _texts(2)})
    intro = _ocr_intro(phone, tmp_path, ocr)
    intro.sleep = seen.append

    result = intro.run("d", "Slot")

    assert result.success and phone.taps == [(150, 300)]
    assert seen.count(slot_intro._LOADING_SECONDS) == slot_intro._LOADING_WAITS


def test_no_loading_detection_on_the_black_overlay_or_when_a_button_is_visible(tmp_path):
    scenes = _scenes(tmp_path)
    seen = []
    black = Phone([scenes["black"], scenes["game"]])
    intro = _ocr_intro(black, tmp_path, FakeOCR(states={scenes["game"]: READY}))
    intro.sleep = seen.append
    assert intro.run("d", "Slot").success and black.taps == [(150, 300)]
    assert slot_intro._LOADING_SECONDS not in seen

    seen.clear()
    phone = Phone([scenes["text"], scenes["game"]])
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: [_box("Continua", 150, 540)]})
    intro = _ocr_intro(phone, tmp_path, ocr)
    intro.sleep = seen.append
    assert intro.run("d", "Slot").success and phone.taps == [(150, 540)]
    assert slot_intro._LOADING_SECONDS not in seen


def test_banner_swipe_right_makes_the_balance_readable_and_is_saved(tmp_path, caplog):
    scenes = _scenes(tmp_path)
    phone = _swipe_phone([scenes["text"], scenes["game"]], works=lambda x1, x2: x1 > x2)
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: _banner_boxes() + _texts(3, 300)})

    with caplog.at_level("INFO"):
        result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and phone.swipes == [(240, 510, 60, 510)]
    assert "swipe destra per banner → saldo leggibile" in caplog.text
    saved = json.loads((tmp_path / "mem" / "slot.intro.json").read_text())["screens"]["300x600"]
    step = [s for s in saved if s["kind"] == "swipe"][0]
    assert step["action"] == "swipe_right_banner" and step["coords"] == [240, 510, 60, 510] and step["wait_after"] == 2


def test_swipe_left_when_right_does_nothing(tmp_path, caplog):
    scenes = _scenes(tmp_path)
    phone = _swipe_phone([scenes["text"], scenes["game"]], works=lambda x1, x2: x1 < x2)
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: _banner_boxes() + _texts(3, 300)})

    with caplog.at_level("INFO"):
        result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and [(s[0], s[2]) for s in phone.swipes] == [(240, 60), (60, 240)]
    assert "swipe destra per banner → nessun cambiamento" in caplog.text
    saved = json.loads((tmp_path / "mem" / "slot.intro.json").read_text())["screens"]["300x600"]
    assert [s["action"] for s in saved if s["kind"] == "swipe"] == ["swipe_left_banner"]


def test_no_swipe_works_then_spin_tap_ends_the_intro(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (160, 520))
    phone.swipe_ok = lambda x1, x2, index: False
    ocr = FakeOCR(states={scenes["text"]: {"saldo": None, "posta": None, "spin_button_coords": (160, 520)},
                          scenes["game"]: READY},
                  boxes={scenes["text"]: _banner_boxes() + _texts(3, 300)})

    result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and len(phone.swipes) == 3 and phone.taps[-1] == (160, 520)
    assert not (tmp_path / "mem" / "slot.intro.json").exists()       # niente e' stato ricordato (lo SPIN mai)


def test_saved_swipe_is_replayed_first_without_other_directions(tmp_path):
    scenes = _scenes(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "slot.intro.json").write_text(json.dumps({"screens": {"300x600": [
        {"sig": signature(scenes["text"]), "x": 60, "y": 510, "kind": "swipe", "action": "swipe_left_banner",
         "coords": [60, 510, 240, 510], "wait_after": 2}]}}))
    phone = _swipe_phone([scenes["text"], scenes["game"]], works=lambda x1, x2: (x1, x2) == (60, 240))
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: _banner_boxes()})

    result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and phone.swipes == [(60, 510, 240, 510)] and phone.taps == []
    assert ocr.box_calls == []


def test_saved_swipe_that_stops_working_is_forgotten_and_strategies_take_over(tmp_path):
    scenes = _scenes(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "slot.intro.json").write_text(json.dumps({"screens": {"300x600": [
        {"sig": signature(scenes["text"]), "x": 60, "y": 510, "kind": "swipe", "action": "swipe_left_banner",
         "coords": [60, 510, 240, 510], "wait_after": 2}]}}))
    phone = Phone([scenes["text"], scenes["game"]], advance_if=lambda x, y, i: (x, y) == (150, 540))
    phone.swipe_ok = lambda x1, x2, index: False
    ocr = FakeOCR(states={scenes["game"]: READY}, boxes={scenes["text"]: [_box("Continua", 150, 540)]})

    result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and phone.swipes == [(60, 510, 240, 510)] and phone.taps == [(150, 540)]
    saved = json.loads((mem / "slot.intro.json").read_text())["screens"]["300x600"]
    assert all(step["kind"] != "swipe" for step in saved)


def test_swipes_are_skipped_without_text_at_the_bottom(tmp_path):
    scenes = _scenes(tmp_path)
    phone = Phone([scenes["text"]], advance_on_tap=False)
    ocr = FakeOCR(boxes={scenes["text"]: _texts(8)})

    _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert phone.swipes == []


def test_loading_then_banner_then_spin_all_passed(tmp_path):
    scenes = _scenes(tmp_path)
    loading = _img(tmp_path, "l.png", (90, 90, 140), patch=((100, 100, 200, 140), (200, 200, 200)))
    banner = _img(tmp_path, "bn.png", (60, 40, 100), patch=((20, 400, 280, 560), (220, 220, 220)))

    def advance(x, y, index):
        return index == 0 or (index == 2 and (x, y) == (160, 520))

    phone = Phone([scenes["black"], loading, banner, scenes["game"]], advance_if=advance)
    phone.swipe_ok = lambda x1, x2, index: False
    state = {"saldo": None, "posta": None, "spin_button_coords": (160, 520)}
    ocr = FakeOCR(states={banner: state, scenes["game"]: READY},
                  boxes={loading: _texts(2), banner: _banner_boxes() + _texts(3, 300)})
    original = phone._screenshot
    calls = {"n": 0}

    def screenshot(device):
        calls["n"] += 1
        if phone.index == 1 and calls["n"] >= 4:                       # il caricamento finisce da solo
            phone.index = 2
        return original(device)

    phone._screenshot = screenshot

    result = _ocr_intro(phone, tmp_path, ocr).run("d", "Slot")

    assert result.success and len(phone.swipes) == 3
    assert phone.taps[0] == (150, 300) and phone.taps[-1] == (160, 520)
