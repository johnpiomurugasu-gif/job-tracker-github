"""Housekeeping, run by its own launchd job every 5 minutes. It prints a line only when it does something.

  a) pop up    a Mac notification if logs/last_ok is more than 30 minutes old (once per outage, again after 4 hours)
  b) backup    db/backups/tracker_YYYYMMDD.db once a day, integrity checked, the newest 7 kept
  c) log trim  logs/sync.log over 5 MB is rotated to sync.log.1 .2 .3, only while holding the run lock

The state for (a) is in logs/housekeeping_state. Log text is searched for two words and never copied anywhere.
Standard library only.
"""
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from scheduler import runlock
from scheduler.runlock import ROOT, read_heartbeat

BACKUPS = ROOT / "db" / "backups"
STALE_SECONDS = 30 * 60          # no good run for this long: pop up
SLEEP_GAP_SECONDS = 10 * 60      # our own last check was this long ago: the Mac was asleep, skip one check
REPEAT_SECONDS = 4 * 3600        # still down: remind after this long
KEEP_BACKUPS = 7
BACKUP_TRIES, BACKUP_WAIT = 3, 2     # tries, seconds between them
BUSY_TIMEOUT = 10                # seconds
LOG_LIMIT = 5 * 1024 * 1024      # rotate sync.log above this many bytes


def now():
    return time.time()


def say(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now()))} {msg}", flush=True)


def load_state():
    try:
        state = json.loads((runlock.LOGS / "housekeeping_state").read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state):
    tmp = runlock.LOGS / "housekeeping_state.tmp"
    tmp.write_text(json.dumps(state))
    os.replace(tmp, runlock.LOGS / "housekeeping_state")


# --- a) pop up -------------------------------------------------------------------------------------------------

def cause():
    """The likely reason, in fixed wording, from the last 100 lines of sync.log. It only looks for two words."""
    try:
        with open(runlock.LOGS / "sync.log", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            tail = f.read().decode("utf-8", "replace").splitlines()[-100:]
    except OSError:
        tail = []
    text = "\n".join(tail)
    grant, aborted = text.rfind("invalid_grant"), text.rfind("ABORTED")      # the later one is the current cause
    if grant > aborted:
        return "Gmail token expired, renew it."
    if aborted > grant:
        return "Ollama seems to be down."
    return "No good sync run for 30 minutes."


def notify(title, message):
    """Mac notification through osascript. Returns True if it was sent."""
    try:
        subprocess.run(["/usr/bin/osascript", "-e", f'display notification "{message}" with title "{title}"'],
                       check=True, capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def check_alive(state, t):
    last = read_heartbeat()
    if last is None:
        stale = t - state["first_seen"] > STALE_SECONDS      # no heartbeat yet: wait 30 minutes after our own first run
    else:
        stale = t - last > STALE_SECONDS
    if not stale:
        if state.pop("alerted_at", None) is not None:
            say("sync is running again; alert cleared")
        return
    if state.get("alerted_at") is None or t - state["alerted_at"] >= REPEAT_SECONDS:
        why = cause()
        if notify("Job tracker stopped", why):
            state["alerted_at"] = t
            say(f"notified: {why}")
        else:
            say("could not send the notification")


# --- b) daily backup -------------------------------------------------------------------------------------------

def daily_backups():
    """The daily backup files, oldest first. Only names of the form tracker_YYYYMMDD.db count as ours."""
    return sorted(p for p in BACKUPS.glob("tracker_*.db") if re.fullmatch(r"tracker_\d{8}\.db", p.name))


def give_up_after(seconds):
    """Connection.backup() waits for a locked source for ever; this progress callback ends the wait after `seconds`."""
    end = time.monotonic() + seconds

    def progress(status, remaining, total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() > end:
            raise sqlite3.OperationalError("database stayed locked")
    return progress


def daily_backup(t):
    final = BACKUPS / time.strftime("tracker_%Y%m%d.db", time.localtime(t))
    if final.exists():
        return
    BACKUPS.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + ".tmp")
    for attempt in range(BACKUP_TRIES):
        tmp.unlink(missing_ok=True)
        try:
            src = sqlite3.connect(Path(runlock.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst, progress=give_up_after(BUSY_TIMEOUT), sleep=0.1)
                finally:
                    dst.close()
            finally:
                src.close()
            break
        except sqlite3.Error:
            if attempt + 1 == BACKUP_TRIES:
                tmp.unlink(missing_ok=True)
                say("backup postponed: the database is busy; will try again next run")
                return
            time.sleep(BACKUP_WAIT)
    chk = sqlite3.connect(tmp)
    try:
        verdict = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if verdict != "ok":
        tmp.unlink(missing_ok=True)
        say("backup FAILED the integrity check and was removed; older backups are untouched")
        return
    os.replace(tmp, final)
    for old in daily_backups()[:-KEEP_BACKUPS]:         # only after the new one checked out
        old.unlink()
    say(f"daily backup made: {final.name} (integrity ok); {len(daily_backups())} kept")


# --- c) log trim -----------------------------------------------------------------------------------------------

def trim_log():
    log = runlock.LOGS / "sync.log"
    try:
        size = log.stat().st_size
    except OSError:
        return
    if size <= LOG_LIMIT:
        return
    lock = runlock.try_lock()
    if lock is None:                 # a run is going; try again in 5 minutes
        return
    try:
        for i in (2, 1):             # .2 -> .3 (the old .3 is replaced), .1 -> .2
            older = runlock.LOGS / f"sync.log.{i}"
            if older.exists():
                os.replace(older, runlock.LOGS / f"sync.log.{i + 1}")
        os.replace(log, runlock.LOGS / "sync.log.1")      # launchd opens sync.log afresh for every run
    finally:
        lock.close()
    say(f"rotated sync.log ({size // 1024} KB) to sync.log.1")


def duty(fn, *args):
    try:
        fn(*args)
    except Exception as exc:         # one failing duty must not stop the others
        say(f"{fn.__name__} failed: {type(exc).__name__}")


def main():
    t = now()
    state = load_state()
    state.setdefault("first_seen", t)
    asleep = t - state.get("last_check", t) > SLEEP_GAP_SECONDS      # the Mac slept: the last good run is stale for a reason
    state["last_check"] = t
    if not asleep:
        duty(check_alive, state, t)
    duty(save_state, state)
    duty(daily_backup, t)
    duty(trim_log)


if __name__ == "__main__":
    main()
