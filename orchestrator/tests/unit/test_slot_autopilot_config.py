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
