"""Run lock, heartbeat and shared paths for the sync, the switch and housekeeping (standard library only).

Lock: fcntl.flock on logs/run.lock, taken without waiting and held for the whole run. The system releases it when the
process ends, even after kill -9, so it can never go stale.
Heartbeat: logs/last_ok holds the time of the last good run (line 1: ISO time with offset, line 2: epoch seconds).
"""
import fcntl
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
DB_PATH = Path(os.environ.get("GMAIL_DB_PATH", ROOT / "db" / "tracker.db"))   # same rule as ingest/gmail_client.py


def try_lock():
    """Take the run lock without waiting. Returns the open lock file (keep it referenced until the run ends),
    or None if another process holds it."""
    LOGS.mkdir(exist_ok=True)
    f = open(LOGS / "run.lock", "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def lock_is_free():
    """True if no run holds the lock right now (takes it for an instant to find out)."""
    f = try_lock()
    if f is None:
        return False
    f.close()
    return True


def write_heartbeat():
    """Record a good run. Written to a temp file first, then os.replace, so a reader never sees half a file."""
    now = datetime.now().astimezone()
    tmp = LOGS / "last_ok.tmp"
    tmp.write_text(f"{now.isoformat(timespec='seconds')}\n{int(now.timestamp())}\n")
    os.replace(tmp, LOGS / "last_ok")


def read_heartbeat():
    """Epoch seconds of the last good run, or None if logs/last_ok is missing or unreadable."""
    try:
        return int((LOGS / "last_ok").read_text().splitlines()[1])
    except (OSError, IndexError, ValueError):
        return None
