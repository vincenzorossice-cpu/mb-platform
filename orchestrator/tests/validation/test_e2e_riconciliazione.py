"""
Pipeline end-to-end di riconciliazione P6, su dispositivo reale.

Catena eseguita: AppApiClient (saldi attesi + stato book da Netcap) ->
TouchInjector (naviga alla schermata saldo sul telefono via ADB) ->
ScreenReader (legge il saldo reale via uiautomator/OCR) -> RulesEngine
(confronta reale vs atteso, decide l'azione) -> AppApiClient.write_rettifica
(se serve una rettifica).

Procedura di riferimento: MB BRAIN/P6-RICONCILIAZIONE-PROCEDURA.md — si
procede per collaboratore, controllando tutti i book; solo i book in stato
"Aperto" o "Limitato" mostrano un valore riconciliabile. La soglia di
materialita' (0.50 EUR) non e' nella guida ma e' gia' codificata in
RulesEngine.MATERIALITY_THRESHOLD: questo script legge semplicemente
ReconciliationResult.action, non reimplementa la soglia.

write_rettifica scrive dati finanziari reali su Netcap (sistema condiviso).
Per evitare scritture accidentali durante un giro di debug della pipeline,
lo script di default gira in DRY-RUN: calcola e mostra cosa scriverebbe ma
non chiama write_rettifica. Serve il flag --live per abilitare le scritture
reali.

Uso:
    # Dry-run su tutti i book Aperto/Limitato del collaboratore:
    python -m orchestrator.tests.validation.test_e2e_riconciliazione \
        --device-id emulator-5554 --collaboratore-id C001 --session-token XYZ

    # Solo alcuni book, scrittura reale delle rettifiche:
    python -m orchestrator.tests.validation.test_e2e_riconciliazione \
        --device-id emulator-5554 --collaboratore-id C001 --session-token XYZ \
        --books 1 7 12 --live
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.app_api_client.interface import BookStatus, ExpectedBalance
from orchestrator.modules.rules_engine.engine import RulesEngine
from orchestrator.modules.rules_engine.interface import Account
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.touch_injector.injector import TouchInjector

OPEN_STATI = {"Aperto", "Limitato"}


def _default_output_path() -> Path:
    # FIX-9: timestamp calcolato al momento della chiamata (non all'import del modulo),
    # cosi' run diversi non si sovrascrivono a vicenda
    # FIX-26.4: i report generati vivono in reports/ (in .gitignore), separati
    # dai fixture di input version-controlled come ninjabet_results.json
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(__file__).parent / "reports" / f"test_e2e_results_{ts}.json"


def is_betfair_book(bookmaker_nome: str) -> bool:
    return "betfair" in bookmaker_nome.lower()


def fetch_netcap_data(app_api_client: AppApiClient, collaboratore_id: str):
    """
    Legge saldi attesi e stato book da Netcap una sola volta.

    Ritorna (expected_by_id, book_statuses) oppure (None, None) se una delle
    due chiamate fallisce (il chiamante gestisce il caso None): senza questi
    dati non si puo' riconciliare nulla, ma la funzione resta riutilizzabile
    e non termina il processo dall'interno.
    """
    expected_result = app_api_client.read_expected_balance(collaboratore_id)
    if not expected_result.success:
        print(f"ERRORE FATALE: read_expected_balance fallita: {expected_result.error} ({expected_result.error_code})")
        return None, None  # FIX-26.1

    book_status_result = app_api_client.read_book_status(collaboratore_id)
    if not book_status_result.success:
        print(f"ERRORE FATALE: read_book_status fallita: {book_status_result.error} ({book_status_result.error_code})")
        return None, None  # FIX-26.1

    expected_by_id = {eb.bookmaker_id: eb for eb in expected_result.data}
    return expected_by_id, book_status_result.data


def select_books(
    requested_ids: Optional[list],
    expected_by_id: dict,
    book_statuses: list,
) -> list:
    """
    Determina quali bookmaker_id processare.

    Se requested_ids e' dato, usa quelli (avvisando per gli id senza saldo
    atteso, o con uno stato noto diverso da Aperto/Limitato). Altrimenti usa
    tutti i book con stato in OPEN_STATI che hanno anche un saldo atteso
    associato.
    """
    stato_by_id = {bs.bookmaker_id: bs.stato for bs in book_statuses}  # FIX-26.3

    if requested_ids:
        selected = []
        for bid in requested_ids:
            if bid not in expected_by_id:
                print(f"AVVISO: bookmaker_id {bid} richiesto ma assente in read_expected_balance, saltato.")
                continue
            stato = stato_by_id.get(bid)
            if stato is not None and stato not in OPEN_STATI:
                # FIX-26.3: un book esplicitamente richiesto ma non Aperto/Limitato
                # (es. Chiuso) va saltato, non riconciliato
                print(f"AVVISO: bookmaker_id {bid} richiesto ma ha stato '{stato}' (non Aperto/Limitato), saltato.")
                continue
            selected.append(bid)
        return selected

    open_ids = {bs.bookmaker_id for bs in book_statuses if bs.stato in OPEN_STATI}
    return [bid for bid in expected_by_id if bid in open_ids]


def process_book(
    bookmaker_id: int,
    expected: ExpectedBalance,
    stato: str,
    device_id: str,
    collaboratore_id: str,
    touch_injector: TouchInjector,
    screen_reader: ScreenReader,
    rules_engine: RulesEngine,
    app_api_client: AppApiClient,
    live: bool,
) -> dict:
    """Esegue la pipeline di riconciliazione per un singolo book."""
    book_name = expected.bookmaker_nome
    record = {
        "bookmaker_id": bookmaker_id,
        "book": book_name,
        "stato": stato,
        "expected_balance": expected.saldo_atteso,
    }

    try:
        nav_result = touch_injector.navigate_to(device_id, "balance_page", book_name)
        if not nav_result.success:
            record["status"] = "error"
            record["error"] = f"navigate_to fallita: {nav_result.error} ({nav_result.error_code})"
            return record

        balance_result = screen_reader.read_balance(device_id, book_name)
        if not balance_result.success:
            record["status"] = "error"
            record["error"] = f"read_balance fallita: {balance_result.error} ({balance_result.error_code})"
            return record

        balance_reading = balance_result.data
        record["real_balance"] = {
            "raw_balance": balance_reading.raw_balance,
            "open_bets": balance_reading.open_bets,
            "real_bonus": balance_reading.real_bonus,
            "wagering_bonus": balance_reading.wagering_bonus,
            "confidence": balance_reading.confidence,
            "read_method": balance_reading.read_method,
        }

        account = Account(
            account_id=str(bookmaker_id),
            identity_id=collaboratore_id,
            book=book_name,
            is_betfair=is_betfair_book(book_name),
        )
        reconcile_result = rules_engine.reconcile(balance_reading, expected.saldo_atteso, account)
        if not reconcile_result.success:
            record["status"] = "error"
            record["error"] = f"reconcile fallita: {reconcile_result.error} ({reconcile_result.error_code})"
            return record

        rec = reconcile_result.data
        record["adjusted_balance"] = rec.adjusted_balance
        record["difference"] = rec.difference
        record["column"] = rec.column
        record["bonus_note"] = rec.bonus_note
        record["action"] = rec.action
        record["rules_applied"] = rec.rules_applied
        record["status"] = "ok"
        # una bancata Betfair non registrata su Netcap fa scattare open_bets_netcap_mismatch
        # qui sopra come hitl_required generico: va gestita dal flusso dedicato
        # (test_betfair_unregistered_ops.py), non mostrata come rettifica ambigua
        record["betfair_open_bets_candidate"] = (
            account.is_betfair and "open_bets_netcap_mismatch" in rec.rules_applied
        )

        if rec.action == "adjust" and rec.rectification is not None:
            record["rectification"] = asdict(rec.rectification)
            if live:
                write_result = app_api_client.write_rettifica(rec.rectification, collaboratore_id, bookmaker_id)
                if not write_result.success:
                    record["rettifica_scritta"] = False
                    record["rettifica_errore"] = f"{write_result.error} ({write_result.error_code})"
                else:
                    write_data = write_result.data
                    record["rettifica_scritta"] = write_data.success
                    record["rettifica_netcap_id"] = write_data.netcap_id
                    record["rettifica_messaggio"] = write_data.message
                    if not write_data.success:
                        record["rettifica_errore"] = write_data.message
            else:
                record["rettifica_scritta"] = False
                record["rettifica_nota"] = "[DRY-RUN] rettifica non scritta (usare --live per scrivere su Netcap)"

        return record

    except Exception as exc:  # noqa: BLE001 - non deve bloccare gli altri book
        record["status"] = "error"
        record["error"] = f"Eccezione non gestita: {exc!r}"
        return record


def print_summary_table(results: list) -> None:
    header = f"{'Book':<20}{'Saldo reale':>14}{'Saldo atteso':>15}{'Differenza':>13}{'Action':>16}  {'Note':<40}"
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        if r["status"] == "error":
            print(f"{r['book']:<20}{'n/d':>14}{r['expected_balance']:>15.2f}{'n/d':>13}{'ERROR':>16}  {r['error']}")
            continue

        real_balance = r["adjusted_balance"]  # FIX-26.2: usa il valore gia' calcolato da RulesEngine
        note = ""
        if r["action"] == "adjust":
            if r.get("rettifica_scritta"):
                note = f"rettifica scritta (netcap_id={r.get('rettifica_netcap_id')})"
            elif "rettifica_errore" in r:
                note = f"rettifica FALLITA: {r['rettifica_errore']}"
            else:
                note = r.get("rettifica_nota", "")
        elif r["action"] == "hitl_required":
            note = "richiede intervento umano"

        print(
            f"{r['book']:<20}"
            f"{real_balance:>14.2f}"
            f"{r['expected_balance']:>15.2f}"
            f"{r['difference']:>13.2f}"
            f"{r['action']:>16}  {note}"
        )


def print_final_counts(results: list, live: bool) -> None:
    total = len(results)
    counts = {"no_adjustment": 0, "adjust": 0, "hitl_required": 0, "error": 0}
    rettifiche_scritte = 0
    for r in results:
        if r["status"] == "error":
            counts["error"] += 1
            continue
        counts[r["action"]] = counts.get(r["action"], 0) + 1  # FIX-9: evita KeyError su action sconosciuta
        if r["action"] == "adjust" and r.get("rettifica_scritta"):
            rettifiche_scritte += 1

    print("\n" + "=" * 60)
    print("RIEPILOGO FINALE")
    print("=" * 60)
    print(f"Book processati   : {total}")
    print(f"no_adjustment     : {counts['no_adjustment']}")
    print(f"adjust            : {counts['adjust']} (rettifiche scritte: {rettifiche_scritte}, modalita': {'LIVE' if live else 'DRY-RUN'})")
    print(f"hitl_required     : {counts['hitl_required']}")
    print(f"error             : {counts['error']}")


def run_pipeline(
    device_id: str,
    collaboratore_id: str,
    session_token: str,
    requested_books: Optional[list],
    live: bool,
    output_path: Path,
) -> bool:
    """
    Esegue la pipeline e2e completa. Ritorna True se nessun book e' finito
    in errore (hitl_required e' considerato un esito legittimo, non un
    errore della pipeline).
    """
    app_api_client = AppApiClient(session_token=session_token)
    screen_reader = ScreenReader()
    touch_injector = TouchInjector(screen_reader=screen_reader)
    rules_engine = RulesEngine()

    expected_by_id, book_statuses = fetch_netcap_data(app_api_client, collaboratore_id)
    if expected_by_id is None:
        # FIX-26.1: fallimento fatale nel recupero dati Netcap - la pipeline si ferma,
        # ma senza terminare il processo dall'interno di un helper
        return False
    stato_by_id = {bs.bookmaker_id: bs.stato for bs in book_statuses}

    bookmaker_ids = select_books(requested_books, expected_by_id, book_statuses)
    if not bookmaker_ids:
        print("Nessun book da processare (nessun book Aperto/Limitato o --books senza corrispondenze).")
        return True

    results = []
    for bookmaker_id in bookmaker_ids:
        expected = expected_by_id[bookmaker_id]
        stato = stato_by_id.get(bookmaker_id, "sconosciuto")
        record = process_book(
            bookmaker_id=bookmaker_id,
            expected=expected,
            stato=stato,
            device_id=device_id,
            collaboratore_id=collaboratore_id,
            touch_injector=touch_injector,
            screen_reader=screen_reader,
            rules_engine=rules_engine,
            app_api_client=app_api_client,
            live=live,
        )
        results.append(record)

    report = {
        "run": {
            "device_id": device_id,
            "collaboratore_id": collaboratore_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "live" if live else "dry-run",
            "books_richiesti": requested_books,
            "books_processati": bookmaker_ids,
        },
        "results": results,
    }
    # FIX-9: il report va scritto PRIMA di qualunque stampa, cosi' l'audit trail
    # delle scritture --live sopravvive anche se il rendering della tabella crasha
    # (es. un'action non prevista).
    output_path.parent.mkdir(parents=True, exist_ok=True)  # FIX-26.4: crea reports/ se manca
    try:
        output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        print(f"\nReport salvato in: {output_path}")

    print_summary_table(results)
    print_final_counts(results, live)

    return not any(r["status"] == "error" for r in results)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline e2e di riconciliazione P6: legge saldi reali via ADB, li confronta con Netcap e scrive rettifiche."
    )
    parser.add_argument("--device-id", required=True, help="ID del dispositivo ADB (es. emulator-5554 o seriale reale).")
    parser.add_argument("--collaboratore-id", required=True, help="ID del collaboratore su Netcap.")
    parser.add_argument(
        "--session-token",
        required=False,
        default=None,
        help="Token di sessione per autenticarsi su Netcap. Se omesso, letto da NETCAP_SESSION_TOKEN.",
    )  # FIX-8: non piu' obbligatorio, fallback su variabile d'ambiente
    parser.add_argument(
        "--books",
        nargs="*",
        type=int,
        default=None,
        help="Lista di bookmaker_id da testare. Default: tutti i book con stato Aperto o Limitato.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Scrive davvero le rettifiche su Netcap. Senza questo flag lo script gira in dry-run.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Percorso del file JSON di report (default: test_e2e_results_<timestamp>.json nella cartella dello script).",
    )
    args = parser.parse_args()

    # FIX-8: fallback a variabile d'ambiente se --session-token non e' stato passato
    session_token = args.session_token or os.getenv("NETCAP_SESSION_TOKEN")
    if not session_token:
        parser.error("--session-token o variabile d'ambiente NETCAP_SESSION_TOKEN richiesti")

    output_path = args.output or _default_output_path()  # FIX-9

    ok = run_pipeline(
        device_id=args.device_id,
        collaboratore_id=args.collaboratore_id,
        session_token=session_token,
        requested_books=args.books,
        live=args.live,
        output_path=output_path,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
