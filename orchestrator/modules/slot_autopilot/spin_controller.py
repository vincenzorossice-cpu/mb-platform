import logging
import time
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

logger = logging.getLogger(__name__)

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
    strategy_name: Optional[str] = None  # strategia che ha localizzato lo spin (None se non tappato)
    # Schermo stabile dopo lo spin e relativa lettura OCR (da TapOutcome): chi deve
    # iniziare lo spin successivo puo' riusarli invece di rifare la lettura.
    screenshot_after: Optional[str] = None
    state_after: Optional[dict] = None
    # Il saldo e' sceso di molto piu' della puntata creduta: la puntata reale e' piu' alta.
    bet_understated: bool = False


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
        started = time.monotonic()
        context_result = context_provider()
        context_seconds = time.monotonic() - started
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
            # Schermo occupato (vincita/bonus in corso): chi chiama aspetta e riprova, non e'
            # un avviso da mostrare a ogni tentativo. Le altre invarianti restano avvisi.
            level = logging.INFO if violated == "UNSAFE_SCREEN_STATE" else logging.WARNING
            logger.log(level, "spin %d bloccato da invariante di sicurezza: %s", spin_index, violated)
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

        attempts = 0
        screenshot_after = None
        state_after = None
        balance_after_read = False   # False: balance_after e' solo il valore "prima" usato come ripiego
        if candidate.verified:
            # Ramo HITL: nessun verified_tap di mezzo (l'operatore ha gia'
            # tappato e verificato), quindi nessun TapOutcome da cui leggere
            # saldo/vincita - qui, e solo qui, serve una lettura di contesto
            # dedicata. Caso raro (fallback), non ottimizzato.
            verified = True
            final_context_result = context_provider()
            final_context = final_context_result.data if final_context_result.success else None
            balance_after = self._read_amount(final_context, "saldo") if final_context else None
            balance_after_read = balance_after is not None
            if balance_after is None:
                balance_after = balance_before
            win_amount = self._read_amount(final_context, "vincita") if final_context else None
        else:
            verified = False
            balance_after = balance_before
            win_amount = None
            for _ in range(self.config.max_spin_attempts_per_step):
                attempts += 1
                # "balance_changed", non "balance_decrease": una vincita puo'
                # far salire il saldo invece di scenderlo della puntata -
                # richiedere specificamente un calo classificherebbe una
                # vincita come spin fallito, causando ritap inutili (e
                # rischiosi) su uno spin gia' riuscito.
                # Solo al primo tentativo: il context e' fresco (letto in cima a
                # spin(), locate() non tocca lo schermo). Ai ritap successivi lo
                # schermo e' cambiato, quindi si rilegge davvero.
                reuse = attempts == 1
                tap_result = self.verifier.verified_tap(
                    device_id, candidate.x, candidate.y,
                    Expectation(kind="balance_changed"),
                    screenshot_before=context.screenshot_path if reuse else None,
                    state_before=context.slot_ocr_state if reuse else None,
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
                # Gia' disponibili dal tap appena verificato: nessuna lettura
                # di contesto aggiuntiva necessaria (era la ridondanza da
                # eliminare per velocita').
                if tap_result.data.balance_after is not None:
                    balance_after = tap_result.data.balance_after
                    balance_after_read = True
                win_amount = tap_result.data.win_amount
                screenshot_after = tap_result.data.screenshot_after
                state_after = tap_result.data.state_after
                if tap_result.data.verified:
                    verified = True
                    self.locator.record_success(candidate.strategy_name, _ELEMENT)
                    break
                if not self.locator.is_trusted_strategy(candidate.strategy_name):
                    # Coordinata indovinata (es. da GPT) che non fa partire lo spin:
                    # ritoccarla e' inutile e ogni giro costa ~20 s. Si scarta il
                    # punto (il prossimo giro rilocalizza evitandolo) e si conta il
                    # fallimento, cosi' dopo qualche errore si scende all'operatore.
                    self.locator.reject_position(_ELEMENT, candidate.x, candidate.y)
                    self.locator.record_failure(candidate.strategy_name, _ELEMENT)
                    logger.warning(
                        "spin %d: il tocco a (%d,%d) via '%s' non ha effetto: punto scartato.",
                        spin_index, candidate.x, candidate.y, candidate.strategy_name,
                    )
                    break

        outcome = "success" if verified else "no_effect"
        if verified:
            # Come fa BetController: una coordinata verificata dal vivo e
            # trovata da GPT/HITL va ricordata, altrimenti ogni sessione la
            # ritrova da capo (save_verified_candidate ignora profile/memory).
            self.locator.save_verified_candidate(
                _ELEMENT, candidate, slot_name, context.screen_width, context.screen_height,
            )
        bet_understated = outcome == "success" and self._bet_understated(balance_before, balance_after, expected_bet)
        if bet_understated:
            # Una sola lettura sbagliata (cifre perse) non deve fermare la sessione: si rilegge il saldo.
            second = self._reread_balance(context_provider)
            if second is not None and not self._bet_understated(balance_before, second, expected_bet):
                logger.warning(
                    "spin %d: la prima lettura del saldo (%s) era incoerente, la seconda (%s) e' coerente: uso la seconda.",
                    spin_index, balance_after, second,
                )
                balance_after = second
                bet_understated = False
        if outcome == "success":
            # La vincita si ricava dal SALDO (dopo - prima + puntata): la scritta "ultima vincita" del gioco
            # resta a schermo finche' non ce n'e' un'altra (o puo' essere confusa con altri numeri) e non
            # dice cosa e' successo in QUESTO spin. La vincita letta serve solo se manca un saldo.
            # L'accredito puo' arrivare con la lettura dello spin dopo: il totale resta esatto.
            from_balance = (
                self._balance_win(balance_before, balance_after, expected_bet) if balance_after_read else None
            )
            if from_balance is not None:
                if win_amount is not None and abs(win_amount - from_balance) > self.config.accounting_tolerance:
                    logger.debug("spin %d: la vincita letta (%s) non coincide con quella dal saldo (%s): uso il saldo.",
                                 spin_index, win_amount, from_balance)
                win_amount = from_balance if from_balance >= Decimal("0.01") else None
        if bet_understated:
            logger.error(
                "spin %d: il saldo e' sceso di %s con una puntata creduta di %s: la puntata reale e' piu' alta.",
                spin_index, balance_before - balance_after, expected_bet,
            )
        accounting_anomaly = None   # il controllo per spin sulla vincita letta e' stato tolto: vedi sopra
        total_seconds = time.monotonic() - started
        logger.info(
            "spin %d: outcome=%s verified=%s saldo %s->%s vincita=%s (contesto %.1f s, tocco+attesa+lettura %.1f s)",
            spin_index, outcome, verified, balance_before, balance_after, win_amount,
            context_seconds, total_seconds - context_seconds,
        )
        self._log_step(
            episode_id, spin_index, slot_name, device_id, candidate.strategy_name,
            (candidate.x, candidate.y), balance_before, balance_after,
            outcome=outcome, verified=verified, win_amount=win_amount, notes=accounting_anomaly,
        )

        return Result.ok(SpinResult(
            slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
            balance_before=balance_before, balance_after=balance_after, win_amount=win_amount,
            verified=verified, attempts=attempts, outcome=outcome,
            accounting_anomaly=accounting_anomaly, strategy_name=candidate.strategy_name,
            screenshot_after=screenshot_after, state_after=state_after, bet_understated=bet_understated,
        ))

    def _reread_balance(self, context_provider) -> Optional[Decimal]:
        result = context_provider()
        if not result.success:
            return None
        return self._read_amount(result.data, "saldo")

    def _bet_understated(self, balance_before, balance_after, bet) -> bool:
        """Una vincita puo' solo RIDURRE il calo di saldo: se il saldo scende di piu' del doppio
        della puntata creduta (piu' la tolleranza), quella puntata non puo' essere giusta."""
        if balance_before is None or balance_after is None or bet is None or bet <= 0:
            return False
        return (balance_before - balance_after) > 2 * bet + self.config.accounting_tolerance

    @staticmethod
    def _balance_win(balance_before, balance_after, bet) -> Optional[Decimal]:
        """dopo - prima + puntata, o None se manca un valore."""
        if balance_before is None or balance_after is None or bet is None:
            return None
        return balance_after - balance_before + bet

    def _check_invariants(self, min_bet_verified, balance_before, expected_bet, context) -> Optional[str]:
        if not min_bet_verified:
            return "MIN_BET_NOT_VERIFIED"
        if balance_before is None or expected_bet is None or balance_before < expected_bet:
            return "INSUFFICIENT_BALANCE"
        if classify_screen_state(context.slot_ocr_state) != "idle":
            return "UNSAFE_SCREEN_STATE"
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
