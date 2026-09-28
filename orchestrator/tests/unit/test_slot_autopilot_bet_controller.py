from decimal import Decimal
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_autopilot.strategies.profile_strategy import ProfileStrategy
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, TapOutcome
from orchestrator.tests.unit._fake_slot_device import (
    FakeSlotDevice,
    fake_ocr_reader,
    fake_screen_reader,
    fake_touch_injector,
)


def _config(**overrides) -> AutoPilotConfig:
    defaults = dict(
        device_id="dev", slot_name="pirots5", target_played_amount=Decimal("30.00"),
        stable_reads_for_min=3, max_bet_lower_attempts=20,
        frame_diff_poll_interval=0.01, frame_diff_max_wait_seconds=1.0,
    )
    defaults.update(overrides)
    return AutoPilotConfig(**defaults)


def _context_provider(initial_bet=Decimal("1.00")):
    context = LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/x.png",
        screen_width=1080, screen_height=2400, slot_ocr_state={"posta": float(initial_bet)},
    )
    return MagicMock(return_value=Result.ok(context))


def _ok_candidate(strategy_name="ocr_anchor"):
    return Result.ok(LocateCandidate(x=200, y=2200, confidence=0.75, strategy_name=strategy_name))


def _tap_outcome(bet_before, bet_after, verified, outcome):
    return TapOutcome(
        tapped_coords=(200, 2200), verified=verified, screenshot_before="/tmp/a.png",
        screenshot_after="/tmp/b.png", diff_score=0.5,
        balance_before=Decimal("10.00"), balance_after=Decimal("10.00"),
        bet_before=bet_before, bet_after=bet_after,
        screen_state_before="idle", screen_state_after="idle", outcome=outcome,
    )


def _controller(locator, verifier, config=None, step_logger=None):
    step_logger = step_logger or MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    return BetController(locator, verifier, step_logger, config or _config()), step_logger


class TestSetMinBetWithMockedCollaborators:
    def test_converges_to_minimum_after_plateau(self):
        locator = MagicMock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.20"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.20"), Decimal("0.10"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "no_effect")),
        ]
        controller, step_logger = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        bet_result = result.data
        assert bet_result.final_bet == Decimal("0.10")
        assert bet_result.min_bet_verified is True
        assert bet_result.outcome == "min_bet_confirmed"
        assert step_logger.log_step.call_count == 6
        assert locator.save_verified_candidate.call_count == 3

    def test_already_at_minimum_from_the_start(self):
        locator = MagicMock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")
        )
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.outcome == "unchanged_already_min"
        assert result.data.min_bet_verified is True
        assert result.data.final_bet == Decimal("1.00")
        assert result.data.attempts == 3

    def test_bet_rising_aborts_immediately(self):
        locator = MagicMock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("2.00"), False, "unexpected_change")
        )
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "VALUE_ROSE"
        assert result.data.outcome == "aborted"
        assert locator.locate.call_count == 1
        assert verifier.verified_tap.call_count == 1

    def test_hitl_stop_mid_loop_aborts(self):
        locator = MagicMock()
        locator.locate.side_effect = [
            _ok_candidate(), _ok_candidate(),
            Result.fail("operator stopped", "HITL_STOP_REQUESTED"),
        ]
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.20"), True, "success")),
        ]
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "HITL_STOP"
        assert result.data.attempts == 3

    def test_element_not_locatable_on_first_attempt(self):
        locator = MagicMock()
        locator.locate.return_value = Result.fail("nothing found", "ELEMENT_NOT_LOCATABLE")
        verifier = MagicMock()
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "ELEMENT_NOT_LOCATABLE"
        assert result.data.attempts == 1
        verifier.verified_tap.assert_not_called()

    def test_max_attempts_exhausted(self):
        locator = MagicMock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")
        )
        config = _config(max_bet_lower_attempts=3, stable_reads_for_min=100)
        controller, _ = _controller(locator, verifier, config=config)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "MAX_ATTEMPTS"
        assert result.data.attempts == 3


class TestEndToEndWithFakeSlotDevice:
    def test_converges_to_true_minimum(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        ladder = [Decimal("0.50"), Decimal("0.20"), Decimal("0.10")]
        state = {"i": 0}

        def _on_tap(d):
            if state["i"] < len(ladder):
                d.bet = ladder[state["i"]]
                state["i"] += 1

        device.add_element("bet_minus", 200, 2200, _on_tap)
        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=device.width, screen_height=device.height,
            elements={"bet_minus": ElementSpec(
                name="bet_minus", norm_x=200 / device.width, norm_y=2200 / device.height,
            )},
        )
        locator = ElementLocator({"profile": ProfileStrategy(ProviderProfileLoader())})
        verifier = ActionVerifier(
            fake_touch_injector(device), fake_screen_reader(device), fake_ocr_reader(device), _config(),
        )
        controller, _ = _controller(locator, verifier, config=_config())

        def _provider():
            return Result.ok(LocateContext(
                device_id="dev", slot_name="pirots5", screenshot_path=device.screenshot(),
                screen_width=device.width, screen_height=device.height,
                slot_ocr_state=device.ocr_state(), profile=profile,
            ))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _provider)
        assert result.success
        assert result.data.final_bet == Decimal("0.10")
        assert result.data.min_bet_verified is True
        assert result.data.outcome == "min_bet_confirmed"

    def test_dead_zone_profile_coords_produce_false_floor(self, tmp_path):
        """Limite noto e dichiarato nel piano: coordinate del profilo su una
        zona morta non fanno mai scendere la catena a Memory/OCR/GPT/HITL
        perche' localizzare e avere-effetto sono passi distinti per design."""
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        # Nessun elemento registrato in quella posizione: sempre zona morta.
        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=device.width, screen_height=device.height,
            elements={"bet_minus": ElementSpec(name="bet_minus", norm_x=0.01, norm_y=0.01)},
        )
        locator = ElementLocator({"profile": ProfileStrategy(ProviderProfileLoader())})
        verifier = ActionVerifier(
            fake_touch_injector(device), fake_screen_reader(device), fake_ocr_reader(device), _config(),
        )
        controller, _ = _controller(locator, verifier, config=_config())

        def _provider():
            return Result.ok(LocateContext(
                device_id="dev", slot_name="pirots5", screenshot_path=device.screenshot(),
                screen_width=device.width, screen_height=device.height,
                slot_ocr_state=device.ocr_state(), profile=profile,
            ))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _provider)
        assert result.success
        assert result.data.outcome == "unchanged_already_min"
        assert result.data.final_bet == Decimal("1.00")
