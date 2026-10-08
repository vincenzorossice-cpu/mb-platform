import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# Spin senza effetto di fila dopo i quali ci si ferma (invece di consumare max_steps).
MAX_CONSECUTIVE_NO_EFFECT_SPINS = 3
MAX_OPERATOR_ACTIONS = 3   # interventi dell'operatore senza spin prima di fermarsi
# Schermo non libero dopo uno spin (vincita/bonus/animazione lunga): si aspetta invece di
# chiudere la sessione, fino a questo tempo.
MAX_BUSY_WAIT_SECONDS = 120
BUSY_POLL_SECONDS = 5
# Dopo tanto tempo di "occupato" si chiede a Sol se il gioco e' in realta' pronto (overlay di una vincita che
# lampeggia sopra i rulli) invece di aspettare fino alla fine e arrendersi.
BUSY_ASK_AFTER_SECONDS = 45

BET_FAILURE_MESSAGES = {
    "BET_UNREADABLE": "Non riesco a leggere la puntata: non tocco nulla (e non spinno).",
    "MONEY_MOVED": "Due tocchi durante la messa a punto della puntata hanno giocato uno spin: mi fermo.",
    "MIN_NOT_PROVEN": "Non riesco a dimostrare che questa sia la puntata minima: per sicurezza non spinno.",
    "MENU_BET_UNREADABLE": "Ho scelto la puntata dal menu ma non riesco a leggerla: non spinno.",
    "GPT_UNAVAILABLE": "GPT non risponde (limite di richieste o rete): riprova tra un minuto. Non ho toccato altro.",
    "ELEMENT_NOT_LOCATABLE": "Non trovo nessun modo di abbassare la puntata (ne' il tasto meno ne' il menu): mi fermo.",
    "STUCK_ON_UNEXPECTED_SCREEN": "Sono rimasto su una schermata inattesa e non riesco a tornare indietro: mi fermo.",
}


@dataclass
class SlotSessionResult:
    """Esito di una sessione di gioco dello SlotAutoPilot su una slot."""
    total_played: Decimal
    spins: int
    # True = la sessione si e' fermata per un problema (non per target o max_steps raggiunti).
    failed: bool
    # None | "PREPARE_FAILED" | "BET_NOT_VERIFIED" | "SPIN_ERROR" | "BET_UNDERSTATED" | "NO_EFFECT" |
    # "BUSY_TIMEOUT" | "BLOCKED"
    stop_reason: Optional[str] = None
    bet_aborted_reason: Optional[str] = None
    initial_balance: Optional[Decimal] = None
    final_balance: Optional[Decimal] = None
    target: Decimal = Decimal("0.00")
    # Media per fase (secondi) degli spin riusciti: calma, tocco, attesa, lettura, altro, totale (None se nessuno).
    avg_timings: Optional[dict] = None

    @property
    def target_reached(self) -> bool:
        """Il giocato ha raggiunto il target senza arresti di sicurezza."""
        return not self.failed and self.stop_reason is None and self.total_played >= self.target


def _looks_disconnected(result) -> bool:
    text = f"{getattr(result, 'error', '')} {getattr(result, 'error_code', '')}".lower()
    return any(word in text for word in ("adb_timeout", "device_offline", "screenshot file not created", "no devices"))


def _log_phase(autopilot, label: str, seconds: float, log) -> None:
    report = getattr(autopilot, "phase_report", None)
    text = report(label, seconds) if callable(report) else None
    if isinstance(text, str) and text:
        log.info("%s", text)


def _log_capture_stats(autopilot, log) -> None:
    """Quanto sono costati gli screenshot ADB in tutta la sessione (il collo di bottiglia misurato il 7 ottobre)."""
    try:
        stats = autopilot.screen_reader.capture_stats
        calls, seconds = int(stats["calls"]), float(stats["seconds"])
        if calls > 0:
            log.info("Screenshot ADB (modo %s): %d in %.0f s, %.2f s a scatto.",
                     getattr(autopilot.screen_reader, "capture_mode", "?"), calls, seconds, seconds / calls)
    except Exception:
        pass


def _ask_if_ready(autopilot, device_id: str) -> str:
    diagnose = getattr(autopilot, "diagnose_busy", None)
    if not callable(diagnose):
        return "unknown"
    verdict = diagnose(device_id)
    return verdict if isinstance(verdict, str) else "unknown"


def _log_event(kind: str) -> None:
    try:
        from orchestrator.core import model_metrics
        model_metrics.log_event(kind)
    except Exception:
        pass


def _busy_diagnostics(autopilot, device_id: str, label: str, log, event: Optional[str]) -> None:
    """Screenshot dello schermo occupato in logs/ (per capire cosa lo blocca) ed evento nelle metriche."""
    if event:
        _log_event(event)
    save = getattr(autopilot, "save_debug_screenshot", None)
    if callable(save):
        path = save(device_id, label)
        if isinstance(path, str):
            log.info("Screenshot dello schermo occupato: %s", path)


