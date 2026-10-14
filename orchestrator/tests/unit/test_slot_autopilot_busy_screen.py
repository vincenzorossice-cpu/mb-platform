from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import session as session_module
from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.session import play_session
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_autopilot.supervisor import SlotSupervisor
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, CalmResult

W, H = 400, 900


def _image(path, top="white", bottom="white", spin="white", noise=False):
    """Schermo W x H: rulli in alto (top), striscia in basso (bottom), pulsante spin al centro-basso (spin)."""
    image = Image.new("RGB", (W, H), top)
    image.paste(Image.new("RGB", (W, int(H * 0.12)), bottom), (0, int(H * 0.88)))
    image.paste(Image.new("RGB", (120, 120), spin), (140, 600))
    if noise:
        image.paste(Image.effect_noise((W, 400), 80).convert("RGB"), (0, 50))
    return image


def _reader(tmp_path, frames):
    """screen_reader finto: ogni _screenshot() salva il frame successivo (l'ultimo si ripete)."""
    reader = MagicMock()
    counter = {"n": 0}

    def shot(device_id):
        index = min(counter["n"], len(frames) - 1)
        counter["n"] += 1
        path = tmp_path / f"f{counter['n']}.png"
        frames[index].save(path)
        return Result.ok(str(path))

    reader._screenshot.side_effect = shot
    return reader


def _verifier(reader):
    config = AutoPilotConfig(
        device_id="d", slot_name="s", target_played_amount=Decimal("1"),
        frame_diff_poll_interval=0.001, frame_diff_max_wait_seconds=2.0,
    )
    return ActionVerifier(MagicMock(), reader, MagicMock(), config), config


SPIN_ZONE = (60, 540, 380, 860)
BAR = (0, int(H * 0.88), W, H)


class TestPartialCalm:
    def test_an_overlay_that_blinks_over_the_reels_is_calm_if_the_bar_and_button_stay_still(self, tmp_path):
        frames = [_image(None, noise=(i % 2 == 0)) for i in range(12)]
        reader = _reader(tmp_path, frames)
        verifier, _ = _verifier(reader)
        ref = tmp_path / "ref.png"
        _image(None).save(ref)

        result = verifier.wait_until_calm("d", str(ref), 2.0, regions=[BAR, SPIN_ZONE])

        assert result.success and result.data.calm and result.data.partial

    def test_a_changing_bar_is_not_calm(self, tmp_path):
        frames = [_image(None, noise=(i % 2 == 0), bottom=("white", "black", "gray")[i % 3]) for i in range(40)]
        verifier, _ = _verifier(_reader(tmp_path, frames))
        ref = tmp_path / "ref.png"
        _image(None).save(ref)

        result = verifier.wait_until_calm("d", str(ref), 0.3, regions=[BAR, SPIN_ZONE])

        assert result.success and not result.data.calm

    def test_a_changing_spin_button_is_not_calm(self, tmp_path):
        frames = [_image(None, noise=(i % 2 == 0), spin=("white", "black")[i % 2]) for i in range(40)]
        verifier, _ = _verifier(_reader(tmp_path, frames))
        ref = tmp_path / "ref.png"
        _image(None).save(ref)

        assert not verifier.wait_until_calm("d", str(ref), 0.3, regions=[BAR, SPIN_ZONE]).data.calm

    def test_without_regions_the_blinking_screen_is_never_calm(self, tmp_path):
        frames = [_image(None, noise=(i % 2 == 0)) for i in range(40)]
        verifier, _ = _verifier(_reader(tmp_path, frames))
        ref = tmp_path / "ref.png"
        _image(None).save(ref)

        assert not verifier.wait_until_calm("d", str(ref), 0.3).data.calm

    def test_a_still_screen_is_calm_at_once_and_not_partial(self, tmp_path):
        verifier, _ = _verifier(_reader(tmp_path, [_image(None)]))
        ref = tmp_path / "ref.png"
        _image(None).save(ref)

        result = verifier.wait_until_calm("d", str(ref), 1.0, regions=[BAR, SPIN_ZONE])

        assert result.data.calm and not result.data.waited and not result.data.partial


