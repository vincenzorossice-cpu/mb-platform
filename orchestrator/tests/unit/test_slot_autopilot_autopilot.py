from decimal import Decimal
from unittest.mock import MagicMock

from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile
from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot, learn_amounts_band
from orchestrator.modules.slot_ocr.reader import OCRBox
from orchestrator.modules.slot_autopilot.bet_controller import BetController, BetResult
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.strategies.profile_strategy import ProfileStrategy
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier
from orchestrator.modules.step_logger.logger import JSONLStepLogger
from orchestrator.tests.unit._fake_slot_device import (
    FakeSlotDevice,
    fake_ocr_reader,
    fake_screen_reader,
    fake_touch_injector,
)


def _config(**overrides) -> AutoPilotConfig:
    defaults = dict(
        device_id="dev", slot_name="pirots5", target_played_amount=Decimal("1.00"),
        require_profile=True,
    )
    defaults.update(overrides)
    return AutoPilotConfig(**defaults)


def _collaborators(config=None):
    return dict(
        config=config or _config(),
        screen_reader=MagicMock(), touch_injector=MagicMock(), ocr_reader=MagicMock(),
        profile_loader=MagicMock(), locator=MagicMock(),
        bet_controller=MagicMock(), spin_controller=MagicMock(), step_logger=MagicMock(),
    )


class TestPrepare:
    def test_prepare_loads_profile_and_starts_episode(self):
        deps = _collaborators()
        deps["profile_loader"].load.return_value = Result.ok(
            ProviderProfile(slot_name="pirots5", provider="x", screen_width=1080, screen_height=2400)
        )
        deps["step_logger"].start_episode.return_value = Result.ok("ep-123")
        autopilot = SlotAutoPilot(**deps)

        result = autopilot.prepare("dev")
        assert result.success
        deps["profile_loader"].load.assert_called_once_with("pirots5")
        deps["step_logger"].start_episode.assert_called_once_with("pirots5", "dev")

    def test_prepare_propagates_profile_failure_when_required(self):
        deps = _collaborators(config=_config(require_profile=True))
        deps["profile_loader"].load.return_value = Result.fail("not found", "PROFILE_NOT_FOUND")
        autopilot = SlotAutoPilot(**deps)

        result = autopilot.prepare("dev")
        assert not result.success
        assert result.error_code == "PROFILE_NOT_FOUND"
        deps["step_logger"].start_episode.assert_not_called()

    def test_prepare_degrades_gracefully_when_profile_not_required(self):
        deps = _collaborators(config=_config(require_profile=False))
        deps["profile_loader"].load.return_value = Result.fail("not found", "PROFILE_NOT_FOUND")
        deps["step_logger"].start_episode.return_value = Result.ok("ep-123")
        autopilot = SlotAutoPilot(**deps)

        result = autopilot.prepare("dev")
        assert result.success
        deps["step_logger"].start_episode.assert_called_once()


class TestOcrRegionWiring:
    def _prepare_with(self, profile):
        deps = _collaborators()
        deps["profile_loader"].load.return_value = Result.ok(profile)
        deps["step_logger"].start_episode.return_value = Result.ok("ep-1")
        SlotAutoPilot(**deps).prepare("dev")
        return deps["ocr_reader"]

    def test_verified_profile_with_canvas_limits_the_ocr(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=1080, screen_height=2400,
            verified=True, canvas_bbox=(0, 247, 1080, 2400),
        )
        ocr_reader = self._prepare_with(profile)
        ocr_reader.set_region.assert_called_once_with((0, 247, 1080, 2400))

    def test_unverified_profile_does_not_limit_the_ocr(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=1080, screen_height=2400,
            verified=False, canvas_bbox=(0, 247, 1080, 2400),
        )
        self._prepare_with(profile).set_region.assert_not_called()

    def test_profile_without_canvas_does_not_limit_the_ocr(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=1080, screen_height=2400, verified=True,
        )
        self._prepare_with(profile).set_region.assert_not_called()


