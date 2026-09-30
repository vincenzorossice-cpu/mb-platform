import dataclasses
import logging
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
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.screen_recovery import ScreenRecovery
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, Expectation
from orchestrator.modules.step_logger.interface import StepLoggerInterface, StepRecord

logger = logging.getLogger(__name__)

_ELEMENT = "bet_minus"
_MENU_TOGGLE_ELEMENT = "bet_menu_toggle"
_BET_VIA_MENU_FLAG = "bet_via_menu"
_MIN_BET_VALUE = "min_bet"


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
                 screen_recovery: Optional[ScreenRecovery] = None):
        self.locator = locator
        self.verifier = verifier
        self.step_logger = step_logger
        self.config = config
        # None = il fallback "menu puntata" e' spento (comportamento storico).
        self.menu_selector = menu_selector
        # None = nessun recupero da schermate aperte per errore (storico).
        self.screen_recovery = screen_recovery

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
        # Minimo verificato in una sessione precedente (auto-appreso): un indizio che,
        # insieme a un tocco sul "-" che non scende, verifica il minimo. NON e' il
        # riscontro fidato di un profilo curato e non salta mai i tocchi da solo.
        remembered_min = self._remembered_min(slot_name)
        logger.info(
            "set_min_bet: puntata iniziale=%s, riscontro fidato=%s, minimo ricordato=%s",
            starting_bet, trusted_min_bet, remembered_min,
        )

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

        while attempts < self.config.max_bet_lower_attempts:
            attempts += 1
            outcome_data = None
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
                # Corsa: il "-" ha gia' funzionato una volta da una fonte fidata; il
                # minimo e' un pavimento (non si puo' "passarlo"), quindi N-1 tocchi
                # ciechi e UN solo tocco verificato, il cui "prima" e' lo schermo
                # precedente alla corsa.
                burst_done = False
                if (
                    self.config.bet_burst_taps > 1 and consecutive_decreases >= 1
                    and current_bet is not None and self.locator.is_trusted_strategy(candidate.strategy_name)
                ):
                    self._blind_taps(
                        device_id, candidate.x, candidate.y, self.config.bet_burst_taps - 1,
                        context.screenshot_path,
                    )
                    burst_done = True
                # Il "prima" e' lo schermo gia' letto nel contesto: niente OCR in piu'.
                tap_result = self.verifier.verified_tap(
                    device_id, candidate.x, candidate.y, Expectation(kind="bet_decrease"),
                    screenshot_before=context.screenshot_path, state_before=context.slot_ocr_state,
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
                consecutive_decreases += 1
                if burst_done and remembered_min is not None and current_bet <= remembered_min:
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
                remembered_min is not None and current_bet is not None and current_bet <= remembered_min
                and self.locator.is_trusted_strategy(candidate.strategy_name)
            )
            if stable_reads >= (1 if remembered_floor else self.config.stable_reads_for_min):
                already_at_known_min = (
                    trusted_min_bet is not None and current_bet is not None and current_bet <= trusted_min_bet
                ) or remembered_floor
                min_bet_verified = any_decrease_seen or already_at_known_min
                outcome_label = "min_bet_confirmed" if any_decrease_seen else "unchanged_already_min"
                logger.info(
                    "set_min_bet: plateau raggiunto (outcome=%s, min_bet_verified=%s, puntata finale=%s)",
                    outcome_label, min_bet_verified, current_bet,
                )
                self._close_bet_menu_if_opened(device_id, context, we_opened_the_menu)
                if min_bet_verified and menu_route_used:
                    save_autopilot_flag(slot_name, _BET_VIA_MENU_FLAG, True, self.config.slot_memory_dir)
                if min_bet_verified:
                    self._remember_min(slot_name, current_bet)
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=current_bet,
                    min_bet_verified=min_bet_verified, attempts=attempts, strategy_sequence=strategy_sequence,
                    aborted_reason=None if min_bet_verified else "NO_DECREASE_OBSERVED",
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
        raw = load_autopilot_value(slot_name, _MIN_BET_VALUE, self.config.slot_memory_dir)
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except Exception:
            return None

    def _remember_min(self, slot_name: str, bet: Optional[Decimal]) -> None:
        if bet is None:
            return
        result = save_autopilot_value(slot_name, _MIN_BET_VALUE, str(bet), self.config.slot_memory_dir)
        if not result.success:
            logger.warning("Impossibile salvare il minimo appreso: %s", result.error)

    def _select_via_menu(
        self, device_id, slot_name, episode_id, context, context_provider,
        starting_bet, strategy_sequence, attempts,
    ) -> tuple:
        """Fallback: puntata piu' bassa dal menu. Ritorna (risultato, menu_aperto):
        (Result finale, _) se e' riuscito e confermato o se bisogna fermarsi;
        (None, True) se il menu e' aperto ma non e' una lista (si usano i pulsanti
        meno li' dentro); (None, False) se non ha funzionato (si prosegue con la
        catena normale, fino all'operatore)."""
        result = self.menu_selector.select_lowest(device_id, slot_name, context, context_provider)
        if not result.success:
            logger.warning("set_min_bet: menu puntata non riuscito (%s): %s", result.error_code, result.error)
            if result.error_code == "MENU_OPEN_NOT_A_LIST":
                return None, True
            if result.error_code == "MENU_STUCK":
                # Un tocco sbagliato ha cambiato lo schermo e non si e' riusciti
                # a ripristinarlo: niente altri tocchi alla cieca.
                return Result.ok(BetResult(
                    slot_name=slot_name, starting_bet=starting_bet, final_bet=starting_bet,
                    min_bet_verified=False, attempts=attempts, strategy_sequence=list(strategy_sequence),
                    aborted_reason="STUCK_ON_UNEXPECTED_SCREEN", outcome="aborted",
                )), False
            return None, False
        selection = result.data
        save_autopilot_flag(slot_name, _BET_VIA_MENU_FLAG, True, self.config.slot_memory_dir)
        logger.info("set_min_bet: puntata minima dal menu: %s EUR (verificata sulla lettura dello schermo)",
                    selection.final_bet)
        self._log_step(
            episode_id, attempts, slot_name, device_id, "menu", None,
            starting_bet, selection.final_bet, outcome="success", verified=True,
        )
        return Result.ok(BetResult(
            slot_name=slot_name, starting_bet=starting_bet, final_bet=selection.final_bet,
            min_bet_verified=True, attempts=attempts,
            strategy_sequence=list(strategy_sequence) + [selection.toggle_strategy, "menu"],
            aborted_reason=None, outcome="min_bet_confirmed",
        )), False

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
