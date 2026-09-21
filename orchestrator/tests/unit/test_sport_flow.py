from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from orchestrator.core.result import Result
from orchestrator.dashboard.app import app
from orchestrator.flows.sport_flow import ExecutionResult, SignalFlowResult, SportFlow
from orchestrator.flows.sport_flow_handler import SportFlowHandler
from orchestrator.modules.app_api_client.interface import WriteResult
from orchestrator.modules.cascade_engine.interface import CascadeState
from orchestrator.modules.multiplier_engine.interface import CascadeCalculation, CascadeEventCalc
from orchestrator.modules.rag_system.interface import TCVerification
from orchestrator.modules.screen_reader.interface import BetStatus
from orchestrator.modules.signal_parser.interface import Signal
from orchestrator.modules.telegram_notifier.notifier import TelegramNotifier
from orchestrator.modules.touch_injector.interface import BackBetResult


# ----------------------------------------------------------------------
# helper factories
# ----------------------------------------------------------------------

def _signal(category: str = "sport", book: str = "SISAL", telegram_message_id: str = "tm1",
            strategy: dict = None) -> Signal:
    return Signal(
        telegram_message_id=telegram_message_id,
        book=book,
        category=category,
        bonus_type="ricorrente",
        difficulty="facile",
        strategy=strategy or {},
        bonus={},
        deadline="2026-09-21T00:00:00",
        priority="alta",
        expected_profit="alto",
    )


def _tc_verification(verified: bool = True, discrepancies: list = None) -> TCVerification:
    return TCVerification(verified=verified, discrepancies=discrepancies or [], matched_docs=["doc1"])


def _event(index: int = 0, lay_stake: float = 48.0, liability: float = 52.0,
           lay_odds: float = 2.1) -> CascadeEventCalc:
    return CascadeEventCalc(
        index=index, lay_stake=lay_stake, liability=liability, lay_odds=lay_odds,
        profit_if_won=2.0, profit_if_all_lost=2.0,
    )


def _cascade_calc(events: list = None, back_stake: float = 50.0, back_odds: list = None,
                   lay_odds: list = None) -> CascadeCalculation:
    return CascadeCalculation(
        events=events if events is not None else [_event()],
        profit_per_scenario={"all_lost": 2.0}, total_liability=52.0, is_valid=True,
        back_stake=back_stake, back_odds=back_odds or [2.0], lay_odds=lay_odds or [2.1], commission=0.045,
    )


def _write_result(success: bool = True, netcap_id: int = 100, message: str = None) -> WriteResult:
    return WriteResult(success=success, netcap_id=netcap_id, message=message)


def _back_bet_result(confirmed: bool = True, bet_id: str = "B1", actual_stake: float = 50.0) -> BackBetResult:
    return BackBetResult(confirmed=confirmed, bet_id=bet_id, actual_stake=actual_stake)


def _cascade_state(betfair_bet_ids: list = None, state: str = "event_1_waiting", device_id: str = "dev-1",
                    lay_odds: list = None, lay_stakes: list = None, liabilities: list = None,
                    back_odds: list = None, back_stake: float = 50.0) -> CascadeState:
    return CascadeState(
        cascade_id="casc-1", operation_id="op-1", identity_id="01", device_id=device_id,
        markets=["1.234"], current_event_index=0, state=state,
        back_stake=back_stake, back_odds=back_odds or [2.0],
        lay_odds=lay_odds or [2.1], lay_stakes=lay_stakes or [48.0], liabilities=liabilities or [52.0],
        commission_rate=0.045, betfair_bet_ids=betfair_bet_ids or ["BF-1"],
        retry_count=0, max_retries=3, next_check_at="2026-09-20T00:01:00", last_check_at="2026-09-20T00:00:00",
    )


def _bet_status(status: str = "settled_won", lay_odds: float = 2.1, confidence: float = 0.9) -> BetStatus:
    return BetStatus(status=status, lay_odds=lay_odds, confidence=confidence)


