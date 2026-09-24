"""
Script di validazione manuale: GPT-4o-mini gioca la slot in piena autonomia.

A differenza di test_slot_play.py (che chiede solo "spin o stop?"), qui il
Decision Engine vede TUTTI i testi OCR con le loro coordinate e decide da solo
cosa tappare ad ogni passo (impostare la posta al minimo, poi spinnare, ecc.).
Non c'e' nessuna logica hardcoded su dove si trovi il tasto spin o il menu
posta: cambia da slot a slot, quindi decide GPT-4o-mini guardando lo schermo.

L'utente apre MANUALMENTE la slot in modalita' demo (for fun) sul telefono
prima di lanciare lo script: la slot e' Canvas/WebGL, quindi uiautomator non
legge nulla e serve OCR (EasyOCR, via SlotOCRReader) sullo screenshot.

Prerequisiti:
- Slot gia' aperta in modalita' DEMO (for fun, soldi virtuali) sul telefono.
- easyocr installato (gia' in orchestrator/requirements.txt).
- --provider gpt4o (default): env var OPENAI_API_KEY impostata. Questo script
  ha senso soprattutto con GPT-4o-mini: Open-Jev (modello locale piccolo)
  difficilmente regge un ragionamento libero su un dump OCR di decine di testi.

Uso:
    python -m orchestrator.tests.validation.test_slot_autonomous \
        --device-id ZE223HNBKJ --target-amount 30 --provider gpt4o
"""

import argparse
import difflib
import logging
import sys
import time
from typing import Optional

from orchestrator.flows.casino_flow import SlotOCRReader
from orchestrator.modules.decision_engine.interface import choice_question
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.touch_injector.injector import TouchInjector
from orchestrator.tests.validation.test_slot_play import build_engine

logger = logging.getLogger(__name__)

OCR_REFRESH_INTERVAL = 3  # step tra una lettura OCR completa e l'altra
MIN_BET = 0.10
DEFAULT_MAX_STEPS = 50
DEFAULT_TARGET_AMOUNT = 30.0
FALLBACK_TAP_COORDS = (540, 1900)


def format_ocr_elements(raw_texts: list) -> str:
    return "\n".join(f"[{x},{y}] {text}" for text, (x, y), _conf in raw_texts)


def build_state_text(
    raw_texts: list,
    initial_balance: Optional[float],
    current_balance: Optional[float],
    total_played: float,
    target_amount: float,
    step: int,
    action_history: list,
) -> str:
    balance_change = None
    if initial_balance is not None and current_balance is not None:
        balance_change = initial_balance - current_balance

    goal_note = ""
    if total_played >= target_amount:
        goal_note = "\nTarget amount reached or exceeded: consider choosing 'stop' unless there is a good reason to continue."

    return f"""You are playing a slot machine in DEMO mode on an Android phone.
You can see the screen via OCR text extraction.

GOAL: Set the bet to minimum (0.10 EUR if possible), play until total played reaches {target_amount} EUR, then stop.

Step: {step}
Current balance: {current_balance if current_balance is not None else "unknown"} EUR
Total played so far: {total_played:.2f} EUR
Target: {target_amount} EUR
Balance change since start: {balance_change if balance_change is not None else "unknown"} EUR{goal_note}

Screen elements (text at [x,y] coordinates):
{format_ocr_elements(raw_texts)}

Previous actions: {action_history[-5:]}
"""


def build_questions(candidate_texts: list) -> dict:
    return {
        "action": choice_question(
            "What should I do next? Choose 'tap' to touch an element on screen, or 'stop' to finish playing.",
            {
                "tap": "Touch/tap an element on the screen",
                "stop": "Stop playing - goal reached or cannot continue",
            },
        ),
        "target": choice_question(
            "If action is tap, which element should I tap? Answer with the exact text of the element.",
            {text: text for text in candidate_texts},
        ),
    }


def resolve_tap_coords(target_text: Optional[str], raw_texts: list) -> Optional[tuple]:
    """Risolve il testo scelto da GPT nelle coordinate dell'elemento OCR
    corrispondente. Match esatto, poi case-insensitive, poi fuzzy match come
    ultimo fallback (stesso pattern usato in GPTDecisionClient._resolve_action
    per le azioni)."""
    if not target_text:
        return None

    for text, (x, y), _conf in raw_texts:
        if text == target_text:
            return (x, y)

    target_lower = target_text.lower()
    for text, (x, y), _conf in raw_texts:
        if text.lower() == target_lower:
            return (x, y)

    texts = [text for text, _coords, _conf in raw_texts]
    match = difflib.get_close_matches(target_lower, [t.lower() for t in texts], n=1, cutoff=0.6)
    if match:
        for text, (x, y), _conf in raw_texts:
            if text.lower() == match[0]:
                return (x, y)

    return None


def read_full_ocr_state(screen_reader: ScreenReader, ocr_reader: SlotOCRReader, device_id: str) -> Optional[dict]:
    ss = screen_reader._screenshot(device_id)
    if not ss.success:
        print(f"ERRORE: screenshot fallito: {ss.error} ({ss.error_code})")
        return None
    return ocr_reader.read_slot_state(ss.data)


