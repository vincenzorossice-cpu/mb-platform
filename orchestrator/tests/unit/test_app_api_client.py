import pytest
from unittest.mock import MagicMock, patch

import requests

from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.rules_engine.interface import Rectification


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------

SALDI_BOOKMAKER_RESPONSE = {
    "collaboratori": [
        {"id": "01", "nome": "Test", "conto_principale": "Hype01", "conto_paypal": "Paypal01", "attivo": 1},
    ],
    "bookmakers": [
        {"id": 1, "nome": "ADMIRALBET", "tipo": "bookmaker", "attivo": 1},
        {"id": 6, "nome": "BETFAIR", "tipo": "exchange", "attivo": 1},
    ],
    "matrix": {
        "01": {"1": 50.0, "6": -1.42e-14},
    },
    "impegnato": {
        "01": {"1": 20},
    },
    "contiMatrix": {
        "01": {"hype": 100.0, "paypal": 50.0},
    },
}

STATI_BOOK_RESPONSE = [
    {"collaboratore_id": "01", "bookmaker_id": 1, "stato": "aperto", "limitazioni": None},
    {"collaboratore_id": "01", "bookmaker_id": 6, "stato": "limitato", "limitazioni": "quote basse"},
    {"collaboratore_id": "02", "bookmaker_id": 1, "stato": "aperto", "limitazioni": None},
]

MOVIMENTAZIONI_RESPONSE = [
    {
        "id": 100, "tipo": "deposito", "importo": 30, "conto_id": 6,
        "bookmaker_id": 1, "data": "2026-09-13", "collaboratore_id": "01",
        "conto_nome": "Paypal01", "bookmaker_nome": "ADMIRALBET", "collaboratore_nome": "Test",
    },
    {
        "id": 101, "tipo": "prelievo", "importo": 15, "conto_id": 6,
        "bookmaker_id": None, "data": "2026-09-10", "collaboratore_id": "01",
        "conto_nome": "Hype01", "bookmaker_nome": None, "collaboratore_nome": "Test",
    },
    {
        "id": 102, "tipo": "deposito", "importo": 50, "conto_id": 3,
        "bookmaker_id": 6, "data": "2026-09-12", "collaboratore_id": "02",
        "conto_nome": "Hype02", "bookmaker_nome": "BETFAIR", "collaboratore_nome": "Altro",
    },
]

def _mock_get_ok(json_data):
    """Crea un mock response con .ok=True e .json() che restituisce json_data."""
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = json_data
    return resp


def _mock_post_ok(json_data):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = json_data
    return resp


def _mock_http_error(status: int = 500):
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status
    return resp


@pytest.fixture
def client() -> AppApiClient:
    return AppApiClient(base_url="http://test:3001/api", timeout=5)


# ------------------------------------------------------------------
# tests
# ------------------------------------------------------------------

class TestReadExpectedBalance:

    def test_success(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)):
            result = client.read_expected_balance("01")

        assert result.success
        balances = result.data
        assert len(balances) == 2

        admiralbet = next(b for b in balances if b.bookmaker_id == 1)
        assert admiralbet.saldo_atteso == 50.0
        assert admiralbet.importo_impegnato == 20.0
        assert admiralbet.bookmaker_nome == "ADMIRALBET"
        assert admiralbet.is_exchange is False

    def test_exchange_flag(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)):
            result = client.read_expected_balance("01")

        betfair = next(b for b in result.data if b.bookmaker_id == 6)
        assert betfair.is_exchange is True
        assert betfair.bookmaker_nome == "BETFAIR"

    def test_rounding(self, client: AppApiClient):
        """Floating point noise -1.42e-14 → 0.0."""
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)):
            result = client.read_expected_balance("01")

        betfair = next(b for b in result.data if b.bookmaker_id == 6)
        assert betfair.saldo_atteso == 0.0

    def test_collaboratore_not_found(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)):
            result = client.read_expected_balance("99")

        assert not result.success
        assert result.error_code == "NETCAP_EMPTY_RESULT"


class TestReadWalletBalance:

    def test_success(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)):
            result = client.read_wallet_balance("01")

        assert result.success
        w = result.data
        assert w.saldo_hype == 100.0
        assert w.saldo_paypal == 50.0


class TestReadBookStatus:

    def test_success(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(STATI_BOOK_RESPONSE)):
            result = client.read_book_status("01")

        assert result.success
        assert len(result.data) == 2

        limitato = next(s for s in result.data if s.bookmaker_id == 6)
        assert limitato.stato == "limitato"
        assert limitato.limitazioni == "quote basse"


class TestReadMovements:

    def test_success(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(MOVIMENTAZIONI_RESPONSE)):
            result = client.read_movements("01")

        assert result.success
        assert len(result.data) == 2

        deposito = next(m for m in result.data if m.id == 100)
        assert deposito.tipo == "deposito"
        assert deposito.importo == 30.0
        assert deposito.bookmaker_id == 1

        prelievo = next(m for m in result.data if m.id == 101)
        assert prelievo.bookmaker_id is None


