import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# Spin senza effetto di fila dopo i quali ci si ferma (invece di consumare max_steps).
MAX_CONSECUTIVE_NO_EFFECT_SPINS = 3
MAX_HELP_REQUESTS = 5       # richieste di aiuto all'operatore per sessione prima di fermarsi
MAX_REENTRIES = 3             # rientri nella slot per sessione (sessione scaduta, pagina di errore)
LOST_CHECK_AFTER_SECONDS = 15  # schermo occupato da cosi' tanto: prima di aspettare oltre si controlla che la slot ci sia
LOST_CHECK_UNREADABLE_SECONDS = 8   # idem quando il saldo non si legge proprio (slot chiusa, lobby)
BET_WINDOW = 4                 # spin consecutivi per accorgersi che la puntata creduta e' troppo alta
BET_WINDOW_RATIO = Decimal("0.6")
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
    reentries: int = 0                   # quante volte si e' rientrati nella slot dopo che ci ha cacciato
    net: Optional[Decimal] = None        # netto vinto/perso sommato su tutti gli ingressi (il saldo demo si ripristina)

    @property
    def target_reached(self) -> bool:
        """Il giocato ha raggiunto il target senza arresti di sicurezza."""
        return not self.failed and self.stop_reason is None and self.total_played >= self.target


def _looks_disconnected(result) -> bool:
    text = f"{getattr(result, 'error', '')} {getattr(result, 'error_code', '')}".lower()
    return any(word in text for word in ("adb_timeout", "device_offline", "screenshot file not created", "no devices"))


def _operator_available(autopilot) -> bool:
    hitl = getattr(autopilot, "hitl_strategy", None)
    return callable(getattr(hitl, "ask_for_help", None)) and isinstance(getattr(hitl, "interventions", None), int)


def _ask_operator(autopilot, device_id: str, reason: str, log, help_state: dict) -> str:
    """Prima di fermarsi la sessione chiede all'operatore. 'unavailable' = nessun operatore o richieste finite
    (ci si ferma come prima), 'stop' = l'operatore vuole fermare tutto, 'retry' = riprovare (ha toccato o ha detto skip)."""
    if not _operator_available(autopilot) or help_state["n"] >= MAX_HELP_REQUESTS:
        return "unavailable"
    help_state["n"] += 1
    log.warning("Chiedo aiuto all'operatore (%d/%d): %s", help_state["n"], MAX_HELP_REQUESTS, reason)
    try:
        action = autopilot.hitl_strategy.ask_for_help(device_id, reason, getattr(autopilot, "_current_bet", None))
    except (EOFError, KeyboardInterrupt):
        return "stop"
    if not isinstance(action, str):
        return "unavailable"
    return "stop" if action == "stop" else "retry"


def _ask_to_reconnect(autopilot, log, help_state: dict) -> str:
    """Telefono che non risponde: aspetta l'operatore (INVIO = riprova, 'stop' = ferma)."""
    if not _operator_available(autopilot) or help_state["n"] >= MAX_HELP_REQUESTS:
        return "unavailable"
    help_state["n"] += 1
    try:
        answer = input("\nIl telefono non risponde (scollegato o ADB bloccato?). Ricollegalo e premi INVIO per "
                       "riprovare, oppure scrivi 'stop': ").strip().lower()
    except (EOFError, OSError, KeyboardInterrupt):
        return "unavailable"
    return "stop" if answer == "stop" else "retry"


def _recheck_bet(autopilot, device_id: str, log) -> bool:
    """Dopo un aiuto dell'operatore la puntata puo' essere cambiata: si rilegge e si riverifica."""
    recheck = autopilot.set_min_bet(device_id)
    if recheck.success and recheck.data.min_bet_verified:
        log.info("Puntata riverificata: %s EUR.", recheck.data.final_bet)
        return True
    reason = recheck.data.aborted_reason if recheck.success else recheck.error_code
    log.error("Impossibile riverificare la puntata (%s).", reason)
    return False


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


