import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.login_manager import adb_input
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement
from orchestrator.modules.slot_autopilot import slot_exit
from orchestrator.modules.slot_autopilot.slot_exit import leave_slot

DEVICE = "emulator-5554"
BUTTON = "android.widget.Button"
IMAGE_BUTTON = "android.widget.ImageButton"
VIEW = "android.view.View"


def _el(text, bounds, cls=BUTTON, rid=None):
    return UIElement(text=text, bounds=bounds, resource_id=rid, class_name=cls, confidence=1.0)


def _screen(*elements):
    return ScreenContent(elements=list(elements), screen_type="unknown", confidence=1.0, method="uiautomator")


def _body(*names):
    """Testi di una pagina (4+ elementi): servono per distinguere una pagina da un'altra."""
    return [_el(name, [0, 300 + 100 * i, 800, 380 + 100 * i], VIEW) for i, name in enumerate(names)]


SLOT_BODY = _body("Gioca", "Puntata 0.10", "Saldo 99.80", "Vincita 0.00")
FULL_WIDTH = _el("", [0, 2200, 1080, 2400], VIEW, rid="game:id/footer")        # fa stimare lo schermo 1080x2400
SLOT = _screen(*SLOT_BODY, FULL_WIDTH)
PROMO = _screen(*_body("Promozioni attive", "Happy Monday", "Prova", "Gioca ora"), FULL_WIDTH)

ESCI = _el("Esci", [900, 100, 1050, 180])                                       # centro (975, 140)
CLOSE_ICON = _el("", [950, 60, 1050, 160], IMAGE_BUTTON, rid="game:id/btn_close")   # centro (1000, 110)


class Reader:
    """Mostra le schermate in sequenza, poi ripete l'ultima."""

    def __init__(self, *screens, fail_first=False):
        self.screens, self.fail_first, self.calls = list(screens), fail_first, 0

    def read_screen(self, device):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            return Result.fail("adb offline", "ADB_TIMEOUT")
        return Result.ok(self.screens.pop(0) if len(self.screens) > 1 else self.screens[0])


class Injector:
    def __init__(self, ok=True):
        self.taps, self.ok = [], ok

    def tap_coordinates(self, device, x, y):
        self.taps.append((x, y))
        return Result.ok(True) if self.ok else Result.fail("no", "TAP_MISSED")


def _run(reader, injector=None, back_ok=True, **kwargs):
    injector = injector or Injector()
    back = MagicMock(return_value=Result.ok(True) if back_ok else Result.fail("adb", "ADB_ERROR"))
    sleeps = []
    result = leave_slot(reader, injector, DEVICE, sleep=sleeps.append, press_back=back, **kwargs)
    return SimpleNamespace(result=result, injector=injector, back=back, sleeps=sleeps)


# ---------------- i cinque casi richiesti ----------------

def test_the_esci_button_is_tapped_in_its_centre_and_the_exit_is_verified(caplog):
    with caplog.at_level(logging.INFO):
        run = _run(Reader(_screen(*SLOT_BODY, ESCI, FULL_WIDTH), PROMO))
    assert run.result.success and run.injector.taps == [(975, 140)] and run.sleeps == [3]
    assert "Esci: trovato pulsante 'Esci' a (975,140)" in caplog.text and "Uscita dalla slot effettuata" in caplog.text
    run.back.assert_not_called()


def test_a_chiudi_button_works_too():
    run = _run(Reader(_screen(*SLOT_BODY, _el("Chiudi", [900, 100, 1050, 180]), FULL_WIDTH), PROMO))
    assert run.result.success and run.injector.taps == [(975, 140)]


def test_a_close_icon_in_the_top_right_corner(caplog):
    with caplog.at_level(logging.INFO):
        run = _run(Reader(_screen(*SLOT_BODY, CLOSE_ICON, FULL_WIDTH), PROMO))
    assert run.result.success and run.injector.taps == [(1000, 110)]
    assert "tocco l'icona di chiusura 'game:id/btn_close' a (1000,110)" in caplog.text
    run.back.assert_not_called()


def test_a_close_text_icon_x_is_recognised():
    x_icon = _el("×", [960, 40, 1040, 120], VIEW)
    run = _run(Reader(_screen(*SLOT_BODY, x_icon, FULL_WIDTH), PROMO))
    assert run.result.success and run.injector.taps == [(1000, 80)]


def test_with_no_button_at_all_the_back_key_is_used(caplog):
    with caplog.at_level(logging.INFO):
        run = _run(Reader(SLOT, PROMO))
    assert run.result.success and run.injector.taps == []
    run.back.assert_called_once_with(DEVICE)
    assert "Esci: non trovato, uso tasto Back" in caplog.text and "Uscita dalla slot effettuata" in caplog.text


