"""Promo completa con un solo comando: navigazione (promo -> attivazione -> slot -> "Prova") e gioco
(puntata minima verificata -> spin fino al target) sullo stesso telefono, con un unico log e il costo AI totale.

Riusa ScreenNavigator (fasi 1-4) e SlotAutoPilot/play_session (fasi 5-7) senza modificarli.

Uso:
    python -m orchestrator.scripts.run_promo_complete --device ZE223HNBKJ --book SISAL \
        --promo-name "Happy Monday" --slot-name "Break The Diamond Piggy" --target 30 --max-steps 200 --demo
"""
import argparse
import logging
import os
import sys
import time
from decimal import Decimal

from orchestrator.core import model_metrics
from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.session import _setup_of
from orchestrator.modules.slot_autopilot.slot_intro import build_slot_intro
from orchestrator.scripts import run_autopilot
from orchestrator.tests.validation import test_casino_navigation as nav_script

logger = logging.getLogger(__name__)

EXIT_BUTTON_HINT = "Esci"
EXIT_WAIT_SECONDS = 3
# Arresti che lasciano la slot in uno stato incerto: non si tocca nulla.
_CRITICAL_STOPS = ("PREPARE_FAILED", "BET_NOT_VERIFIED", "SPIN_ERROR", "BLOCKED")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Navigazione + gioco di una promo casino in un solo comando.")
    parser.add_argument("--device", required=True, help="ID del dispositivo ADB.")
    parser.add_argument("--book", default="SISAL")
    parser.add_argument("--promo-name", required=True)
    parser.add_argument("--slot-name", required=True)
    parser.add_argument("--target", type=float, required=True, help="Importo target da giocare (EUR).")
    parser.add_argument("--max-steps", type=int, default=200, help="Massimo passi di navigazione e di spin.")
    parser.add_argument("--url", default=nav_script.SISAL_PROMOTIONS_URL)
    parser.add_argument("--demo", action="store_true", help="Obbligatorio: la slot si apre in demo (Prova), mai soldi veri.")
    activation = parser.add_mutually_exclusive_group()
    activation.add_argument("--already-active", action="store_true")
    activation.add_argument("--not-active", action="store_true")
    parser.add_argument("--live", action="store_true", help="Disattiva il dry-run dell'autopilot.")
    parser.add_argument("--no-gpt", action="store_true")
    parser.add_argument("--min-bet", type=float, default=None,
                        help="Puntata minima NOTA della slot (EUR): vale come minimo dimostrato e ripara la memoria.")
    parser.add_argument("--reset-slot-memory", action="store_true",
                        help="Prima di partire sposta in slot_memory/_backup/ la memoria di questa slot (riparte da zero).")
    parser.add_argument("--nav-direct", action="store_true",
                        help="Navigazione: un punto diretto di Sol al posto della griglia icone (la griglia resta come ripiego).")
    parser.add_argument("--no-hitl", action="store_true", help="Niente operatore da terminale: se serve aiuto ci si ferma.")
    parser.add_argument("--no-save-steps", action="store_true")
    parser.add_argument("--log-dir", default="logs")
    return parser.parse_args(argv)


def _client_cost(client) -> tuple:
    """(chiamate, costo, completo): costo sommato sui modelli; completo=False se un prezzo e' ignoto."""
    usage = getattr(client, "_usage", None) or {}
    calls = sum(entry["calls"] for entry in usage.values())
    known = [entry["cost"] for entry in usage.values() if entry["cost"] is not None]
    return calls, sum(known), len(known) == len(usage)


def total_cost(*clients) -> dict:
    """Costo AI sommato su piu' client (None = client assente)."""
    calls, cost, complete = 0, 0.0, True
    for client in clients:
        if client is None:
            continue
        c_calls, c_cost, c_complete = _client_cost(client)
        calls += c_calls
        cost += c_cost
        complete = complete and c_complete
    return {"calls": calls, "cost": cost, "complete": complete}


