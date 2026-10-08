import re
import time
from typing import Callable, Optional

from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.tests.validation.test_slot_autonomous import (
    _HITL_SCREEN_CHANGED_DIRECTIVE,
    _SPIN_INTENT_KEYWORDS,
    _looks_like_min_bet_confirmation,
    ask_human_help,
    get_image_dimensions,
    normalize_texts,
    parse_operator_description,
    read_ocr_state,
)

_TAP_SETTLE_SECONDS = 3
# Una frase che indica la cella da un VALORE ("il piu' basso", "quella da 0.10", "il minimo"): se la
# vista non la trova, il vecchio interprete a parole chiave leggerebbe "basso" come "in basso" e
# toccherebbe un punto a caso. Meglio ammettere di non aver capito e richiedere aiuto.
# Verbi all'imperativo con cui l'operatore chiede un tocco ("premi la X", "tocca il meno", "apri il menu").
_TAP_INSTRUCTION = re.compile(
    r"\b(premi|premere|tocca|toccare|tappa|tappare|clicca|cliccare|apri|aprire|chiudi|chiudere|seleziona|"
    r"scegli|usa|vai|press|tap|click|open|close|select|choose)\b",
    re.IGNORECASE,
)
_VALUE_DESCRIPTION = re.compile(
    r"\d+[.,]\d+|\b(pi[uù]'?\s+(basso|bassa|alto|alta|piccolo|piccola|grande)|minim\w*|massim\w*|"
    r"lowest|highest|smallest|largest)\b",
    re.IGNORECASE,
)


def handle_precise_hitl_escalation(
    screen_reader, ocr_reader, touch_injector, device_id: str,
    current_bet: Optional[float], min_bet: Optional[float],
    describe: Callable[[str, LocateContext], Optional[tuple]], element: str = "",
) -> tuple:
    """Stesso contratto di handle_hitl_escalation (test_slot_autonomous.py, mai
    modificato): ("stop"|"skip"|"resolved"|"spin_confirmed"|"hint", dati,
    puntata_minima_confermata, coordinate_tappate).

    Differenza: una descrizione a parole dell'operatore ("l'icona delle monete
    impilate in basso al centro") non viene ridotta alle sole parole di
    posizione (una cella 3x3 da 360x800 px, che non colpisce mai un'icona):
    `describe(frase, contesto)` la cerca sullo screenshot con la griglia fine e
    ritorna il punto da tappare. Se non trova nulla, si ripiega sul vecchio
    interprete a parole chiave (grossolano ma mai peggio di prima)."""
    confirmed_min_bet: Optional[float] = None
    while True:
        outcome = (ask_human_help(current_bet, min_bet, element="spin") if element == "spin"
                   else ask_human_help(current_bet, min_bet))

        if outcome[0] == "stop":
            return ("stop", None, confirmed_min_bet, None)
        if outcome[0] == "skip":
            return ("skip", None, confirmed_min_bet, None)

        description = None
        pre_tap_state = {}
        if outcome[0] == "coords":
            _, x, y = outcome
            print(f"Eseguo il tap suggerito dall'operatore: ({x},{y})")
        else:
            description = outcome[1]
            if _looks_like_min_bet_confirmation(description):
                if not _TAP_INSTRUCTION.search(description):
                    # Una frase di stato, non un'istruzione: niente da cercare e da toccare.
                    print("Operatore conferma che la puntata e' gia' al minimo: non tocco nulla, verifico io lo schermo.")
                    return ("info", description, current_bet, None)
                if current_bet is not None:
                    confirmed_min_bet = current_bet
                    print(f"Operatore conferma: {current_bet:.2f} EUR e' la puntata minima per questa slot.")

            shot = screen_reader._screenshot(device_id)
            if not shot.success:
                print(f"ERRORE: screenshot fallito: {shot.error} ({shot.error_code})")
                continue
            pre_tap_state = ocr_reader.read_slot_state(shot.data)
            width, height = get_image_dimensions(shot.data)

            point = _describe_safely(describe, description, LocateContext(
                device_id=device_id, slot_name="operator", screenshot_path=shot.data,
                screen_width=width, screen_height=height,
                raw_texts=pre_tap_state.get("raw_texts", []),
            ))
            if point is not None:
                print(f"Ho trovato '{description}' a ({point[0]},{point[1]}): tocco li'.")
            elif _VALUE_DESCRIPTION.search(description):
                print(
                    "Non ho trovato con la vista la cella indicata dal valore: non tocco a caso. "
                    "Riprova indicando l'importo scritto (es. 'la cella 0.10') o le coordinate (es. '540,600')."
                )
                continue
            else:
                point = parse_operator_description(description, pre_tap_state.get("raw_texts", []), width, height)
                if point is None:
                    print(
                        "Non ho capito dove tappare. Prova con coordinate (es. '540,600') o una "
                        "descrizione posizionale (es. 'in basso a destra', 'sopra PUNTATA')."
                    )
                    continue
                print("Non ho trovato l'elemento con la vista: uso solo la posizione approssimativa "
                      f"({point[0]},{point[1]}).")
            x, y = point
            print(f"Eseguo il tap suggerito dall'operatore ('{description}') in ({x},{y})")

        touch_injector.tap_coordinates(device_id, x, y)
        time.sleep(_TAP_SETTLE_SECONDS)
        ocr_state = read_ocr_state(screen_reader, ocr_reader, device_id)
        if ocr_state is None:
            continue

        new_bet = ocr_state.get("posta")
        if new_bet is not None and current_bet is not None and new_bet < current_bet:
            print(f"Posta abbassata con successo: {current_bet:.2f} -> {new_bet:.2f} EUR.")
            return ("resolved", ocr_state, confirmed_min_bet, (x, y))

        if outcome[0] == "coords":
            # Come nel flusso storico: per le coordinate esplicite conta solo
            # se la puntata e' scesa (nessuno stato "prima" da confrontare).
            print("La posta non sembra essere cambiata. Richiedo di nuovo aiuto.")
            if new_bet is not None:
                current_bet = new_bet
            continue

        pre_tap_balance = pre_tap_state.get("saldo")
        new_balance = ocr_state.get("saldo")
        if new_balance is not None and pre_tap_balance is not None and new_balance < pre_tap_balance:
            if description and any(kw in description.lower() for kw in _SPIN_INTENT_KEYWORDS):
                print(f"Spin eseguito su richiesta esplicita dell'operatore: saldo {pre_tap_balance} -> {new_balance}.")
                if new_bet is not None:
                    current_bet = new_bet
                return ("spin_confirmed", ocr_state, confirmed_min_bet, (x, y))
            print(f"ATTENZIONE: il saldo e' sceso ({pre_tap_balance} -> {new_balance}): hai spinato per errore?")
            if new_bet is not None:
                current_bet = new_bet
            continue

        pre_texts = normalize_texts(pre_tap_state.get("raw_texts", []))
        post_texts = normalize_texts(ocr_state.get("raw_texts", []))
        if post_texts - pre_texts:
            print("Lo schermo e' cambiato dopo il tap. Segnalo a GPT lo stato osservato (non la frase originale).")
            return ("hint", _HITL_SCREEN_CHANGED_DIRECTIVE, confirmed_min_bet, (x, y))

        print("Non sembra essere cambiato nulla. Richiedo di nuovo aiuto.")
        if new_bet is not None:
            current_bet = new_bet


def _describe_safely(describe, description: str, context: LocateContext) -> Optional[tuple]:
    try:
        return describe(description, context)
    except Exception as exc:
        print(f"La ricerca con la vista e' fallita ({exc}).")
        return None
