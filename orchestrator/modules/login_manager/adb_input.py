"""Input ADB usato dai recipe di login (Mapper ed Executor). Non tocca TouchInjector."""
import subprocess

from orchestrator.core.result import Result

KEYCODE_ENTER = 66


def press_enter(device_id: str, timeout: int = 10) -> Result[bool]:
    """Preme Enter (keyevent 66): chiude la tastiera / esegue l'azione IME."""
    try:
        proc = subprocess.run(
            ["adb", "-s", device_id, "shell", "input", "keyevent", str(KEYCODE_ENTER)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Result.fail("Timeout premendo Enter via ADB", "ADB_TIMEOUT")
    except Exception as e:
        return Result.fail(f"Enter via ADB fallito: {e}", "ADB_ERROR")
    if proc.returncode != 0:
        return Result.fail(f"Enter via ADB fallito: {(proc.stderr or '').strip()}", "ADB_ERROR")
    return Result.ok(True)
