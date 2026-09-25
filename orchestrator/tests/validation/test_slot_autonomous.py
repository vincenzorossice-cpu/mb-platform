"""
Script di validazione manuale: GPT-4o-mini Vision gioca la slot in piena
autonomia guardando lo screenshot.

Il Decision Engine VEDE lo screenshot del telefono (Vision API di GPT-4o-mini)
per capire il layout e lo stato dello schermo, ma NON restituisce mai
coordinate [x,y] dirette: secondo slot_anatomy.md, gpt-4o-mini non e'
affidabile nel "grounding spaziale" (indicare pixel precisi). Sceglie invece
un elemento candidato individuato dall'OCR (target_text, risolto con
precisione dalle coordinate misurate dall'OCR stesso) oppure, per icone senza
etichetta, una regione grossolana su una griglia 3x3 (target_region). GPT
classifica anche lo stato dello schermo (screen_state: idle/spinning/modal/
ecc., vedi slot_anatomy.md sez. 7): e' il segnale principale per sapere se un
menu e' aperto e se i numeri letti in quel momento sono attendibili — molto
piu' robusto di confrontare due letture OCR, che in produzione sono leggermente
diverse ad ogni lettura anche a schermo fermo (rumore OCR).

L'OCR (EasyOCR, via SlotOCRReader) resta in uso per leggere saldo/posta/testi
grezzi (candidati per il targeting), riletto dopo ogni tap reale e comunque
almeno ogni OCR_REFRESH_INTERVAL step.

Include: un safety-net anti-spin-prematuro (blocca i tentativi di spin mentre
la posta non e' al minimo, imparando le coordinate spin anche quando l'OCR non
le etichetta), un anti-loop consapevole se si e' dentro un menu o no, una
escalation HITL da terminale (l'operatore non vede mai lo screenshot: quello
lo vede solo GPT via Vision API) con suggerimenti cumulativi (non piu'
usa-e-getta), e una memoria persistente per slot tra un'esecuzione e l'altra
(--slot-name): le coordinate spin imparate e i suggerimenti confermati utili
si salvano su file e si ricaricano automaticamente la prossima volta che si
gioca la STESSA slot.

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
        --device-id ZE223HNBKJ --target-amount 30 --slot-name "Pirots 5"
"""

import argparse
import difflib
import json
import logging
import os
import re
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
MIN_BET = 0.10  # SOLO soglia di sicurezza "saldo troppo basso, fermati": la puntata
# minima REALE di ogni slot e' scoperta a runtime (known_min_bet), mai assunta fissa qui.
DEFAULT_MAX_STEPS = 50
DEFAULT_TARGET_AMOUNT = 30.0
VISION_RESIZE_SCALE = 1.0  # dimezza lo screenshot prima di inviarlo alla Vision API
SPIN_PROXIMITY_PX = 100  # tap entro questa distanza dal tasto spin noto -> considerato un tentativo di spin
SCREENSHOTS_DIR = "screenshots"
SLOT_MEMORY_DIR = "slot_memory"
MAX_CONSECUTIVE_SETUP_STEPS = 5  # oltre questa soglia in fase "setup" senza progressi -> HITL
REPEAT_TAP_TOLERANCE_PX = 50  # tap entro questa distanza sono considerati "lo stesso punto"
STUCK_SCREEN_THRESHOLD = 3  # step consecutivi senza cambiamenti a schermo -> nudge esplorativo
MAX_TAPPED_TARGETS = 10  # quanti tap recenti tenere in memoria per anti-loop/elementi non provati
MAX_REPEAT_ATTEMPTS_OUTSIDE_MENU = 2  # fuori da un menu, un tap fermo e' un segnale affidabile di errore
MAX_REPEAT_ATTEMPTS_INSIDE_MENU = 4  # dentro un menu, selezionare una cella diversa non cambia l'OCR:
# serve piu' pazienza prima di considerare un tap davvero "senza effetto" (vedi Causa B nel piano)
NEARBY_OFFSETS_PX = (40, 80)  # offset (px) usati per suggerire coordinate vicine a un tap bloccato
HINT_TTL_STEPS = 5  # per quanti step un suggerimento resta attivo nello state text
MIN_BET_DISCOVERY_PATIENCE_STEPS = 3  # letture consecutive senza un nuovo minimo -> il piu' basso
# visto finora diventa il minimo scoperto per questa slot (nessun numero hardcoded)
_TRANSIENT_SCREEN_STATES = {"loading", "spinning", "win_presentation", "feature"}  # animazioni/minigiochi:
# un fotogramma qui non e' affidabile per decidere alcun tap (vedi slot_anatomy.md sez. 7)
TRANSIENT_STATE_WAIT_SECONDS = 3.0  # attesa extra prima di richiedere di nuovo un parere a GPT
_RELATIVE_OFFSET_PX = 100  # scostamento generico per descrizioni HITL relative ("sopra X", "a destra di X")
_HITL_SCREEN_CHANGED_DIRECTIVE = (
    "The screen changed after the operator's tap. Look at the CURRENT screenshot: if a bet menu is "
    "open, select the LOWEST amount available; if you are back on the main game screen with the bet "
    "at minimum, look for the SPIN button."
)

# Parole chiave generiche (italiano) per riconoscere l'INTENTO dell'operatore
# in una descrizione HITL testuale, senza alcun riferimento a una slot specifica.
_SPIN_INTENT_KEYWORDS = ("spin", "gira", "girare", "gioca", "freccia circolare")
_MIN_BET_SUBJECT_PHRASES = ("puntata minima", "posta minima", "puntata al minimo", "posta al minimo")
_MIN_BET_CONFIRMATION_MARKERS = ("abbiamo", "gia", "già", "corretta", "raggiunta", "ok", "va bene")


def _looks_like_min_bet_confirmation(description: str) -> bool:
    """Vero se l'operatore sta confermando che la puntata ATTUALE e' gia' il
    minimo per questa slot (es. 'abbiamo la puntata minima corretta'):
    vocabolario generico, nessun riferimento a una slot specifica."""
    text = description.lower()
    has_subject = any(p in text for p in _MIN_BET_SUBJECT_PHRASES)
    has_marker = any(m in text for m in _MIN_BET_CONFIRMATION_MARKERS)
    return has_subject and has_marker

# Frazioni (larghezza, altezza) del centro di ciascuna cella di una griglia 3x3,
# usate per tradurre una regione grossolana (target_region) in un punto reale
# da tappare quando l'elemento non ha un'etichetta testuale riconoscibile.
_REGION_FRACTIONS = {
    "top-left": (1 / 6, 1 / 6), "top-center": (1 / 2, 1 / 6), "top-right": (5 / 6, 1 / 6),
    "middle-left": (1 / 6, 1 / 2), "center": (1 / 2, 1 / 2), "middle-right": (5 / 6, 1 / 2),
    "bottom-left": (1 / 6, 5 / 6), "bottom-center": (1 / 2, 5 / 6), "bottom-right": (5 / 6, 5 / 6),
}

# Stati in cui i numeri letti via OCR (saldo/posta) sono considerati attendibili
# per il calcolo di total_played (slot_anatomy.md sez. 7): durante il
# caricamento, uno spin in corso o la presentazione di una vincita i valori
# possono essere in transizione. None = nessuna classificazione ancora
# disponibile (es. primissimo step): si sceglie di fidarsi per non bloccare
# tutto al cold-start.
_BALANCE_TRUSTED_STATES = {None, "idle", "modal"}


