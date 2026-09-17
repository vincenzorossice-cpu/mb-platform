import json
from unittest.mock import MagicMock

import pytest

from orchestrator.core.config import load_config
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.result import Result

from orchestrator.modules.app_api_client.interface import ExpectedBalance, WriteResult
from orchestrator.modules.movement_importer.interface import Movement
from orchestrator.modules.multiplier_engine.interface import CascadeCalculation
from orchestrator.modules.opsec.interface import CorrelationCheck
from orchestrator.modules.rules_engine.interface import Rectification, ReconciliationResult
from orchestrator.modules.screen_reader.interface import BalanceReading
from orchestrator.modules.signal_parser.interface import Signal


# ------------------------------------------------------------------
# helpers / fixtures
# ------------------------------------------------------------------

def _balance_reading(raw_balance: float = 100.0, confidence: float = 0.95) -> BalanceReading:
    return BalanceReading(
        raw_balance=raw_balance, open_bets=0.0, real_bonus=0.0,
        wagering_bonus=None, read_method="ocr", confidence=confidence,
    )


def _reconciliation_result(action: str, rectification: Rectification = None) -> ReconciliationResult:
    return ReconciliationResult(
        adjusted_balance=100.0, expected_balance=100.0, difference=0.0,
        column="BOOK", bonus_note=None, action=action,
        rectification=rectification, rules_applied=[],
    )


def _signal(strategy: dict = None) -> Signal:
    return Signal(
        telegram_message_id="msg-1", book="BETFAIR", category="sport",
        bonus_type="ricorrente", difficulty="facile",
        strategy=strategy or {"back_stake": 100.0, "back_odds": [2.0], "lay_odds": [2.1], "commission": 0.045},
        bonus={}, deadline="2026-09-20T00:00:00", priority="alta",
        expected_profit="medio", tc_verified=True,
    )


def _movement(importo: float = 50.0) -> Movement:
    return Movement(
        tipo="deposito", importo=importo, conto_id=None, bookmaker_id=None,
        bookmaker_nome=None, data="2026-09-17", collaboratore_id="01",
        descrizione="PayPal", fonte="paypal_ocr", confidence=0.9,
    )


@pytest.fixture
def orch(tmp_path) -> Orchestrator:
    o = Orchestrator(config_path=str(tmp_path / "nonexistent.json"))

    o.device_manager = MagicMock()
    o.device_manager.acquire_lock.return_value = True
    o.device_manager.get_device_for_identity.return_value = "dev-1"

    o.screen_reader = MagicMock()
    o.touch_injector = MagicMock()
    o.multiplier_engine = MagicMock()
    o.rules_engine = MagicMock()
    o.app_api_client = MagicMock()
    o.movement_importer = MagicMock()
    o.signal_parser = MagicMock()
    o.cascade_engine = MagicMock()

    o.opsec = MagicMock()
    o.opsec.check_correlation_risk.return_value = Result.ok(
        CorrelationCheck(risk_level="low", warnings=[], blocked=False)
    )

    return o


def _one_identity_config(bookmaker_id: int = 6, name: str = "BETFAIR", is_exchange: bool = True) -> dict:
    return {
        "identities": [{
            "id": "01", "name": "Test", "device_id": "dev-1",
            "books": [{"bookmaker_id": bookmaker_id, "name": name, "is_exchange": is_exchange}],
        }],
    }


# ------------------------------------------------------------------
# config
# ------------------------------------------------------------------

class TestLoadConfig:
    def test_load_config(self, tmp_path):
        """File JSON valido con 2 identità -> config caricato correttamente."""
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "identities": [{"id": "01"}, {"id": "02"}], "netcap": {}, "schedule": {},
        }))

        config = load_config(str(config_path))

        assert len(config["identities"]) == 2

    def test_load_config_missing(self, tmp_path):
        """File non esistente -> config vuoto di default."""
        config = load_config(str(tmp_path / "missing.json"))

        assert config == {"identities": [], "netcap": {}, "schedule": {}}


# ------------------------------------------------------------------
# run_daily_riconciliazione
# ------------------------------------------------------------------