class TestBalanceSummary:
    def _png(self, tmp_path):
        path = str(tmp_path / "shot.png")
        Image.new("RGB", (1080, 2400), color="black").save(path)
        return path

    def test_prepare_captures_initial_balance(self, tmp_path):
        deps = _collaborators()
        screenshot_path = self._png(tmp_path)
        deps["screen_reader"]._screenshot.return_value = Result.ok(screenshot_path)
        deps["ocr_reader"].read_slot_state.return_value = {"posta": 1.0, "saldo": 10.0, "raw_texts": []}
        deps["ocr_reader"].read_text_boxes.return_value = Result.ok([])
        deps["profile_loader"].load.return_value = Result.ok(
            ProviderProfile(slot_name="pirots5", provider="x", screen_width=1080, screen_height=2400)
        )
        deps["step_logger"].start_episode.return_value = Result.ok("ep-123")
        autopilot = SlotAutoPilot(**deps)

        assert autopilot.prepare("dev").success
        initial, last = autopilot.get_balance_summary()
        assert initial == Decimal("10.0")
        assert last == Decimal("10.0")

    def test_spin_updates_last_known_balance_at_no_extra_cost(self, tmp_path):
        deps = _collaborators()
        screenshot_path = self._png(tmp_path)
        deps["screen_reader"]._screenshot.return_value = Result.ok(screenshot_path)
        deps["ocr_reader"].read_slot_state.return_value = {"posta": 0.10, "saldo": 10.0, "raw_texts": []}
        deps["ocr_reader"].read_text_boxes.return_value = Result.ok([])
        deps["profile_loader"].load.return_value = Result.ok(
            ProviderProfile(slot_name="pirots5", provider="x", screen_width=1080, screen_height=2400)
        )
        deps["step_logger"].start_episode.return_value = Result.ok("ep-123")
        deps["spin_controller"].spin.return_value = Result.ok(
            MagicMock(outcome="success", bet_amount=Decimal("0.10"), balance_after=Decimal("9.90"))
        )
        autopilot = SlotAutoPilot(**deps)

        autopilot.prepare("dev")
        autopilot.spin("dev", 1)

        initial, last = autopilot.get_balance_summary()
        assert initial == Decimal("10.0")
        assert last == Decimal("9.90")

    def test_balance_summary_none_when_never_prepared(self):
        deps = _collaborators()
        autopilot = SlotAutoPilot(**deps)
        assert autopilot.get_balance_summary() == (None, None)


class TestSetMinBetAndSpinWiring:
    def _png(self, tmp_path):
        path = str(tmp_path / "shot.png")
        Image.new("RGB", (1080, 2400), color="black").save(path)
        return path

    def test_context_rebuilt_fresh_on_every_call(self, tmp_path):
        deps = _collaborators()
        screenshot_path = self._png(tmp_path)
        deps["screen_reader"]._screenshot.return_value = Result.ok(screenshot_path)
        deps["ocr_reader"].read_slot_state.return_value = {"posta": 1.0, "saldo": 10.0, "raw_texts": []}
        deps["ocr_reader"].read_text_boxes.return_value = Result.ok([])

        def _calls_context_twice(device_id, slot_name, episode_id, context_provider):
            context_provider()
            context_provider()
            return Result.ok(BetResult(
                slot_name=slot_name, starting_bet=Decimal("1.00"), final_bet=Decimal("0.10"),
                min_bet_verified=True, attempts=2, outcome="min_bet_confirmed",
            ))

        deps["bet_controller"].set_min_bet.side_effect = _calls_context_twice
        autopilot = SlotAutoPilot(**deps)

        result = autopilot.set_min_bet("dev")
        assert result.success
        assert deps["screen_reader"]._screenshot.call_count == 2

    def test_spin_forwards_last_known_min_bet_state(self, tmp_path):
        deps = _collaborators()
        screenshot_path = self._png(tmp_path)
        deps["screen_reader"]._screenshot.return_value = Result.ok(screenshot_path)
        deps["ocr_reader"].read_slot_state.return_value = {"posta": 0.10, "saldo": 10.0, "raw_texts": []}
        deps["ocr_reader"].read_text_boxes.return_value = Result.ok([])
        deps["bet_controller"].set_min_bet.return_value = Result.ok(BetResult(
            slot_name="pirots5", starting_bet=Decimal("1.00"), final_bet=Decimal("0.10"),
            min_bet_verified=True, attempts=3, outcome="min_bet_confirmed",
        ))
        deps["spin_controller"].spin.return_value = Result.ok(MagicMock(outcome="blocked", bet_amount=None))
        autopilot = SlotAutoPilot(**deps)

        autopilot.set_min_bet("dev")
        autopilot.spin("dev", 1)

        call_args = deps["spin_controller"].spin.call_args[0]
        # (device_id, slot_name, episode_id, spin_index, context_provider, min_bet_verified, expected_bet)
        assert call_args[5] is True
        assert call_args[6] == Decimal("0.10")

    def test_spin_before_set_min_bet_is_blocked(self, tmp_path):
        deps = _collaborators()
        screenshot_path = self._png(tmp_path)
        deps["screen_reader"]._screenshot.return_value = Result.ok(screenshot_path)
        deps["ocr_reader"].read_slot_state.return_value = {"posta": 1.0, "saldo": 10.0, "raw_texts": []}
        deps["ocr_reader"].read_text_boxes.return_value = Result.ok([])
        deps["spin_controller"].spin.return_value = Result.ok(MagicMock(outcome="blocked", bet_amount=None))
        autopilot = SlotAutoPilot(**deps)

        autopilot.spin("dev", 1)
        call_args = deps["spin_controller"].spin.call_args[0]
        assert call_args[5] is False
        assert call_args[6] is None


