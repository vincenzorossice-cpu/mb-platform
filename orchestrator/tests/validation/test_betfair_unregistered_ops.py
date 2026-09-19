"""
Script di detection per bancate Betfair non registrate su Netcap.

Catena eseguita: BetfairOpenBetsReader (legge le bancate aperte via
uiautomator/OCR) -> AppApiClient.get_operazioni_aperte (operazioni APERTO su
Netcap per il collaboratore) -> RulesEngine.detect_unregistered_operations
(confronta) -> RulesEngine.save_unregistered_alerts (salva gli alert per la
HITL Dashboard).

A differenza di test_e2e_riconciliazione.py, questo script non scrive mai su
Netcap: legge soltanto e produce un report JSON. La scrittura reale
(creazione dell'operazione + riga giocata_sport) avviene esclusivamente dalla
HITL Dashboard, dopo che un umano ha completato book/back_stake/back_odds.

Il device deve essere GIA' sulla schermata Betfair "Scommesse Exchange ->
Aperte" prima di lanciare lo script: non esiste (ancora) una destinazione
TouchInjector per navigarci automaticamente.

Uso:
    python -m orchestrator.tests.validation.test_betfair_unregistered_ops \
        --device-id ZT322ZS58S --collaboratore-id 08
"""

import argparse
import os
import sys
from pathlib import Path

from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.rules_engine.engine import RulesEngine
from orchestrator.modules.screen_reader.betfair_open_bets import BetfairOpenBetsReader
from orchestrator.modules.screen_reader.reader import ScreenReader


def _default_report_dir() -> str:
    return str(Path(__file__).parent / "reports")


def print_summary_table(open_bets: list, alerts: list) -> None:
    header = f"{'Evento':<30}{'Selezione':<15}{'Lay Stake':>12}{'Responsabilità':>16}  {'Stato':<20}"
    print("\n" + header)
    print("-" * len(header))
    for bet in open_bets:
        is_alert = any(a.betfair_open_bet.get("bet_id") == bet.bet_id for a in alerts)
        note = "NON REGISTRATA" if is_alert else "registrata su Netcap"
        print(
            f"{bet.event:<30}{bet.selection:<15}{bet.lay_stake:>12.2f}{bet.liability:>16.2f}  {note:<20}"
        )


def run_detection(
    device_id: str,
    collaboratore_id: str,
    session_token: str,
    report_dir: str,
) -> bool:
    """Esegue la pipeline di detection. Ritorna True se non ci sono stati
    errori fatali (nessuna bancata da registrare e' un esito legittimo)."""
    app_api_client = AppApiClient(session_token=session_token)
    screen_reader = ScreenReader()
    betfair_reader = BetfairOpenBetsReader(screen_reader=screen_reader)
    rules_engine = RulesEngine()

    open_bets_result = betfair_reader.read_open_bets(device_id)
    if not open_bets_result.success:
        print(f"ERRORE FATALE: read_open_bets fallita: {open_bets_result.error} ({open_bets_result.error_code})")
        return False
    open_bets = open_bets_result.data

    if not open_bets:
        print("Nessuna bancata aperta trovata su Betfair.")
        return True

    netcap_ops_result = app_api_client.get_operazioni_aperte(collaboratore_id)
    if not netcap_ops_result.success:
        print(f"ERRORE FATALE: get_operazioni_aperte fallita: {netcap_ops_result.error} ({netcap_ops_result.error_code})")
        return False
    netcap_operations = netcap_ops_result.data

    detect_result = rules_engine.detect_unregistered_operations(open_bets, collaboratore_id, netcap_operations)
    if not detect_result.success:
        print(f"ERRORE FATALE: detect_unregistered_operations fallita: {detect_result.error} ({detect_result.error_code})")
        return False
    alerts = detect_result.data

    print_summary_table(open_bets, alerts)
    print(f"\nBancate aperte: {len(open_bets)} — non registrate: {len(alerts)}")

    if alerts:
        save_result = rules_engine.save_unregistered_alerts(alerts, collaboratore_id, report_dir=report_dir)
        if not save_result.success:
            print(f"ERRORE: save_unregistered_alerts fallita: {save_result.error} ({save_result.error_code})")
            return False
        print(f"Alert salvati in: {save_result.data}")
        print("Apri la HITL Dashboard per completare i dati mancanti (book, back stake, back odds).")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detection bancate Betfair aperte non registrate su Netcap: salva alert per la HITL Dashboard."
    )
    parser.add_argument("--device-id", required=True, help="ID del dispositivo ADB (es. emulator-5554 o seriale reale).")
    parser.add_argument("--collaboratore-id", required=True, help="ID del collaboratore su Netcap.")
    parser.add_argument(
        "--session-token",
        required=False,
        default=None,
        help="Token di sessione per autenticarsi su Netcap. Se omesso, letto da NETCAP_SESSION_TOKEN.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Cartella dove salvare il report JSON (default: reports/ accanto a questo script).",
    )
    args = parser.parse_args()

    session_token = args.session_token or os.getenv("NETCAP_SESSION_TOKEN")
    if not session_token:
        parser.error("--session-token o variabile d'ambiente NETCAP_SESSION_TOKEN richiesti")

    report_dir = args.output_dir or _default_report_dir()

    ok = run_detection(
        device_id=args.device_id,
        collaboratore_id=args.collaboratore_id,
        session_token=session_token,
        report_dir=report_dir,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
