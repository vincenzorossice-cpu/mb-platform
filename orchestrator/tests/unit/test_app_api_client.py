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
        assert step1_body["tipologia"] == "Ricorrenti"
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


class TestCreateRiconciliazioneOperazione:

    def test_create_success(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok({"id": 300})) as mock_post:
            result = client.create_riconciliazione_operazione("2026-09-17")

        assert result.success
        assert result.data == 300

        body = mock_post.call_args.kwargs["json"]
        assert body["nome"] == "Riconciliazione 2026-09-17"
        assert body["tipologia"] == "Ricorrenti"
        assert body["categoria"] == "Sport"
        assert body["bookmaker_id"] == 6
        assert body["data_apertura"] == "2026-09-17"

    def test_create_missing_id(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok({})):
            result = client.create_riconciliazione_operazione("2026-09-17")

        assert not result.success
        assert result.error_code == "NETCAP_PARSE_ERROR"

    def test_create_http_error(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_http_error(500)):
            result = client.create_riconciliazione_operazione("2026-09-17")

        assert not result.success
        assert result.error_code == "NETCAP_HTTP_ERROR"


class TestAddRigaRettifica:

    def test_add_riga_book_column(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok({"id": 991})) as mock_post:
            result = client.add_riga_rettifica(145, "08", 6, 190.74, "BOOK", "Differenza riconciliazione")

        assert result.success
        assert result.data == 991
        body = mock_post.call_args.kwargs["json"]
        assert body["profitto_book"] == 190.74
        assert body["profitto_banco"] == 0
        assert body["fee_percentuale"] == 0

    def test_add_riga_banco_column_still_uses_profitto_book(self, client: AppApiClient):
        """FIX bug #2: anche con column='BANCO' l'importo va sempre in profitto_book,
        mai in profitto_banco, per evitare che Netcap applichi la fee Betfair."""
        with patch.object(client.session, "post", return_value=_mock_post_ok({"id": 991})) as mock_post:
            result = client.add_riga_rettifica(145, "08", 6, 190.74, "BANCO", "Differenza riconciliazione")

        assert result.success
        body = mock_post.call_args.kwargs["json"]
        assert body["profitto_book"] == 190.74
        assert body["profitto_banco"] == 0

    def test_add_riga_note_with_bonus(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok({"id": 991})) as mock_post:
            client.add_riga_rettifica(145, "08", 6, 10.0, "BOOK", "reason", bonus_note="BONUS: 5.00")

        body = mock_post.call_args.kwargs["json"]
        assert body["note"] == "reason | BONUS: 5.00"

    def test_add_riga_note_without_bonus(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok({"id": 991})) as mock_post:
            client.add_riga_rettifica(145, "08", 6, 10.0, "BOOK", "reason")

        body = mock_post.call_args.kwargs["json"]
        assert body["note"] == "reason"

    def test_add_riga_missing_id(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok({})):
            result = client.add_riga_rettifica(145, "08", 6, 10.0, "BOOK", "reason")

        assert not result.success
        assert result.error_code == "NETCAP_PARSE_ERROR"


class TestCloseRiga:

    def test_close_riga_success(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok({})) as mock_post:
            result = client.close_riga(991)

        assert result.success
        assert result.data is True
        url = mock_post.call_args.args[0]
        assert url.endswith("/operazioni/righe/991/chiudi")

    def test_close_riga_no_json_body_required(self, client: AppApiClient):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.side_effect = ValueError("no body")
        with patch.object(client.session, "post", return_value=resp):
            result = client.close_riga(991)

        assert result.success
        assert result.data is True

    def test_close_riga_http_error(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_http_error(500)):
            result = client.close_riga(991)

        assert not result.success
        assert result.error_code == "NETCAP_HTTP_ERROR"


class TestCloseOperazione:

    def test_close_operazione_success(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_post_ok({})) as mock_post:
            result = client.close_operazione(145)

        assert result.success
        assert result.data is True
        url = mock_post.call_args.args[0]
        assert url.endswith("/operazioni/145/chiudi")

    def test_close_operazione_http_error(self, client: AppApiClient):
        with patch.object(client.session, "post", return_value=_mock_http_error(500)):
            result = client.close_operazione(145)

        assert not result.success
        assert result.error_code == "NETCAP_HTTP_ERROR"


