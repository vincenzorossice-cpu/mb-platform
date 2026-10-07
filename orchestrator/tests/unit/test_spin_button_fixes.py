from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.spin_controller import SpinController
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, CalmResult, TapOutcome
from orchestrator.modules.slot_ocr.vision_reader import ResilientOCRReader
from orchestrator.scripts import spin_button_probe


def _config(**kw):
    return AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"),
                           frame_diff_poll_interval=0.0, spin_calm_max_wait_seconds=1.0, **kw)


def _verifier(states):
    """Verificatore con un telefono finto: ogni scatto e' un percorso e ready_check ne legge lo stato in sequenza."""
    reader = MagicMock()
    reader._screenshot.return_value = Result.ok("frame.png")
    verifier = ActionVerifier(MagicMock(), reader, MagicMock(), _config())
    sequence = iter(states)
    last = {"v": states[-1]}

    def check(path):
        last["v"] = next(sequence, last["v"])
        return last["v"]

    return verifier, check, reader


class TestPulsingArrowBeforeTheTap:
    def test_a_pulsing_arrow_is_accepted_at_its_first_visible_frame(self):
        verifier, check, reader = _verifier([False, True, False, False, True])

        result = verifier.wait_until_button_ready("d", check, 5.0)

        assert result.data.calm and result.data.waited and reader._screenshot.call_count == 2

    def test_busy_busy_ready_is_accepted_at_the_first_ready(self):
        verifier, check, reader = _verifier([False, False, True])

        assert verifier.wait_until_button_ready("d", check, 5.0).data.calm
        assert reader._screenshot.call_count == 3

    def test_an_arrow_visible_at_once_costs_one_screenshot(self):
        verifier, check, reader = _verifier([True])

        result = verifier.wait_until_button_ready("d", check, 5.0)

        assert result.data.calm and not result.data.waited and reader._screenshot.call_count == 1

    def test_an_arrow_that_never_comes_back_still_times_out(self):
        verifier, check, _ = _verifier([False])

        result = verifier.wait_until_button_ready("d", check, 0.05)

        assert result.success and not result.data.calm


class TestOneFrameIsEnoughLate:
    def _run(self, typical, monkeypatch, interval=1.0):
        verifier, check, reader = _verifier([True])               # la freccia e' gia' tornata, mai vista "occupata"
        verifier.config.spin_calm_max_wait_seconds = 30.0
        clock = {"now": 100.0}
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.verifier.time.monotonic", lambda: clock["now"])
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.verifier.time.sleep",
                            lambda s: clock.__setitem__("now", clock["now"] + interval))
        wait, ended = verifier._wait_after_tap("d", SimpleNamespace(region=None), check, 100.0, 0.0, typical)
        return reader._screenshot.call_count, ended

    def test_after_85_percent_of_the_typical_duration_a_single_ready_frame_ends_the_wait(self, monkeypatch):
        shots, ended = self._run(typical=2.0, monkeypatch=monkeypatch)        # 85% = 1,7 s

        assert ended and shots == 3          # frame a 0 e 1 s (prima dei 1,2 s minimi) e a 2 s: pronto e abbastanza tardi

    def test_without_a_typical_duration_two_ready_frames_are_still_required(self, monkeypatch):
        shots, ended = self._run(typical=0.0, monkeypatch=monkeypatch)

        assert ended and shots == 4          # primo pronto utile a 2 s, conferma a 3 s


def _controller():
    locator = MagicMock()
    locator.locate.return_value = Result.ok(LocateCandidate(x=540, y=2000, confidence=1.0, strategy_name="profile"))
    locator.is_trusted_strategy.return_value = True
    verifier = MagicMock()
    verifier.wait_until_calm.return_value = Result.ok(CalmResult("p.png", calm=True, waited=False, seconds=0.1))
    verifier.screen_reader.capture_stats = {"calls": 10, "seconds": 11.0}
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    return SpinController(locator, verifier, step_logger, _config()), verifier


