"""
Script di validazione del MultiplierEngine (equal-profit cascade) rispetto
ai risultati prodotti dallo strumento NinjaBet MultiOdds.

Formula validata: la cascata equal-profit implementata in
orchestrator/modules/multiplier_engine/calculator.py, basata sui concetti
operativi descritti in MB BRAIN/P5-SPORT-MULTIPLE-CASCATA.md (copertura a
cascata leg-by-leg: si banca un evento alla volta, quota combinata = prodotto
delle quote back) e in MB BRAIN/P5-FONDAMENTI-MATCHED-BETTING.md (punta/banca,
responsabilita' = liability della lay). Le due guide descrivono il processo
operativo ma non contengono la derivazione matematica: la formula vive nel
codice del MultiplierEngine, che e' l'oggetto di questa validazione.

Uso:
    # Solo i nostri risultati, nessun confronto (utile per ispezionare i numeri):
    python -m orchestrator.tests.validation.test_multiplier_vs_ninjabet

    # Confronto con i risultati NinjaBet MultiOdds:
    python -m orchestrator.tests.validation.test_multiplier_vs_ninjabet \
        --ninjabet-file percorso/ninjabet_results.json

Schema JSON atteso per i risultati NinjaBet (una entry per segnale, chiave
= id del segnale, es. "signal_1"):

    {
      "signal_1": {
        "events": [
          {"lay_stake": 48.50, "liability": 45.20, "profit": 12.30},
          {"lay_stake": 60.10, "liability": 51.00, "profit": 12.30}
        ],
        "profit_all_lost": 12.30
      },
      "signal_2": { ... }
    }

Un segnale assente dal JSON viene stampato con i soli valori del nostro
motore e stato "NO DATA" (non fa fallire lo script). La funzione
run_validation() puo' anche essere chiamata direttamente con un dict Python
al posto di un file, per un uso programmatico.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from orchestrator.modules.multiplier_engine.calculator import MultiplierEngine

TOLERANCE_EUR = 0.50

# 10 segnali di esempio (quote plausibili, non partite reali) che coprono
# doppie, triple e quadruple con commissioni exchange realistiche.
SIGNALS = [
    {
        "id": "signal_1",
        "label": "Doppia - 1.88/1.32 vs 1.88/1.33",
        "back_stake": 50.0,
        "back_odds": [1.88, 1.32],
        "lay_odds": [1.88, 1.33],
        "commission": 0.045,
    },
    {
        "id": "signal_2",
        "label": "Doppia - 1.25/1.17 vs 1.27/1.19",
        "back_stake": 50.0,
        "back_odds": [1.25, 1.17],
        "lay_odds": [1.27, 1.19],
        "commission": 0.045,
    },
    {
        "id": "signal_3",
        "label": "Doppia - 1.27/1.09 vs 1.29/1.11",
        "back_stake": 50.0,
        "back_odds": [1.27, 1.09],
        "lay_odds": [1.29, 1.11],
        "commission": 0.045,
    },
    {
        "id": "signal_4",
        "label": "Doppia - 1.88/1.65 vs 1.89/1.67",
        "back_stake": 50.0,
        "back_odds": [1.88, 1.65],
        "lay_odds": [1.89, 1.67],
        "commission": 0.045,
    },
    {
        "id": "signal_5",
        "label": "Tripla - 1.58/1.12/1.30 vs 1.63/1.17/1.39",
        "back_stake": 50.0,
        "back_odds": [1.58, 1.12, 1.30],
        "lay_odds": [1.63, 1.17, 1.39],
        "commission": 0.045,
    },
    {
        "id": "signal_6",
        "label": "Tripla - 1.58/2.08/3.45 vs 1.61/2.24/3.70",
        "back_stake": 50.0,
        "back_odds": [1.58, 2.08, 3.45],
        "lay_odds": [1.61, 2.24, 3.70],
        "commission": 0.045,
    },
    {
        "id": "signal_7",
        "label": "Tripla - 1.16/1.73/1.80 vs 1.20/1.83/1.86",
        "back_stake": 50.0,
        "back_odds": [1.16, 1.73, 1.80],
        "lay_odds": [1.20, 1.83, 1.86],
        "commission": 0.045,
    },
    {
        "id": "signal_8",
        "label": "Tripla - 1.29/1.48/2.04 vs 1.35/1.55/2.14",
        "back_stake": 50.0,
        "back_odds": [1.29, 1.48, 2.04],
        "lay_odds": [1.35, 1.55, 2.14],
        "commission": 0.045,
    },
    {
        "id": "signal_9",
        "label": "Quadrupla - 1.41/1.64/2.38/2.48 vs 1.48/1.68/2.44/2.58",
        "back_stake": 50.0,
        "back_odds": [1.41, 1.64, 2.38, 2.48],
        "lay_odds": [1.48, 1.68, 2.44, 2.58],
        "commission": 0.045,
    },
    {
        "id": "signal_10",
        "label": "Quadrupla - 2.63/1.19/1.37/1.44 vs 2.68/1.26/1.42/1.49",
        "back_stake": 50.0,
        "back_odds": [2.63, 1.19, 1.37, 1.44],
        "lay_odds": [2.68, 1.26, 1.42, 1.49],
        "commission": 0.045,
    },
]


def values_match(ours: float, theirs: float, tol: float = TOLERANCE_EUR) -> bool:
    return abs(ours - theirs) <= tol


def _tolerance_for(field_name: str, reference_value: float) -> float:
    """FIX-27.4: tolleranza relativa (1%) per lay_stake/liability oltre 100 EUR
    (valori piu' grandi hanno un rumore di arrotondamento proporzionalmente
    maggiore); tolleranza assoluta fissa altrove, incluso sempre per il profit."""
    if field_name in ("lay_stake", "liability") and abs(reference_value) > 100:
        return abs(reference_value) * 0.01
    return TOLERANCE_EUR


def compute_signal(engine: MultiplierEngine, signal: dict):
    """Calcola il risultato del nostro MultiplierEngine per un segnale."""
    return engine.calculate_cascade(
        back_stake=signal["back_stake"],
        back_odds=signal["back_odds"],
        lay_odds=signal["lay_odds"],
        commission=signal.get("commission", 0.045),
    )


def compare_signal(result, ninjabet_entry: Optional[dict]):
    """
    Confronta il risultato del nostro engine con l'entry NinjaBet (se presente).

    Ritorna (status, diffs) dove status e' "PASS", "NO DATA", "ENGINE_FAIL"
    (il motore ha rifiutato il segnale) o "MISMATCH" (discrepanza numerica con
    NinjaBet oltre tolleranza); diffs e' una lista di stringhe che descrivono
    ogni discrepanza fuori tolleranza.
    """
    if not result.success:
        # FIX-27.3: distinto da MISMATCH, non e' un confronto numerico fallito
        return "ENGINE_FAIL", [f"MultiplierEngine ha rifiutato il segnale: {result.error} ({result.error_code})"]

    if ninjabet_entry is None:
        return "NO DATA", []

    diffs = []
    our_events = result.data.events
    nb_events = ninjabet_entry.get("events", [])

    if len(nb_events) != len(our_events):
        diffs.append(
            f"Numero eventi diverso: noi={len(our_events)} NinjaBet={len(nb_events)}"
        )
    else:
        for i, (our_ev, nb_ev) in enumerate(zip(our_events, nb_events), start=1):
            for field_name, nb_key in (
                ("lay_stake", "lay_stake"),
                ("liability", "liability"),
                ("profit_if_won", "profit"),
            ):
                our_val = getattr(our_ev, field_name)
                nb_val = nb_ev.get(nb_key)
                if nb_val is None:
                    diffs.append(f"Evento {i}: campo '{nb_key}' mancante nei dati NinjaBet")
                    continue
                tol = _tolerance_for(field_name, nb_val)  # FIX-27.4
                if not values_match(our_val, nb_val, tol):
                    diffs.append(
                        f"Evento {i} [{nb_key}]: noi={our_val:.2f} NinjaBet={nb_val:.2f} "
                        f"delta={abs(our_val - nb_val):.2f} (tolleranza {tol:.2f})"
                    )

    nb_profit_all_lost = ninjabet_entry.get("profit_all_lost")
    if nb_profit_all_lost is not None and our_events:
        our_profit_all_lost = our_events[-1].profit_if_all_lost
        if not values_match(our_profit_all_lost, nb_profit_all_lost, TOLERANCE_EUR):
            diffs.append(
                f"profit_all_lost: noi={our_profit_all_lost:.2f} "
                f"NinjaBet={nb_profit_all_lost:.2f} "
                f"delta={abs(our_profit_all_lost - nb_profit_all_lost):.2f} "
                f"(tolleranza {TOLERANCE_EUR:.2f})"
            )

    status = "MISMATCH" if diffs else "PASS"  # FIX-27.3
    return status, diffs


def print_signal_table(signal: dict, result, ninjabet_entry: Optional[dict], status: str, diffs: list) -> None:
    print(f"\n=== {signal['id']} — {signal['label']} ===")
    print(
        f"back_stake={signal['back_stake']:.2f}  "
        f"back_odds={signal['back_odds']}  "
        f"lay_odds={signal['lay_odds']}  "
        f"commission={signal.get('commission', 0.045):.3f}"
    )

    if not result.success:
        print(f"  MultiplierEngine FAIL: {result.error} ({result.error_code})")
        print(f"  Stato: {status}")
        return

    nb_events = (ninjabet_entry or {}).get("events", [])
    header = (
        f"{'Evento':<8}{'Lay Stake (noi)':>16}{'Lay Stake (NB)':>16}"
        f"{'Liability (noi)':>18}{'Liability (NB)':>16}"
        f"{'Profit (noi)':>14}{'Profit (NB)':>13}"
    )
    print(header)
    print("-" * len(header))
    for i, ev in enumerate(result.data.events):
        nb_ev = nb_events[i] if i < len(nb_events) else {}
        nb_lay_stake = nb_ev.get("lay_stake")
        nb_liability = nb_ev.get("liability")
        nb_profit = nb_ev.get("profit")
        print(
            f"{i + 1:<8}"
            f"{ev.lay_stake:>16.2f}"
            f"{(f'{nb_lay_stake:.2f}' if nb_lay_stake is not None else 'n/d'):>16}"
            f"{ev.liability:>18.2f}"
            f"{(f'{nb_liability:.2f}' if nb_liability is not None else 'n/d'):>16}"
            f"{ev.profit_if_won:>14.2f}"
            f"{(f'{nb_profit:.2f}' if nb_profit is not None else 'n/d'):>13}"
        )

    nb_profit_all_lost = (ninjabet_entry or {}).get("profit_all_lost")
    print(
        f"profit_all_lost (noi)={result.data.events[-1].profit_if_all_lost:.2f}  "
        f"profit_all_lost (NB)={(f'{nb_profit_all_lost:.2f}' if nb_profit_all_lost is not None else 'n/d')}"  # FIX-BASSA-5
    )
    print(f"total_liability (noi)={result.data.total_liability:.2f}")

    if result.data.warnings:  # FIX-27.2
        for w in result.data.warnings:
            print(f"  Warning: {w}")

    print(f"  Stato: {status}")
    for diff in diffs:
        print(f"    - {diff}")


def run_validation(ninjabet_data: Optional[dict] = None) -> int:
    """
    Esegue la validazione su tutti i SIGNALS, stampa la tabella comparativa
    e il riepilogo finale. Ritorna il codice di uscita: 0 se tutti i segnali
    con dati NinjaBet sono PASS, 1 se almeno uno e' ENGINE_FAIL o MISMATCH,
    2 se nessun segnale aveva dati NinjaBet da confrontare (NO DATA totale).
    """
    ninjabet_data = ninjabet_data or {}
    engine = MultiplierEngine()

    counts = {"PASS": 0, "MISMATCH": 0, "ENGINE_FAIL": 0, "NO DATA": 0}  # FIX-27.3

    for signal in SIGNALS:
        result = compute_signal(engine, signal)
        ninjabet_entry = ninjabet_data.get(signal["id"])
        status, diffs = compare_signal(result, ninjabet_entry)
        print_signal_table(signal, result, ninjabet_entry, status, diffs)
        counts[status] += 1

    total = len(SIGNALS)
    print("\n" + "=" * 60)
    print("RIEPILOGO FINALE")
    print("=" * 60)
    print(f"Segnali totali : {total}")
    print(f"PASS           : {counts['PASS']}")
    print(f"MISMATCH       : {counts['MISMATCH']}")
    print(f"ENGINE_FAIL    : {counts['ENGINE_FAIL']}")
    print(f"NO DATA        : {counts['NO DATA']}")
    print(f"Tolleranza     : ±{TOLERANCE_EUR:.2f} EUR (relativa 1% per lay_stake/liability oltre 100 EUR)")  # FIX-27.4

    if counts["NO DATA"] == total:  # FIX-27.1
        print("\nESITO: NO DATA — nessun confronto eseguito")
        return 2

    if counts["MISMATCH"] == 0 and counts["ENGINE_FAIL"] == 0:
        print("\nESITO: PASS")
        return 0

    print("\nESITO: FAIL")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida il MultiplierEngine confrontandolo con i risultati di NinjaBet MultiOdds."
    )
    parser.add_argument(
        "--ninjabet-file",
        type=Path,
        default=None,
        help="File JSON con i risultati NinjaBet MultiOdds da confrontare (vedi schema nel docstring del modulo).",
    )
    args = parser.parse_args()

    ninjabet_data = None
    if args.ninjabet_file is not None:
        ninjabet_data = json.loads(args.ninjabet_file.read_text())

    sys.exit(run_validation(ninjabet_data))  # FIX-27.1: run_validation ritorna gia' l'exit code


if __name__ == "__main__":
    main()