def region_to_point(region: Optional[str], width: int, height: int) -> Optional[tuple]:
    """Punto generico al centro di una delle 9 celle di una griglia 3x3.
    Nessuna coordinata specifica di slot: solo geometria su frazioni fisse."""
    fractions = _REGION_FRACTIONS.get(region)
    if fractions is None:
        return None
    fx, fy = fractions
    return (int(round(width * fx)), int(round(height * fy)))


def _find_text_coords(query: str, raw_texts: list) -> Optional[tuple]:
    """Cerca `query` tra i testi OCR: match esatto, poi case-insensitive, poi
    fuzzy (difflib). Ritorna le coordinate ESATTE misurate dall'OCR per il
    primo match trovato, o None se nessuno supera la soglia di somiglianza."""
    for text, (x, y), _conf in raw_texts:
        if text == query:
            return (x, y)
    query_lower = query.lower()
    for text, (x, y), _conf in raw_texts:
        if text.lower() == query_lower:
            return (x, y)
    texts = [text for text, _coords, _conf in raw_texts]
    match = difflib.get_close_matches(query_lower, [t.lower() for t in texts], n=1, cutoff=0.6)
    if match:
        for text, (x, y), _conf in raw_texts:
            if text.lower() == match[0]:
                return (x, y)
    return None


def resolve_target(
    target: Optional[dict], raw_texts: list, real_width: int, real_height: int,
) -> Optional[tuple]:
    """Risolve la scelta di GPT (target_text o target_region) in coordinate
    reali da tappare. Il testo si risolve con le coordinate ESATTE misurate
    dall'OCR (match esatto, poi case-insensitive, poi fuzzy) — niente
    grounding spaziale richiesto a GPT. La regione si traduce geometricamente
    in un punto reale, senza alcuna coordinata hardcoded per slot."""
    if not target:
        return None

    target_text = target.get("text")
    if target_text:
        coords = _find_text_coords(target_text, raw_texts)
        if coords is not None:
            return coords

    target_region = target.get("region")
    if target_region:
        return region_to_point(target_region, real_width, real_height)

    return None


# Preposizioni italiane generiche (valide per qualunque slot) che indicano una
# posizione RELATIVA a un elemento OCR gia' visibile, con la direzione dello
# scostamento da applicare (dx, dy) in unita' di _RELATIVE_OFFSET_PX.
_RELATIVE_PATTERNS = (
    (re.compile(r"pi[uù]'?\s+in\s+alto\s+di\s+(.+)", re.IGNORECASE), (0, -1)),
    (re.compile(r"pi[uù]'?\s+in\s+basso\s+di\s+(.+)", re.IGNORECASE), (0, 1)),
    (re.compile(r"sopra\s+(?:a\s+)?(.+)", re.IGNORECASE), (0, -1)),
    (re.compile(r"sotto\s+(?:a\s+)?(.+)", re.IGNORECASE), (0, 1)),
    (re.compile(r"a\s+sinistra\s+di\s+(.+)", re.IGNORECASE), (-1, 0)),
    (re.compile(r"a\s+destra\s+di\s+(.+)", re.IGNORECASE), (1, 0)),
    (re.compile(r"vicino\s+a\s+(.+)", re.IGNORECASE), (0, 0)),
    (re.compile(r"accanto\s+a\s+(.+)", re.IGNORECASE), (0, 0)),
)

# Parole chiave italiane generiche per la griglia 3x3, combinate in una delle
# 9 regioni di _REGION_FRACTIONS.
_TOP_WORDS = ("alto", "sopra")
_BOTTOM_WORDS = ("basso", "sotto")
_LEFT_WORDS = ("sinistra",)
_RIGHT_WORDS = ("destra",)
_CENTER_WORDS = ("centro", "centrale")


def _keyword_region(description: str) -> Optional[str]:
    text = description.lower()
    has_top = any(w in text for w in _TOP_WORDS)
    has_bottom = any(w in text for w in _BOTTOM_WORDS)
    has_left = any(w in text for w in _LEFT_WORDS)
    has_right = any(w in text for w in _RIGHT_WORDS)
    has_center = any(w in text for w in _CENTER_WORDS)

    if has_top and has_left:
        return "top-left"
    if has_top and has_right:
        return "top-right"
    if has_bottom and has_left:
        return "bottom-left"
    if has_bottom and has_right:
        return "bottom-right"
    if has_top:
        return "top-center"
    if has_bottom:
        return "bottom-center"
    if has_left:
        return "middle-left"
    if has_right:
        return "middle-right"
    if has_center:
        return "center"
    return None


def parse_operator_description(
    description: str, raw_texts: list, width: int, height: int,
) -> Optional[tuple]:
    """Interpreta una descrizione libera dell'operatore (in italiano) in
    coordinate reali da tappare SUBITO, senza passare da GPT — vocabolario e
    geometria generici, validi per qualunque slot:

    1. Riferimento relativo a un'etichetta OCR visibile ("sopra PUNTATA", "a
       destra di PUNTATA", "vicino a X"): l'etichetta viene cercata tra i
       testi OCR (stesso fuzzy-match di resolve_target) e si applica uno
       scostamento fisso generico nella direzione indicata.
    2. Regione grossolana a griglia 3x3 ("in basso a destra", "al centro"):
       tradotta geometricamente con region_to_point, nessuna coordinata
       hardcoded per slot.

    Ritorna None se la descrizione non contiene alcun riferimento posizionale
    riconoscibile (l'operatore va interpellato di nuovo con piu' dettagli)."""
    for pattern, (dx_sign, dy_sign) in _RELATIVE_PATTERNS:
        match = pattern.search(description)
        if not match:
            continue
        label = match.group(1).strip(" .!?")
        if not label:
            continue
        coords = _find_text_coords(label, raw_texts)
        if coords is None:
            continue
        x, y = coords
        x = min(max(x + dx_sign * _RELATIVE_OFFSET_PX, 0), width)
        y = min(max(y + dy_sign * _RELATIVE_OFFSET_PX, 0), height)
        return (x, y)

    region = _keyword_region(description)
    if region is not None:
        return region_to_point(region, width, height)

    return None


def nearby_offsets(x: int, y: int, offsets: tuple = NEARBY_OFFSETS_PX) -> list:
    """Genera coordinate vicine a (x,y) nelle 4 direzioni cardinali, per
    suggerire a GPT di affinare la stima invece di abbandonare l'area: utile
    quando un tap e' "fermo" solo perche' e' dentro un menu che non produce
    segnali OCR distinguibili (vedi Causa B), non perche' l'area sia sbagliata."""
    candidates = []
    for d in offsets:
        candidates.extend([(x - d, y), (x + d, y), (x, y - d), (x, y + d)])
    return candidates


def normalize_texts(raw_texts: list) -> set:
    """Normalizza i testi OCR (spazi/maiuscole) per un confronto robusto al
    rumore di lettura tra due screenshot dello stesso schermo statico."""
    return {" ".join(text.split()).lower() for text, _coords, _conf in raw_texts if text.strip()}


