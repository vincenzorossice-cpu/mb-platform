from decimal import Decimal
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.spin_controller import SpinController
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_autopilot.verifier import TapOutcome


def _config(**overrides) -> AutoPilotConfig:
    defaults = dict(
        device_id="dev", slot_name="pirots5", target_played_amount=Decimal("30.00"),
        max_spin_attempts_per_step=3, accounting_tolerance=Decimal("0.01"),
    )
    defaults.update(overrides)
    return AutoPilotConfig(**defaults)


def _context_result(balance, win=None, idle=True):
    state = {"saldo": balance}
    if win is not None:
        state["vincita"] = win
    state["spin_button_coords"] = (540, 2000) if idle else None
    context = LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/x.png",
        screen_width=1080, screen_height=2400, slot_ocr_state=state,
    )
    return Result.ok(context)


def _candidate(strategy_name="ocr_anchor", verified=False):
    return Result.ok(LocateCandidate(x=540, y=2000, confidence=0.75, strategy_name=strategy_name, verified=verified))


def _tap_outcome(verified, balance_after=None, win_amount=None):
    return Result.ok(TapOutcome(
        tapped_coords=(540, 2000), verified=verified, screenshot_before="/tmp/a.png",
        screenshot_after="/tmp/b.png", diff_score=0.5,
        balance_before=Decimal("10.00"), balance_after=balance_after,
        bet_before=Decimal("0.10"), bet_after=Decimal("0.10"),
        screen_state_before="idle", screen_state_after="idle",
        outcome="success" if verified else "no_effect",
        win_amount=win_amount,
    ))


def _controller(locator=None, verifier=None, config=None, step_logger=None):
    step_logger = step_logger or MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    locator = locator if locator is not None else MagicMock()
    verifier = verifier if verifier is not None else MagicMock()
    return SpinController(locator, verifier, step_logger, config or _config()), step_logger, locator, verifier