class TestDurationHistory:
    def test_spins_with_a_win_never_enter_the_history(self):
        controller, _ = _controller()

        controller._record_duration("Pirots 5", 6.1, had_win=True)

        assert controller._durations.get("Pirots 5", []) == []

    def test_a_long_first_sample_without_a_median_is_ignored(self):
        controller, _ = _controller()

        controller._record_duration("Pirots 5", 10.0)          # sopra i 12 s? no: 10 s e' accettato
        controller._record_duration("Pirots 5", 18.0)          # oltre 12 s con meno di 2 campioni: scartato

        assert controller._durations["Pirots 5"] == [10.0]

    def test_the_typical_duration_is_the_median_of_the_recent_normal_spins(self):
        controller, _ = _controller()
        for seconds in (5.5, 6.1, 4.8):
            controller._record_duration("Pirots 5", seconds)

        assert controller._typical_duration("Pirots 5") == pytest.approx(5.5)

    def test_no_typical_duration_before_two_samples(self):
        controller, _ = _controller()
        controller._record_duration("Pirots 5", 5.5)

        assert controller._typical_duration("Pirots 5") == 0.0

    def test_a_win_spin_is_not_recorded_through_a_real_spin_and_the_typical_duration_reaches_the_verifier(self):
        controller, verifier = _controller()

        def outcome(after, wait):
            return Result.ok(TapOutcome(
                tapped_coords=(540, 2000), verified=True, screenshot_before="a.png", screenshot_after="b.png",
                diff_score=0.4, balance_before=Decimal("10"), balance_after=Decimal(after), bet_before=Decimal("0.2"),
                bet_after=Decimal("0.2"), screen_state_before="idle", screen_state_after="idle", outcome="success",
                state_after={"saldo": 1}, wait_seconds=wait))

        context = SimpleNamespace(slot_ocr_state={"saldo": 10.0, "posta": 0.2}, screenshot_path="x.png",
                                  screen_width=1080, screen_height=2400, profile=None)

        def spin(index):
            return controller.spin("Pirots 5", "Pirots 5", "ep", index, MagicMock(return_value=Result.ok(context)),
                                   True, Decimal("0.2"))

        verifier.verified_tap.return_value = outcome("10.53", 10.0)       # vincita 0,73: non conta
        spin(1)
        assert controller._durations.get("Pirots 5", []) == []
        verifier.verified_tap.return_value = outcome("9.8", 6.1)
        spin(2)
        verifier.verified_tap.return_value = outcome("9.8", 5.5)
        spin(3)
        verifier.verified_tap.return_value = outcome("9.8", 5.5)
        spin(4)

        assert controller._durations["Pirots 5"] == [6.1, 5.5, 5.5]
        # al 4o spin lo storico e' quello degli spin 2 e 3 (6,1 e 5,5): mediana 5,8
        assert verifier.verified_tap.call_args.kwargs["typical_duration"] == pytest.approx(5.8)


def _ocr(states):
    ocr = MagicMock()
    ocr.region = None
    ocr.read_slot_state.side_effect = list(states)
    return ocr


def _vision(*reads):
    vision = MagicMock()
    vision.read.side_effect = [dict(read) for read in reads] + [dict(reads[-1])] * 50
    return vision


