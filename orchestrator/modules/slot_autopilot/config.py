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
    # 20s, non 5: alcune slot (es. Pirots5) hanno animazioni di vincita piu'
    # lunghe del normale - il ciclo di attesa esce comunque subito appena lo
    # schermo si stabilizza, quindi un tetto piu' alto non rallenta gli spin
    # normali, da' solo piu' pazienza a quelli con un'animazione lunga.
    frame_diff_max_wait_seconds: float = 20.0
    # Recupero da una schermata aperta per errore: "tornati" = lo schermo e'
    # uguale a quello prima del tap sbagliato entro questa differenza (piu'
    # larga della soglia di stabilita': un'animazione di sfondo non deve
    # impedire di riconoscere lo schermo originale).
    recovery_return_threshold: float = 0.06
    max_recovery_attempts: int = 4
    # Discesa della puntata: dopo UNA discesa verificata con una coordinata fidata si
    # tocca "in corsa" (N-1 tocchi ciechi + 1 verificato). 1 = nessuna corsa. Il
    # minimo e' un pavimento: tocchi in piu' sono innocui. Ogni `check_every` tocchi
    # uno screenshot (senza OCR) protegge da popup/cambi di schermata: se cambia piu'
    # di `abort_diff` la corsa si ferma.
    bet_burst_taps: int = 30
    bet_burst_interval_seconds: float = 0.4
    bet_burst_check_every: int = 10
    bet_burst_abort_diff: float = 0.10
    # Spin: ogni N spin una lettura OCR completa (lo schermo intero) anche se di
    # norma si legge solo la fascia di saldo/puntata.
    full_read_every_spins: int = 10
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
        if self.recovery_return_threshold <= 0:
            return Result.fail("recovery_return_threshold must be > 0", "CONFIG_INVALID")
        if self.max_recovery_attempts < 1:
            return Result.fail("max_recovery_attempts must be >= 1", "CONFIG_INVALID")
        if self.full_read_every_spins < 1:
            return Result.fail("full_read_every_spins must be >= 1", "CONFIG_INVALID")
        if self.bet_burst_check_every < 1:
            return Result.fail("bet_burst_check_every must be >= 1", "CONFIG_INVALID")
        if self.bet_burst_abort_diff <= 0:
            return Result.fail("bet_burst_abort_diff must be > 0", "CONFIG_INVALID")
        if self.bet_burst_taps < 1:
            return Result.fail("bet_burst_taps must be >= 1", "CONFIG_INVALID")
        if self.bet_burst_interval_seconds < 0:
            return Result.fail("bet_burst_interval_seconds must be >= 0", "CONFIG_INVALID")
        if self.frame_diff_max_wait_seconds <= 0:
            return Result.fail("frame_diff_max_wait_seconds must be > 0", "CONFIG_INVALID")
        return Result.ok(self)