def find_untried_elements(raw_texts: list, tapped_targets: list, tolerance: float = REPEAT_TAP_TOLERANCE_PX) -> list:
    """Testi OCR le cui coordinate non sono vicine a nessun tap gia' tentato."""
    def already_tried(x, y):
        return any(abs(x - t["x"]) <= tolerance and abs(y - t["y"]) <= tolerance for t in tapped_targets)

    return [text for text, (x, y), _conf in raw_texts if text.strip() and not already_tried(x, y)]


def count_consecutive_same_spot_failures(
    tapped_targets: list, x: int, y: int, tolerance: float = REPEAT_TAP_TOLERANCE_PX,
) -> int:
    """Quanti tap PIU' RECENTI consecutivi sono caduti vicino a (x,y) senza mai
    cambiare lo schermo (si ferma al primo tap diverso o riuscito, guardando
    la cronologia a ritroso)."""
    count = 0
    for t in reversed(tapped_targets):
        if abs(t["x"] - x) <= tolerance and abs(t["y"] - y) <= tolerance and not t["screen_changed"]:
            count += 1
        else:
            break
    return count


def resize_screenshot_for_vision(screenshot_path: str, scale: float = VISION_RESIZE_SCALE) -> tuple:
    """Ridimensiona lo screenshot per ridurre token/costo della Vision API.

    Ritorna (percorso_immagine_ridotta, larghezza_ridotta, altezza_ridotta,
    larghezza_originale, altezza_originale). Le dimensioni originali servono a
    risolvere target_region in coordinate reali da tappare: l'OCR lavora
    sempre sullo screenshot originale (non ridimensionato), quindi anche il
    targeting per testo e' gia' preciso in quello spazio senza bisogno di
    riscalare nulla."""
    from PIL import Image

    with Image.open(screenshot_path) as img:
        orig_w, orig_h = img.size
        resized_w, resized_h = max(1, int(orig_w * scale)), max(1, int(orig_h * scale))
        resized = img.resize((resized_w, resized_h))
        resized_path = screenshot_path.rsplit(".", 1)[0] + "_small.png"
        resized.save(resized_path)

    return resized_path, resized_w, resized_h, orig_w, orig_h


def get_image_dimensions(image_path: str) -> tuple:
    """Larghezza/altezza reali di uno screenshot, per risolvere una
    descrizione HITL o una regione grossolana senza dover ridimensionare
    nulla (uso al di fuori del percorso Vision, che quello lo fa gia' da se')."""
    from PIL import Image

    with Image.open(image_path) as img:
        return img.size


def determine_phase(current_bet: Optional[float], known_min_bet: Optional[float]) -> str:
    """'setup' se la puntata minima per QUESTA slot non e' ancora nota (si sta
    ancora esplorando), o e' nota ma la posta letta e' ancora sopra di essa;
    'playing' quando la posta e' al minimo scoperto (o sconosciuta: non
    possiamo chiedere di abbassarla se non sappiamo nemmeno qual e')."""
    if current_bet is None:
        return "playing"
    if known_min_bet is None:
        return "setup"
    if current_bet > known_min_bet:
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
    known_min_bet: Optional[float] = None,
    corrective_note: Optional[str] = None,
    menu_open: bool = False,
    new_texts: Optional[set] = None,
    disappeared_texts: Optional[set] = None,
    steps_without_screen_change: int = 0,
) -> str:
    phase = determine_phase(current_bet, known_min_bet)
    min_bet_str = f"{known_min_bet:.2f} EUR" if known_min_bet is not None else "not yet known for this slot"

    phase_banner = ""
    if phase == "setup":
        if current_bet is not None:
            phase_banner = (
                f"\nPHASE: BET SETUP - Lower the bet before spinning! Current bet {current_bet:.2f} EUR "
                f"is above the minimum ({min_bet_str})."
            )
        else:
            phase_banner = "\nPHASE: BET SETUP - Lower the bet before spinning!"
        if menu_open:
            phase_banner += (
                "\nA bet menu/overlay appears to be open. This slot's total bet may be made of more "
                "than one selectable option (e.g. a bet-per-line amount AND a number of lines): try the "
                "LOWEST value in EACH selectable row, and watch the TOTAL bet shown on screen — once it "
                "stops decreasing no matter what you try, that is the minimum for this slot. Then look "
                "for a CLOSE/CONFIRM/OK button, or tap the SAME icon you used to open this menu, to "
                "return to the main game screen. Do NOT tap the same bet amount again if you already "
                "selected it — you need to CLOSE the menu now."
            )
        else:
            phase_banner += (
                "\nTO LOWER THE BET: look for the bet amount display (usually shows something like "
                "'€10.00' or 'PUNTATA €10.00') and tap on it to open the bet selection menu. OR look "
                "for +/- buttons near the bet amount. OR look for a coins/credits icon (but note: on "
                "some slots a coin+'+' icon means 'add demo credits', not 'open bet menu' — if it "
                "doesn't work, try the bet amount text itself). DO NOT tap the SPIN button. Focus "
                "ONLY on finding the bet control."
            )
    else:
        phase_banner = (
            "\nBET IS AT MINIMUM. Now find and tap the SPIN button. Do NOT tap anything else — not "
            "autospin/auto-play, not settings, not the bet menu, not any other button — unless SPIN "
            "itself is not visible on screen right now."
        )

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

GOAL: Lower the bet to the LOWEST amount this slot allows, play until total played reaches {target_amount} EUR, then stop.

Step: {step}
Step phase: {phase}{phase_banner}{screen_diff_note}
Current balance (read via OCR): {current_balance if current_balance is not None else "unknown"} EUR
Current bet: {current_bet if current_bet is not None else "unknown"} EUR (minimum bet for this slot: {min_bet_str})
WARNING: If current bet is above minimum, you MUST lower it first. Do not spin with a high bet.
Total played so far: {total_played:.2f} EUR (this is the SUM of all bets, NOT the balance difference)
Net profit/loss: {net_profit_loss} EUR
Target: {target_amount} EUR{goal_note}{exceed_warning}

Previous actions: {action_history[-5:]}

