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
import subprocess
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
SPIN_PROXIMITY_PX = 100  # tap entro questa distanza dal tasto spin noto -> considerato un tentativo di spin
SCREENSHOTS_DIR = "screenshots"
MAX_CONSECUTIVE_SETUP_STEPS = 5  # oltre questa soglia in fase "setup" senza progressi -> HITL
REPEAT_TAP_TOLERANCE_PX = 50  # tap entro questa distanza sono considerati "lo stesso punto"
STUCK_SCREEN_THRESHOLD = 3  # step consecutivi senza cambiamenti a schermo -> nudge esplorativo
MAX_TAPPED_TARGETS = 10  # quanti tap recenti tenere in memoria per anti-loop/elementi non provati


def normalize_texts(raw_texts: list) -> set:
    """Normalizza i testi OCR (spazi/maiuscole) per un confronto robusto al
    rumore di lettura tra due screenshot dello stesso schermo statico."""
    return {" ".join(text.split()).lower() for text, _coords, _conf in raw_texts if text.strip()}


def find_untried_elements(raw_texts: list, tapped_targets: list, tolerance: float = REPEAT_TAP_TOLERANCE_PX) -> list:
    """Testi OCR le cui coordinate non sono vicine a nessun tap gia' tentato."""
    def already_tried(x, y):
        return any(abs(x - t["x"]) <= tolerance and abs(y - t["y"]) <= tolerance for t in tapped_targets)

    return [text for text, (x, y), _conf in raw_texts if text.strip() and not already_tried(x, y)]


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
    corrective_note: Optional[str] = None,
    menu_open: bool = False,
    new_texts: Optional[set] = None,
    disappeared_texts: Optional[set] = None,
    steps_without_screen_change: int = 0,
) -> str:
    phase = determine_phase(current_bet, min_bet)

    phase_banner = ""
    if phase == "setup":
        phase_banner = (
            f"\nPHASE: BET SETUP - Lower the bet before spinning! Current bet {current_bet:.2f} EUR "
            f"is above the minimum ({min_bet:.2f} EUR)."
        )
        if menu_open:
            phase_banner += (
                "\nA bet menu/overlay appears to be open. Select the LOWEST amount available, then "
                "look for a CLOSE/CONFIRM/OK button or tap OUTSIDE the menu area to close it. Do NOT "
                "tap the same bet amount again if you already selected it — you need to CLOSE the "
                "menu now."
            )
        else:
            phase_banner += (
                "\nTO LOWER THE BET: look for the bet amount display (usually shows something like "
                "'€10.00' or 'PUNTATA €10.00') and tap on it to open the bet selection menu. OR look "
                "for +/- buttons near the bet amount. OR look for a coins/credits icon. DO NOT tap "
                "the SPIN button. DO NOT tap random elements. Focus ONLY on finding the bet control."
            )
    else:
        phase_banner = "\nBET IS AT MINIMUM. Now find and tap the SPIN button."

    screen_diff_note = ""
    if new_texts:
        screen_diff_note = f"\nNEW elements appeared on screen: {sorted(new_texts)}. A menu or popup may have opened."
    elif disappeared_texts:
        screen_diff_note = f"\nSome elements disappeared: {sorted(disappeared_texts)}. A menu may have closed."
    elif steps_without_screen_change >= STUCK_SCREEN_THRESHOLD:
        screen_diff_note = (
            f"\nThe screen has not changed for {steps_without_screen_change} attempts. Try tapping a "
            "completely different area."
        )

    corrective_block = f"\n\nIMPORTANT: {corrective_note}" if corrective_note else ""

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

    return f"""You are playing a slot machine in DEMO mode on an Android phone.{corrective_block}

GOAL: Set the bet to minimum (0.10 EUR if possible), play until total played reaches {target_amount} EUR, then stop.

Step: {step}
Step phase: {phase}{phase_banner}{screen_diff_note}
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


def _open_screenshot(screenshot_path: str) -> None:
    """Prova ad aprire lo screenshot col visualizzatore di default del SO,
    cosi' l'operatore puo' guardarlo senza cercare il file a mano."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", screenshot_path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", screenshot_path])
        else:
            subprocess.Popen(["xdg-open", screenshot_path])
    except OSError:
        pass  # nessun visualizzatore disponibile: l'operatore apre il file manualmente


