import random
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.opsec.interface import CorrelationCheck
from orchestrator.modules.opsec.monitor import CorrelationMonitor, OpSec, OpSecScheduler

# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def make_op(identity_id="01", book="Stanleybet", stake=100.0,
            opened_at="2026-09-16T10:00:00", op_type="sport"):
    return {
        "identity_id": identity_id,
        "book": book,
        "stake_back": stake,
        "opened_at": opened_at,
        "operation_type": op_type,
    }


class FrozenDateTime(datetime):
    """datetime.now() congelato a un valore fisso, per test deterministici sul rollover giornaliero."""

    _fixed_now = datetime(2026, 9, 16, 23, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed_now


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------

@pytest.fixture
def monitor() -> CorrelationMonitor:
    return CorrelationMonitor()


@pytest.fixture
def scheduler() -> OpSecScheduler:
    return OpSecScheduler()


# ------------------------------------------------------------------
# check_correlation_risk
# ------------------------------------------------------------------

class TestCheckCorrelationLowRisk:
    def test_check_correlation_low_risk(self, monitor: CorrelationMonitor):
        """3 operazioni su book, importi e ore diversi -> risk=low, nessun warning."""
        batch = [
            make_op(identity_id="01", book="Stanleybet", stake=100.0, opened_at="2026-09-16T08:00:00"),
            make_op(identity_id="02", book="SNAI", stake=150.0, opened_at="2026-09-16T12:00:00"),
            make_op(identity_id="03", book="Sisal", stake=200.0, opened_at="2026-09-16T16:00:00"),
        ]
        result = monitor.check_correlation_risk(batch)
        assert result.success
        assert result.data.risk_level == "low"
        assert result.data.warnings == []
        assert result.data.blocked is False


class TestCheckCorrelationHighSameBook:
    def test_check_correlation_high_same_book(self, monitor: CorrelationMonitor):
        """11 operazioni sullo stesso book nella stessa ora (> MAX_CONCURRENT_SAME_BOOK*2) -> blocked."""
        batch = [
            make_op(identity_id=f"id{i:02d}", book="Stanleybet", stake=100.0 + i,
                     opened_at=f"2026-09-16T10:{i * 5:02d}:00")
            for i in range(11)
        ]
        result = monitor.check_correlation_risk(batch)
        assert result.success
        assert result.data.blocked is True
        assert result.data.risk_level == "high"


class TestCheckCorrelationWarningSameBook:
    def test_check_correlation_warning_same_book(self, monitor: CorrelationMonitor):
        """6 operazioni sullo stesso book nella stessa ora (> MAX_CONCURRENT_SAME_BOOK) -> warning, non blocked."""
        batch = [
            make_op(identity_id=f"id{i:02d}", book="Stanleybet", stake=200.0 + i,
                     opened_at=f"2026-09-16T10:{i * 8:02d}:00")
            for i in range(6)
        ]
        result = monitor.check_correlation_risk(batch)
        assert result.success
        assert result.data.blocked is False
        assert result.data.warnings != []
        assert result.data.risk_level == "medium"


class TestCheckCorrelationSameAmount:
    def test_check_correlation_same_amount(self, monitor: CorrelationMonitor):
        """9 operazioni con lo stesso importo (> MAX_SAME_AMOUNT) -> warning."""
        batch = [
            make_op(identity_id=f"id{i:02d}", book=f"Book{i}", stake=100.0,
                     opened_at=f"2026-09-16T{i:02d}:00:00")
            for i in range(9)
        ]
        result = monitor.check_correlation_risk(batch)
        assert result.success
        assert result.data.blocked is False
        assert any("importo" in warning for warning in result.data.warnings)


class TestCheckCorrelationHourlyLimit:
    def test_check_correlation_hourly_limit(self, monitor: CorrelationMonitor):
        """16 operazioni nella stessa ora (> MAX_HOURLY_OPERATIONS) -> warning, non blocked."""
        batch = [
            make_op(identity_id=f"id{i:02d}", book=f"Book{i}", stake=100.0 + i,
                     opened_at=f"2026-09-16T10:{(i * 3):02d}:00")
            for i in range(16)
        ]
        result = monitor.check_correlation_risk(batch)
        assert result.success
        assert result.data.blocked is False
        assert result.data.warnings != []
        assert result.data.risk_level == "medium"


class TestCheckCorrelationHourlyBlocked:
    def test_check_correlation_hourly_blocked(self, monitor: CorrelationMonitor):
        """23 operazioni nella stessa ora (> MAX_HOURLY_OPERATIONS*1.5) -> blocked."""
        batch = [
            make_op(identity_id=f"id{i:02d}", book=f"Book{i}", stake=100.0 + i,
                     opened_at=f"2026-09-16T10:{(i * 2):02d}:00")
            for i in range(23)
        ]
        result = monitor.check_correlation_risk(batch)
        assert result.success
        assert result.data.blocked is True
        assert result.data.risk_level == "high"


class TestCheckCorrelationTemporalSpacing:
    def test_check_correlation_temporal_spacing(self, monitor: CorrelationMonitor):
        """2 operazioni a 30s di distanza (< MIN_OPERATION_SPACING) -> warning."""
        batch = [
            make_op(identity_id="01", book="Stanleybet", stake=100.0, opened_at="2026-09-16T10:00:00"),
            make_op(identity_id="02", book="SNAI", stake=150.0, opened_at="2026-09-16T10:00:30"),
        ]
        result = monitor.check_correlation_risk(batch)
        assert result.success
        assert result.data.blocked is False
        assert result.data.warnings != []
        assert result.data.risk_level == "medium"


class TestCheckCorrelationEmptyBatch:
    def test_check_correlation_empty_batch(self, monitor: CorrelationMonitor):
        """Batch vuoto -> risk=low, nessun warning, non blocked."""
        result = monitor.check_correlation_risk([])
        assert result.success
        assert result.data.risk_level == "low"
        assert result.data.warnings == []
        assert result.data.blocked is False


# ------------------------------------------------------------------
# schedule_operation
# ------------------------------------------------------------------

class TestScheduleNormal:
    def test_schedule_normal(self, scheduler: OpSecScheduler):
        """urgency=normal -> datetime futuro, nella finestra preferita hash-based + jitter deterministico."""
        random.seed(42)
        now_before = datetime.now()
        result = scheduler.schedule_operation("identity-01", "sport", "Stanleybet", urgency="normal")
        assert result.success
        scheduled = datetime.fromisoformat(result.data)
        assert scheduled >= now_before
        # base_hour("identity-01")=10 (md5 deterministico) + jitter(seed 42)=163min -> 12:43
        assert scheduled.hour == 12
        assert scheduled.minute == 43


class TestScheduleHighUrgency:
    def test_schedule_high_urgency(self, scheduler: OpSecScheduler):
        """urgency=high -> datetime = now, nessuna diversity applicata."""
        before = datetime.now()
        result = scheduler.schedule_operation("identity-01", "sport", "Stanleybet", urgency="high")
        after = datetime.now()
        assert result.success
        scheduled = datetime.fromisoformat(result.data)
        assert before <= scheduled <= after


class TestScheduleDeterministicWindow:
    def test_schedule_deterministic_window(self, scheduler: OpSecScheduler):
        """Stessa identity_id -> stesso base_hour (hash md5 deterministico tra chiamate)."""
        first = scheduler._preferred_base_hour("identity-01")
        second = scheduler._preferred_base_hour("identity-01")
        assert first == second == 10


class TestScheduleDifferentIdentities:
    def test_schedule_different_identities(self, scheduler: OpSecScheduler):
        """Identity_id diversi -> base_hour diverso (probabile)."""
        base_a = scheduler._preferred_base_hour("identity-01")
        base_b = scheduler._preferred_base_hour("identity-02")
        assert base_a != base_b


class TestSchedulePastRescheduled:
    def test_schedule_past_rescheduled(self, scheduler: OpSecScheduler, monkeypatch):
        """Se l'orario calcolato nella finestra preferita è già passato -> spostato al giorno successivo."""
        monkeypatch.setattr("orchestrator.modules.opsec.monitor.datetime", FrozenDateTime)
        random.seed(42)
        result = scheduler.schedule_operation("identity-01", "sport", "Stanleybet", urgency="normal")
        assert result.success
        scheduled = datetime.fromisoformat(result.data)
        assert scheduled.date() == date(2026, 9, 17)
        assert scheduled.hour == 12
        assert scheduled.minute == 43


class TestScheduleCorrelationBlock:
    def test_schedule_correlation_block(self):
        """Se check_correlation_risk segnala blocked, lo schedule viene posticipato di 1-3 ore."""
        mock_monitor = MagicMock()
        mock_monitor.check_correlation_risk.side_effect = [
            Result.ok(CorrelationCheck(risk_level="high", warnings=[], blocked=True)),
            Result.ok(CorrelationCheck(risk_level="low", warnings=[], blocked=False)),
        ]
        scheduler = OpSecScheduler(monitor=mock_monitor)

        random.seed(42)
        result = scheduler.schedule_operation("identity-01", "sport", "Stanleybet", urgency="normal")

        assert result.success
        scheduled = datetime.fromisoformat(result.data)
        # base 12:43 (jitter seed 42) + shift 1h (randint(1,3) seed 42) -> 13:43
        assert scheduled.hour == 13
        assert scheduled.minute == 43
        assert mock_monitor.check_correlation_risk.call_count == 2


# ------------------------------------------------------------------
# _group_by
# ------------------------------------------------------------------

class TestGroupBy:
    def test_group_by(self):
        """Raggruppa correttamente una lista di dict secondo la key_func fornita."""
        items = [
            {"book": "Stanleybet", "n": 1},
            {"book": "SNAI", "n": 2},
            {"book": "Stanleybet", "n": 3},
        ]
        groups = CorrelationMonitor._group_by(items, lambda o: o["book"])
        assert set(groups.keys()) == {"Stanleybet", "SNAI"}
        assert len(groups["Stanleybet"]) == 2
        assert len(groups["SNAI"]) == 1


# ------------------------------------------------------------------
# OpSec — orchestratore
# ------------------------------------------------------------------

class TestOpSecDelegatesToCollaborators:
    def test_opsec_delegates_to_collaborators(self):
        """OpSec.check_correlation_risk e schedule_operation delegano a monitor/scheduler condivisi."""
        opsec = OpSec()
        result = opsec.check_correlation_risk([])
        assert result.success
        assert result.data.risk_level == "low"

        schedule_result = opsec.schedule_operation("identity-01", "sport", "Stanleybet", urgency="high")
        assert schedule_result.success
