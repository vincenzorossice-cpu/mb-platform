"""
Script di validazione manuale: GPT-4o-mini Vision gioca la slot in piena
autonomia guardando lo screenshot.

Rispetto alla versione precedente (basata su testi OCR), qui il Decision
Engine VEDE lo screenshot del telefono (Vision API di GPT-4o-mini) invece di
un dump di testi con coordinate: capisce il layout come un umano (bottoni,
menu modali aperti, dove si trova il tasto SPIN) e sceglie autonomamente dove
tappare restituendo coordinate [x,y]. L'OCR (EasyOCR, via SlotOCRReader) resta
in uso solo per leggere saldo/posta (valori numerici: l'OCR e' piu' preciso e
molto piu' economico della vision per questo).

Questo script richiede GPT-4o-mini (Vision e' una capacita' specifica di
GPTDecisionClient, non fa parte del contratto generico DecisionEngineClientInterface
implementato anche da Open-Jev): non supporta --provider openjev.

L'utente apre MANUALMENTE la slot in modalita' demo (for fun) sul telefono
prima di lanciare lo script: la slot e' Canvas/WebGL, quindi uiautomator non
legge nulla e serve OCR/screenshot.

Prerequisiti:
- Slot gia' aperta in modalita' DEMO (for fun, soldi virtuali) sul telefono.
- easyocr e Pillow installati (gia' in orchestrator/requirements.txt).
- env var OPENAI_API_KEY impostata.

Uso:
    python -m orchestrator.tests.validation.test_slot_autonomous \
        --device-id ZE223HNBKJ --target-amount 30
"""

import argparse
import logging
import os
import shutil
import sys
import time
from typing import Optional

from orchestrator.flows.casino_flow import SlotOCRReader
from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient
from orchestrator.modules.decision_engine.interface import choice_question
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.touch_injector.injector import TouchInjector

logger = logging.getLogger(__name__)

OCR_REFRESH_INTERVAL = 3  # step tra una lettura OCR (saldo/posta) e l'altra
MIN_BET = 0.10  # posta minima assunta; usata per la fase setup/playing e per lo stop su saldo basso
DEFAULT_MAX_STEPS = 50
DEFAULT_TARGET_AMOUNT = 30.0
VISION_RESIZE_SCALE = 1.0  # dimezza lo screenshot prima di inviarlo alla Vision API
SPIN_PROXIMITY_PX = 100  # tap entro questa distanza dal tasto spin noto -> non serve rileggere l'OCR
SCREENSHOTS_DIR = "screenshots"


def resize_screenshot_for_vision(screenshot_path: str, scale: float = VISION_RESIZE_SCALE) -> tuple:
    """Ridimensiona lo screenshot per ridurre token/costo della Vision API.

    Ritorna (percorso_immagine_ridotta, larghezza_ridotta, altezza_ridotta,
    fattore_scala_x, fattore_scala_y). I fattori di scala vanno moltiplicati
    alle coordinate restituite da GPT (nello spazio dell'immagine ridotta) per
    riportarle allo spazio dello screenshot originale (schermo reale)."""
    from PIL import Image

    with Image.open(screenshot_path) as img:
        orig_w, orig_h = img.size
        resized_w, resized_h = max(1, int(orig_w * scale)), max(1, int(orig_h * scale))
        resized = img.resize((resized_w, resized_h))
        resized_path = screenshot_path.rsplit(".", 1)[0] + "_small.png"
        resized.save(resized_path)

    return resized_path, resized_w, resized_h, orig_w / resized_w, orig_h / resized_h


def descale_coords(x: float, y: float, scale_x: float, scale_y: float) -> tuple:
    return (int(round(x * scale_x)), int(round(y * scale_y)))


def determine_phase(current_bet: Optional[float], min_bet: float = MIN_BET) -> str:
    """'setup' finche' la posta letta via OCR e' sopra il minimo, 'playing' quando
    e' al minimo (o sconosciuta: non possiamo chiedere di abbassarla se non
    sappiamo qual e')."""
    if current_bet is not None and current_bet > min_bet:
        return "setup"
    return "playing"


