"""Autospin e Turbo dentro la sessione di gioco: esplorazione (solo demo, dopo la puntata minima verificata), misura
del turbo con i tempi degli spin normali, cicli di autospin con fermata al 90% del target e tap manuale per il finale.
Ogni dubbio = tap manuale (il comportamento di sempre). Tutto e' no-op senza SlotSetup o con la configurazione spenta."""
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from orchestrator.modules.slot_autopilot.config import AutoPilotConfig

_DEFAULT_PERIOD = 6.0
_MAX_CYCLES_FAILED = 1             # un cycle fallito spegne l'autospin per il resto della sessione


@dataclass
class CycleOutcome:
    played: Decimal = Decimal("0")
    spins: int = 0
    stop_reason: Optional[str] = None      # se non None la sessione deve fermarsi con questo motivo
    recheck_bet: bool = False              # la puntata puo' essere cambiata: set_min_bet prima di proseguire


class SpeedupController:
    def __init__(self, autopilot, setup, device_id: str, slot_name: str, target: Decimal, log: logging.Logger):
        config = getattr(autopilot, "config", None)
        self.config = config if isinstance(config, AutoPilotConfig) else None
        self.autopilot, self.setup, self.device_id, self.slot_name = autopilot, setup, device_id, slot_name
        self.target, self.log = target, log
        self.usable = self.config is not None and setup is not None and callable(getattr(setup, "autospin_info", None))
        self.autospin = None                 # AutospinInfo se si puo' usare
        self._autospin_off = False
        self._turbo_phase: Optional[str] = None        # None | "before" | "after"
        self._turbo_info = None
        self._times_before: list = []
        self._times_after: list = []
        self._seconds: list = []             # secondi degli spin normali (periodo di riferimento per l'autospin)

    # ---- preparazione (dopo la puntata minima verificata) ----------------------------------------------------------
    def prepare(self) -> Optional[str]:
        """Esplora l'autospin e prepara il turbo. Ritorna un motivo di arresto (stringa) o None."""
        if not self.usable:
            return None
        if self.config.autospin_enabled:
            found = self.setup.explore_autospin(self.device_id, self.slot_name)
            if not found.success and found.error_code == "AUTOSPIN_RUNNING":
                self.log.error("Autospin avviato in esplorazione e non fermabile: mi fermo.")
                return "AUTOSPIN_NOT_STOPPED"
            info = found.data if found.success else self.setup.autospin_info(self.slot_name)
            if info is not None and info.usable():
                self.autospin = info
            else:
                self.log.info("Autospin: non disponibile, uso tap manuale")
        if self.config.turbo_enabled:
            turbo = self.setup.turbo_info(self.slot_name)
            if turbo is not None and turbo.status == "present" and turbo.verified:
                manager = getattr(getattr(self.setup, "explorer", None), "turbo_manager", None)
                if manager is not None:
                    manager.enable_turbo(self.device_id, turbo)
                    self.log.info("Turbo: attivato (%.1f s -> %.1f s per spin nella misura salvata)",
                                  turbo.seconds_without or 0, turbo.seconds_with or 0)
            elif (turbo is None or (turbo.status == "unknown" and turbo.attempts < 2)) and getattr(self.setup, "demo", False):
                self._turbo_phase = "before"
        return None

    # ---- turbo: misura con gli spin normali -------------------------------------------------------------------------
    def note_spin(self, seconds: float) -> None:
        """Un spin normale riuscito. Raccoglie i tempi e guida la misura del turbo (5 senza, attiva, 5 con)."""
        if not self.usable:
            return
        self._seconds.append(float(seconds))
        need = self.config.speedup_measure_spins + 1          # il primo spin di ogni gruppo non si conta (apprendimenti)
        if self._turbo_phase == "before":
            self._times_before.append(seconds)
            if len(self._times_before) >= need:
                found = self.setup.explore_turbo(self.device_id, self.slot_name)
                if found.success and found.data.status == "present":
                    self._turbo_info, self._turbo_phase = found.data, "after"
                else:
                    self._turbo_phase = None
                    if found.success:
                        self.setup.save_turbo(self.slot_name, found.data)
        elif self._turbo_phase == "after":
            self._times_after.append(seconds)
            if len(self._times_after) >= need:
                manager = self.setup.explorer.turbo_manager
                without = manager.measure_spin_time(self._times_before[1:])
                with_turbo = manager.measure_spin_time(self._times_after[1:])
                self.setup.conclude_turbo(self.device_id, self.slot_name, self._turbo_info, without, with_turbo)
                self._turbo_phase = None

    @property
    def measuring_turbo(self) -> bool:
        return self._turbo_phase is not None

    # ---- autospin ------------------------------------------------------------------------------------------------------
    def period(self) -> float:
        if self.autospin is not None and self.autospin.period_seconds:
            return float(self.autospin.period_seconds)
        recent = self._seconds[-20:]
        return sum(recent) / len(recent) if recent else _DEFAULT_PERIOD

    def maybe_cycle(self, total_played: Decimal, spin_index: int, max_steps: int, free_spins: bool,
                    spins_done: int, bet: Optional[Decimal]) -> Optional[CycleOutcome]:
        """Un ciclo di autospin se conviene; None = si prosegue col tap manuale."""
        if (not self.usable or self.autospin is None or self._autospin_off or free_spins or spins_done < 1
                or self._turbo_phase is not None or bet is None or bet <= 0):
            return None
        limit = self.target * Decimal(str(self.config.autospin_stop_threshold))
        if total_played >= limit or spin_index >= max_steps:
            return None
        result = self.autopilot.autospin_cycle(
            self.device_id, self.autospin, bet, limit - total_played, max_steps - spin_index, self.period())
        if result is None:
            self._autospin_off = True
            return None
        outcome = CycleOutcome(played=result.credited_played, spins=result.credited_spins)
        self.log.info("Autospin: %d giri accreditati (prudente), %s EUR, %d cicli (%s).", result.credited_spins,
                      result.credited_played, result.cycles, result.reason)
        if result.reason == "stop_failed":
            self.log.error("Autospin non fermato (%s): mi fermo.", result.message)
            outcome.stop_reason = "AUTOSPIN_NOT_STOPPED"
        elif result.reason in ("threshold", "budget"):
            self._autospin_off = True
            self.log.info("Autospin: fermato al %d%% del target, passo a tap manuale",
                          int(self.config.autospin_stop_threshold * 100))
        else:
            self._autospin_off = True
            outcome.recheck_bet = result.reason == "bet_changed"
            self.log.warning("Autospin interrotto (%s: %s): da qui tap manuale.", result.reason, result.message)
        return outcome
