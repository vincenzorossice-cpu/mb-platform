import dataclasses
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import (
    load_autopilot_flag,
    load_autopilot_value,
    save_autopilot_flag,
    save_autopilot_value,
)
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector, _looks_like_menu, menu_amounts
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.screen_recovery import ScreenRecovery, money_moved
from orchestrator.modules.slot_autopilot.supervisor import Assist
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, Expectation
from orchestrator.modules.step_logger.interface import StepLoggerInterface, StepRecord

logger = logging.getLogger(__name__)

_ELEMENT = "bet_minus"
_MENU_TOGGLE_ELEMENT = "bet_menu_toggle"
_PLUS_ELEMENT = "bet_plus"
_SPIN_ELEMENT = "spin"
_ZONE_ELEMENTS = ("bet_minus", "bet_plus", "bet_menu_toggle", "close_button")
_SPIN_ZONE_RADIUS_PX = 90
_BET_VIA_MENU_FLAG = "bet_via_menu"
_MIN_BET_VALUE = "min_bet"
_MIN_BET_TRUSTED_VALUE = "min_bet_trusted"


@dataclass
class _ProbeOutcome:
    """Esito della prova del pavimento. status: 'floor' (pavimento provato) |
    'continue' (la puntata e' stata alzata: riprendere la discesa da `bet`) |
    'inconclusive' (nulla e' cambiato) | 'stuck' (schermata sbagliata non ripristinata)."""
    status: str
    bet: Optional[Decimal] = None
    minus_alive: bool = False
    minus_dead: bool = False


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
                 step_logger: StepLoggerInterface, config: AutoPilotConfig,
                 menu_selector: Optional[BetMenuSelector] = None,
                 screen_recovery: Optional[ScreenRecovery] = None,
                 supervisor=None):
        self.locator = locator
        self.verifier = verifier
        self.step_logger = step_logger
        self.config = config
        # None = il fallback "menu puntata" e' spento (comportamento storico).
        self.menu_selector = menu_selector
        # None = nessun recupero da schermate aperte per errore (storico).
        self.screen_recovery = screen_recovery
        # None = nessun supervisore: si passa direttamente all'operatore (storico).
        self.supervisor = supervisor
        # slot -> puntata minima verificata in QUESTA sessione: ripetere la verifica (es. dopo un aiuto
        # dell'operatore) non deve riaprire il menu se la puntata e' gia' a quel valore o sotto.
        self._session_min = {}
        self._declared_applied = set()

    def forget_session_min(self, slot_name: str) -> None:
        """Dopo un rientro nella slot la puntata torna quella di partenza: il minimo verificato non vale piu'."""
        self._session_min.pop(slot_name, None)

    def set_min_bet(
        self, device_id: str, slot_name: str, episode_id: str,
        context_provider: Callable[[], Result],
    ) -> Result[BetResult]:
        result = self._set_min_bet(device_id, slot_name, episode_id, context_provider)
        declared = self.config.known_min_bet
        if (declared is not None and result.success and result.data.min_bet_verified
                and result.data.final_bet is not None and result.data.final_bet > declared):
            logger.error("set_min_bet: la puntata %s e' piu' alta del minimo dichiarato %s: non la do' per minima.",
                         result.data.final_bet, declared)
            result.data.min_bet_verified = False
            result.data.aborted_reason = "ABOVE_KNOWN_MIN"
            result.data.outcome = "aborted"
        if result.success and result.data.min_bet_verified and (result.data.attempts or result.data.strategy_sequence):
            # Qualcosa e' stato toccato (menu, discesa, operatore): un pannello puo' essere rimasto aperto.
            try:
                if self.menu_selector is not None:
                    self.menu_selector.ensure_closed(device_id, slot_name, context_provider)
            except Exception as exc:    # il controllo non deve mai rompere la verifica della puntata
                logger.warning("set_min_bet: controllo del pannello aperto fallito: %s", exc)
        if result.success and result.data.min_bet_verified and result.data.final_bet is not None:
            known = self._session_min.get(slot_name)
            self._session_min[slot_name] = (
                result.data.final_bet if known is None else min(known, result.data.final_bet)
            )
        return result

    def _set_min_bet(
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
        self._apply_declared_min(slot_name)
        trusted_min_bet = None
        if context.profile is not None and context.profile.verified and context.profile.known_min_bet is not None:
            trusted_min_bet = Decimal(str(context.profile.known_min_bet))
        proven_min = self._trusted_min(slot_name)
        if proven_min is not None:
            trusted_min_bet = proven_min if trusted_min_bet is None else min(trusted_min_bet, proven_min)

        starting_bet = self._read_bet(context)
        # Minimo verificato in una sessione precedente (auto-appreso): un indizio che,
        # insieme a un tocco sul "-" che non scende, verifica il minimo. NON e' il
        # riscontro fidato di un profilo curato e non salta mai i tocchi da solo.
        remembered_min = self._remembered_min(slot_name)
        logger.info(
            "set_min_bet: puntata iniziale=%s, riscontro fidato=%s, minimo ricordato=%s",
            starting_bet, trusted_min_bet, remembered_min,
        )

        session_min = self._session_min.get(slot_name)
        if session_min is not None and starting_bet is not None and starting_bet <= session_min:
            logger.info(
                "set_min_bet: puntata %s gia' al minimo verificato in questa sessione (%s): nessun tocco.",
                starting_bet, session_min,
            )
            return Result.ok(BetResult(
                slot_name=slot_name, starting_bet=starting_bet, final_bet=starting_bet,
                min_bet_verified=True, attempts=0, strategy_sequence=[],
                aborted_reason=None, outcome="unchanged_already_min",
            ))

        if starting_bet is None:
            # Senza la puntata leggibile nessun tocco e' verificabile: e' quando un tocco alla cieca
            # ha gia' giocato uno spin. Meglio fermarsi che tirare a indovinare.
            logger.error("set_min_bet: non riesco a leggere la puntata (neppure per visione): non tocco nulla.")
            return Result.ok(BetResult(
                slot_name=slot_name, starting_bet=None, final_bet=None, min_bet_verified=False,
                attempts=0, strategy_sequence=[], aborted_reason="BET_UNREADABLE", outcome="aborted",
            ))

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

        menu_tried = False
        we_opened_the_menu = False
        menu_route_used = False
        if self.menu_selector is not None and load_autopilot_flag(
            slot_name, _BET_VIA_MENU_FLAG, self.config.slot_memory_dir,
        ):
            # Sessione precedente: qui la puntata si cambia dal menu (nessun
            # tasto meno utilizzabile). Si va dritti li', niente giri a vuoto.
            logger.info("set_min_bet: la memoria dice che questa slot cambia puntata dal menu: provo li' per primo.")
            menu_tried = True
            menu_outcome, menu_open = self._select_via_menu(
                device_id, slot_name, episode_id, context, context_provider, starting_bet, [], 1,
            )
            if menu_outcome is not None:
                return menu_outcome
            if menu_open:
                we_opened_the_menu = menu_route_used = True
                self.locator.reset_failures(_ELEMENT)
                refreshed = context_provider()
                if refreshed.success:
                    context = refreshed.data

        if (
            not we_opened_the_menu
            and context.profile is not None and context.profile.verified
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
        consecutive_decreases = 0  # discese verificate di fila (abilita la raffica)
        probe_done = False
        last_step = None  # gradino osservato in un tocco singolo verificato (dimensiona la corsa)
        minus_proven_alive = False  # la prova "+ poi -" ha dimostrato che il "-" funziona davvero
        decreases_seen = 0
        money_moved_events = 0
        spin_zone_done = False
        unavailable_streak = 0

        while attempts < self.config.max_bet_lower_attempts:
            attempts += 1
            outcome_data = None
            iteration_started = time.monotonic()
            run_seconds = 0.0
            if self.menu_selector is not None and not menu_tried and not we_opened_the_menu:
                # Prima di chiamare l'operatore: se il "-" non si trova da soli, su molte slot la
                # puntata si cambia dal menu (icona monete): lo provo io, una volta sola.
                locate_result = self.locator.locate(_ELEMENT, context, exclude_strategies=("hitl",))
                if not locate_result.success and locate_result.error_code != "GPT_LOCATOR_UNAVAILABLE":
                    menu_tried = True
                    logger.info("set_min_bet: '%s' non trovato (%s): provo il menu puntata prima di chiedere aiuto.",
                                _ELEMENT, locate_result.error_code)
                    menu_outcome, menu_open = self._select_via_menu(
                        device_id, slot_name, episode_id, context, context_provider,
                        starting_bet, strategy_sequence, attempts, allow_operator=False,
                    )
                    if menu_outcome is not None:
                        return menu_outcome
                    self.locator.reset_failures(_ELEMENT)
                    if menu_open:
                        we_opened_the_menu = menu_route_used = True
                    refreshed = context_provider()
                    if refreshed.success:
                        context = refreshed.data
                    if menu_open:
                        continue
                    locate_result = self._locate_before_operator(context)
                    if isinstance(locate_result, Assist):
                        verdict = locate_result
                        if verdict.outcome == "at_min":
                            return self._supervisor_confirmed_min(
                                slot_name, episode_id, device_id, starting_bet, verdict.bet,
                                strategy_sequence, attempts, any_decrease_seen,
                            )
                        strategy_sequence.append("supervisor")
                        self.locator.reset_failures(_ELEMENT)
                        refreshed = context_provider()
                        if refreshed.success:
                            context = refreshed.data
                        continue
            else:
                locate_result = self._locate_before_operator(context)
                if isinstance(locate_result, Assist):
                    verdict = locate_result
                    if verdict.outcome == "at_min":
                        return self._supervisor_confirmed_min(
                            slot_name, episode_id, device_id, starting_bet, verdict.bet,
                            strategy_sequence, attempts, any_decrease_seen,
                        )
                    # "closed": un menu non voluto e' stato chiuso, lo schermo e' nuovo
                    strategy_sequence.append("supervisor")
                    self.locator.reset_failures(_ELEMENT)
                    refreshed = context_provider()
                    if refreshed.success:
                        context = refreshed.data
                    continue
            if not locate_result.success:
                if locate_result.error_code == "HITL_INFO_ONLY":
                    # L'operatore non ha dato un'istruzione ma un'informazione ("la puntata e' gia' al minimo"):
                    # nessun tocco. Vale come riscontro indipendente (come un aiuto dell'operatore).
                    final_bet = self.supervisor.read_bet(device_id) if self.supervisor is not None else None
                    logger.info("set_min_bet: l'operatore conferma la puntata minima (letta: %s).", final_bet)
                    return self._supervisor_confirmed_min(
                        slot_name, episode_id, device_id, starting_bet, final_bet,
                        strategy_sequence + ["hitl"], attempts, any_decrease_seen, by_operator=True,
                    )
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

                if locate_result.error_code == "GPT_LOCATOR_UNAVAILABLE":
                    # Non e' "l'elemento non c'e'": GPT non risponde (limite di richieste, rete).
                    unavailable_streak += 1
                    if unavailable_streak >= 3:
                        logger.error("set_min_bet: GPT non raggiungibile per %d tentativi di fila: mi fermo.",
                                     unavailable_streak)
                        self._close_bet_menu_if_opened(device_id, context, we_opened_the_menu)
                        return Result.ok(BetResult(
                            slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                            min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                            aborted_reason="GPT_UNAVAILABLE", outcome="aborted",
                        ))
                else:
                    unavailable_streak = 0

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
                if locate_result.error_code == "HITL_HINT_ONLY" and refreshed.success:
                    # L'operatore ha toccato qualcosa e lo schermo e' cambiato: e' una schermata nuova,
                    # gli errori di prima non valgono, e va guardato COSA e' successo.
                    self.locator.reset_failures(_ELEMENT)
                    if "hitl" not in strategy_sequence:
                        strategy_sequence.append("hitl")
                    operator_tap = self.locator.last_hitl_tap()
                    if self.menu_selector is not None and _looks_like_menu(menu_amounts(context.text_boxes)):
                        logger.info("set_min_bet: l'operatore ha aperto un menu con una lista di puntate: scelgo la piu' bassa.")
                        selection = self.menu_selector.select_from_open_menu(
                            device_id, slot_name, context, context_provider, operator_tap,
                            target=self._trusted_min(slot_name),
                        )
                        if selection.success:
                            return self._menu_result(
                                device_id, slot_name, episode_id, starting_bet, selection.data,
                                strategy_sequence + ["menu"], attempts,
                            )
                        logger.warning("set_min_bet: scelta dal menu aperto non riuscita (%s): %s",
                                       selection.error_code, selection.error)
                    else:
                        seen_bet = self._read_bet(context)
                        if seen_bet is not None and current_bet is not None and seen_bet < current_bet:
                            logger.info("set_min_bet: dopo l'aiuto dell'operatore la puntata e' scesa: %s -> %s.",
                                        current_bet, seen_bet)
                            current_bet = lowest_bet_seen = seen_bet
                            any_decrease_seen = True
                            decreases_seen += 1
                            stable_reads = 0
                            if self.menu_selector is not None and not menu_tried and not we_opened_the_menu:
                                menu_tried = True
                                menu_outcome, menu_open = self._select_via_menu(
                                    device_id, slot_name, episode_id, context, context_provider,
                                    starting_bet, strategy_sequence, attempts + 1,
                                )
                                if menu_outcome is not None:
                                    return menu_outcome
                                if menu_open:
                                    we_opened_the_menu = menu_route_used = True
                                    refreshed = context_provider()
                                    if refreshed.success:
                                        context = refreshed.data
                elif self.locator.is_chain_exhausted(_ELEMENT):
                    # Nessuna strategia ha piu' nulla da provare: ripetere il giro e' solo tempo perso.
                    logger.error("set_min_bet: nessuna strategia puo' piu' localizzare '%s': mi fermo.", _ELEMENT)
                    self._close_bet_menu_if_opened(device_id, context, we_opened_the_menu)
                    return Result.ok(BetResult(
                        slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                        min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                        aborted_reason="ELEMENT_NOT_LOCATABLE", outcome="aborted",
                    ))
                continue

            candidate = locate_result.data
            strategy_sequence.append(candidate.strategy_name)

            screenshot_before_tap = None
            burst_done = False
            if candidate.verified:
                refreshed_for_hitl = context_provider()
                new_bet = (
                    self._read_bet(refreshed_for_hitl.data) if refreshed_for_hitl.success else None
                )
                verified = True
                outcome = "success"
            else:
                if (
                    self.config.avoid_spin_zone and not spin_zone_done
                    and not self.locator.is_trusted_strategy(candidate.strategy_name)
                ):
                    # Un'ipotesi (GPT): prima di tapparla si localizza lo SPIN (senza tappare) e si
                    # escludono ~90 px attorno ad esso: un tocco li' gioca e spende.
                    spin_zone_done = True
                    spin_located = self.locator.locate(_SPIN_ELEMENT, context, exclude_strategies=("hitl",))
                    if spin_located.success:
                        spin = spin_located.data
                        self.locator.exclude_zone(
                            _ZONE_ELEMENTS, spin.x, spin.y, _SPIN_ZONE_RADIUS_PX, "big round SPIN button",
                        )
                        logger.info("set_min_bet: spin localizzato a (%d,%d): zona da evitare di %d px.",
                                    spin.x, spin.y, _SPIN_ZONE_RADIUS_PX)
                        if math.hypot(candidate.x - spin.x, candidate.y - spin.y) <= _SPIN_ZONE_RADIUS_PX:
                            logger.warning(
                                "set_min_bet: il candidato (%d,%d) e' sul pulsante di spin: non lo tocco.",
                                candidate.x, candidate.y,
                            )
                            continue

                # Corsa: il "-" ha gia' funzionato una volta da una fonte fidata; il
                # minimo e' un pavimento (non si puo' "passarlo"), quindi N-1 tocchi
                # ciechi e UN solo tocco verificato, il cui "prima" e' lo schermo
                # precedente alla corsa.
                burst_done = False
                if (
                    self.config.bet_burst_taps > 1 and consecutive_decreases >= 1
                    and current_bet is not None and self.locator.is_trusted_strategy(candidate.strategy_name)
                ):
                    run_taps = self._run_size(current_bet, trusted_min_bet or remembered_min, last_step)
                    run_started = time.monotonic()
                    self._blind_taps(
                        device_id, candidate.x, candidate.y, run_taps - 1, context.screenshot_path,
                    )
                    run_seconds = time.monotonic() - run_started
                    burst_done = True
                # Il "prima" e' lo schermo gia' letto nel contesto: niente OCR in piu'.
                tap_result = self.verifier.verified_tap(
                    device_id, candidate.x, candidate.y, Expectation(kind="bet_decrease"),
                    screenshot_before=context.screenshot_path, state_before=context.slot_ocr_state,
                    # Coordinata fidata: basta leggere la fascia di saldo/puntata (~1,5 s
                    # invece di ~9 s); con GPT/operatore si legge tutto come prima.
                    region_hint=context.amounts_band if self.locator.is_trusted_strategy(candidate.strategy_name) else None,
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
                screenshot_before_tap = outcome_data.screenshot_before
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

            iteration_seconds = time.monotonic() - iteration_started
            logger.info(
                "set_min_bet: tentativo %d/%d: %s -> %s (outcome=%s, strategia=%s, coords=(%d,%d)) "
                "[%.1f s, di cui corsa di tocchi %.1f s]",
                attempts, self.config.max_bet_lower_attempts, current_bet, new_bet, outcome,
                candidate.strategy_name, candidate.x, candidate.y, iteration_seconds, run_seconds,
            )
            self._log_step(
                episode_id, attempts, slot_name, device_id, candidate.strategy_name,
                (candidate.x, candidate.y), current_bet, new_bet, outcome=outcome,
                verified=verified,
            )

            if (
                not verified and outcome_data is not None and money_moved(
                    {"saldo": outcome_data.balance_before, "posta": outcome_data.bet_before},
                    {"saldo": outcome_data.balance_after, "posta": outcome_data.bet_after},
                )
            ):
                # Il saldo e' cambiato senza scalare con la puntata: il tocco ha GIOCATO UNO SPIN.
                # Niente ritocco ne' recupero (spenderebbero ancora): il punto e' sbagliato per sempre.
                money_moved_events += 1
                self.locator.reject_position(_ELEMENT, candidate.x, candidate.y)
                self.locator.remember_rejected(slot_name, _ELEMENT, candidate.x, candidate.y)
                logger.error(
                    "set_min_bet: il tocco a (%d,%d) ha giocato uno spin (saldo %s -> %s): punto scartato per sempre.",
                    candidate.x, candidate.y, outcome_data.balance_before, outcome_data.balance_after,
                )
                if money_moved_events >= 2:
                    return Result.ok(BetResult(
                        slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                        min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                        aborted_reason="MONEY_MOVED", outcome="aborted",
                    ))
                consecutive_decreases = 0
                refreshed = context_provider()
                if refreshed.success:
                    context = refreshed.data
                continue

            if not verified and outcome == "unexpected_change" and screenshot_before_tap is not None:
                # Il tap ha cambiato lo schermo senza abbassare la puntata:
                # quasi certamente ha aperto un'altra schermata (info, regole...).
                # Le letture fatte li' sono spazzatura (es. "10.0" da un testo
                # della pagina): prima si torna indietro, poi si riprende.
                self.locator.reject_position(_ELEMENT, candidate.x, candidate.y)
                if self.screen_recovery is not None:
                    recovery = self.screen_recovery.recover(
                        device_id, screenshot_before_tap, (candidate.x, candidate.y), context_provider,
                        {"posta": outcome_data.bet_before, "saldo": outcome_data.balance_before},
                    )
                    if not recovery.success:
                        logger.error(
                            "set_min_bet: bloccato su una schermata inattesa, recupero fallito (%s): mi fermo.",
                            recovery.error_code,
                        )
                        return Result.ok(BetResult(
                            slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                            min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                            aborted_reason="STUCK_ON_UNEXPECTED_SCREEN", outcome="aborted",
                        ))
                    if recovery.data.method != "already_original":
                        # Provato: il tocco aveva cambiato lo schermo e abbiamo
                        # dovuto annullarlo: non riproporlo mai piu' su questa slot.
                        self.locator.remember_rejected(slot_name, _ELEMENT, candidate.x, candidate.y)
                    consecutive_decreases = 0
                    refreshed = context_provider()
                    if refreshed.success:
                        context = refreshed.data
                    continue

            if (
                not verified and outcome == "no_effect" and outcome_data is not None
                and self.config.floor_probe_enabled and not probe_done and not any_decrease_seen
                and trusted_min_bet is None and remembered_min is None and current_bet is not None
                and self.locator.is_trusted_strategy(candidate.strategy_name)
            ):
                # Puntata che non scende da una coordinata gia' provata e nessun minimo noto:
                # prima di ripetere tocchi alla cieca si PROVA che il "-" e' vivo (+ poi -).
                probe_done = True
                probe = self._probe_floor(
                    device_id, slot_name, context, candidate, outcome_data, current_bet, context_provider,
                )
                if probe.status == "floor":
                    self._close_bet_menu_if_opened(device_id, context, we_opened_the_menu)
                    if menu_route_used:
                        save_autopilot_flag(slot_name, _BET_VIA_MENU_FLAG, True, self.config.slot_memory_dir)
                    self._remember_min(slot_name, current_bet, trusted=True)
                    return Result.ok(BetResult(
                        slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                        min_bet_verified=True, attempts=attempts, strategy_sequence=strategy_sequence,
                        aborted_reason=None, outcome="unchanged_already_min",
                    ))
                if probe.status == "stuck":
                    return Result.ok(BetResult(
                        slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                        min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                        aborted_reason="STUCK_ON_UNEXPECTED_SCREEN", outcome="aborted",
                    ))
                if probe.status == "continue":
                    # La puntata e' stata alzata di un gradino: si riparte da li' (nessun
                    # falso VALUE_ROSE), col "-" vivo o da sostituire.
                    current_bet = lowest_bet_seen = probe.bet
                    any_decrease_seen = probe.minus_alive
                    minus_proven_alive = minus_proven_alive or probe.minus_alive
                    consecutive_decreases = 1 if probe.minus_alive else 0
                    stable_reads = 0
                    if probe.minus_dead:
                        self.locator.exhaust_strategy(candidate.strategy_name, _ELEMENT)
                        self.locator.reject_position(_ELEMENT, candidate.x, candidate.y)
                    refreshed = context_provider()
                    if refreshed.success:
                        context = refreshed.data
                    continue

            if new_bet is not None and current_bet is not None and new_bet > current_bet:
                logger.error("set_min_bet: la puntata e' SALITA (%s -> %s): abort immediato", current_bet, new_bet)
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=new_bet,
                    min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                    aborted_reason="VALUE_ROSE", outcome="aborted",
                ))

            bet_before_update = current_bet
            if new_bet is not None:
                current_bet = new_bet

            if verified and lowest_bet_seen is not None and current_bet is not None and current_bet < lowest_bet_seen:
                self.locator.save_verified_candidate(
                    _ELEMENT, candidate, slot_name, context.screen_width, context.screen_height,
                )
                lowest_bet_seen = current_bet
                any_decrease_seen = True
                stable_reads = 0
                consecutive_decreases += 1
                decreases_seen += 1
                if not burst_done and bet_before_update is not None:
                    last_step = bet_before_update - current_bet  # un solo tocco: e' il gradino vero
                if burst_done and trusted_min_bet is not None and current_bet <= trusted_min_bet:
                    # Dopo una corsa la puntata e' scesa fino al minimo gia' verificato
                    # in passato: pavimento confermato, non serve un'altra corsa.
                    stable_reads = self.config.stable_reads_for_min

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
                    self._remember_min(slot_name, current_bet)
                    return Result.ok(BetResult(
                        slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                        min_bet_verified=True, attempts=attempts, strategy_sequence=strategy_sequence,
                        aborted_reason=None, outcome="min_bet_confirmed",
                    ))
            elif not verified and not is_trusted and self.locator.is_exhausted(candidate.strategy_name, _ELEMENT):
                consecutive_decreases = 0
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
                if self.menu_selector is not None and not menu_tried and not we_opened_the_menu:
                    # Il tasto meno non funziona (o non esiste): prima di
                    # scomodare un umano, prova il menu puntata (icona monete).
                    menu_tried = True
                    menu_outcome, menu_open = self._select_via_menu(
                        device_id, slot_name, episode_id, context, context_provider,
                        starting_bet, strategy_sequence, attempts + 1,
                    )
                    if menu_outcome is not None:
                        return menu_outcome
                    if menu_open:
                        # Menu a righe con pulsanti -/+: aperto, si prosegue
                        # col solito ciclo dei pulsanti meno, stavolta li' dentro.
                        we_opened_the_menu = menu_route_used = True
                        self.locator.reset_failures(_ELEMENT)
                        refreshed = context_provider()
                        if refreshed.success:
                            context = refreshed.data
                        continue
                refreshed = context_provider()
                if refreshed.success:
                    context = refreshed.data
                continue
            else:
                stable_reads += 1
                consecutive_decreases = 0
                if burst_done and outcome == "no_effect":
                    # Una corsa intera di tocchi senza nessuna discesa vale piu' delle
                    # letture di stabilita': il pavimento e' confermato.
                    stable_reads = max(stable_reads, self.config.stable_reads_for_min)

            remembered_floor = (
                trusted_min_bet is not None and current_bet is not None and current_bet <= trusted_min_bet
                and self.locator.is_trusted_strategy(candidate.strategy_name)
            )
            if stable_reads >= (1 if remembered_floor else self.config.stable_reads_for_min):
                already_at_known_min = (
                    trusted_min_bet is not None and current_bet is not None and current_bet <= trusted_min_bet
                ) or remembered_floor
                operator_involved = "hitl" in strategy_sequence
                if not self.config.floor_probe_enabled:
                    # Comportamento storico: una discesa letta basta.
                    min_bet_verified = any_decrease_seen or already_at_known_min
                    proof = "discesa letta (prova del pavimento spenta)"
                else:
                    # Il minimo di una slot va DIMOSTRATO: una discesa letta dall'OCR non basta (una
                    # cifra persa o un controllo che cicla i valori ne fabbricano una finta). Serve un
                    # riscontro indipendente: minimo curato/ricordato, operatore, oppure la prova
                    # "+ poi -" che il "-" funziona davvero.
                    proven = already_at_known_min or minus_proven_alive or operator_involved
                    if (
                        not proven and not probe_done and outcome_data is not None
                        and current_bet is not None
                    ):
                        probe_done = True
                        probe = self._probe_floor(
                            device_id, slot_name, context, candidate, outcome_data, current_bet, context_provider,
                        )
                        if probe.status == "floor":
                            proven = True
                        elif probe.status == "stuck":
                            return Result.ok(BetResult(
                                slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                                min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
                                aborted_reason="STUCK_ON_UNEXPECTED_SCREEN", outcome="aborted",
                            ))
                        elif probe.status == "continue":
                            current_bet = lowest_bet_seen = probe.bet
                            any_decrease_seen = probe.minus_alive
                            minus_proven_alive = minus_proven_alive or probe.minus_alive
                            consecutive_decreases = 1 if probe.minus_alive else 0
                            stable_reads = 0
                            if probe.minus_dead:
                                self.locator.exhaust_strategy(candidate.strategy_name, _ELEMENT)
                                self.locator.reject_position(_ELEMENT, candidate.x, candidate.y)
                            refreshed = context_provider()
                            if refreshed.success:
                                context = refreshed.data
                            continue
                    min_bet_verified = proven
                    proof = (
                        "minimo curato/ricordato" if already_at_known_min
                        else "prova del pavimento (+ poi -)" if minus_proven_alive or proven and not operator_involved
                        else "operatore" if operator_involved else "NESSUNA"
                    )
                logger.info(
                    "set_min_bet: evidenza del minimo: discese verificate=%d, profilo curato=%s, ricordato=%s, "
                    "operatore=%s, riscontro=%s -> verificato=%s",
                    decreases_seen, trusted_min_bet, remembered_min, operator_involved, proof, min_bet_verified,
                )
                outcome_label = "min_bet_confirmed" if any_decrease_seen else "unchanged_already_min"
                if not min_bet_verified and any_decrease_seen and self.config.floor_probe_enabled:
                    logger.error(
                        "Non riesco a dimostrare che %s e' la puntata minima: per sicurezza non spinno.", current_bet,
                    )
                logger.info(
                    "set_min_bet: plateau raggiunto (outcome=%s, min_bet_verified=%s, puntata finale=%s)",
                    outcome_label, min_bet_verified, current_bet,
                )
                self._close_bet_menu_if_opened(device_id, context, we_opened_the_menu)
                if min_bet_verified and menu_route_used:
                    save_autopilot_flag(slot_name, _BET_VIA_MENU_FLAG, True, self.config.slot_memory_dir)
                if min_bet_verified:
                    self._remember_min(slot_name, current_bet, trusted=bool(minus_proven_alive or operator_involved))
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                    min_bet_verified=min_bet_verified, attempts=attempts, strategy_sequence=strategy_sequence,
                    aborted_reason=(
                        None if min_bet_verified
                        else "MIN_NOT_PROVEN" if any_decrease_seen and self.config.floor_probe_enabled
                        else "NO_DECREASE_OBSERVED"
                    ),
                    outcome=outcome_label,
                ))

            reusable = (
                outcome_data is not None and outcome_data.state_after is not None
                and outcome_data.screenshot_after and outcome_data.outcome in ("success", "no_effect")
                and self.locator.is_trusted_strategy(candidate.strategy_name)
            )
            if reusable:
                # Lo schermo dopo il tocco e' gia' stato letto per verificarlo e
                # la prossima ricerca e' da fonte fidata (coordinata nota): niente
                # OCR in piu' (era ~10 s per ogni discesa).
                context = dataclasses.replace(
                    context, screenshot_path=outcome_data.screenshot_after,
                    slot_ocr_state=outcome_data.state_after,
                    raw_texts=outcome_data.state_after.get("raw_texts", []), text_boxes=[],
                )
            else:
                refreshed = context_provider()
                if refreshed.success:
                    context = refreshed.data

        logger.warning("set_min_bet: tentativi esauriti (%d) senza plateau", attempts)
        self._close_bet_menu_if_opened(device_id, context, we_opened_the_menu)
        return Result.ok(BetResult(
            slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
            min_bet_verified=False, attempts=attempts, strategy_sequence=strategy_sequence,
            aborted_reason="MAX_ATTEMPTS", outcome="aborted",
        ))

    def _probe_floor(
        self, device_id, slot_name, context, minus, minus_outcome, start_bet, context_provider,
    ) -> "_ProbeOutcome":
        """Prova che il "-" e' vivo quando la puntata non scende: "+" (deve salire) e poi
        "-" (deve tornare esattamente alla puntata di partenza). Se torna li', il "-"
        funziona e a quel valore non scendeva oltre: pavimento provato."""
        plus_result = self.locator.locate(_PLUS_ELEMENT, context, exclude_strategies=("hitl",))
        if not plus_result.success:
            logger.info("set_min_bet: prova del pavimento non possibile ('%s' non localizzabile).", _PLUS_ELEMENT)
            return _ProbeOutcome("inconclusive")
        plus = plus_result.data
        plus_trusted = self.locator.is_trusted_strategy(plus.strategy_name)

        plus_tap = self.verifier.verified_tap(
            device_id, plus.x, plus.y, Expectation(kind="bet_increase"),
            screenshot_before=minus_outcome.screenshot_after, state_before=minus_outcome.state_after,
            region_hint=context.amounts_band,
        )
        if not plus_tap.success or not plus_tap.data.verified:
            if not plus_trusted:
                self.locator.record_failure(plus.strategy_name, _PLUS_ELEMENT)
            if plus_tap.success and plus_tap.data.outcome == "unexpected_change":
                self.locator.reject_position(_PLUS_ELEMENT, plus.x, plus.y)
                if self.screen_recovery is not None:
                    recovery = self.screen_recovery.recover(
                        device_id, plus_tap.data.screenshot_before, (plus.x, plus.y), context_provider,
                        {"posta": plus_tap.data.bet_before, "saldo": plus_tap.data.balance_before},
                    )
                    if not recovery.success:
                        return _ProbeOutcome("stuck")
                    if recovery.data.method != "already_original":
                        self.locator.remember_rejected(slot_name, _PLUS_ELEMENT, plus.x, plus.y)
            logger.info("set_min_bet: prova del pavimento inconclusiva: il '+' non ha alzato la puntata.")
            return _ProbeOutcome("inconclusive")

        raised_bet = plus_tap.data.bet_after
        logger.info("set_min_bet: prova del pavimento: '+' ha alzato la puntata %s -> %s", start_bet, raised_bet)
        minus_tap = self.verifier.verified_tap(
            device_id, minus.x, minus.y, Expectation(kind="bet_decrease"),
            screenshot_before=plus_tap.data.screenshot_after, state_before=plus_tap.data.state_after,
            region_hint=context.amounts_band,
        )
        if not minus_tap.success or not minus_tap.data.verified:
            logger.warning("set_min_bet: dopo il '+' il '-' non abbassa la puntata: la coordinata e' da sostituire.")
            return _ProbeOutcome("continue", bet=raised_bet, minus_dead=True)

        back_bet = minus_tap.data.bet_after
        if back_bet == start_bet:
            logger.info(
                "set_min_bet: prova del pavimento riuscita: il '-' riporta la puntata a %s e prima non scendeva "
                "oltre: %s e' il minimo.", back_bet, start_bet,
            )
            self.locator.save_verified_candidate(
                _PLUS_ELEMENT, plus, slot_name, context.screen_width, context.screen_height,
            )
            return _ProbeOutcome("floor", bet=start_bet, minus_alive=True)
        logger.info("set_min_bet: dopo il '+' il '-' porta la puntata a %s (non a %s): riprendo la discesa.", back_bet, start_bet)
        return _ProbeOutcome("continue", bet=back_bet, minus_alive=True)

    def _run_size(self, current_bet, known_min, last_step) -> int:
        """Tocchi della corsa: con un minimo noto e un gradino osservato bastano quelli che
        servono (gradino dimezzato per prudenza, +2 di margine, mai meno di 8 ne' piu' di
        bet_burst_taps). Se e' sottostimata, la corsa dopo continua: solo un giro in piu'."""
        if known_min is None or last_step is None or last_step <= 0 or current_bet is None:
            return self.config.bet_burst_taps
        needed = math.ceil((current_bet - known_min) / (last_step / 2)) + 2
        return min(self.config.bet_burst_taps, max(8, needed))

    def _blind_taps(self, device_id: str, x: int, y: int, count: int, reference_screenshot=None) -> int:
        """Tocchi consecutivi sulla stessa coordinata senza verificare ciascuno. Ogni
        `bet_burst_check_every` tocchi uno screenshot (nessuna OCR) confrontato con lo
        schermo precedente alla corsa: se e' cambiato molto (popup, altra schermata)
        la corsa si ferma. Ritorna i tocchi eseguiti."""
        logger.info("set_min_bet: corsa di %d tocchi a (%d,%d) prima della verifica", count + 1, x, y)
        done = 0
        for index in range(count):
            self.verifier.touch_injector.tap_coordinates(device_id, x, y)
            done += 1
            time.sleep(self.config.bet_burst_interval_seconds)
            if reference_screenshot and done % self.config.bet_burst_check_every == 0 and done < count:
                if self._screen_changed_a_lot(device_id, reference_screenshot):
                    logger.warning("set_min_bet: lo schermo e' cambiato durante la corsa (dopo %d tocchi): mi fermo.", done)
                    break
        return done

    def _screen_changed_a_lot(self, device_id: str, reference_screenshot: str) -> bool:
        try:
            shot = self.verifier.screen_reader._screenshot(device_id)
            if not shot.success:
                return False
            diff = diff_score(reference_screenshot, shot.data)
            return bool(diff.success and diff.data >= self.config.bet_burst_abort_diff)
        except Exception:
            return False

    def _remembered_min(self, slot_name: str) -> Optional[Decimal]:
        """Suggerimento (mai una prova): l'ultimo minimo visto, che non sale mai. Serve a dimensionare i tocchi."""
        return self._memory_decimal(slot_name, _MIN_BET_VALUE)

    def _trusted_min(self, slot_name: str) -> Optional[Decimal]:
        """Minimo DIMOSTRATO (dichiarato con --min-bet, confermato dall'operatore, provato con "+ poi -" o con la
        ricerca per scorrimento completa). Solo questo puo' far dire "sono gia' al minimo" senza verificare."""
        values = [v for v in (self.config.known_min_bet, self._memory_decimal(slot_name, _MIN_BET_TRUSTED_VALUE))
                  if v is not None]
        return min(values) if values else None

    def _memory_decimal(self, slot_name: str, key: str) -> Optional[Decimal]:
        raw = load_autopilot_value(slot_name, key, self.config.slot_memory_dir)
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except Exception:
            return None

    def _remember_min(self, slot_name: str, bet: Optional[Decimal], trusted: bool = False) -> None:
        """Il suggerimento non sale mai (min tra vecchio e nuovo); il minimo dimostrato si scrive solo con
        trusted=True (e anche quello non sale)."""
        if bet is None:
            return
        for key, allowed in ((_MIN_BET_VALUE, True), (_MIN_BET_TRUSTED_VALUE, trusted)):
            if not allowed:
                continue
            old = self._memory_decimal(slot_name, key)
            value = bet if old is None else min(old, bet)
            result = save_autopilot_value(slot_name, key, str(value), self.config.slot_memory_dir)
            if not result.success:
                logger.warning("Impossibile salvare il minimo appreso: %s", result.error)

    def _apply_declared_min(self, slot_name: str) -> None:
        """--min-bet: il minimo dichiarato dall'operatore sostituisce anche un ricordo sporco (piu' alto)."""
        declared = self.config.known_min_bet
        if declared is None or slot_name in self._declared_applied:
            return
        self._declared_applied.add(slot_name)
        for key in (_MIN_BET_VALUE, _MIN_BET_TRUSTED_VALUE):
            save_autopilot_value(slot_name, key, str(declared), self.config.slot_memory_dir)
        logger.info("set_min_bet: minimo dichiarato dall'operatore per '%s': %s EUR (memoria aggiornata).",
                    slot_name, declared)

    def _select_via_menu(
        self, device_id, slot_name, episode_id, context, context_provider,
        starting_bet, strategy_sequence, attempts, allow_operator: bool = True,
    ) -> tuple:
        """Fallback: puntata piu' bassa dal menu. Ritorna (risultato, menu_aperto):
        (Result finale, _) se e' riuscito e confermato o se bisogna fermarsi;
        (None, True) se il menu e' aperto ma non e' una lista (si usano i pulsanti
        meno li' dentro); (None, False) se non ha funzionato (si prosegue con la
        catena normale, fino all'operatore)."""
        result = self.menu_selector.select_lowest(
            device_id, slot_name, context, context_provider, allow_operator=allow_operator,
            target=self._trusted_min(slot_name),
        )
        if not result.success:
            logger.warning("set_min_bet: menu puntata non riuscito (%s): %s", result.error_code, result.error)
            if result.error_code == "MENU_OPEN_NOT_A_LIST":
                return None, True
            if result.error_code == "MENU_SELECTION_UNREADABLE":
                # La cella e' stata scelta ma la puntata non si legge: nessun altro tocco e nessuna ricerca
                # costosa del "-": senza leggere la puntata non si puo' dimostrare nulla.
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=None,
                    min_bet_verified=False, attempts=attempts, strategy_sequence=list(strategy_sequence),
                    aborted_reason="MENU_BET_UNREADABLE", outcome="aborted",
                )), False
            if result.error_code == "MENU_STUCK":
                # Un tocco sbagliato ha cambiato lo schermo e non si e' riusciti
                # a ripristinarlo: niente altri tocchi alla cieca.
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=starting_bet,
                    min_bet_verified=False, attempts=attempts, strategy_sequence=list(strategy_sequence),
                    aborted_reason="STUCK_ON_UNEXPECTED_SCREEN", outcome="aborted",
                )), False
            return None, False
        known_floor = [v for v in (self._trusted_min(slot_name), self._remembered_min(slot_name)) if v is not None]
        if known_floor and result.data.final_bet > min(known_floor):
            logger.warning(
                "set_min_bet: dal menu risulta %s ma questa slot e' scesa fino a %s: non e' il minimo vero, "
                "non lo do' per verificato.", result.data.final_bet, min(known_floor),
            )
            return None, False
        return self._menu_result(
            device_id, slot_name, episode_id, starting_bet, result.data,
            list(strategy_sequence) + [result.data.toggle_strategy, "menu"], attempts,
        ), False

    def _menu_result(self, device_id, slot_name, episode_id, starting_bet, selection, strategy_sequence, attempts):
        """Esito finale di una puntata scelta dal menu: minimo = il piu' basso elencato, letto dallo schermo."""
        save_autopilot_flag(slot_name, _BET_VIA_MENU_FLAG, True, self.config.slot_memory_dir)
        self._remember_min(slot_name, selection.final_bet, trusted=selection.proven)
        logger.info("set_min_bet: puntata minima dal menu: %s EUR (verificata sulla lettura dello schermo)",
                    selection.final_bet)
        self._log_step(
            episode_id, attempts, slot_name, device_id, "menu", None,
            starting_bet, selection.final_bet, outcome="success", verified=True,
        )
        return Result.ok(BetResult(
            slot_name=slot_name, starting_bet=starting_bet, final_bet=selection.final_bet,
            min_bet_verified=True, attempts=attempts, strategy_sequence=list(strategy_sequence),
            aborted_reason=None, outcome="min_bet_confirmed",
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

    def _locate_before_operator(self, context):
        """locate(_ELEMENT) con Sol come supervisore PRIMA dell'operatore: se la catena senza operatore fallisce, Sol
        guarda lo schermo (puntata gia' al minimo? menu non voluto aperto?). Ritorna il Result del locate oppure un
        Assist se il supervisore ha risolto o cambiato qualcosa. Senza supervisore e' il locate di sempre."""
        if self.supervisor is None:
            return self.locator.locate(_ELEMENT, context)
        result = self.locator.locate(_ELEMENT, context, exclude_strategies=("hitl",))
        if result.success or result.error_code == "GPT_LOCATOR_UNAVAILABLE":
            return result
        verdict = self.supervisor.assist(context.device_id, self._known_min(context))
        if verdict.outcome != "nothing":
            return verdict
        only_operator = tuple(name for name in self.locator._STRATEGY_ORDER if name != "hitl")
        return self.locator.locate(_ELEMENT, context, exclude_strategies=only_operator)

    def _known_min(self, context) -> Optional[Decimal]:
        profile = context.profile
        if profile is not None and profile.verified and profile.known_min_bet is not None:
            return Decimal(str(profile.known_min_bet))
        return self._trusted_min(context.slot_name)

    def _supervisor_confirmed_min(
        self, slot_name, episode_id, device_id, starting_bet, final_bet, strategy_sequence, attempts,
        any_decrease_seen, by_operator: bool = False,
    ) -> Result[BetResult]:
        """La puntata e' al minimo per riscontro di Sol (valore letto <= minimo noto) o dell'operatore."""
        sequence = list(strategy_sequence)
        if "supervisor" not in sequence and not by_operator:
            sequence.append("supervisor")
        if final_bet is not None:
            self._remember_min(slot_name, final_bet, trusted=by_operator)
        logger.info("set_min_bet: puntata minima confermata (%s) senza altri tocchi: %s.",
                    "operatore" if by_operator else "supervisore", final_bet)
        self._log_step(
            episode_id, attempts, slot_name, device_id, "supervisor" if not by_operator else "hitl", None,
            starting_bet, final_bet, outcome="success", verified=True,
        )
        return Result.ok(BetResult(
            slot_name=slot_name, starting_bet=starting_bet, final_bet=final_bet,
            min_bet_verified=True, attempts=attempts, strategy_sequence=sequence,
            aborted_reason=None, outcome="min_bet_confirmed" if any_decrease_seen else "unchanged_already_min",
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
