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
        spin_calm_check=False,   # ogni test lo accende se serve (verifier finto: non c'e' uno schermo vero)
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

    def test_a_stale_ocr_win_is_ignored_in_favour_of_the_balance(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        # La scritta "ultima vincita" dice 0.05 ma il saldo e' salito a 10.40: vale il saldo (vincita 0.50).
        verifier.verified_tap.return_value = _tap_outcome(
            True, balance_after=Decimal("10.40"), win_amount=Decimal("0.05"),
        )
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.outcome == "success"
        assert result.data.win_amount == Decimal("0.50")
        assert result.data.accounting_anomaly is None

    def test_a_win_label_that_persists_does_not_invent_wins_or_anomalies(self):
        # Sette spin di fila con la stessa "ultima vincita" (1.0) a schermo e il saldo che scende di una puntata.
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        results = []
        balance = Decimal("100.00")
        for index in range(7):
            verifier = MagicMock()
            verifier.verified_tap.return_value = _tap_outcome(
                True, balance_after=balance - Decimal("0.10"), win_amount=Decimal("1.0"),
            )
            controller, _, _, _ = _controller(locator=locator, verifier=verifier)
            results.append(controller.spin(
                "dev", "pirots5", "ep", index + 1, MagicMock(return_value=_context_result(balance)),
                True, Decimal("0.10"),
            ).data)
            balance -= Decimal("0.10")

        assert all(r.win_amount is None and r.accounting_anomaly is None for r in results)

    def test_the_read_win_is_the_fallback_when_a_balance_is_missing(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=None, win_amount=Decimal("0.50"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)

        result = controller.spin("dev", "pirots5", "ep1", 1,
                                 MagicMock(return_value=_context_result(Decimal("10.00"))), True, Decimal("0.10"))

        assert result.data.win_amount == Decimal("0.50")

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
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.90"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        context_provider = MagicMock(return_value=_context_result(Decimal("10.00")))

        result = controller.spin("dev", "pirots5", "ep1", 1, context_provider, True, Decimal("0.10"))
        assert result.success
        assert result.data.balance_after == Decimal("9.90")
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


class TestSpinIsRemembered:
    def _run(self, strategy_name):
        controller, _, locator, verifier = _controller()
        locator.locate.return_value = _candidate(strategy_name)
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.90"))
        result = controller.spin(
            "dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal("10.00"))),
            True, Decimal("0.10"),
        )
        return result, locator

    def test_verified_spin_is_offered_to_memory_and_strategy_is_reported(self):
        result, locator = self._run("gpt")
        assert result.data.strategy_name == "gpt"
        locator.save_verified_candidate.assert_called_once()
        assert locator.save_verified_candidate.call_args.args[0] == "spin"

    def test_unverified_spin_is_not_remembered(self):
        controller, _, locator, verifier = _controller()
        locator.locate.return_value = _candidate("gpt")
        verifier.verified_tap.return_value = _tap_outcome(False)
        controller.spin(
            "dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal("10.00"))),
            True, Decimal("0.10"),
        )
        locator.save_verified_candidate.assert_not_called()


class TestNoEffectSpinFromGuessedCoordinate:
    def _run(self, trusted):
        locator = MagicMock()
        locator.locate.return_value = _candidate("gpt")
        locator.is_trusted_strategy.return_value = trusted
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(False)
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        result = controller.spin(
            "dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal("10.00"))),
            True, Decimal("0.10"),
        )
        return result, locator, verifier

    def test_untrusted_point_is_rejected_counted_and_not_retapped(self):
        result, locator, verifier = self._run(trusted=False)

        assert result.data.outcome == "no_effect" and result.data.attempts == 1
        assert verifier.verified_tap.call_count == 1
        locator.reject_position.assert_called_once_with("spin", 540, 2000)
        locator.record_failure.assert_called_once_with("gpt", "spin")

    def test_trusted_source_keeps_its_retries(self):
        result, locator, verifier = self._run(trusted=True)

        assert result.data.attempts == 3 and verifier.verified_tap.call_count == 3
        locator.reject_position.assert_not_called()

    def test_a_verified_spin_resets_the_failure_count(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate("gpt")
        locator.is_trusted_strategy.return_value = False
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.90"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)

        controller.spin("dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal("10.00"))),
                        True, Decimal("0.10"))

        locator.record_success.assert_called_once_with("gpt", "spin")


class TestInferredWin:
    def _spin(self, balance_after, win_amount=None, bet="0.10", balance_before="10.00"):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(
            True, balance_after=Decimal(balance_after), win_amount=win_amount,
        )
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        return controller.spin(
            "dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal(balance_before))),
            True, Decimal(bet),
        ).data

    def test_unread_win_is_inferred_from_the_balance(self):
        result = self._spin("10.40")          # 10.00 -0.10 +0.50

        assert result.win_amount == Decimal("0.50")
        assert result.accounting_anomaly is None

    def test_no_win_when_the_balance_only_drops_by_the_bet(self):
        assert self._spin("9.90").win_amount is None

    def test_a_win_read_by_the_ocr_is_kept_and_still_cross_checked(self):
        result = self._spin("9.95", win_amount=Decimal("0.05"))

        assert result.win_amount == Decimal("0.05") and result.accounting_anomaly is None

    def test_sub_cent_noise_is_not_a_win(self):
        assert self._spin("9.905").win_amount is None

    def test_result_exposes_the_screen_after_the_spin_for_reuse(self):
        result = self._spin("9.90")

        assert result.screenshot_after == "/tmp/b.png"


class TestTimingBreakdownInTheLog:
    def test_each_spin_logs_how_long_the_context_and_the_tap_took(self, caplog):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("9.90"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)

        with caplog.at_level("INFO"):
            controller.spin("dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal("10.00"))),
                            True, Decimal("0.10"))

        line = next(r.getMessage() for r in caplog.records if "outcome=success" in r.getMessage())
        assert "contesto" in line and "tocco+attesa+lettura" in line

    def test_a_busy_screen_block_is_logged_quietly_other_blocks_as_warnings(self, caplog):
        controller, _, _, _ = _controller()
        with caplog.at_level("INFO"):
            controller.spin("dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal("10.00"), idle=False)),
                            True, Decimal("0.10"))
            controller.spin("dev", "pirots5", "ep", 2, MagicMock(return_value=_context_result(Decimal("0.01"))),
                            True, Decimal("0.10"))

        by_level = {r.levelname for r in caplog.records if "bloccato" in r.getMessage()}
        assert by_level == {"INFO", "WARNING"}


class TestBetUnderstated:
    def _spin(self, balance_after, bet="0.10", balance_before="100.00", win=None):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal(balance_after), win_amount=win)
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        # Prima lettura: il saldo "prima"; ogni lettura successiva (la rilettura) mostra il saldo "dopo".
        provider = MagicMock(side_effect=[_context_result(Decimal(balance_before))]
                             + [_context_result(Decimal(balance_after))] * 3)
        return controller.spin("dev", "pirots5", "ep", 1, provider, True, Decimal(bet)).data

    def test_a_misread_balance_is_corrected_by_a_second_reading(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("0.99"))   # lettura sbagliata
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)
        provider = MagicMock(side_effect=[_context_result(Decimal("99.99")), _context_result(Decimal("99.89"))])

        result = controller.spin("dev", "pirots5", "ep", 1, provider, True, Decimal("0.10")).data

        assert not result.bet_understated
        assert result.balance_after == Decimal("99.89")
        assert result.win_amount == Decimal("0.00") or result.win_amount is None

    def test_an_alarm_confirmed_by_the_second_reading_still_stops(self):
        assert self._spin("90.00").bet_understated

    def test_a_balance_drop_far_above_the_believed_bet_is_flagged(self):
        assert self._spin("90.00").bet_understated          # calo 10.00 con puntata creduta 0.10

    def test_a_normal_drop_or_a_win_is_not_flagged(self):
        assert not self._spin("99.90").bet_understated
        assert not self._spin("100.40").bet_understated
        assert not self._spin("99.95", win=Decimal("0.05")).bet_understated      # perso 0.10, vinto 0.05

    def test_the_limit_is_the_bet_plus_a_quarter_of_margin(self):
        # puntata creduta 0.10: limite 0.10 + 0.01 (tolleranza) + 0.025 (margine) = 0.135
        assert not self._spin("99.87").bet_understated       # calo 0.13
        assert self._spin("99.86").bet_understated           # calo 0.14
        assert self._spin("99.80").bet_understated           # 2 x 0.10: prima passava inosservato

    def test_unreadable_balance_is_not_flagged(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=None)
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)

        result = controller.spin("dev", "pirots5", "ep", 1,
                                 MagicMock(return_value=_context_result(Decimal("100.00"))), True, Decimal("0.10"))

        assert not result.data.bet_understated


