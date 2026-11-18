"""The switch for the new filing path (build stage 1 made the switch; build stage 5 makes "on" work).

    venv/bin/python -m scheduler.resolver_switch off|on|status [--db PATH] [--lock PATH]

The mode lives in sync_state.resolver_mode. run_sync reads it first: anything but 'on' (including a missing key or an
unreadable database) means today's path.

  on      runs every check below, prints EACH of them as ok or FAILED (not just the first failure), and turns the mode on in ONE
          transaction only if all pass; otherwise the mode is not changed. The checks:
            1  the database is migrated (schema version 6 or more)
            2  sync_state.import_saved_done is set (the one-off import was applied)
            3  the four seed names are on the Discarded list
            4  sync_state.resolver_cursor is set (the new path knows where to start)
            5  no run is in progress: the run lock is free (taken without waiting, and held while the mode is changed)
            6  scheduler.resolver_run imports cleanly in a fresh interpreter
            7  secrets/tavily_key.txt exists and is not empty (never printed)
            8  a database backup from today exists (db/tracker.db.backup-YYYYMMDD-* or db/backups/tracker_YYYYMMDD.db)
  off     turns the mode off (today's path).
  status  prints the mode, the schema version, the last good run, the run lock and the newest daily backup.

  --db PATH    act on this database (default db/tracker.db, or GMAIL_DB_PATH): a rehearsal on a copy.
  --lock PATH  the run lock file to look at (default logs/run.lock; with --db on a copy it defaults to PATH.lock, so a copy never touches logs/run.lock).
"""
import sqlite3
import sys
import time
from pathlib import Path

from scheduler.runlock import DB_PATH, lock_is_free, read_heartbeat

BUSY_TIMEOUT = 10        # seconds
ROOT = Path(__file__).resolve().parent.parent
TAVILY_KEY = ROOT / "secrets" / "tavily_key.txt"
DB_DIR = ROOT / "db"                     # where today's backups are looked for
RESOLVER_RUN = "scheduler.resolver_run"
USAGE = "usage: venv/bin/python -m scheduler.resolver_switch off|on|status [--db PATH] [--lock PATH]"


def read_mode(db_path):
    """The stored resolver_mode value, or None if the key, the table or the database is missing or unreadable."""
    try:
        c = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
        try:
            row = c.execute("SELECT value FROM sync_state WHERE key = 'resolver_mode'").fetchone()
        finally:
            c.close()
    except Exception:
        return None
    return None if row is None else row[0]


def get_mode(db_path):
    """'on' only if the stored value says so; everything else is 'off'."""
    return "on" if str(read_mode(db_path) or "").strip().lower() == "on" else "off"


def schema_version(db_path):
    try:
        c = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
        try:
            return c.execute("PRAGMA user_version").fetchone()[0]
        finally:
            c.close()
    except Exception:
        return "unknown"


def _lock_path(db, lock):
    """The lock file to use: --lock, else None (logs/run.lock) for the live database and PATH.lock beside a copy (a copy never touches logs/run.lock)."""
    if lock:
        return Path(lock)
    return None if Path(db).resolve() == Path(DB_PATH).resolve() else Path(str(db) + ".lock")


def _take_lock(path):
    """The run lock without waiting: the open file, or None if a run holds it. path None = logs/run.lock."""
    if path is None:
        from scheduler.runlock import try_lock
        return try_lock()
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def status(db=None, lock=None, explicit=False):
    from scheduler.housekeeping import daily_backups     # imported here so the sync never loads housekeeping
    db = DB_PATH if db is None else db
    raw = read_mode(db)
    last = read_heartbeat()
    backups = daily_backups()
    day = backups[-1].name[8:16] if backups else None
    if explicit:
        print(f"database: {db}")
    print(f"mode: {get_mode(db)}" + (" (not set)" if raw is None else ""))
    print(f"schema version: {schema_version(db)}")
    print("last good run: " + ("never (logs/last_ok not written yet)" if last is None else f"{(time.time() - last) / 60:.0f} minutes ago"))
    if lock is None:
        free = lock_is_free()
    else:
        held = _take_lock(lock)
        free = held is not None
        if held is not None:
            held.close()
    print("run lock: " + ("free" if free else "held (a run is going)"))
    print("newest daily backup: " + (f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else "none yet"))