def test_when_back_does_nothing_we_stay_in_the_slot_with_a_warning(caplog):
    with caplog.at_level(logging.INFO):
        run = _run(Reader(SLOT))                                     # lo schermo non cambia mai
    assert not run.result.success and run.result.error_code == "EXIT_FAILED"
    run.back.assert_called_once()                                    # una sola volta
    assert "resto nella slot" in caplog.text


# ---------------- scelta del pulsante ----------------

def test_esci_has_priority_over_chiudi():
    chiudi = _el("Chiudi", [800, 100, 900, 180])
    run = _run(Reader(_screen(*SLOT_BODY, chiudi, ESCI, FULL_WIDTH), PROMO))
    assert run.injector.taps == [(975, 140)]


def test_menu_and_textual_x_labels_away_from_the_corner_are_never_tapped():
    menu = _el("Menu", [900, 100, 1050, 180])                           # apre un menu, non esce
    multiplier = _el("x2", [900, 200, 1050, 280], VIEW)                 # moltiplicatore nell'angolo: non e' una chiusura
    label_x = _el("X", [100, 1500, 200, 1580], VIEW)                    # "X" lontana dall'angolo in alto a destra
    run = _run(Reader(_screen(*SLOT_BODY, menu, multiplier, label_x, FULL_WIDTH), PROMO))
    assert run.injector.taps == [] and run.back.call_count == 1         # niente da toccare: Back


def test_a_chiudi_inside_a_long_text_is_not_a_button():
    text = _el("Per uscire dal gioco chiudi la finestra del browser e torna alla pagina", [0, 100, 900, 180], VIEW)
    run = _run(Reader(_screen(*SLOT_BODY, text, FULL_WIDTH), PROMO))
    assert run.injector.taps == []


def test_a_popup_is_closed_first_and_then_esci_is_found():
    popup = _screen(*SLOT_BODY, _el("Chiudi", [900, 100, 1050, 180]), FULL_WIDTH)
    after_popup = _screen(*SLOT_BODY, ESCI, FULL_WIDTH)                # popup chiuso: la slot e' ancora li'
    run = _run(Reader(popup, after_popup, PROMO))
    assert run.result.success and run.injector.taps == [(975, 140), (975, 140)] and run.sleeps == [3, 3]
    run.back.assert_not_called()


def test_the_same_element_is_not_tapped_twice():
    stuck = _screen(*SLOT_BODY, ESCI, FULL_WIDTH)                      # il tocco su Esci non cambia nulla
    run = _run(Reader(stuck))
    assert run.injector.taps == [(975, 140)]                            # una volta sola
    run.back.assert_called_once()                                       # poi si passa al Back
    assert not run.result.success


def test_a_failed_tap_moves_on_to_the_next_strategy():
    run = _run(Reader(_screen(*SLOT_BODY, ESCI, FULL_WIDTH), PROMO), injector=Injector(ok=False))
    assert run.result.success and run.back.call_count == 1


def test_a_failed_back_is_not_fatal(caplog):
    with caplog.at_level(logging.WARNING):
        run = _run(Reader(SLOT), back_ok=False)
    assert not run.result.success and "tasto Back fallito" in caplog.text


# ---------------- icone ----------------

def test_a_single_unmarked_icon_in_the_corner_is_tapped():
    icon = _el("", [950, 60, 1050, 160], IMAGE_BUTTON, rid="game:id/icon_7")
    run = _run(Reader(_screen(*SLOT_BODY, icon, FULL_WIDTH), PROMO))
    assert run.injector.taps == [(1000, 110)]


def test_two_unmarked_icons_are_not_guessed():
    icons = [_el("", [850, 60, 950, 160], IMAGE_BUTTON, rid="game:id/icon_a"),
             _el("", [950, 60, 1050, 160], IMAGE_BUTTON, rid="game:id/icon_b")]
    run = _run(Reader(_screen(*SLOT_BODY, *icons, FULL_WIDTH), PROMO))
    assert run.injector.taps == [] and run.back.call_count == 1


def test_a_labelled_button_in_the_corner_is_not_an_anonymous_icon():
    run = _run(Reader(_screen(*SLOT_BODY, _el("Info", [950, 60, 1050, 160]), FULL_WIDTH), PROMO))
    assert run.injector.taps == [] and run.back.call_count == 1


def test_the_marked_icon_wins_over_others():
    other = _el("", [850, 60, 950, 160], IMAGE_BUTTON, rid="game:id/icon_sound")
    run = _run(Reader(_screen(*SLOT_BODY, other, CLOSE_ICON, FULL_WIDTH), PROMO))
    assert run.injector.taps == [(1000, 110)]


