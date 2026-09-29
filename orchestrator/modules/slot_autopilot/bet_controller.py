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
_MENU_TOGGLE_ELEMENT = "bet_menu_toggle"


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

        if trusted_min_bet is not None and starting_bet is not None and starting_bet <= trusted_min_bet:
            logger.info(
                "set_min_bet: puntata gia' al minimo confermato dal profilo (%s <= %s): nessun tap necessario.",
                starting_bet, trusted_min_bet,
            )
            return Result.ok(BetResult(
                slot_name=slot_name, starting_bet=starting_bet, final_bet=starting_bet,
                min_bet_verified=True, attempts=0, strategy_sequence=[],
                aborted_reason=None, outcome="unchanged_already_min",
            ))

        we_opened_the_menu = False
        if (
            context.profile is not None and context.profile.verified
            and _ELEMENT not in context.profile.elements
            and _MENU_TOGGLE_ELEMENT in context.profile.elements
        ):
            # Il profilo curato dice che questa slot non ha un pulsante "-"
            # ma ha un'icona che apre/chiude il menu puntata: apriamolo da
            # soli, invece di far scoprire la stessa cosa a un umano via
            # HITL al primo fallimento di locate("bet_minus").
            logger.info(
                "set_min_bet: profilo senza '%s' ma con '%s': apro il menu puntata prima di procedere.",
                _ELEMENT, _MENU_TOGGLE_ELEMENT,
            )
            toggle_result = self.locator.locate(_MENU_TOGGLE_ELEMENT, context)
            if toggle_result.success:
                toggle_candidate = toggle_result.data
                toggle_tap = self.verifier.verified_tap(
                    device_id, toggle_candidate.x, toggle_candidate.y, Expectation(kind="screen_changed"),
                )
                we_opened_the_menu = toggle_tap.success and toggle_tap.data.verified
                logger.info("set_min_bet: apertura menu puntata -> riuscita=%s", we_opened_the_menu)
                refreshed = context_provider()
                if refreshed.success:
                    context = refreshed.data
            else:
                logger.warning(
                    "set_min_bet: '%s' documentato ma non localizzabile (%s): procedo comunque su '%s'.",
                    _MENU_TOGGLE_ELEMENT, toggle_result.error_code, _ELEMENT,
                )

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
                if locate_result.error_code == "HITL_STOP_REQUESTED":
                    logger.warning("set_min_bet: l'operatore ha chiesto di fermarsi, abort=HITL_STOP")
                    self._log_step(
                        episode_id, attempts, slot_name, device_id, None, None,
                        current_bet, current_bet, outcome="error", error_code=locate_result.error_code,
                    )
                    return Result.ok(BetResult(
                        slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                        min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                        aborted_reason="HITL_STOP", outcome="aborted",
                    ))

                # Fallimento "morbido": non abortisce. Copre sia il caso
                # generico (nessuna strategia ha funzionato QUESTO giro, ma
                # potrebbe funzionare al prossimo con uno schermo diverso) sia
                # HITL_HINT_ONLY/HITL_SKIPPED (l'operatore ha gia' interagito
                # utilmente - es. aperto il menu giusto - e serve solo un
                # altro scambio, non un abort). Consuma un tentativo e
                # riprova con un contesto fresco.
                logger.warning(
                    "set_min_bet: tentativo %d/%d fallito (%s), riprovo",
                    attempts, self.config.max_bet_lower_attempts, locate_result.error_code,
                )
                self._log_step(
                    episode_id, attempts, slot_name, device_id, None, None,
                    current_bet, current_bet, outcome="error", error_code=locate_result.error_code,
                )
                refreshed = context_provider()
                if refreshed.success:
                    context = refreshed.data
                continue

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

            # Localizzare e avere-effetto sono passi distinti (vedi
            # ElementLocator): solo qui, dopo aver visto l'esito reale del
            # tap, sappiamo se questa strategia sta davvero funzionando per
            # questo elemento o sta solo restituendo una coordinata inerte.
            # Le fonti gia' fidate (profile/memory/hitl) sono escluse: un
            # loro plateau e' la firma di un minimo vero, non di un bersaglio
            # sbagliato, e non va mai fatto esaurire da questo meccanismo.
            is_trusted = self.locator.is_trusted_strategy(candidate.strategy_name)
            if verified:
                self.locator.record_success(candidate.strategy_name, _ELEMENT)
            elif not is_trusted:
                self.locator.record_failure(candidate.strategy_name, _ELEMENT)

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

                if trusted_min_bet is not None and current_bet <= trusted_min_bet:
                    # Il riscontro incrociato col profilo verificato e' gia'
                    # la prova: chiedere altre stable_reads_for_min letture
                    # di conferma (spesso via nuovi scambi HITL) sarebbe
                    # solo spreco, non maggiore sicurezza.
                    logger.info(
                        "set_min_bet: puntata scesa al minimo confermato dal profilo (%s <= %s): fermo subito.",
                        current_bet, trusted_min_bet,
                    )
                    self._close_bet_menu_if_opened(device_id, context, we_opened_the_menu)
                    return Result.ok(BetResult(
                        slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                        min_bet_verified=True, attempts=attempts, strategy_sequence=strategy_sequence,
                        aborted_reason=None, outcome="min_bet_confirmed",
                    ))
            elif not verified and not is_trusted and self.locator.is_exhausted(candidate.strategy_name, _ELEMENT):
                # Questa strategia ha appena esaurito i suoi tentativi: il
                # prossimo locate() e' garantito saltarla e scendere nella
                # catena (es. verso hitl). Un tap a vuoto da una strategia
                # ormai screditata non e' un'evidenza valida di "gia' al
                # minimo" - e' evidenza che stiamo tappando il posto
                # sbagliato. Non contarlo come lettura stabile: si passa
                # subito al tentativo successivo, dando alla strategia
                # successiva la sua occasione prima di concludere il plateau.
                logger.info(
                    "set_min_bet: strategia '%s' esaurita per '%s' senza effetto reale: non e' evidenza di "
                    "plateau, riprovo con la prossima strategia disponibile.",
                    candidate.strategy_name, _ELEMENT,
                )
                refreshed = context_provider()
                if refreshed.success:
                    context = refreshed.data
                continue
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
                if min_bet_verified:
                    self._close_bet_menu_if_opened(device_id, context, we_opened_the_menu)
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

    def _close_bet_menu_if_opened(self, device_id: str, context, we_opened_the_menu: bool) -> None:
        """Richiude il menu puntata solo se e' stato il sistema stesso ad
        aprirlo: lasciare lo schermo com'era trovato, non bloccante se il
        tap di richiusura fallisce (solo loggato)."""
        if not we_opened_the_menu:
            return
        toggle_result = self.locator.locate(_MENU_TOGGLE_ELEMENT, context)
        if not toggle_result.success:
            logger.warning(
                "set_min_bet: impossibile richiudere '%s' (%s): lo lascio aperto.",
                _MENU_TOGGLE_ELEMENT, toggle_result.error_code,
            )
            return
        candidate = toggle_result.data
        self.verifier.verified_tap(device_id, candidate.x, candidate.y, Expectation(kind="screen_changed"))
        logger.info("set_min_bet: menu puntata richiuso.")

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