class TestShutdown:
    def test_shutdown_uses_accumulated_counters(self, tmp_path):
        deps = _collaborators()
        deps["step_logger"].end_episode.return_value = Result.ok(True)
        autopilot = SlotAutoPilot(**deps)
        autopilot._episode_id = "ep-1"
        autopilot._total_spins = 3
        autopilot._total_played = Decimal("0.30")

        result = autopilot.shutdown("dev")
        assert result.success
        summary = deps["step_logger"].end_episode.call_args[0][1]
        assert summary.total_spins == 3
        assert summary.total_played == "0.30"

    def test_shutdown_twice_is_idempotent(self, tmp_path):
        deps = _collaborators()
        deps["step_logger"].end_episode.return_value = Result.ok(True)
        autopilot = SlotAutoPilot(**deps)
        autopilot._episode_id = "ep-1"

        first = autopilot.shutdown("dev")
        second = autopilot.shutdown("dev")
        assert first.success
        assert second.success


class TestUnexpectedExceptions:
    def test_prepare_catches_unexpected_exception(self):
        deps = _collaborators()
        deps["profile_loader"].load.side_effect = RuntimeError("boom")
        autopilot = SlotAutoPilot(**deps)
        result = autopilot.prepare("dev")
        assert not result.success
        assert result.error_code == "UNEXPECTED_ERROR"

    def test_set_min_bet_catches_unexpected_exception(self):
        deps = _collaborators()
        deps["bet_controller"].set_min_bet.side_effect = RuntimeError("boom")
        autopilot = SlotAutoPilot(**deps)
        result = autopilot.set_min_bet("dev")
        assert not result.success
        assert result.error_code == "UNEXPECTED_ERROR"

    def test_spin_catches_unexpected_exception(self):
        deps = _collaborators()
        deps["spin_controller"].spin.side_effect = RuntimeError("boom")
        autopilot = SlotAutoPilot(**deps)
        result = autopilot.spin("dev", 1)
        assert not result.success
        assert result.error_code == "UNEXPECTED_ERROR"