class TestSafetyInvariants:
    def test_min_bet_not_verified_blocks(self):
        controller, step_logger, locator, verifier = _controller()
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, False, Decimal("0.10"))
        assert result.success
        assert result.data.outcome == "blocked"
        assert result.data.violated_invariant == "MIN_BET_NOT_VERIFIED"
        locator.locate.assert_not_called()
        assert step_logger.log_step.call_count == 1

    def test_blocked_invariant_is_logged(self, caplog):
        controller, _, _, _ = _controller()
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        with caplog.at_level("WARNING"):
            controller.spin("dev", "pirots5", "ep1", 1, context_provider, False, Decimal("0.10"))
        messages = [record.getMessage() for record in caplog.records]
        assert any("MIN_BET_NOT_VERIFIED" in m for m in messages)

    def test_insufficient_balance_blocks(self):
        controller, _, locator, _ = _controller()
        context_provider = MagicMock(return_value=_context_result(Decimal("0.05")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.violated_invariant == "INSUFFICIENT_BALANCE"
        locator.locate.assert_not_called()

    def test_unsafe_screen_state_blocks(self):
        controller, _, locator, _ = _controller()
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00"), idle=False))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.violated_invariant == "UNSAFE_SCREEN_STATE"
        locator.locate.assert_not_called()

    def test_icon_only_spin_button_is_not_blocked_when_saldo_and_posta_are_readable(self):
        """Pirots5 ha un bottone spin puramente iconografico: spin_button_coords
        non viene mai valorizzato dall'OCR, ma saldo e puntata si leggono
        entrambi puliti - lo spin non deve essere bloccato in questo caso."""
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.90"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)

        context = LocateContext(
            device_id="dev", slot_name="pirots5", screenshot_path="/tmp/x.png",
            screen_width=1080, screen_height=2400,
            slot_ocr_state={"spin_button_coords": None, "saldo": 10.00, "posta": 0.10},
        )
        context_provider = MagicMock(return_value=Result.ok(context))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.violated_invariant is None
        assert result.data.outcome == "success"
        locator.locate.assert_called_once()


class TestCleanSpins:
    def test_clean_spin_no_win(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.90"))
        controller, step_logger, _, _ = _controller(locator=locator, verifier=verifier)

        context_provider = MagicMock(side_effect=[
            _context_result(Decimal("10.00")), _context_result(Decimal("9.90")),
        ])

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.outcome == "success"
        assert result.data.verified is True
        assert result.data.accounting_anomaly is None
        assert result.data.balance_after == Decimal("9.90")
        assert step_logger.log_step.call_count == 1

    def test_spin_with_win_accounting_checks_out(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(
            True, balance_after=Decimal("10.40"), win_amount=Decimal("0.50"),
        )
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.accounting_anomaly is None
        assert result.data.win_amount == Decimal("0.50")

    def test_accounting_mismatch_beyond_tolerance_is_not_a_failure(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        # Atteso 9.90 (10.00 - 0.10 + 0), osservato 10.40: scarto enorme, ma il
        # tap era verificato -> successo comunque, anomalia solo annotata.
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("10.40"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success  # la Result e' comunque ok
        assert result.data.outcome == "success"
        assert result.data.accounting_anomaly is not None

    def test_accounting_mismatch_within_tolerance_is_not_an_anomaly(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.895"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.accounting_anomaly is None

    def test_win_that_increases_balance_is_success_on_first_attempt(self):
        """Fix 7: una vincita che supera la puntata fa SALIRE il saldo - deve
        contare come spin riuscito al primo tentativo, non un fallimento che
        scatena ritap inutili (e rischiosi) su uno spin gia' riuscito."""
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(
            True, balance_after=Decimal("15.43"), win_amount=Decimal("5.43"),
        )
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.outcome == "success"
        assert result.data.attempts == 1
        verifier.verified_tap.assert_called_once()

    def test_no_extra_context_read_after_a_verified_non_hitl_tap(self):
        """Fix 9: niente terza lettura di contesto per rileggere saldo/vincita
        dopo un tap gia' verificato - i dati vengono da tap_result, gratis."""
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.80"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.balance_after == Decimal("9.80")
        assert context_provider.call_count == 1


class TestLocateAndHitl:
    def test_spin_not_locatable_is_error_not_blocked(self):
        locator = MagicMock()
        locator.locate.return_value = Result.fail("nothing found", "ELEMENT_NOT_LOCATABLE")
        controller, step_logger, _, _ = _controller(locator=locator)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.outcome == "error"
        logged_record = step_logger.log_step.call_args[0][1]
        assert logged_record.error_code == "ELEMENT_NOT_LOCATABLE"

    def test_hitl_preverified_candidate_skips_verifier(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate(strategy_name="hitl", verified=True)
        verifier = MagicMock()
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(side_effect=[
            _context_result(Decimal("10.00")), _context_result(Decimal("9.90")),
        ])

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.outcome == "success"
        assert result.data.verified is True
        verifier.verified_tap.assert_not_called()


class TestContextFailureAndRetries:
    def test_context_provider_failure_propagates_but_logs_once(self):
        controller, step_logger, locator, _ = _controller()
        context_provider = MagicMock(return_value=Result.fail("adb dead", "ADB_TIMEOUT"))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert not result.success
        assert result.error_code == "ADB_TIMEOUT"
        assert step_logger.log_step.call_count == 1
        locator.locate.assert_not_called()

    def test_retry_succeeds_on_second_attempt(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [_tap_outcome(False), _tap_outcome(True)]
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(side_effect=[
            _context_result(Decimal("10.00")), _context_result(Decimal("9.90")),
        ])

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.outcome == "success"
        assert result.data.attempts == 2
        assert verifier.verified_tap.call_count == 2

    def test_all_attempts_fail_is_no_effect(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(False)
        config = _config(max_spin_attempts_per_step=3)
        controller, _, _, _ = _controller(locator=locator, verifier=verifier, config=config)
        context_provider = MagicMock(side_effect=[
            _context_result(Decimal("10.00")), _context_result(Decimal("10.00")),
        ])

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.outcome == "no_effect"
        assert result.data.attempts == 3
        assert verifier.verified_tap.call_count == 3


class TestReuseOfContextForTap:
    def test_first_tap_reuses_the_context_already_read(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.90"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_result = _context_result(Decimal("10.00"))
        context_provider = MagicMock(return_value=context_result)

        controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))

        kwargs = verifier.verified_tap.call_args.kwargs
        assert kwargs["screenshot_before"] == context_result.data.screenshot_path
        assert kwargs["state_before"] == context_result.data.slot_ocr_state

    def test_retap_after_a_failed_attempt_rereads_the_screen(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [_tap_outcome(False), _tap_outcome(True)]
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))

        first, second = verifier.verified_tap.call_args_list
        assert first.kwargs["screenshot_before"] is not None
        assert second.kwargs["screenshot_before"] is None
        assert second.kwargs["state_before"] is None
