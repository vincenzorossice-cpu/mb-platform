import subprocess
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.device_manager import DeviceManager


@pytest.fixture
def manager() -> DeviceManager:
    return DeviceManager(adb_timeout=5)


# ------------------------------------------------------------------
# discover_devices
# ------------------------------------------------------------------

class TestDiscoverDevices:
    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_discover_devices_parses_online(self, mock_run, manager):
        """`adb devices` con 2 device online -> lista dei 2 id."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\nZE223GVFJ2\tdevice\nZE223HNBKJ\tdevice\n\n",
        )

        result = manager.discover_devices()

        assert result == ["ZE223GVFJ2", "ZE223HNBKJ"]

    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_discover_devices_skips_offline_unauthorized(self, mock_run, manager):
        """Device offline/unauthorized non vengono inclusi."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\nZE223GVFJ2\tdevice\nABC123\toffline\nDEF456\tunauthorized\n",
        )

        result = manager.discover_devices()

        assert result == ["ZE223GVFJ2"]

    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_discover_devices_timeout(self, mock_run, manager):
        """Timeout ADB -> lista vuota, nessuna eccezione propagata."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["adb", "devices"], timeout=5)

        result = manager.discover_devices()

        assert result == []

    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_discover_devices_adb_not_found(self, mock_run, manager):
        """adb non in PATH -> lista vuota."""
        mock_run.side_effect = FileNotFoundError()

        result = manager.discover_devices()

        assert result == []


# ------------------------------------------------------------------
# check_online
# ------------------------------------------------------------------

class TestCheckOnline:
    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_check_online_true(self, mock_run, manager):
        """echo ok con returncode 0 -> True."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n")

        assert manager.check_online("ZE223GVFJ2") is True

    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_check_online_false_nonzero_returncode(self, mock_run, manager):
        """returncode != 0 -> False."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        assert manager.check_online("ZE223GVFJ2") is False

    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_check_online_timeout(self, mock_run, manager):
        """Timeout -> False."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["adb"], timeout=5)

        assert manager.check_online("ZE223GVFJ2") is False


# ------------------------------------------------------------------
# read_battery
# ------------------------------------------------------------------

class TestReadBattery:
    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_read_battery_parses_level(self, mock_run, manager):
        """dumpsys battery con 'level: 85' -> 85."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Current Battery Service state:\n  AC powered: false\n  level: 85\n  scale: 100\n",
        )

        assert manager.read_battery("ZE223GVFJ2") == 85

    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_read_battery_no_match(self, mock_run, manager):
        """Output senza 'level:' -> None."""
        mock_run.return_value = MagicMock(returncode=0, stdout="unexpected output")

        assert manager.read_battery("ZE223GVFJ2") is None

    @patch("orchestrator.core.device_manager.subprocess.run")
    def test_read_battery_error_returncode(self, mock_run, manager):
        """returncode != 0 -> None."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        assert manager.read_battery("ZE223GVFJ2") is None


# ------------------------------------------------------------------
# lock management
# ------------------------------------------------------------------

class TestLocks:
    def test_acquire_lock_free(self, manager):
        """Device senza lock -> acquisizione riuscita."""
        assert manager.acquire_lock("dev-1", "op-1") is True

    def test_acquire_lock_busy(self, manager):
        """Device già locked -> acquisizione fallisce."""
        manager.acquire_lock("dev-1", "op-1")

        assert manager.acquire_lock("dev-1", "op-2") is False

    def test_release_then_reacquire(self, manager):
        """Dopo release, il lock può essere riacquisito."""
        manager.acquire_lock("dev-1", "op-1")
        manager.release_lock("dev-1")

        assert manager.acquire_lock("dev-1", "op-2") is True


# ------------------------------------------------------------------
# heartbeat_all
# ------------------------------------------------------------------

class TestHeartbeatAll:
    def test_heartbeat_all_reports_online_and_battery(self, manager):
        """3 device scoperti, 2 online con batteria, 1 offline -> report coerente."""
        manager.discover_devices = MagicMock(return_value=["dev-1", "dev-2", "dev-3"])
        manager.check_online = MagicMock(side_effect=[True, True, False])
        manager.read_battery = MagicMock(side_effect=[80, 55])

        report = manager.heartbeat_all()

        assert report == {
            "dev-1": {"online": True, "battery": 80},
            "dev-2": {"online": True, "battery": 55},
            "dev-3": {"online": False, "battery": None},
        }
        assert manager._devices == report


# ------------------------------------------------------------------
# get_device_for_identity
# ------------------------------------------------------------------

class TestGetDeviceForIdentity:
    def test_get_device_for_identity_found(self, manager):
        config = {"identities": [{"id": "01", "device_id": "ZE223GVFJ2"}]}

        assert manager.get_device_for_identity("01", config) == "ZE223GVFJ2"

    def test_get_device_for_identity_not_found(self, manager):
        config = {"identities": [{"id": "01", "device_id": "ZE223GVFJ2"}]}

        assert manager.get_device_for_identity("99", config) is None