class TestBetIsNotRereadEverySpin:
    def _bigbass(self, count):
        # Big Bass: l'OCR legge il saldo ma mai la puntata.
        return [{"saldo": round(99998.0 - 0.1 * i, 2), "posta": None, "raw_texts": []} for i in range(count + 1)]

    def test_the_big_bass_session_makes_a_vision_call_only_every_ten_spins(self):
        vision = _vision({"saldo": 99998.0, "posta": 0.1})
        reader = ResilientOCRReader(_ocr(self._bigbass(25)), vision)
        first = reader.read_slot_state("/first.png")
        reader.set_spin_mode(True)

        results = [reader.read_slot_state(f"/spin{i}.png") for i in range(25)]

        assert first["posta"] == 0.1 and all(state["posta"] == 0.1 for state in results)
        assert vision.read.call_count == 3            # prima lettura + controlli alla 11a e alla 22a lettura

    def test_outside_spin_mode_the_vision_is_still_used_each_time(self):
        vision = _vision({"saldo": 99998.0, "posta": 0.1})
        reader = ResilientOCRReader(_ocr(self._bigbass(5)), vision)
        reader.read_slot_state("/first.png")

        for i in range(4):
            reader.read_slot_state(f"/menu{i}.png")

        assert vision.read.call_count == 5

    def test_a_bet_that_was_never_read_is_not_invented(self):
        vision = _vision({"saldo": 99998.0, "posta": None})
        reader = ResilientOCRReader(_ocr(self._bigbass(3)), vision)
        reader.read_slot_state("/first.png")
        reader.set_spin_mode(True)

        state = reader.read_slot_state("/spin.png")

        assert state["posta"] is None and vision.read.call_count == 2

    def test_when_the_ocr_reads_the_bet_the_normal_path_is_unchanged(self):
        vision = _vision({"saldo": 100.0, "posta": 0.2})
        ocr = _ocr([{"saldo": 100.0, "posta": 0.2, "raw_texts": []}, {"saldo": 99.8, "posta": 0.2, "raw_texts": []}])
        reader = ResilientOCRReader(ocr, vision)
        reader.read_slot_state("/first.png")
        reader.set_spin_mode(True)

        assert reader.read_slot_state("/spin.png")["posta"] == 0.2 and vision.read.call_count == 1

    def test_a_suspicious_balance_forces_a_vision_read(self):
        vision = _vision({"saldo": 99998.0, "posta": 0.1})
        states = self._bigbass(3)
        states[2] = {"saldo": 20000.0, "posta": None, "raw_texts": []}     # salto incoerente col saldo precedente
        reader = ResilientOCRReader(_ocr(states), vision)
        reader.read_slot_state("/first.png")
        reader.set_spin_mode(True)
        reader.read_slot_state("/spin1.png")
        before = vision.read.call_count

        reader.read_slot_state("/spin2.png")

        assert vision.read.call_count == before + 1


class TestProbeAnalysisOnTheRealData:
    PIROTS_1 = [(1.2, 1.0), (2.2, 1.0), (3.2, 0.05), (4.2, 0.0), (5.3, 1.0), (6.4, 0.0), (7.4, 0.0), (8.5, 1.0),
                (9.5, 0.0), (10.6, 0.05), (11.6, 0.99), (12.6, 0.0), (13.7, 0.06), (14.8, 0.97)]
    PIROTS_2 = [(0.9, 1.0), (1.7, 1.0), (2.8, 1.0), (3.9, 0.0), (5.0, 0.0), (6.1, 0.0), (7.2, 0.0), (8.2, 1.0),
                (9.3, 1.0), (10.3, 0.0), (11.4, 1.0), (12.5, 0.0), (13.6, 0.0), (14.6, 1.0)]
    BIG_BASS = [(1.2, 1.0), (2.2, 1.0)] + [(float(t), 0.0) for t in range(3, 15)]

    def test_pirots5_probe_one_shows_a_pulsing_arrow_not_a_long_animation(self):
        summary = spin_button_probe.analyse(self.PIROTS_1)

        assert summary["absent_from"] == 3.2 and summary["back_at"] == 5.3 and summary["duration"] == pytest.approx(1.0)
        assert summary["pulsing"] and summary["pulses"] == 4 and summary["pulse_period"] == pytest.approx(3.17, abs=0.05)

    def test_a_steady_return_is_not_pulsing(self):
        rows = [(1.2, 1.0), (2.2, 0.1), (3.2, 0.0), (4.2, 1.0), (5.2, 1.0), (6.2, 1.0)]

        summary = spin_button_probe.analyse(rows)

        assert not summary["pulsing"] and summary["back_at"] == 4.2 and summary["pulses"] == 1

    def test_big_bass_probe_the_arrow_never_pulses(self):
        rows = [(1.2, 1.0), (2.2, 0.0), (3.2, 1.0), (4.2, 1.0), (5.1, 1.0)]

        summary = spin_button_probe.analyse(rows)

        assert summary["absent_from"] == 2.2 and summary["back_at"] == 3.2 and not summary["pulsing"]

    def test_pirots5_probe_two_pulses_after_a_steady_phase(self):
        summary = spin_button_probe.analyse(self.PIROTS_2)

        assert summary["absent_from"] == 3.9 and summary["back_at"] == 8.2