class TestSpinControllerCalmRegions:
    def test_the_zones_are_the_bottom_strip_and_a_square_around_the_spin_button(self):
        context = LocateContext(device_id="d", slot_name="s", screenshot_path="/x.png", screen_width=1080,
                                screen_height=2400)
        regions = SpinController._calm_regions(context, LocateCandidate(x=518, y=1775, confidence=1, strategy_name="profile"))

        assert regions[0] == (0, 2112, 1080, 2400)
        assert regions[1] == (358, 1615, 678, 1935)

    def test_unknown_screen_size_gives_no_zones(self):
        context = LocateContext(device_id="d", slot_name="s", screenshot_path="/x.png", screen_width=0, screen_height=0)
        assert SpinController._calm_regions(context, LocateCandidate(x=1, y=1, confidence=1, strategy_name="profile")) is None


def _controller():
    config = AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"))
    return SpinController(MagicMock(), MagicMock(), MagicMock(), config)


class TestForcedSpin:
    def test_force_skips_the_screen_state_invariant_but_not_the_others(self):
        controller = _controller()
        unsafe_context = SimpleNamespace(slot_ocr_state={})   # nessun saldo/puntata leggibili: 'unsafe'

        assert controller._check_invariants(True, Decimal("10"), Decimal("0.2"), unsafe_context) == "UNSAFE_SCREEN_STATE"
        assert controller._check_invariants(True, Decimal("10"), Decimal("0.2"), unsafe_context, force=True) is None
        assert controller._check_invariants(False, Decimal("10"), Decimal("0.2"), unsafe_context, force=True) == "MIN_BET_NOT_VERIFIED"
        assert controller._check_invariants(True, Decimal("0.1"), Decimal("0.2"), unsafe_context, force=True) == "INSUFFICIENT_BALANCE"

    def test_a_blocked_spin_says_which_check_blocked(self):
        controller = _controller()
        context = SimpleNamespace(slot_ocr_state={"saldo": 10.0}, screenshot_path=None, screen_width=0, screen_height=0, profile=None)
        provider = MagicMock(return_value=Result.ok(context))

        result = controller.spin("d", "s", "ep", 1, provider, True, Decimal("0.2"))

        assert result.data.outcome == "blocked" and result.data.blocked_detail == "saldo/puntata non leggibili"

    def test_the_autopilot_passes_force_only_when_asked(self):
        # firma storica invariata: nessun kwarg se non forzato
        spin_controller = MagicMock()
        spin_controller.spin.return_value = Result.ok(SpinResult("s", 1, Decimal("0.2"), None, None, None, False, 0, "blocked"))
        autopilot = _autopilot(spin_controller)

        autopilot.spin("d", 1)
        autopilot.spin("d", 2, force=True)

        assert "force" not in spin_controller.spin.call_args_list[0].kwargs
        assert spin_controller.spin.call_args_list[1].kwargs["force"] is True


def _autopilot(spin_controller):
    ocr = MagicMock()
    ocr.region = None
    autopilot = SlotAutoPilot(
        config=AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1")),
        screen_reader=MagicMock(), touch_injector=MagicMock(), ocr_reader=ocr, profile_loader=MagicMock(),
        locator=MagicMock(), bet_controller=MagicMock(), spin_controller=spin_controller, step_logger=MagicMock(),
    )
    return autopilot


def _blocked(detail="schermo non fermo"):
    return Result.ok(SpinResult("s", 1, Decimal("0.2"), None, None, None, False, 0, "blocked",
                                violated_invariant="UNSAFE_SCREEN_STATE", blocked_detail=detail))


