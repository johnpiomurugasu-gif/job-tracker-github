"""The one-off import of the saved 30 days (written in build stage 2, run for real in build stage 5).

    venv/bin/python -m scheduler.import_saved DB             dry run: prints the counts and writes nothing (the default)
    venv/bin/python -m scheduler.import_saved DB --apply     writes

It applies the final-stage decisions of the v6 replay (scratch/dryrun_v6.db, stage S10: stage_rows_v6, confirmations_v6, sender_links_v6)
to the MIGRATED database DB, through the row writers of scheduler/resolver_run.py (file_resolution and the insert_* helpers), so an
imported email looks exactly like one the new path filed. Nothing is decided again: no Gmail listing, no Ollama, no Tavily.

Per run, in ONE transaction (BEGIN IMMEDIATE ... COMMIT; any error rolls everything back):
  * the saved emails, oldest first, except ids already in events, review_queue or quiet_log (counted as "already recorded");
  * the saved auto confirmations (company and application);
  * the saved sender links that are not there yet (INSERT OR IGNORE: seeded links stay, the two Workday conflicts stay conflicts);
  * the four seed names on the discarded list (after the quiet rows, so each starts at 0 emails since);
  * Acme, Globex, Initech, Umbrella, Hooli and Vandelay marked confirmed by name key
    (ingest.sender_resolver.discard_key, equal keys only); a name that is not found is reported;
  * sync_state.resolver_cursor = the start of the day before the newest saved email, only if it is missing, so the normal job fetches
    the gap; sync_state.import_saved_done = the date and the counts. A second run finds that key and does nothing.
A saved decision that does not fit this database is not dropped: a company it points at that is gone becomes a review row, and a new
company whose name is already there is attached to it instead of being added twice (both counted). One exception, the live rule for a
discarded name (R7 and R10): when the company is gone and its name key is on the discarded list (it was deleted with "Ignore future
emails" ticked), the email goes to the quiet log, or, if its status is visible, to review with the category discarded_company_replied.

detail_text (events) and subject (review rows) are not in the saved data. With --apply they are read from Gmail by message id (read
only, the same stored form as the live path: subject plus the first 500 characters) BEFORE the transaction. An id that Gmail says is
gone (404) is stored with NULL and counted; any other Gmail error stops the import with nothing written. (A saved row that the discarded-name rule
turns into a quiet row is read like the others and its text is not stored: the quiet log has no text column.) A dry run reads nothing and
runs the very same code on an in-memory copy of the database (the target is opened read only), so its counts are the --apply counts.
It takes the run lock (waits up to LOCK_WAIT seconds for a running sync to finish; --lock PATH uses a lock file of its own instead of logs/run.lock, for a rehearsal
on a copy). It prints counts and company names only.
The reads are paced and retried (build stage 5): READ_PAUSE seconds between two reads (none before the first), because Gmail's per-minute quota stopped an unpaced import
after about 100 reads (HTTP 403 rateLimitExceeded); a read that fails with HTTP 429, 403 with the reason rateLimitExceeded or userRateLimitExceeded, or 500, 502, 503 or 504
is tried again after RETRY_WAITS seconds (a Retry-After from Gmail is used instead, up to RETRY_AFTER_CAP), at most 6 tries per message; if the last try fails the import
stops as before with nothing written. A progress line with counts only is printed every 50 reads.
Standard library only.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sqlite3
import sys
import time
from pathlib import Path

import ingest.sender_resolver as SR
import scheduler.discard as D
import scheduler.resolver_run as R
import scheduler.runlock as RL
from scheduler.run_sync import _detail          # the old path's rule for the stored text, reused

ROOT = Path(__file__).resolve().parent.parent
SAVED = ROOT / "scratch" / "dryrun_v6.db"
STAGE = "S10"                                   # the final stage of the v6 replay: the rules the new path runs with
KEY_DONE = "import_saved_done"
REAL_COMPANIES = ("Acme", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay")
LOCK_WAIT = 60                                  # seconds to wait for a running sync to finish
READ_PAUSE = 0.7                                # seconds between two Gmail reads (none before the first)
RETRY_WAITS = (5, 10, 20, 40, 60)               # seconds to wait before try 2, 3, 4, 5 and 6 of one message (so at most 6 tries)
RETRY_AFTER_CAP = 120                           # a Retry-After from Gmail is used instead of RETRY_WAITS, but never waited longer than this
RETRY_STATUS = (429, 500, 502, 503, 504)        # HTTP statuses that are tried again; 403 only with one of RETRY_REASONS
RETRY_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")
PROGRESS_EVERY = 50                             # a progress line (counts only) after this many reads
SLEEP = time.sleep                              # tests replace this one name, so nothing waits
READ_STATS = {"reads": 0, "retries": 0}         # of the last read_texts call: messages answered, and requests that had to be repeated
MISSING = "the company this saved decision points at is not in the database"
REPLIED = "discarded company replied"            # the live path's reason for R10


def load_saved():
    """(rows, auto confirmation company names, links) of the final stage, read only. Rows are oldest first (date, then id)."""
    db = sqlite3.connect(f"file:{SAVED}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT s.message_id AS id, s.email_date AS date, s.sender, s.llama_company AS company, s.status, l.stage, l.confidence, s.method, "
            "s.linked_company, s.linked_company_id, s.review_reason, s.review_category, s.would_create, s.quiet_name "
            "FROM stage_rows_v6 s JOIN llama_cache l ON l.message_id = s.message_id WHERE s.stage = ? "
            "ORDER BY s.email_date, s.message_id", (STAGE,)).fetchall()
        confirms = [r[0] for r in db.execute("SELECT company FROM confirmations_v6 WHERE stage = ? ORDER BY confirmed_msg", (STAGE,))]
        links = db.execute("SELECT company_name, kind, value, method FROM sender_links_v6 ORDER BY id").fetchall()
    finally:
        db.close()
    if not rows:
        raise RuntimeError("no saved decisions found")
    return rows, confirms, links


def _try_lock_at(path):
    """A lock of its own: flock on ``path`` without waiting. The open file, or None if another process holds it."""
    import fcntl
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def take_lock(path=None):
    """The run lock (logs/run.lock, or the file ``path``), waiting up to LOCK_WAIT seconds for a running sync to finish. None if it stays taken."""
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        lock = RL.try_lock() if path is None else _try_lock_at(path)
        if lock is not None or time.monotonic() >= deadline:
            return lock
        time.sleep(1)


def open_target(path, apply):
    """--apply: the database itself. Dry run: an in-memory copy of it, made from a read only connection (the backup API)."""
    if apply:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    else:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn = sqlite3.connect(":memory:", isolation_level=None)
        src.backup(conn)
        src.close()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _status(exc):
    return getattr(getattr(exc, "resp", None), "status", None)


def _reasons(exc):
    """The machine readable reasons of a Google API error ('rateLimitExceeded' ...), from its JSON body or its error_details; an empty set if there are none."""
    try:
        errors = json.loads(getattr(exc, "content", b"") or b"{}")["error"]["errors"]
    except Exception:
        errors = getattr(exc, "error_details", None)
    return {e.get("reason") for e in errors if isinstance(e, dict)} if isinstance(errors, (list, tuple)) else set()


def _retryable(exc):
    return _status(exc) in RETRY_STATUS or (_status(exc) == 403 and bool(_reasons(exc) & set(RETRY_REASONS)))


def _retry_after(exc):
    """Seconds from a Retry-After header (capped at RETRY_AFTER_CAP), or None."""
    resp = getattr(exc, "resp", None)
    try:
        return min(max(float(resp.get("retry-after")), 0.0), RETRY_AFTER_CAP)
    except (AttributeError, TypeError, ValueError):
        return None


def read_texts(ids):
    """{id: (subject, stored text)} from Gmail, read only; None for an id Gmail says is gone (404). Reads are READ_PAUSE seconds apart (none before the first); a rate limit
    or a passing server error is tried again after RETRY_WAITS (see the module text). Any other error, or the same one after the last try, is raised: nothing has been written."""
    from ingest.gmail_client import _get_email
    service, out = R.get_service(), {}
    READ_STATS.update(reads=0, retries=0)
    for n, mid in enumerate(ids, 1):
        if n > 1:
            SLEEP(READ_PAUSE)
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                e = _get_email(service, mid)
            except Exception as exc:
                if _status(exc) == 404:
                    out[mid] = None
                    break
                if not _retryable(exc):
                    raise
                if attempt == len(RETRY_WAITS):
                    print(f"ABORTED: Gmail still refused message ...{mid[-6:]} after {attempt + 1} tries (HTTP {_status(exc)}); nothing was written. Wait a few minutes and run the import again.")
                    raise
                READ_STATS["retries"] += 1
                wait = _retry_after(exc)
                SLEEP(RETRY_WAITS[attempt] if wait is None else wait)
            else:
                out[mid] = (e.subject, _detail(e))
                break
        READ_STATS["reads"] += 1
        if n % PROGRESS_EVERY == 0:
            print(f"read {n} of {len(ids)}", flush=True)
    return out


def resolution(conn, row, discarded):
    """The SR.Resolution the writers want, from one saved row, plus what this database changes about it (note).
    ``discarded`` is {name_key: name} from discarded_companies as it was before this import."""
    m, name = row["method"], row["linked_company"]
    if m == "quiet":
        return SR.Resolution("quiet", quiet_name=row["quiet_name"])
    if m in ("review", "search_unavailable"):
        return SR.Resolution(m, reason=row["review_reason"], review_reason=row["review_reason"], review_category=row["review_category"])
    cid = row["linked_company_id"]
    if cid is None or (conn.execute("SELECT name FROM companies WHERE id = ?", (cid,)).fetchone() or (None,))[0] != name:
        cid = R.company_id_by_name(conn, name)                 # the saved id does not carry this name here: go by name
    dname = discarded.get(SR.discard_key(name)) if cid is None else None
    if dname is not None and row["status"] in SR.VISIBLE_STATUSES:     # S1a: the live rule for a discarded name (R10)
        return SR.Resolution("review", company=dname, reason=REPLIED, review_reason=REPLIED, review_category=SR.CAT_DISCARDED_REPLIED, note="gone_replied")
    if dname is not None:                                              # R7
        return SR.Resolution("quiet", quiet_name=dname, note="gone_quiet")
    create = row["would_create"] == "Y" and cid is None
    if cid is None and not create:
        return SR.Resolution("review", reason=MISSING, review_reason=MISSING, review_category=SR.CAT_OTHER, note="missing")
    note = "existed" if row["would_create"] == "Y" and not create else ""
    return SR.Resolution(m, company=name, company_id=cid, would_create=create, would_create_confirmed=create and m != "content",
                         passed_checks=list(SR.CHECKS) if m == "content" else [], note=note)


def totals(conn):
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    like = R.NOTE_PREFIX + "%"
    return {"companies": one("SELECT COUNT(*) FROM companies WHERE notes LIKE ?", like),
            "confirmed": one("SELECT COUNT(*) FROM companies WHERE notes LIKE ? AND confirmed_by_user = 1", like),
            "events": one("SELECT COUNT(*) FROM events"), "review": one("SELECT COUNT(*) FROM review_queue"),
            "quiet": one("SELECT COUNT(*) FROM quiet_log"), "links": one("SELECT COUNT(*) FROM sender_links")}


def apply_saved(conn, saved, texts):
    """All the writes, in one transaction on ``conn`` (autocommit, nothing open). ``texts`` {id: (subject, text) | None}. Returns the counts."""
    rows, confirms, links = saved
    conn.execute("BEGIN IMMEDIATE")
    try:
        handled = R.handled_ids(conn)
        discarded = dict(conn.execute("SELECT name_key, name FROM discarded_companies"))
        before, c = totals(conn), {"saved": len(rows), "recorded": 0, "missing": 0, "existed": 0, "gone_quiet": 0, "gone_replied": 0}
        for row in rows:
            if row["id"] in handled:
                c["recorded"] += 1
                continue
            res, text = resolution(conn, row, discarded), texts.get(row["id"])
            if res.note:
                c[res.note] += 1
            rec = {"id": row["id"], "sender": row["sender"], "subject": text[0] if text else None, "detail": text[1] if text else None,
                   "epoch": calendar.timegm(time.strptime(row["date"], "%Y-%m-%d")), "date": row["date"], "company": row["company"],
                   "status": row["status"], "stage": row["stage"], "confidence": row["confidence"]}
            if res.note == "gone_replied" and SR.discard_key(rec["company"]) != SR.discard_key(res.company):
                rec["company"] = res.company                            # the dashboard finds the discarded entry by the key of the row's company
            R.file_resolution(conn, None, rec, res, {})
        c["imported"] = len(rows) - c["recorded"]
        names = [n for n in confirms if R.company_id_by_name(conn, n) is not None]
        for n in names:
            R.confirm_company(conn, R.company_id_by_name(conn, n))
        c["auto_confirmed"], c["auto_total"] = len(names), len(confirms)
        mid = totals(conn)                                      # the figures the replay predicts (the six real names come after)
        for company, kind, value, method in links:
            cid = R.company_id_by_name(conn, company)
            if cid is not None:
                R.insert_link(conn, kind, value, cid, method)
        c["seeds"] = D.add_seeds(conn, SR.SEED_DISCARDED_NAMES)
        have = conn.execute("SELECT id, name, confirmed_by_user FROM companies").fetchall()
        c["real_not_found"], c["real_changed"] = [], 0
        for name in REAL_COMPANIES:
            hits = [(cid, ok) for cid, n, ok in have if SR.discard_key(n) == SR.discard_key(name)]
            c["real_not_found"] += [] if hits else [name]
            c["real_changed"] += sum(1 for _, ok in hits if not ok)
            for cid, _ in hits:
                R.confirm_company(conn, cid)
        end = totals(conn)
        c.update(companies=mid["companies"] - before["companies"], confirmed=mid["confirmed"] - before["confirmed"], events=mid["events"] - before["events"],
                 review=mid["review"] - before["review"], quiet=mid["quiet"] - before["quiet"], links=end["links"] - before["links"])
        c["unconfirmed"] = c["companies"] - c["confirmed"]
        c["unreadable"] = sum(1 for t in texts.values() if t is None)
        c["cursor"] = None
        if conn.execute("SELECT 1 FROM sync_state WHERE key = ?", (R.KEY_CURSOR,)).fetchone() is None:
            c["cursor"] = calendar.timegm(time.strptime(max(r["date"] for r in rows), "%Y-%m-%d")) - 86400
            R.set_state(conn, R.KEY_CURSOR, str(c["cursor"]))
            R.set_state(conn, R.KEY_BOUNDARY, "[]")
        R.set_state(conn, KEY_DONE, json.dumps({"date": R.now(), "counts": c}))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return c


def report(c, apply):
    day = time.strftime("%d %b %Y", time.gmtime(c["cursor"])) if c["cursor"] else None
    return [f"import_saved ({'applied' if apply else 'DRY RUN: nothing written'})",
            f"  saved decisions     {c['saved']}",
            f"  already recorded    {c['recorded']}   (in events, review_queue or quiet_log: skipped)",
            f"  imported            {c['imported']}",
            f"    new companies     {c['companies']}   (confirmed {c['confirmed']}, unconfirmed {c['unconfirmed']})",
            f"    events            {c['events']}",
            f"    review rows       {c['review']}",
            f"    quiet rows        {c['quiet']}",
            f"  sender links added  {c['links']}",
            f"  auto confirmations  {c['auto_confirmed']} of {c['auto_total']}",
            f"  seed names added    {c['seeds']}   (discarded list)",
            f"  real companies      {len(REAL_COMPANIES) - len(c['real_not_found'])} of {len(REAL_COMPANIES)} found and confirmed ({c['real_changed']} were unconfirmed)"
            + (f"; NOT FOUND: {', '.join(c['real_not_found'])}" if c["real_not_found"] else ""),
            f"  fitted to this database: {c['missing']} to review (company gone), {c['existed']} attached (company already there)",
            f"  discarded before the import: {c['gone_quiet']} to the quiet log, {c['gone_replied']} to review (company gone, name on the discarded list)",
            f"  resolver cursor     " + (f"set to the start of {day} UTC ({c['cursor']})" if c["cursor"] else "already set: left as it was"),
            *([f"  gmail reads         {c['gmail']['reads']} ({c['gmail']['retries']} repeated after a rate limit or a server error)"] if apply and c.get("gmail") else []),
            f"  detail text         " + (f"read from Gmail by id; {c['unreadable']} unreadable, stored as NULL" if apply else "not read in a dry run"),
            "" if apply else "  To write it: add --apply (it takes the run lock and writes in one transaction)."]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m scheduler.import_saved", description="Import the saved 30 days of decisions (dry run unless --apply).")
    ap.add_argument("db", help="path of the migrated tracker database")
    ap.add_argument("--apply", action="store_true", help="write (the default is a dry run that writes nothing)")
    ap.add_argument("--lock", metavar="PATH", help="take this lock file instead of logs/run.lock (a rehearsal on a copy)")
    args = ap.parse_args(argv)
    if not Path(args.db).is_file():
        print(f"ABORTED: {args.db} is not a file")
        return 1
    lock = take_lock(args.lock)
    if lock is None:
        print("ABORTED: the sync is running (the run lock is taken); nothing was done. Try again in a minute.")
        return 1
    conn = None
    try:
        conn = open_target(args.db, args.apply)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 6:
            print("ABORTED: the database is not migrated (run db/migrate_stage6.py first)")
            return 1
        done = conn.execute("SELECT value FROM sync_state WHERE key = ?", (KEY_DONE,)).fetchone()
        if done:
            d = json.loads(done[0])
            print(f"Already imported on {d['date'][:10]}: {d['counts']['imported']} emails, {d['counts']['companies']} new companies. Nothing to do.")
            return 0
        saved = load_saved()
        handled = R.handled_ids(conn)
        texts = read_texts([r["id"] for r in saved[0] if r["id"] not in handled and r["method"] != "quiet"]) if args.apply else {}
        counts = apply_saved(conn, saved, texts)
        if args.apply:
            counts["gmail"] = dict(READ_STATS)
        print("\n".join(line for line in report(counts, args.apply) if line))
        return 0
    finally:
        if conn is not None:
            conn.close()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
