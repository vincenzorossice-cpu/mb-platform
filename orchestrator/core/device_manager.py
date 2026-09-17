import re
import subprocess
from typing import Optional


class DeviceManager:
    """Gestisce i telefoni Android via ADB diretto (12 telefoni, 1 PC)."""

    def __init__(self, adb_timeout: int = 10):
        self.adb_timeout = adb_timeout
        self._devices: dict[str, dict] = {}  # device_id -> {"online": bool, "battery": Optional[int]}
        self._locks: dict[str, Optional[str]] = {}  # device_id -> operation_id (or None)

    def discover_devices(self) -> list[str]:
        """Esegue `adb devices` e restituisce lista di device_id online."""
        try:
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=self.adb_timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

        device_ids = []
        for line in result.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                device_ids.append(parts[0])
        return device_ids

    def check_online(self, device_id: str) -> bool:
        """Verifica se un dispositivo è raggiungibile via ADB."""
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "echo", "ok"],
                capture_output=True,
                text=True,
                timeout=self.adb_timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return result.returncode == 0 and "ok" in result.stdout

    def read_battery(self, device_id: str) -> Optional[int]:
        """Legge livello batteria: adb -s device_id shell dumpsys battery | grep level"""
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "dumpsys", "battery"],
                capture_output=True,
                text=True,
                timeout=self.adb_timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

        if result.returncode != 0:
            return None

        match = re.search(r"level:\s*(\d+)", result.stdout)
        return int(match.group(1)) if match else None

    def acquire_lock(self, device_id: str, operation_id: str) -> bool:
        """Acquisisce lock su un device (una operazione alla volta)."""
        if device_id in self._locks and self._locks[device_id] is not None:
            return False
        self._locks[device_id] = operation_id
        return True

    def release_lock(self, device_id: str):
        """Rilascia il lock."""
        self._locks[device_id] = None

    def heartbeat_all(self) -> dict:
        """Check tutti i dispositivi: online/offline + batteria.
        Returns: {device_id: {"online": bool, "battery": Optional[int]}}
        """
        report = {}
        for device_id in self.discover_devices():
            online = self.check_online(device_id)
            battery = self.read_battery(device_id) if online else None
            self._devices[device_id] = {"online": online, "battery": battery}
            report[device_id] = {"online": online, "battery": battery}
        return report

    def get_device_for_identity(self, identity_id: str, config: dict) -> Optional[str]:
        """Map identity_id → device_id dal config."""
        for identity in config.get("identities", []):
            if identity["id"] == identity_id:
                return identity.get("device_id")
        return None
