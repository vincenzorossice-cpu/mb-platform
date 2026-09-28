import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, Expectation
from orchestrator.modules.step_logger.interface import StepLoggerInterface, StepRecord

logger = logging.getLogger(__name__)

_ELEMENT = "bet_minus"


@dataclass
class BetResult:
    slot_name: str
    starting_bet: Optional[Decimal]
    final_bet: Optional[Decimal]
    min_bet_verified: bool
    attempts: int
    strategy_sequence: list = field(default_factory=list)
    # None | "VALUE_ROSE" | "MAX_ATTEMPTS" | "HITL_STOP" | "ELEMENT_NOT_LOCATABLE" | "NO_DECREASE_OBSERVED"
    aborted_reason: Optional[str] = None
    outcome: str = "aborted"  # "min_bet_confirmed" | "unchanged_already_min" | "aborted"


class BetController:
    """Abbassa la puntata al minimo vero, un tap alla volta, fermandosi subito
    se il valore sale (segno che e' stato colpito il controllo sbagliato).
    Ritorna sempre Result.ok: un abort e' un esito valido e loggato, non un
    errore di programmazione - e' SlotAutoPilot a decidere se procedere in
    base a min_bet_verified."""

    def __init__(self, locator: ElementLocator, verifier: ActionVerifier,
                 step_logger: StepLoggerInterface, config: AutoPilotConfig):
        self.locator = locator
        self.verifier = verifier
        self.step_logger = step_logger
        self.config = config

    def set_min_bet(
        self, device_id: str, slot_name: str, episode_id: str,
        context_provider: Callable[[], Result],
    ) -> Result[BetResult]:
        context_result = context_provider()
        if not context_result.success:
            return Result.fail(context_result.error, context_result.error_code)
        context = context_result.data

        # Riscontro incrociato fidato solo se e' un valore curato e confermato
        # a monte (profilo verificato), mai da un profilo non verificato: e'
        # cio' che manca oggi e che permette di distinguere "siamo davvero
        # gia' al minimo documentato" da "stiamo tappando il posto sbagliato".
        trusted_min_bet = None
        if context.profile is not None and context.profile.verified and context.profile.known_min_bet is not None:
            trusted_min_bet = Decimal(str(context.profile.known_min_bet))

        starting_bet = self._read_bet(context)
        logger.info("set_min_bet: puntata iniziale=%s, riscontro fidato=%s", starting_bet, trusted_min_bet)
        lowest_bet_seen = starting_bet
        current_bet = starting_bet
        stable_reads = 0
        any_decrease_seen = False
        strategy_sequence = []
        attempts = 0

        while attempts < self.config.max_bet_lower_attempts:
            attempts += 1
            locate_result = self.locator.locate(_ELEMENT, context)
            if not locate_result.success:
                aborted_reason = (
                    "HITL_STOP" if locate_result.error_code == "HITL_STOP_REQUESTED"
                    else "ELEMENT_NOT_LOCATABLE"
                )
                logger.warning(
                    "set_min_bet: impossibile localizzare '%s' (%s), abort=%s",
                    _ELEMENT, locate_result.error_code, aborted_reason,
                )
                self._log_step(
                    episode_id, attempts, slot_name, device_id, None, None,
                    current_bet, current_bet, outcome="error", error_code=locate_result.error_code,
                )
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                    min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                    aborted_reason=aborted_reason, outcome="aborted",
                ))

            candidate = locate_result.data
            strategy_sequence.append(candidate.strategy_name)

            if candidate.verified:
                refreshed_for_hitl = context_provider()
                new_bet = (
                    self._read_bet(refreshed_for_hitl.data) if refreshed_for_hitl.success else None
                )
                verified = True
                outcome = "success"
            else:
                tap_result = self.verifier.verified_tap(
                    device_id, candidate.x, candidate.y, Expectation(kind="bet_decrease"),
                )
                if not tap_result.success:
                    self._log_step(
                        episode_id, attempts, slot_name, device_id, candidate.strategy_name,
                        (candidate.x, candidate.y), current_bet, current_bet,
                        outcome="error", error_code=tap_result.error_code,
                    )
                    return Result.ok(BetResult(
                        slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                        min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                        aborted_reason="ELEMENT_NOT_LOCATABLE", outcome="aborted",
                    ))
                outcome_data = tap_result.data
                new_bet = outcome_data.bet_after
                verified = outcome_data.verified
                outcome = outcome_data.outcome

            logger.info(
                "set_min_bet: tentativo %d/%d: %s -> %s (outcome=%s, strategia=%s, coords=(%d,%d))",
                attempts, self.config.max_bet_lower_attempts, current_bet, new_bet, outcome,
                candidate.strategy_name, candidate.x, candidate.y,
            )
            self._log_step(
                episode_id, attempts, slot_name, device_id, candidate.strategy_name,
                (candidate.x, candidate.y), current_bet, new_bet, outcome=outcome,
                verified=verified,
            )

            if new_bet is not None and current_bet is not None and new_bet > current_bet:
                logger.error("set_min_bet: la puntata e' SALITA (%s -> %s): abort immediato", current_bet, new_bet)
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=new_bet,
                    min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                    aborted_reason="VALUE_ROSE", outcome="aborted",
                ))

            if new_bet is not None:
                current_bet = new_bet

            if verified and lowest_bet_seen is not None and current_bet is not None and current_bet < lowest_bet_seen:
                self.locator.save_verified_candidate(
                    _ELEMENT, candidate, slot_name, context.screen_width, context.screen_height,
                )
                lowest_bet_seen = current_bet
                any_decrease_seen = True
                stable_reads = 0
            else:
                stable_reads += 1

            if stable_reads >= self.config.stable_reads_for_min:
                already_at_known_min = (
                    trusted_min_bet is not None and current_bet is not None and current_bet <= trusted_min_bet
                )
                min_bet_verified = any_decrease_seen or already_at_known_min
                outcome_label = "min_bet_confirmed" if any_decrease_seen else "unchanged_already_min"
                logger.info(
                    "set_min_bet: plateau raggiunto (outcome=%s, min_bet_verified=%s, puntata finale=%s)",
                    outcome_label, min_bet_verified, current_bet,
                )
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                    min_bet_verified=min_bet_verified, attempts=attempts, strategy_sequence=strategy_sequence,
                    aborted_reason=None if min_bet_verified else "NO_DECREASE_OBSERVED",
                    outcome=outcome_label,
                ))

            refreshed = context_provider()
            if refreshed.success:
                context = refreshed.data

        logger.warning("set_min_bet: tentativi esauriti (%d) senza plateau", attempts)
        return Result.ok(BetResult(
            slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
            min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
            aborted_reason="MAX_ATTEMPTS", outcome="aborted",
        ))

    def _read_bet(self, context) -> Optional[Decimal]:
        value = context.slot_ocr_state.get("posta") if context else None
        return Decimal(str(value)) if value is not None else None

    def _log_step(self, episode_id, step_index, slot_name, device_id, strategy_used,
                  coords, bet_before, bet_after, outcome, verified=False, error_code=None):
        record = StepRecord(
            episode_id=episode_id, step_index=step_index, timestamp=_now(),
            slot_name=slot_name, device_id=device_id, phase="bet_setup", action_type="tap",
            element_name=_ELEMENT, strategy_used=strategy_used, requested_coords=coords,
            balance_before=None, balance_after=None,
            bet_before=str(bet_before) if bet_before is not None else None,
            bet_after=str(bet_after) if bet_after is not None else None,
            win_amount=None, verified=verified, diff_score=None, outcome=outcome,
            error_code=error_code, notes=None,
        )
        self.step_logger.log_step(episode_id, record)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
