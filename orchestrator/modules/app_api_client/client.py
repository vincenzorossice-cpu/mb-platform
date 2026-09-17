import os
from datetime import date, datetime
from typing import Optional

import requests

from orchestrator.core.result import Result
from orchestrator.modules.rules_engine.interface import Rectification
from orchestrator.modules.app_api_client.interface import (
    AppApiClientInterface,
    BookStatus,
    ExpectedBalance,
    MovementRecord,
    WalletBalance,
    WriteResult,
)


class AppApiClient(AppApiClientInterface):
    """Client sincrono per l'API Netcap."""

    # error codes
    NETCAP_UNREACHABLE = "NETCAP_UNREACHABLE"
    NETCAP_TIMEOUT = "NETCAP_TIMEOUT"
    NETCAP_HTTP_ERROR = "NETCAP_HTTP_ERROR"
    NETCAP_PARSE_ERROR = "NETCAP_PARSE_ERROR"
    NETCAP_EMPTY_RESULT = "NETCAP_EMPTY_RESULT"

    def __init__(self, base_url: str = "http://37.221.197.184:3001/api", timeout: int = 30, session_token: str = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # FIX-8: fallback a variabile d'ambiente se session_token non passato esplicitamente
        token = session_token or os.getenv("NETCAP_SESSION_TOKEN")
        if token:
            self.session.cookies.set("session_token", token)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def read_expected_balance(self, collaboratore_id: str) -> Result[list[ExpectedBalance]]:
        """GET /saldi-bookmaker → lista ExpectedBalance per il collaboratore."""

        res = self._get_json("/saldi-bookmaker")
        if not res.success:
            return res  # type: ignore[return-value]

        data = res.data
        try:
            matrix = data["matrix"]
            impegnato = data.get("impegnato", {})
            bookmakers_list = data["bookmakers"]
        except (KeyError, TypeError) as exc:
            return Result.fail(f"Struttura inattesa: {exc}", self.NETCAP_PARSE_ERROR)

        if collaboratore_id not in matrix:
            return Result.fail(
                f"Collaboratore {collaboratore_id} non trovato in matrix",
                self.NETCAP_EMPTY_RESULT,
            )

        book_map = self._build_book_map(bookmakers_list)
        coll_matrix = matrix[collaboratore_id]
        coll_impegnato = impegnato.get(collaboratore_id, {})

        balances: list[ExpectedBalance] = []
        for bk_id_str, saldo in coll_matrix.items():
            bk_id = int(bk_id_str)
            info = book_map.get(bk_id)
            balances.append(ExpectedBalance(
                collaboratore_id=collaboratore_id,
                bookmaker_id=bk_id,
                bookmaker_nome=info["nome"] if info else f"BK-{bk_id}",
                saldo_atteso=round(saldo, 2),
                importo_impegnato=round(float(coll_impegnato.get(bk_id_str, 0)), 2),
                is_exchange=info["tipo"] == "exchange" if info else False,
            ))

        return Result.ok(balances)

    def read_wallet_balance(self, collaboratore_id: str) -> Result[WalletBalance]:
        """GET /saldi-bookmaker → WalletBalance dal contiMatrix."""

        res = self._get_json("/saldi-bookmaker")
        if not res.success:
            return res  # type: ignore[return-value]

        data = res.data
        try:
            conti = data["contiMatrix"]
        except (KeyError, TypeError) as exc:
            return Result.fail(f"Struttura inattesa: {exc}", self.NETCAP_PARSE_ERROR)

        if collaboratore_id not in conti:
            return Result.fail(
                f"Collaboratore {collaboratore_id} non trovato in contiMatrix",
                self.NETCAP_EMPTY_RESULT,
            )

        entry = conti[collaboratore_id]
        return Result.ok(WalletBalance(
            collaboratore_id=collaboratore_id,
            saldo_hype=round(float(entry.get("hype", 0)), 2),
            saldo_paypal=round(float(entry.get("paypal", 0)), 2),
        ))

    def read_book_status(self, collaboratore_id: str) -> Result[list[BookStatus]]:
        """GET /stati-book → lista BookStatus per il collaboratore."""

        res = self._get_json("/stati-book")
        if not res.success:
            return res  # type: ignore[return-value]

        data = res.data
        if not isinstance(data, list):
            return Result.fail("Risposta /stati-book non è una lista", self.NETCAP_PARSE_ERROR)

        statuses: list[BookStatus] = []
        for item in data:
            if item.get("collaboratore_id") == collaboratore_id:
                statuses.append(BookStatus(
                    collaboratore_id=collaboratore_id,
                    bookmaker_id=int(item["bookmaker_id"]),
                    stato=item.get("stato", "sconosciuto"),
                    limitazioni=item.get("limitazioni"),
                ))

        return Result.ok(statuses)

    def read_movements(
        self,
        collaboratore_id: str,
        date_from: str = None,
    ) -> Result[list[MovementRecord]]:
        """GET /movimentazioni → lista MovementRecord per il collaboratore."""

        res = self._get_json("/movimentazioni")
        if not res.success:
            return res  # type: ignore[return-value]

        data = res.data
        if not isinstance(data, list):
            return Result.fail("Risposta /movimentazioni non è una lista", self.NETCAP_PARSE_ERROR)

        records: list[MovementRecord] = []
        for item in data:
            if item.get("collaboratore_id") != collaboratore_id:
                continue
            if date_from:
                # FIX-19: normalizza sia la data della riga sia date_from (ISO o italiano
                # DD/MM/YYYY) prima di confrontarle; se una delle due non si riconosce,
                # ripiega sul confronto stringa originale
                item_date = self._parse_date(item.get("data", ""))
                from_date = self._parse_date(date_from)
                if item_date is not None and from_date is not None:
                    if item_date < from_date:
                        continue
                elif item.get("data", "") < date_from:
                    continue
            records.append(MovementRecord(
                id=int(item["id"]),
                tipo=item["tipo"],
                importo=round(float(item["importo"]), 2),
                bookmaker_id=int(item["bookmaker_id"]) if item.get("bookmaker_id") is not None else None,
                bookmaker_nome=item.get("bookmaker_nome"),
                collaboratore_id=collaboratore_id,
                data=item.get("data", ""),
            ))

        return Result.ok(records)

    def write_rettifica(
        self,
        rectification: Rectification,
        collaboratore_id: str,
        bookmaker_id: int,
    ) -> Result[WriteResult]:
        """Registra una rettifica su Netcap con il flusso a 2 step:
        1) crea l'operazione contenitore, 2) aggiunge la riga con l'importo."""
        # FIX-NETCAP-2STEP: Netcap non accetta piu' una rettifica in una sola POST;
        # richiede POST /operazioni seguita da POST /operazioni/{id}/righe.
        existing_check = self._find_existing_operazione_rettifica(bookmaker_id)
        if existing_check.success and existing_check.data is not None:
            operazione_id = existing_check.data
        else:
            create_res = self._create_operazione_rettifica(bookmaker_id, collaboratore_id)
            if not create_res.success:
                return Result.fail(create_res.error, create_res.error_code)
            operazione_id = create_res.data

        riga_res = self._add_riga_rettifica(operazione_id, collaboratore_id, bookmaker_id, rectification)
        if not riga_res.success:
            return Result.fail(
                f"Operazione creata (id={operazione_id}) ma riga fallita: {riga_res.error}",
                riga_res.error_code,
            )

        return Result.ok(WriteResult(success=True, netcap_id=riga_res.data, message="Rettifica scritta"))

    def _find_existing_operazione_rettifica(self, bookmaker_id: int) -> Result[Optional[int]]:
        """FIX-NETCAP-2STEP: cerca un'operazione di rettifica gia' creata oggi per
        questo bookmaker, per evitare di duplicare lo Step 1 (idempotenza)."""
        url = f"{self.base_url}/operazioni"
        params = {
            "nome": "Rettifica riconciliazione",
            "bookmaker_id": bookmaker_id,
            "data_apertura": date.today().isoformat(),
        }
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.ConnectionError as exc:
            return Result.fail(f"Connessione fallita: {exc}", self.NETCAP_UNREACHABLE)
        except requests.Timeout:
            return Result.fail(f"Timeout su {url}", self.NETCAP_TIMEOUT)

        if not resp.ok:
            return Result.fail(f"HTTP {resp.status_code} da {url}", self.NETCAP_HTTP_ERROR)

        try:
            data = resp.json()
        except ValueError as exc:
            return Result.fail(f"JSON non valido: {exc}", self.NETCAP_PARSE_ERROR)

        if isinstance(data, list) and data:
            existing = data[0]
            return Result.ok(existing.get("id") if isinstance(existing, dict) else None)

        return Result.ok(None)

    def _create_operazione_rettifica(self, bookmaker_id: int, collaboratore_id: str) -> Result[int]:
        """FIX-NETCAP-2STEP: Step 1 - crea il contenitore operazione su Netcap."""
        idempotency_key = f"rettifica_{collaboratore_id}_{bookmaker_id}_{date.today().isoformat()}"
        body = {
            "nome": "Rettifica riconciliazione",
            "tipologia": "Ricorrenti",
            "categoria": "Sport",
            "bookmaker_id": bookmaker_id,
            "data_apertura": date.today().isoformat(),
            "note": idempotency_key,
        }
        res = self._post_json("/operazioni", body)
        if not res.success:
            return res  # type: ignore[return-value]

        operazione_id = res.data.get("id") if isinstance(res.data, dict) else None
        if operazione_id is None:
            # FIX-NETCAP-2STEP: risposta Step 1 senza id -> fallimento esplicito
            return Result.fail(f"Risposta Step 1 senza id: {res.data}", self.NETCAP_PARSE_ERROR)
        return Result.ok(operazione_id)

    def _add_riga_rettifica(
        self,
        operazione_id: int,
        collaboratore_id: str,
        bookmaker_id: int,
        rectification: Rectification,
    ) -> Result[int]:
        """FIX-NETCAP-2STEP: Step 2 - aggiunge la riga con l'importo all'operazione."""
        amount = round(rectification.amount, 2)
        profitto_book = amount if rectification.column == "BOOK" else 0
        profitto_banco = amount if rectification.column == "BANCO" else 0

        note = f"Rettifica riconciliazione automatica - {rectification.reason}"
        if rectification.bonus_note:
            note = f"{note} | {rectification.bonus_note}"

        body = {
            "collaboratore_id": collaboratore_id,
            "bookmaker_id": bookmaker_id,
            "tipo_riga": "puntata_e_bancata",
            "profitto_book": profitto_book,
            "profitto_banco": profitto_banco,
            "fee_percentuale": 0,
            "note": note,
        }
        res = self._post_json(f"/operazioni/{operazione_id}/righe", body)
        if not res.success:
            return res  # type: ignore[return-value]

        riga_id = res.data.get("id") if isinstance(res.data, dict) else None
        if riga_id is None:
            # FIX-NETCAP-2STEP: risposta Step 2 senza id -> fallimento esplicito
            return Result.fail(f"Risposta Step 2 senza id: {res.data}", self.NETCAP_PARSE_ERROR)
        return Result.ok(riga_id)

    def write_operazione(self, operazione: dict) -> Result[WriteResult]:
        """POST /operazioni → registra operazione generica su Netcap."""
        return self._write_operazione(operazione)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _get_json(self, path: str) -> Result[dict | list]:
        """GET + parse JSON. Gestisce tutti gli errori infrastrutturali."""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.ConnectionError as exc:
            return Result.fail(f"Connessione fallita: {exc}", self.NETCAP_UNREACHABLE)
        except requests.Timeout:
            return Result.fail(f"Timeout su {url}", self.NETCAP_TIMEOUT)

        if not resp.ok:
            return Result.fail(
                f"HTTP {resp.status_code} da {url}",
                self.NETCAP_HTTP_ERROR,
            )

        try:
            return Result.ok(resp.json())
        except ValueError as exc:
            return Result.fail(f"JSON non valido: {exc}", self.NETCAP_PARSE_ERROR)

    def _post_json(self, path: str, body: dict) -> Result[dict]:
        """POST JSON + parse response."""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.post(url, json=body, timeout=self.timeout)
        except requests.ConnectionError as exc:
            return Result.fail(f"Connessione fallita: {exc}", self.NETCAP_UNREACHABLE)
        except requests.Timeout:
            return Result.fail(f"Timeout su {url}", self.NETCAP_TIMEOUT)

        print(f"[Netcap] POST {url} -> status={resp.status_code} body={resp.text}")  # FIX-NETCAP-2STEP

        if not resp.ok:
            return Result.fail(
                f"HTTP {resp.status_code} da {url}",
                self.NETCAP_HTTP_ERROR,
            )

        try:
            return Result.ok(resp.json())
        except ValueError as exc:
            return Result.fail(f"JSON non valido: {exc}", self.NETCAP_PARSE_ERROR)

    def _write_operazione(self, body: dict) -> Result[WriteResult]:
        """POST /operazioni e wrappa il risultato in WriteResult."""
        res = self._post_json("/operazioni", body)
        if not res.success:
            return Result.ok(WriteResult(success=False, netcap_id=None, message=res.error))

        data = res.data
        netcap_id = data.get("id") if isinstance(data, dict) else None
        return Result.ok(WriteResult(success=True, netcap_id=netcap_id, message=None))

    def _build_book_map(self, bookmakers_list: list[dict]) -> dict[int, dict]:
        """bookmaker_id → {nome, tipo}."""
        return {
            int(bk["id"]): {"nome": bk["nome"], "tipo": bk.get("tipo", "bookmaker")}
            for bk in bookmakers_list
        }

    def _parse_date(self, date_str: str) -> Optional[date]:
        """FIX-19: converte una data in formato ISO (YYYY-MM-DD) o italiano
        (DD/MM/YYYY) in un oggetto date. Ritorna None se il formato non e' riconosciuto."""
        if not date_str:
            return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return None
