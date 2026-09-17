from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.cascade_engine.engine import CascadeEngine
from orchestrator.modules.cascade_engine.interface import CascadeState
from orchestrator.modules.multiplier_engine.interface import CascadeCalculation, CascadeEventCalc
from orchestrator.modules.screen_reader.interface import BetStatus
from orchestrator.modules.touch_injector.interface import LayBetResult


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _make_calculation(n: int = 3) -> CascadeCalculation:
    back_odds = [2.0] * n
    lay_odds = [2.1, 2.2, 2.3][:n]
    lay_stakes = [48.0, 46.0, 44.0][:n]
    liabilities = [round(s * (o - 1), 2) for s, o in zip(lay_stakes, lay_odds)]
    events = [
        CascadeEventCalc(
            index=i, lay_stake=lay_stakes[i], liability=liabilities[i], lay_odds=lay_odds[i],
            profit_if_won=10.0, profit_if_all_lost=10.0,
        )
        for i in range(n)
    ]
    return CascadeCalculation(
        events=events, profit_per_scenario={}, total_liability=max(liabilities), is_valid=True,
        warnings=[], back_stake=100.0, back_odds=back_odds, lay_odds=lay_odds, commission=0.045,
    )


def _make_operation(device_id: str = "dev-1", n: int = 3) -> dict:
    return {
        "operation_id": "op-1",
        "identity_id": "id-1",
        "device_id": device_id,
        "commission_rate": 0.045,
        "markets": [f"MKT-{i}" for i in range(n)],
    }


def _make_state(**overrides) -> CascadeState:
    defaults = dict(
        cascade_id="cid", operation_id="op-1", identity_id="id-1", device_id="dev-1",
        markets=["MKT-0", "MKT-1", "MKT-2"], current_event_index=0, state="event_1_waiting",
        back_stake=100.0, back_odds=[2.0, 2.0, 2.0], lay_odds=[2.1, 2.2, 2.3],
        lay_stakes=[48.0, 46.0, 44.0], liabilities=[52.8, 50.6, 45.32],
        commission_rate=0.045, betfair_bet_ids=["BF1"], retry_count=0, max_retries=3,
        next_check_at="", last_check_at="",
    )
    defaults.update(overrides)
    return CascadeState(**defaults)


def _seed_cascade(engine: CascadeEngine, current_event_index: int = 0,
                   state_name: str = "event_1_waiting", n: int = 3):
    """Crea una cascata reale via start_cascade, poi forza lo stato desiderato."""
    result = engine.start_cascade(_make_operation(n=n), _make_calculation(n=n))
    assert result.success
    cascade_id = result.data
    state = engine._cascades[cascade_id]
    state.current_event_index = current_event_index
    state.state = state_name
    engine._cascades[cascade_id] = state
    return cascade_id, state


def _engine_with_mocks():
    screen_reader = MagicMock()
    touch_injector = MagicMock()
    touch_injector.place_lay_bet.return_value = Result.ok(
        LayBetResult(confirmed=True, bet_id="BF-INIT", actual_odds=2.1, actual_stake=48.0)
    )
    engine = CascadeEngine(screen_reader=screen_reader, touch_injector=touch_injector)
    return engine, screen_reader, touch_injector


# ------------------------------------------------------------------
# start_cascade
# ------------------------------------------------------------------

class TestStartCascade:
    def test_start_cascade_success(self):
        """place_lay_bet riuscito -> cascade_id restituito, state=event_1_waiting."""
        touch_injector = MagicMock()
        touch_injector.place_lay_bet.return_value = Result.ok(
            LayBetResult(confirmed=True, bet_id="BF1", actual_odds=2.1, actual_stake=48.0)
        )
        engine = CascadeEngine(touch_injector=touch_injector)

        result = engine.start_cascade(_make_operation(), _make_calculation())

        assert result.success
        state = engine._cascades[result.data]
        assert state.state == "event_1_waiting"
        assert state.betfair_bet_ids == ["BF1"]

    def test_start_cascade_lay_failed(self):
        """place_lay_bet fallito -> LAY_PLACEMENT_FAILED, state salvato = error."""
        touch_injector = MagicMock()
        touch_injector.place_lay_bet.return_value = Result.fail("tap missed", "TAP_MISSED")
        engine = CascadeEngine(touch_injector=touch_injector)

        result = engine.start_cascade(_make_operation(), _make_calculation())

        assert not result.success
        assert result.error_code == "LAY_PLACEMENT_FAILED"
        cascade_id = next(iter(engine._cascades))
        assert engine._cascades[cascade_id].state == "error"

    def test_start_cascade_device_busy(self):
        """Device già locked -> DEVICE_BUSY, nessuna bancata piazzata."""
        touch_injector = MagicMock()
        engine = CascadeEngine(touch_injector=touch_injector)
        engine._device_locks["dev-busy"] = True

        result = engine.start_cascade(_make_operation(device_id="dev-busy"), _make_calculation())

        assert not result.success
        assert result.error_code == "DEVICE_BUSY"
        touch_injector.place_lay_bet.assert_not_called()


