import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.flows.casino_flow import CasinoFlow, CasinoPhaseResult, CasinoResult
from orchestrator.flows.casino_orchestrator import CasinoOrchestrator, CasinoResultStore
from orchestrator.modules.app_api_client.interface import WriteResult
from orchestrator.modules.signal_parser.interface import Signal


def _signal(**overrides):
    defaults = dict(
        telegram_message_id="42", book="Sisal", category="casino", bonus_type="ricorrente", difficulty="facile",
        strategy={}, bonus={}, deadline="2026-12-31", priority="alta", expected_profit="alto", status="tc_verified",
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _casino_result(profit=1.0):
    phase = CasinoPhaseResult(
        phase="real", slot_name="Slot", initial_balance=100.0, final_balance=100.0 + profit, target_amount=30.0,
        played_amount=30.0, spins_done=300, spins_needed=300, completed=True,
    )
    return CasinoResult(
        book="Sisal", collaboratore_id="c1", promo_name="Promo", phases=[phase], total_profit=profit,
        initial_balance=100.0, final_balance=100.0 + profit, completed=True,
    )


@pytest.fixture
def parts(tmp_path):
    navigator = MagicMock()
    navigator.navigate.return_value = Result.ok(True)
    flow = MagicMock(spec=CasinoFlow)
    flow.slot_opener = None
    flow.execute_promo.return_value = Result.ok(_casino_result())
    store = CasinoResultStore(report_dir=str(tmp_path))
    orchestrator = CasinoOrchestrator(navigator, flow, result_store=store)
    return orchestrator, navigator, flow, tmp_path


PROMO = {"promo_name": "Promo", "slot_real": "Slot"}


class TestValidation:
    def test_a_non_casino_signal_is_rejected(self, parts):
        orchestrator, navigator, flow, _ = parts

        result = orchestrator.run(_signal(category="sport"), PROMO, "dev", "c1", "raffaele")

        assert not result.success and result.error_code == "SIGNAL_NOT_CASINO"
        navigator.navigate.assert_not_called()

    @pytest.mark.parametrize("confirmed_by", ["", "   ", None])
    def test_an_unconfirmed_signal_never_starts(self, parts, confirmed_by):
        orchestrator, navigator, flow, _ = parts

        result = orchestrator.run(_signal(), PROMO, "dev", "c1", confirmed_by)

        assert result.error_code == "SIGNAL_NOT_CONFIRMED"
        navigator.navigate.assert_not_called()
        flow.execute_promo.assert_not_called()

    def test_tc_discrepancies_block_unless_explicitly_allowed(self, parts):
        orchestrator, navigator, flow, _ = parts

        blocked = orchestrator.run(_signal(status="tc_discrepancy"), PROMO, "dev", "c1", "raffaele")
        allowed = orchestrator.run(_signal(status="tc_discrepancy"), PROMO, "dev", "c1", "raffaele",
                                   allow_tc_discrepancy=True)

        assert blocked.error_code == "SIGNAL_TC_DISCREPANCY" and allowed.success


class TestFlow:
    def test_navigation_uses_vision_and_comes_before_the_phases(self, parts):
        orchestrator, navigator, flow, _ = parts
        order = []
        navigator.navigate.side_effect = lambda *a, **k: order.append("nav") or Result.ok(True)
        flow.execute_promo.side_effect = lambda *a, **k: order.append("flow") or Result.ok(_casino_result())

        result = orchestrator.run(_signal(), PROMO, "dev", "c1", "raffaele", demo_mode=True)

        assert result.success and order == ["nav", "nav", "flow"]
        for call in navigator.navigate.call_args_list:
            assert call.kwargs["vision_mode"] is True and call.kwargs["demo_mode"] is True
            assert call.kwargs["book"] == "Sisal"
        assert "Promozioni" in navigator.navigate.call_args_list[0].args[1]
        assert "Promo" in navigator.navigate.call_args_list[1].args[1]
        assert len(result.data.navigation_log) == 2

    def test_a_failed_navigation_stops_everything(self, parts):
        orchestrator, navigator, flow, _ = parts
        navigator.navigate.return_value = Result.fail("loop", "NAVIGATION_STUCK")

        result = orchestrator.run(_signal(), PROMO, "dev", "c1", "raffaele")

        assert not result.success and result.error_code == "NAVIGATION_STUCK" and "Promozioni" in result.error
        flow.execute_promo.assert_not_called()

    def test_the_flow_opens_each_slot_through_the_navigator(self, parts):
        orchestrator, navigator, flow, _ = parts
        assert flow.slot_opener == orchestrator._open_slot
        orchestrator._device_id, orchestrator._signal = "dev", _signal()

        orchestrator._open_slot("dev", "Big Bass", "real")
        orchestrator._open_slot("dev", "Big Bass", "fun")

        goals = [call.args[1] for call in navigator.navigate.call_args_list]
        assert "Big Bass" in goals[0] and "buy-in" in goals[1]       # il buy-in solo nella fase real
        assert "Big Bass" in goals[2] and len(goals) == 3

    def test_an_existing_slot_opener_is_not_replaced(self):
        flow = MagicMock(spec=CasinoFlow)
        mine = MagicMock()
        flow.slot_opener = mine

        CasinoOrchestrator(MagicMock(), flow)

        assert flow.slot_opener is mine

    def test_a_failed_slot_open_is_reported(self, parts):
        orchestrator, navigator, _, _ = parts
        orchestrator._device_id, orchestrator._signal = "dev", _signal()
        navigator.navigate.return_value = Result.fail("non trovo la slot", "GOAL_NOT_REACHED")

        result = orchestrator._open_slot("dev", "Big Bass", "real")

        assert not result.success and result.error_code == "GOAL_NOT_REACHED"

    def test_a_failing_flow_is_reported(self, parts):
        orchestrator, _, flow, _ = parts
        flow.execute_promo.return_value = Result.fail("saldo illeggibile", "BALANCE_READ_FAILED")

        result = orchestrator.run(_signal(), PROMO, "dev", "c1", "raffaele")

        assert result.error_code == "BALANCE_READ_FAILED"


class TestResultAndNetcap:
    def test_the_result_is_saved_as_pending_for_the_dashboard(self, parts):
        orchestrator, _, _, tmp_path = parts

        result = orchestrator.run(_signal(), PROMO, "dev", "c1", "raffaele")

        assert result.success and result.data.pending_item_id.startswith("casino_42_")
        saved = json.loads(open(result.data.pending_path, encoding="utf-8").read())
        flow = saved["flows"][0]
        assert flow["status"] == "pending_hitl" and flow["confirmed_by"] == "raffaele"
        assert flow["signal"]["book"] == "Sisal" and flow["casino_result"]["total_profit"] == 1.0

    def test_a_save_failure_does_not_lose_the_result(self, parts):
        orchestrator, _, _, _ = parts
        orchestrator.result_store = MagicMock()
        orchestrator.result_store.save_pending.return_value = Result.fail("disk", "CASINO_RESULT_SAVE_FAILED")

        result = orchestrator.run(_signal(), PROMO, "dev", "c1", "raffaele")

        assert result.success and result.data.pending_path is None and result.data.casino_result.completed

    def test_netcap_is_not_written_by_default(self, parts):
        orchestrator, _, _, _ = parts
        orchestrator.app_api_client = MagicMock()

        orchestrator.run(_signal(), dict(PROMO, bookmaker_id=7), "dev", "c1", "raffaele")

        orchestrator.app_api_client.write_operazione.assert_not_called()

    def test_netcap_is_written_only_with_the_flag_and_a_bookmaker_id(self, parts):
        orchestrator, _, _, _ = parts
        orchestrator.write_to_netcap = True
        orchestrator.app_api_client = MagicMock()
        orchestrator.app_api_client.write_operazione.return_value = Result.ok(WriteResult(True, 99, None))

        result = orchestrator.run(_signal(), dict(PROMO, bookmaker_id=7), "dev", "c1", "raffaele")

        body = orchestrator.app_api_client.write_operazione.call_args.args[0]
        assert body["bookmaker_id"] == 7 and body["categoria"] == "Casino" and body["profitto_book"] == 1.0
        assert result.data.netcap_write.netcap_id == 99

    def test_netcap_without_a_bookmaker_id_is_skipped_not_guessed(self, parts):
        orchestrator, _, _, _ = parts
        orchestrator.write_to_netcap = True
        orchestrator.app_api_client = MagicMock()

        result = orchestrator.run(_signal(), PROMO, "dev", "c1", "raffaele")

        orchestrator.app_api_client.write_operazione.assert_not_called()
        assert result.success and result.data.netcap_write is None

    def test_the_gpt_cost_summary_is_attached(self, parts):
        orchestrator, _, _, _ = parts
        orchestrator.usage_reporter = MagicMock()
        orchestrator.usage_reporter.usage_summary.return_value = "GPT - gpt-4o-mini: 3 chiamate"

        result = orchestrator.run(_signal(), PROMO, "dev", "c1", "raffaele")

        saved = json.loads(open(result.data.pending_path, encoding="utf-8").read())
        assert saved["flows"][0]["gpt_usage"] == "GPT - gpt-4o-mini: 3 chiamate"