class TestRunDailyRiconciliazione:
    def test_run_daily_riconciliazione_success(self, orch):
        """1 ID, 1 book, saldo allineato -> no_adjustment, nessun errore."""
        orch.config = _one_identity_config()
        orch.app_api_client.read_expected_balance.return_value = Result.ok([
            ExpectedBalance(collaboratore_id="01", bookmaker_id=6, bookmaker_nome="BETFAIR",
                             saldo_atteso=100.0, importo_impegnato=0.0, is_exchange=True),
        ])
        orch.screen_reader.read_balance.return_value = Result.ok(_balance_reading())
        orch.rules_engine.reconcile.return_value = Result.ok(_reconciliation_result("no_adjustment"))

        result = orch.run_daily_riconciliazione()

        assert result.success
        assert result.data == {"total": 1, "adjusted": 0, "no_adjustment": 1, "hitl": 0, "errors": []}

    def test_run_daily_riconciliazione_adjust(self, orch):
        """Differenza > soglia -> action=adjust, dry-run: write_rettifica NON chiamato."""
        orch.config = _one_identity_config()
        orch.app_api_client.read_expected_balance.return_value = Result.ok([
            ExpectedBalance(collaboratore_id="01", bookmaker_id=6, bookmaker_nome="BETFAIR",
                             saldo_atteso=100.0, importo_impegnato=0.0, is_exchange=True),
        ])
        orch.screen_reader.read_balance.return_value = Result.ok(_balance_reading(raw_balance=105.0))
        rectification = Rectification(amount=5.0, column="BOOK", reason="differenza saldo")
        orch.rules_engine.reconcile.return_value = Result.ok(_reconciliation_result("adjust", rectification))

        result = orch.run_daily_riconciliazione(live=False)

        assert result.success
        assert result.data["adjusted"] == 1
        orch.app_api_client.write_rettifica.assert_not_called()

    def test_run_daily_riconciliazione_adjust_live(self, orch):
        """live=True, action=adjust -> write_rettifica chiamato."""
        orch.config = _one_identity_config()
        orch.app_api_client.read_expected_balance.return_value = Result.ok([
            ExpectedBalance(collaboratore_id="01", bookmaker_id=6, bookmaker_nome="BETFAIR",
                             saldo_atteso=100.0, importo_impegnato=0.0, is_exchange=True),
        ])
        orch.screen_reader.read_balance.return_value = Result.ok(_balance_reading(raw_balance=105.0))
        rectification = Rectification(amount=5.0, column="BOOK", reason="differenza saldo")
        orch.rules_engine.reconcile.return_value = Result.ok(_reconciliation_result("adjust", rectification))
        orch.app_api_client.write_rettifica.return_value = Result.ok(WriteResult(success=True, netcap_id=1, message=None))

        result = orch.run_daily_riconciliazione(live=True)

        assert result.success
        assert result.data["adjusted"] == 1
        orch.app_api_client.write_rettifica.assert_called_once_with(rectification, "01", 6)

    def test_run_daily_riconciliazione_hitl(self, orch):
        """confidence bassa -> action=hitl_required, aggiunto a coda HITL."""
        orch.config = _one_identity_config()
        orch.app_api_client.read_expected_balance.return_value = Result.ok([
            ExpectedBalance(collaboratore_id="01", bookmaker_id=6, bookmaker_nome="BETFAIR",
                             saldo_atteso=100.0, importo_impegnato=0.0, is_exchange=True),
        ])
        orch.screen_reader.read_balance.return_value = Result.ok(_balance_reading(confidence=0.5))
        orch.rules_engine.reconcile.return_value = Result.ok(_reconciliation_result("hitl_required"))

        result = orch.run_daily_riconciliazione()

        assert result.success
        assert result.data["hitl"] == 1
        assert len(orch.get_hitl_queue()) == 1
        assert orch.get_hitl_queue()[0]["type"] == "reconciliation"

    def test_run_daily_riconciliazione_error(self, orch):
        """ScreenReader fallisce -> errore registrato, run non si interrompe."""
        orch.config = _one_identity_config()
        orch.app_api_client.read_expected_balance.return_value = Result.ok([
            ExpectedBalance(collaboratore_id="01", bookmaker_id=6, bookmaker_nome="BETFAIR",
                             saldo_atteso=100.0, importo_impegnato=0.0, is_exchange=True),
        ])
        orch.screen_reader.read_balance.return_value = Result.fail("OCR fallita", "OCR_LOW_CONFIDENCE")

        result = orch.run_daily_riconciliazione()

        assert result.success
        assert result.data["total"] == 1
        assert result.data["adjusted"] == 0
        assert result.data["no_adjustment"] == 0
        assert len(result.data["errors"]) == 1
        orch.rules_engine.reconcile.assert_not_called()

    def test_run_daily_riconciliazione_multiple_ids(self, orch):
        """2 ID x 2 book -> 4 riconciliazioni, tutte processate."""
        orch.config = {
            "identities": [
                {
                    "id": "01", "name": "A", "device_id": "dev-1",
                    "books": [
                        {"bookmaker_id": 6, "name": "BETFAIR", "is_exchange": True},
                        {"bookmaker_id": 42, "name": "SISAL", "is_exchange": False},
                    ],
                },
                {
                    "id": "02", "name": "B", "device_id": "dev-2",
                    "books": [
                        {"bookmaker_id": 6, "name": "BETFAIR", "is_exchange": True},
                        {"bookmaker_id": 42, "name": "SISAL", "is_exchange": False},
                    ],
                },
            ],
        }
        orch.app_api_client.read_expected_balance.return_value = Result.ok([
            ExpectedBalance(collaboratore_id="x", bookmaker_id=6, bookmaker_nome="BETFAIR",
                             saldo_atteso=100.0, importo_impegnato=0.0, is_exchange=True),
            ExpectedBalance(collaboratore_id="x", bookmaker_id=42, bookmaker_nome="SISAL",
                             saldo_atteso=50.0, importo_impegnato=0.0, is_exchange=False),
        ])
        orch.screen_reader.read_balance.return_value = Result.ok(_balance_reading())
        orch.rules_engine.reconcile.return_value = Result.ok(_reconciliation_result("no_adjustment"))

        result = orch.run_daily_riconciliazione()

        assert result.success
        assert result.data["total"] == 4
        assert result.data["no_adjustment"] == 4
        assert result.data["errors"] == []
        assert orch.rules_engine.reconcile.call_count == 4

    def test_run_daily_riconciliazione_opsec_blocked(self, orch):
        """OpSec segnala rischio alto -> skip, HITL, nessuna lettura saldo."""
        orch.config = _one_identity_config()
        orch.app_api_client.read_expected_balance.return_value = Result.ok([
            ExpectedBalance(collaboratore_id="01", bookmaker_id=6, bookmaker_nome="BETFAIR",
                             saldo_atteso=100.0, importo_impegnato=0.0, is_exchange=True),
        ])
        orch.opsec.check_correlation_risk.return_value = Result.ok(
            CorrelationCheck(risk_level="high", warnings=["troppe operazioni"], blocked=True)
        )

        result = orch.run_daily_riconciliazione()

        assert result.success
        assert len(orch.get_hitl_queue()) == 1
        orch.screen_reader.read_balance.assert_not_called()


