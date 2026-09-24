"""
Script di validazione manuale: gioco slot guidato da un Decision Engine
(Open-Jev o GPT-4o-mini, a scelta via --provider).

Test focalizzato SOLO sul gioco della slot: verifica che il Decision Engine
riesca a leggere lo stato della slot via OCR (SALDO/POSTA/VINCITA/tasto SPIN)
e a decidere se girare o fermarsi.

L'utente apre MANUALMENTE la slot in modalita' demo (for fun) sul telefono
prima di lanciare lo script: la slot e' Canvas/WebGL, quindi uiautomator non
legge nulla e serve OCR (EasyOCR, via SlotOCRReader) sullo screenshot.

Prerequisiti:
- Slot gia' aperta in modalita' DEMO (for fun, soldi virtuali) sul telefono.
- easyocr installato (gia' in orchestrator/requirements.txt).
- --provider openjev: server Open-Jev raggiungibile (env var DECISION_ENGINE_URL
  o --engine-url).
- --provider gpt4o: env var OPENAI_API_KEY impostata.

Uso:
    python -m orchestrator.tests.validation.test_slot_play \
        --device-id ZE223HNBKJ --target-spins 10 --provider gpt4o
"""

import argparse
import sys
import time
from typing import Optional

from orchestrator.flows.casino_flow import SlotOCRReader
from orchestrator.modules.decision_engine.interface import (
    DecisionEngineClientInterface,
    choice_question,
    noul_question,
)
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.touch_injector.injector import TouchInjector

FALLBACK_SPIN_COORDS = (540, 1900)


def build_engine(provider: str, engine_url: Optional[str]) -> DecisionEngineClientInterface:
    if provider == "gpt4o":
        from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient

        return GPTDecisionClient()

    from orchestrator.modules.decision_engine.client import DecisionEngineClient

    return DecisionEngineClient(base_url=engine_url) if engine_url else DecisionEngineClient()


def build_state_text(
    saldo: Optional[float],
    posta: Optional[float],
    vincita: Optional[float],
    spin_coords: Optional[tuple],
    spins_done: int,
    target_spins: int,
    previous_balance: Optional[float],
) -> str:
    return f"""You are inside a slot machine game (demo mode, virtual money).
Goal: Play {target_spins} spins at minimum bet, then stop.

Current screen (via OCR):
- SALDO: {saldo}
- POSTA (bet): {posta}
- VINCITA (last win): {vincita}
- SPIN button: {"visible at " + str(spin_coords) if spin_coords else "not found"}

Spin history: {spins_done} spins done, {target_spins} target
Previous balance: {previous_balance if previous_balance is not None else "N/A"}
Current balance: {saldo}
"""


def build_questions() -> dict:
    return {
        "action": choice_question(
            "You are playing a slot machine (demo mode, virtual money). Should I spin again or stop?",
            {
                "spin": "Spin again - balance is sufficient and goal not reached",
                "stop": "Stop - goal reached or balance too low",
            },
        ),
        "balance_ok": noul_question(
            "Is the current balance sufficient for another spin at the current bet amount?"
        ),
    }


def read_slot_state(screen_reader: ScreenReader, ocr_reader: SlotOCRReader, device_id: str) -> Optional[dict]:
    ss = screen_reader._screenshot(device_id)
    if not ss.success:
        print(f"ERRORE: screenshot fallito: {ss.error} ({ss.error_code})")
        return None
    return ocr_reader.read_slot_state(ss.data)


