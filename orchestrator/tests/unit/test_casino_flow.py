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


# ----------------------------------------------------------------------
# Spin eseguiti dallo SlotAutoPilot
# ----------------------------------------------------------------------

from decimal import Decimal

from orchestrator.modules.slot_autopilot.bet_controller import BetResult
from orchestrator.modules.slot_autopilot.spin_controller import SpinResult


def _autopilot(bet="0.10", verified=True, balances=(Decimal("100"), Decimal("97"))):
    autopilot = MagicMock()
    autopilot.prepare.return_value = Result.ok(True)
    autopilot.set_min_bet.return_value = Result.ok(BetResult(
        slot_name="s", starting_bet=Decimal("2"), final_bet=Decimal(bet), min_bet_verified=verified, attempts=1,
        aborted_reason=None if verified else "MIN_NOT_PROVEN",
    ))
    autopilot.get_balance_summary.return_value = balances
    autopilot.spin.side_effect = lambda device, index: Result.ok(SpinResult(
        slot_name="s", spin_index=index, bet_amount=Decimal(bet), balance_before=None,
        balance_after=Decimal("99"), win_amount=None, verified=True, attempts=1, outcome="success",
    ))
    return autopilot


@pytest.fixture
def autopilot_flow(mock_deps, monkeypatch):
    monkeypatch.setattr("orchestrator.flows.casino_flow.time.sleep", lambda seconds: None)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.session.time.sleep", lambda seconds: None)
    reader, injector = mock_deps
    autopilots = []

    def factory(device_id, slot_name, target):
        autopilot = _autopilot()
        autopilots.append((slot_name, target, autopilot))
        return Result.ok(autopilot)

    opener = MagicMock(return_value=Result.ok(True))
    flow = CasinoFlow(reader, injector, autopilot_factory=factory, slot_opener=opener)
    flow.ocr_reader = MagicMock()
    flow.ocr_reader.read_slot_state.return_value = {"saldo": 100.0, "posta": 0.10, "spin_button_coords": (540, 1900)}
    flow._real_money_enabled = True
    flow.autopilots, flow.opener = autopilots, opener
    return flow, injector