def ask_human_help(screenshot_path: Optional[str], current_bet: Optional[float], min_bet: float) -> tuple:
    """Chiede aiuto all'operatore via terminale quando il sistema non riesce
    ad abbassare la posta da solo dopo troppi tentativi. Ritorna una tupla:
    - ("stop",): l'operatore vuole fermare tutto
    - ("skip",): l'operatore vuole lasciar riprovare GPT-4o senza aiuto
    - ("coords", x, y): l'operatore ha dato coordinate da tappare
    - ("text", descrizione): suggerimento testuale da passare a GPT-4o
    """
    bet_str = f"{current_bet:.2f}" if current_bet is not None else "unknown"
    print(f"\n{'='*60}")
    print("HITL: Non riesco ad abbassare la puntata.")
    print(f"Puntata attuale: {bet_str} EUR | Minimo: {min_bet:.2f} EUR")
    print(f"Screenshot: {screenshot_path or 'non disponibile'}")
    print(f"{'='*60}")

    if screenshot_path:
        _open_screenshot(screenshot_path)

    print("\nCosa devo tappare per abbassare la puntata?")
    print("Opzioni:")
    print("  - Descrivi l'elemento (es. 'tappa sulle monete in alto')")
    print("  - Dai le coordinate (es. '540,600')")
    print("  - Scrivi 'skip' per saltare e continuare con GPT")
    print("  - Scrivi 'stop' per fermare tutto")

    user_input = input("> ").strip()

    if user_input.lower() == "stop":
        return ("stop",)
    if user_input.lower() == "skip":
        return ("skip",)

    if "," in user_input:
        parts = user_input.split(",")
        if len(parts) == 2:
            try:
                x, y = int(parts[0].strip()), int(parts[1].strip())
                return ("coords", x, y)
            except ValueError:
                pass

    return ("text", user_input)


def handle_hitl_escalation(
    screen_reader: ScreenReader,
    ocr_reader: SlotOCRReader,
    touch_injector: TouchInjector,
    device_id: str,
    current_bet: Optional[float],
    min_bet: float,
) -> tuple:
    """Gestisce l'escalation HITL: chiede ripetutamente aiuto all'operatore
    finche' non arriva uno stop, uno skip, un suggerimento testuale, o
    coordinate che abbassano davvero la posta. Ritorna:
    - ("stop", None)
    - ("skip", None)
    - ("resolved", ocr_state) — posta abbassata con successo, stato fresco
    - ("hint", descrizione) — suggerimento testuale da passare a GPT-4o
    """
    while True:
        ss = screen_reader._screenshot(device_id)
        screenshot_path = ss.data if ss.success else None

        outcome = ask_human_help(screenshot_path, current_bet, min_bet)

        if outcome[0] == "stop":
            return ("stop", None)
        if outcome[0] == "skip":
            return ("skip", None)

        if outcome[0] == "coords":
            _, x, y = outcome
            print(f"Eseguo il tap suggerito dall'operatore: ({x},{y})")
            touch_injector.tap_coordinates(device_id, x, y)
            time.sleep(3)
            ocr_state = read_ocr_state(screen_reader, ocr_reader, device_id)
            if ocr_state is None:
                continue
            new_bet = ocr_state.get("posta")
            if new_bet is not None and current_bet is not None and new_bet < current_bet:
                print(f"Posta abbassata con successo: {current_bet:.2f} -> {new_bet:.2f} EUR.")
                return ("resolved", ocr_state)
            print("La posta non sembra essere cambiata. Richiedo di nuovo aiuto.")
            if new_bet is not None:
                current_bet = new_bet
            continue

        # outcome[0] == "text"
        return ("hint", outcome[1])


