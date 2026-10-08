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
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.value_parser import parse_amount
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, Expectation, classify_screen_state
from orchestrator.modules.step_logger.interface import StepLoggerInterface, StepRecord

logger = logging.getLogger(__name__)

_ELEMENT = "spin"
_MAX_NORMAL_DURATION_SECONDS = 20.0   # oltre, e' un bonus/feature: non conta per la durata tipica
_MAX_FIRST_SAMPLE_SECONDS = 12.0      # primi campioni: sopra e' quasi certamente una vincita/bonus
_MAX_INITIAL_WAIT_SECONDS = 8.0
_BUSY_SAMPLE_DELAY_SECONDS = 1.0  # quanto dopo il tocco si campiona il pulsante "occupato"
_SPIN_ZONE_HALF_PX = 160          # quadrato di ~320 px attorno al pulsante spin
_BOTTOM_STRIP_FRACTION = 0.12     # ultimo 12% dell'altezza: saldo, puntata, vincita


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
    outcome: str  # "success" | "blocked" | "no_effect" | "error" | "operator_action" (non e' uno spin) | "slot_lost"
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
        self._dead_spin_points = {}        # slot -> ((x, y), volte di fila) tocchi che non hanno mai addebitato

    def spin(
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
            if ready_check is not None:
                # La freccia dello spin e' la misura giusta (un overlay di vincita muove lo schermo anche a gioco pronto)
                calm = self.verifier.wait_until_button_ready(device_id, ready_check, self.config.spin_calm_max_wait_seconds)
            if calm is None:
                calm = self.verifier.wait_until_calm(
                    device_id, context.screenshot_path, self.config.spin_calm_max_wait_seconds,
                    regions=self._calm_regions(context, candidate) if self.config.spin_partial_calm else None,
                )
            calm_seconds = time.monotonic() - calm_started
            if calm.success and calm.data.partial:
                logger.info("spin %d: i rulli sono in animazione ma barra degli importi e pulsante sono fermi: "
                            "tocco (calma parziale).", spin_index)
            if calm.success and not calm.data.calm:
                violated = "UNSAFE_SCREEN_STATE"
                logger.info("spin %d: lo schermo non si ferma (animazione in corso?): non tocco.", spin_index)
                self._log_step(
                    episode_id, spin_index, slot_name, device_id, None, None,
                    balance_before, balance_before, outcome="blocked", notes=violated,
                )
                return Result.ok(SpinResult(
                    slot_name=slot_name, spin_index=spin_index, bet_amount=expected_bet,
                    balance_before=balance_before, balance_after=balance_before, win_amount=None,
                    verified=False, attempts=0, outcome="blocked", violated_invariant=violated,
                    blocked_detail="schermo non fermo",
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
        last_wait_seconds = None
        busy_sample = {}
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
                if learning_button and attempts == 1:
                    extra["on_after_tap"] = lambda: self._sample_busy_button(device_id, busy_sample)
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
        button_status = None
        if verified and learning_button:
            if busy_sample.get("path"):
                self.button.learn(
                    slot_name, (context.screen_width, context.screen_height), candidate.x, candidate.y,
                    context.screenshot_path, busy_sample["path"],
                )
                button_status = self.button.status(slot_name)
            else:
                button_status = "NON appreso: lo screenshot 'occupato' dopo il tocco non e' stato raccolto"
                logger.warning("pulsante spin: %s", button_status)
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
                if from_balance < Decimal("0.01"):
                    # Perso (saldo alla mano): il prossimo spin puo' saltare il controllo di calma se parte subito.
                    self._calm_skip_deadline = time.monotonic() + self.config.spin_calm_skip_max_age_seconds
        if bet_understated:
            logger.error(
                "spin %d: il saldo e' sceso di %s con una puntata creduta di %s: la puntata reale e' piu' alta.",
                spin_index, balance_before - balance_after, expected_bet,
            )
        accounting_anomaly = None   # il controllo per spin sulla vincita letta e' stato tolto: vedi sopra
        if outcome == "success" and not force and last_wait_seconds is not None:
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
            timings=timings, button_status=button_status,
        ))

    def _note_dead_point(self, slot_name: str, candidate, spin_index: int) -> None:
        """Il tocco ha cambiato lo schermo senza addebitare. Una volta puo' essere un'animazione; la seconda volta
        di fila sullo stesso punto ricordato e' un punto sbagliato (es. una cella della puntata che apre il menu):
        lo si dimentica e lo si scarta, cosi' il giro dopo si rilocalizza invece di ripetere lo stesso errore."""
        if candidate.strategy_name != "memory":
            return
        point = (candidate.x, candidate.y)
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
        if balance_before is None or expected_bet is None or balance_before < expected_bet:
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