from orchestrator.modules.slot_autopilot.verifier import CalmResult


def _calm(calm=True, waited=False, path="/tmp/calm.png"):
    return Result.ok(CalmResult(screenshot_path=path, calm=calm, waited=waited, seconds=0.5))


def _unexpected(balance_before="4999.80", balance_after="4999.80"):
    return Result.ok(TapOutcome(
        tapped_coords=(540, 2000), verified=False, screenshot_before="/tmp/a.png", screenshot_after="/tmp/b.png",
        diff_score=0.145, balance_before=Decimal(balance_before), balance_after=Decimal(balance_after),
        bet_before=Decimal("0.20"), bet_after=Decimal("0.20"), screen_state_before="idle",
        screen_state_after="idle", outcome="unexpected_change",
    ))


class TestCalmBeforeTapping:
    def _spin(self, calm_results, tap_results, contexts, strategy="profile", expected_bet="0.20"):
        locator = MagicMock()
        locator.locate.return_value = _candidate(strategy)
        locator.is_trusted_strategy.return_value = strategy in ("profile", "memory")
        verifier = MagicMock()
        verifier.wait_until_calm.side_effect = list(calm_results)
        verifier.verified_tap.side_effect = list(tap_results)
        controller, _, _, _ = _controller(
            locator=locator, verifier=verifier, config=_config(spin_calm_check=True, max_spin_attempts_per_step=3),
        )
        provider = MagicMock(side_effect=list(contexts))
        result = controller.spin("dev", "pirots5", "ep", 1, provider, True, Decimal(expected_bet))
        return result, verifier, provider

    def test_a_calm_screen_is_tapped_at_once_without_a_new_reading(self):
        result, verifier, provider = self._spin(
            [_calm()], [_tap_outcome(True, balance_after=Decimal("99.80"))], [_context_result(Decimal("100.00"))],
        )

        assert result.data.outcome == "success" and result.data.attempts == 1
        assert provider.call_count == 1                           # nessuna rilettura: niente da aggiornare
        verifier.wait_until_calm.assert_called_once()

    def test_a_moving_screen_is_waited_for_and_the_balance_is_read_again(self):
        """Scenario dello spin 2 di Pirots5: la vincita viene accreditata mentre si aspetta."""
        result, verifier, provider = self._spin(
            [_calm(waited=True)],
            [_tap_outcome(True, balance_after=Decimal("4999.93"))],
            [_context_result(Decimal("4999.80")), _context_result(Decimal("5000.13"))],   # accreditato nell'attesa
            expected_bet="0.20",
        )

        assert provider.call_count == 2
        assert result.data.balance_before == Decimal("5000.13")
        assert result.data.win_amount is None            # 5000.13 -> 4999.93 = solo la puntata: nessuna vincita

    def test_a_screen_that_never_calms_down_blocks_the_spin_without_tapping(self):
        result, verifier, _ = self._spin([_calm(calm=False, waited=True)], [], [_context_result(Decimal("100"))])

        assert result.data.outcome == "blocked" and result.data.violated_invariant == "UNSAFE_SCREEN_STATE"
        verifier.verified_tap.assert_not_called()

    def test_the_invariants_are_checked_again_on_the_fresh_reading(self):
        result, verifier, _ = self._spin(
            [_calm(waited=True)], [],
            [_context_result(Decimal("100")), _context_result(Decimal("0.05"))],      # saldo ora insufficiente
        )

        assert result.data.violated_invariant == "INSUFFICIENT_BALANCE"
        verifier.verified_tap.assert_not_called()

    def test_with_the_check_off_nothing_is_added(self):
        locator = MagicMock()
        locator.locate.return_value = _candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = _tap_outcome(True, balance_after=Decimal("99.90"))
        controller, _, _, _ = _controller(locator=locator, verifier=verifier)    # spin_calm_check=False

        controller.spin("dev", "pirots5", "ep", 1, MagicMock(return_value=_context_result(Decimal("100"))), True,
                        Decimal("0.10"))

        verifier.wait_until_calm.assert_not_called()


