from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.value_parser import parse_amount
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, Expectation, classify_screen_state
from orchestrator.modules.step_logger.interface import StepLoggerInterface, StepRecord

_ELEMENT = "spin"


@dataclass
class SpinResult:
    slot_name: str
    spin_index: int
    bet_amount: Optional[Decimal]
    balance_before: Optional[Decimal]
    balance_after: Optional[Decimal]
    win_amount: Optional[Decimal]
    verified: bool
    attempts: int
    outcome: str  # "success" | "blocked" | "no_effect" | "error"
    violated_invariant: Optional[str] = None  # "MIN_BET_NOT_VERIFIED" | "INSUFFICIENT_BALANCE" | "UNSAFE_SCREEN_STATE"
    accounting_anomaly: Optional[str] = None


class SpinController:
    """Esegue uno spin solo dopo aver verificato 3 invarianti di sicurezza, in
    quest'ordine: puntata minima verificata, saldo sufficiente, schermo
    'idle'. Il controllo contabile post-spin non e' MAI bloccante: un'anomalia
    viene solo annotata, non trasformata in un fallimento."""

    def __init__(self, locator: ElementLocator, verifier: ActionVerifier,
                 step_logger: StepLoggerInterface, config: AutoPilotConfig):
        self.locator = locator
        self.verifier = verifier
        self.step_logger = step_logger
        self.config = config

    def spin(
        self, device_id: str, slot_name: str, episode_id: str, spin_index: int,
        context_provider: Callable[[], Result], min_bet_verified: bool,
        expected_bet: Optional[Decimal],
    ) -> Result[SpinResult]:
        context_result = context_provider()
        if not context_result.success:
            self._log_step(
                episode_id, spin_index, slot_name, device_id, None, None, None, None,
                outcome="error", error_code=context_result.error_code,
            )
            return Result.fail(context_result.error, context_result.error_code)
        context = context_result.data
        balance_before = self._read_amount(context, "saldo")

        violated = self._check_invariants(min_bet_verified, balance_before, expected_bet, context)
        if violated is not None:
            self._log_step(
                episode_id, spin_index, slot_name, device_id, None, None,
                balance_before, balance_before, outcome="blocked", notes=violated,
            )
            return Result.ok(SpinResult(
                slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                balance_before=balance_before, balance_after=balance_before, win_amount=None,
                verified=False, attempts=0, outcome="blocked", violated_invariant=violated,
            ))

        locate_result = self.locator.locate(_ELEMENT, context)
        if not locate_result.success:
            self._log_step(
                episode_id, spin_index, slot_name, device_id, None, None,
                balance_before, balance_before, outcome="error", error_code=locate_result.error_code,
            )
            return Result.ok(SpinResult(
                slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                balance_before=balance_before, balance_after=balance_before, win_amount=None,
                verified=False, attempts=0, outcome="error",
            ))
        candidate = locate_result.data
        min_decrease = expected_bet if expected_bet is not None else Decimal("0.00")

        attempts = 0
        if candidate.verified:
            verified = True
        else:
            verified = False
            for _ in range(self.config.max_spin_attempts_per_step):
                attempts += 1
                tap_result = self.verifier.verified_tap(
                    device_id, candidate.x, candidate.y,
                    Expectation(kind="balance_decrease", min_decrease=min_decrease),
                )
                if not tap_result.success:
                    self._log_step(
                        episode_id, spin_index, slot_name, device_id, candidate.strategy_name,
                        (candidate.x, candidate.y), balance_before, balance_before,
                        outcome="error", error_code=tap_result.error_code,
                    )
                    return Result.ok(SpinResult(
                        slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                        balance_before=balance_before, balance_after=balance_before, win_amount=None,
                        verified=False, attempts=attempts, outcome="error",
                    ))
                if tap_result.data.verified:
                    verified = True
                    break

        final_context_result = context_provider()
        final_context = final_context_result.data if final_context_result.success else None
        balance_after = self._read_amount(final_context, "saldo") if final_context else balance_before
        win_amount = self._read_amount(final_context, "vincita") if final_context else None

        outcome = "success" if verified else "no_effect"
        accounting_anomaly = None
        if outcome == "success":
            accounting_anomaly = self._check_accounting(balance_before, expected_bet, win_amount, balance_after)

        self._log_step(
            episode_id, spin_index, slot_name, device_id, candidate.strategy_name,
            (candidate.x, candidate.y), balance_before, balance_after,
            outcome=outcome, verified=verified, win_amount=win_amount, notes=accounting_anomaly,
        )

        return Result.ok(SpinResult(
            slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
            balance_before=balance_before, balance_after=balance_after, win_amount=win_amount,
            verified=verified, attempts=attempts, outcome=outcome,
            accounting_anomaly=accounting_anomaly,
        ))

    def _check_invariants(self, min_bet_verified, balance_before, expected_bet, context) -> Optional[str]:
        if not min_bet_verified:
            return "MIN_BET_NOT_VERIFIED"
        if balance_before is None or expected_bet is None or balance_before < expected_bet:
            return "INSUFFICIENT_BALANCE"
        if classify_screen_state(context.slot_ocr_state) != "idle":
            return "UNSAFE_SCREEN_STATE"
        return None

    def _check_accounting(self, balance_before, bet, win_amount, balance_after) -> Optional[str]:
        if balance_before is None or balance_after is None:
            return None
        expected_after = balance_before - (bet or Decimal("0.00")) + (win_amount or Decimal("0.00"))
        if abs(balance_after - expected_after) > self.config.accounting_tolerance:
            return (
                f"Accounting mismatch: expected balance {expected_after}, "
                f"observed {balance_after} (bet={bet}, win={win_amount})"
            )
        return None

    def _read_amount(self, context, field: str) -> Optional[Decimal]:
        if context is None:
            return None
        value = context.slot_ocr_state.get(field)
        if value is None:
            return None
        parsed = parse_amount(str(value))
        return parsed.data if parsed.success else None

    def _log_step(self, episode_id, step_index, slot_name, device_id, strategy_used, coords,
                  balance_before, balance_after, outcome, verified=False, win_amount=None,
                  error_code=None, notes=None):
        record = StepRecord(
            episode_id=episode_id, step_index=step_index, timestamp=_now(),
            slot_name=slot_name, device_id=device_id, phase="spin", action_type="spin",
            element_name=_ELEMENT, strategy_used=strategy_used, requested_coords=coords,
            balance_before=str(balance_before) if balance_before is not None else None,
            balance_after=str(balance_after) if balance_after is not None else None,
            bet_before=None, bet_after=None,
            win_amount=str(win_amount) if win_amount is not None else None,
            verified=verified, diff_score=None, outcome=outcome, error_code=error_code, notes=notes,
        )
        self.step_logger.log_step(episode_id, record)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