def cost_line(navigation_client, play_client) -> str:
    nav, play, total = total_cost(navigation_client), total_cost(play_client), total_cost(navigation_client, play_client)

    def fmt(entry):
        return f"~${entry['cost']:.4f}" + ("" if entry["complete"] else " (parziale: prezzo di un modello ignoto)")
    return (f"COSTO AI TOTALE: navigazione {fmt(nav)} ({nav['calls']} chiamate) + gioco {fmt(play)} "
            f"({play['calls']} chiamate) = {fmt(total)} ({total['calls']} chiamate)")


def navigate_to_slot(args, navigation_client, screen_reader, touch_injector) -> Result[int]:
    """Fasi 1-4: da Chrome alla slot aperta in demo. Result con il numero di passi."""
    from orchestrator.modules.book_navigator.navigator import BookNavigator
    from orchestrator.modules.screen_navigator.navigator import ScreenNavigator
    from orchestrator.modules.slot_autopilot.strategies.gpt_locator_strategy import build_region_refiner

    stamp = time.strftime("%Y%m%d_%H%M%S")
    steps_dir = os.path.join(args.log_dir, f"promo_complete_{stamp}")
    step_saver = None if args.no_save_steps else nav_script.make_step_saver(steps_dir)
    book_navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)
    nav_stats = {"direct": 0, "grid": 0}
    nav_refiner = build_region_refiner(navigation_client)
    if getattr(args, "nav_direct", False):
        from orchestrator.modules.screen_navigator.direct_point import build_direct_refiner
        nav_refiner = build_direct_refiner(navigation_client, nav_refiner, nav_stats)
    navigator = ScreenNavigator(
        screen_reader=screen_reader, touch_injector=touch_injector, decision_engine_client=navigation_client,
        region_refiner=nav_refiner, max_steps=args.max_steps, step_observer=step_saver,
        app_guard=lambda device: book_navigator.is_app_in_front(device, nav_script.CHROME_PACKAGE),
        app_restore=lambda device: book_navigator.bring_to_front(device, nav_script.CHROME_PACKAGE),
    )
    opened = book_navigator.open_chrome(args.device, args.url)
    if not opened.success:
        return Result.fail(f"impossibile aprire Chrome: {opened.error}", opened.error_code)
    goal = nav_script.build_goal(
        args.promo_name, args.slot_name, nav_script.already_active_mode(args), stop_at_prova=True,
    )
    logger.info("Goal: %s", goal)
    result = navigator.navigate(args.device, goal=goal, book=args.book, demo_mode=True, vision_mode=True)
    for entry in navigator.last_history:
        logger.info("  %2s. %s -> %s", entry["step"], entry["action"], entry["target"])
    if getattr(args, "nav_direct", False):
        logger.info("Navigazione: %d punti diretti di Sol, %d ricerche a griglia (ripiego).",
                    nav_stats["direct"], nav_stats["grid"])
    if not result.success:
        return Result.fail(result.error, result.error_code)
    return Result.ok(len(navigator.last_history))


def exit_slot(screen_reader, touch_injector, device_id: str, sleep=time.sleep) -> Result[bool]:
    """Esce dalla slot toccando "Esci". Mai bloccante: se non c'e' si segnala e basta."""
    try:
        screen = screen_reader.read_screen(device_id)
        found = screen.success and any(
            EXIT_BUTTON_HINT.lower() in (element.text or "").lower() for element in screen.data.elements
        )
        if not found:
            logger.warning("Pulsante '%s' non trovato: resto nella slot (il target e' gia' raggiunto).", EXIT_BUTTON_HINT)
            return Result.fail("Esci non trovato", "EXIT_NOT_FOUND")
        tap = touch_injector.tap_element(device_id, EXIT_BUTTON_HINT)
        if not tap.success:
            logger.warning("Tocco su '%s' fallito: %s", EXIT_BUTTON_HINT, tap.error)
            return Result.fail(tap.error, tap.error_code)
        sleep(EXIT_WAIT_SECONDS)
        logger.info("Uscita dalla slot effettuata")
        return Result.ok(True)
    except Exception as exc:
        logger.warning("Uscita dalla slot non riuscita: %s", exc)
        return Result.fail(str(exc), "EXIT_ERROR")