def progress_line(spin_index: int, spin_result, total_played: Decimal, target: Decimal, seconds: float) -> str:
    bet = f"{spin_result.bet_amount:.2f}" if spin_result.bet_amount is not None else "-"
    balance = f"{spin_result.balance_after}" if spin_result.balance_after is not None else "-"
    win = f"{spin_result.win_amount:.2f}" if spin_result.win_amount else "0.00"
    line = (
        f"Spin {spin_index} | puntata {bet} | saldo {balance} | vincita {win} | "
        f"giocato {total_played:.2f}/{target:.2f} | {seconds:.0f} s"
    )
    timings = getattr(spin_result, "timings", None)
    if isinstance(timings, dict):
        line += (
            f" [calma {timings.get('calma', 0):.1f} | tocco {timings.get('tocco', 0):.1f} | attesa "
            f"{timings.get('attesa', 0):.1f} ({timings.get('scatti', 0)} scatti) | lettura "
            f"{timings.get('lettura', 0):.1f} | altro {timings.get('altro', 0):.1f}]"
        )
    return line


_TIMING_KEYS = ("contesto", "calma", "tocco", "attesa", "lettura", "altro", "totale")


def average_timings(collected: list) -> Optional[dict]:
    """Media per fase dei dizionari `timings` degli spin; None se non ce n'e' nessuno."""
    rows = [t for t in collected if isinstance(t, dict)]
    if not rows:
        return None
    return {key: round(sum(float(t.get(key, 0) or 0) for t in rows) / len(rows), 2) for key in _TIMING_KEYS}


def timing_summary_line(averages: dict, spins: int) -> str:
    return (
        f"Tempo medio per spin ({spins} spin): totale {averages['totale']:.1f} s = contesto {averages['contesto']:.1f} "
        f"+ calma {averages['calma']:.1f} + tocco {averages['tocco']:.1f} + attesa schermo fermo "
        f"{averages['attesa']:.1f} + lettura {averages['lettura']:.1f} + altro {averages['altro']:.1f}"
    )