def build_vision_state_text(
    initial_balance: Optional[float],
    current_balance: Optional[float],
    current_bet: Optional[float],
    total_played: float,
    target_amount: float,
    step: int,
    action_history: list,
    min_bet: float = MIN_BET,
) -> str:
    phase = determine_phase(current_bet, min_bet)

    phase_banner = ""
    if phase == "setup":
        phase_banner = (
            f"\nPHASE: BET SETUP - Lower the bet before spinning! Current bet {current_bet:.2f} EUR "
            f"is above the minimum ({min_bet:.2f} EUR)."
        )

    net_profit_loss = "unknown"
    if initial_balance is not None and current_balance is not None:
        net_profit_loss = f"{current_balance - initial_balance:.2f}"

    exceed_warning = ""
    if current_bet is not None and (total_played + current_bet) > target_amount:
        projected = total_played + current_bet
        exceed_warning = (
            f"\nWARNING: Playing another spin will exceed the target ({total_played:.2f} EUR played + "
            f"{current_bet:.2f} EUR bet = {projected:.2f} EUR, target is {target_amount:.2f} EUR). "
            "Consider stopping."
        )

    goal_note = ""
    if total_played >= target_amount:
        goal_note = (
            "\nTarget amount reached or exceeded: consider choosing 'stop' unless there is a "
            "good reason to continue."
        )

    return f"""You are playing a slot machine in DEMO mode on an Android phone.

GOAL: Set the bet to minimum (0.10 EUR if possible), play until total played reaches {target_amount} EUR, then stop.

Step: {step}
Step phase: {phase}{phase_banner}
Current balance (read via OCR): {current_balance if current_balance is not None else "unknown"} EUR
Current bet: {current_bet if current_bet is not None else "unknown"} EUR (MINIMUM BET: {min_bet:.2f} EUR or lowest available)
WARNING: If current bet is above minimum, you MUST lower it first. Do not spin with a high bet.
Total played so far: {total_played:.2f} EUR (this is the SUM of all bets, NOT the balance difference)
Net profit/loss: {net_profit_loss} EUR
Target: {target_amount} EUR{goal_note}{exceed_warning}

Previous actions: {action_history[-5:]}

Look at the attached screenshot and decide what to do next.
"""


def build_questions() -> dict:
    return {
        "action": choice_question(
            "What should I do next? Choose 'tap' to touch a point on the screen, or 'stop' to finish playing.",
            {
                "tap": "Touch a point on the screen",
                "stop": "Stop playing - goal reached or cannot continue",
            },
        ),
    }


def read_ocr_state(screen_reader: ScreenReader, ocr_reader: SlotOCRReader, device_id: str) -> Optional[dict]:
    ss = screen_reader._screenshot(device_id)
    if not ss.success:
        print(f"ERRORE: screenshot fallito: {ss.error} ({ss.error_code})")
        return None
    return ocr_reader.read_slot_state(ss.data)