Look at the attached screenshot and decide what to do next.
"""


def build_questions(candidate_texts: list) -> dict:
    return {
        "action": choice_question(
            "What should I do next? Choose 'tap' to touch an element on screen, or 'stop' to finish playing.",
            {
                "tap": "Touch an element on the screen",
                "stop": "Stop playing - goal reached or cannot continue",
            },
        ),
        "target": choice_question(
            "If action is tap, which element should I tap? Answer with the exact text of the "
            "element if it has a visible label, or leave it and use a region instead.",
            {text: text for text in candidate_texts},
        ),
    }


def read_ocr_state(screen_reader: ScreenReader, ocr_reader: SlotOCRReader, device_id: str) -> Optional[dict]:
    ss = screen_reader._screenshot(device_id)
    if not ss.success:
        print(f"ERRORE: screenshot fallito: {ss.error} ({ss.error_code})")
        return None
    return ocr_reader.read_slot_state(ss.data)


def ask_human_help(current_bet: Optional[float], min_bet: Optional[float]) -> tuple:
    """Chiede aiuto all'operatore via terminale quando il sistema non riesce
    ad abbassare la posta da solo dopo troppi tentativi. Non mostra lo
    screenshot: quello lo vede solo GPT-4o-mini via Vision API, non serve
    aprirlo anche sul terminale/desktop dell'operatore. `min_bet` puo' essere
    `None` se il minimo per questa slot non e' ancora stato scoperto. Ritorna
    una tupla:
    - ("stop",): l'operatore vuole fermare tutto
    - ("skip",): l'operatore vuole lasciar riprovare GPT-4o senza aiuto
    - ("coords", x, y): l'operatore ha dato coordinate da tappare
    - ("text", descrizione): comando testuale da eseguire (posizione e/o
      conferma della puntata minima)
    """
    bet_str = f"{current_bet:.2f}" if current_bet is not None else "unknown"
    min_bet_str = f"{min_bet:.2f} EUR" if min_bet is not None else "non ancora nota"
    print(f"\n{'='*60}")
    print("HITL: Non riesco ad abbassare la puntata.")
    print(f"Puntata attuale: {bet_str} EUR | Minimo: {min_bet_str}")
    print(f"{'='*60}")

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
    min_bet: Optional[float],
) -> tuple:
    """Gestisce l'escalation HITL: chiede ripetutamente aiuto all'operatore
    finche' non arriva uno stop, uno skip, o un comando (coordinate o
    descrizione) che abbassa davvero la posta. Sia le coordinate sia le
    descrizioni testuali sono COMANDI eseguiti DIRETTAMENTE qui (tap subito,
    GPT-4o-mini non viene mai interpellato in questa funzione — non ne ha
    nemmeno un riferimento): un log reale ha mostrato che passare la
    descrizione dell'operatore a GPT come "suggerimento" viene ignorato. Una
    descrizione si traduce in coordinate con parse_operator_description
    (etichetta OCR + scostamento relativo, o regione grossolana a griglia
    3x3): se non e' interpretabile si richiede di nuovo aiuto senza tappare
    nulla.

    La descrizione viene anche ispezionata per due segnali generici
    indipendenti dal parsing posizionale: una CONFERMA esplicita che la
    puntata attuale e' il minimo per questa slot, e un INTENTO esplicito di
    spin (nel qual caso un calo di saldo non e' trattato come un incidente).

    Ritorna sempre una quadrupla (azione, dati, puntata_minima_confermata,
    coordinate_tappate):
    - ("stop", None, conferma, None)
    - ("skip", None, conferma, None)
    - ("resolved", ocr_state, conferma, (x, y)) — posta abbassata con successo
    - ("spin_confirmed", ocr_state, conferma, (x, y)) — calo di saldo ma
      l'operatore aveva esplicitamente chiesto di spinnare
    - ("hint", direttiva, conferma, (x, y)) — lo schermo e' cambiato dopo il
      tap dell'operatore: direttiva generica sullo stato osservato (mai la
      frase originale, che GPT ha gia' dimostrato di ignorare)
    'conferma' e 'coordinate_tappate' possono essere None se non applicabili
    a quell'esito."""
    confirmed_min_bet: Optional[float] = None
    while True:
        outcome = ask_human_help(current_bet, min_bet)

        if outcome[0] == "stop":
            return ("stop", None, confirmed_min_bet, None)
        if outcome[0] == "skip":
            return ("skip", None, confirmed_min_bet, None)

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
                return ("resolved", ocr_state, confirmed_min_bet, (x, y))
            print("La posta non sembra essere cambiata. Richiedo di nuovo aiuto.")
            if new_bet is not None:
                current_bet = new_bet
            continue

        # outcome[0] == "text": descrizione libera dell'operatore, e' un
        # COMANDO da eseguire subito, non un suggerimento per GPT.
        description = outcome[1]

        if _looks_like_min_bet_confirmation(description):
            confirmed_min_bet = current_bet
            bet_str = f"{current_bet:.2f} EUR" if current_bet is not None else "sconosciuta"
            print(f"Operatore conferma: {bet_str} e' la puntata minima per questa slot.")

        ss = screen_reader._screenshot(device_id)
        if not ss.success:
            print(f"ERRORE: screenshot fallito: {ss.error} ({ss.error_code})")
            continue
        pre_tap_state = ocr_reader.read_slot_state(ss.data)
        width, height = get_image_dimensions(ss.data)

        parsed_target = parse_operator_description(description, pre_tap_state.get("raw_texts", []), width, height)
        if parsed_target is None:
            print(
                "Non ho capito dove tappare. Prova con coordinate (es. '540,600') o una "
                "descrizione posizionale (es. 'in basso a destra', 'sopra PUNTATA')."
            )
            continue

        x, y = parsed_target
        print(f"Eseguo il tap suggerito dall'operatore ('{description}') in ({x},{y})")
        touch_injector.tap_coordinates(device_id, x, y)
        time.sleep(3)
        ocr_state = read_ocr_state(screen_reader, ocr_reader, device_id)
        if ocr_state is None:
            continue

        new_bet = ocr_state.get("posta")
        if new_bet is not None and current_bet is not None and new_bet < current_bet:
            print(f"Posta abbassata con successo: {current_bet:.2f} -> {new_bet:.2f} EUR.")
            return ("resolved", ocr_state, confirmed_min_bet, (x, y))

        pre_tap_balance = pre_tap_state.get("saldo")
        new_balance = ocr_state.get("saldo")
        if new_balance is not None and pre_tap_balance is not None and new_balance < pre_tap_balance:
            if any(kw in description.lower() for kw in _SPIN_INTENT_KEYWORDS):
                print(f"Spin eseguito su richiesta esplicita dell'operatore: saldo {pre_tap_balance} -> {new_balance}.")
                if new_bet is not None:
                    current_bet = new_bet
                return ("spin_confirmed", ocr_state, confirmed_min_bet, (x, y))
            print(f"ATTENZIONE: il saldo e' sceso ({pre_tap_balance} -> {new_balance}): hai spinato per errore?")
            if new_bet is not None:
                current_bet = new_bet
            continue

        pre_tap_texts = normalize_texts(pre_tap_state.get("raw_texts", []))
        post_tap_texts = normalize_texts(ocr_state.get("raw_texts", []))
        if post_tap_texts - pre_tap_texts:
            print("Lo schermo e' cambiato dopo il tap. Segnalo a GPT lo stato osservato (non la frase originale).")
            return ("hint", _HITL_SCREEN_CHANGED_DIRECTIVE, confirmed_min_bet, (x, y))

        print("Non sembra essere cambiato nulla. Richiedo di nuovo aiuto.")
        if new_bet is not None:
            current_bet = new_bet
        continue


def _slot_memory_path(slot_name: str) -> str:
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in slot_name.strip().lower())
    return os.path.join(SLOT_MEMORY_DIR, f"{safe_name or 'unknown'}.json")


def load_slot_memory(slot_name: str) -> dict:
    """Carica cio' che e' stato imparato su questa slot in esecuzioni
    precedenti (coordinate spin, suggerimenti confermati utili, puntata
    minima scoperta, icona di apertura/chiusura del menu puntata). Ritorna
    una memoria vuota se non esiste ancora nulla o il file e' illeggibile."""
    empty = {
        "known_spin_coords": None, "learned_hints": [],
        "known_min_bet": None, "known_bet_menu_toggle": None,
    }
    path = _slot_memory_path(slot_name)
    if not os.path.exists(path):
        return empty
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return empty
    coords = data.get("known_spin_coords")
    toggle = data.get("known_bet_menu_toggle")
    return {
        "known_spin_coords": tuple(coords) if coords else None,
        "learned_hints": list(data.get("learned_hints", [])),
        "known_min_bet": data.get("known_min_bet"),
        "known_bet_menu_toggle": tuple(toggle) if toggle else None,
    }


