from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image, ImageDraw

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.bet_watch import BetWatch
from orchestrator.modules.slot_autopilot.spin_controller import SpinResult

W, H = 600, 200
VALUES = ["0.10", "0.20", "0.30", "0.40", "0.50"]


def _cell_x(i):
    return 50 + 110 * i


def _screen(tmp_path, name, selected):
    """Riga di 5 celle di puntata: quella selezionata e' verde, le altre nere."""
    image = Image.new("RGB", (W, H), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    for i in range(len(VALUES)):
        x = _cell_x(i)
        draw.rectangle((x - 10, 70, x + 90, 130), fill=(0, 160, 40) if i == selected else (0, 0, 0))
        draw.text((x + 20, 92), VALUES[i], fill=(255, 255, 255))
    path = str(tmp_path / name)
    image.save(path)
    return path


def _boxes(values=VALUES):
    return [SimpleNamespace(text=v, bbox=(_cell_x(i) + 10, 85, _cell_x(i) + 70, 115), confidence=0.9)
            for i, v in enumerate(values)]


def test_learns_the_selected_cell_by_value_and_notices_when_the_selection_moves(tmp_path):
    first = _screen(tmp_path, "a.png", selected=0)
    watch = BetWatch()

    assert watch.learn(first, _boxes(), Decimal("0.10"))
    assert watch.check(_screen(tmp_path, "same.png", selected=0)) is False
    assert watch.check(_screen(tmp_path, "moved.png", selected=2)) is True       # tocco accidentale sullo 0.30


def test_nothing_to_watch_without_a_row_of_cells(tmp_path):
    watch = BetWatch()

    assert not watch.learn(_screen(tmp_path, "a.png", 0), _boxes(VALUES[:2]), Decimal("0.10"))   # 2 celle: non e' una riga
    assert watch.check(_screen(tmp_path, "b.png", 2)) is None
    assert not watch.learn(_screen(tmp_path, "c.png", 0), _boxes(), Decimal("0.05"))            # il valore non c'e'
    assert not watch.learn(_screen(tmp_path, "d.png", 0), [], Decimal("0.10"))


def test_a_disabled_watch_does_nothing(tmp_path):
    watch = BetWatch()
    watch.enabled = False

    assert not watch.learn(_screen(tmp_path, "a.png", 0), _boxes(), Decimal("0.10"))
    assert watch.check(_screen(tmp_path, "b.png", 2)) is None


def test_relearning_replaces_the_fingerprint(tmp_path):
    watch = BetWatch()
    watch.learn(_screen(tmp_path, "a.png", 0), _boxes(), Decimal("0.10"))

    watch.learn(_screen(tmp_path, "b.png", 2), _boxes(), Decimal("0.30"))          # ora la puntata verificata e' 0.30

    assert watch.check(_screen(tmp_path, "c.png", 2)) is False
    assert watch.check(_screen(tmp_path, "d.png", 0)) is True


def test_unreadable_screenshot_is_not_verifiable(tmp_path):
    watch = BetWatch()
    watch.learn(_screen(tmp_path, "a.png", 0), _boxes(), Decimal("0.10"))

    assert watch.check(str(tmp_path / "missing.png")) is None


# ---- cablaggio in SlotAutoPilot.spin e in sessione ---------------------------------------------------------------------

def _autopilot_with_watch(tmp_path, screenshot_after):
    from orchestrator.tests.unit.test_slot_autopilot_autopilot import _collaborators, _config
    from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
    deps = _collaborators(config=_config())
    result = SpinResult(slot_name="pirots5", spin_index=1, bet_amount=Decimal("0.10"), balance_before=Decimal("100"),
                        balance_after=Decimal("99.9"), win_amount=None, verified=True, attempts=1, outcome="success",
                        screenshot_after=screenshot_after)
    deps["spin_controller"].spin.return_value = Result.ok(result)
    deps["ocr_reader"].region = None
    autopilot = SlotAutoPilot(**deps)
    autopilot.bet_watch.learn(_screen(tmp_path, "ref.png", 0), _boxes(), Decimal("0.10"))
    return autopilot


def _with_second_shot(autopilot, path, monkeypatch):
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.autopilot.time.sleep", lambda s: None)
    autopilot.screen_reader._screenshot.return_value = Result.ok(path)
    return autopilot


def test_spin_flags_a_changed_bet_cell_when_two_screenshots_agree(tmp_path, monkeypatch, caplog):
    moved = _screen(tmp_path, "after.png", selected=2)
    autopilot = _with_second_shot(_autopilot_with_watch(tmp_path, moved), _screen(tmp_path, "again.png", 2), monkeypatch)

    with caplog.at_level("INFO"):
        result = autopilot.spin("dev", 1)

    assert result.success and result.data.bet_cell_changed is True
    assert "Bet Watch: delta visivo" in caplog.text and "soglia 25" in caplog.text           # il delta e' nel log


def test_an_animation_seen_only_in_the_first_screenshot_is_not_an_alarm(tmp_path, monkeypatch, caplog):
    flicker = _screen(tmp_path, "after.png", selected=2)                                       # solo nel primo
    autopilot = _with_second_shot(_autopilot_with_watch(tmp_path, flicker), _screen(tmp_path, "again.png", 0), monkeypatch)

    with caplog.at_level("INFO"):
        result = autopilot.spin("dev", 1)

    assert result.data.bet_cell_changed is False
    assert "era un'animazione, nessun allarme" in caplog.text


def test_spin_leaves_the_flag_off_when_the_cell_is_the_same(tmp_path, monkeypatch):
    autopilot = _with_second_shot(_autopilot_with_watch(tmp_path, _screen(tmp_path, "after.png", selected=0)),
                                  _screen(tmp_path, "again.png", 0), monkeypatch)

    assert autopilot.spin("dev", 1).data.bet_cell_changed is False


def test_bet_watch_never_looks_at_the_quick_stop_test_spin(tmp_path, monkeypatch):
    from orchestrator.tests.unit.test_slot_autopilot_autopilot import _collaborators, _config
    from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
    moved = _screen(tmp_path, "after.png", selected=2)
    deps = _collaborators(config=_config())
    result = SpinResult(slot_name="pirots5", spin_index=1, bet_amount=Decimal("0.10"), balance_before=Decimal("100"),
                        balance_after=Decimal("99.9"), win_amount=None, verified=True, attempts=1, outcome="success",
                        screenshot_after=moved, quick_mode="test")
    deps["spin_controller"].spin.return_value = Result.ok(result)
    deps["ocr_reader"].region = None
    autopilot = SlotAutoPilot(**deps)
    autopilot.bet_watch.learn(_screen(tmp_path, "ref.png", 0), _boxes(), Decimal("0.10"))
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.autopilot.time.sleep", lambda s: None)
    deps["screen_reader"]._screenshot.return_value = Result.ok(moved)

    out = autopilot.spin("dev", 1)

    assert out.data.bet_cell_changed is False                         # la cella e' diversa, ma e' lo spin di prova
    deps["screen_reader"]._screenshot.assert_not_called()


def test_the_last_delta_is_recorded_for_the_logs(tmp_path):
    watch = BetWatch()
    watch.learn(_screen(tmp_path, "a.png", 0), _boxes(), Decimal("0.10"))

    watch.check(_screen(tmp_path, "same.png", 0))
    same = watch.last_delta
    watch.check(_screen(tmp_path, "moved.png", 3))

    assert same == 0.0 and watch.last_delta > 25


def test_set_min_bet_relearns_the_cell_with_one_fresh_reading(tmp_path, monkeypatch):
    from orchestrator.tests.unit.test_slot_autopilot_autopilot import _collaborators, _config
    from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
    from orchestrator.modules.slot_autopilot.bet_controller import BetResult
    monkeypatch.chdir(tmp_path)
    shot = _screen(tmp_path, "now.png", selected=0)
    deps = _collaborators(config=_config(bet_watch_enabled=True))
    deps["screen_reader"]._screenshot.return_value = Result.ok(shot)
    deps["ocr_reader"].read_slot_state.return_value = {"posta": 0.1, "saldo": 10.0, "raw_texts": []}
    deps["ocr_reader"].read_text_boxes.return_value = Result.ok(_boxes())
    deps["bet_controller"].set_min_bet.return_value = Result.ok(BetResult(
        slot_name="pirots5", starting_bet=Decimal("0.10"), final_bet=Decimal("0.10"), min_bet_verified=True,
        attempts=0, outcome="unchanged_already_min"))
    autopilot = SlotAutoPilot(**deps)

    autopilot.set_min_bet("dev")

    assert autopilot.bet_watch.active
    assert autopilot.bet_watch.check(_screen(tmp_path, "moved.png", selected=3)) is True


class TestSessionReactsToAChangedCell:
    @staticmethod
    def _spins(changed_at):
        def spin(i):
            return Result.ok(SpinResult(
                slot_name="s", spin_index=i, bet_amount=Decimal("0.10"), balance_before=Decimal("100"),
                balance_after=Decimal("99.9"), win_amount=None, verified=True, attempts=1, outcome="success",
                bet_cell_changed=i in changed_at))
        return [spin(i) for i in range(1, 12)]

    def test_the_bet_is_read_again_and_that_spin_is_not_counted(self, monkeypatch):
        from orchestrator.modules.slot_autopilot.session import play_session
        from orchestrator.tests.unit.test_slot_autopilot_session import _autopilot
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)
        autopilot = _autopilot(self._spins({2}))

        result = play_session(autopilot, "dev", "slot", Decimal("0.30"), 50)

        assert result.target_reached and not result.failed
        assert autopilot.set_min_bet.call_count == 2                 # iniziale + riletta per la cella cambiata
        assert result.total_played == Decimal("0.30") and result.spins == 4    # lo spin 2 non e' contato

    def test_too_many_alarms_switch_the_watch_off_and_the_session_goes_on(self, monkeypatch):
        from orchestrator.modules.slot_autopilot import session as session_module
        from orchestrator.modules.slot_autopilot.session import play_session
        from orchestrator.tests.unit.test_slot_autopilot_session import _autopilot
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)
        autopilot = _autopilot(self._spins(set(range(1, 12))))
        autopilot.bet_watch = SimpleNamespace(enabled=True)

        result = play_session(autopilot, "dev", "slot", Decimal("0.10"), 50)

        assert not result.failed and autopilot.bet_watch.enabled is False
        assert autopilot.set_min_bet.call_count == 1 + session_module.MAX_VISUAL_RECHECKS

    def test_a_bet_that_cannot_be_verified_again_stops_the_session(self, monkeypatch):
        from orchestrator.modules.slot_autopilot.bet_controller import BetResult
        from orchestrator.modules.slot_autopilot.session import play_session
        from orchestrator.tests.unit.test_slot_autopilot_session import _autopilot
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda s: None)
        autopilot = _autopilot(self._spins({1}))
        good = autopilot.set_min_bet.return_value
        bad = Result.ok(BetResult(slot_name="s", starting_bet=Decimal("0.30"), final_bet=None, min_bet_verified=False,
                                  attempts=1, aborted_reason="BET_UNREADABLE"))
        autopilot.set_min_bet.side_effect = [good, bad, bad, bad]

        result = play_session(autopilot, "dev", "slot", Decimal("0.30"), 50)

        assert result.failed and result.stop_reason == "BET_NOT_VERIFIED"
