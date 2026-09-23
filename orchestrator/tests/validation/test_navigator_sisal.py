"""
Script di validazione manuale per ScreenNavigator su Sisal.

Apre direttamente la pagina promozioni di Sisal (bypassando la home page) e
lascia che ScreenNavigator, guidato dal Decision Engine, raggiunga e attivi
la promo richiesta.

Il device deve avere Chrome installato e (se richiesto dalla promo) l'utente
gia' loggato su Sisal: questo script non gestisce il login.

Uso:
    python -m orchestrator.tests.validation.test_navigator_sisal \
        --device-id ZT322ZS58S --promo-name "Slot in Anteprima"

    # Modalita' demo (for fun, nessuna spesa reale):
    python -m orchestrator.tests.validation.test_navigator_sisal \
        --device-id ZT322ZS58S --promo-name "Slot in Anteprima" --demo
"""

import argparse
import sys

from orchestrator.modules.book_navigator.navigator import BookNavigator
from orchestrator.modules.decision_engine.client import DecisionEngineClient
from orchestrator.modules.screen_navigator.navigator import ScreenNavigator
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.touch_injector.injector import TouchInjector

SISAL_PROMOTIONS_URL = "https://www.sisal.it/bonus/promozioni"


def build_goal(promo_name: str, demo: bool) -> str:
    goal = f"Attiva la promozione '{promo_name}'"
    if demo:
        goal += " in modalita' demo (for fun, senza soldi veri)"
    return goal


def run_navigation(device_id: str, promo_name: str, demo: bool, max_steps: int) -> bool:
    """Apre la pagina promozioni Sisal e lascia che ScreenNavigator raggiunga
    l'obiettivo. Ritorna True se l'obiettivo e' stato raggiunto."""
    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    book_navigator = BookNavigator(screen_reader=screen_reader, touch_injector=touch_injector)
    decision_engine_client = DecisionEngineClient()
    navigator = ScreenNavigator(
        screen_reader=screen_reader,
        touch_injector=touch_injector,
        decision_engine_client=decision_engine_client,
        max_steps=max_steps,
    )

    print(f"Apro {SISAL_PROMOTIONS_URL} su {device_id}...")
    open_result = book_navigator.open_chrome(device_id, SISAL_PROMOTIONS_URL)
    if not open_result.success:
        print(f"ERRORE FATALE: impossibile aprire Chrome: {open_result.error} ({open_result.error_code})")
        return False

    goal = build_goal(promo_name, demo)
    print(f"Goal: {goal}")

    result = navigator.navigate(device_id, goal=goal, book="SISAL", demo_mode=demo)

    if result.success:
        print("Obiettivo raggiunto.")
        return True

    print(f"ERRORE: navigazione fallita: {result.error} ({result.error_code})")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida ScreenNavigator su Sisal partendo direttamente dalla pagina promozioni."
    )
    parser.add_argument("--device-id", required=True, help="ID del dispositivo ADB (es. emulator-5554 o seriale reale).")
    parser.add_argument(
        "--promo-name", required=True, help="Nome della promo da cercare e attivare (es. 'Slot in Anteprima').",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Modalita' demo: cerca 'Prova'/'For fun' invece di 'Gioca' con soldi veri.",
    )
    parser.add_argument(
        "--max-steps", type=int, default=20, help="Numero massimo di step di navigazione (default: 20).",
    )
    args = parser.parse_args()

    ok = run_navigation(
        device_id=args.device_id,
        promo_name=args.promo_name,
        demo=args.demo,
        max_steps=args.max_steps,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