class TestEndToEndSmoke:
    def test_full_flow_with_fake_slot_device_and_real_step_logger(self, tmp_path):
        device = FakeSlotDevice(tmp_path / "frames", balance=Decimal("10.00"), bet=Decimal("1.00"))
        ladder = [Decimal("0.50"), Decimal("0.20"), Decimal("0.10")]
        state = {"i": 0}

        def _bet_minus_tap(d):
            if state["i"] < len(ladder):
                d.bet = ladder[state["i"]]
                state["i"] += 1

        def _spin_tap(d):
            d.balance = d.balance - d.bet

        device.add_element("bet_minus", 200, 2200, _bet_minus_tap)
        device.add_element("spin", 540, 2000, _spin_tap)

        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=device.width, screen_height=device.height,
            elements={
                "bet_minus": ElementSpec(name="bet_minus", norm_x=200 / device.width, norm_y=2200 / device.height),
                "spin": ElementSpec(name="spin", norm_x=540 / device.width, norm_y=2000 / device.height),
            },
        )
        profile_loader = MagicMock()
        profile_loader.load.return_value = Result.ok(profile)

        config = _config(
            target_played_amount=Decimal("1.00"), stable_reads_for_min=3, max_bet_lower_attempts=20,
            frame_diff_poll_interval=0.01, frame_diff_max_wait_seconds=1.0,
            dataset_dir=str(tmp_path / "dataset"),
        )
        locator = ElementLocator({"profile": ProfileStrategy(ProviderProfileLoader())})
        verifier = ActionVerifier(
            fake_touch_injector(device), fake_screen_reader(device), fake_ocr_reader(device), config,
        )
        step_logger = JSONLStepLogger(dataset_dir=config.dataset_dir)
        bet_controller = BetController(locator, verifier, step_logger, config)
        spin_controller = SpinController(locator, verifier, step_logger, config)

        autopilot = SlotAutoPilot(
            config=config, screen_reader=fake_screen_reader(device),
            touch_injector=fake_touch_injector(device), ocr_reader=fake_ocr_reader(device),
            profile_loader=profile_loader, locator=locator, bet_controller=bet_controller,
            spin_controller=spin_controller, step_logger=step_logger,
        )

        assert autopilot.prepare("dev").success
        bet_result = autopilot.set_min_bet("dev")
        assert bet_result.success
        assert bet_result.data.final_bet == Decimal("0.10")

        for i in range(1, 4):
            spin_result = autopilot.spin("dev", i)
            assert spin_result.success

        initial, last = autopilot.get_balance_summary()
        assert initial == Decimal("10.00")
        assert last == device.balance

        assert autopilot.shutdown("dev").success

        episode_dir = tmp_path / "dataset" / "pirots5"
        jsonl_files = list(episode_dir.glob("*.jsonl"))
        summary_files = list(episode_dir.glob("*.summary.json"))
        assert len(jsonl_files) == 1
        assert len(summary_files) == 1
        with open(jsonl_files[0]) as f:
            lines = f.readlines()
        assert len(lines) >= 4


class TestProfileEvidence:
    def _autopilot(self, profile, builder):
        deps = _collaborators()
        deps["profile_loader"].load.return_value = (
            Result.ok(profile) if profile is not None else Result.fail("nf", "PROFILE_NOT_FOUND")
        )
        deps["config"] = _config(require_profile=False)
        deps["step_logger"].start_episode.return_value = Result.ok("ep-1")
        deps["step_logger"].end_episode.return_value = Result.ok(True)
        autopilot = SlotAutoPilot(**deps, profile_builder=builder)
        autopilot._screen_size = (1080, 2400)
        autopilot.prepare("dev")
        return autopilot

    def _spin_result(self, outcome="success", strategy="memory"):
        return Result.ok(SpinResult(
            slot_name="pirots5", spin_index=1, bet_amount=Decimal("0.20"), balance_before=None,
            balance_after=None, win_amount=None, verified=outcome == "success", attempts=1,
            outcome=outcome, strategy_name=strategy,
        ))

    def test_builder_gets_evidence_when_slot_has_no_profile(self):
        builder = MagicMock()
        builder.record_session.return_value = Result.ok("draft_written")
        autopilot = self._autopilot(None, builder)
        autopilot._min_bet_verified, autopilot._current_bet = True, Decimal("0.20")
        autopilot.spin_controller.spin.return_value = self._spin_result(strategy="gpt")
        autopilot.spin("dev", 1)

        autopilot.shutdown("dev")

        evidence = builder.record_session.call_args.args[0]
        assert evidence.min_bet_verified and evidence.spins_ok == 1
        assert evidence.assisted and evidence.no_effect_spins == 0

    def test_unassisted_session_is_reported_as_clean(self):
        builder = MagicMock()
        builder.record_session.return_value = Result.ok("draft_written")
        autopilot = self._autopilot(None, builder)
        autopilot.spin_controller.spin.return_value = self._spin_result(strategy="memory")
        autopilot.spin("dev", 1)
        autopilot.spin_controller.spin.return_value = self._spin_result(outcome="no_effect", strategy="memory")
        autopilot.spin("dev", 2)

        autopilot.shutdown("dev")

        evidence = builder.record_session.call_args.args[0]
        assert not evidence.assisted and evidence.no_effect_spins == 1

    def test_curated_profile_is_never_sent_to_the_builder(self):
        builder = MagicMock()
        profile = ProviderProfile(slot_name="pirots5", provider="x", screen_width=1080, screen_height=2400, verified=True)
        self._autopilot(profile, builder).shutdown("dev")
        builder.record_session.assert_not_called()

    def test_unverified_auto_profile_is_sent_to_the_builder(self):
        builder = MagicMock()
        builder.record_session.return_value = Result.ok("draft_written")
        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=1080, screen_height=2400, auto_generated=True,
        )
        self._autopilot(profile, builder).shutdown("dev")
        builder.record_session.assert_called_once()

    def test_builder_failure_never_breaks_shutdown(self):
        builder = MagicMock()
        builder.record_session.side_effect = RuntimeError("disk full")
        assert self._autopilot(None, builder).shutdown("dev").success


