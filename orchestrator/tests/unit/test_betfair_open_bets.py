import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from orchestrator.core.result import Result
from orchestrator.dashboard.app import app
from orchestrator.dashboard.data_loader import DEFAULT_REPORT_DIR
from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.app_api_client.interface import WriteResult
from orchestrator.modules.rules_engine.engine import RulesEngine
from orchestrator.modules.rules_engine.interface import UnregisteredOperationAlert
from orchestrator.modules.screen_reader.betfair_open_bets import BetfairOpenBet, BetfairOpenBetsReader
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement
from orchestrator.modules.screen_reader.reader import ScreenReader

REAL_REPORT_DIR = Path(DEFAULT_REPORT_DIR)

# campione reale, estratto da uiautomator (pagina Betfair Scommesse Exchange -> Aperte)
SAMPLE_BET_TEXTS = [
    "Aperte",
    "Piazzata 19-set-26 13:40:45",
    "Dettagli Gil Vicente - Maritimo Pareggio- Esito Finale ID scommessa Betfair 1:443519021791| Abbinate: 19-set-26 15:39:37",
    "Gil Vicente - Maritimo",
    "Pareggio",
    "Esito Finale",
    "Tipo",
    "Banca",
    "Quota 3,50",
    "3,50",
    "Puntata (€) 461,66",
    "461,66",
    "Bonus (€) --",
    "Responsabilità (€) 1.154,15",
    "1.154,15",
    "Profitto potenziale (€) 461,66",
    "461,66",
    "Stato",
    "Abbinate",
]


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------

def _make_screen_reader(texts: list[str]) -> ScreenReader:
    """ScreenReader reale (per riusare _clean_italian_amount), con read_screen
    stubato per restituire gli elementi passati."""
    screen_reader = ScreenReader()
    screen_reader.read_screen = MagicMock(return_value=Result.ok(ScreenContent(
        elements=[UIElement(text=t, bounds=[0, 0, 0, 0]) for t in texts],
        screen_type="open_bets",
        confidence=1.0,
        method="uiautomator",
    )))
    return screen_reader


def _make_bet(bet_id: str = "1:443519021791", lay_stake: float = 461.66, liability: float = 1154.15) -> BetfairOpenBet:
    return BetfairOpenBet(
        bet_id=bet_id,
        event="Gil Vicente - Maritimo",
        selection="Pareggio",
        bet_type="Banca",
        lay_odds=3.50,
        lay_stake=lay_stake,
        liability=liability,
        potential_profit=461.66,
        status="Abbinate",
    )


@pytest.fixture
def engine() -> RulesEngine:
    return RulesEngine()


def _write_alert_report(filename: str, alerts: list[dict], collaboratore_id: str = "08") -> Path:
    REAL_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REAL_REPORT_DIR / filename
    path.write_text(json.dumps({
        "collaboratore_id": collaboratore_id,
        "detected_at": "2026-09-19T16:00:00",
        "alerts": alerts,
    }), encoding="utf-8")
    return path


# ------------------------------------------------------------------
# 1-3: parsing
# ------------------------------------------------------------------

class TestParseBetfairOpenBet:
    def test_parse_one_bet(self):
        """Un blocco Tipo/Banca...Stato/Abbinate produce un BetfairOpenBet completo."""
        reader = BetfairOpenBetsReader(screen_reader=_make_screen_reader(SAMPLE_BET_TEXTS))
        result = reader.read_open_bets("device-1")

        assert result.success
        bets = result.data
        assert len(bets) == 1
        bet = bets[0]
        assert bet.bet_id == "1:443519021791"
        assert bet.event == "Gil Vicente - Maritimo"
        assert bet.selection == "Pareggio"
        assert bet.bet_type == "Banca"
        assert bet.lay_odds == pytest.approx(3.50)
        assert bet.lay_stake == pytest.approx(461.66)
        assert bet.liability == pytest.approx(1154.15)
        assert bet.potential_profit == pytest.approx(461.66)
        assert bet.status == "Abbinate"
        assert bet.market == "Esito Finale"
        assert bet.placed_at == "2026-09-19T13:40:45"
        assert bet.matched_at == "2026-09-19T15:39:37"

    def test_parse_multiple_bets(self):
        """Due blocchi concatenati -> 'pending' si resetta correttamente tra i due."""
        texts = SAMPLE_BET_TEXTS + SAMPLE_BET_TEXTS
        reader = BetfairOpenBetsReader(screen_reader=_make_screen_reader(texts))
        result = reader.read_open_bets("device-1")

        assert result.success
        assert len(result.data) == 2
        for bet in result.data:
            assert bet.event == "Gil Vicente - Maritimo"
            assert bet.lay_stake == pytest.approx(461.66)

    def test_parse_no_open_bets(self):
        """Nessun blocco Tipo/Banca in schermata -> lista vuota."""
        reader = BetfairOpenBetsReader(screen_reader=_make_screen_reader(["Aperte", "Non sono presenti scommesse"]))
        result = reader.read_open_bets("device-1")

        assert result.success
        assert result.data == []


