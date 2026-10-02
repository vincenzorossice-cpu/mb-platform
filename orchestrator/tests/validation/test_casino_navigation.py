"""
Validazione manuale della navigazione casino con GPT-6.1 Sol Vision.

Parte dalla pagina promozioni di un book (Sisal) e lascia che ScreenNavigator, con
vision_mode=True, arrivi dentro una slot in modalita' DEMO: promo -> attivazione ->
verifica del messaggio -> slot -> "Prova" (mai "Gioca") -> stop.

Il telefono deve avere Chrome e l'utente gia' loggato sul book: il login non e' gestito.
Serve OPENAI_API_KEY. Il modello e' gpt-6.1-sol (DECISION_ENGINE_PROVIDER=gpt6sol o GPT_MODEL).

Uso:
    python -m orchestrator.tests.validation.test_casino_navigation --device-id ZE223HNBKJ --demo
"""

import argparse
import os
import shutil
import sys
import time

SISAL_PROMOTIONS_URL = "https://www.sisal.it/bonus/promozioni"
EXPECTED_MODEL = "gpt-6.1-sol"
LOGS_DIR = "logs"


def build_goal(promo_name: str, slot_name: str) -> str:
    return (
        f"Percorso in ordine, una fase alla volta. "
        f"FASE 1: nella lista delle promozioni trova la promo '{promo_name}' e toccala. "
        f"FASE 2: nella pagina della promo tocca 'Attiva la promozione' oppure 'Aderisci'. "
        f"FASE 3: verifica che sullo schermo sia scritto 'Promozione attivata correttamente'; "
        f"non andare avanti finche' questo testo non e' leggibile. "
        f"FASE 4: trova la slot '{slot_name}' e tocca il suo nome. "
        f"FASE 5: nella pagina della slot tocca 'Prova' (modalita' demo, for fun), MAI 'Gioca'. "
        f"Rispondi 'done' SOLO quando sei dentro la slot in demo, cioe' vedi il gioco caricato "
        f"(rulli e pulsante di spin). Non giocare nessuno spin."
    )


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Valida la navigazione casino con GPT-6.1 Sol Vision.")
    parser.add_argument("--device-id", default="ZE223HNBKJ", help="ID ADB del telefono (default: ZE223HNBKJ).")
    parser.add_argument("--book", default="SISAL")
    parser.add_argument("--promo-name", default="Slot in Esclusiva")
    parser.add_argument("--slot-name", default="Turbo Storm Typhoon")
    parser.add_argument("--url", default=SISAL_PROMOTIONS_URL)
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--demo", action="store_true", help="Obbligatorio: il test entra in demo (Prova), mai soldi veri.")
    return parser.parse_args(argv)


def _save_final_screenshot(screen_reader, device_id: str) -> str:
    shot = screen_reader._screenshot(device_id)
    if not shot.success:
        return ""
    os.makedirs(LOGS_DIR, exist_ok=True)
    target = os.path.join(LOGS_DIR, f"casino_navigation_final_{time.strftime('%Y%m%d_%H%M%S')}.png")
    shutil.copyfile(shot.data, target)
    return target


def run_navigation(args: argparse.Namespace) -> bool:
    from orchestrator.modules.book_navigator.navigator import BookNavigator
    from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient
    from orchestrator.modules.screen_navigator.navigator import ScreenNavigator
    from orchestrator.modules.screen_reader.reader import ScreenReader
    from orchestrator.modules.slot_autopilot.strategies.gpt_locator_strategy import build_region_refiner
    from orchestrator.modules.touch_injector.injector import TouchInjector

    if not os.environ.get("GPT_MODEL"):
        os.environ.setdefault("DECISION_ENGINE_PROVIDER", "gpt6sol")
    try:
        client = GPTDecisionClient()
    except ValueError as exc:
        print(f"ERRORE: {exc}")
        return False
    print(f"Modello: {client.model}")
    if client.model != EXPECTED_MODEL:
        print(f"ERRORE: il test richiede {EXPECTED_MODEL}, ma il modello attivo e' {client.model}.")
        return False

    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    book_navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)
    navigator = ScreenNavigator(
        screen_reader=screen_reader, touch_injector=touch_injector, decision_engine_client=client,
        region_refiner=build_region_refiner(client), max_steps=args.max_steps,
    )

    print(f"Apro {args.url} su {args.device_id}...")
    opened = book_navigator.open_chrome(args.device_id, args.url)
    if not opened.success:
        print(f"ERRORE FATALE: impossibile aprire Chrome: {opened.error} ({opened.error_code})")
        return False

    goal = build_goal(args.promo_name, args.slot_name)
    print(f"Goal: {goal}\n")
    result = navigator.navigate(args.device_id, goal=goal, book=args.book, demo_mode=True, vision_mode=True)

    print("\n=== STORICO AZIONI ===")
    for entry in navigator.last_history:
        print(f"  {entry['step']:>2}. {entry['action']} -> {entry['target']}")
    print(client.usage_summary())
    final_shot = _save_final_screenshot(screen_reader, args.device_id)
    if final_shot:
        print(f"Screenshot finale: {final_shot}")

    if result.success:
        print("\nNAVIGAZIONE COMPLETATA - slot pronta per gioco")
        print("Controlla lo screenshot finale: 'done' e' deciso dal modello, non verificato dal codice.")
        return True
    print(f"\nERRORE: navigazione fallita: {result.error} ({result.error_code})")
    return False


def main(argv=None) -> None:
    args = parse_args(argv)
    if not args.demo:
        print("Rifiuto di partire senza --demo: questo test entra nella slot in modalita' demo (Prova).")
        sys.exit(2)
    sys.exit(0 if run_navigation(args) else 1)


if __name__ == "__main__":
    main()
