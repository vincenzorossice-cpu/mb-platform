import logging
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import forget_autopilot_element
from orchestrator.modules.slot_autopilot.config import (
    QUICK_STOP_MAX_GAP, QUICK_STOP_RETRY_DELAY, AutoPilotConfig, quick_stop_delay,
)
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.value_parser import parse_amount
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, CalmResult, Expectation, classify_screen_state
from orchestrator.modules.step_logger.interface import StepLoggerInterface, StepRecord

logger = logging.getLogger(__name__)
quick_log = logging.getLogger("orchestrator.quick_stop")     # sulla console (vedi _CONSOLE_INFO_LOGGERS in run_autopilot)

_ELEMENT = "spin"
_MAX_NORMAL_DURATION_SECONDS = 20.0   # oltre, e' un bonus/feature: non conta per la durata tipica
_MAX_FIRST_SAMPLE_SECONDS = 12.0      # primi campioni: sopra e' quasi certamente una vincita/bonus
_MAX_INITIAL_WAIT_SECONDS = 8.0
_QUICK_USE_WAIT = 1.5         # attesa dopo il secondo tocco quando si usa il Quick Stop
_QUICK_TEST_WAIT = 1.5        # nel test: attesa dopo il secondo tocco prima di guardare saldo e rulli
_BUSY_SAMPLE_DELAY_SECONDS = 1.0  # quanto dopo il tocco si campiona il pulsante "occupato"
_SPIN_ZONE_HALF_PX = 160          # quadrato di ~320 px attorno al pulsante spin
_BOTTOM_STRIP_FRACTION = 0.12     # ultimo 12% dell'altezza: saldo, puntata, vincita
_FIRST_SPIN_CALM_SECONDS = 5.0    # prima del primo spin non c'e' vincita ne' bonus: la calma non si aspetta a lungo
_REELS_STILL_SECONDS = 6.0        # ultimo ripiego (senza freccia): i soli rulli, fermi, bastano a toccare
_REELS_ANNOTATE_SECONDS = 3.0     # a schermo bloccato: i rulli sono fermi o si muovono? (decide se serve il Vigile)


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
    outcome: str  # "success" | "blocked" | "no_effect" | "error" | "operator_action" (non e' uno spin) | "slot_lost" | "bonus_event" (accredito, non uno spin)
    violated_invariant: Optional[str] = None  # "MIN_BET_NOT_VERIFIED" | "INSUFFICIENT_BALANCE" | "UNSAFE_SCREEN_STATE"
    # Quale controllo ha bloccato (solo per UNSAFE_SCREEN_STATE): "schermo non fermo" | "saldo/puntata non leggibili"
    blocked_detail: Optional[str] = None
    # Secondi per fase di questo spin: contesto, calma, tocco, attesa (con scatti), lettura, altro, totale.
    timings: Optional[dict] = None
    # Solo nello spin in cui si e' tentato di apprendere il pulsante: com'e' andata (appreso / perche' no).
    button_status: Optional[str] = None
    accounting_anomaly: Optional[str] = None
    strategy_name: Optional[str] = None  # strategia che ha localizzato lo spin (None se non tappato)
    # Schermo stabile dopo lo spin e relativa lettura OCR (da TapOutcome): chi deve
    # iniziare lo spin successivo puo' riusarli invece di rifare la lettura.
    screenshot_after: Optional[str] = None
    state_after: Optional[dict] = None
    # Il saldo e' sceso di molto piu' della puntata creduta: la puntata reale e' piu' alta.
    bet_understated: bool = False
    # Il tocco ha cambiato lo schermo ma non c'e' stato addebito (bonus intro, free spin, animazione): chi chiama non
    # deve toccare di nuovo lo spin finche' non ha capito cosa succede.
    anomaly: bool = False
    # La cella di puntata selezionata (letta a set_min_bet) non ha piu' lo stesso aspetto: la puntata e' stata cambiata.
    bet_cell_changed: bool = False
    # Come e' andato il Quick Stop in questo spin: "test" (due tocchi di prova, lo schermo dopo e' in animazione),
    # "use" o "off". Bet Watch non guarda mai uno spin di test.
    quick_mode: str = "off"