# ------------------------------------------------------------------
# 4-5: detection
# ------------------------------------------------------------------

class TestDetectUnregisteredOperations:
    def test_detect_unregistered(self, engine: RulesEngine):
        """Bancata su Betfair, nessuna operazione Netcap -> 1 alert pending."""
        bet = _make_bet()
        result = engine.detect_unregistered_operations([bet], collaboratore_id="08", netcap_operations=[])

        assert result.success
        alerts = result.data
        assert len(alerts) == 1
        assert isinstance(alerts[0], UnregisteredOperationAlert)
        assert alerts[0].status == "pending"
        assert alerts[0].collaboratore_id == "08"
        assert alerts[0].betfair_open_bet["bet_id"] == "1:443519021791"

    def test_detect_already_registered(self, engine: RulesEngine):
        """Bancata con operazione Netcap dal contropuntata corrispondente (±1€) -> nessun alert."""
        bet = _make_bet(lay_stake=461.66)
        netcap_operations = [{"righe": [{"contropuntata": 461.50}]}]
        result = engine.detect_unregistered_operations([bet], collaboratore_id="08", netcap_operations=netcap_operations)

        assert result.success
        assert result.data == []


# ------------------------------------------------------------------
# 6: save
# ------------------------------------------------------------------

class TestSaveUnregisteredAlerts:
    def test_save_alerts_creates_file(self, engine: RulesEngine, tmp_path):
        alert = UnregisteredOperationAlert(
            collaboratore_id="08",
            betfair_open_bet={"bet_id": "1:443519021791"},
            detected_at="2026-09-19T16:00:00",
        )
        result = engine.save_unregistered_alerts([alert], collaboratore_id="08", report_dir=str(tmp_path))

        assert result.success
        file_path = Path(result.data)
        assert file_path.exists()
        assert file_path.name.startswith("unregistered_ops_08_")

        saved = json.loads(file_path.read_text(encoding="utf-8"))
        assert saved["collaboratore_id"] == "08"
        assert len(saved["alerts"]) == 1
        assert saved["alerts"][0]["item_id"].startswith("unreg_08_0_")
        assert saved["alerts"][0]["status"] == "pending"


# ------------------------------------------------------------------
# 7: complete operation (dashboard route)
# ------------------------------------------------------------------

