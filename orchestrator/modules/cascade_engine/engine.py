import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.cascade_engine.interface import CascadeEngineInterface, CascadeState
from orchestrator.modules.multiplier_engine.interface import CascadeCalculation
from orchestrator.modules.touch_injector.interface import LayBetResult


class CascadeEngine(CascadeEngineInterface):
    """Motore state machine della cascata di copertura Betfair — sincrono, zero eccezioni."""

    CASCADE_NOT_FOUND = "CASCADE_NOT_FOUND"
    DEVICE_BUSY = "DEVICE_BUSY"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    BET_NOT_FOUND = "BET_NOT_FOUND"
    LAY_PLACEMENT_FAILED = "LAY_PLACEMENT_FAILED"
    MAX_RETRIES_EXCEEDED = "MAX_RETRIES_EXCEEDED"
    STATE_INCONSISTENT = "STATE_INCONSISTENT"
    ODDS_NEGATIVE_PROFIT = "ODDS_NEGATIVE_PROFIT"

    DEFAULT_MAX_RETRIES = 3
    ODDS_CHANGE_THRESHOLD = 0.05
    RETRY_BACKOFF_S = {1: 30, 2: 60}    # retry_count -> secondi; a retry_count>=max_retries -> hitl_blocked
    RECHECK_UNSETTLED_S = 30
    RECHECK_MATCHED_S = 60

    def __init__(self, screen_reader=None, touch_injector=None, multiplier_engine=None):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.multiplier_engine = multiplier_engine
        self._cascades: dict[str, CascadeState] = {}
        self._device_locks: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def start_cascade(self, operation: dict, calculation: CascadeCalculation) -> Result[str]:
        """Avvia la cascata: piazza la prima bancata su Betfair e registra lo stato."""
        cascade_id = str(uuid.uuid4())
        device_id = operation["device_id"]

        events = sorted(calculation.events, key=lambda e: e.index)
        state = CascadeState(
            cascade_id=cascade_id,
            operation_id=operation["operation_id"],
            identity_id=operation["identity_id"],
            device_id=device_id,
            markets=list(operation.get("markets", [])),
            current_event_index=0,
            state="pending",
            back_stake=calculation.back_stake,
            back_odds=list(calculation.back_odds),
            lay_odds=[e.lay_odds for e in events],
            lay_stakes=[e.lay_stake for e in events],
            liabilities=[e.liability for e in events],
            commission_rate=operation.get("commission_rate", calculation.commission),
            betfair_bet_ids=[],
            retry_count=0,
            max_retries=self.DEFAULT_MAX_RETRIES,
            next_check_at=self._iso_in(60),
            last_check_at=self._now_iso(),
        )

        if not self._acquire_device_lock(device_id):
            return Result.fail(f"Device {device_id} occupato", self.DEVICE_BUSY)

        place_result = self._place_lay(state, cascade_id, 0)
        if not place_result.success:
            state.state = "error"
            self._alert_hitl(cascade_id, f"Piazzamento prima bancata fallito: {place_result.error}")
            self._release_device_lock(device_id)
            self._cascades[cascade_id] = state
            return Result.fail(place_result.error, self.LAY_PLACEMENT_FAILED)

        state.betfair_bet_ids = [place_result.data.bet_id]
        state.state = self._waiting_state(0)
        self._release_device_lock(device_id)
        self._cascades[cascade_id] = state
        return Result.ok(cascade_id)

    def check_and_advance(self, cascade_id: str) -> Result[CascadeState]:
        """Controlla l'esito dell'evento corrente su Betfair e avanza la cascata se risolto."""
        state = self._cascades.get(cascade_id)
        if state is None:
            return Result.fail(f"Cascade {cascade_id} non trovata", self.CASCADE_NOT_FOUND)

        if state.state in ("completed", "error", "hitl_blocked"):
            return Result.ok(state)

        if not self._acquire_device_lock(state.device_id):
            return Result.fail(f"Device {state.device_id} occupato", self.DEVICE_BUSY)

        try:
            if self.screen_reader is None:
                return Result.fail("ScreenReader non configurato", self.STATE_INCONSISTENT)

            status_result = self.screen_reader.read_bet_status(state.device_id, betfair=True)

            if not status_result.success:
                self._register_failed_read(state, cascade_id)
            else:
                status = status_result.data.status
                if status == "settled_won":
                    state.retry_count = 0
                    self._handle_event_won(state)
                elif status == "settled_lost":
                    state.retry_count = 0
                    self._handle_event_lost(state, cascade_id, observed_lay_odds=status_result.data.lay_odds)
                elif status == "matched":
                    state.retry_count = 0
                    state.next_check_at = self._iso_in(self.RECHECK_MATCHED_S)
                elif status == "unsettled":
                    state.retry_count = 0
                    state.next_check_at = self._iso_in(self.RECHECK_UNSETTLED_S)
                elif status == "not_found":
                    state.state = "hitl_blocked"
                    self._alert_hitl(cascade_id, "Scommessa non trovata su Betfair (My Bets vuoto)")
                else:
                    self._register_failed_read(state, cascade_id)

            state.last_check_at = self._now_iso()
            self._cascades[cascade_id] = state
            return Result.ok(state)
        finally:
            self._release_device_lock(state.device_id)

    def recover_cascade(self, cascade_id: str) -> Result[CascadeState]:
        """Recovery di una cascata in stato error confrontando con lo stato reale su Betfair."""
        state = self._cascades.get(cascade_id)
        if state is None:
            return self._recover_from_reality(cascade_id)

        if state.state != "error":
            return Result.ok(state)

        if self.screen_reader is None:
            return Result.fail("ScreenReader non configurato", self.STATE_INCONSISTENT)

        if not self._acquire_device_lock(state.device_id):
            return Result.fail(f"Device {state.device_id} occupato", self.DEVICE_BUSY)

        try:
            status_result = self.screen_reader.read_bet_status(state.device_id, betfair=True)

            if not status_result.success:
                state.state = "hitl_blocked"
                self._alert_hitl(cascade_id, f"Recovery fallita: {status_result.error}")
            else:
                status = status_result.data.status
                if status == "settled_won":
                    state.retry_count = 0
                    self._handle_event_won(state)
                elif status == "settled_lost":
                    state.retry_count = 0
                    self._handle_event_lost(state, cascade_id, observed_lay_odds=status_result.data.lay_odds)
                else:
                    state.retry_count = 0
                    state.state = self._waiting_state(state.current_event_index)

            state.last_check_at = self._now_iso()
            self._cascades[cascade_id] = state
            return Result.ok(state)
        finally:
            self._release_device_lock(state.device_id)

    # ------------------------------------------------------------------
    # private: state machine
    # ------------------------------------------------------------------

    def _handle_event_won(self, state: CascadeState) -> None:
        """Bancata vinta -> cascata completa (in produzione aggiornerebbe operation/Netcap)."""
        state.state = "completed"

    def _handle_event_lost(
        self, state: CascadeState, cascade_id: str, observed_lay_odds: Optional[float] = None,
    ) -> None:
        """Bancata persa -> banca il prossimo evento, o completa se era l'ultimo."""
        state.current_event_index += 1

        if state.current_event_index >= len(state.lay_odds):
            # tutti gli eventi persi -> la multipla ha vinto sul book, copertura conclusa
            state.state = "completed"
            return

        if self.multiplier_engine is not None and observed_lay_odds is not None:
            planned = state.lay_odds[state.current_event_index - 1]
            if abs(observed_lay_odds - planned) > self.ODDS_CHANGE_THRESHOLD:
                recalc = self.multiplier_engine.recalculate_from_index(
                    state.back_stake, state.back_odds, state.lay_odds,
                    state.commission_rate, state.current_event_index,
                )
                if not recalc.success:
                    state.state = "hitl_blocked"
                    self._alert_hitl(cascade_id, f"Ricalcolo fallito: {recalc.error}")
                    return
                if not recalc.data.is_valid:
                    state.state = "hitl_blocked"
                    self._alert_hitl(cascade_id, "Ricalcolo produce profitto negativo")
                    return
                # recalculate_from_index reindicizza gli eventi da 0 -> offset sull'indice reale
                for event in recalc.data.events:
                    real_index = state.current_event_index + event.index
                    state.lay_odds[real_index] = event.lay_odds
                    state.lay_stakes[real_index] = event.lay_stake
                    state.liabilities[real_index] = event.liability

        place_result = self._place_lay(state, cascade_id, state.current_event_index)
        if not place_result.success:
            state.state = "error"
            self._alert_hitl(cascade_id, f"Piazzamento bancata fallito: {place_result.error}")
            return

        state.betfair_bet_ids.append(place_result.data.bet_id)
        state.state = self._waiting_state(state.current_event_index)
        state.next_check_at = self._iso_in(60)

    def _recover_from_reality(self, cascade_id: str) -> Result[CascadeState]:
        """Stub: nessuno stato in memoria per questo cascade_id."""
        return Result.fail(f"Cascade {cascade_id} non trovata", self.CASCADE_NOT_FOUND)

    def _register_failed_read(self, state: CascadeState, cascade_id: str) -> None:
        """Lettura stato scommessa fallita o status sconosciuto: incrementa i retry o blocca."""
        state.retry_count += 1
        if state.retry_count >= state.max_retries:
            state.state = "hitl_blocked"
            self._alert_hitl(
                cascade_id, f"Superati i retry massimi ({state.max_retries}) su lettura stato scommessa"
            )
        else:
            delay = self.RETRY_BACKOFF_S.get(state.retry_count, 120)
            state.next_check_at = self._iso_in(delay)

    # ------------------------------------------------------------------
    # private: helpers
    # ------------------------------------------------------------------

    def _place_lay(self, state: CascadeState, cascade_id: str, index: int) -> Result[LayBetResult]:
        """Piazza la bancata per l'evento index (simula successo se touch_injector è None)."""
        if self.touch_injector is None:
            return Result.ok(LayBetResult(
                confirmed=True,
                bet_id=f"SIM-{cascade_id}-{index}",
                actual_odds=state.lay_odds[index],
                actual_stake=state.lay_stakes[index],
            ))
        return self.touch_injector.place_lay_bet(
            state.device_id, state.markets[index], state.lay_odds[index], state.lay_stakes[index],
        )

    def _acquire_device_lock(self, device_id: str) -> bool:
        """Lock simulato via dict: True se acquisito, False se già occupato."""
        if self._device_locks.get(device_id):
            return False
        self._device_locks[device_id] = True
        return True

    def _release_device_lock(self, device_id: str) -> None:
        """Rilascia il lock simulato sul device."""
        self._device_locks.pop(device_id, None)

    def _alert_hitl(self, cascade_id: str, message: str) -> None:
        """Stub per HITL dashboard: per ora solo print."""
        print(f"[HITL ALERT] cascade={cascade_id}: {message}")

    def _won_state(self, index: int) -> str:
        return f"event_{index + 1}_won"

    def _layed_state(self, index: int) -> str:
        return f"event_{index + 1}_layed"

    def _waiting_state(self, index: int) -> str:
        return f"event_{index + 1}_waiting"

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _iso_in(self, seconds: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