class TestVerifyBalanceAfterRettifica:

    def _saldi_response(self, saldo: float):
        return {
            "collaboratori": [{"id": "01", "nome": "Test"}],
            "bookmakers": [{"id": 6, "nome": "BETFAIR", "tipo": "exchange"}],
            "matrix": {"01": {"6": saldo}},
        }

    def test_verify_match(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(self._saldi_response(100.0))):
            result = client.verify_balance_after_rettifica("01", 6, 100.0)

        assert result.success
        assert result.data is True

    def test_verify_mismatch(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(self._saldi_response(80.0))):
            result = client.verify_balance_after_rettifica("01", 6, 100.0)

        assert result.success
        assert result.data is False

    def test_verify_epsilon_tolerance(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(self._saldi_response(100.001))):
            result = client.verify_balance_after_rettifica("01", 6, 100.0)

        assert result.success
        assert result.data is True

    def test_verify_collaboratore_not_found(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(self._saldi_response(100.0))):
            result = client.verify_balance_after_rettifica("99", 6, 100.0)

        assert not result.success
        assert result.error_code == "NETCAP_EMPTY_RESULT"

    def test_verify_bookmaker_not_found(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(self._saldi_response(100.0))):
            result = client.verify_balance_after_rettifica("01", 999, 100.0)

        assert not result.success
        assert result.error_code == "NETCAP_EMPTY_RESULT"

    def test_verify_get_fails_propagates(self, client: AppApiClient):
        with patch.object(client.session, "get", side_effect=requests.ConnectionError("refused")):
            result = client.verify_balance_after_rettifica("01", 6, 100.0)

        assert not result.success
        assert result.error_code == "NETCAP_UNREACHABLE"


class TestRiconciliazioneEndToEndFlow:

    def test_full_flow_create_add_close_close_verify(self, client: AppApiClient):
        saldi_response = {
            "collaboratori": [{"id": "08", "nome": "Test"}],
            "bookmakers": [{"id": 6, "nome": "BETFAIR", "tipo": "exchange"}],
            "matrix": {"08": {"6": 190.74}},
        }
        with patch.object(
            client.session, "post",
            side_effect=[
                _mock_post_ok({"id": 145}),   # create_riconciliazione_operazione
                _mock_post_ok({"id": 991}),   # add_riga_rettifica
                _mock_post_ok({}),            # close_riga
                _mock_post_ok({}),            # close_operazione
            ],
        ) as mock_post, patch.object(
            client.session, "get", return_value=_mock_get_ok(saldi_response),
        ) as mock_get:
            create_result = client.create_riconciliazione_operazione("2026-09-17")
            assert create_result.success
            operazione_id = create_result.data
            assert operazione_id == 145

            riga_result = client.add_riga_rettifica(operazione_id, "08", 6, 190.74, "BANCO", "Differenza riconciliazione")
            assert riga_result.success
            riga_id = riga_result.data
            assert riga_id == 991

            close_riga_result = client.close_riga(riga_id)
            assert close_riga_result.success

            close_op_result = client.close_operazione(operazione_id)
            assert close_op_result.success

            verify_result = client.verify_balance_after_rettifica("08", 6, 190.74)
            assert verify_result.success
            assert verify_result.data is True

        assert mock_post.call_count == 4
        assert mock_get.call_count == 1

        add_riga_body = mock_post.call_args_list[1].kwargs["json"]
        assert add_riga_body["profitto_book"] == 190.74
        assert add_riga_body["profitto_banco"] == 0


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


class TestNetcapRequestException:
    """FIX-18: eccezioni requests diverse da ConnectionError/Timeout non devono
    uscire dal client come eccezione grezza."""

    def test_get_json_generic_request_exception(self, client: AppApiClient):
        with patch.object(
            client.session, "get",
            side_effect=requests.exceptions.ChunkedEncodingError("broken stream"),
        ):
            result = client.read_expected_balance("01")

        assert not result.success
        assert result.error_code == "NETCAP_UNREACHABLE"

    def test_post_json_generic_request_exception(self, client: AppApiClient):
        with patch.object(
            client.session, "post",
            side_effect=requests.exceptions.TooManyRedirects("too many redirects"),
        ):
            result = client.create_riconciliazione_operazione("2026-09-17")

        assert not result.success
        assert result.error_code == "NETCAP_UNREACHABLE"

    def test_post_no_body_generic_request_exception(self, client: AppApiClient):
        with patch.object(
            client.session, "post",
            side_effect=requests.exceptions.TooManyRedirects("too many redirects"),
        ):
            result = client.close_riga(991)

        assert not result.success
        assert result.error_code == "NETCAP_UNREACHABLE"

    def test_find_existing_operazione_generic_request_exception(self, client: AppApiClient):
        rect = Rectification(amount=10.0, column="BOOK", reason="r")
        with patch.object(
            client.session, "get",
            side_effect=requests.exceptions.ChunkedEncodingError("broken stream"),
        ):
            result = client.write_rettifica(rect, collaboratore_id="01", bookmaker_id=1)

        assert not result.success
        assert result.error_code == "NETCAP_UNREACHABLE"

    def test_get_operazioni_aperte_generic_request_exception(self, client: AppApiClient):
        with patch.object(
            client.session, "get",
            side_effect=requests.exceptions.InvalidURL("bad url"),
        ):
            result = client.get_operazioni_aperte("01")

        assert not result.success
        assert result.error_code == "NETCAP_UNREACHABLE"