@pytest.fixture
def flow() -> SportFlow:
    config = {
        "identities": [
            {"id": "01", "device_id": "dev-1", "books": [
                {"bookmaker_id": 42, "name": "SISAL", "is_exchange": False},
                {"bookmaker_id": 6, "name": "BETFAIR", "is_exchange": True},
            ]},
            {"id": "02", "device_id": "dev-2", "books": [
                {"bookmaker_id": 42, "name": "SISAL", "is_exchange": False},
            ]},
        ]
    }
    f = SportFlow(
        signal_parser=MagicMock(),
        rag_system=MagicMock(),
        multiplier_engine=MagicMock(),
        touch_injector=MagicMock(),
        cascade_engine=MagicMock(),
        app_api_client=MagicMock(),
        screen_reader=MagicMock(),
        device_manager=MagicMock(),
        config=config,
    )
    f.device_manager.get_device_for_identity.side_effect = lambda identity_id, cfg: {
        "01": "dev-1", "02": "dev-2",
    }.get(identity_id)
    f.device_manager.acquire_lock.return_value = True
    return f


# ----------------------------------------------------------------------
# process_signal
# ----------------------------------------------------------------------

class TestProcessSignal:
    def test_process_signal_sport_success(self, flow):
        """Segnale sport, T&C ok -> calcola la cascata con MultiplierEngine."""
        signal = _signal(category="sport", strategy={
            "back_stake": 50.0, "back_odds": [2.0], "lay_odds": [2.1], "identity_id": "01",
        })
        flow.signal_parser.parse.return_value = Result.ok(signal)
        flow.rag_system.verify_tc.return_value = Result.ok(_tc_verification(verified=True))
        calc = _cascade_calc()
        flow.multiplier_engine.calculate_cascade.return_value = Result.ok(calc)

        result = flow.process_signal("raw text", "tm1")

        assert result.success
        assert result.data.status == "pending_hitl"
        assert result.data.cascade_calc == calc
        assert result.data.identity_ids_available == ["01"]
        flow.multiplier_engine.calculate_cascade.assert_called_once_with(
            back_stake=50.0, back_odds=[2.0], lay_odds=[2.1], commission=0.045,
        )

    def test_process_signal_tc_discrepancy(self, flow):
        """RAGSystem trova discrepanze T&C -> status=tc_discrepancy, nessun calcolo."""
        signal = _signal(category="sport", strategy={"back_stake": 50.0})
        flow.signal_parser.parse.return_value = Result.ok(signal)
        flow.rag_system.verify_tc.return_value = Result.ok(
            _tc_verification(verified=False, discrepancies=["stake minimo non rispettato"])
        )

        result = flow.process_signal("raw text", "tm1")

        assert result.success
        assert result.data.status == "tc_discrepancy"
        assert result.data.cascade_calc is None
        flow.multiplier_engine.calculate_cascade.assert_not_called()

    def test_process_signal_casino(self, flow):
        """Segnale casino -> cascade_calc=None, MultiplierEngine non chiamato."""
        signal = _signal(category="casino", strategy={})
        flow.signal_parser.parse.return_value = Result.ok(signal)
        flow.rag_system.verify_tc.return_value = Result.ok(_tc_verification(verified=True))

        result = flow.process_signal("raw text", "tm1")

        assert result.success
        assert result.data.status == "pending_hitl"
        assert result.data.cascade_calc is None
        flow.multiplier_engine.calculate_cascade.assert_not_called()


# ----------------------------------------------------------------------
# execute_operation
# ----------------------------------------------------------------------