def run_autonomous_session(
    device_id: str,
    target_amount: float,
    max_steps: int,
    engine_url: Optional[str],
    provider: str = "gpt4o",
) -> bool:
    """Gioca la slot gia' aperta sul device lasciando che il Decision Engine
    scelga autonomamente cosa tappare ad ogni passo, guardando tutti i testi
    OCR a schermo. Ritorna True se la sessione si e' conclusa in modo pulito
    (stop deciso dall'engine o saldo insufficiente), False in caso di errore."""
    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    ocr_reader = SlotOCRReader()
    engine = build_engine(provider, engine_url)

    print(f"=== TEST GIOCO SLOT AUTONOMO (provider: {provider}) ===")
    print(f"Device: {device_id}")
    print(f"Target giocato: {target_amount} EUR, max {max_steps} step")
    print("Assicurati che la slot sia aperta in modalita' DEMO sul telefono.")
    input("Premi INVIO per iniziare...")

    ocr_state = read_full_ocr_state(screen_reader, ocr_reader, device_id)
    if ocr_state is None:
        return False

    initial_balance = ocr_state.get("saldo")
    current_balance = initial_balance
    total_played = 0.0
    action_history: list[dict] = []
    steps_since_ocr = 0
    force_refresh = False

    print(f"Saldo iniziale: {initial_balance}")

    for step in range(1, max_steps + 1):
        if force_refresh or steps_since_ocr >= OCR_REFRESH_INTERVAL:
            ocr_state = read_full_ocr_state(screen_reader, ocr_reader, device_id)
            if ocr_state is None:
                return False
            steps_since_ocr = 0
            force_refresh = False
            if ocr_state.get("saldo") is not None:
                current_balance = ocr_state["saldo"]
                if initial_balance is not None:
                    total_played = initial_balance - current_balance
        else:
            steps_since_ocr += 1

        print(f"\n=== Step {step}/{max_steps} ===")
        print(f"Saldo: {current_balance} | Giocato finora: {total_played:.2f} EUR")

        if current_balance is not None and current_balance < MIN_BET:
            print(f"Saldo insufficiente (< {MIN_BET}). Stop automatico.")
            break

        raw_texts = ocr_state.get("raw_texts", [])
        candidate_texts = [text for text, _coords, _conf in raw_texts]

        state_text = build_state_text(
            raw_texts, initial_balance, current_balance, total_played, target_amount, step, action_history,
        )
        questions = build_questions(candidate_texts)

        print(f"Chiedo a {provider}...")
        result = engine.ask(state_text, questions)
        if not result.success:
            print(f"ERRORE: Decision Engine: {result.error} ({result.error_code})")
            return False

        answers = result.data.get("answers", {})
        action = answers.get("action", {}).get("choice", "stop")
        target_text = answers.get("target", {}).get("choice")
        confidence = answers.get("action", {}).get("confidence", 0.0)

        print(f"Decisione: {action} target={target_text!r} (confidence: {confidence:.2f})")

        if action == "stop":
            print("Il Decision Engine dice STOP. Fine del test.")
            break

        coords = resolve_tap_coords(target_text, raw_texts)
        if coords is None:
            print(f"Target {target_text!r} non trovato tra gli elementi OCR. Rileggo lo schermo al prossimo step.")
            force_refresh = True
            continue

        is_spin_tap = ocr_state.get("spin_button_coords") == coords
        force_refresh = not is_spin_tap

        print(f"Tapping {target_text!r} at {coords}...")
        tap_result = touch_injector.tap_coordinates(device_id, coords[0], coords[1])
        if not tap_result.success:
            print(f"ERRORE: tap fallito: {tap_result.error} ({tap_result.error_code})")

        action_history.append({"step": step, "action": action, "target": target_text})
        action_history = action_history[-5:]
        time.sleep(1.5)

    print("\n=== RISULTATO ===")
    print(f"Saldo iniziale: {initial_balance}")
    print(f"Saldo finale: {current_balance}")
    print(f"Totale giocato: {total_played:.2f} EUR")
    print("\nStorico:")
    for a in action_history:
        print(f"  Step {a['step']}: {a['action']} -> {a['target']}")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida il gioco autonomo della slot: GPT-4o-mini vede tutti i testi OCR e decide da solo cosa tappare."
    )
    parser.add_argument("--device-id", required=True, help="ID del dispositivo ADB (es. emulator-5554 o seriale reale).")
    parser.add_argument("--target-amount", type=float, default=DEFAULT_TARGET_AMOUNT, help="Importo obiettivo giocato in EUR (default: 30.0).")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="Numero massimo di step di sicurezza (default: 50).")
    parser.add_argument(
        "--engine-url", default=None,
        help="URL del Decision Engine Open-Jev (default: env var DECISION_ENGINE_URL o http://localhost:8001). "
        "Ignorato con --provider gpt4o.",
    )
    parser.add_argument(
        "--provider", choices=["openjev", "gpt4o"], default="gpt4o",
        help="Decision Engine da usare: 'gpt4o' (default, richiede OPENAI_API_KEY) o "
        "'openjev' (richiede il server locale).",
    )
    args = parser.parse_args()

    ok = run_autonomous_session(
        device_id=args.device_id,
        target_amount=args.target_amount,
        max_steps=args.max_steps,
        engine_url=args.engine_url,
        provider=args.provider,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