def play_session(
    autopilot, device_id: str, slot_name: str, target: Decimal, max_steps: int, log: logging.Logger = None,
) -> SlotSessionResult:
    """Prepara la slot, porta la puntata al minimo VERIFICATO e gioca fino al target (o a un arresto di
    sicurezza). E' il ciclo usato sia da run_autopilot sia da CasinoFlow. `log` permette a chi chiama di
    tenere i propri messaggi (filtri della console compresi)."""
    log = log or logger
    log.info("Preparazione: profilo '%s', dispositivo %s...", slot_name, device_id)
    phase_started = time.monotonic()
    prepare_result = autopilot.prepare(device_id)
    if not prepare_result.success:
        log.error("prepare() fallito: %s (%s)", prepare_result.error, prepare_result.error_code)
        return SlotSessionResult(Decimal("0.00"), 0, failed=True, stop_reason="PREPARE_FAILED", target=target)
    log.info("Preparazione completata.")
    _log_phase(autopilot, "Preparazione", time.monotonic() - phase_started, log)

    log.info("Verifica puntata minima in corso...")
    phase_started = time.monotonic()
    bet_result = autopilot.set_min_bet(device_id)
    _log_phase(autopilot, "Puntata minima", time.monotonic() - phase_started, log)
    if not bet_result.success or not bet_result.data.min_bet_verified:
        reason = bet_result.data.aborted_reason if bet_result.success else bet_result.error_code
        log.error(
            "Impossibile verificare la puntata minima (%s): %s", reason,
            BET_FAILURE_MESSAGES.get(reason, "mi fermo senza spinnare."),
        )
        return SlotSessionResult(
            Decimal("0.00"), 0, failed=True, stop_reason="BET_NOT_VERIFIED", bet_aborted_reason=reason,
            target=target,
        )
    log.info(
        "Puntata minima verificata: %s EUR (outcome=%s, tentativi=%d)",
        bet_result.data.final_bet, bet_result.data.outcome, bet_result.data.attempts,
    )

    total_played = Decimal("0.00")
    spin_index = 0
    consecutive_no_effect = 0
    operator_actions = 0
    stop_reason = None
    failed = False
    busy_since = None
    busy_asked = False
    force_next = False
    collected_timings = []
    button_status_shown = False
    while total_played < target and spin_index < max_steps:
        spin_index += 1
        started = time.monotonic()
        if force_next:
            force_next = False
            spin_result = autopilot.spin(device_id, spin_index, force=True)   # Sol: il gioco e' pronto
        else:
            spin_result = autopilot.spin(device_id, spin_index)
        seconds = time.monotonic() - started
        if not spin_result.success:
            log.error("spin() fallito: %s (%s)", spin_result.error, spin_result.error_code)
            if _looks_disconnected(spin_result):
                log.error("Il telefono non risponde (scollegato o ADB bloccato?): ricollegalo e rilancia.")
            return SlotSessionResult(total_played, spin_index, failed=True, stop_reason="SPIN_ERROR", target=target)
        if spin_result.data.bet_understated:
            log.error(
                "La puntata reale sembra piu' alta di %s (il saldo e' sceso molto piu' del previsto): mi fermo.",
                spin_result.data.bet_amount,
            )
            failed, stop_reason = True, "BET_UNDERSTATED"
            break
        if spin_result.data.outcome == "operator_action":
            # L'operatore ha toccato altro (es. la puntata): non e' uno spin. La puntata puo' essere cambiata:
            # si rilegge e si riverifica prima di proseguire.
            operator_actions += 1
            spin_index -= 1
            if operator_actions > MAX_OPERATOR_ACTIONS:
                log.error("Troppi interventi dell'operatore senza uno spin: mi fermo.")
                failed, stop_reason = True, "BLOCKED"
                break
            log.info("Intervento dell'operatore senza spin: ricontrollo la puntata prima di continuare.")
            recheck = autopilot.set_min_bet(device_id)
            if not recheck.success or not recheck.data.min_bet_verified:
                reason = recheck.data.aborted_reason if recheck.success else recheck.error_code
                log.error("Impossibile riverificare la puntata (%s): mi fermo senza spinnare.", reason)
                failed, stop_reason = True, "BET_NOT_VERIFIED"
                break
            log.info("Puntata riverificata: %s EUR.", recheck.data.final_bet)
            continue
        if spin_result.data.outcome != "blocked":
            busy_since = None
            busy_asked = False
        if spin_result.data.outcome == "no_effect":
            consecutive_no_effect += 1
            if consecutive_no_effect >= MAX_CONSECUTIVE_NO_EFFECT_SPINS:
                log.error(
                    "%d spin di fila senza effetto: il tasto spin non e' stato trovato. Mi fermo "
                    "(rilancia: il punto sbagliato viene ricordato solo per la sessione).",
                    consecutive_no_effect,
                )
                failed, stop_reason = True, "NO_EFFECT"
                break
        else:
            consecutive_no_effect = 0
        if spin_result.data.outcome == "success" and spin_result.data.bet_amount:
            total_played += spin_result.data.bet_amount
            collected_timings.append(getattr(spin_result.data, "timings", None))
            status = getattr(spin_result.data, "button_status", None)
            if isinstance(status, str) and not button_status_shown:
                button_status_shown = True
                log.info("Pulsante spin: %s", status)
            log.info("%s", progress_line(spin_index, spin_result.data, total_played, target, seconds))
        elif spin_result.data.outcome == "blocked":
            if spin_result.data.violated_invariant == "UNSAFE_SCREEN_STATE":
                if busy_since is None:
                    busy_since = time.monotonic()
                    log.info(
                        "Schermo occupato (%s; vincita/bonus in corso?): aspetto che finisca (al massimo %d s)...",
                        getattr(spin_result.data, "blocked_detail", None) or "causa non indicata",
                        MAX_BUSY_WAIT_SECONDS,
                    )
                    _busy_diagnostics(autopilot, device_id, "occupato_inizio", log, "schermo_occupato")
                waited = time.monotonic() - busy_since
                if waited >= BUSY_ASK_AFTER_SECONDS and not busy_asked:
                    busy_asked = True
                    verdict = _ask_if_ready(autopilot, device_id)
                    if verdict == "ready":
                        log.info("Sol: il gioco e' pronto (overlay/animazione sui rulli): provo uno spin.")
                        _log_event("schermo_occupato_risolto_da_sol")
                        force_next = True
                        spin_index -= 1
                        continue
                    if verdict == "closed":
                        log.info("Sol: c'era un menu non voluto, chiuso: riprendo.")
                        _log_event("schermo_occupato_risolto_da_sol")
                        busy_since, busy_asked = None, False
                        spin_index -= 1
                        continue
                if waited < MAX_BUSY_WAIT_SECONDS:
                    time.sleep(BUSY_POLL_SECONDS)
                    spin_index -= 1   # l'attesa non consuma max_steps
                    continue
                _busy_diagnostics(autopilot, device_id, "occupato_scaduto", log, None)
                log.warning("Lo schermo e' rimasto occupato per oltre %d s: mi fermo.", MAX_BUSY_WAIT_SECONDS)
                failed, stop_reason = True, "BUSY_TIMEOUT"
            else:
                log.warning("Spin bloccato da un'invariante di sicurezza: %s", spin_result.data.violated_invariant)
                stop_reason = "BLOCKED"
            break
    log.info("Sessione conclusa: %s/%s EUR giocati in %d spin.", total_played, target, spin_index)
    averages = average_timings(collected_timings)
    if averages is not None:
        log.info("%s", timing_summary_line(averages, len([t for t in collected_timings if isinstance(t, dict)])))
    _log_capture_stats(autopilot, log)

    initial_balance, final_balance = autopilot.get_balance_summary()
    if initial_balance is not None and final_balance is not None:
        net = final_balance - initial_balance
        log.info(
            "Riepilogo: saldo iniziale=%s, saldo finale=%s, netto=%s EUR",
            initial_balance, final_balance, net,
        )
    return SlotSessionResult(
        total_played, spin_index, failed=failed, stop_reason=stop_reason,
        initial_balance=initial_balance, final_balance=final_balance, target=target, avg_timings=averages,
    )