def _success(bet="0.20"):
    return Result.ok(SpinResult("s", 1, Decimal(bet), Decimal("10"), Decimal("9.8"), Decimal("0"), True, 1, "success"))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(session_module.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(session_module.time, "sleep", fake.sleep)
    return fake


def _session_autopilot(spins, verdict="busy"):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(SimpleNamespace(min_bet_verified=True, final_bet=Decimal("0.2"),
                                                                    outcome="x", attempts=1, aborted_reason=None))
    autopilot.spin.side_effect = list(spins)
    autopilot.diagnose_busy.return_value = verdict
    autopilot.get_balance_summary.return_value = (None, None)
    autopilot.save_debug_screenshot.return_value = "logs/occupato.png"
    return autopilot


class TestBusyScreenEscalation:
    def test_after_45_seconds_sol_is_asked_and_a_ready_game_gets_one_forced_spin(self, clock):
        # blocchi per ~50 s, poi (forzato) successo
        spins = [_blocked()] * 11 + [_success()] * 3
        autopilot = _session_autopilot(spins, verdict="ready")

        result = play_session(autopilot, "d", "s", Decimal("0.4"), 50)

        assert not result.failed and result.total_played == Decimal("0.40")
        autopilot.diagnose_busy.assert_called_once_with("d")
        forced = [call for call in autopilot.spin.call_args_list if call.kwargs.get("force")]
        assert len(forced) == 1

    def test_a_busy_verdict_keeps_waiting_and_ends_with_the_historical_timeout(self, clock):
        autopilot = _session_autopilot([_blocked()] * 60, verdict="busy")

        result = play_session(autopilot, "d", "s", Decimal("5"), 500)

        assert result.failed and result.stop_reason == "BUSY_TIMEOUT"
        autopilot.diagnose_busy.assert_called_once()                      # una sola domanda a Sol
        assert not [c for c in autopilot.spin.call_args_list if c.kwargs.get("force")]

    def test_a_closed_menu_resumes_without_a_forced_spin(self, clock):
        spins = [_blocked()] * 10 + [_success()] * 3
        autopilot = _session_autopilot(spins, verdict="closed")

        result = play_session(autopilot, "d", "s", Decimal("0.4"), 50)

        assert not result.failed and result.total_played == Decimal("0.40")
        assert not [c for c in autopilot.spin.call_args_list if c.kwargs.get("force")]

    def test_the_busy_message_says_which_check_blocked_and_screenshots_are_saved(self, clock, caplog):
        autopilot = _session_autopilot([_blocked("saldo/puntata non leggibili")] + [_success()] * 3, verdict="busy")

        with caplog.at_level("INFO"):
            play_session(autopilot, "d", "s", Decimal("0.4"), 50, log=session_module.logger)

        assert "saldo/puntata non leggibili" in caplog.text
        autopilot.save_debug_screenshot.assert_any_call("d", "occupato_inizio")

    def test_a_short_busy_wait_never_calls_sol(self, clock):
        autopilot = _session_autopilot([_blocked()] * 3 + [_success()] * 3)

        play_session(autopilot, "d", "s", Decimal("0.4"), 50)

        autopilot.diagnose_busy.assert_not_called()


class TestSupervisorCheckReady:
    def _sup(self, tmp_path, payload, describe=None):
        reader = MagicMock()
        path = tmp_path / "s.png"
        Image.new("RGB", (1080, 2400), "white").save(path)
        reader._screenshot.return_value = Result.ok(str(path))
        gpt = MagicMock()
        gpt._VISION_MAX_TOKENS = 300
        gpt._create_completion.return_value = Result.ok(payload)
        return SlotSupervisor(gpt, reader, MagicMock(), describe=describe, settle_seconds=0)

    def test_a_win_overlay_with_the_spin_button_visible_is_ready(self, tmp_path):
        sup = self._sup(tmp_path, {"screen_state": "win_presentation", "spin_ready": True, "action": "none"})
        assert sup.check_ready("d") == "ready"

    def test_a_feature_in_progress_is_busy(self, tmp_path):
        assert self._sup(tmp_path, {"screen_state": "feature", "spin_ready": False, "action": "none"}).check_ready("d") == "busy"

    def test_ready_flag_with_a_modal_state_is_not_ready(self, tmp_path):
        assert self._sup(tmp_path, {"screen_state": "modal", "spin_ready": True, "action": "none"}).check_ready("d") == "busy"

    def test_missing_flag_is_busy(self, tmp_path):
        assert self._sup(tmp_path, {"screen_state": "idle", "action": "none"}).check_ready("d") == "busy"

    def test_budget_exhausted_is_unknown(self, tmp_path):
        sup = self._sup(tmp_path, {"screen_state": "idle", "spin_ready": True, "action": "none"})
        sup.interventions = sup.max_interventions

        assert sup.check_ready("d") == "unknown"

    def test_the_prompt_asks_for_spin_ready_even_with_an_overlay(self, tmp_path):
        sup = self._sup(tmp_path, {"screen_state": "idle", "spin_ready": True, "action": "none"})
        sup.check_ready("d")

        system = sup.gpt_client._create_completion.call_args.args[0][0]["content"]
        assert "spin_ready" in system and "EVEN IF a win overlay" in system

    def test_without_a_supervisor_the_autopilot_answers_unknown(self):
        autopilot = _autopilot(MagicMock())
        assert autopilot.diagnose_busy("d") == "unknown"


class TestControllerUsesTheZones:
    def _run(self, partial_calm=True, calm=None):
        locator = MagicMock()
        locator.locate.return_value = Result.ok(LocateCandidate(x=518, y=1775, confidence=1.0, strategy_name="profile"))
        locator.is_trusted_strategy.return_value = True
        verifier = MagicMock()
        verifier.wait_until_calm.return_value = Result.ok(calm or CalmResult("p.png", calm=True, waited=False, seconds=0.1))
        verifier.verified_tap.return_value = Result.fail("stop here", "TAP_FAILED")
        config = AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"),
                                 spin_partial_calm=partial_calm)
        controller = SpinController(locator, verifier, MagicMock(), config)
        context = SimpleNamespace(slot_ocr_state={"saldo": 10.0, "posta": 0.2}, screenshot_path="/x.png",
                                  screen_width=1080, screen_height=2400, profile=None)
        controller.spin("d", "s", "ep", 1, MagicMock(return_value=Result.ok(context)), True, Decimal("0.2"))
        return verifier

    def test_the_zones_are_passed_to_the_calm_check(self):
        verifier = self._run()

        assert verifier.wait_until_calm.call_args.kwargs["regions"][0] == (0, 2112, 1080, 2400)

    def test_partial_calm_can_be_switched_off(self):
        verifier = self._run(partial_calm=False)

        assert verifier.wait_until_calm.call_args.kwargs["regions"] is None

    def test_partial_calm_is_logged(self, caplog):
        with caplog.at_level("INFO"):
            self._run(calm=CalmResult("p.png", calm=True, waited=True, seconds=1.0, partial=True))

        assert "calma parziale" in caplog.text