def run_slot_session(
    device_id: str, target_spins: int, max_spins: int, engine_url: Optional[str], provider: str = "openjev",
) -> bool:
    """Gioca la slot gia' aperta sul device, delegando al Decision Engine
    scelto (Open-Jev o GPT-4o-mini) la decisione spin/stop ad ogni giro.
    Ritorna True se la sessione si e' conclusa in modo pulito (target
    raggiunto o stop deciso), False in caso di errore."""
    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    ocr_reader = SlotOCRReader()
    engine = build_engine(provider, engine_url)

    print(f"=== TEST GIOCO SLOT (provider: {provider}) ===")
    print(f"Device: {device_id}")
    print(f"Target: {target_spins} spin, max {max_spins}")
    print("Assicurati che la slot sia aperta in modalita' DEMO sul telefono.")
    input("Premi INVIO per iniziare...")

    state = read_slot_state(screen_reader, ocr_reader, device_id)
    if state is None:
        return False

    initial_balance = state.get("saldo")
    current_balance = initial_balance
    previous_balance = None

    print(f"Saldo iniziale: {initial_balance}")
    print(f"Posta: {state.get('posta')}")
    print(f"Tasto SPIN: {state.get('spin_button_coords')}")

    spins_done = 0
    spin_history: list[dict] = []

    while spins_done < max_spins:
        state = read_slot_state(screen_reader, ocr_reader, device_id)
        if state is None:
            return False

        saldo = state.get("saldo")
        posta = state.get("posta")
        vincita = state.get("vincita")
        spin_coords = state.get("spin_button_coords")

        if saldo is not None:
            current_balance = saldo

        print(f"\n=== Spin {spins_done + 1}/{target_spins} ===")
        print(f"Saldo: {saldo}")
        print(f"Posta: {posta}")
        print(f"Vincita: {vincita}")

        if saldo is not None and posta is not None and saldo < posta:
            print("Saldo insufficiente per un altro spin. Stop.")
            break

        state_text = build_state_text(
            saldo, posta, vincita, spin_coords, spins_done, target_spins, previous_balance,
        )
        questions = build_questions()

        print(f"Chiedo a {provider}...")
        result = engine.ask(state_text, questions)
        if not result.success:
            print(f"ERRORE: Decision Engine: {result.error} ({result.error_code})")
            return False

        answers = result.data.get("answers", {})
        action = answers.get("action", {}).get("choice", "stop")
        confidence = answers.get("action", {}).get("confidence", 0.0)
        balance_ok = answers.get("balance_ok", {}).get("noul", 0.5)

        print(f"Decisione: {action} (confidence: {confidence:.2f}, balance_ok: {balance_ok:.2f})")

        if action == "stop":
            print("Il Decision Engine dice STOP. Fine del test.")
            break

        tap_coords = spin_coords or FALLBACK_SPIN_COORDS
        if not spin_coords:
            print(f"Il Decision Engine dice SPIN ma non trovo il tasto SPIN via OCR, uso fallback {FALLBACK_SPIN_COORDS}.")

        print(f"Tapping SPIN at {tap_coords}...")
        tap_result = touch_injector.tap_coordinates(device_id, tap_coords[0], tap_coords[1])
        if not tap_result.success:
            print(f"ERRORE: tap fallito: {tap_result.error} ({tap_result.error_code})")

        previous_balance = current_balance
        spins_done += 1
        spin_history.append({"spin": spins_done, "balance": current_balance})
        time.sleep(2)

        if spins_done >= target_spins:
            print(f"Target di {target_spins} spin raggiunto.")
            break

    print("\n=== RISULTATO ===")
    print(f"Spin fatti: {spins_done}/{target_spins}")
    print(f"Saldo iniziale: {initial_balance}")
    print(f"Saldo finale: {current_balance}")
    if initial_balance is not None and current_balance is not None:
        print(f"Differenza: {current_balance - initial_balance:+.2f} €")
    print("\nStorico:")
    for s in spin_history:
        print(f"  Spin {s['spin']}: saldo {s['balance']}")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida il gioco della slot guidato da un Decision Engine (Open-Jev o GPT-4o-mini, spin/stop via OCR)."
    )
    parser.add_argument("--device-id", required=True, help="ID del dispositivo ADB (es. emulator-5554 o seriale reale).")
    parser.add_argument("--target-spins", type=int, default=10, help="Numero di spin obiettivo (default: 10).")
    parser.add_argument("--max-spins", type=int, default=20, help="Numero massimo di spin di sicurezza (default: 20).")
    parser.add_argument(
        "--engine-url", default=None,
        help="URL del Decision Engine Open-Jev (default: env var DECISION_ENGINE_URL o http://localhost:8001). "
        "Ignorato con --provider gpt4o.",
    )
    parser.add_argument(
        "--provider", choices=["openjev", "gpt4o"], default="openjev",
        help="Decision Engine da usare: 'openjev' (default, richiede il server locale) o "
        "'gpt4o' (richiede OPENAI_API_KEY).",
    )
    args = parser.parse_args()

    ok = run_slot_session(
        device_id=args.device_id,
        target_spins=args.target_spins,
        max_spins=args.max_spins,
        engine_url=args.engine_url,
        provider=args.provider,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