class TestCompleteOperation:
    def test_complete_operation_writes_to_netcap(self):
        filename = "unregistered_ops_test_complete.json"
        alert = {
            "item_id": "unreg_test_0_123",
            "collaboratore_id": "test",
            "betfair_open_bet": {
                "bet_id": "1:443519021791",
                "event": "Gil Vicente - Maritimo",
                "lay_stake": 461.66,
                "lay_odds": 3.50,
                "liability": 1154.15,
            },
            "detected_at": "2026-09-19T16:00:00",
            "status": "pending",
            "bookmaker_id": None,
            "back_stake": None,
            "back_odds": None,
            "tipologia": "Ricorrenti",
        }
        path = _write_alert_report(filename, [alert], collaboratore_id="test")
        try:
            with patch("orchestrator.dashboard.app.AppApiClient") as mock_client_cls:
                mock_instance = MagicMock()
                mock_instance.write_operazione.return_value = Result.ok(
                    WriteResult(success=True, netcap_id=999, message=None)
                )
                mock_instance.add_riga_cascata.return_value = Result.ok(
                    WriteResult(success=True, netcap_id=111, message=None)
                )
                mock_client_cls.return_value = mock_instance
                mock_client_cls.BETFAIR_EXCHANGE_ID = 6

                client = TestClient(app)
                response = client.post("/complete_op/unreg_test_0_123", json={
                    "bookmaker_id": 5,
                    "back_stake": 400.0,
                    "back_odds": 3.60,
                })

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "completed"
            assert body["operazione_id"] == 999
            assert body["riga_id"] == 111
            assert body["linked_existing"] is False

            mock_instance.write_operazione.assert_called_once()
            mock_instance.add_riga_cascata.assert_called_once()
            riga_kwargs = mock_instance.add_riga_cascata.call_args.kwargs
            assert riga_kwargs["operazione_id"] == 999
            assert riga_kwargs["back_stake"] == pytest.approx(400.0)
            assert riga_kwargs["back_odds"] == pytest.approx(3.60)
            assert riga_kwargs["liability"] == pytest.approx(1154.15)
            assert riga_kwargs["lay_odds"] == pytest.approx(3.50)
            assert riga_kwargs["bet_gruppo_id"] is None  # "singola" di default

            updated = json.loads(path.read_text(encoding="utf-8"))
            assert updated["alerts"][0]["status"] == "completed"
        finally:
            path.unlink(missing_ok=True)

    def test_complete_operation_alert_not_found(self):
        with patch("orchestrator.dashboard.app.AppApiClient") as mock_client_cls:
            client = TestClient(app)
            response = client.post("/complete_op/does-not-exist", json={
                "bookmaker_id": 5, "back_stake": 100.0, "back_odds": 2.0,
            })
        assert response.status_code == 404
        mock_client_cls.assert_not_called()

    def test_complete_op_new_cascade(self):
        """op_type='cascata' su nuova operazione -> bet_gruppo_id generato dall'operazione_id."""
        filename = "unregistered_ops_test_cascade.json"
        alert = {
            "item_id": "unreg_test_cascade_0_123",
            "collaboratore_id": "test",
            "betfair_open_bet": {
                "bet_id": "1:443519021791",
                "event": "Gil Vicente - Maritimo",
                "lay_stake": 461.66,
                "lay_odds": 3.50,
                "liability": 1154.15,
            },
            "detected_at": "2026-09-19T16:00:00",
            "status": "pending",
            "bookmaker_id": None,
            "back_stake": None,
            "back_odds": None,
            "tipologia": "Ricorrenti",
        }
        path = _write_alert_report(filename, [alert], collaboratore_id="test")
        try:
            with patch("orchestrator.dashboard.app.AppApiClient") as mock_client_cls:
                mock_instance = MagicMock()
                mock_instance.write_operazione.return_value = Result.ok(
                    WriteResult(success=True, netcap_id=999, message=None)
                )
                mock_instance.add_riga_cascata.return_value = Result.ok(
                    WriteResult(success=True, netcap_id=222, message=None)
                )
                mock_client_cls.return_value = mock_instance

                client = TestClient(app)
                response = client.post("/complete_op/unreg_test_cascade_0_123", json={
                    "bookmaker_id": 5, "back_stake": 400.0, "back_odds": 3.60,
                    "op_type": "cascata", "total_events": 3, "current_event": 1,
                })

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "completed"
            assert body["linked_existing"] is False

            mock_instance.write_operazione.assert_called_once()
            riga_kwargs = mock_instance.add_riga_cascata.call_args.kwargs
            assert riga_kwargs["bet_gruppo_id"].startswith("cascade_999_")
            assert riga_kwargs["note"] == "Cascata evento 1/3"
        finally:
            path.unlink(missing_ok=True)

    def test_complete_op_link_existing(self):
        """existing_op_id fornito -> nessuna nuova operazione, bet_gruppo_id riusato da quella esistente."""
        filename = "unregistered_ops_test_link.json"
        alert = {
            "item_id": "unreg_test_link_0_123",
            "collaboratore_id": "test",
            "betfair_open_bet": {
                "bet_id": "1:443519021792",
                "event": "Gil Vicente - Maritimo",
                "lay_stake": 200.0,
                "lay_odds": 2.10,
                "liability": 500.0,
            },
            "detected_at": "2026-09-19T16:00:00",
            "status": "pending",
            "bookmaker_id": None,
            "back_stake": None,
            "back_odds": None,
            "tipologia": "Ricorrenti",
        }
        path = _write_alert_report(filename, [alert], collaboratore_id="test")
        try:
            with patch("orchestrator.dashboard.app.AppApiClient") as mock_client_cls:
                mock_instance = MagicMock()
                mock_instance.get_open_operations.return_value = Result.ok([
                    {"id": 777, "nome": "Gil Vicente - Maritimo", "stato": "APERTO", "bet_gruppo_id": "cascade_777_111"},
                ])
                mock_instance.add_riga_cascata.return_value = Result.ok(
                    WriteResult(success=True, netcap_id=333, message=None)
                )
                mock_client_cls.return_value = mock_instance

                client = TestClient(app)
                response = client.post("/complete_op/unreg_test_link_0_123", json={
                    "bookmaker_id": 5, "back_stake": 150.0, "back_odds": 2.05,
                    "existing_op_id": 777,
                })

            assert response.status_code == 200
            body = response.json()
            assert body["operazione_id"] == 777
            assert body["linked_existing"] is True

            mock_instance.write_operazione.assert_not_called()
            riga_kwargs = mock_instance.add_riga_cascata.call_args.kwargs
            assert riga_kwargs["operazione_id"] == 777
            assert riga_kwargs["bet_gruppo_id"] == "cascade_777_111"
        finally:
            path.unlink(missing_ok=True)

    def test_dashboard_open_operations_route(self):
        with patch("orchestrator.dashboard.app.AppApiClient") as mock_client_cls:
            mock_instance = MagicMock()
            mock_instance.get_open_operations.return_value = Result.ok([
                {"id": 119, "nome": "Gil Vicente - Maritimo", "stato": "APERTO", "bet_gruppo_id": None},
            ])
            mock_client_cls.return_value = mock_instance

            client = TestClient(app)
            response = client.get("/open_operations/08")

        assert response.status_code == 200
        assert response.json() == [
            {"id": 119, "nome": "Gil Vicente - Maritimo", "stato": "APERTO", "bet_gruppo_id": None},
        ]
        mock_instance.get_open_operations.assert_called_once_with("08")


