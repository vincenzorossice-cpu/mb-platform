from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.flows.casino_flow import CasinoFlow, CasinoPhaseResult, CasinoResult, SlotOCRReader
from orchestrator.modules.screen_reader.interface import BalanceReading


# ----------------------------------------------------------------------
# helper factories
# ----------------------------------------------------------------------

def _balance_reading(raw_balance: float = 100.0, open_bets: float = 0.0,
                      real_bonus: float = 0.0, wagering_bonus=None) -> BalanceReading:
    return BalanceReading(
        raw_balance=raw_balance, open_bets=open_bets, real_bonus=real_bonus,
        wagering_bonus=wagering_bonus, read_method="uiautomator", confidence=1.0,
    )


@pytest.fixture
def mock_deps():
    reader = MagicMock()
    injector = MagicMock()
    reader._screenshot.return_value = Result.ok("/tmp/test.png")
    injector.tap_coordinates.return_value = Result.ok(True)
    return reader, injector


@pytest.fixture
def flow(mock_deps):
    reader, injector = mock_deps
    flow = CasinoFlow(screen_reader=reader, touch_injector=injector)
    flow.ocr_reader = MagicMock()
    flow.ocr_reader.read_slot_state.return_value = {
        "saldo": 100.0, "posta": 0.10, "spin_button_coords": (540, 1900),
    }
    return flow


class TestSlotOCRReader:
    def test_extract_amount_simple(self):
        """Legge importi con virgola decimale e punto delle migliaia."""
        r = SlotOCRReader()
        assert r._extract_amount("114,36") == 114.36
        assert r._extract_amount("0,10") == 0.10
        assert r._extract_amount("1.234,56") == 1234.56
        assert r._extract_amount("non un numero") is None


class TestCasinoFlow:
    def test_execute_phase_success(self, flow, mock_deps):
        """30 spin da 0.10 EUR per un target di 3.0 EUR: la fase si completa."""
        reader, injector = mock_deps

        saldo_sequence = [100.0] * 25 + [97.0] * 10
        flow._read_slot_balance = MagicMock(side_effect=saldo_sequence)
        flow._exit_slot = MagicMock()

        result = flow._execute_phase(
            device_id="test", slot_name="Test Slot", target_amount=3.0,
            spin_amount=0.10, phase_name="real",
        )

        assert result.completed is True
        assert result.spins_done == 30
        assert result.target_amount == 3.0

    def test_execute_phase_stops_when_insufficient(self, flow, mock_deps):
        """Se il saldo scende sotto lo spin minimo, la fase si ferma prima del target."""
        flow._read_slot_balance = MagicMock(side_effect=[100.0, 95.0, 0.05])
        flow._exit_slot = MagicMock()

        result = flow._execute_phase(
            device_id="test", slot_name="Test", target_amount=10.0,
            spin_amount=0.10, phase_name="real",
        )

        assert result.completed is False
        assert result.spins_done < 100

    def test_precise_spin_counting(self, flow, mock_deps):
        """Verifica che il conteggio spin e' preciso: 28 spin totali, non 30."""
        flow._read_slot_balance = MagicMock(side_effect=[100.0] * 40)
        flow._exit_slot = MagicMock()

        result = flow._execute_phase(
            device_id="test", slot_name="Test", target_amount=2.8,  # 2.8 / 0.10 = 28 spin
            spin_amount=0.10, phase_name="real",
        )

        assert result.spins_done == 28  # NON 30
        assert result.completed is True

    def test_stop_request(self, flow, mock_deps):
        """stop() chiamato dall'esterno interrompe il loop entro pochi spin."""
        reader, injector = mock_deps
        flow._read_slot_balance = MagicMock(return_value=100.0)
        flow._exit_slot = MagicMock()

        def stop_after_5(*args, **kwargs):
            if injector.tap_coordinates.call_count > 5:
                flow.stop()
            return Result.ok(True)
        injector.tap_coordinates.side_effect = stop_after_5

        result = flow._execute_phase(
            device_id="test", slot_name="Test", target_amount=10.0,
            spin_amount=0.10, phase_name="real",
        )

        assert result.spins_done <= 6

    def test_read_slot_balance_returns_none(self, flow, mock_deps):
        """Se lo screenshot fallisce, _read_slot_balance ritorna None senza sollevare."""
        reader, injector = mock_deps
        reader._screenshot.return_value = Result.fail("error", "ADB_TIMEOUT")

        balance = flow._read_slot_balance("test_device")
        assert balance is None