def save_slot_memory(slot_name: str, memory: dict) -> None:
    os.makedirs(SLOT_MEMORY_DIR, exist_ok=True)
    path = _slot_memory_path(slot_name)
    serializable = {
        "known_spin_coords": list(memory["known_spin_coords"]) if memory.get("known_spin_coords") else None,
        "learned_hints": list(memory.get("learned_hints", [])),
        "known_min_bet": memory.get("known_min_bet"),
        "known_bet_menu_toggle": (
            list(memory["known_bet_menu_toggle"]) if memory.get("known_bet_menu_toggle") else None
        ),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2)


def _persist_slot_memory(
    slot_name: Optional[str],
    known_spin_coords: Optional[tuple] = None,
    new_hints: Optional[list] = None,
    known_min_bet: Optional[float] = None,
    known_bet_menu_toggle: Optional[tuple] = None,
) -> None:
    """Aggiorna la memoria su file per questa slot (se un nome e' stato dato):
    unisce quanto gia' salvato con le nuove informazioni, senza perdere nulla."""
    if not slot_name:
        return
    memory = load_slot_memory(slot_name)
    if known_spin_coords is not None:
        memory["known_spin_coords"] = known_spin_coords
    if new_hints:
        hints = set(memory.get("learned_hints", []))
        hints.update(new_hints)
        memory["learned_hints"] = sorted(hints)
    if known_min_bet is not None:
        memory["known_min_bet"] = known_min_bet
    if known_bet_menu_toggle is not None:
        memory["known_bet_menu_toggle"] = known_bet_menu_toggle
    save_slot_memory(slot_name, memory)


