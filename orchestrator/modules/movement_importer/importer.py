import os
import re
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.movement_importer.interface import (
    MovementImporterInterface,
    Movement,
    ConsentStatus,
)
from orchestrator.modules.app_api_client.interface import WriteResult


class MovementImporter(MovementImporterInterface):
    """Importatore sincrono di movimenti Hype/PayPal/GoCardless verso Netcap."""

    EMAIL_PARSE_ERROR = "EMAIL_PARSE_ERROR"
    PAYPAL_OCR_FAILED = "PAYPAL_OCR_FAILED"
    NETCAP_WRITE_FAILED = "NETCAP_WRITE_FAILED"
    DUPLICATE_FOUND = "DUPLICATE_FOUND"  # solo informativo, mai usato come error_code

    # FIX-1: "Pagamento"/"addebitato" = soldi escono da Hype verso il book -> deposito sul book
    # FIX-6: (\d[\d.,]*\d) invece di \d+[.,]\d{2} per catturare l'importo intero anche con
    # separatore delle migliaia (es. "1.234,56"), non solo le ultime 2 cifre decimali
    HYPE_PAGAMENTO_RE = re.compile(r"Pagamento di EUR\s+(\d[\d.,]*\d).*?da.*?(\d{2}/\d{2}/\d{4})", re.DOTALL)
    # FIX-1: "Accredito"/"ricevuto" = soldi entrano in Hype dal book -> prelievo dal book
    PAYPAL_ADDEBITO_RE = re.compile(r"Ti abbiamo addebitato EUR\s+(\d[\d.,]*\d).*?Data.*?(\d{2}/\d{2}/\d{4})", re.DOTALL)

    def __init__(
        self,
        app_api_client,
        screen_reader,
        email_folder: str = None,
        email_bodies: list[str] = None,
    ):
        self.app_api_client = app_api_client
        self.screen_reader = screen_reader
        self.email_folder = email_folder  # cartella locale .eml/.txt, stub — IMAP è lavoro futuro
        self._injected_email_bodies = email_bodies  # hook di test, bypassa il filesystem

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def import_hype(self, collaboratore_id: str, date_from: str = None, date_to: str = None) -> Result[list[Movement]]:
        bodies = self._read_email_bodies()
        if not bodies:
            print("Warning: Nessuna email trovata")  # stesso pattern di screen_reader._parse_xml
            return Result.ok([])

        movements: list[Movement] = []
        for body in bodies:
            movement = self._parse_hype_email(body, collaboratore_id)
            if movement is not None:
                movements.append(movement)
        return Result.ok(movements)

    def import_paypal(self, device_id: str, collaboratore_id: str, date_from: str = None) -> Result[list[Movement]]:
        screen = self.screen_reader.read_screen(device_id, "movements")
        if not screen.success:
            return Result.fail(f"Lettura schermo PayPal fallita: {screen.error}", self.PAYPAL_OCR_FAILED)

        movements: list[Movement] = []
        for elem in screen.data.elements:
            # FIX-2: _extract_amount richiesto esplicitamente dalla spec per il testo OCR/UI
            # (a differenza di _clean_italian_amount usato per le email Hype)
            amount = self.screen_reader._extract_amount(elem.text)
            if amount is None:
                continue
            tipo = self._paypal_tipo_from_text(elem.text)
            if tipo is None:
                continue
            movements.append(Movement(
                tipo=tipo,
                importo=amount,
                conto_id=None,
                bookmaker_id=None,
                bookmaker_nome=None,
                data="",
                collaboratore_id=collaboratore_id,
                descrizione="Import automatico PayPal",
                fonte="paypal_ocr",
                confidence=getattr(elem, "confidence", screen.data.confidence),
            ))
        return Result.ok(movements)

    def check_consent_expiry(self, collaboratore_id: str) -> Result[ConsentStatus]:
        print("Warning: GoCardless non configurato")
        return Result.ok(ConsentStatus(
            collaboratore_id=collaboratore_id,
            is_valid=True,
            expires_at=None,
            days_remaining=None,
        ))

    def write_movements_to_netcap(self, movements: list[Movement], session_token: str = None) -> Result[list[WriteResult]]:
        if not movements:
            return Result.ok([])

        # FIX-3: _get_json usato invece di read_movements() perché quest'ultimo scarta
        # conto_id nel MovementRecord, campo indispensabile per la deduplicazione
        # (collaboratore_id + importo + data + conto_id). Nessuna modifica a app_api_client
        # è nello scope di questo modulo.
        raw_res = self.app_api_client._get_json("/movimentazioni")
        if not raw_res.success:
            # se il controllo di deduplicazione fallisce non si può garantire idempotenza:
            # meglio fallire l'intero batch che rischiare doppie scritture silenziose
            return Result.fail(f"Impossibile verificare duplicati: {raw_res.error}", self.NETCAP_WRITE_FAILED)
        all_raw = raw_res.data if isinstance(raw_res.data, list) else []

        results: list[WriteResult] = []
        for movement in movements:
            if self._find_duplicate(all_raw, movement):
                results.append(WriteResult(success=True, netcap_id=None, message="Duplicato, skip"))
                continue

            body = {
                "tipo": movement.tipo,
                "importo": round(movement.importo, 2),
                "conto_id": movement.conto_id,
                "bookmaker_id": movement.bookmaker_id,
                "data": movement.data,
                "collaboratore_id": movement.collaboratore_id,
                "descrizione": movement.descrizione,
                "tag": "auto_import",
            }
            # FIX-4: _post_json usato invece di write_operazione() perché quest'ultimo
            # scrive su /operazioni (endpoint diverso, body diverso); qui serve POST /movimentazioni
            res = self.app_api_client._post_json("/movimentazioni", body)
            if not res.success:
                results.append(WriteResult(success=False, netcap_id=None, message=res.error))
                continue

            data = res.data
            netcap_id = data.get("id") if isinstance(data, dict) else None
            results.append(WriteResult(success=True, netcap_id=netcap_id, message=None))

        return Result.ok(results)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _read_email_bodies(self) -> list[str]:
        if self._injected_email_bodies is not None:
            return self._injected_email_bodies
        if not self.email_folder or not os.path.isdir(self.email_folder):
            return []
        bodies = []
        for name in sorted(os.listdir(self.email_folder)):
            if name.endswith(".eml") or name.endswith(".txt"):
                path = os.path.join(self.email_folder, name)
                with open(path, encoding="utf-8", errors="ignore") as f:
                    bodies.append(f.read())
        return bodies

    def _parse_hype_email(self, body: str, collaboratore_id: str) -> Optional[Movement]:
        match = self.HYPE_PAGAMENTO_RE.search(body)
        if match:
            tipo = "deposito"
        else:
            match = self.PAYPAL_ADDEBITO_RE.search(body)
            if match:
                tipo = "prelievo"
            else:
                return None

        # FIX-5: _clean_italian_amount richiesto esplicitamente dalla spec per gestire
        # correttamente punto/virgola come separatore migliaia/decimali
        importo = self.screen_reader._clean_italian_amount(match.group(1))
        if importo is None:
            return None
        data_iso = self._to_iso_date(match.group(2))

        return Movement(
            tipo=tipo,
            importo=importo,
            conto_id=None,
            bookmaker_id=None,
            bookmaker_nome=None,
            data=data_iso,
            collaboratore_id=collaboratore_id,
            descrizione="Import automatico Hype",
            fonte="hype_email",
            confidence=0.95,
        )

    def _paypal_tipo_from_text(self, text: str) -> Optional[str]:
        t = text.lower()
        if "+" in text or "ricevuto" in t:
            return "prelievo"  # soldi entrano in PayPal -> escono dal book
        if "-" in text or "inviato" in t or "pagato" in t:
            return "deposito"  # soldi escono da PayPal -> entrano nel book
        return None

    def _to_iso_date(self, ddmmyyyy: str) -> str:
        d, m, y = ddmmyyyy.split("/")
        return f"{y}-{m}-{d}"

    def _find_duplicate(self, all_raw: list[dict], movement: Movement) -> bool:
        for item in all_raw:
            if (
                item.get("collaboratore_id") == movement.collaboratore_id
                and item.get("conto_id") == movement.conto_id
                and item.get("data") == movement.data
                and abs(float(item.get("importo", 0)) - movement.importo) < 0.005
            ):
                return True
        return False