# --- on: the checks ---------------------------------------------------------------------------------------------------
def _db_checks(conn):
    """Checks 1 to 4, read inside the transaction that turns the mode on. [(label, ok, detail)]"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    try:
        state = dict(conn.execute("SELECT key, value FROM sync_state"))
    except sqlite3.Error:
        state = {}
    out = [("the database is migrated", isinstance(version, int) and version >= 6, f"schema version {version}, needs 6 or more")]
    done = state.get("import_saved_done")
    out.append(("the one-off import is recorded (sync_state.import_saved_done)", bool(done), "set" if done else "not set: run the import first"))
    try:
        from ingest.sender_resolver import SEED_DISCARDED_NAMES, discard_key
        have = {r[0] for r in conn.execute("SELECT name_key FROM discarded_companies")}
        missing = [n for n in SEED_DISCARDED_NAMES if discard_key(n) not in have]
        out.append(("the four seed names are on the Discarded list", not missing, f"missing: {', '.join(missing)}" if missing else f"{len(SEED_DISCARDED_NAMES)} of {len(SEED_DISCARDED_NAMES)} present"))
    except Exception as exc:
        out.append(("the four seed names are on the Discarded list", False, f"could not check ({type(exc).__name__})"))
    cursor = state.get("resolver_cursor")
    ok = str(cursor or "").isdigit()
    out.append(("the new path's cursor is set (sync_state.resolver_cursor)", ok, f"{cursor} ({time.strftime('%Y-%m-%d %H:%M', time.gmtime(int(cursor)))} UTC)" if ok else "not set"))
    return out


def _import_check():
    import subprocess
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); import {RESOLVER_RUN}"
    try:
        r = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return False, f"could not start ({type(exc).__name__})"
    if r.returncode == 0:
        return True, "imports cleanly"
    last = (r.stderr.strip().splitlines() or ["?"])[-1].split(":")[0]
    return False, f"does not import ({last})"


def _key_check():
    try:
        ok = TAVILY_KEY.is_file() and TAVILY_KEY.read_text().strip() != ""
    except OSError:
        ok = False
    return ok, "present, not empty (not shown)" if ok else "missing or empty"


def _backup_check():
    day = time.strftime("%Y%m%d")
    found = sorted(DB_DIR.glob(f"tracker.db.backup-{day}*")) + sorted((DB_DIR / "backups").glob(f"tracker_{day}.db"))
    found = [p for p in found if p.is_file() and p.stat().st_size > 0]
    return bool(found), (f"{found[-1].name}" if found else f"none dated {day[:4]}-{day[4:6]}-{day[6:]}")


DB_LABELS = ("the database is migrated", "the one-off import is recorded (sync_state.import_saved_done)", "the four seed names are on the Discarded list",
             "the new path's cursor is set (sync_state.resolver_cursor)")


def on(db, lock_path):
    """Run every check, print each, and turn the mode on in one transaction if they all pass. 0 = on, 1 = refused (nothing changed)."""
    lock = conn = None
    results = []
    try:
        have_db = Path(db).is_file()                      # a database that is not there is never created, and no lock file is made for it
        lock = _take_lock(lock_path) if have_db else None
        if not have_db:
            db_results = [(label, False, "the database file does not exist") for label in DB_LABELS]
        else:
            try:
                conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=rw", uri=True, timeout=BUSY_TIMEOUT, isolation_level=None)   # never creates a database
                conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT * 1000}")
                conn.execute("BEGIN IMMEDIATE")           # the checks of the database are read inside the transaction that turns the mode on
                db_results = _db_checks(conn)
            except sqlite3.Error as exc:
                db_results = [(label, False, f"cannot use the database ({type(exc).__name__})") for label in DB_LABELS]
        results += db_results
        results.append(("no run is in progress (the run lock is free)", lock is not None,
                        ("free" if lock is not None else "a run holds the lock: try again when it has finished") if have_db else "not checked: the database file does not exist"))
        results.append(("scheduler.resolver_run imports in a fresh interpreter", *_import_check()))
        results.append(("the Tavily key file exists and is not empty", *_key_check()))
        results.append(("a database backup from today exists", *_backup_check()))
        print(f"resolver_switch on (database: {db}):")
        for label, ok, detail in results:
            print(f"  {'ok    ' if ok else 'FAILED'}  {label}: {detail}")
        failed = [label for label, ok, _ in results if not ok]
        if failed:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
            print(f"resolver_mode was not changed: {len(failed)} of {len(results)} checks FAILED.")
            return 1
        conn.execute("INSERT INTO sync_state (key, value) VALUES ('resolver_mode', 'on') ON CONFLICT(key) DO UPDATE SET value = 'on'")
        conn.execute("COMMIT")
        print(f"resolver_mode is on: all {len(results)} checks passed, the sync runs the new filing path.")
        return 0
    except sqlite3.Error as exc:
        if conn is not None and conn.in_transaction:
            conn.execute("ROLLBACK")
        print(f"could not set the mode: {exc}")
        return 1
    finally:
        if conn is not None:
            conn.close()
        if lock is not None:
            lock.close()


def off(db):
    try:
        c = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=rw", uri=True, timeout=BUSY_TIMEOUT)    # never creates a database
        try:
            c.execute("INSERT INTO sync_state (key, value) VALUES ('resolver_mode', 'off') "
                      "ON CONFLICT(key) DO UPDATE SET value = 'off'")
            c.commit()
        finally:
            c.close()
    except sqlite3.Error as exc:
        print(f"could not set the mode: {exc}")
        return 1
    print("resolver_mode is off: the sync runs today's path only.")
    return 0


def _parse(argv):
    """(command, {'db': ..., 'lock': ...}) or None for a usage error."""
    if not argv or argv[0] not in ("on", "off", "status"):
        return None
    opts, rest = {}, list(argv[1:])
    while rest:
        flag = rest.pop(0)
        if flag in ("--db", "--lock") and rest:
            opts[flag[2:]] = rest.pop(0)
        else:
            return None
    return argv[0], opts


def main(argv):
    parsed = _parse(argv)
    if parsed is None:
        print(USAGE)
        return 2
    cmd, opts = parsed
    db = Path(opts["db"]) if "db" in opts else DB_PATH
    lock = _lock_path(db, opts.get("lock"))
    if cmd == "status":
        status(db, lock, explicit="db" in opts)
        return 0
    return off(db) if cmd == "off" else on(db, lock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