def _box(text, x, y):
    return OCRBox(text=text, bbox=(x - 40, y - 15, x + 40, y + 15), confidence=0.9)


BAR = [_box("CREDITO", 150, 2200), _box("€99.998,00", 350, 2200), _box("PUNTATA", 600, 2200), _box("€0,10", 800, 2200)]
STATE = {"saldo": 99998.0, "posta": 0.1, "raw_texts": []}
CANVAS = (0, 247, 1080, 2400)


class TestLearnAmountsBand:
    def test_band_covers_labels_and_values_on_their_rows_with_margin(self):
        assert learn_amounts_band(BAR, STATE, CANVAS, (1080, 2400)) == (0, 2145, 1080, 2255)

    def test_band_is_clamped_to_the_canvas(self):
        low = [_box("SALDO", 100, 260), _box("POSTA", 500, 262), _box("10,00", 300, 260)]
        band = learn_amounts_band(low, STATE, CANVAS, (1080, 2400))
        assert band[1] == 247

    def test_no_band_without_readable_amounts_or_labels(self):
        assert learn_amounts_band(BAR, {"saldo": None, "posta": 0.1}, CANVAS, (1080, 2400)) is None
        assert learn_amounts_band([_box("SPIN", 500, 2000)], STATE, CANVAS, (1080, 2400)) is None

    def test_scattered_labels_give_no_band(self):
        scattered = BAR + [_box("DEMO", 500, 400)]
        assert learn_amounts_band(scattered, STATE, CANVAS, (1080, 2400)) is None

    def test_without_a_canvas_the_whole_screen_is_the_limit(self):
        assert learn_amounts_band(BAR, STATE, None, (1080, 2400)) == (0, 2145, 1080, 2255)