# ------------------------------------------------------------------
# check_and_advance
# ------------------------------------------------------------------

class TestCheckAndAdvance:
    def test_check_and_advance_won(self):
        """read_bet_status='settled_won' -> state=completed."""
        engine, screen_reader, _ = _engine_with_mocks()
        cascade_id, _ = _seed_cascade(engine)
        screen_reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="settled_won", lay_odds=2.1, confidence=1.0)
        )

        result = engine.check_and_advance(cascade_id)

        assert result.success
        assert result.data.state == "completed"

    def test_check_and_advance_lost_advance(self):
        """'settled_lost' su evento 1 -> piazza evento 2, state=event_2_waiting."""
        engine, screen_reader, touch_injector = _engine_with_mocks()
        cascade_id, _ = _seed_cascade(engine)
        screen_reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="settled_lost", lay_odds=2.1, confidence=1.0)
        )

        result = engine.check_and_advance(cascade_id)

        assert result.success
        assert result.data.state == "event_2_waiting"
        assert result.data.current_event_index == 1
        assert touch_injector.place_lay_bet.call_count == 2  # seed + evento 2

    def test_check_and_advance_lost_completed(self):
        """'settled_lost' sull'ultimo evento -> state=completed (multipla vinta)."""
        engine, screen_reader, touch_injector = _engine_with_mocks()
        cascade_id, _ = _seed_cascade(engine, current_event_index=2, state_name="event_3_waiting")
        screen_reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="settled_lost", lay_odds=2.3, confidence=1.0)
        )

        result = engine.check_and_advance(cascade_id)

        assert result.success
        assert result.data.state == "completed"
        touch_injector.place_lay_bet.assert_called_once()  # solo la placement del seed

    def test_check_and_advance_unsettled(self):
        """'unsettled' -> next_check_at aggiornato, state invariato."""
        engine, screen_reader, _ = _engine_with_mocks()
        cascade_id, _ = _seed_cascade(engine)
        screen_reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="unsettled", lay_odds=2.1, confidence=1.0)
        )

        result = engine.check_and_advance(cascade_id)

        assert result.success
        assert result.data.state == "event_1_waiting"
        assert result.data.retry_count == 0
        next_dt = datetime.fromisoformat(result.data.next_check_at)
        assert next_dt > datetime.now(timezone.utc)

    def test_check_and_advance_max_retries(self):
        """3 fallimenti consecutivi di lettura -> hitl_blocked."""
        engine, screen_reader, _ = _engine_with_mocks()
        cascade_id, _ = _seed_cascade(engine)
        screen_reader.read_bet_status.return_value = Result.fail("ocr low confidence", "OCR_LOW_CONFIDENCE")

        result = None
        for _ in range(3):
            result = engine.check_and_advance(cascade_id)

        assert result.success
        assert result.data.state == "hitl_blocked"
        assert result.data.retry_count == 3

    def test_check_and_advance_not_found(self):
        """cascade_id inesistente -> CASCADE_NOT_FOUND."""
        engine = CascadeEngine()

        result = engine.check_and_advance("id-inesistente")

        assert not result.success
        assert result.error_code == "CASCADE_NOT_FOUND"

    def test_check_and_advance_already_completed(self):
        """state=completed -> return ok senza leggere lo stato reale."""
        engine, screen_reader, _ = _engine_with_mocks()
        cascade_id, _ = _seed_cascade(engine, state_name="completed")

        result = engine.check_and_advance(cascade_id)

        assert result.success
        assert result.data.state == "completed"
        screen_reader.read_bet_status.assert_not_called()


