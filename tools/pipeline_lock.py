"""
PID-based lock so two instances of the same write-heavy pipeline script never
run concurrently against the same target DB.

Root incident this prevents: two agents (or one agent run twice) launched
`migrate_local_to_vercel.py` at the same time. Both reported success, but
neither's inserts actually accumulated — concurrent writers into the same
tables silently raced each other. A lock makes the second launch refuse to
start instead of racing.

Usage (top of a script's main()):
    from pipeline_lock import acquire
    acquire("migrate_local_to_vercel")
"""
import atexit
import os
import subprocess
import sys

LOCK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _pid_is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in out.stdout
        except Exception:
            return True  # can't verify -> assume alive, fail safe (refuse to start)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def acquire(name: str) -> None:
    """Exit the process immediately if another live instance already holds
    this lock. Otherwise claim it; it is released automatically at exit
    (normal or via exception) through atexit.

    Uses O_CREAT|O_EXCL for the actual claim so two processes racing to
    create the lock at the same instant can't both "win" (plain
    exists-then-write has that race; exclusive create does not)."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, f".{name}.lock")

    for attempt in range(2):  # 1 retry, after clearing a confirmed-stale lock
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            old_pid = None
            try:
                with open(lock_path) as f:
                    old_pid = int(f.read().strip())
            except (ValueError, OSError):
                pass

            if old_pid and _pid_is_alive(old_pid):
                print(f"[LOCK] '{name}' is already running as PID {old_pid}.")
                print(f"[LOCK] Tail its log under logs/ instead of starting a second instance.")
                print(f"[LOCK] If PID {old_pid} is genuinely dead (crashed without cleanup), "
                      f"stop it if it still shows in tasklist, then delete {lock_path} and retry.")
                sys.exit(1)

            print(f"[LOCK] Stale lock from dead PID {old_pid}, removing.")
            try:
                os.remove(lock_path)
            except OSError:
                pass
    else:
        print(f"[LOCK] Could not acquire '{name}' lock after retry.")
        sys.exit(1)

    def _release():
        try:
            if os.path.exists(lock_path):
                with open(lock_path) as f:
                    if f.read().strip() == str(os.getpid()):
                        os.remove(lock_path)
        except OSError:
            pass

    atexit.register(_release)
