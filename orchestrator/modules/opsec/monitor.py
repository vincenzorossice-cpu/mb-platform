import hashlib
import random
from datetime import datetime, timedelta
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.opsec.interface import CorrelationCheck, OpSecInterface


class CorrelationMonitor:
    """Verifica il rischio di correlation detection su un batch di operazioni."""

    MAX_CONCURRENT_SAME_BOOK = 5      # max identità sullo stesso book nella stessa ora
    MAX_SAME_AMOUNT = 8                # max operazioni con stesso importo nello stesso giorno
    MAX_HOURLY_OPERATIONS = 15         # max operazioni totali per ora
    MIN_OPERATION_SPACING = 60         # minima distanza temporale in secondi tra operazioni
    DAILY_MAX_PER_IDENTITY = 10        # max operazioni per identità per giorno

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def check_correlation_risk(self, batch: list[dict]) -> Result[CorrelationCheck]:
        if not batch:
            return Result.ok(CorrelationCheck(risk_level="low", warnings=[], blocked=False))

        warnings: list[str] = []
        blocked = False

        timed_ops = [op for op in batch if op.get("opened_at")]

        # 1 — troppe identità sullo stesso book nella stessa ora
        by_book_hour = self._group_by(timed_ops, lambda o: (o["book"], self._hour_key(o)))
        for (book, hour), ops in by_book_hour.items():
            if len(ops) > self.MAX_CONCURRENT_SAME_BOOK:
                warnings.append(
                    f"{len(ops)} identità sul book {book} nell'ora {hour} "
                    f"(soglia {self.MAX_CONCURRENT_SAME_BOOK})"
                )
            if len(ops) > self.MAX_CONCURRENT_SAME_BOOK * 2:
                blocked = True

        # 2 — stesso importo ripetuto
        by_amount = self._group_by(batch, lambda o: round(o["stake_back"], 2))
        for amount, ops in by_amount.items():
            if amount > 0 and len(ops) > self.MAX_SAME_AMOUNT:
                warnings.append(
                    f"{len(ops)} operazioni con importo {amount} "
                    f"(soglia {self.MAX_SAME_AMOUNT})"
                )

        # 3 — troppe operazioni totali per ora
        by_hour = self._group_by(timed_ops, self._hour_key)
        for hour, ops in by_hour.items():
            if len(ops) > self.MAX_HOURLY_OPERATIONS:
                warnings.append(
                    f"{len(ops)} operazioni totali nell'ora {hour} "
                    f"(soglia {self.MAX_HOURLY_OPERATIONS})"
                )
            if len(ops) > self.MAX_HOURLY_OPERATIONS * 1.5:
                blocked = True

        # 4 — pattern temporali identici (spaziatura minima)
        sorted_ops = sorted(timed_ops, key=lambda o: o["opened_at"])
        for previous, current in zip(sorted_ops, sorted_ops[1:]):
            delta = (self._parse(current["opened_at"]) - self._parse(previous["opened_at"]))
            if delta.total_seconds() < self.MIN_OPERATION_SPACING:
                warnings.append(
                    f"operazioni a {delta.total_seconds():.0f}s di distanza "
                    f"(soglia {self.MIN_OPERATION_SPACING}s)"
                )

        risk_level = "high" if blocked else ("medium" if warnings else "low")
        return Result.ok(CorrelationCheck(risk_level=risk_level, warnings=warnings, blocked=blocked))

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by(items: list[dict], key_func: Callable[[dict], object]) -> dict:
        groups: dict = {}
        for item in items:
            groups.setdefault(key_func(item), []).append(item)
        return groups

    @staticmethod
    def _hour_key(op: dict) -> str:
        return op["opened_at"][:13]

    @staticmethod
    def _parse(opened_at: str) -> datetime:
        return datetime.fromisoformat(opened_at)


class OpSecScheduler:
    """Calcola quando un'operazione può essere eseguita, applicando temporal diversity."""

    PREFERRED_WINDOW_START_HOUR = 8    # inizio finestra preferita (08:00)
    PREFERRED_WINDOW_SPAN_HOURS = 12   # ampiezza finestra preferita (-> 08:00-19:00 circa)
    MAX_JITTER_MINUTES = 180           # jitter massimo (3 ore)

    def __init__(self, monitor: Optional[CorrelationMonitor] = None):
        self.monitor = monitor or CorrelationMonitor()

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def schedule_operation(self, identity_id: str, operation_type: str,
                            book: str, urgency: str = "normal") -> Result[str]:
        now = datetime.now()

        if urgency == "high":
            return Result.ok(now.isoformat())

        base_hour = self._preferred_base_hour(identity_id)
        scheduled = self._with_jitter(now, base_hour)
        if scheduled < now:
            scheduled += timedelta(days=1)

        if self._is_blocked(identity_id, operation_type, book, scheduled):
            scheduled += timedelta(hours=random.randint(1, 3))
            if self._is_blocked(identity_id, operation_type, book, scheduled):
                scheduled += timedelta(days=1)

        return Result.ok(scheduled.isoformat())

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _preferred_base_hour(self, identity_id: str) -> int:
        digest = hashlib.md5(identity_id.encode()).hexdigest()
        return self.PREFERRED_WINDOW_START_HOUR + (int(digest, 16) % self.PREFERRED_WINDOW_SPAN_HOURS)

    def _with_jitter(self, now: datetime, base_hour: int) -> datetime:
        jitter_minutes = random.randint(0, self.MAX_JITTER_MINUTES)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + timedelta(hours=base_hour, minutes=jitter_minutes)

    def _is_blocked(self, identity_id: str, operation_type: str, book: str, scheduled: datetime) -> bool:
        batch = [{
            "identity_id": identity_id,
            "book": book,
            "stake_back": 0.0,
            "opened_at": scheduled.isoformat(),
            "operation_type": operation_type,
        }]
        check = self.monitor.check_correlation_risk(batch)
        return check.success and check.data.blocked


class OpSec(OpSecInterface):
    """Orchestratore OpSec — sincrono, zero eccezioni."""

    OPSEC_BLOCKED = "OPSEC_BLOCKED"     # correlation risk troppo alto, operazione bloccata
    OPSEC_WARNING = "OPSEC_WARNING"     # correlation risk medio, operazione permessa con warning

    def __init__(self):
        self.monitor = CorrelationMonitor()
        self.scheduler = OpSecScheduler(monitor=self.monitor)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def schedule_operation(self, identity_id: str, operation_type: str,
                            book: str, urgency: str = "normal") -> Result[str]:
        return self.scheduler.schedule_operation(identity_id, operation_type, book, urgency)

    def check_correlation_risk(self, batch: list[dict]) -> Result[CorrelationCheck]:
        return self.monitor.check_correlation_risk(batch)