@pytest.mark.parametrize("icon", [
    _el("", [100, 60, 200, 160], IMAGE_BUTTON, rid="game:id/btn_close"),         # a sinistra
    _el("", [950, 900, 1050, 1000], IMAGE_BUTTON, rid="game:id/btn_close"),      # troppo in basso
    _el("", [700, 60, 1050, 400], IMAGE_BUTTON, rid="game:id/btn_close"),        # troppo grande
    _el("", [950, 60, 1050, 160], "android.widget.EditText", rid="game:id/btn_close"),
])
def test_icons_outside_the_corner_or_of_the_wrong_kind_are_ignored(icon):
    run = _run(Reader(_screen(*SLOT_BODY, icon, FULL_WIDTH), PROMO))
    assert run.injector.taps == [] and run.back.call_count == 1


# ---------------- verifica e casi limite ----------------

def test_an_unreadable_first_screen_touches_nothing():
    run = _run(Reader(SLOT, fail_first=True))
    assert not run.result.success and run.injector.taps == [] and run.back.call_count == 0


def test_a_screen_without_elements_uses_back_and_reports_it_cannot_verify(caplog):
    with caplog.at_level(logging.INFO):
        run = _run(Reader(_screen(), _screen()))
    assert run.result.success and run.back.call_count == 1
    assert "uscita non verificabile" in caplog.text


def test_a_changed_chrome_url_counts_as_left_even_with_similar_texts():
    bar = lambda url: _el(url, [0, 0, 900, 100], "android.widget.EditText", rid="com.android.chrome:id/url_bar")   # noqa: E731
    before = _screen(*SLOT_BODY, FULL_WIDTH, bar("www.sisal.it/slot/hellos"))
    after = _screen(*SLOT_BODY, FULL_WIDTH, bar("www.sisal.it/bonus/promozioni"))
    run = _run(Reader(before, after))
    assert run.result.success


def test_the_number_of_actions_is_capped():
    run = _run(Reader(_screen(*SLOT_BODY, _el("Chiudi", [900, 100, 1050, 180]), FULL_WIDTH)), max_actions=1)
    assert not run.result.success and len(run.injector.taps) == 1 and run.back.call_count == 0


def test_an_exception_is_not_fatal(caplog):
    reader = MagicMock()
    reader.read_screen.side_effect = RuntimeError("adb")
    with caplog.at_level(logging.WARNING):
        run = _run(reader)
    assert not run.result.success and run.result.error_code == "EXIT_ERROR" and caplog.records


class TestLeftSlot:
    def test_a_different_page_is_left(self):
        assert slot_exit._left_slot(SLOT, PROMO) is True

    def test_a_popup_over_the_same_page_is_not(self):
        popup = _screen(*SLOT_BODY, FULL_WIDTH, _el("Vuoi uscire?", [100, 500, 900, 600], VIEW))
        assert slot_exit._left_slot(SLOT, popup) is False

    def test_nearly_empty_screens_cannot_be_judged(self):
        assert slot_exit._left_slot(_screen(), _screen(_el("Gioca", [0, 0, 10, 10], VIEW))) is None

    def test_a_page_that_disappears_is_left(self):
        assert slot_exit._left_slot(SLOT, _screen()) is True


# ---------------- tasto Back via ADB ----------------

class TestAdbKeys:
    RUN = "orchestrator.modules.login_manager.adb_input.subprocess.run"

    def test_back_sends_keyevent_4(self):
        with patch(self.RUN, return_value=SimpleNamespace(returncode=0, stderr="")) as run:
            assert adb_input.press_back(DEVICE).success
        assert run.call_args.args[0] == ["adb", "-s", DEVICE, "shell", "input", "keyevent", "4"]

    def test_enter_still_sends_keyevent_66(self):
        with patch(self.RUN, return_value=SimpleNamespace(returncode=0, stderr="")) as run:
            assert adb_input.press_enter(DEVICE).success
        assert run.call_args.args[0][-3:] == ["input", "keyevent", "66"]

    def test_errors_become_results(self):
        with patch(self.RUN, return_value=SimpleNamespace(returncode=1, stderr="no device")):
            assert adb_input.press_back(DEVICE).error_code == "ADB_ERROR"
        with patch(self.RUN, side_effect=subprocess.TimeoutExpired("adb", 10)):
            assert adb_input.press_back(DEVICE).error_code == "ADB_TIMEOUT"
        with patch(self.RUN, side_effect=OSError("adb non trovato")):
            assert adb_input.press_back(DEVICE).error_code == "ADB_ERROR"