class TestWriteRettifica:
    """FIX-NETCAP-2STEP: write_rettifica ora fa 2 POST in sequenza
    (POST /operazioni poi POST /operazioni/{id}/righe)."""

    def test_write_rettifica_success(self, client: AppApiClient):
        rect = Rectification(amount=190.74, column="BANCO", reason="Differenza riconciliazione: +190.74 EUR")

        with patch.object(client.session, "get", return_value=_mock_get_ok([])) as mock_get, \
                patch.object(
                    client.session, "post",
                    side_effect=[_mock_post_ok({"id": 145}), _mock_post_ok({"id": 991})],
                ) as mock_post:
            result = client.write_rettifica(rect, collaboratore_id="08", bookmaker_id=6)

        assert result.success
        assert result.data.success is True
        assert result.data.netcap_id == 991

        mock_get.assert_called_once()
        assert mock_post.call_count == 2

        step1_body = mock_post.call_args_list[0].kwargs["json"]
        assert step1_body["tipologia"] == "Rettifica"
        assert step1_body["categoria"] == "Sport"
        assert step1_body["bookmaker_id"] == 6

        step2_url = mock_post.call_args_list[1].args[0]
        step2_body = mock_post.call_args_list[1].kwargs["json"]
        assert step2_url.endswith("/operazioni/145/righe")
        assert step2_body["collaboratore_id"] == "08"
        assert step2_body["profitto_banco"] == 190.74
        assert step2_body["profitto_book"] == 0

    def test_write_rettifica_banco(self, client: AppApiClient):
        rect = Rectification(amount=190.74, column="BANCO", reason="r")
        with patch.object(client.session, "get", return_value=_mock_get_ok([])), \
                patch.object(
                    client.session, "post",
                    side_effect=[_mock_post_ok({"id": 145}), _mock_post_ok({"id": 991})],
                ) as mock_post:
            client.write_rettifica(rect, collaboratore_id="08", bookmaker_id=6)

        step2_body = mock_post.call_args_list[1].kwargs["json"]
        assert step2_body["profitto_banco"] == 190.74
        assert step2_body["profitto_book"] == 0

    def test_write_rettifica_book(self, client: AppApiClient):
        rect = Rectification(amount=117.46, column="BOOK", reason="r")
        with patch.object(client.session, "get", return_value=_mock_get_ok([])), \
                patch.object(
                    client.session, "post",
                    side_effect=[_mock_post_ok({"id": 200}), _mock_post_ok({"id": 991})],
                ) as mock_post:
            client.write_rettifica(rect, collaboratore_id="01", bookmaker_id=42)

        step2_body = mock_post.call_args_list[1].kwargs["json"]
        assert step2_body["profitto_book"] == 117.46
        assert step2_body["profitto_banco"] == 0

    def test_write_rettifica_step1_fails(self, client: AppApiClient):
        rect = Rectification(amount=10.0, column="BOOK", reason="r")
        with patch.object(client.session, "get", return_value=_mock_get_ok([])), \
                patch.object(client.session, "post", return_value=_mock_http_error(500)) as mock_post:
            result = client.write_rettifica(rect, collaboratore_id="01", bookmaker_id=1)

        assert not result.success
        assert result.error_code == "NETCAP_HTTP_ERROR"
        mock_post.assert_called_once()

    def test_write_rettifica_step2_fails(self, client: AppApiClient):
        rect = Rectification(amount=10.0, column="BOOK", reason="r")
        with patch.object(client.session, "get", return_value=_mock_get_ok([])), \
                patch.object(
                    client.session, "post",
                    side_effect=[_mock_post_ok({"id": 145}), _mock_http_error(500)],
                ) as mock_post:
            result = client.write_rettifica(rect, collaboratore_id="01", bookmaker_id=1)

        assert not result.success
        assert result.error_code == "NETCAP_HTTP_ERROR"
        assert "Operazione creata" in result.error
        assert "ma riga fallita" in result.error
        assert mock_post.call_count == 2

    def test_write_rettifica_idempotent(self, client: AppApiClient):
        """Se esiste gia' un'operazione oggi per questo bookmaker, salta lo Step 1
        e usa l'operazione_id esistente per lo Step 2."""
        rect = Rectification(amount=10.0, column="BOOK", reason="r")
        with patch.object(client.session, "get", return_value=_mock_get_ok([{"id": 500}])) as mock_get, \
                patch.object(client.session, "post", return_value=_mock_post_ok({"id": 991})) as mock_post:
            result = client.write_rettifica(rect, collaboratore_id="01", bookmaker_id=1)

        assert result.success
        assert result.data.netcap_id == 991
        mock_get.assert_called_once()
        mock_post.assert_called_once()
        step2_url = mock_post.call_args.args[0]
        assert step2_url.endswith("/operazioni/500/righe")


class TestNetcapUnreachable:

    def test_connection_error(self, client: AppApiClient):
        with patch.object(client.session, "get", side_effect=requests.ConnectionError("refused")):
            result = client.read_expected_balance("01")

        assert not result.success
        assert result.error_code == "NETCAP_UNREACHABLE"


class TestNetcapTimeout:

    def test_timeout(self, client: AppApiClient):
        with patch.object(client.session, "get", side_effect=requests.Timeout("timed out")):
            result = client.read_expected_balance("01")

        assert not result.success
        assert result.error_code == "NETCAP_TIMEOUT"


class TestNetcapHttpError:

    def test_server_error(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_http_error(500)):
            result = client.read_expected_balance("01")

        assert not result.success
        assert result.error_code == "NETCAP_HTTP_ERROR"
