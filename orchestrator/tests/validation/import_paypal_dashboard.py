"""Script che importa movimenti PayPal e li salva per la HITL Dashboard.

Legge i movimenti dallo schermo PayPal via ScreenReader/OCR (MovementImporter.import_paypal),
poi li salva in reports/movimenti_pending_<collaboratore_id>_<timestamp>.json
(MovementImporter.save_pending_movements) cosi' che la HITL Dashboard possa mostrarli
per l'approvazione/rifiuto.

Uso:
    python -m orchestrator.tests.validation.import_paypal_dashboard \
        --device-id ZE223HNBKJ --collaboratore-id 01
"""

import argparse
import os
import sys

from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.movement_importer.importer import MovementImporter
from orchestrator.modules.screen_reader.reader import ScreenReader


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importa movimenti PayPal via OCR e li salva pending per la HITL Dashboard."
    )
    parser.add_argument("--device-id", required=True, help="ID del dispositivo ADB (es. emulator-5554 o seriale reale).")
    parser.add_argument("--collaboratore-id", required=True, help="ID del collaboratore su Netcap.")
    parser.add_argument(
        "--session-token",
        required=False,
        default=None,
        help="Token di sessione per autenticarsi su Netcap. Se omesso, letto da NETCAP_SESSION_TOKEN.",
    )
    args = parser.parse_args()

    session_token = args.session_token or os.getenv("NETCAP_SESSION_TOKEN")
    if not session_token:
        parser.error("--session-token o variabile d'ambiente NETCAP_SESSION_TOKEN richiesti")

    app_api_client = AppApiClient(session_token=session_token)
    screen_reader = ScreenReader()
    importer = MovementImporter(app_api_client, screen_reader)

    import_result = importer.import_paypal(device_id=args.device_id, collaboratore_id=args.collaboratore_id)
    if not import_result.success:
        print(f"ERRORE: import PayPal fallito: {import_result.error} ({import_result.error_code})")
        sys.exit(1)

    save_result = importer.save_pending_movements(import_result.data, args.collaboratore_id)
    if not save_result.success:
        print(f"ERRORE: salvataggio movimenti pending fallito: {save_result.error}")
        sys.exit(1)

    print(f"{len(import_result.data)} movimenti salvati in {save_result.data}.")
    print("Movimenti salvati. Apri la dashboard per approvarli.")


if __name__ == "__main__":
    main()
