from dataclasses import dataclass
from decimal import Decimal

from orchestrator.core.result import Result


@dataclass
class AutoPilotConfig:
    """Configurazione di una sessione di SlotAutoPilot. target_played_amount e'
    obbligatorio (nessun default): senza un tetto esplicito l'autopilot non parte."""
    device_id: str
    slot_name: str
    target_played_amount: Decimal
    max_steps: int = 500
    adb_timeout: int = 10
    tap_settle_seconds: float = 1.5
    spin_settle_seconds: float = 2.5
    stable_reads_for_min: int = 3
    max_bet_lower_attempts: int = 20
    max_spin_attempts_per_step: int = 3
    frame_diff_stable_threshold: float = 0.02
    frame_diff_poll_interval: float = 0.3
    frame_diff_max_wait_seconds: float = 5.0
    balance_rise_abort_margin: Decimal = Decimal("0.00")
    accounting_tolerance: Decimal = Decimal("0.01")
    enable_gpt_fallback: bool = True
    enable_hitl_fallback: bool = True
    require_profile: bool = True
    dataset_dir: str = "dataset"
    profiles_dir: str = "provider_profiles"
    slot_memory_dir: str = "slot_memory"
    dry_run: bool = False

    def validate(self) -> Result["AutoPilotConfig"]:
        if self.target_played_amount <= 0:
            return Result.fail("target_played_amount must be > 0", "CONFIG_INVALID")
        if self.max_steps <= 0:
            return Result.fail("max_steps must be > 0", "CONFIG_INVALID")
        if self.adb_timeout <= 0:
            return Result.fail("adb_timeout must be > 0", "CONFIG_INVALID")
        if self.tap_settle_seconds <= 0:
            return Result.fail("tap_settle_seconds must be > 0", "CONFIG_INVALID")
        if self.spin_settle_seconds <= 0:
            return Result.fail("spin_settle_seconds must be > 0", "CONFIG_INVALID")
        if self.stable_reads_for_min < 1:
            return Result.fail("stable_reads_for_min must be >= 1", "CONFIG_INVALID")
        if self.max_bet_lower_attempts < 1:
            return Result.fail("max_bet_lower_attempts must be >= 1", "CONFIG_INVALID")
        if self.max_spin_attempts_per_step < 1:
            return Result.fail("max_spin_attempts_per_step must be >= 1", "CONFIG_INVALID")
        if self.frame_diff_stable_threshold < 0:
            return Result.fail("frame_diff_stable_threshold must be >= 0", "CONFIG_INVALID")
        if self.frame_diff_poll_interval <= 0:
            return Result.fail("frame_diff_poll_interval must be > 0", "CONFIG_INVALID")
        if self.frame_diff_max_wait_seconds <= 0:
            return Result.fail("frame_diff_max_wait_seconds must be > 0", "CONFIG_INVALID")
        return Result.ok(self)