def _log_arrow_stats(autopilot, log) -> None:
    """Una riga per capire dalla console se la freccia dello spin lavora o se si aspetta lo schermo fermo."""
    try:
        stats = autopilot.spin_controller.arrow_stats
        spins = int(stats["spins"])
        if spins > 0:
            log.info(
                "Freccia spin: riferimento %s, %d spin chiusi dalla freccia, %d dallo schermo fermo, %.1f scatti di "
                "attesa per spin.", "SI" if stats["with_reference"] else "NO", int(stats["arrow"]),
                int(stats["still"]), stats["shots"] / spins,
            )
    except Exception:
        pass


def _setup_of(autopilot):
    """L'oggetto SlotSetup dell'autopilot, solo se e' uno vero (stato testo o None)."""
    setup = getattr(autopilot, "slot_setup", None)
    if setup is None or not callable(getattr(setup, "prepare", None)):
        return None
    return setup if isinstance(getattr(setup, "status", "?"), (str, type(None))) else None


def _log_slot_memory(autopilot, log) -> None:
    """Riga di diagnosi: la slot era gia' mappata (zero GPT per orientarsi) o e' stata esplorata, e quanto e' costato GPT."""
    try:
        setup = _setup_of(autopilot)
        status = setup.status if setup is not None else None
        client = getattr(autopilot, "gpt_client", None)
        usage = getattr(client, "_usage", None)
        calls = sum(int(entry["calls"]) for entry in usage.values()) if isinstance(usage, dict) else None
        costs = [entry["cost"] for entry in usage.values()] if isinstance(usage, dict) else []
        if status:
            log.info("Slot memory: %s.", status + (" (slot gia' mappata)" if status == "USATA" else ""))
        if calls is not None:
            if calls == 0:
                log.info("Costo GPT del gioco: $0 (zero chiamate)%s.", " - slot mappata" if status == "USATA" else "")
            else:
                known = [c for c in costs if c is not None]
                log.info("Costo GPT del gioco: ~$%.4f (%d chiamate)%s.", sum(known), calls,
                         "" if len(known) == len(costs) else " (parziale: prezzo di un modello ignoto)")
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


def _check_lost(autopilot, device_id: str) -> str:
    """Sol dice se la slot e' ancora sullo schermo: 'in_game' | 'lost' | 'unknown' (senza supervisore: unknown)."""
    check = getattr(getattr(autopilot, "supervisor", None), "check_in_game", None)
    if not callable(check):
        return "unknown"
    try:
        verdict = check(device_id)
    except Exception:
        return "unknown"
    return verdict if isinstance(verdict, str) else "unknown"


def _check_bet_window(recent: list, spin, autopilot, device_id: str, log) -> Decimal:
    """Puntata creduta piu' alta di quella vera: se in BET_WINDOW spin di fila il calo massimo di saldo e' sotto il
    60% della puntata creduta (una puntata persa darebbe un calo pari alla puntata), si rilegge la puntata dallo
    schermo. Ritorna l'importo da togliere dal giocato (0 se la puntata era giusta)."""
    if spin.balance_before is None or spin.balance_after is None or not spin.bet_amount:
        recent.clear()
        return Decimal("0")
    recent.append((spin.balance_before - spin.balance_after, spin.bet_amount))
    del recent[:-BET_WINDOW]
    if len(recent) < BET_WINDOW or len({bet for _, bet in recent}) != 1:
        return Decimal("0")
    believed = recent[-1][1]
    if max(drop for drop, _ in recent) >= believed * BET_WINDOW_RATIO:
        return Decimal("0")
    recent.clear()
    log.warning(
        "Il saldo scende di molto meno della puntata creduta (%s) in %d spin di fila: rileggo la puntata.",
        believed, BET_WINDOW,
    )
    recheck = autopilot.set_min_bet(device_id)
    real = recheck.data.final_bet if recheck.success and recheck.data.min_bet_verified else None
    if real is None or real >= believed:
        log.info("Puntata riletta: %s (nessuna correzione).", real)
        return Decimal("0")
    log.warning("La puntata vera e' %s, non %s: correggo il giocato degli ultimi %d spin.", real, believed, BET_WINDOW)
    return (believed - real) * BET_WINDOW