def _prepare_slot_map(autopilot, args) -> None:
    """Mappa della slot PRIMA di giocare (dopo navigazione e intro): trovata -> coordinate salvate, zero GPT;
    assente -> la esplora Sol e la salva. play_session richiama prepare(): e' idempotente, non rifa' nulla."""
    setup = _setup_of(autopilot)
    if setup is None:
        logger.warning("Slot memory NON attiva (GPT spento o SLOT_MAP=0): uso il motore classico, senza mappa.")
        return
    logger.info("Slot memory: preparo la slot '%s' (minimo dichiarato: %s).", args.slot_name,
                args.min_bet if args.min_bet is not None else "nessuno")
    outcome = setup.prepare(args.device, args.slot_name)
    if not outcome.success:
        logger.warning("Slot memory: preparazione non riuscita (%s): %s", outcome.error_code, outcome.error)


def make_reenter(args, navigation_client, autopilot):
    """Rientro nella slot dopo che il sito ci ha cacciati (sessione scaduta, pagina di errore): si riparte dall'URL
    del sito, non da "indietro". La promo ormai e' attiva: promo -> nome della slot -> "Prova", poi le intro."""
    import copy

    def reenter(device_id: str) -> Result[bool]:
        rejoin_args = copy.copy(args)
        rejoin_args.already_active, rejoin_args.not_active = True, False
        nav = navigate_to_slot(rejoin_args, navigation_client, autopilot.screen_reader, autopilot.touch_injector)
        if not nav.success:
            return Result.fail(f"navigazione di rientro fallita: {nav.error}", nav.error_code)
        intro = build_slot_intro(autopilot).run(device_id, args.slot_name)
        if not intro.success:
            return Result.fail(f"intro di rientro non superate: {intro.error}", intro.error_code)
        return Result.ok(True)

    return reenter


def _ask_intro_help(autopilot, args) -> str:
    """Intro non superata: prima di giocare si chiede all'operatore (se c'e')."""
    hitl = getattr(autopilot, "hitl_strategy", None)
    ask = getattr(hitl, "ask_for_help", None)
    if not callable(ask) or not isinstance(getattr(hitl, "interventions", None), int):
        return "continue"
    try:
        return "stop" if ask(args.device, "le schermate d'ingresso della slot (benvenuto, schermo intero, Continua) "
                                          "non si superano da sole.") == "stop" else "continue"
    except (EOFError, KeyboardInterrupt):
        return "stop"