class TestNetcapParseErrorOnMalformedItems:
    """FIX-18: dati per-item malformati nella risposta devono abortire con
    NETCAP_PARSE_ERROR invece di sollevare KeyError/ValueError/TypeError grezzi."""

    def test_read_expected_balance_bad_bookmaker_id(self, client: AppApiClient):
        bad = {
            **SALDI_BOOKMAKER_RESPONSE,
            "matrix": {"01": {"not-an-int": 50.0}},
        }
        with patch.object(client.session, "get", return_value=_mock_get_ok(bad)):
            result = client.read_expected_balance("01")

        assert not result.success
        assert result.error_code == "NETCAP_PARSE_ERROR"

    def test_read_wallet_balance_bad_amount(self, client: AppApiClient):
        bad = {
            **SALDI_BOOKMAKER_RESPONSE,
            "contiMatrix": {"01": {"hype": "not-a-number", "paypal": 50.0}},
        }
        with patch.object(client.session, "get", return_value=_mock_get_ok(bad)):
            result = client.read_wallet_balance("01")

        assert not result.success
        assert result.error_code == "NETCAP_PARSE_ERROR"

    def test_read_book_status_bad_bookmaker_id(self, client: AppApiClient):
        bad = [{"collaboratore_id": "01", "bookmaker_id": "not-an-int", "stato": "aperto"}]
        with patch.object(client.session, "get", return_value=_mock_get_ok(bad)):
            result = client.read_book_status("01")

        assert not result.success
        assert result.error_code == "NETCAP_PARSE_ERROR"

    def test_read_movements_missing_id(self, client: AppApiClient):
        bad = [{"tipo": "deposito", "importo": 30, "bookmaker_id": 1,
                 "collaboratore_id": "01", "data": "2026-09-13"}]  # niente "id"
        with patch.object(client.session, "get", return_value=_mock_get_ok(bad)):
            result = client.read_movements("01")

        assert not result.success
        assert result.error_code == "NETCAP_PARSE_ERROR"

    def test_verify_balance_bad_saldo_value(self, client: AppApiClient):
        bad = {
            "collaboratori": [{"id": "01", "nome": "Test"}],
            "bookmakers": [{"id": 6, "nome": "BETFAIR", "tipo": "exchange"}],
            "matrix": {"01": {"6": "not-a-number"}},
        }
        with patch.object(client.session, "get", return_value=_mock_get_ok(bad)):
            result = client.verify_balance_after_rettifica("01", 6, 100.0)

        assert not result.success
        assert result.error_code == "NETCAP_PARSE_ERROR"


class TestSaldiBookmakerCaching:
    """FIX-25: /saldi-bookmaker e' condiviso da read_expected_balance,
    read_wallet_balance e verify_balance_after_rettifica; deve essere
    riusato entro 60s invece di rifare una GET per ciascuno."""

    def test_second_saldi_call_uses_cache(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)) as mock_get:
            client.read_expected_balance("01")
            client.read_wallet_balance("01")

        assert mock_get.call_count == 1

    def test_clear_cache_forces_fresh_get(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)) as mock_get:
            client.read_expected_balance("01")
            client.clear_cache()
            client.read_wallet_balance("01")

        assert mock_get.call_count == 2

    def test_cache_expires_after_ttl(self, client: AppApiClient):
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)) as mock_get, \
                patch("orchestrator.modules.app_api_client.client.time.time") as mock_time:
            mock_time.return_value = 1000.0
            client.read_expected_balance("01")
            mock_time.return_value = 1000.0 + 61  # oltre il TTL di 60s
            client.read_wallet_balance("01")

        assert mock_get.call_count == 2

    def test_failed_get_does_not_poison_cache(self, client: AppApiClient):
        """Una GET fallita non deve cancellare una cache ancora valida."""
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)):
            client.read_expected_balance("01")  # popola la cache

        with patch.object(client.session, "get", side_effect=requests.ConnectionError("boom")):
            result = client.read_wallet_balance("01")  # servito dalla cache, get() non richiamata

        assert result.success

    def test_write_invalidates_saldi_cache(self, client: AppApiClient):
        """FIX-25: una scrittura (es. close_riga) deve invalidare la cache di
        /saldi-bookmaker, cosi' una lettura successiva ottiene dati freschi."""
        with patch.object(client.session, "get", return_value=_mock_get_ok(SALDI_BOOKMAKER_RESPONSE)) as mock_get, \
                patch.object(client.session, "post", return_value=_mock_post_ok({})):
            client.read_expected_balance("01")  # popola la cache
            client.close_riga(991)              # deve invalidare la cache
            client.read_expected_balance("01")  # deve rifare la GET

        assert mock_get.call_count == 2