def play_session(
    autopilot, device_id: str, slot_name: str, target: Decimal, max_steps: int, log: logging.Logger = None,
    reenter=None,
) -> SlotSessionResult:
    """Prepara la slot, porta la puntata al minimo VERIFICATO e gioca fino al target (o a un arresto di
    sicurezza). E' il ciclo usato sia da run_autopilot sia da CasinoFlow. `log` permette a chi chiama di
    tenere i propri messaggi (filtri della console compresi). `reenter(device) -> Result`, se dato, riporta
    dentro la slot quando il sito ci ha cacciati (sessione scaduta, pagina di errore): il giocato prosegue."""
    log = log or logger
    log.info("Preparazione: profilo '%s', dispositivo %s...", slot_name, device_id)
    phase_started = time.monotonic()
    prepare_result = autopilot.prepare(device_id)
    if not prepare_result.success:
        log.error("prepare() fallito: %s (%s)", prepare_result.error, prepare_result.error_code)
        return SlotSessionResult(Decimal("0.00"), 0, failed=True, stop_reason="PREPARE_FAILED", target=target)
    log.info("Preparazione completata.")
    _log_phase(autopilot, "Preparazione", time.monotonic() - phase_started, log)

    slot_setup = _setup_of(autopilot)
    if slot_setup is not None:
        prepared_map = slot_setup.prepare(device_id, slot_name)
        if not prepared_map.success:
            log.warning("Slot memory: preparazione non riuscita (%s): proseguo come prima.", prepared_map.error_code)
    reexplored = {"spin": False, "bet_menu": False}

    log.info("Verifica puntata minima in corso...")
    phase_started = time.monotonic()
    bet_result = autopilot.set_min_bet(device_id)
    _log_phase(autopilot, "Puntata minima", time.monotonic() - phase_started, log)
    help_state = {"n": 0}
    while not bet_result.success or not bet_result.data.min_bet_verified:
        reason = bet_result.data.aborted_reason if bet_result.success else bet_result.error_code
        log.error(
            "Impossibile verificare la puntata minima (%s): %s", reason,
            BET_FAILURE_MESSAGES.get(reason, "mi fermo senza spinnare."),
        )
        if slot_setup is not None and reason != "HITL_STOP" and not reexplored["bet_menu"]:
            reexplored["bet_menu"] = True
            log.warning("Slot memory: la puntata non si imposta con la mappa salvata: rifaccio l'esplorazione del menu.")
            if slot_setup.reexplore(device_id, slot_name, "bet_menu").success:
                bet_result = autopilot.set_min_bet(device_id)
                continue
        if reason != "HITL_STOP" and _ask_operator(
            autopilot, device_id, f"Non riesco a verificare la puntata minima ({reason}).", log, help_state,
        ) == "retry":
            bet_result = autopilot.set_min_bet(device_id)
            continue
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
    reentry = {"n": 0, "net": Decimal("0")}
    recent_spins = []          # (calo di saldo, puntata creduta) degli ultimi spin riusciti
    lost_checked = False
    try:
        first_initial = autopilot.get_balance_summary()[0]
    except Exception:
        first_initial = None

    def try_reenter(reason: str) -> bool:
        """Il sito ci ha cacciati: si rientra dalla navigazione, si ripristina la puntata e si prosegue con lo
        stesso giocato. Il saldo del segmento finito entra nel netto (in demo rientrando il saldo si ripristina)."""
        if reenter is None:
            return False
        while reentry["n"] < MAX_REENTRIES:
            reentry["n"] += 1
            log.warning("RIENTRO %d/%d nella slot: %s", reentry["n"], MAX_REENTRIES, reason)
            try:
                initial, final = autopilot.get_balance_summary()
                if initial is not None and final is not None:
                    reentry["net"] += final - initial
            except Exception:
                pass
            rejoined = reenter(device_id)
            prepared = autopilot.prepare(device_id) if rejoined.success else rejoined
            if rejoined.success and prepared.success:
                forget = getattr(getattr(autopilot, "bet_controller", None), "forget_session_min", None)
                if callable(forget):
                    forget(slot_name)
                bet = autopilot.set_min_bet(device_id)
                if bet.success and bet.data.min_bet_verified:
                    log.info("Rientrato nella slot: puntata %s EUR, riprendo dal giocato %s.",
                             bet.data.final_bet, total_played)
                    return True
                log.error("Rientrato ma non riesco a verificare la puntata minima.")
            else:
                log.error("Rientro nella slot non riuscito: %s", rejoined.error if not rejoined.success else prepared.error)
            if _ask_operator(autopilot, device_id, "non riesco a rientrare nella slot e a riprendere.", log,
                             help_state) != "retry":
                return False
        log.error("Rientri esauriti (%d): mi fermo.", MAX_REENTRIES)
        return False

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
                if _ask_to_reconnect(autopilot, log, help_state) == "retry":
                    spin_index -= 1
                    continue
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
        if spin_result.data.outcome == "slot_lost":
            if try_reenter("il pulsante spin non c'e' e la slot non e' piu' sullo schermo"):
                consecutive_no_effect, busy_since, busy_asked, lost_checked = 0, None, False, False
                recent_spins.clear()
                spin_index -= 1
                continue
            failed, stop_reason = True, "SLOT_LOST"
            break
        if spin_result.data.outcome != "blocked":
            busy_since = None
            busy_asked = False
            lost_checked = False
        if spin_result.data.outcome == "no_effect":
            consecutive_no_effect += 1
            if consecutive_no_effect >= MAX_CONSECUTIVE_NO_EFFECT_SPINS:
                log.error(
                    "%d spin di fila senza effetto: il tasto spin non e' stato trovato. Mi fermo "
                    "(rilancia: il punto sbagliato viene ricordato solo per la sessione).",
                    consecutive_no_effect,
                )
                if _check_lost(autopilot, device_id) == "lost" and try_reenter(
                    f"{consecutive_no_effect} spin senza addebito e la slot non e' piu' sullo schermo",
                ):
                    consecutive_no_effect, busy_since, busy_asked, lost_checked = 0, None, False, False
                    recent_spins.clear()
                    spin_index -= 1
                    continue
                if slot_setup is not None and not reexplored["spin"]:
                    reexplored["spin"] = True
                    log.warning("Slot memory: lo spin salvato non addebita: rifaccio l'esplorazione del pulsante.")
                    if slot_setup.reexplore(device_id, slot_name, "spin").success:
                        consecutive_no_effect = 0
                        spin_index -= 1
                        continue
                verdict = _ask_operator(
                    autopilot, device_id,
                    f"{consecutive_no_effect} spin senza addebito: il pulsante spin non risponde (forse un menu o un "
                    f"pop-up copre il gioco).", log, help_state,
                )
                if verdict == "retry" and _recheck_bet(autopilot, device_id, log):
                    consecutive_no_effect = 0
                    spin_index -= 1
                    continue
                failed, stop_reason = True, "NO_EFFECT"
                break
        else:
            consecutive_no_effect = 0
        if spin_result.data.outcome == "success" and spin_result.data.bet_amount:
            total_played += spin_result.data.bet_amount
            correction = _check_bet_window(recent_spins, spin_result.data, autopilot, device_id, log)
            if correction:
                total_played -= correction
                log.warning("Giocato corretto di -%s EUR: ora %s.", correction, total_played)
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
                lost_after = (LOST_CHECK_UNREADABLE_SECONDS
                              if "non leggibil" in str(getattr(spin_result.data, "blocked_detail", None) or "")
                              else LOST_CHECK_AFTER_SECONDS)
                if waited >= lost_after and not lost_checked and reenter is not None:
                    lost_checked = True
                    if _check_lost(autopilot, device_id) == "lost" and try_reenter(
                        f"schermo occupato da {waited:.0f} s e la slot non e' piu' sullo schermo",
                    ):
                        consecutive_no_effect, busy_since, busy_asked, lost_checked = 0, None, False, False
                        recent_spins.clear()
                        spin_index -= 1
                        continue
                if waited >= BUSY_ASK_AFTER_SECONDS and not busy_asked:
                    busy_asked = True
                    verdict = _ask_if_ready(autopilot, device_id)
                    if verdict == "lost" and try_reenter("Sol: la slot non e' piu' sullo schermo"):
                        consecutive_no_effect, busy_since, busy_asked, lost_checked = 0, None, False, False
                        recent_spins.clear()
                        spin_index -= 1
                        continue
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
                if _check_lost(autopilot, device_id) == "lost" and try_reenter(
                    "lo schermo e' occupato da troppo e la slot non e' piu' sullo schermo",
                ):
                    consecutive_no_effect, busy_since, busy_asked, lost_checked = 0, None, False, False
                    recent_spins.clear()
                    spin_index -= 1
                    continue
                verdict = _ask_operator(
                    autopilot, device_id,
                    f"lo schermo risulta occupato da oltre {MAX_BUSY_WAIT_SECONDS} s (vincita, bonus o pop-up?).",
                    log, help_state,
                )
                if verdict == "retry" and _recheck_bet(autopilot, device_id, log):
                    busy_since, busy_asked = None, False
                    spin_index -= 1
                    continue
                failed, stop_reason = True, "BUSY_TIMEOUT"
            else:
                if _check_lost(autopilot, device_id) == "lost" and try_reenter(
                    f"spin bloccato ({spin_result.data.violated_invariant}) e la slot non e' piu' sullo schermo",
                ):
                    consecutive_no_effect, busy_since, busy_asked, lost_checked = 0, None, False, False
                    recent_spins.clear()
                    spin_index -= 1
                    continue
                log.warning("Spin bloccato da un'invariante di sicurezza: %s", spin_result.data.violated_invariant)
                stop_reason = "BLOCKED"
            break
    log.info("Sessione conclusa: %s/%s EUR giocati in %d spin.", total_played, target, spin_index)
    averages = average_timings(collected_timings)
    if averages is not None:
        log.info("%s", timing_summary_line(averages, len([t for t in collected_timings if isinstance(t, dict)])))
    _log_capture_stats(autopilot, log)
    _log_arrow_stats(autopilot, log)
    _log_slot_memory(autopilot, log)

    initial_balance, final_balance = autopilot.get_balance_summary()
    net = None
    if initial_balance is not None and final_balance is not None:
        net = reentry["net"] + final_balance - initial_balance
        if reentry["n"]:
            initial_balance = first_initial if first_initial is not None else initial_balance
            log.info(
                "Riepilogo: %d rientri nella slot, netto totale=%s EUR (somma dei netti di ogni ingresso: in demo il "
                "saldo si ripristina quando si rientra).", reentry["n"], net,
            )
        else:
            log.info(
                "Riepilogo: saldo iniziale=%s, saldo finale=%s, netto=%s EUR",
                initial_balance, final_balance, net,
            )
    return SlotSessionResult(
        total_played, spin_index, failed=failed, stop_reason=stop_reason,
        initial_balance=initial_balance, final_balance=final_balance, target=target, avg_timings=averages,
        reentries=reentry["n"], net=net,
    )
