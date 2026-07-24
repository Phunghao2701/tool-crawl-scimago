"""Standalone fallback lock used when the tool runs outside the parent repo."""

import atexit
import os
import subprocess
import sys
from pathlib import Path


LOCK_DIR = Path(__file__).resolve().parent.parent / "logs"


def _pid_is_alive(pid):
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            return True

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire(name):
    """Claim an exclusive PID lock and release it automatically on exit."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f".{name}.lock"

    for _attempt in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(descriptor, str(os.getpid()).encode())
            os.close(descriptor)
            break
        except FileExistsError:
            old_pid = None
            try:
                old_pid = int(lock_path.read_text().strip())
            except (OSError, ValueError):
                pass

            if old_pid and _pid_is_alive(old_pid):
                print(f"[LOCK] '{name}' is already running as PID {old_pid}.")
                raise SystemExit(1)

            try:
                lock_path.unlink()
            except OSError:
                pass
    else:
        print(f"[LOCK] Could not acquire '{name}' lock after retry.")
        raise SystemExit(1)

    def release():
        try:
            if lock_path.exists() and lock_path.read_text().strip() == str(os.getpid()):
                lock_path.unlink()
        except OSError:
            pass

    atexit.register(release)
