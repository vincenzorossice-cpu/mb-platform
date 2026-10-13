"""Input ADB usato dai recipe di login e dall'uscita dalla slot. Non tocca TouchInjector."""
import subprocess

from orchestrator.core.result import Result

KEYCODE_BACK = 4
KEYCODE_ENTER = 66


def press_key(device_id: str, keycode: int, timeout: int = 10) -> Result[bool]:
    """`adb -s <id> shell input keyevent <keycode>`."""
    try:
        proc = subprocess.run(
            ["adb", "-s", device_id, "shell", "input", "keyevent", str(keycode)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Result.fail(f"Timeout premendo il tasto {keycode} via ADB", "ADB_TIMEOUT")
    except Exception as e:
        return Result.fail(f"Tasto {keycode} via ADB fallito: {e}", "ADB_ERROR")
    if proc.returncode != 0:
        return Result.fail(f"Tasto {keycode} via ADB fallito: {(proc.stderr or '').strip()}", "ADB_ERROR")
    return Result.ok(True)


def press_enter(device_id: str, timeout: int = 10) -> Result[bool]:
    """Preme Enter (keyevent 66): chiude la tastiera / esegue l'azione IME."""
    return press_key(device_id, KEYCODE_ENTER, timeout)


def press_back(device_id: str, timeout: int = 10) -> Result[bool]:
    """Preme il tasto Back di Android (keyevent 4)."""
    return press_key(device_id, KEYCODE_BACK, timeout)