# ------------------------------------------------------------------
# run_import_movimenti
# ------------------------------------------------------------------

class TestRunImportMovimenti:
    def test_run_import_movimenti(self, orch):
        """MovementImporter trova 2 movimenti -> save_pending_movements chiamato."""
        movements = [_movement(50.0), _movement(30.0)]
        orch.movement_importer.import_paypal.return_value = Result.ok(movements)
        orch.movement_importer.save_pending_movements.return_value = Result.ok("report.json")

        result = orch.run_import_movimenti("dev-1", "01")

        assert result.success
        assert result.data["movements_found"] == 2
        assert result.data["movements_saved"] == 2
        orch.movement_importer.save_pending_movements.assert_called_once_with(movements, "01")
        orch.device_manager.acquire_lock.assert_called_once()
        orch.device_manager.release_lock.assert_called_once_with("dev-1")

    def test_run_import_movimenti_device_busy(self, orch):
        """Lock non acquisibile -> DEVICE_BUSY, import non tentato."""
        orch.device_manager.acquire_lock.return_value = False

        result = orch.run_import_movimenti("dev-1", "01")

        assert not result.success
        assert result.error_code == "DEVICE_BUSY"
        orch.movement_importer.import_paypal.assert_not_called()


# ------------------------------------------------------------------
# process_signal
# ------------------------------------------------------------------