# ------------------------------------------------------------------
# recover_cascade
# ------------------------------------------------------------------

class TestRecoverCascade:
    def test_recover_cascade_from_error(self):
        """state=error, read_bet_status='settled_won' -> state=completed."""
        engine, screen_reader, _ = _engine_with_mocks()
        cascade_id, _ = _seed_cascade(engine, state_name="error")
        screen_reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="settled_won", lay_odds=2.1, confidence=1.0)
        )

        result = engine.recover_cascade(cascade_id)

        assert result.success
        assert result.data.state == "completed"

    def test_recover_cascade_hitl(self):
        """state=error, read_bet_status fallisce -> hitl_blocked."""
        engine, screen_reader, _ = _engine_with_mocks()
        cascade_id, _ = _seed_cascade(engine, state_name="error")
        screen_reader.read_bet_status.return_value = Result.fail("device offline", "DEVICE_OFFLINE")

        result = engine.recover_cascade(cascade_id)

        assert result.success
        assert result.data.state == "hitl_blocked"


# ------------------------------------------------------------------
# _handle_event_lost
# ------------------------------------------------------------------

class TestHandleEventLost:
    def test_handle_event_lost_with_recalc(self):
        """Quote cambiate >0.05 -> recalculate_from_index chiamato, nuovi lay_stakes usati."""
        multiplier_engine = MagicMock()
        new_events = [
            CascadeEventCalc(index=0, lay_stake=50.0, liability=55.0, lay_odds=2.3,
                              profit_if_won=12.0, profit_if_all_lost=12.0),
            CascadeEventCalc(index=1, lay_stake=40.0, liability=44.0, lay_odds=2.4,
                              profit_if_won=12.0, profit_if_all_lost=12.0),
        ]
        multiplier_engine.recalculate_from_index.return_value = Result.ok(CascadeCalculation(
            events=new_events, profit_per_scenario={}, total_liability=55.0, is_valid=True,
        ))
        touch_injector = MagicMock()
        touch_injector.place_lay_bet.return_value = Result.ok(
            LayBetResult(confirmed=True, bet_id="BF2", actual_odds=2.3, actual_stake=50.0)
        )
        engine = CascadeEngine(touch_injector=touch_injector, multiplier_engine=multiplier_engine)
        state = _make_state()

        engine._handle_event_lost(state, "cid", observed_lay_odds=2.3)  # pianificata era 2.1

        multiplier_engine.recalculate_from_index.assert_called_once()
        args = multiplier_engine.recalculate_from_index.call_args[0]
        assert args[4] == 1  # from_index == current_event_index dopo l'incremento
        assert state.lay_odds[1] == 2.3
        assert state.lay_stakes[1] == 50.0
        assert state.liabilities[1] == 55.0
        assert state.state == "event_2_waiting"

    def test_handle_event_lost_recalc_failed(self):
        """recalculate_from_index fallisce -> hitl_blocked, nessuna nuova bancata piazzata."""
        multiplier_engine = MagicMock()
        multiplier_engine.recalculate_from_index.return_value = Result.fail("bad odds", "INVALID_ODDS")
        touch_injector = MagicMock()
        engine = CascadeEngine(touch_injector=touch_injector, multiplier_engine=multiplier_engine)
        state = _make_state()

        engine._handle_event_lost(state, "cid", observed_lay_odds=2.5)  # pianificata era 2.1

        assert state.state == "hitl_blocked"
        touch_injector.place_lay_bet.assert_not_called()


# ------------------------------------------------------------------
# device lock
# ------------------------------------------------------------------

class TestDeviceLockRelease:
    def test_device_lock_release(self):
        """Il lock viene rilasciato dopo check_and_advance, sia su successo che su fallimento."""
        engine, screen_reader, _ = _engine_with_mocks()
        cascade_id, state = _seed_cascade(engine)

        screen_reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="unsettled", lay_odds=2.1, confidence=1.0)
        )
        engine.check_and_advance(cascade_id)
        assert state.device_id not in engine._device_locks

        screen_reader.read_bet_status.return_value = Result.fail("boom", "OCR_LOW_CONFIDENCE")
        engine.check_and_advance(cascade_id)
        assert state.device_id not in engine._device_locks