# ---- animazioni ambientali: i soli rulli contano, e prima del primo spin nessun Vigile --------------------------------

class TestReelsOnlyWait:
    REGION = (int(W * 0.05), int(H * 0.22), int(W * 0.95), int(H * 0.68))

    def test_ambient_animations_outside_the_reels_do_not_matter(self, tmp_path):
        frames = [_image(None, bottom=color) for color in ("red", "blue", "green", "black", "white", "red")]
        verifier, _ = _verifier(_reader(tmp_path, frames))

        result = verifier.wait_until_reels_still("d", self.REGION, 2.0)

        assert result.success and result.data.calm and result.data.partial

    def test_moving_reels_are_not_still(self, tmp_path):
        frames = [_image(None, top=color) for color in ("red", "blue", "green", "black") * 300]       # sempre in movimento
        verifier, _ = _verifier(_reader(tmp_path, frames))

        result = verifier.wait_until_reels_still("d", self.REGION, 0.3)

        assert result.success and not result.data.calm


class TestNoWardenBeforeTheFirstSpin:
    @staticmethod
    def _run(monkeypatch, spins):
        from orchestrator.tests.unit.test_slot_autopilot_session import _autopilot
        clock = {"now": 0.0}
        fake_time = SimpleNamespace(
            monotonic=lambda: clock["now"], sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        )
        monkeypatch.setattr(session_module, "time", fake_time)
        autopilot = _autopilot(spins)
        autopilot.warden = SimpleNamespace(handle=MagicMock(return_value="resume"), calls=0)
        result = play_session(autopilot, "dev", "slot", Decimal("0.20"), 80)
        return autopilot, result

    @staticmethod
    def _blocked(count):
        return [Result.ok(SpinResult(
            slot_name="s", spin_index=1, bet_amount=Decimal("0.10"), balance_before=Decimal("100"),
            balance_after=Decimal("100"), win_amount=None, verified=False, attempts=0, outcome="blocked",
            violated_invariant="UNSAFE_SCREEN_STATE", blocked_detail="schermo non fermo")) for _ in range(count)]

    @staticmethod
    def _spin(index):
        return Result.ok(SpinResult(
            slot_name="s", spin_index=index, bet_amount=Decimal("0.10"), balance_before=Decimal("100"),
            balance_after=Decimal("99.9"), win_amount=None, verified=True, attempts=1, outcome="success"))

    def test_busy_screen_with_no_spin_done_never_calls_the_warden(self, monkeypatch):
        spins = self._blocked(12) + [self._spin(1), self._spin(2)]
        autopilot, result = self._run(monkeypatch, spins)

        autopilot.warden.handle.assert_not_called()
        assert not result.failed and result.target_reached

    def test_busy_screen_after_a_spin_calls_the_warden(self, monkeypatch):
        spins = [self._spin(1)] + self._blocked(12) + [self._spin(2)]
        autopilot, result = self._run(monkeypatch, spins)

        autopilot.warden.handle.assert_called()
        assert not result.failed and result.target_reached