class TestProcessSignal:
    def test_process_signal(self, orch):
        """Pipeline completa: parse -> opsec ok -> calcolo cascata, nessun piazzamento reale."""
        orch.signal_parser.parse_and_verify.return_value = Result.ok(_signal())
        calculation = CascadeCalculation(events=[], profit_per_scenario={}, total_liability=10.0, is_valid=True)
        orch.multiplier_engine.calculate_cascade.return_value = Result.ok(calculation)

        result = orch.process_signal("raw signal text", "msg-1")

        assert result.success
        assert result.data["signal_parsed"] is True
        assert result.data["tc_verified"] is True
        assert result.data["calculation"] == calculation
        assert result.data["cascade_started"] is False
        orch.cascade_engine.start_cascade.assert_not_called()

    def test_process_signal_opsec_blocked(self, orch):
        """OpSec blocca -> HITL, nessun calcolo cascata."""
        orch.signal_parser.parse_and_verify.return_value = Result.ok(_signal())
        orch.opsec.check_correlation_risk.return_value = Result.ok(
            CorrelationCheck(risk_level="high", warnings=["rischio correlation"], blocked=True)
        )

        result = orch.process_signal("raw signal text", "msg-1")

        assert result.success
        assert result.data["cascade_started"] is False
        assert result.data["calculation"] is None
        assert len(orch.get_hitl_queue()) == 1
        orch.multiplier_engine.calculate_cascade.assert_not_called()


# ------------------------------------------------------------------
# run_health_check
# ------------------------------------------------------------------

class TestRunHealthCheck:
    def test_run_health_check(self, orch):
        """2/3 telefoni online, Netcap raggiungibile -> report coerente."""
        orch.device_manager.heartbeat_all.return_value = {
            "dev-1": {"online": True, "battery": 80},
            "dev-2": {"online": False, "battery": None},
            "dev-3": {"online": True, "battery": 50},
        }
        orch.app_api_client.read_book_status.return_value = Result.ok([])

        report = orch.run_health_check()

        assert report["devices_online"] == 2
        assert report["devices_total"] == 3
        assert report["netcap_reachable"] is True
        assert report["hitl_pending"] == 0

    def test_run_health_check_netcap_unreachable(self, orch):
        """Netcap irraggiungibile -> netcap_reachable=False."""
        orch.device_manager.heartbeat_all.return_value = {}
        orch.app_api_client.read_book_status.return_value = Result.fail("Connessione fallita", "NETCAP_UNREACHABLE")

        report = orch.run_health_check()

        assert report["netcap_reachable"] is False


# ------------------------------------------------------------------
# HITL queue
# ------------------------------------------------------------------

class TestHitlQueue:
    def test_hitl_queue(self, orch):
        orch.add_hitl_item("type1", {"a": 1})
        orch.add_hitl_item("type2", {"b": 2}, severity="critical")

        queue = orch.get_hitl_queue()

        assert len(queue) == 2
        assert queue[0]["id"] == "hitl_1"
        assert queue[0]["type"] == "type1"
        assert queue[1]["id"] == "hitl_2"
        assert queue[1]["severity"] == "critical"


# ------------------------------------------------------------------
# run_full_cycle
# ------------------------------------------------------------------

class TestRunFullCycle:
    def test_run_full_cycle(self, orch):
        """Chiama health check poi riconciliazione, in quest'ordine."""
        orch.run_health_check = MagicMock(return_value={"devices_online": 1, "devices_total": 1,
                                                          "netcap_reachable": True, "hitl_pending": 0})
        orch.run_daily_riconciliazione = MagicMock(
            return_value=Result.ok({"total": 1, "adjusted": 0, "no_adjustment": 1, "hitl": 0, "errors": []})
        )

        report = orch.run_full_cycle(live=True)

        orch.run_health_check.assert_called_once()
        orch.run_daily_riconciliazione.assert_called_once_with(live=True)
        assert report["health"]["devices_online"] == 1
        assert report["riconciliazione"]["no_adjustment"] == 1