class TestExecuteOperation:
    def test_execute_operation_success(self, flow):
        """TouchInjector + AppApiClient ok -> operazione creata su Netcap."""
        signal = _signal(category="sport", book="SISAL", strategy={
            "back_stake": 50.0, "back_odds": [2.0], "lay_odds": [2.1],
        })
        calc = _cascade_calc()
        flow_result = SignalFlowResult(
            signal=signal, tc_verification=_tc_verification(), cascade_calc=calc,
            identity_ids_available=["01"], status="pending_hitl", hitl_item_id="sport_tm1_123",
        )

        flow.touch_injector.place_back_bet.return_value = Result.ok(_back_bet_result())
        flow.cascade_engine.start_cascade.return_value = Result.ok("casc-1")
        flow.app_api_client.write_operazione.return_value = Result.ok(_write_result(netcap_id=200))
        flow.app_api_client.add_riga_cascata.return_value = Result.ok(_write_result(netcap_id=300))

        with patch.object(SportFlowHandler, "get_pending_flow", return_value=flow_result):
            result = flow.execute_operation("sport_tm1_123", ["01"])

        assert result.success
        assert result.data.identities_executed == ["01"]
        assert result.data.identities_failed == []
        assert result.data.cascade_started is True
        assert result.data.operation_id == 200
        flow.touch_injector.place_back_bet.assert_called_once()
        flow.cascade_engine.start_cascade.assert_called_once()
        flow.app_api_client.write_operazione.assert_called_once()
        flow.app_api_client.add_riga_cascata.assert_called_once()
        flow.device_manager.release_lock.assert_called_once_with("dev-1")

    def test_execute_operation_partial_failure(self, flow):
        """1 identita' fallisce (back bet non confermato), 1 ok -> non blocca."""
        signal = _signal(category="sport", book="SISAL", strategy={
            "back_stake": 50.0, "back_odds": [2.0], "lay_odds": [2.1],
        })
        calc = _cascade_calc()
        flow_result = SignalFlowResult(
            signal=signal, tc_verification=_tc_verification(), cascade_calc=calc,
            identity_ids_available=["01", "02"], status="pending_hitl", hitl_item_id="sport_tm1_123",
        )

        flow.touch_injector.place_back_bet.side_effect = [
            Result.ok(_back_bet_result(confirmed=False)),
            Result.ok(_back_bet_result(confirmed=True)),
        ]
        flow.cascade_engine.start_cascade.return_value = Result.ok("casc-1")
        flow.app_api_client.write_operazione.return_value = Result.ok(_write_result(netcap_id=200))
        flow.app_api_client.add_riga_cascata.return_value = Result.ok(_write_result(netcap_id=300))

        with patch.object(SportFlowHandler, "get_pending_flow", return_value=flow_result):
            result = flow.execute_operation("sport_tm1_123", ["01", "02"])

        assert result.success
        assert result.data.identities_failed == ["01"]
        assert result.data.identities_executed == ["02"]
        assert len(result.data.errors) == 1


# ----------------------------------------------------------------------
# monitor_cascade
# ----------------------------------------------------------------------

class TestMonitorCascade:
    def test_monitor_cascade_event_closed(self, flow):
        """CascadeEngine avanza (nuova bancata gia' piazzata da solo) -> viene registrata su Netcap."""
        flow._cascade_state[200] = {
            "cascade_id": "casc-1", "bet_gruppo_id": "cascade_x_01",
            "identity_id": "01", "bookmaker_id": 6, "registered_events": 1,
        }
        state = _cascade_state(
            betfair_bet_ids=["BF-1", "BF-2"], state="event_2_waiting",
            lay_odds=[2.1, 1.8], lay_stakes=[48.0, 50.0], liabilities=[52.0, 55.0],
        )
        flow.cascade_engine.check_and_advance.return_value = Result.ok(state)
        flow.app_api_client.add_riga_cascata.return_value = Result.ok(_write_result())

        result = flow.monitor_cascade(200)

        assert result.success
        assert result.data is False
        flow.app_api_client.add_riga_cascata.assert_called_once()
        assert flow._cascade_state[200]["registered_events"] == 2
        flow.app_api_client.close_bet_gruppo_outcome.assert_not_called()

    def test_monitor_cascade_all_closed(self, flow):
        """Cascata completa -> chiude il gruppo con outcome PUT e chiude l'operazione."""
        flow._cascade_state[200] = {
            "cascade_id": "casc-1", "bet_gruppo_id": "cascade_x_01",
            "identity_id": "01", "bookmaker_id": 6, "registered_events": 1,
        }
        state = _cascade_state(betfair_bet_ids=["BF-1"], state="completed")
        flow.cascade_engine.check_and_advance.return_value = Result.ok(state)
        flow.screen_reader.read_bet_status.return_value = Result.ok(_bet_status(status="settled_lost"))
        flow.app_api_client.close_bet_gruppo_outcome.return_value = Result.ok(True)
        flow.app_api_client.close_operazione.return_value = Result.ok(True)

        result = flow.monitor_cascade(200)

        assert result.success
        assert result.data is True
        flow.app_api_client.close_bet_gruppo_outcome.assert_called_once_with("cascade_x_01", True, False)
        flow.app_api_client.close_operazione.assert_called_once_with(200)


# ----------------------------------------------------------------------
# SportFlowHandler (persistenza HITL)
# ----------------------------------------------------------------------