def run_autonomous_session(
    device_id: str, target_amount: float, max_steps: int, save_screenshots: bool = False,
) -> bool:
    """Gioca la slot gia' aperta sul device lasciando che GPT-4o-mini Vision
    scelga autonomamente dove tappare guardando lo screenshot ad ogni passo.
    L'OCR resta in uso solo per saldo/posta, riletto dopo ogni tap reale (per
    rilevare in modo affidabile se il saldo e' sceso, cioe' se uno spin e'
    avvenuto) e comunque almeno ogni OCR_REFRESH_INTERVAL step.

    Se la posta e' sopra il minimo (fase "setup"), un tap su/vicino al tasto
    spin noto viene BLOCCATO (mai eseguito) invece di essere inoltrato al
    dispositivo: GPT riceve una nota correttiva al turno successivo. Questo
    funziona su qualunque slot perche' si basa solo su known_spin_coords
    (rilevato via OCR per quella specifica slot) e sulla posta letta via OCR,
    non su coordinate hardcoded. Se dopo MAX_CONSECUTIVE_SETUP_STEPS step
    consecutivi la fase resta "setup", la sessione si ferma e richiede
    intervento umano (HITL).

    total_played e' la SOMMA delle poste piazzate, dedotta osservando il
    saldo: ogni volta che una lettura OCR fresca mostra un saldo piu' basso di
    quello precedente (segno che uno spin e' avvenuto, indipendentemente da
    quale fase pensavamo di essere), si aggiunge la posta corrente (letta via
    OCR) a total_played -- mai saldo_iniziale - saldo_attuale, che sarebbe il
    net loss e diminuirebbe con le vincite.

    Ritorna True se la sessione si e' conclusa in modo pulito (stop deciso dal
    Decision Engine, target raggiunto o saldo insufficiente), False in caso di
    errore o escalation HITL."""
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
    corrective_note: Optional[str] = None
    consecutive_setup_steps = 0
    tapped_targets: list[dict] = []
    last_tap_coords: Optional[tuple] = None
    menu_open = False
    steps_without_screen_change = 0
    previous_raw_texts: Optional[set] = normalize_texts(ocr_state.get("raw_texts", []))
    new_texts: set = set()
    disappeared_texts: set = set()

    print(f"Saldo iniziale: {initial_balance}")

    for step in range(1, max_steps + 1):
        if force_ocr_refresh or steps_since_ocr >= OCR_REFRESH_INTERVAL:
            previous_balance = current_balance
            ocr_state = read_ocr_state(screen_reader, ocr_reader, device_id)
            if ocr_state is None:
                return False
            steps_since_ocr = 0
            force_ocr_refresh = False
            if ocr_state.get("saldo") is not None:
                current_balance = ocr_state["saldo"]
                if previous_balance is not None and current_balance < previous_balance:
                    spin_bet = current_bet if current_bet is not None else (previous_balance - current_balance)
                    total_played += spin_bet
                    print(
                        f"Spin rilevato! Saldo: {previous_balance} -> {current_balance}. "
                        f"Posta: {spin_bet:.2f}. Total played: {total_played:.2f}"
                    )
                    if last_tap_coords is not None:
                        # Anche se l'OCR non ha mai etichettato il tasto SPIN
                        # (nessun testo "SPIN"/"XITER"/"GIRA" trovato), abbiamo
                        # appena OSSERVATO che tappare qui causa uno spin:
                        # impariamo queste coordinate cosi' il safety-net qui
                        # sotto puo' bloccare i tentativi successivi durante il
                        # setup, invece di rilevarli solo a posteriori.
                        known_spin_coords = last_tap_coords
                        print(f"Coordinate del tasto SPIN apprese da questo spin: {known_spin_coords}")
            if ocr_state.get("posta") is not None:
                current_bet = ocr_state["posta"]
            if ocr_state.get("spin_button_coords") is not None:
                known_spin_coords = ocr_state["spin_button_coords"]

            # Confronta i testi OCR con la lettura precedente per capire se lo
            # schermo e' cambiato (menu aperto/chiuso) dopo l'ultimo tap reale.
            # Un cambio di saldo conta anch'esso come "schermo cambiato" (uno
            # spin normale spesso non altera altre etichette oltre al numero
            # del saldo, che potrebbe comunque comparire identico all'OCR se
            # il valore e' arrotondato uguale): senza questo, uno spin
            # ripetuto legittimo sullo stesso tasto verrebbe scambiato per un
            # loop e bloccato dall'anti-loop qui sotto.
            current_raw_texts = normalize_texts(ocr_state.get("raw_texts", []))
            new_texts = current_raw_texts - previous_raw_texts if previous_raw_texts is not None else set()
            disappeared_texts = previous_raw_texts - current_raw_texts if previous_raw_texts is not None else set()
            balance_changed = (
                previous_balance is not None and current_balance is not None and current_balance != previous_balance
            )
            screen_changed = bool(new_texts or disappeared_texts) or balance_changed
            if new_texts:
                menu_open = True
            elif disappeared_texts:
                menu_open = False
            steps_without_screen_change = 0 if screen_changed else steps_without_screen_change + 1
            previous_raw_texts = current_raw_texts

            if last_tap_coords is not None:
                tapped_targets.append({"x": last_tap_coords[0], "y": last_tap_coords[1], "screen_changed": screen_changed})
                tapped_targets = tapped_targets[-MAX_TAPPED_TARGETS:]
                last_tap_coords = None
        else:
            steps_since_ocr += 1

        phase = determine_phase(current_bet)
        print(f"\n=== Step {step}/{max_steps} (fase: {phase}) ===")
        print(f"Saldo: {current_balance} | Posta: {current_bet} | Giocato finora: {total_played:.2f} EUR")

        if phase == "setup":
            consecutive_setup_steps += 1
            if consecutive_setup_steps > MAX_CONSECUTIVE_SETUP_STEPS:
                print(
                    f"HITL: il sistema non e' riuscito ad abbassare la posta al minimo dopo "
                    f"{MAX_CONSECUTIVE_SETUP_STEPS} step. Chiedo aiuto all'operatore."
                )
                hitl_action, hitl_data = handle_hitl_escalation(
                    screen_reader, ocr_reader, touch_injector, device_id, current_bet, MIN_BET,
                )
                if hitl_action == "stop":
                    print("Operatore ha richiesto lo stop. Fine sessione.")
                    return False

                consecutive_setup_steps = 0
                if hitl_action == "resolved":
                    ocr_state = hitl_data
                    if ocr_state.get("saldo") is not None:
                        current_balance = ocr_state["saldo"]
                    if ocr_state.get("posta") is not None:
                        current_bet = ocr_state["posta"]
                    if ocr_state.get("spin_button_coords") is not None:
                        known_spin_coords = ocr_state["spin_button_coords"]
                    previous_raw_texts = normalize_texts(ocr_state.get("raw_texts", []))
                    steps_since_ocr = 0
                    force_ocr_refresh = False
                elif hitl_action == "hint":
                    corrective_note = f"The human operator suggests: {hitl_data}"
                # hitl_action == "skip": nessun'altra azione, si riprova con GPT
                phase = determine_phase(current_bet)
        else:
            consecutive_setup_steps = 0

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
                corrective_note=corrective_note, menu_open=menu_open, new_texts=new_texts,
                disappeared_texts=disappeared_texts, steps_without_screen_change=steps_without_screen_change,
            )
            corrective_note = None
            new_texts, disappeared_texts = set(), set()
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

        if phase == "setup" and is_spin_tap:
            print(
                f"BLOCCATO: GPT ha tentato di tappare SPIN con la posta ancora a {current_bet:.2f} EUR "
                "(fase setup). Tap NON eseguito."
            )
            corrective_note = (
                f"BLOCKED: You tried to tap the SPIN button but the bet is still {current_bet:.2f}€ "
                f"(minimum is {MIN_BET:.2f}€). You MUST lower the bet FIRST. Look for the bet amount "
                "display, a coins icon, or +/- buttons. DO NOT tap the SPIN button."
            )
            action_history.append({"step": step, "action": "blocked_spin_attempt", "target": (real_x, real_y), "phase": phase})
            action_history = action_history[-5:]
            time.sleep(1.0)
            continue

        recent_same_spot = [
            t for t in tapped_targets[-2:]
            if abs(t["x"] - real_x) <= REPEAT_TAP_TOLERANCE_PX
            and abs(t["y"] - real_y) <= REPEAT_TAP_TOLERANCE_PX
            and not t["screen_changed"]
        ]
        if len(recent_same_spot) >= 2:
            print(f"BLOCCATO: tap ripetuto su ({real_x},{real_y}) senza effetto. Suggerisco elementi non provati.")
            untried = find_untried_elements(ocr_state.get("raw_texts", []), tapped_targets)
            bet_str = f"{current_bet:.2f}€" if current_bet is not None else "unknown"
            corrective_note = (
                "You tapped this area twice with no effect. "
                f"The bet is still {bet_str}. Look at the screenshot AGAIN. Find a DIFFERENT element to "
                "tap. Try: coins icon, settings gear, +/- buttons, or the bet amount text itself."
            )
            if untried:
                corrective_note += f" Elements you have NOT tried yet: {untried}."
            action_history.append({"step": step, "action": "blocked_repeat_tap", "target": (real_x, real_y), "phase": phase})
            action_history = action_history[-5:]
            time.sleep(1.0)
            continue

        print(f"Tapping at ({real_x},{real_y})...")
        tap_result = touch_injector.tap_coordinates(device_id, real_x, real_y)
        if not tap_result.success:
            print(f"ERRORE: tap fallito: {tap_result.error} ({tap_result.error_code})")
        else:
            # Ogni tap reale puo' aver cambiato lo stato (spin o aggiustamento
            # posta): rileggiamo l'OCR al prossimo step per aggiornare
            # saldo/posta e rilevare eventuali spin (vedi total_played sopra),
            # e per valutare se ha cambiato lo schermo (vedi tapped_targets sopra).
            force_ocr_refresh = True
            last_tap_coords = (real_x, real_y)

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