# ------------------------------------------------------------------
# 8-9: AppApiClient — cascata / operazioni aperte
# ------------------------------------------------------------------

class TestAddRigaCascata:
    def test_add_riga_cascata_payload(self):
        """Verifica lo schema reale (puntata_e_bancata, niente quota_*) e il bet_gruppo_id."""
        client = AppApiClient(session_token="x")
        client._post_json = MagicMock(return_value=Result.ok({"id": 555}))

        result = client.add_riga_cascata(
            operazione_id=42,
            collaboratore_id="08",
            bookmaker_id=5,
            back_stake=400.0,
            back_odds=3.60,
            liability=1154.15,
            lay_odds=3.50,
            bet_gruppo_id="cascade_42_123",
        )

        assert result.success
        assert result.data.success
        assert result.data.netcap_id == 555

        client._post_json.assert_called_once()
        path, body = client._post_json.call_args.args
        assert path == "/operazioni/42/righe"
        assert body["tipo_riga"] == "puntata_e_bancata"
        assert body["parent_id"] is None
        assert body["banco_collaboratore_id"] is None
        assert body["punta"] == pytest.approx(400.0)
        assert body["contropuntata"] == pytest.approx(1154.15)  # liability, non lay_stake
        assert body["bet_gruppo_id"] == "cascade_42_123"
        assert "quota_punta" not in body
        assert "quota_contropuntata" not in body


class TestGetOpenOperations:
    def test_get_open_operations_maps_shape(self):
        """Wrapper su get_operazioni_aperte: estrae bet_gruppo_id dalle righe annidate."""
        client = AppApiClient(session_token="x")
        client.get_operazioni_aperte = MagicMock(return_value=Result.ok([
            {"id": 119, "nome": "Gil Vicente - Maritimo", "stato": "APERTO",
             "righe": [{"bet_gruppo_id": "cascade_119_xxx"}]},
        ]))

        result = client.get_open_operations("08")

        assert result.success
        assert result.data == [{
            "id": 119, "nome": "Gil Vicente - Maritimo", "stato": "APERTO",
            "bet_gruppo_id": "cascade_119_xxx",
        }]
