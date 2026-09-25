from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.flows.casino_flow import CasinoFlow, SlotOCRReader
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


def _detection(text: str, x: int, y: int, conf: float = 0.9):
    """Riproduce il formato nativo di easyocr.Reader.readtext(): (bbox, text, conf)."""
    bbox = [[x, y], [x + 150, y], [x + 150, y + 30], [x, y + 30]]
    return (bbox, text, conf)


def _ocr_reader_with(detections: list) -> SlotOCRReader:
    reader = SlotOCRReader()
    reader._ocr = MagicMock()
    reader._ocr.readtext.return_value = detections
    return reader


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

    def test_extract_amount_us_format(self):
        """Formato americano: virgola delle migliaia, punto decimale."""
        r = SlotOCRReader()
        assert r._extract_amount("5,000.00") == 5000.00
        assert r._extract_amount("MODALITÀ DEMO € 5,000.00") == 5000.00

    def test_extract_amount_label_and_amount_combined(self):
        """L'importo va estratto anche quando e' nella stessa stringa della label."""
        r = SlotOCRReader()
        assert r._extract_amount("PUNTATA € 1.00") == 1.00
        assert r._extract_amount("SALDO 114,36 €") == 114.36
        assert r._extract_amount("POSTA 0,10€") == 0.10

    def test_real_bug_scenario_saldo_and_posta_on_same_row(self):
        """Riproduce il bug reale: SALDO e PUNTATA su detection separate ma con
        y quasi identica (stessa riga della UI). Prima del fix, l'importo della
        posta finiva assegnato al saldo (if/elif esclusivo su sola y)."""
        ocr_reader = _ocr_reader_with([
            _detection("MODALITÀ DEMO € 5,000.00", x=13, y=2219, conf=0.94),
            _detection("PUNTATA € 1.00", x=729, y=2225, conf=0.65),
        ])

        state = ocr_reader.read_slot_state("fake.png")

        assert state["saldo"] == 5000.00
        assert state["posta"] == 1.00

    def test_fallback_when_amount_in_separate_element(self):
        """Label e importo in due detection distinte, entro y+-50px: usa il
        fallback per associare l'importo piu' vicino (in x) alla label."""
        ocr_reader = _ocr_reader_with([
            _detection("SALDO", x=10, y=100, conf=0.9),
            _detection("€ 114,36", x=200, y=105, conf=0.9),
        ])

        state = ocr_reader.read_slot_state("fake.png")

        assert state["saldo"] == 114.36

    def test_fallback_does_not_steal_other_labels_value(self):
        """Con SALDO/POSTA sulla stessa riga e importi in detection separate,
        ognuna deve prendere il proprio importo piu' vicino, non quello altrui."""
        ocr_reader = _ocr_reader_with([
            _detection("SALDO", x=10, y=100, conf=0.9),
            _detection("100,00", x=120, y=102, conf=0.9),
            _detection("POSTA", x=400, y=100, conf=0.9),
            _detection("0,10", x=500, y=102, conf=0.9),
        ])

        state = ocr_reader.read_slot_state("fake.png")

        assert state["saldo"] == 100.00
        assert state["posta"] == 0.10

    def test_spin_button_recognizes_gira(self):
        ocr_reader = _ocr_reader_with([_detection("GIRA", x=500, y=1900, conf=0.9)])
        state = ocr_reader.read_slot_state("fake.png")
        assert state["spin_button_coords"] == (550, 1920)

    def test_xiter_feature_button_not_mistaken_for_spin(self):
        """'X-iter' e' il bottone feature/bonus-buy di Wazdan, un elemento
        distinto dal tasto spin: non deve mai essere riconosciuto come tale
        (bug reale: un falso positivo qui sovrascriveva le coordinate spin
        imparate correttamente, bloccando poi tap legittimi in quella zona)."""
        ocr_reader = _ocr_reader_with([_detection("X-iter", x=50, y=1800, conf=0.9)])
        state = ocr_reader.read_slot_state("fake.png")
        assert state["spin_button_coords"] is None

        ocr_reader = _ocr_reader_with([_detection("Xiter", x=50, y=1800, conf=0.9)])
        state = ocr_reader.read_slot_state("fake.png")
        assert state["spin_button_coords"] is None

    def test_balance_label_recognizes_balance_keyword(self):
        ocr_reader = _ocr_reader_with([_detection("BALANCE € 25.50", x=10, y=100, conf=0.9)])
        state = ocr_reader.read_slot_state("fake.png")
        assert state["saldo"] == 25.50

    def test_bet_label_recognizes_stake_keyword(self):
        ocr_reader = _ocr_reader_with([_detection("STAKE 0.20", x=10, y=100, conf=0.9)])
        state = ocr_reader.read_slot_state("fake.png")
        assert state["posta"] == 0.20


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