class SpinController:
    """Esegue uno spin solo dopo aver verificato 3 invarianti di sicurezza, in
    quest'ordine: puntata minima verificata, saldo sufficiente, schermo
    'idle'. Il controllo contabile post-spin non e' MAI bloccante: un'anomalia
    viene solo annotata, non trasformata in un fallimento."""

    def __init__(self, locator: ElementLocator, verifier: ActionVerifier,
                 step_logger: StepLoggerInterface, config: AutoPilotConfig, button=None):
        self.locator = locator
        self.verifier = verifier
        self.step_logger = step_logger
        self.config = config
        # SpinButtonState opzionale: senza, solo il confronto tra screenshot (comportamento storico).
        self.button = button
        self._button_logged = set()        # righe sul pulsante spin gia' scritte (una volta per slot e motivo)
        self._durations = {}               # slot -> durate recenti (s) tra tocco e fine animazione, spin normali
        # Fino a quando (monotonic) il prossimo spin puo' saltare il controllo di calma: lo spin precedente e'
        # finito senza vincita, lo schermo fermo e' recente.
        self._calm_skip_deadline = 0.0
        # Opzionale: lost_check(device) -> 'in_game' | 'lost' | 'unknown' (SlotSupervisor.check_in_game). Se il pulsante
        # spin non si trova, prima dell'operatore si controlla che la slot sia ancora sullo schermo.
        self.lost_check = None
        # Safety net sulla puntata: allarmi (calo oltre il limite), falsi positivi (la rilettura era coerente),
        # confermati. Servono a vedere se i falsi positivi calano.
        self.recheck_stats = {"alarms": 0, "false_positives": 0, "confirmed": 0}
        self._last_normal_spin = None   # (spin_index, saldo prima, saldo dopo, puntata) dell'ultimo spin riuscito
        self.arrow_stats = {"arrow": 0, "still": 0, "with_reference": False, "shots": 0, "spins": 0}
        # Quick Stop (secondo tocco sullo spin per fermare l'animazione). quick_stop_state: oggetto con
        # quick_stop(slot) -> True/False/None e set_quick_stop(slot, abilitato, motivo) (SlotSetup). Senza, e' spento.
        self.quick_stop_state = None
        self.quick_stop_suspended = False           # la sessione lo sospende durante i free spin
        self._quick_seen_slots = set()              # il test si fa solo al primo spin della sessione
        self._quick_retry = set()                   # slot il cui test a QUICK_STOP_DELAY e' "ignorato": riprova a 0.3 s
        self._quick_noted = set()                   # (slot, motivo) gia' scritti in console
        self._quick_retrying = set()                # slot con la riprova a 0.3 s in corso (una sola volta)
        self._quick_delay_used = {}                 # slot -> delay con cui il Quick Stop e' stato provato con successo
        self.quick_stats = {"state": "untested", "reason": "", "spins": 0, "seconds": 0.0}
        self._worked_points = {}           # slot -> punti dello spin che hanno gia' addebitato (mai da dimenticare)
        # Opzionale: verified_spin_point(slot) -> (x, y) | None, il punto dello spin verificato nella mappa.
        self.verified_spin_point = None
        self._anomaly = {}                 # slot -> True dopo "schermo cambiato senza addebito" (animazione/bonus in corso?)
        self._dead_spin_points = {}        # slot -> ((x, y), volte di fila) tocchi che non hanno mai addebitato
        self._spins_done = {}              # slot -> spin con un tocco vero (senza non c'e' vincita ne' bonus da aspettare)

    def spin(
        self, device_id: str, slot_name: str, episode_id: str, spin_index: int,
        context_provider: Callable[[], Result], min_bet_verified: bool,
        expected_bet: Optional[Decimal], force: bool = False,
    ) -> Result[SpinResult]:
        result = self._spin_once(device_id, slot_name, episode_id, spin_index, context_provider, min_bet_verified,
                                 expected_bet, force)
        data = result.data if result.success else None
        if data is not None and isinstance(data.attempts, int) and data.attempts > 0 and data.outcome != "blocked":
            self._spins_done[slot_name] = self._spins_done.get(slot_name, 0) + 1
        return result

    def _spin_once(
        self, device_id: str, slot_name: str, episode_id: str, spin_index: int,
        context_provider: Callable[[], Result], min_bet_verified: bool,
        expected_bet: Optional[Decimal], force: bool = False,
    ) -> Result[SpinResult]:
        """force=True (solo dopo che Sol ha giudicato il gioco pronto): salta il controllo di calma e l'invariante
        'schermo libero'; restano puntata minima verificata e saldo sufficiente."""
        started = time.monotonic()
        skip_calm_until, self._calm_skip_deadline = self._calm_skip_deadline, 0.0
        calm_seconds = 0.0
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

        violated = self._check_invariants(min_bet_verified, balance_before, expected_bet, context, force)
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
                blocked_detail="saldo/puntata non leggibili" if violated == "UNSAFE_SCREEN_STATE" else None,
            ))

        if callable(self.lost_check):
            locate_result = self.locator.locate(_ELEMENT, context, exclude_strategies=("hitl",))
            if not locate_result.success and self.lost_check(device_id) == "lost":
                logger.warning("spin %d: il pulsante spin non c'e' e la slot non e' piu' sullo schermo.", spin_index)
                self._log_step(
                    episode_id, spin_index, slot_name, device_id, None, None,
                    balance_before, balance_before, outcome="slot_lost",
                )
                return Result.ok(SpinResult(
                    slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                    balance_before=balance_before, balance_after=balance_before, win_amount=None,
                    verified=False, attempts=0, outcome="slot_lost",
                ))
            if not locate_result.success:
                locate_result = self.locator.locate(_ELEMENT, context)   # ora anche l'operatore
        else:
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

        ready_check = None
        learning_button = False
        detection_on = self.config.spin_button_detection and os.environ.get(
            "SPIN_BUTTON_DETECTION", "1").strip().lower() not in ("0", "false", "no")
        if self.button is None:
            self._log_button_once(slot_name, "nessun oggetto", logging.INFO, "disattivato: nessun rilevatore collegato")
        elif not detection_on:
            self._log_button_once(slot_name, "spento", logging.INFO,
                                  "disattivato (spin_button_detection=False o SPIN_BUTTON_DETECTION=0)")
        elif candidate.verified:
            self._log_button_once(slot_name, "operatore", logging.INFO,
                                  "non usato: la coordinata dello spin arriva dall'operatore")
        else:
            screen_size = (context.screen_width, context.screen_height)
            if self.button.reference(slot_name, screen_size) is not None:
                self._log_button_once(slot_name, "riferimento", logging.INFO,
                                      f"riferimento trovato (dimensione schermo {screen_size}): lo uso")

                def ready_check(path, _x=candidate.x, _y=candidate.y, _size=screen_size):
                    return self.button.classify(slot_name, _size, path, _x, _y)
            elif not context.screenshot_path:
                self._log_button_once(slot_name, "senza screenshot", logging.WARNING,
                                      "apprendimento saltato: il contesto non ha uno screenshot")
            elif self.button.needs_learning(slot_name, screen_size):
                learning_button = True
                self._log_button_once(slot_name, "apprendimento", logging.INFO,
                                      "nessun riferimento: lo apprendo in questo spin (campione 'pronto' ora, "
                                      "'occupato' ~1 s dopo il tocco)")
            else:
                self._log_button_once(
                    slot_name, "rinunciato", logging.WARNING,
                    "apprendimento saltato: riferimento mancante e tentativi gia' esauriti; uso il confronto tra "
                    "screenshot (SPIN_BUTTON_RELEARN=1 per riprovare)")
        calm_skippable = (
            self.config.spin_skip_calm_after_loss and time.monotonic() <= skip_calm_until
        )
        if calm_skippable:
            logger.debug("spin %d: lo spin precedente non ha vinto e lo schermo fermo e' recente: salto la calma.",
                         spin_index)
        if (
            self.config.spin_calm_check and not force and not calm_skippable
            and not candidate.verified and context.screenshot_path
        ):
            # Mai toccare uno schermo che si muove (animazione di vincita, saldo che si aggiorna): il tocco
            # andrebbe perso e ritoccare alla cieca rischia due spin.
            calm_started = time.monotonic()
            calm = None
            first_spin = not self._spins_done.get(slot_name)
            reels_state = None            # "fermi" | "in movimento" | None (non misurato)
            calm_limit = self.config.spin_calm_max_wait_seconds
            if first_spin:
                calm_limit = min(calm_limit, _FIRST_SPIN_CALM_SECONDS)
            if ready_check is not None:
                # La freccia dello spin e' la misura giusta (un overlay di vincita muove lo schermo anche a gioco pronto)
                calm = self.verifier.wait_until_button_ready(device_id, ready_check, calm_limit)
            if calm is None:
                calm = self.verifier.wait_until_calm(
                    device_id, context.screenshot_path, calm_limit,
                    regions=self._calm_regions(context, candidate) if self.config.spin_partial_calm else None,
                )
            if calm.success and not calm.data.calm and not first_spin and ready_check is None:
                # Animazioni ambientali (banner, jackpot, luci) che non si fermano mai e nessuna freccia da guardare:
                # contano solo i rulli, il resto dello schermo si ignora.
                region = self._wait_region(context)
                if region is not None:
                    logger.warning("spin %d: animazioni ambientali continue rilevate, uso frame parziale per spin.",
                                   spin_index)
                    reels = self.verifier.wait_until_reels_still(device_id, region, _REELS_STILL_SECONDS)
                    if reels.success:
                        reels_state = "fermi" if reels.data.calm else "in movimento"
                    if reels.success and reels.data.calm:
                        logger.info("spin %d: i rulli sono fermi (il resto dello schermo si muove): tocco.", spin_index)
                        calm = reels
            calm_seconds = time.monotonic() - calm_started
            if calm.success and not calm.data.calm and first_spin:
                logger.info("spin %d: schermo non fermo a causa di animazioni ambientali, procedo con il primo spin.",
                            spin_index)
                calm = Result.ok(CalmResult(calm.data.screenshot_path, calm=True, waited=calm.data.waited,
                                            seconds=calm.data.seconds))
            if calm.success and calm.data.partial:
                logger.info("spin %d: i rulli sono in animazione ma barra degli importi e pulsante sono fermi: "
                            "tocco (calma parziale).", spin_index)
            if calm.success and not calm.data.calm:
                violated = "UNSAFE_SCREEN_STATE"
                logger.info("spin %d: lo schermo non si ferma (animazione in corso?): non tocco.", spin_index)
                if reels_state is None and not first_spin:
                    region = self._wait_region(context)
                    if region is not None:
                        reels = self.verifier.wait_until_reels_still(device_id, region, _REELS_ANNOTATE_SECONDS)
                        if reels.success:
                            reels_state = "fermi" if reels.data.calm else "in movimento"
                detail = "schermo non fermo" + (f" (rulli {reels_state})" if reels_state else "")
                self._log_step(
                    episode_id, spin_index, slot_name, device_id, None, None,
                    balance_before, balance_before, outcome="blocked", notes=violated,
                )
                return Result.ok(SpinResult(
                    slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                    balance_before=balance_before, balance_after=balance_before, win_amount=None,
                    verified=False, attempts=0, outcome="blocked", violated_invariant=violated,
                    blocked_detail=detail,
                ))
            if calm.success and calm.data.waited:
                # Si e' aspettato: il saldo letto prima e' vecchio (una vincita puo' essere stata accreditata
                # nel frattempo e finirebbe nello spin sbagliato). Si rilegge.
                fresh = context_provider()
                if fresh.success:
                    context = fresh.data
                    balance_before = self._read_amount(context, "saldo")
                    violated = self._check_invariants(min_bet_verified, balance_before, expected_bet, context)
                    if violated is not None:
                        logger.info("spin %d bloccato da invariante di sicurezza: %s", spin_index, violated)
                        self._log_step(
                            episode_id, spin_index, slot_name, device_id, None, None,
                            balance_before, balance_before, outcome="blocked", notes=violated,
                        )
                        return Result.ok(SpinResult(
                            slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                            balance_before=balance_before, balance_after=balance_before, win_amount=None,
                            verified=False, attempts=0, outcome="blocked", violated_invariant=violated,
                            blocked_detail="saldo/puntata non leggibili" if violated == "UNSAFE_SCREEN_STATE" else None,
                        ))

        attempts = 0
        tap_seconds = wait_seconds = read_seconds = 0.0
        wait_captures = 0
        ended_ready = False
        anomaly = False
        last_wait_seconds = None
        busy_sample = {}
        screenshot_after = None
        state_after = None
        balance_after_read = False   # False: balance_after e' solo il valore "prima" usato come ripiego
        quick_mode = "off"
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
            hitl_action = self.locator.last_hitl_action() if candidate.strategy_name == "hitl" else None
            if isinstance(hitl_action, str) and (
                hitl_action != "spin_confirmed" or not balance_after_read or balance_after == balance_before
            ):
                # L'operatore ha toccato qualcosa che NON ha speso (es. ha abbassato la puntata): non e' uno spin,
                # non si conta il giocato e quel punto non diventa il pulsante spin.
                logger.warning(
                    "spin %d: l'intervento dell'operatore (%s) non e' uno spin (saldo %s -> %s): non lo conto e non "
                    "ricordo quel punto come pulsante spin.", spin_index, hitl_action, balance_before, balance_after,
                )
                self._log_step(
                    episode_id, spin_index, slot_name, device_id, candidate.strategy_name,
                    (candidate.x, candidate.y), balance_before, balance_after,
                    outcome="operator_action", notes=hitl_action,
                )
                return Result.ok(SpinResult(
                    slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                    balance_before=balance_before, balance_after=balance_after, win_amount=None,
                    verified=False, attempts=0, outcome="operator_action", strategy_name=candidate.strategy_name,
                ))
        else:
            verified = False
            balance_after = balance_before
            win_amount = None
            quick_mode = self._quick_mode(slot_name, force, candidate)
            quick = {}
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
                extra = {}
                initial_wait = self._initial_wait(slot_name)
                if initial_wait > 0:
                    extra["initial_wait"] = initial_wait
                typical = self._typical_duration(slot_name)
                if typical > 0:
                    extra["typical_duration"] = typical
                if ready_check is not None:
                    extra["ready_check"] = ready_check
                else:
                    wait_region = self._wait_region(context)
                    if wait_region is not None:
                        extra["wait_region"] = wait_region      # senza freccia: si guardano i rulli, non lo schermo intero
                if attempts == 1 and (learning_button or quick_mode != "off"):
                    def after_tap(_learn=learning_button, _mode=quick_mode, _x=candidate.x, _y=candidate.y):
                        tapped_at = time.monotonic()
                        if _mode != "off":
                            # Back-to-back: nessuno screenshot/OCR/campione tra i due tocchi (il campione "occupato"
                            # della freccia si salta in questo spin: ci pensano gli spin normali).
                            self._quick_second_tap(device_id, _x, _y, _mode, tapped_at, slot_name, quick)
                        elif _learn:
                            self._sample_busy_button(device_id, busy_sample)
                    extra["on_after_tap"] = after_tap
                    if quick_mode == "use":
                        extra["skip_wait"] = True       # niente attesa dello schermo fermo: un solo screenshot
                tap_result = self.verifier.verified_tap(
                    device_id, candidate.x, candidate.y,
                    Expectation(kind="balance_changed"),
                    screenshot_before=context.screenshot_path if reuse else None,
                    state_before=context.slot_ocr_state if reuse else None,
                    **extra,
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
                ended_ready = bool(getattr(tap_result.data, "ended_ready", False))
                last_wait_seconds = float(getattr(tap_result.data, "wait_seconds", 0.0) or 0.0) or None
                tap_seconds += tap_result.data.tap_seconds
                wait_seconds += tap_result.data.wait_seconds
                wait_captures += tap_result.data.wait_captures
                read_seconds += tap_result.data.read_seconds
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
                    self._dead_spin_points.pop(slot_name, None)
                    break
                if (
                    self.config.spin_calm_check and tap_result.data.outcome == "unexpected_change"
                    and self.locator.is_trusted_strategy(candidate.strategy_name)
                ):
                    # Schermo cambiato ma saldo uguale: o un'animazione (il tocco e' andato perso) o uno
                    # spin partito con il saldo non ancora scalato. Mai ritoccare alla cieca: si aspetta che
                    # lo schermo si fermi e si rilegge il saldo.
                    settled = self._settle_after_unexpected(
                        device_id, tap_result.data, context_provider, balance_before,
                    )
                    if settled is not None:
                        balance_after, screenshot_after, state_after = settled
                        balance_after_read = True
                        win_amount = None
                        verified = True
                        self.locator.record_success(candidate.strategy_name, _ELEMENT)
                        logger.info("spin %d: addebito visto dopo l'attesa (saldo %s -> %s): lo spin e' partito.",
                                    spin_index, balance_before, balance_after)
                    else:
                        logger.warning(
                            "spin %d: schermo cambiato ma nessun addebito dopo l'attesa: non ritocco "
                            "(potrebbe essere un'animazione); il prossimo spin riparte da uno schermo fermo. "
                            "Tocco a (%d,%d) via '%s'.",
                            spin_index, candidate.x, candidate.y, candidate.strategy_name,
                        )
                        self._note_dead_point(slot_name, candidate, spin_index)
                        self._anomaly[slot_name] = True
                        anomaly = True
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
        quick_explained = False
        if not candidate.verified and quick_mode != "off":
            quick_explained = self._quick_conclude(
                slot_name, quick_mode, quick, balance_before, balance_after if balance_after_read else None,
                expected_bet, started,
            )
        if outcome == "success" and balance_after_read and balance_before is not None:
            if balance_after < balance_before:
                self._anomaly.pop(slot_name, None)          # un addebito vero: si torna alla normalita'
                self._worked_points.setdefault(slot_name, set()).add((candidate.x, candidate.y))
            elif balance_after > balance_before and self._anomaly.get(slot_name):
                # Saldo SALITO subito dopo un'anomalia e senza alcun addebito visto: l'accredito di un bonus o di
                # un'animazione in corso, non uno spin pagato. Non si conta (nel dubbio meno giocato, mai di piu').
                logger.info(
                    "spin %d: Evento bonus: saldo salito %s -> %s dopo un'anomalia, senza addebito: non contato come spin.",
                    spin_index, balance_before, balance_after,
                )
                self._calm_skip_deadline = 0.0
                self._log_step(
                    episode_id, spin_index, slot_name, device_id, candidate.strategy_name,
                    (candidate.x, candidate.y), balance_before, balance_after, outcome="bonus_event",
                )
                return Result.ok(SpinResult(
                    slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                    balance_before=balance_before, balance_after=balance_after, win_amount=None,
                    verified=False, attempts=attempts, outcome="bonus_event",
                ))
        button_status = None
        if verified and learning_button:
            if busy_sample.get("path"):
                self.button.learn(
                    slot_name, (context.screen_width, context.screen_height), candidate.x, candidate.y,
                    context.screenshot_path, busy_sample["path"],
                )
                button_status = self.button.status(slot_name)
            elif quick_mode != "off":
                logger.info("pulsante spin: campione 'occupato' saltato in questo spin (Quick Stop back-to-back): "
                            "lo apprendo da uno spin normale.")
            else:
                button_status = "NON appreso: lo screenshot 'occupato' dopo il tocco non e' stato raccolto"
                logger.warning("pulsante spin: %s", button_status)
        if verified:
            self._worked_points.setdefault(slot_name, set()).add((candidate.x, candidate.y))
            self.arrow_stats["spins"] += 1
            self.arrow_stats["shots"] += wait_captures
            self.arrow_stats["arrow" if ended_ready else "still"] += 1
            self.arrow_stats["with_reference"] = self.arrow_stats["with_reference"] or ready_check is not None
        if verified and ended_ready:
            # La freccia e' tornata: se il prossimo spin parte subito non serve controllare di nuovo.
            self._calm_skip_deadline = time.monotonic() + self.config.spin_calm_skip_max_age_seconds
        if verified:
            # Come fa BetController: una coordinata verificata dal vivo e
            # trovata da GPT/HITL va ricordata, altrimenti ogni sessione la
            # ritrova da capo (save_verified_candidate ignora profile/memory).
            self.locator.save_verified_candidate(
                _ELEMENT, candidate, slot_name, context.screen_width, context.screen_height,
            )
        bet_understated = (outcome == "success" and not quick_explained
                           and self._bet_understated(balance_before, balance_after, expected_bet))
        if bet_understated:
            # Una sola lettura sbagliata (cifre perse, saldo non ancora aggiornato) non deve fermare la sessione:
            # doppia lettura di conferma con attesa; l'allarme scatta solo se le letture lo confermano.
            corrected = self._confirm_balance_drop(
                spin_index, balance_before, balance_after, expected_bet, context_provider,
                context.screenshot_path, screenshot_after,
            )
            if corrected is not None:
                balance_after = corrected
                if state_after is not None:   # il contesto dello spin dopo parte da questa lettura: niente valore sbagliato
                    state_after = {**state_after, "saldo": str(corrected)}
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
                if from_balance < Decimal("0.01"):
                    # Perso (saldo alla mano): il prossimo spin puo' saltare il controllo di calma se parte subito.
                    self._calm_skip_deadline = time.monotonic() + self.config.spin_calm_skip_max_age_seconds
        if bet_understated:
            logger.error(
                "spin %d: il saldo e' sceso di %s con una puntata creduta di %s: la puntata reale e' piu' alta.",
                spin_index, balance_before - balance_after, expected_bet,
            )
        if outcome == "success":
            self._last_normal_spin = (spin_index, balance_before, balance_after, expected_bet)
        accounting_anomaly = None   # il controllo per spin sulla vincita letta e' stato tolto: vedi sopra
        if outcome == "success" and not force and last_wait_seconds is not None and quick_mode == "off":
            self._record_duration(
                slot_name, last_wait_seconds, had_win=win_amount is not None and win_amount >= Decimal("0.01"),
            )
        total_seconds = time.monotonic() - started
        timings = {
            "contesto": round(context_seconds, 2), "calma": round(calm_seconds, 2), "tocco": round(tap_seconds, 2),
            "attesa": round(wait_seconds, 2), "scatti": wait_captures, "lettura": round(read_seconds, 2),
            "totale": round(total_seconds, 2),
        }
        timings["altro"] = round(max(0.0, total_seconds - context_seconds - calm_seconds - tap_seconds
                                     - wait_seconds - read_seconds), 2)
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
            quick_mode=quick_mode,
            timings=timings, button_status=button_status, anomaly=anomaly,
        ))

    # ---- Quick Stop ------------------------------------------------------------------------------------------
    def _quick_note(self, slot_name: str, text: str) -> None:
        """Una riga per slot e per motivo: il Quick Stop non resta mai muto."""
        key = (slot_name, text)
        if key not in self._quick_noted:
            self._quick_noted.add(key)
            quick_log.info("Quick Stop: %s", text)

    def _quick_mode(self, slot_name: str, force: bool, candidate) -> str:
        """'off' | 'test' (primo spin della sessione, mai provato) | 'use' (provato e funzionante)."""
        state = self.quick_stop_state
        why = None
        if os.environ.get("QUICK_STOP", "1").strip().lower() in ("0", "false", "no"):
            why = "spento da QUICK_STOP=0"
        elif state is None:
            why = "nessuna mappa della slot (GPT spento o non collegata)"
        elif force:
            why = "spin forzato dal vigile"
        elif candidate.verified:
            why = "il punto dello spin arriva dall'operatore"
        elif self.quick_stop_suspended:
            why = "free spin in corso"
        elif self._anomaly.get(slot_name):
            why = "anomalia sullo spin precedente"
        if why is not None:
            self._quick_note(slot_name, f"NON TESTATO ({why}).")
            return "off"
        known = state.quick_stop(slot_name)
        first_spin = slot_name not in self._quick_seen_slots
        self._quick_seen_slots.add(slot_name)
        if known is True:
            self.quick_stats["state"] = "enabled"
            self._quick_note(slot_name, "ABILITATO dalla mappa (provato in una sessione precedente).")
            return "use"
        if known is False:
            self.quick_stats.update(state="disabled", reason=self.quick_stats["reason"] or "gia' provato: non funziona")
            self._quick_note(slot_name, "DISABILITATO dalla mappa (provato in una sessione precedente: non funziona).")
            return "off"
        if slot_name in self._quick_retry:
            self._quick_retry.discard(slot_name)
            self._quick_retrying.add(slot_name)
            quick_log.info("Quick Stop test: AVVIO riprova (delay %.1f s).", QUICK_STOP_RETRY_DELAY)
            return "test"
        if first_spin:
            quick_log.info("Quick Stop test: AVVIO (primo spin, delay %.1f s).", self._quick_delay_for(slot_name, "test"))
            return "test"
        return "off"

    def _quick_delay_for(self, slot_name: str, mode: str) -> float:
        if mode == "test" and slot_name in self._quick_retrying:
            return QUICK_STOP_RETRY_DELAY
        return self._quick_delay_used.get(slot_name, quick_stop_delay())

    def _quick_second_tap(self, device_id: str, x: int, y: int, mode: str, tapped_at: float, slot_name: str,
                          holder: dict) -> None:
        """Dopo il primo tocco: dopo il delay (0.5 s di default, 0.3 s nella riprova) il secondo, senza nient'altro in
        mezzo. Poi, nel test, due screenshot (il saldo da quello di ~1.5 s dopo, prima delle vincite); nell'uso 1.5 s."""
        delay = self._quick_delay_for(slot_name, mode)
        time.sleep(max(0.0, delay - (time.monotonic() - tapped_at)))
        self.verifier.touch_injector.tap_coordinates(device_id, int(x), int(y))
        gap = time.monotonic() - tapped_at
        holder.update(second_tap=True, gap=gap, delay=delay)
        quick_log.info("Quick Stop: intervallo tra i tocchi %.2f s (delay %.1f s).", gap, delay)
        if mode == "use":
            time.sleep(_QUICK_USE_WAIT)
            return
        time.sleep(_QUICK_TEST_WAIT)
        shots = []
        for _ in range(2):
            shot = self.verifier.screen_reader._screenshot(device_id)
            shots.append(shot.data if shot.success else None)
        holder["s1"], holder["s2"] = shots

    def _quick_conclude(self, slot_name: str, mode: str, holder: dict, balance_before, balance_after, bet,
                        started: float) -> bool:
        """Esito del test (primo spin) o controllo dell'uso. True se il calo di saldo e' spiegato dal Quick Stop (doppio
        spin): chi chiama non lo tratta come "puntata sbagliata"."""
        if bet is None or bet <= 0 or balance_before is None:
            return False
        if mode == "use":
            self.quick_stats["spins"] += 1
            self.quick_stats["seconds"] += time.monotonic() - started
            if balance_after is not None and self._bet_understated(balance_before, balance_after, bet):
                if self._beyond_double_spin(balance_before, balance_after, bet):
                    # Due tocchi a questa puntata costano al massimo il doppio: oltre, la puntata a schermo e' cambiata.
                    quick_log.warning(
                        "Calo di saldo %s oltre il doppio della puntata %s: non e' un doppio tocco, la puntata e' "
                        "cambiata (Quick Stop resta attivo, rileggo la puntata).",
                        balance_before - balance_after, bet)
                    return False
                quick_log.warning("Quick Stop disabilitato: doppio addebito rilevato (saldo %s -> %s con puntata %s).",
                               balance_before, balance_after, bet)
                self._set_quick(slot_name, False, "doppio addebito rilevato")
                return True
            return False
        # ---- test (primo spin, o riprova a 0.3 s) ----
        retrying = slot_name in self._quick_retrying
        self._quick_retrying.discard(slot_name)
        if not holder.get("second_tap") or not holder.get("s1"):
            quick_log.info("Quick Stop test: NON CONCLUSIVO (secondo tocco o screenshot mancanti): riprovo alla prossima sessione.")
            return False
        gap = holder.get("gap")
        if gap is not None and gap > QUICK_STOP_MAX_GAP:
            quick_log.warning("Quick Stop test: NON CONCLUSIVO (secondo tocco arrivato dopo %.1f s): riprovo.", gap)
            return False
        mid = self._balance_from_image(holder["s1"])
        if mid is None:
            quick_log.info("Quick Stop test: NON CONCLUSIVO (saldo illeggibile a t=3.5 s): riprovo alla prossima sessione.")
            return False
        drop = balance_before - mid
        if drop >= bet * Decimal("1.75"):
            quick_log.warning("Quick Stop test: DISABILITATO (doppio spin: saldo sceso %s con puntata %s).", drop, bet)
            self._set_quick(slot_name, False, "doppio spin")
            return True
        if drop >= bet * Decimal("0.75"):
            still = holder.get("s2") is not None and self._reels_still(holder["s1"], holder["s2"])
            if still:
                quick_log.info("Quick Stop test: ABILITATO (saldo sceso 1x, animazione saltata).")
                self._quick_delay_used[slot_name] = holder.get("delay", quick_stop_delay())
                self._set_quick(slot_name, True, "animazione saltata dal secondo tocco")
            elif not retrying:
                # Ignorato a QUICK_STOP_DELAY: nulla viene salvato, al prossimo spin si riprova una volta a 0.3 s.
                self._quick_retry.add(slot_name)
                quick_log.info("Quick Stop test: ignorato con delay %.1f s, riprovo al prossimo spin con %.1f s.",
                            holder.get("delay", 0.0), QUICK_STOP_RETRY_DELAY)
            else:
                quick_log.info("Quick Stop test: DISABILITATO (ignorato: i rulli erano ancora in movimento, anche a %.1f s).",
                            QUICK_STOP_RETRY_DELAY)
                self._set_quick(slot_name, False, "tocco ignorato (0.5 s e 0.3 s)")
            return False
        quick_log.info("Quick Stop test: NON CONCLUSIVO (nessun addebito visto a t=3.5 s): riprovo alla prossima sessione.")
        return False

    def _set_quick(self, slot_name: str, enabled: bool, reason: str) -> None:
        self.quick_stats.update(state="enabled" if enabled else "disabled", reason=reason)
        try:
            self.quick_stop_state.set_quick_stop(slot_name, enabled, reason)
        except Exception as exc:   # ricordare l'esito non deve mai rompere lo spin
            quick_log.warning("Quick Stop: esito non salvato: %s", exc)

    def _balance_from_image(self, path: str):
        try:
            value = self.verifier.ocr_reader.read_slot_state(path).get("saldo")
        except Exception:
            return None
        parsed = parse_amount(str(value)) if value is not None else None
        return parsed.data if parsed is not None and parsed.success else None

    def _reels_still(self, first: str, second: str) -> bool:
        region = None
        try:
            from PIL import Image
            with Image.open(first) as image:
                width, height = image.size
            fractions = getattr(self.config, "spin_wait_region", None)
            if fractions:
                x1, y1, x2, y2 = fractions
                region = (int(width * x1), int(height * y1), int(width * x2), int(height * y2))
        except OSError:
            region = None
        diff = diff_score(first, second, region)
        return bool(diff.success and diff.data < self.config.frame_diff_stable_threshold)

    def clear_anomaly(self, slot_name: str) -> None:
        """Dopo che il vigile ha chiarito la situazione (bonus finito): si riparte da zero sulle anomalie."""
        self._anomaly.pop(slot_name, None)
        self._dead_spin_points.pop(slot_name, None)

    def _is_protected_point(self, slot_name: str, point: tuple) -> bool:
        """Un punto che ha gia' addebitato o che la mappa dice verificato non e' mai 'sbagliato': un tocco senza
        addebito li' e' un'anomalia (bonus, free spin, animazione), non un punto da dimenticare."""
        if any(abs(point[0] - x) < 60 and abs(point[1] - y) < 60 for x, y in self._worked_points.get(slot_name, ())):
            return True
        known = self.verified_spin_point(slot_name) if callable(self.verified_spin_point) else None
        return bool(known) and abs(point[0] - known[0]) < 60 and abs(point[1] - known[1]) < 60

    def _note_dead_point(self, slot_name: str, candidate, spin_index: int) -> None:
        """Il tocco ha cambiato lo schermo senza addebitare. Una volta puo' essere un'animazione; la seconda volta
        di fila sullo stesso punto ricordato e' un punto sbagliato (es. una cella della puntata che apre il menu):
        lo si dimentica e lo si scarta, cosi' il giro dopo si rilocalizza invece di ripetere lo stesso errore."""
        if candidate.strategy_name != "memory":
            return
        point = (candidate.x, candidate.y)
        if self._is_protected_point(slot_name, point):
            logger.info("spin %d: il punto (%d,%d) ha gia' funzionato (o e' verificato nella mappa): un tocco senza "
                        "addebito e' un'anomalia (bonus/animazione), non lo dimentico.", spin_index, point[0], point[1])
            return
        previous, count = self._dead_spin_points.get(slot_name, (None, 0))
        count = count + 1 if previous == point else 1
        self._dead_spin_points[slot_name] = (point, count)
        if count < 2:
            return
        logger.warning(
            "spin %d: il punto ricordato (%d,%d) non ha mai addebitato per %d volte di fila: lo dimentico e lo scarto.",
            spin_index, point[0], point[1], count,
        )
        forget_autopilot_element(slot_name, _ELEMENT, self.config.slot_memory_dir)
        self.locator.reject_position(_ELEMENT, point[0], point[1])
        self._dead_spin_points.pop(slot_name, None)

    def _confirm_balance_drop(self, spin_index, balance_before, balance_after, bet, context_provider,
                              before_shot=None, after_shot=None) -> Optional[Decimal]:
        """Doppia lettura di conferma quando il calo di saldo supera il limite.

        Dopo balance_recheck_delay_seconds si rilegge il saldo (al massimo 2 volte):
        - lettura coerente col calo normale -> falso positivo: ritorna quella lettura;
        - lettura ancora anomala e concorde con una precedente (entro la tolleranza) -> confermato: None;
        - anomala ma diversa (OCR instabile) -> un'altra rilettura; l'ultima anomala, o illeggibile -> confermato.
        Nel dubbio si resta prudenti. Logga sempre, con le letture e gli screenshot prima/dopo.
        """
        delay = max(0.0, self.config.balance_recheck_delay_seconds)
        tolerance = self.config.accounting_tolerance
        self.recheck_stats["alarms"] += 1
        drop = balance_before - balance_after
        logger.warning(
            "spin %d: calo di saldo anomalo %s con una puntata creduta di %s (saldo %s -> %s): doppia lettura di "
            "conferma dopo %.1f s. Screenshot prima=%s dopo=%s.%s",
            spin_index, drop, bet, balance_before, balance_after, delay, before_shot, after_shot,
            self._previous_spin_note(spin_index, balance_after, bet),
        )
        readings = [balance_after]
        for attempt in (1, 2):
            if delay > 0:
                time.sleep(delay)
            reading = self._reread_balance(context_provider)
            readings.append(reading)
            if reading is not None and not self._bet_understated(balance_before, reading, bet):
                self.recheck_stats["false_positives"] += 1
                logger.warning(
                    "spin %d: doppia lettura del saldo %s: la rilettura %d (%s) e' coerente, il calo reale e' %s: "
                    "falso positivo, lo spin conta normalmente.",
                    spin_index, [str(r) for r in readings], attempt, reading, balance_before - reading,
                )
                return reading
            if reading is not None and any(r is not None and abs(r - reading) <= tolerance for r in readings[:-1]):
                break   # due letture anomale concordi: il calo e' reale
        self.recheck_stats["confirmed"] += 1
        logger.warning(
            "spin %d: doppia lettura del saldo %s: calo anomalo CONFERMATO (%s con puntata creduta %s).",
            spin_index, [str(r) for r in readings], drop, bet,
        )
        return None

    def _previous_spin_note(self, spin_index, balance_after, bet) -> str:
        """Solo diagnostica: se lo spin precedente sembrava una vincita pari all'eccesso di calo, la lettura
        a cavallo tra i due spin e' sospetta (un valore sbagliato sposta il calo da uno spin all'altro)."""
        last = self._last_normal_spin
        if last is None or last[0] != spin_index - 1 or None in last[1:3] or last[3] is None:
            return ""
        prev_before, prev_after, prev_bet = last[1], last[2], last[3]
        prev_win = prev_after - prev_before + prev_bet
        two_spin_drop = prev_before - balance_after
        return (f" Spin precedente: saldo {prev_before} -> {prev_after} (vincita dedotta {prev_win}); "
                f"calo sui due spin {two_spin_drop} contro puntate {prev_bet} + {bet}.")

    def _reread_balance(self, context_provider) -> Optional[Decimal]:
        result = context_provider()
        if not result.success:
            return None
        return self._read_amount(result.data, "saldo")

    def _beyond_double_spin(self, balance_before, balance_after, bet) -> bool:
        """Il calo supera due spin alla puntata creduta (piu' tolleranza e margine): non puo' essere il Quick Stop."""
        margin = max(bet * Decimal("0.25"), Decimal("0.02"))
        return (balance_before - balance_after) > 2 * bet + self.config.accounting_tolerance + margin

    def _bet_understated(self, balance_before, balance_after, bet) -> bool:
        """Una vincita puo' solo RIDURRE il calo di saldo: se il saldo scende di piu' della puntata creduta
        (piu' la tolleranza e un margine del 25%, minimo 0.02), quella puntata non puo' essere giusta. Con il
        vecchio limite del doppio, 0.40 giocato con 0.20 creduto passava inosservato."""
        if balance_before is None or balance_after is None or bet is None or bet <= 0:
            return False
        margin = max(bet * Decimal("0.25"), Decimal("0.02"))
        return (balance_before - balance_after) > bet + self.config.accounting_tolerance + margin

    def _settle_after_unexpected(self, device_id: str, outcome, context_provider, balance_before):
        """(saldo, screenshot, stato) dopo l'attesa se il saldo e' SCESO (c'e' stato un addebito: lo spin e'
        partito), altrimenti None. Un saldo uguale o salito non prova nulla (puo' essere una vincita
        accreditata): in quel caso non si ritocca."""
        self.verifier.wait_until_calm(device_id, outcome.screenshot_after, self.config.spin_calm_max_wait_seconds)
        fresh = context_provider()
        if not fresh.success:
            return None
        balance_now = self._read_amount(fresh.data, "saldo")
        if balance_now is None or balance_before is None or balance_now >= balance_before:
            return None
        return balance_now, fresh.data.screenshot_path, fresh.data.slot_ocr_state

    @staticmethod
    def _balance_win(balance_before, balance_after, bet) -> Optional[Decimal]:
        """dopo - prima + puntata, o None se manca un valore."""
        if balance_before is None or balance_after is None or bet is None:
            return None
        return balance_after - balance_before + bet

    def _log_button_once(self, slot_name: str, key: str, level: int, message: str) -> None:
        if (slot_name, key) not in self._button_logged:
            self._button_logged.add((slot_name, key))
            logger.log(level, "pulsante spin: %s", message)

    def _record_duration(self, slot_name: str, seconds: float, had_win: bool = False) -> None:
        """Durata tra tocco e fine animazione di uno spin NORMALE: gli spin con una vincita (animazione piu' lunga) e i
        bonus (molto piu' lunghi della mediana) non entrano, cosi' l'attesa iniziale non si allunga per colpa loro."""
        history = self._durations.setdefault(slot_name, [])
        if had_win or seconds <= 0 or seconds > _MAX_NORMAL_DURATION_SECONDS:
            return
        if len(history) < 2 and seconds > _MAX_FIRST_SAMPLE_SECONDS:
            return                      # senza mediana: un campione lungo e' quasi di certo una vincita o un bonus
        if len(history) >= 2 and seconds > 1.6 * statistics.median(history):
            return
        history.append(seconds)
        del history[:-5]

    def _wait_region(self, context) -> Optional[tuple]:
        fractions = getattr(self.config, "spin_wait_region", None)
        width, height = getattr(context, "screen_width", 0), getattr(context, "screen_height", 0)
        if not fractions or not width or not height:
            return None
        x1, y1, x2, y2 = fractions
        return int(width * x1), int(height * y1), int(width * x2), int(height * y2)

    def _typical_duration(self, slot_name: str) -> float:
        """Mediana delle durate normali recenti (0 finche' non ce ne sono almeno 2)."""
        history = self._durations.get(slot_name, [])
        return float(statistics.median(history)) if len(history) >= 2 else 0.0

    def _initial_wait(self, slot_name: str) -> float:
        """Quanto aspettare dopo il tocco prima di scattare: nei primi secondi l'animazione c'e' di sicuro. Prudente:
        70% della piu' corta delle ultime 3 durate normali, meno il costo di uno scatto; niente prima di 2 spin."""
        history = self._durations.get(slot_name, [])
        if len(history) < 2 or not self.config.spin_smart_wait:
            return 0.0
        stats = getattr(getattr(self.verifier, "screen_reader", None), "capture_stats", None)
        try:
            shot = float(stats["seconds"]) / float(stats["calls"]) if stats and stats["calls"] else 1.5
        except (TypeError, KeyError, ZeroDivisionError):
            shot = 1.5
        return round(min(_MAX_INITIAL_WAIT_SECONDS, max(0.0, 0.7 * min(history[-3:]) - shot)), 2)

    def _sample_busy_button(self, device_id: str, holder: dict) -> None:
        """Nello spin di apprendimento: uno screenshot ~1 s dopo il tocco, mentre il gioco gira."""
        time.sleep(_BUSY_SAMPLE_DELAY_SECONDS)
        shot = self.verifier.screen_reader._screenshot(device_id)
        if shot.success:
            holder["path"] = shot.data

    @staticmethod
    def _calm_regions(context, candidate) -> Optional[list]:
        """Le zone che contano per toccare lo spin: la striscia in basso con saldo/puntata/vincita e un quadrato
        attorno al pulsante spin. I rulli (dove passano gli overlay delle vincite) non contano."""
        width, height = context.screen_width, context.screen_height
        if not width or not height:
            return None
        half = _SPIN_ZONE_HALF_PX
        spin_zone = (
            max(0, int(candidate.x) - half), max(0, int(candidate.y) - half),
            min(int(width), int(candidate.x) + half), min(int(height), int(candidate.y) + half),
        )
        return [(0, int(height * (1 - _BOTTOM_STRIP_FRACTION)), int(width), int(height)), spin_zone]

    def _check_invariants(self, min_bet_verified, balance_before, expected_bet, context, force: bool = False) -> Optional[str]:
        if not min_bet_verified:
            return "MIN_BET_NOT_VERIFIED"
        if balance_before is None:
            # Saldo non leggibile (slot chiusa, lobby, pagina di errore): non e' "saldo insufficiente". Passa dal
            # percorso "schermo occupato", che controlla se si e' ancora nella slot e, se no, rientra.
            return "UNSAFE_SCREEN_STATE"
        if expected_bet is None or balance_before < expected_bet:
            return "INSUFFICIENT_BALANCE"
        if not force and classify_screen_state(context.slot_ocr_state) != "idle":
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