def run_autonomous_session(
    device_id: str,
    target_amount: float,
    max_steps: int,
    save_screenshots: bool = False,
    slot_name: Optional[str] = None,
    known_min_bet: Optional[float] = None,
) -> bool:
    """Gioca la slot gia' aperta sul device lasciando che GPT-4o-mini Vision
    scelga autonomamente cosa tappare guardando lo screenshot ad ogni passo.
    GPT non restituisce mai coordinate pixel dirette (inaffidabile per
    grounding spaziale, vedi slot_anatomy.md): sceglie un elemento OCR per
    testo (risolto con precisione dalle coordinate misurate dall'OCR) o, in
    mancanza di etichetta, una regione grossolana su griglia 3x3 (risolta
    geometricamente, senza coordinate hardcoded per nessuna slot).

    GPT classifica anche lo stato dello schermo (screen_state): e' il segnale
    principale per "menu aperto" (screen_state == 'modal') e per decidere se i
    numeri letti via OCR sono attendibili — molto piu' robusto di un confronto
    tra due letture OCR, che in produzione sono leggermente diverse ad ogni
    lettura anche a schermo fermo (rumore OCR che in passato impediva
    all'anti-loop di accorgersi di tap ripetuti a vuoto).

    Safety-net anti-spin-prematuro: se la posta e' sopra il minimo (fase
    "setup"), un tap su/vicino al tasto spin noto viene BLOCCATO (mai
    eseguito). known_spin_coords viene sia letto dall'OCR (label "SPIN"/"GIRA")
    sia IMPARATO a runtime osservando un calo di saldo dopo un tap, cosi' il
    primo spin "accidentale" (inevitabile: non possiamo bloccare coordinate
    mai osservate prima) non si ripete piu' nella stessa sessione.

    Anti-loop consapevole del contesto: un tap ripetuto sullo stesso punto
    senza alcun cambiamento a schermo viene bloccato dopo
    MAX_REPEAT_ATTEMPTS_OUTSIDE_MENU tentativi fuori da un menu, ma solo dopo
    MAX_REPEAT_ATTEMPTS_INSIDE_MENU dentro un menu: li' si suggeriscono
    coordinate VICINE (nearby_offsets) per affinare la stima invece di
    abbandonare l'area.

    HITL: se dopo MAX_CONSECUTIVE_SETUP_STEPS step consecutivi la fase resta
    "setup", il sistema chiede aiuto all'operatore da terminale (mai uno
    screenshot aperto). I suggerimenti si accumulano (active_hints, ciascuno
    con il proprio TTL) invece di sovrascriversi.

    Memoria persistente: se slot_name e' dato, le coordinate spin imparate, i
    suggerimenti che hanno portato la fase da "setup" a "playing", la puntata
    minima scoperta per questa slot e l'icona di apertura del menu puntata
    vengono salvati su file e ricaricati automaticamente in esecuzioni future
    sulla stessa slot (vedi load_slot_memory/save_slot_memory).

    Puntata minima: MAI assunta fissa. Si traccia il valore piu' basso
    osservato mentre non e' ancora nota (lowest_bet_seen); dopo
    MIN_BET_DISCOVERY_PATIENCE_STEPS letture senza un nuovo minimo, quel
    valore diventa known_min_bet (scoperta empirica). Una conferma esplicita
    dell'operatore durante l'HITL ("abbiamo la puntata minima corretta") la
    stabilisce subito, come fallback.

    Icona menu puntata: se un tap (di GPT o dell'operatore via HITL) precede
    una transizione dello schermo a "modal", le sue coordinate vengono
    imparate come known_bet_menu_toggle_coords e riusate in AUTONOMIA (un
    tap diretto, senza interpellare GPT) per aprire il menu al bisogno —
    mai un default globale: solo se gia' osservato per QUESTA slot.

    total_played e' la SOMMA delle poste piazzate, dedotta osservando il
    saldo — mai saldo_iniziale - saldo_attuale — e contata solo quando l'ultimo
    screen_state noto rende il numero attendibile (non durante spin/loading/
    win_presentation in corso).

    Ritorna True se la sessione si e' conclusa in modo pulito (stop deciso dal
    Decision Engine, target raggiunto o saldo insufficiente), False in caso di
    errore o escalation HITL con stop dall'operatore."""
    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    ocr_reader = SlotOCRReader()
    engine = GPTDecisionClient()

    if save_screenshots:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

    empty_memory = {
        "known_spin_coords": None, "learned_hints": [],
        "known_min_bet": None, "known_bet_menu_toggle": None,
    }
    slot_memory = load_slot_memory(slot_name) if slot_name else empty_memory

    print("=== TEST GIOCO SLOT AUTONOMO (GPT-4o-mini Vision) ===")
    print(f"Device: {device_id}")
    print(f"Target giocato: {target_amount} EUR, max {max_steps} step")
    if slot_name:
        print(f"Slot: {slot_name} (memoria: {len(slot_memory['learned_hints'])} hint, "
              f"spin noto: {slot_memory['known_spin_coords']}, "
              f"puntata minima nota: {slot_memory['known_min_bet']} (da riverificare in questa sessione), "
              f"icona menu nota: {slot_memory['known_bet_menu_toggle']})")
    print("Assicurati che la slot sia aperta in modalita' DEMO sul telefono.")
    input("Premi INVIO per iniziare...")

    ocr_state = read_ocr_state(screen_reader, ocr_reader, device_id)
    if ocr_state is None:
        return False

    initial_balance = ocr_state.get("saldo")
    current_balance = initial_balance
    current_bet = ocr_state.get("posta")
    known_spin_coords = ocr_state.get("spin_button_coords") or slot_memory.get("known_spin_coords")
    # known_min_bet_from_param e' un'affermazione ESPLICITA e attuale
    # dell'operatore per QUESTA invocazione (es. --min-bet): resta fidata
    # subito. Un valore che arriva SOLO dalla memoria su file (scoperto in
    # una sessione passata, potenzialmente sbagliato) NON e' verificato: deve
    # essere ri-confermato dal vivo (plateau o conferma HITL) prima di poter
    # guidare la fase "playing" — mai piu' uno spin sulla sola fiducia nella
    # memoria (bug reale: known_min_bet=4.0 salvato per errore, usato subito).
    known_min_bet_from_param = known_min_bet
    known_min_bet: Optional[float] = known_min_bet_from_param if known_min_bet_from_param is not None else slot_memory.get("known_min_bet")
    min_bet_verified = known_min_bet_from_param is not None
    lowest_bet_seen: Optional[float] = known_min_bet if known_min_bet is not None else current_bet
    steps_without_new_min = 0
    known_bet_menu_toggle_coords: Optional[tuple] = slot_memory.get("known_bet_menu_toggle")
    menu_toggle_autotap_pending = True
    last_menu_toggle_candidate: Optional[tuple] = None
    # Diventa vero solo con un tap su un elemento DIVERSO dall'icona
    # apri/chiudi nota: senza un'icona nota (scoperta da zero) qualunque tap
    # in setup conta gia' come esplorazione (nessuna icona con cui confrontare).
    explored_bet_options_this_session = known_bet_menu_toggle_coords is None
    last_seen_bet_for_awareness: Optional[float] = current_bet
    bet_change_note: Optional[str] = None
    total_played = 0.0
    action_history: list[dict] = []
    steps_since_ocr = 0
    force_ocr_refresh = False
    corrective_note: Optional[str] = None
    active_hints: list[dict] = [
        {"raw": h, "ttl": HINT_TTL_STEPS} for h in slot_memory.get("learned_hints", [])
    ]
    consecutive_setup_steps = 0
    tapped_targets: list[dict] = []
    last_tap_coords: Optional[tuple] = None
    menu_open = False
    last_screen_state: Optional[str] = None
    steps_without_screen_change = 0
    previous_raw_texts: Optional[set] = normalize_texts(ocr_state.get("raw_texts", []))
    new_texts: set = set()
    disappeared_texts: set = set()
    previous_phase = determine_phase(current_bet, known_min_bet if min_bet_verified else None)

    print(f"Saldo iniziale: {initial_balance}")

    for step in range(1, max_steps + 1):
        if force_ocr_refresh or steps_since_ocr >= OCR_REFRESH_INTERVAL:
            previous_balance = current_balance
            ocr_state = read_ocr_state(screen_reader, ocr_reader, device_id)
            if ocr_state is None:
                return False
            steps_since_ocr = 0
            force_ocr_refresh = False
            balance_trusted = last_screen_state in _BALANCE_TRUSTED_STATES
            if ocr_state.get("saldo") is not None:
                current_balance = ocr_state["saldo"]
                if previous_balance is not None and current_balance < previous_balance:
                    if balance_trusted:
                        spin_bet = current_bet if current_bet is not None else (previous_balance - current_balance)
                        total_played += spin_bet
                        print(
                            f"Spin rilevato! Saldo: {previous_balance} -> {current_balance}. "
                            f"Posta: {spin_bet:.2f}. Total played: {total_played:.2f}"
                        )
                        if last_tap_coords is not None:
                            # Anche se l'OCR non ha mai etichettato il tasto SPIN,
                            # abbiamo appena OSSERVATO che tappare qui causa uno
                            # spin: impariamo queste coordinate cosi' il
                            # safety-net qui sotto puo' bloccare i tentativi
                            # successivi durante il setup, invece di rilevarli
                            # solo a posteriori.
                            known_spin_coords = last_tap_coords
                            print(f"Coordinate del tasto SPIN apprese da questo spin: {known_spin_coords}")
                            _persist_slot_memory(slot_name, known_spin_coords, [])
                    else:
                        print(
                            f"Calo di saldo rilevato ({previous_balance} -> {current_balance}) ma l'ultimo "
                            f"stato noto era '{last_screen_state}', non 'idle': non conteggiato per prudenza "
                            "(potrebbe essere un valore in transizione)."
                        )
            if ocr_state.get("posta") is not None:
                current_bet = ocr_state["posta"]
                if (
                    last_seen_bet_for_awareness is not None
                    and current_bet != last_seen_bet_for_awareness
                ):
                    # Non solo il log a terminale: anche GPT deve "accorgersi"
                    # esplicitamente di aver cambiato la puntata, invece di
                    # continuare a cercare come se nulla fosse successo.
                    bet_change_note = (
                        f"NOTE: the displayed bet changed from {last_seen_bet_for_awareness:.2f} EUR "
                        f"to {current_bet:.2f} EUR since your last observation."
                    )
                last_seen_bet_for_awareness = current_bet

                if not min_bet_verified:
                    # La puntata minima di questa slot non e' mai una verita'
                    # assoluta finche' non e' stata OSSERVATA in QUESTA
                    # sessione: un valore che arriva dalla memoria (magari
                    # sbagliato, come nel bug reale known_min_bet=4.0 invece
                    # di 0.20) deve essere ri-confermato, non solo assunto.
                    # Si scopre/conferma osservando quando smette di scendere
                    # nonostante si continui a provare (plateau) — se la
                    # memoria era gia' corretta, lowest_bet_seen parte da li'
                    # e la conferma arriva prima; se era sbagliata, un valore
                    # piu' basso osservato la corregge naturalmente.
                    if lowest_bet_seen is None or current_bet < lowest_bet_seen:
                        lowest_bet_seen = current_bet
                        steps_without_new_min = 0
                    else:
                        steps_without_new_min += 1
                    if (
                        lowest_bet_seen is not None
                        and steps_without_new_min >= MIN_BET_DISCOVERY_PATIENCE_STEPS
                        and explored_bet_options_this_session
                    ):
                        known_min_bet = lowest_bet_seen
                        min_bet_verified = True
                        print(f"Puntata minima verificata per questa slot in questa sessione: {known_min_bet:.2f} EUR (memorizzata).")
                        _persist_slot_memory(slot_name, known_min_bet=known_min_bet)
            if ocr_state.get("spin_button_coords") is not None:
                known_spin_coords = ocr_state["spin_button_coords"]

            # Il confronto testi OCR resta solo per il nudge generico "sei
            # fermo da troppo" (screen_diff_note/steps_without_screen_change):
            # NON decide piu' se un menu e' aperto (screen_state lo fa in modo
            # molto piu' affidabile, vedi sopra), perche' in produzione l'OCR
            # legge testi leggermente diversi ad ogni lettura anche a schermo
            # fermo, mascherando i tap davvero senza effetto.
            current_raw_texts = normalize_texts(ocr_state.get("raw_texts", []))
            new_texts = current_raw_texts - previous_raw_texts if previous_raw_texts is not None else set()
            disappeared_texts = previous_raw_texts - current_raw_texts if previous_raw_texts is not None else set()
            balance_changed = (
                previous_balance is not None and current_balance is not None and current_balance != previous_balance
            )
            screen_changed = bool(new_texts or disappeared_texts) or balance_changed
            steps_without_screen_change = 0 if screen_changed else steps_without_screen_change + 1
            previous_raw_texts = current_raw_texts

            if last_tap_coords is not None:
                tapped_targets.append({"x": last_tap_coords[0], "y": last_tap_coords[1], "screen_changed": screen_changed})
                tapped_targets = tapped_targets[-MAX_TAPPED_TARGETS:]
                last_tap_coords = None
        else:
            steps_since_ocr += 1

        phase = determine_phase(current_bet, known_min_bet if min_bet_verified else None)
        print(f"\n=== Step {step}/{max_steps} (fase: {phase}) ===")
        print(f"Saldo: {current_balance} | Posta: {current_bet} | Giocato finora: {total_played:.2f} EUR")

        if phase == "playing" and previous_phase == "setup" and active_hints:
            # I suggerimenti attivi nel momento in cui la posta e' scesa al
            # minimo vengono considerati confermati utili e salvati per la
            # prossima volta che si gioca questa slot.
            print("Posta al minimo raggiunta: salvo i suggerimenti attivi come confermati utili.")
            _persist_slot_memory(slot_name, new_hints=[h["raw"] for h in active_hints])
        previous_phase = phase

        for h in active_hints:
            h["ttl"] -= 1
        active_hints = [h for h in active_hints if h["ttl"] > 0]
        if phase == "playing":
            active_hints = []

        if (
            phase == "setup"
            and not menu_open
            and known_bet_menu_toggle_coords is not None
            and menu_toggle_autotap_pending
        ):
            # "Training" reale: questa icona ha gia' aperto il menu puntata su
            # QUESTA slot (imparata da un tap precedente, di GPT o
            # dell'operatore) - la ritappiamo subito in autonomia invece di
            # far ripetere a GPT/operatore un lavoro gia' fatto. Un solo
            # tentativo per ciclo: se non funziona si ricade su GPT/HITL.
            tx, ty = known_bet_menu_toggle_coords
            print(f"Riuso l'icona menu-puntata gia' nota per questa slot: tap autonomo in ({tx},{ty}).")
            touch_injector.tap_coordinates(device_id, tx, ty)
            menu_toggle_autotap_pending = False
            last_menu_toggle_candidate = (tx, ty)
            force_ocr_refresh = True
            time.sleep(1.5)
            continue

        if phase == "setup":
            consecutive_setup_steps += 1
            if consecutive_setup_steps > MAX_CONSECUTIVE_SETUP_STEPS:
                print(
                    f"HITL: il sistema non e' riuscito ad abbassare la posta al minimo dopo "
                    f"{MAX_CONSECUTIVE_SETUP_STEPS} step. Chiedo aiuto all'operatore."
                )
                hitl_action, hitl_data, confirmed_min_bet, hitl_tapped_coords = handle_hitl_escalation(
                    screen_reader, ocr_reader, touch_injector, device_id, current_bet,
                    known_min_bet if min_bet_verified else None,
                )
                if confirmed_min_bet is not None and not min_bet_verified:
                    # L'operatore ha guardato lo schermo ORA: e' gia' una
                    # verifica dal vivo, anche se la memoria diceva un valore
                    # diverso (la corregge).
                    known_min_bet = confirmed_min_bet
                    min_bet_verified = True
                    print(f"Puntata minima confermata dall'operatore per questa slot: {known_min_bet:.2f} EUR (memorizzata).")
                    _persist_slot_memory(slot_name, known_min_bet=known_min_bet)

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
                elif hitl_action == "spin_confirmed":
                    # L'operatore ha chiesto esplicitamente di spinnare: non e'
                    # un incidente, va contato regolarmente e la coordinata
                    # imparata come tasto SPIN (come se l'avesse trovato GPT).
                    ocr_state = hitl_data
                    played_amount = None
                    if current_balance is not None and ocr_state.get("saldo") is not None:
                        played_amount = current_balance - ocr_state["saldo"]
                    if ocr_state.get("saldo") is not None:
                        current_balance = ocr_state["saldo"]
                    if ocr_state.get("posta") is not None:
                        current_bet = ocr_state["posta"]
                    if hitl_tapped_coords is not None:
                        known_spin_coords = hitl_tapped_coords
                        _persist_slot_memory(slot_name, known_spin_coords=known_spin_coords)
                    if played_amount is not None and played_amount > 0:
                        total_played += played_amount
                        print(f"Spin confermato dall'operatore: {played_amount:.2f} EUR. Totale giocato: {total_played:.2f} EUR.")
                    previous_raw_texts = normalize_texts(ocr_state.get("raw_texts", []))
                    steps_since_ocr = 0
                    force_ocr_refresh = False
                elif hitl_action == "hint":
                    # Cumulativo, non usa-e-getta: l'operatore ha gia' dovuto
                    # ripetersi troppe volte nei log reali perche' il
                    # suggerimento spariva dopo un solo turno (Causa C).
                    active_hints.append({"raw": hitl_data, "ttl": HINT_TTL_STEPS})
                    if known_bet_menu_toggle_coords is None and hitl_tapped_coords is not None:
                        # Lo schermo e' cambiato dopo QUESTO tap dell'operatore:
                        # e' il segnale generico di un'icona menu-puntata,
                        # imparata qui esattamente come se l'avesse trovata GPT.
                        known_bet_menu_toggle_coords = hitl_tapped_coords
                        print(f"Icona menu-puntata imparata dall'operatore per questa slot: {known_bet_menu_toggle_coords}.")
                        _persist_slot_memory(slot_name, known_bet_menu_toggle=known_bet_menu_toggle_coords)
                # hitl_action == "skip": nessun'altra azione, si riprova con GPT
                phase = determine_phase(current_bet, known_min_bet if min_bet_verified else None)
                previous_phase = phase
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

        resized_path, resized_w, resized_h, orig_w, orig_h = resize_screenshot_for_vision(screenshot_path)
        try:
            if save_screenshots:
                shutil.copy(resized_path, os.path.join(SCREENSHOTS_DIR, f"step_{step:04d}.png"))

            min_bet_gap_note = None
            verify_min_bet_note = None
            if known_min_bet is not None and not min_bet_verified:
                # La memoria non e' verita': va sempre ri-controllata dal vivo
                # in questa sessione (bug reale: un minimo salvato per errore
                # veniva usato subito senza mai riaprire il menu puntata).
                verify_min_bet_note = (
                    f"The remembered minimum bet for this slot is {known_min_bet:.2f} EUR from a previous "
                    "session, but it has NOT been verified yet THIS session — open the bet menu and make "
                    "sure the LOWEST amount actually available now is selected (it might be different)."
                )
            elif known_min_bet is not None and current_bet is not None and current_bet > known_min_bet:
                min_bet_gap_note = (
                    f"The discovered minimum bet for this slot is {known_min_bet:.2f} EUR; the current "
                    f"bet is {current_bet:.2f} EUR — select the option that brings it back down to "
                    f"{known_min_bet:.2f} EUR."
                )

            hint_texts = [f"The human operator suggests: {h['raw']}" for h in active_hints]
            notes = (
                ([corrective_note] if corrective_note else [])
                + ([bet_change_note] if bet_change_note else [])
                + ([verify_min_bet_note] if verify_min_bet_note else [])
                + ([min_bet_gap_note] if min_bet_gap_note else [])
                + hint_texts
            )
            combined_note = " ".join(notes) or None

            raw_texts = ocr_state.get("raw_texts", [])
            candidate_texts = [text for text, _coords, _conf in raw_texts]

            effective_min_bet = known_min_bet if min_bet_verified else None
            state_text = build_vision_state_text(
                initial_balance, current_balance, current_bet, total_played, target_amount, step, action_history,
                known_min_bet=effective_min_bet, corrective_note=combined_note, menu_open=menu_open, new_texts=new_texts,
                disappeared_texts=disappeared_texts, steps_without_screen_change=steps_without_screen_change,
            )
            corrective_note = None
            bet_change_note = None
            new_texts, disappeared_texts = set(), set()
            questions = build_questions(candidate_texts)

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
        screen_state = answers.get("screen_state", {}).get("choice")

        last_screen_state = screen_state
        was_menu_open = menu_open
        menu_open = (screen_state == "modal")
        if not was_menu_open and menu_open and known_bet_menu_toggle_coords is None and last_menu_toggle_candidate is not None:
            # Il tap dell'ultimo step (di GPT, non serve che sia stato
            # l'operatore) precede l'apertura del menu: impariamo queste
            # coordinate come icona menu-puntata per questa slot, cosi' un
            # prossimo ciclo (o una prossima sessione) puo' riaprirlo subito.
            known_bet_menu_toggle_coords = last_menu_toggle_candidate
            print(f"Icona menu-puntata imparata per questa slot: {known_bet_menu_toggle_coords}.")
            _persist_slot_memory(slot_name, known_bet_menu_toggle=known_bet_menu_toggle_coords)
        if was_menu_open and not menu_open:
            # Il momento della verita' per un menu selezione-posta: la HUD
            # sottostante (es. "PUNTATA €X") resta congelata finche' il menu
            # e' aperto e si aggiorna solo quando GPT stesso segnala che non
            # e' piu' in stato 'modal'.
            bet_str = f"{current_bet:.2f} EUR" if current_bet is not None else "sconosciuta"
            print(f"Menu chiuso: posta ora {bet_str}.")
            menu_toggle_autotap_pending = True

        print(f"Decisione: {action} target={target} screen_state={screen_state} (confidence: {confidence:.2f})")

        if screen_state in _TRANSIENT_SCREEN_STATES:
            # Un fotogramma a meta' animazione/minigioco non e' affidabile per
            # decidere alcun tap (ne' per un'azione ne' per un target): si
            # ignora l'intera decisione e si aspetta che lo stato si stabilizzi,
            # invece di ritentare a vuoto (bug reale: tap ripetuti su SPIN o su
            # elementi a caso mentre un minigioco lungo era ancora in corso).
            print(f"Schermo in transizione ({screen_state}): aspetto che l'animazione/minigioco finisca prima di riprovare.")
            time.sleep(TRANSIENT_STATE_WAIT_SECONDS)
            force_ocr_refresh = True
            continue

        if action == "stop":
            print("Il Decision Engine dice STOP. Fine del test.")
            break

        real_target = resolve_target(target, ocr_state.get("raw_texts", []), orig_w, orig_h)
        if real_target is None:
            print("GPT non ha indicato un target risolvibile (ne' testo ne' regione valida). Rileggo lo schermo al prossimo step.")
            force_ocr_refresh = True
            continue
        real_x, real_y = real_target

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
            min_bet_display = f"{known_min_bet:.2f}€" if known_min_bet is not None else "not yet known — keep lowering it"
            corrective_note = (
                f"BLOCKED: You tried to tap the SPIN button but the bet is still {current_bet:.2f}€ "
                f"(minimum for this slot is {min_bet_display}). You MUST lower the bet FIRST. Look for the bet "
                "amount display, a coins icon, or +/- buttons. DO NOT tap the SPIN button."
            )
            action_history.append({"step": step, "action": "blocked_spin_attempt", "target": (real_x, real_y), "phase": phase})
            action_history = action_history[-5:]
            time.sleep(1.0)
            continue

        # Dentro un menu, selezionare una cella diversa spesso non cambia
        # nessun testo OCR ne' il saldo/posta sottostanti (che si aggiornano
        # solo alla chiusura del menu): un tap "fermo" li' non e' un segnale
        # affidabile di errore quanto lo e' fuori da un menu, quindi serve
        # piu' pazienza prima di considerarlo un vero loop.
        repeat_threshold = MAX_REPEAT_ATTEMPTS_INSIDE_MENU if menu_open else MAX_REPEAT_ATTEMPTS_OUTSIDE_MENU
        same_spot_failures = count_consecutive_same_spot_failures(tapped_targets, real_x, real_y)
        if same_spot_failures >= repeat_threshold:
            print(
                f"BLOCCATO: tap ripetuto su ({real_x},{real_y}) senza effetto "
                f"({same_spot_failures} volte, dentro un menu={menu_open})."
            )
            if menu_open:
                # Non spingere GPT ad abbandonare l'area: qui la mancanza di
                # segnale OCR e' attesa anche per un tap corretto, quindi
                # suggeriamo di affinare la stima invece di cambiare zona.
                offsets = nearby_offsets(real_x, real_y)
                corrective_note = (
                    f"You tapped this area {same_spot_failures} times with no visible change in the "
                    "text on screen — but inside a menu, selecting a different option often does not "
                    "show up that way until the menu is closed. Your estimate might just be a bit off. "
                    f"Try one of these nearby coordinates instead: {offsets}."
                )
            else:
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
            if phase == "setup":
                last_menu_toggle_candidate = (real_x, real_y)
                # La verifica del minimo (plateau piu' sotto) deve poter
                # contare su un tentativo VERO su un'opzione, non solo su
                # apri/chiudi ripetuto della stessa icona (bug reale: GPT ha
                # ritappato solo l'icona nota 3 volte, la posta non e' mai
                # cambiata, e il plateau ha "verificato" un valore mai messo
                # alla prova).
                is_toggle_tap = (
                    known_bet_menu_toggle_coords is not None
                    and abs(real_x - known_bet_menu_toggle_coords[0]) <= REPEAT_TAP_TOLERANCE_PX
                    and abs(real_y - known_bet_menu_toggle_coords[1]) <= REPEAT_TAP_TOLERANCE_PX
                )
                if not is_toggle_tap:
                    explored_bet_options_this_session = True

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
    parser.add_argument(
        "--slot-name", default=None,
        help="Nome della slot (es. 'Pirots 5'): abilita la memoria persistente tra esecuzioni "
        f"(coordinate spin, puntata minima scoperta, icona menu-puntata e suggerimenti confermati "
        f"utili, salvati in {SLOT_MEMORY_DIR}/).",
    )
    parser.add_argument(
        "--min-bet", type=float, default=None,
        help="Puntata minima gia' nota per questa slot (EUR), se la conosci gia': salta la fase di "
        "scoperta automatica. Se omesso, viene scoperta osservando le opzioni disponibili (o "
        "confermata dall'operatore durante un'eventuale escalation HITL).",
    )
    args = parser.parse_args()

    ok = run_autonomous_session(
        device_id=args.device_id,
        target_amount=args.target_amount,
        max_steps=args.max_steps,
        save_screenshots=args.save_screenshots,
        slot_name=args.slot_name,
        known_min_bet=args.min_bet,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