class TestRealMotionOnlyCallsTheWarden:
    """E: schermo non fermo ma rulli fermi (jackpot, banner) non e' un bonus: niente Vigile."""
    _run = staticmethod(TestNoWardenBeforeTheFirstSpin._run)
    _spin = staticmethod(TestNoWardenBeforeTheFirstSpin._spin)

    @staticmethod
    def _blocked(count, detail):
        return [Result.ok(SpinResult(
            slot_name="s", spin_index=1, bet_amount=Decimal("0.10"), balance_before=Decimal("100"),
            balance_after=Decimal("100"), win_amount=None, verified=False, attempts=0, outcome="blocked",
            violated_invariant="UNSAFE_SCREEN_STATE", blocked_detail=detail)) for _ in range(count)]

    def _play(self, monkeypatch, spins, relearn, target="0.20"):
        from orchestrator.tests.unit.test_slot_autopilot_session import _autopilot
        clock = {"now": 0.0}
        monkeypatch.setattr(session_module, "time", SimpleNamespace(
            monotonic=lambda: clock["now"], sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds)))
        autopilot = _autopilot(spins)
        autopilot.warden = SimpleNamespace(handle=MagicMock(return_value="resume"), calls=0)
        autopilot.relearn_ambient = MagicMock(return_value=relearn)
        result = play_session(autopilot, "dev", "slot", Decimal(target), 80)
        return autopilot, result

    def test_still_reels_first_relearn_the_ambient_cells_and_never_call_the_warden(self, monkeypatch):
        spins = [self._spin(1)] + self._blocked(1, "schermo non fermo (rulli fermi)") + [self._spin(2)]

        autopilot, result = self._play(monkeypatch, spins, relearn=True)

        assert autopilot.relearn_ambient.call_count == 1
        autopilot.warden.handle.assert_not_called()
        assert result.target_reached and not result.failed

    def test_if_relearning_does_not_help_the_spin_is_forced_without_the_warden(self, monkeypatch):
        spins = [self._spin(1)] + self._blocked(3, "schermo non fermo (rulli fermi)") + [self._spin(2)]

        autopilot, result = self._play(monkeypatch, spins, relearn=False)

        forced = [call.kwargs.get("force") for call in autopilot.spin.call_args_list]
        assert forced[-2:] == [True, True] or True in forced                  # gli spin dopo i blocchi sono forzati
        autopilot.warden.handle.assert_not_called()
        assert result.target_reached

    def test_after_the_forced_spins_are_used_up_the_warden_is_back(self, monkeypatch):
        many = 2 + session_module.MAX_AMBIENT_FORCED + 12
        spins = [self._spin(1)] + self._blocked(many, "schermo non fermo (rulli fermi)") + [self._spin(2)]

        autopilot, result = self._play(monkeypatch, spins, relearn=False)

        autopilot.warden.handle.assert_called()
        assert sum(1 for call in autopilot.spin.call_args_list if call.kwargs.get("force")) \
            == session_module.MAX_AMBIENT_FORCED

    def test_moving_reels_behave_as_before(self, monkeypatch):
        spins = [self._spin(1)] + self._blocked(12, "schermo non fermo (rulli in movimento)") + [self._spin(2)]

        autopilot, result = self._play(monkeypatch, spins, relearn=True)

        autopilot.relearn_ambient.assert_not_called()
        autopilot.warden.handle.assert_called()
        assert not any(call.kwargs.get("force") for call in autopilot.spin.call_args_list)

    def test_a_good_spin_resets_the_forced_counter(self, monkeypatch):
        blocked = self._blocked(1, "schermo non fermo (rulli fermi)")
        spins = [self._spin(1)] + blocked * 2 + [self._spin(2)] + blocked * 2 + [self._spin(3)]

        autopilot, result = self._play(monkeypatch, spins, relearn=False, target="0.30")

        assert sum(1 for call in autopilot.spin.call_args_list if call.kwargs.get("force")) >= 3
        autopilot.warden.handle.assert_not_called()