class _Screens:
    """Schermo finto: un PNG reale per ogni screenshot, OCR che dipende dalla regione."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.shots = 0
        self.regions_used = []
        self.region = None
        self.band_state = dict(STATE)
        self.full_state = dict(STATE)

    def screenshot(self, device_id):
        self.shots += 1
        path = str(self.tmp_path / f"s{self.shots}.png")
        Image.new("RGB", (1080, 2400)).save(path)
        return Result.ok(path)

    def set_region(self, region):
        self.region = region

    def read_slot_state(self, path):
        self.regions_used.append(self.region)
        return dict(self.full_state if self.region in (None, CANVAS) else self.band_state)

    def read_text_boxes(self, path):
        return Result.ok(list(BAR))


def _pilot(tmp_path, spin_outcomes, config=None, profile=None):
    screens = _Screens(tmp_path)
    deps = _collaborators(config=config or _config(require_profile=False))
    deps["profile_loader"].load.return_value = (
        Result.ok(profile) if profile is not None else Result.fail("nf", "PROFILE_NOT_FOUND")
    )
    deps["step_logger"].start_episode.return_value = Result.ok("ep")
    deps["screen_reader"]._screenshot.side_effect = screens.screenshot
    ocr = deps["ocr_reader"]
    type(ocr).region = property(lambda self: screens.region)
    ocr.set_region.side_effect = screens.set_region
    ocr.read_slot_state.side_effect = screens.read_slot_state
    ocr.read_text_boxes.side_effect = screens.read_text_boxes
    contexts = []

    def fake_spin(device_id, slot, episode, index, provider, min_ok, bet):
        contexts.append(provider())
        outcome, strategy = spin_outcomes[index - 1]
        return Result.ok(SpinResult(
            slot_name=slot, spin_index=index, bet_amount=Decimal("0.10"), balance_before=None,
            balance_after=Decimal("99997.9"), win_amount=None, verified=outcome == "success", attempts=1,
            outcome=outcome, strategy_name=strategy, screenshot_after="/tmp/after.png" if outcome == "success" else None,
            state_after=dict(STATE) if outcome == "success" else None,
        ))

    deps["spin_controller"].spin.side_effect = fake_spin
    autopilot = SlotAutoPilot(**deps)
    autopilot.prepare("dev")
    autopilot._min_bet_verified, autopilot._current_bet = True, Decimal("0.10")
    return autopilot, screens, contexts


class TestFastSpinCycle:
    def test_prepare_learns_the_band(self, tmp_path):
        autopilot, _, _ = _pilot(tmp_path, [])
        assert autopilot._amounts_band == (0, 2145, 1080, 2255)

    def test_the_ocr_works_on_the_band_during_a_spin_and_the_region_is_restored(self, tmp_path):
        autopilot, screens, _ = _pilot(tmp_path, [("no_effect", "memory")])
        screens.region = CANVAS
        seen = []
        original = autopilot.spin_controller.spin.side_effect

        def peek(*args):
            seen.append(screens.region)
            return original(*args)

        autopilot.spin_controller.spin.side_effect = peek
        autopilot.spin("dev", 1)

        assert seen == [(0, 2145, 1080, 2255)] and screens.region == CANVAS

    def test_the_region_is_restored_even_when_the_spin_raises(self, tmp_path):
        autopilot, screens, _ = _pilot(tmp_path, [])
        screens.region = CANVAS
        autopilot.spin_controller.spin.side_effect = RuntimeError("boom")

        result = autopilot.spin("dev", 1)

        assert not result.success and screens.region == CANVAS

    def test_a_verified_trusted_spin_provides_the_next_context_without_a_new_reading(self, tmp_path):
        autopilot, screens, contexts = _pilot(tmp_path, [("success", "memory"), ("success", "memory")])
        autopilot.spin("dev", 1)
        shots_before = screens.shots

        autopilot.spin("dev", 2)

        assert screens.shots == shots_before                 # nessuno screenshot/OCR a inizio spin
        assert contexts[1].data.screenshot_path == "/tmp/after.png"
        assert contexts[1].data.slot_ocr_state["saldo"] == 99998.0

    def test_no_reuse_after_no_effect_or_an_untrusted_strategy(self, tmp_path):
        for outcome, strategy in (("no_effect", "memory"), ("success", "gpt")):
            autopilot, screens, contexts = _pilot(tmp_path, [(outcome, strategy), ("success", "memory")])
            autopilot.spin("dev", 1)
            shots_before = screens.shots

            autopilot.spin("dev", 2)

            assert screens.shots > shots_before
            assert contexts[1].data.screenshot_path != "/tmp/after.png"

    def test_a_full_reading_is_forced_every_n_spins(self, tmp_path):
        outcomes = [("success", "memory")] * 4
        autopilot, screens, contexts = _pilot(
            tmp_path, outcomes, config=_config(require_profile=False, full_read_every_spins=2),
        )
        screens.region = CANVAS
        for index in (1, 2, 3):
            autopilot.spin("dev", index)

        # il terzo spin parte da una lettura completa (regione = canvas), non dal contesto riusato
        assert CANVAS in screens.regions_used[-1:]
        assert contexts[2].data.screenshot_path != "/tmp/after.png"

    def test_band_reading_without_amounts_falls_back_to_one_full_reading(self, tmp_path):
        autopilot, screens, contexts = _pilot(tmp_path, [("no_effect", "gpt"), ("success", "memory")])
        screens.region = CANVAS
        screens.band_state = {"saldo": None, "posta": None, "raw_texts": []}
        autopilot.spin("dev", 1)

        autopilot.spin("dev", 2)

        assert contexts[1].data.slot_ocr_state["saldo"] == 99998.0     # dalla lettura completa
        assert screens.region == CANVAS

    def test_without_a_band_nothing_changes(self, tmp_path):
        autopilot, screens, contexts = _pilot(tmp_path, [("no_effect", "memory")])
        autopilot._amounts_band = None
        screens.region = CANVAS
        autopilot.spin("dev", 1)

        assert screens.region == CANVAS and contexts[0].success