class TestSportFlowHandler:
    def test_save_load_pending_flow(self, tmp_path):
        """Salva e ricarica un flow pending dal filesystem."""
        signal = _signal(category="sport", strategy={
            "back_stake": 50.0, "back_odds": [2.0], "lay_odds": [2.1],
        })
        calc = _cascade_calc()
        flow_result = SignalFlowResult(
            signal=signal, tc_verification=_tc_verification(), cascade_calc=calc,
            identity_ids_available=["01"], status="pending_hitl",
        )
        handler = SportFlowHandler()
        report_dir = str(tmp_path)

        with patch.object(TelegramNotifier, "notify_sport_flow", return_value=Result.ok(True)):
            save_result = handler.save_pending_flow(flow_result, report_dir=report_dir)
        assert save_result.success
        item_id = save_result.data

        pending = handler.load_pending_flows(report_dir=report_dir)
        assert len(pending) == 1
        assert pending[0]["item_id"] == item_id

        reloaded = handler.get_pending_flow(item_id, report_dir=report_dir)
        assert reloaded is not None
        assert reloaded.signal.telegram_message_id == signal.telegram_message_id
        assert reloaded.cascade_calc.events[0].lay_stake == calc.events[0].lay_stake

    def test_save_pending_flow_sends_telegram_notification(self, tmp_path):
        """save_pending_flow notifica Raffaele via Telegram dopo il salvataggio."""
        signal = _signal(category="sport", book="STANLEYBET")
        flow_result = SignalFlowResult(
            signal=signal, tc_verification=_tc_verification(), cascade_calc=None,
            identity_ids_available=["01", "02"], status="pending_hitl",
        )
        handler = SportFlowHandler()

        with patch.object(TelegramNotifier, "notify_sport_flow", return_value=Result.ok(True)) as mock_notify:
            save_result = handler.save_pending_flow(flow_result, report_dir=str(tmp_path))

        assert save_result.success
        mock_notify.assert_called_once()
        _args, kwargs = mock_notify.call_args
        assert kwargs["book"] == "STANLEYBET"
        assert kwargs["identities_ready"] == 2
        assert save_result.data in kwargs["dashboard_url"]

    def test_save_pending_flow_survives_telegram_failure(self, tmp_path):
        """Un fallimento della notifica Telegram non deve far fallire il salvataggio."""
        signal = _signal(category="sport")
        flow_result = SignalFlowResult(
            signal=signal, tc_verification=_tc_verification(), cascade_calc=None,
            identity_ids_available=["01"], status="pending_hitl",
        )
        handler = SportFlowHandler()

        with patch.object(
            TelegramNotifier, "notify_sport_flow",
            return_value=Result.fail("errore rete", "TELEGRAM_SEND_ERROR"),
        ):
            save_result = handler.save_pending_flow(flow_result, report_dir=str(tmp_path))

        assert save_result.success
        assert handler.load_pending_flows(report_dir=str(tmp_path))


# ----------------------------------------------------------------------
# route dashboard
# ----------------------------------------------------------------------

class TestDashboardSportFlowRoutes:
    def test_dashboard_pending_flows_route(self):
        """GET /pending_sport_flows restituisce i flow pending via SportFlowHandler."""
        with patch.object(SportFlowHandler, "load_pending_flows", return_value=[{"item_id": "sport_tm1_1"}]):
            client = TestClient(app)
            response = client.get("/pending_sport_flows")

        assert response.status_code == 200
        assert response.json() == [{"item_id": "sport_tm1_1"}]

    def test_dashboard_confirm_flow_route(self):
        """POST /confirm_sport_flow/{id} chiama execute_operation su tutte le identita' selezionate."""
        exec_result = ExecutionResult(
            operation_id=200, bet_gruppo_id="cascade_x_01",
            identities_executed=["01"], identities_failed=[], cascade_started=True, errors=[],
        )
        with patch("orchestrator.dashboard.app._build_sport_flow") as mock_build, \
             patch.object(SportFlowHandler, "update_flow_status", return_value=True) as mock_update:
            mock_flow = MagicMock()
            mock_flow.execute_operation.return_value = Result.ok(exec_result)
            mock_build.return_value = mock_flow

            client = TestClient(app)
            response = client.post("/confirm_sport_flow/sport_tm1_123", json={"identity_ids": ["01"]})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "executed"
        assert body["result"]["operation_id"] == 200
        mock_flow.execute_operation.assert_called_once_with("sport_tm1_123", ["01"])
        mock_update.assert_called_once_with("sport_tm1_123", "executed")
