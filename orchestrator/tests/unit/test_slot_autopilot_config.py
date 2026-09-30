from decimal import Decimal

from orchestrator.modules.slot_autopilot.config import AutoPilotConfig


def _config(**overrides) -> AutoPilotConfig:
    defaults = dict(
        device_id="ZT322ZSJVS", slot_name="pirots5", target_played_amount=Decimal("30.00"),
    )
    defaults.update(overrides)
    return AutoPilotConfig(**defaults)


def test_defaults_validate_ok():
    result = _config().validate()
    assert result.success


def test_zero_target_played_amount_fails():
    result = _config(target_played_amount=Decimal("0")).validate()
    assert not result.success
    assert result.error_code == "CONFIG_INVALID"


def test_zero_max_steps_fails():
    result = _config(max_steps=0).validate()
    assert not result.success
    assert result.error_code == "CONFIG_INVALID"


def test_negative_frame_diff_stable_threshold_fails():
    result = _config(frame_diff_stable_threshold=-0.1).validate()
    assert not result.success
    assert result.error_code == "CONFIG_INVALID"


def test_zero_stable_reads_for_min_fails():
    result = _config(stable_reads_for_min=0).validate()
    assert not result.success
    assert result.error_code == "CONFIG_INVALID"


def test_zero_max_spin_attempts_per_step_fails():
    result = _config(max_spin_attempts_per_step=0).validate()
    assert not result.success
    assert result.error_code == "CONFIG_INVALID"


def test_burst_defaults_are_on_and_valid():
    config = _config()
    assert config.bet_burst_taps == 30 and config.bet_burst_interval_seconds == 0.4
    assert config.validate().success


def test_burst_of_one_tap_means_disabled_and_is_valid():
    assert _config(bet_burst_taps=1).validate().success


def test_zero_burst_taps_fails():
    result = _config(bet_burst_taps=0).validate()
    assert not result.success and result.error_code == "CONFIG_INVALID"


def test_negative_burst_interval_fails():
    result = _config(bet_burst_interval_seconds=-0.1).validate()
    assert not result.success and result.error_code == "CONFIG_INVALID"


def test_full_read_every_spins_default_and_validation():
    assert _config().full_read_every_spins == 10
    assert not _config(full_read_every_spins=0).validate().success


def test_burst_safety_check_defaults_and_validation():
    config = _config()
    assert config.bet_burst_check_every == 10 and config.bet_burst_abort_diff == 0.10
    assert not _config(bet_burst_check_every=0).validate().success
    assert not _config(bet_burst_abort_diff=0).validate().success