def run_autonomous_session(
    device_id: str, target_amount: float, max_steps: int, save_screenshots: bool = False,
) -> bool:
    """Gioca la slot gia' aperta sul device lasciando che GPT-4o-mini Vision
    scelga autonomamente dove tappare guardando lo screenshot ad ogni passo.
    L'OCR resta in uso solo per saldo/posta (letti su una cadenza piu' rada,
    riusata finche' l'ultimo tap e' stato vicino al tasto spin noto).

    total_played e' la SOMMA delle poste piazzate (non saldo_iniziale -
    saldo_attuale, che sarebbe il net loss e diminuirebbe con le vincite): si
    incrementa di current_bet solo quando un tap va a buon fine durante la
    fase "playing" (posta gia' al minimo), assumendo che l'unico tap sensato
    in quella fase sia uno spin.

    Ritorna True se la sessione si e' conclusa in modo pulito (stop deciso dal
    Decision Engine, target raggiunto o saldo insufficiente), False in caso di
    errore."""
    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    ocr_reader = SlotOCRReader()
    engine = GPTDecisionClient()

    if save_screenshots:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

    print("=== TEST GIOCO SLOT AUTONOMO (GPT-4o-mini Vision) ===")
    print(f"Device: {device_id}")
    print(f"Target giocato: {target_amount} EUR, max {max_steps} step")
    print("Assicurati che la slot sia aperta in modalita' DEMO sul telefono.")
    input("Premi INVIO per iniziare...")

    ocr_state = read_ocr_state(screen_reader, ocr_reader, device_id)
    if ocr_state is None:
        return False

    initial_balance = ocr_state.get("saldo")
    current_balance = initial_balance
    current_bet = ocr_state.get("posta")
    known_spin_coords = ocr_state.get("spin_button_coords")
    total_played = 0.0
    action_history: list[dict] = []
    steps_since_ocr = 0
    force_ocr_refresh = False

    print(f"Saldo iniziale: {initial_balance}")

    for step in range(1, max_steps + 1):
        if force_ocr_refresh or steps_since_ocr >= OCR_REFRESH_INTERVAL:
            ocr_state = read_ocr_state(screen_reader, ocr_reader, device_id)
            if ocr_state is None:
                return False
            steps_since_ocr = 0
            force_ocr_refresh = False
            if ocr_state.get("saldo") is not None:
                current_balance = ocr_state["saldo"]
            if ocr_state.get("posta") is not None:
                current_bet = ocr_state["posta"]
            if ocr_state.get("spin_button_coords") is not None:
                known_spin_coords = ocr_state["spin_button_coords"]
        else:
            steps_since_ocr += 1

        phase = determine_phase(current_bet)
        print(f"\n=== Step {step}/{max_steps} (fase: {phase}) ===")
        print(f"Saldo: {current_balance} | Posta: {current_bet} | Giocato finora: {total_played:.2f} EUR")

        if current_balance is not None and current_balance < MIN_BET:
            print(f"Saldo insufficiente (< {MIN_BET}). Stop automatico.")
            break

        if total_played >= target_amount:
            print(f"Target di {target_amount} EUR giocati raggiunto. Stop automatico.")
            break

        ss = screen_reader._screenshot(device_id)
        if not ss.success:
            print(f"ERRORE: screenshot fallito: {ss.error} ({ss.error_code})")
            return False
        screenshot_path = ss.data

        resized_path, resized_w, resized_h, scale_x, scale_y = resize_screenshot_for_vision(screenshot_path)
        try:
            if save_screenshots:
                shutil.copy(resized_path, os.path.join(SCREENSHOTS_DIR, f"step_{step:04d}.png"))

            state_text = build_vision_state_text(
                initial_balance, current_balance, current_bet, total_played, target_amount, step, action_history,
            )
            questions = build_questions()

            print("Chiedo a GPT-4o-mini Vision...")
            result = engine.ask_with_image(resized_path, state_text, questions, resized_w, resized_h)
        finally:
            try:
                os.remove(resized_path)
            except OSError:
                pass

        if not result.success:
            print(f"ERRORE: Decision Engine: {result.error} ({result.error_code})")
            return False

        answers = result.data.get("answers", {})
        action = answers.get("action", {}).get("choice", "stop")
        target = answers.get("target", {}).get("choice")
        confidence = answers.get("action", {}).get("confidence", 0.0)

        print(f"Decisione: {action} target(risoluzione immagine)={target} (confidence: {confidence:.2f})")

        if action == "stop":
            print("Il Decision Engine dice STOP. Fine del test.")
            break

        if not target:
            print("GPT non ha restituito coordinate valide. Rileggo lo schermo al prossimo step.")
            force_ocr_refresh = True
            continue

        real_x, real_y = descale_coords(target[0], target[1], scale_x, scale_y)

        is_spin_tap = (
            known_spin_coords is not None
            and abs(real_x - known_spin_coords[0]) <= SPIN_PROXIMITY_PX
            and abs(real_y - known_spin_coords[1]) <= SPIN_PROXIMITY_PX
        )
        force_ocr_refresh = not is_spin_tap

        print(f"Tapping at ({real_x},{real_y})...")
        tap_result = touch_injector.tap_coordinates(device_id, real_x, real_y)
        if not tap_result.success:
            print(f"ERRORE: tap fallito: {tap_result.error} ({tap_result.error_code})")
        elif phase == "playing" and current_bet is not None:
            # Fase "playing" = posta gia' al minimo: l'unico tap sensato e' uno
            # spin, quindi contiamo la posta come giocata.
            total_played += current_bet

        action_history.append({"step": step, "action": action, "target": (real_x, real_y), "phase": phase})
        action_history = action_history[-5:]
        time.sleep(1.5)

    print("\n=== RISULTATO ===")
    print(f"Saldo iniziale: {initial_balance}")
    print(f"Saldo finale: {current_balance}")
    print(f"Totale giocato: {total_played:.2f} EUR")
    if initial_balance is not None and current_balance is not None:
        print(f"Net profit/loss: {current_balance - initial_balance:+.2f} EUR")
    print("\nStorico:")
    for a in action_history:
        print(f"  Step {a['step']}: {a['action']} -> {a['target']} ({a['phase']})")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida il gioco autonomo della slot: GPT-4o-mini Vision vede lo screenshot e decide da solo cosa tappare."
    )
    parser.add_argument("--device-id", required=True, help="ID del dispositivo ADB (es. emulator-5554 o seriale reale).")
    parser.add_argument("--target-amount", type=float, default=DEFAULT_TARGET_AMOUNT, help="Importo obiettivo giocato in EUR (default: 30.0).")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="Numero massimo di step di sicurezza (default: 50).")
    parser.add_argument(
        "--save-screenshots", action="store_true",
        help=f"Salva ogni screenshot inviato a GPT-4o-mini nella cartella {SCREENSHOTS_DIR}/ per debug.",
    )
    args = parser.parse_args()

    ok = run_autonomous_session(
        device_id=args.device_id,
        target_amount=args.target_amount,
        max_steps=args.max_steps,
        save_screenshots=args.save_screenshots,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