class TestNoBlindRetapAfterAnUnexpectedChange:
    def _spin(self, taps, calm_results, contexts, strategy="profile"):
        return TestCalmBeforeTapping()._spin(calm_results, taps, contexts, strategy)

    def test_a_charge_seen_after_the_wait_means_the_spin_started_no_retap(self):
        result, verifier, provider = self._spin(
            [_unexpected()], [_calm(), _calm(waited=True)],
            [_context_result(Decimal("4999.80")), _context_result(Decimal("4999.60"))],
        )

        assert result.data.outcome == "success" and result.data.attempts == 1
        assert result.data.balance_after == Decimal("4999.60")
        assert verifier.verified_tap.call_count == 1

    def test_an_unchanged_balance_does_not_trigger_a_blind_retap(self):
        result, verifier, _ = self._spin(
            [_unexpected()], [_calm(), _calm(waited=True)],
            [_context_result(Decimal("4999.80")), _context_result(Decimal("4999.80"))],
        )

        assert result.data.outcome == "no_effect" and result.data.attempts == 1
        assert verifier.verified_tap.call_count == 1      # prima i tocchi erano fino a 3

    def test_a_balance_that_rose_is_a_credit_not_a_spin_and_no_retap(self):
        result, verifier, _ = self._spin(
            [_unexpected()], [_calm(), _calm(waited=True)],
            [_context_result(Decimal("4999.80")), _context_result(Decimal("5000.13"))],
        )

        assert result.data.outcome == "no_effect" and verifier.verified_tap.call_count == 1

    def test_a_plain_no_effect_is_still_retried_for_trusted_coordinates(self):
        no_effect = Result.ok(TapOutcome(
            tapped_coords=(540, 2000), verified=False, screenshot_before="/tmp/a.png", screenshot_after="/tmp/b.png",
            diff_score=0.0, balance_before=Decimal("100"), balance_after=Decimal("100"), bet_before=None,
            bet_after=None, screen_state_before="idle", screen_state_after="idle", outcome="no_effect",
        ))
        result, verifier, _ = self._spin(
            [no_effect, _tap_outcome(True, balance_after=Decimal("99.80"))], [_calm()],
            [_context_result(Decimal("100.00"))],
        )

        assert result.data.outcome == "success" and verifier.verified_tap.call_count == 2