class TestPhasesWithTheAutopilot:
    def test_a_real_phase_is_played_by_the_autopilot_not_by_the_old_loop(self, autopilot_flow):
        flow, injector = autopilot_flow

        phase = flow._execute_phase("dev", "Slot Real", 0.30, 0.10, "real")

        assert phase.completed and phase.spins_done == 3 and phase.played_amount == pytest.approx(0.30)
        assert phase.initial_balance == 100.0 and phase.final_balance == 97.0
        injector.tap_coordinates.assert_not_called()                 # nessun tocco a tempo fisso
        slot, target, autopilot = flow.autopilots[0]
        assert slot == "Slot Real" and target == Decimal("0.3")
        autopilot.set_min_bet.assert_called_once()
        autopilot.shutdown.assert_called_once_with("dev")

    def test_real_money_phases_need_an_explicit_consent(self, autopilot_flow):
        flow, _ = autopilot_flow
        flow._real_money_enabled = False

        for phase_name in ("real", "real_bonus"):
            phase = flow._execute_phase("dev", "Slot", 0.30, 0.10, phase_name)
            assert not phase.completed and "real_money_enabled" in phase.notes[0]

        assert flow.autopilots == []        # lo SlotAutoPilot non viene nemmeno creato
        flow.opener.assert_not_called()

    def test_consent_comes_from_the_promo_config(self, autopilot_flow, monkeypatch):
        flow, _ = autopilot_flow
        flow._real_money_enabled = False
        flow.screen_reader.read_balance.return_value = Result.fail("no", "X")

        result = flow.execute_promo("dev", "c1", "book", {"real_money_enabled": True, "slot_real": "S", "target_real": 0.3,
                                                          "spin_real": 0.1})

        assert not result.success                       # il saldo iniziale non si legge: ma il flag e' stato letto
        assert flow._real_money_enabled is True

    def test_the_fun_phase_needs_no_consent(self, autopilot_flow):
        flow, injector = autopilot_flow
        flow._real_money_enabled = False

        phase = flow._execute_fun_phase(
            "dev", "Slot Fun", target_balance=50.0, rollover_target=0.30, spin_high=1.0, spin_low=0.10,
            fun_bonus_amount=30.0,
        )

        assert phase.phase == "fun" and phase.completed and phase.spins_done == 3
        injector.tap_coordinates.assert_not_called()    # saldo 100 >= target 50: la 2A e' gia' soddisfatta

    def test_phase_2a_with_high_bets_keeps_the_old_loop(self, autopilot_flow):
        flow, injector = autopilot_flow
        flow.ocr_reader.read_slot_state.side_effect = [
            {"saldo": 20.0, "posta": 1.0, "spin_button_coords": (540, 1900)},    # saldo iniziale
            {"saldo": 20.0, "posta": 1.0, "spin_button_coords": (540, 1900)},    # _find_spin_button
            {"saldo": 60.0, "posta": 1.0, "spin_button_coords": (540, 1900)},    # dopo 5 spin: saldo letto
        ] + [{"saldo": 60.0, "posta": 1.0, "spin_button_coords": (540, 1900)}] * 20

        phase = flow._execute_fun_phase(
            "dev", "Slot Fun", target_balance=50.0, rollover_target=0.30, spin_high=1.0, spin_low=0.10,
            fun_bonus_amount=20.0,
        )

        assert injector.tap_coordinates.call_count >= 1             # la 2A tocca con il vecchio ciclo
        assert flow.autopilots and flow.autopilots[0][1] == Decimal("0.3")   # la 2B passa all'autopilot
        assert phase.spins_done >= 3

    def test_the_slot_is_opened_before_each_phase(self, autopilot_flow):
        flow, _ = autopilot_flow

        flow._execute_phase("dev", "Slot Real", 0.20, 0.10, "real")
        flow._execute_fun_phase("dev", "Slot Fun", 50.0, 0.20, 1.0, 0.10, 30.0)

        assert [call.args for call in flow.opener.call_args_list] == [
            ("dev", "Slot Real", "real"), ("dev", "Slot Fun", "fun"),
        ]       # la 2B non riapre la slot gia' aperta dalla 2A

    def test_a_slot_that_cannot_be_opened_fails_the_phase_without_playing(self, autopilot_flow):
        flow, _ = autopilot_flow
        flow.opener.return_value = Result.fail("menu non trovato", "NAVIGATION_STUCK")

        phase = flow._execute_phase("dev", "Slot Real", 0.30, 0.10, "real")

        assert not phase.completed and "Impossibile aprire la slot" in phase.notes[0]
        assert flow.autopilots == []

    def test_an_unverified_minimum_bet_fails_the_phase_with_the_reason(self, autopilot_flow):
        flow, _ = autopilot_flow
        flow.autopilot_factory = lambda device, slot, target: Result.ok(_autopilot(verified=False))

        phase = flow._execute_phase("dev", "Slot Real", 0.30, 0.10, "real")

        assert not phase.completed and phase.spins_done == 0
        assert "BET_NOT_VERIFIED" in phase.notes[0] and "MIN_NOT_PROVEN" in phase.notes[0]

    def test_an_autopilot_that_cannot_be_created_fails_the_phase(self, autopilot_flow):
        flow, _ = autopilot_flow
        flow.autopilot_factory = lambda device, slot, target: Result.fail("config", "CONFIG_INVALID")

        phase = flow._execute_phase("dev", "Slot Real", 0.30, 0.10, "real")

        assert not phase.completed and "CONFIG_INVALID" in phase.notes[0]

    def test_an_unexpected_error_in_the_session_is_reported_and_the_autopilot_is_shut_down(self, autopilot_flow):
        flow, _ = autopilot_flow
        broken = _autopilot()
        broken.prepare.side_effect = RuntimeError("adb morto")
        flow.autopilot_factory = lambda device, slot, target: Result.ok(broken)

        phase = flow._execute_phase("dev", "Slot Real", 0.30, 0.10, "real")

        assert not phase.completed and "adb morto" in phase.notes[0]
        broken.shutdown.assert_called_once_with("dev")

    def test_without_a_factory_nothing_changes(self, flow, mock_deps, monkeypatch):
        monkeypatch.setattr("orchestrator.flows.casino_flow.time.sleep", lambda seconds: None)
        _, injector = mock_deps

        phase = flow._execute_phase("dev", "Slot", 0.20, 0.10, "real")

        assert phase.completed and injector.tap_coordinates.call_count == 2


class TestFullPromoWithTheAutopilot:
    def test_three_phases_run_in_order_and_use_the_autopilot_for_the_minimum_bet_parts(self, autopilot_flow):
        flow, _ = autopilot_flow
        reader = flow.screen_reader
        reader.read_balance.side_effect = [
            Result.ok(_balance_reading(100.0)),                                   # saldo iniziale
            Result.ok(_balance_reading(100.0, wagering_bonus=0.20)),              # dopo la fase 1: fun bonus
            Result.ok(_balance_reading(100.0, real_bonus=0.20)),                  # dopo la fase 2: real bonus
            Result.ok(_balance_reading(101.0)),                                   # saldo finale
        ]
        reader.find_element.return_value = Result.fail("no", "ELEMENT_NOT_FOUND")  # _exit_slot: niente da tappare
        promo = {
            "real_money_enabled": True, "promo_name": "Promo",
            "slot_real": "Slot Real", "slot_fun": "Slot Fun", "slot_real_bonus": "Slot Bonus",
            "target_real": 0.30, "target_fun_balance": 50.0, "fun_rollover_multiplier": 1, "fun_cap": None,
            "real_bonus_amount": 0.20, "spin_real": 0.10, "spin_fun": 1.0, "spin_fun_rollover": 0.10,
            "spin_real_bonus": 0.10,
        }

        result = flow.execute_promo("dev", "c1", "book", promo)

        assert result.success and result.data.completed
        assert [p.phase for p in result.data.phases] == ["real", "fun", "real_bonus"]
        assert [slot for slot, _, _ in flow.autopilots] == ["Slot Real", "Slot Fun", "Slot Bonus"]
        assert result.data.total_profit == pytest.approx(1.0)