def run(args) -> int:
    from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient
    from orchestrator.modules.slot_autopilot.session import play_session

    log_path = run_autopilot._configure_logging(args.log_dir, f"promo_{args.slot_name}")
    logger.info("Log unico (navigazione + gioco): %s", log_path)
    run_id = model_metrics.start_run("promo_completa", args.slot_name.replace(" ", "_"))

    if getattr(args, "reset_slot_memory", False):
        run_autopilot._reset_slot_memory(args.slot_name)

    # Fail-fast: chiave rifiutata o configurazione invalida prima di spendere la navigazione.
    build = run_autopilot.build_autopilot_for(
        device_id=args.device, slot_name=args.slot_name, target=args.target, max_steps=args.max_steps,
        enable_gpt=not args.no_gpt, enable_hitl=not args.no_hitl, live=args.live, known_min_bet=args.min_bet,
    )
    if not build.success:
        logger.error("Impossibile inizializzare SlotAutoPilot: %s (%s)", build.error, build.error_code)
        return 1
    autopilot = build.data
    try:
        navigation_client = GPTDecisionClient(model=nav_script.EXPECTED_MODEL)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    exit_code, nav_ok, session, nav_seconds, play_seconds, stop = 1, False, None, 0.0, 0.0, None
    try:
        logger.info("=== FASE NAVIGAZIONE: %s -> %s -> %s ===", args.book, args.promo_name, args.slot_name)
        started = time.monotonic()
        nav = navigate_to_slot(args, navigation_client, autopilot.screen_reader, autopilot.touch_injector)
        nav_seconds = time.monotonic() - started
        nav_ok = nav.success
        if not nav_ok:
            logger.error("Navigazione fallita: %s (%s): non avvio il gioco.", nav.error, nav.error_code)
        else:
            logger.info("Navigazione completata in %d passi (%.0f s): slot aperta in demo, passo al gioco.",
                        nav.data, nav_seconds)
            intro = build_slot_intro(autopilot).run(args.device, args.slot_name)
            if not intro.success:
                logger.warning("Schermate d'ingresso non superate (%s): %s", intro.error_code, intro.error)
                asked = _ask_intro_help(autopilot, args)
                if asked == "stop":
                    raise KeyboardInterrupt
            else:
                logger.info("Schermate d'ingresso superate: %d giri, %d tocchi, %d nuove ricordate.",
                            intro.data.rounds, intro.data.taps, intro.data.learned)
            _prepare_slot_map(autopilot, args)
            logger.info("=== FASE GIOCO: %s, target %s EUR ===", args.slot_name, args.target)
            started = time.monotonic()
            session = play_session(autopilot, args.device, args.slot_name, Decimal(str(args.target)),
                                   args.max_steps, log=logger,
                                   reenter=make_reenter(args, navigation_client, autopilot))
            play_seconds = time.monotonic() - started
            stop = session.stop_reason
            exit_code = 1 if session.failed else 0
            if stop in _CRITICAL_STOPS:
                logger.warning("Arresto %s: non esco dalla slot, stato incerto.", stop)
            else:
                exit_slot(autopilot.screen_reader, autopilot.touch_injector, args.device)
    except KeyboardInterrupt:
        logger.warning("Interrotto dall'operatore (KeyboardInterrupt).")
        exit_code = 1
    finally:
        play_client = getattr(autopilot, "gpt_client", None)
        hitl = getattr(autopilot, "hitl_strategy", None)
        for client in (navigation_client, play_client):
            if client is not None:
                logger.info("%s", client.usage_summary())
        logger.info("%s", cost_line(navigation_client, play_client))
        logger.info(
            "ESITO: navigazione %s, spin %s, giocato %s EUR, free spin %s (non contati), rientri %s, stop=%s; tempi: navigazione %.0f s, gioco "
            "%.0f s; interventi operatore: %d", "ok" if nav_ok else "FALLITA", getattr(session, "spins", 0),
            getattr(session, "total_played", 0), getattr(session, "free_spins", 0), getattr(session, "reentries", 0), stop,
            nav_seconds, play_seconds,
            getattr(hitl, "interventions", 0),
        )
        try:
            nav_cost, play_cost = total_cost(navigation_client), total_cost(play_client)
            model_metrics.log_run(
                type_of_run="promo_completa", model=navigation_client.model, book=args.book, device=args.device,
                promo=args.promo_name, slot=args.slot_name, navigation_ok=nav_ok, stop_reason=stop,
                spins=getattr(session, "spins", 0), total_played=str(getattr(session, "total_played", 0)),
                nav_seconds=round(nav_seconds, 1), play_seconds=round(play_seconds, 1),
                cost_navigation=round(nav_cost["cost"], 4), cost_play=round(play_cost["cost"], 4),
                cost_total=round(nav_cost["cost"] + play_cost["cost"], 4), run_id=run_id,
            )
        except Exception as exc:
            logger.debug("metriche del run non scritte: %s", exc)
        shutdown = autopilot.shutdown(args.device)
        if not shutdown.success:
            logger.error("shutdown() fallito: %s (%s)", shutdown.error, shutdown.error_code)
    return exit_code


def main(argv=None) -> None:
    args = parse_args(argv)
    if not args.demo:
        print("Rifiuto di partire senza --demo: la slot si apre in modalita' demo (Prova), mai soldi veri.")
        sys.exit(2)
    sys.exit(run(args))


if __name__ == "__main__":
    main()
