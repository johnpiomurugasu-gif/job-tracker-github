"""The new filing path (build stage 2). scheduler/run_sync.py imports this module and calls run() only when
sync_state.resolver_mode is 'on'; with the mode off it is never imported.

run(dry_run=False, limit=None) -> bool        True = good run; False = ABORTED (Ollama down) or a handled failure;
                                              anything unexpected (a Gmail invalid_grant, a database error) is raised.
It does NOT take the run lock (run_sync holds it) and it sets RELAXED_ALERT_ENABLED, AUTO_CONFIRM_ENABLED and every rule in
ingest.content_check.RULE_NAMES explicitly at the start (the module defaults stay off).

One run: read the cursor, list Gmail oldest first (ingest.broad_fetch: everything except Promotions/Social/Forums/sent, plus the old
sender domains), at most MAX_PER_RUN emails, skipping ids already in events / review_queue / quiet_log or judged in the cursor's second.
Each email: the existing classifier; the confidence gate of ingest.matcher.decide (parse failure, confidence < 0.75 -> review);
then ingest.sender_resolver.decide_email, the SAME decision function the replay uses; then ONE transaction (BEGIN IMMEDIATE ... COMMIT)
that writes the rows by method AND moves the cursor, so a failure part way leaves no partial rows and the cursor only passes emails
that were fully handled. The state the decision needs is rebuilt from the database at the start of every run (load_state).

State kept in sync_state (no schema change): resolver_cursor (epoch of the newest fully handled email), resolver_boundary (JSON list of
[id, epoch] for every email handled in the cursor's second or the second before it: the listing starts one second early so nothing in
the cursor's own second is missed, and these ids stop that overlap from being judged twice), import_saved_done (written by
scheduler/import_saved.py). The old cursor last_synced_epoch is never touched.

The row writing functions (file_resolution and the insert_* helpers) are shared with scheduler/import_saved.py.
Companies this path adds carry notes starting with NOTE_PREFIX; load_state uses that to rebuild the replay's ``created`` dict (companies
the resolver added) separately from ``existing`` (tracked companies), exactly as the replay has them.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from types import SimpleNamespace

import ingest.content_check as CC
import ingest.sender_resolver as SR
from ingest.matcher import CONFIDENCE_THRESHOLD, REVIEW, SKIP, CompanyMatch, decide
from ingest.search_client import SearchClient
from scheduler.run_sync import _advance_application, _detail, _iso      # the old path's row rules, reused (not copied)

MAX_PER_RUN = 5
KEY_CURSOR, KEY_BOUNDARY = "resolver_cursor", "resolver_boundary"
NOTE_PREFIX = "new path:"                      # companies.notes of every company this path adds
CAT_LOW_CONFIDENCE, CAT_PARSE_FAILURE = "low_confidence", "parse_failure"        # review categories of the confidence gate
SEARCH_SESSION = None                          # tests put a fake HTTP session here; None = the real requests module
_NO_MATCH = CompanyMatch(None, None, 0.0, False, False)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def use_rules():
    """Switch on everything this path relies on, explicitly (the module defaults are off)."""
    CC.set_enabled_rules(CC.RULE_NAMES)
    CC.RELAXED_ALERT_ENABLED = True
    SR.AUTO_CONFIRM_ENABLED = True


def is_new_path(notes):
    return (notes or "").startswith(NOTE_PREFIX)


# --- Gmail and the cursor ----------------------------------------------------------------------------------------------
def get_service():
    from ingest.gmail_client import build_service
    return build_service()


def handled_ids(conn):
    """Every Gmail id already filed (events, review_queue) or logged as quiet."""
    return {r[0] for r in conn.execute("SELECT source_email_id FROM events WHERE source_email_id IS NOT NULL "
                                       "UNION SELECT source_email_id FROM review_queue WHERE source_email_id IS NOT NULL "
                                       "UNION SELECT message_id FROM quiet_log")}


def read_marks(conn):
    """(cursor epoch, [[id, epoch], ...] handled in the cursor's second or the one before). A missing cursor starts at the old
    cursor (last_synced_epoch)."""
    kv = dict(conn.execute("SELECT key, value FROM sync_state WHERE key IN (?,?,'last_synced_epoch')", (KEY_CURSOR, KEY_BOUNDARY)))
    cursor = int(kv.get(KEY_CURSOR) or kv.get("last_synced_epoch") or 0)
    return cursor, (json.loads(kv[KEY_BOUNDARY]) if kv.get(KEY_CURSOR) and kv.get(KEY_BOUNDARY) else [])


def set_state(conn, key, value):
    conn.execute("INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def fetch_emails(conn, cursor, boundary, limit):
    """The only function that talks to Gmail for the listing (read only). Tests replace get_service()."""
    from ingest import broad_fetch
    from ingest.gmail_client import ATS_SENDER_DOMAINS, EXTRA_SENDER_DOMAINS
    return broad_fetch.oldest_unhandled(get_service(), cursor, handled_ids(conn) | {b[0] for b in boundary}, limit,
                                        domains=tuple(ATS_SENDER_DOMAINS) + tuple(EXTRA_SENDER_DOMAINS))


# --- the decision state, rebuilt from the database ----------------------------------------------------------------------
def load_state(conn, search):
    """The dict ingest.sender_resolver.decide_email wants, from the tables: existing = tracked companies, created = companies this
    path added (notes start with NOTE_PREFIX; confirmed = confirmed_by_user), links and conflicts from sender_links, aliases from
    ingest, discarded = {name_key: name} from discarded_companies."""
    existing, created = [], {}
    for cid, name, notes, confirmed in conn.execute("SELECT id, name, notes, confirmed_by_user FROM companies ORDER BY id"):
        if is_new_path(notes):
            created[SR._key(name)] = {"name": name, "confirmed": bool(confirmed), "first": None, "first_date": None, "first_method": None}
        else:
            existing.append((cid, name))
    links, conflicts = {"address": {}, "domain": {}}, {}
    for kind, value, cid, method, note, cname in conn.execute(
            "SELECT l.kind, l.value, l.company_id, l.method, l.note, c.name FROM sender_links l "
            "LEFT JOIN companies c ON c.id = l.company_id ORDER BY l.id"):
        if method == "conflict":
            conflicts[value] = note
        elif cname is not None:
            links[kind][value] = (cid, cname, method)
    return {"links": links, "conflicts": conflicts, "existing": existing, "aliases": SR.tracked_aliases(existing),
            "created": created, "discarded": dict(conn.execute("SELECT name_key, name FROM discarded_companies")), "search": search}


# --- the confidence gate (steps 1 to 3 of ingest.matcher.decide, so the wording is the old path's) --------------------
def gate(cls):
    """None if the classification may go on to the resolver, else ('skip' | 'review', reason, category)."""
    d = decide(cls, _NO_MATCH, cls.company)
    if d.outcome == SKIP:
        return "skip", d.reason, None
    if d.outcome == REVIEW and cls.needs_review:
        return "review", d.reason, CAT_PARSE_FAILURE
    if d.outcome == REVIEW and cls.confidence < CONFIDENCE_THRESHOLD:
        return "review", d.reason, CAT_LOW_CONFIDENCE
    return None


# --- row writing (shared with scheduler/import_saved.py); all of it runs inside the caller's transaction ---------------
def record(email, cls, detail=True):
    """The facts the writers need about one email. ``email`` has id, sender, subject, body, internal_date."""
    return {"id": email.id, "sender": email.sender, "subject": email.subject, "detail": _detail(email) if detail else None,
            "epoch": email.internal_date, "date": _iso(email.internal_date), "company": cls.company, "status": cls.status,
            "stage": cls.stage, "confidence": cls.confidence}


def insert_company(conn, name, confirmed, how, checks=None):
    return conn.execute("INSERT INTO companies (name, industry, notes, confirmed_by_user, content_checks_passed) VALUES (?, NULL, ?, ?, ?)",
                        (name, f"{NOTE_PREFIX} {how}", int(bool(confirmed)), checks)).lastrowid


def insert_application(conn, company_id, rec, confirmed):
    return conn.execute("INSERT INTO applications (company_id, role_title, date_applied, source, current_status, current_stage, last_updated, "
                        "confirmed_by_user) VALUES (?, NULL, NULL, NULL, ?, ?, ?, ?)",
                        (company_id, rec["status"], rec["stage"], rec["date"], int(bool(confirmed)))).lastrowid


def insert_event(conn, app_id, rec):
    conn.execute("INSERT INTO events (application_id, event_type, event_date, source_email_id, detail_text, confidence, confirmed) "
                 "VALUES (?, ?, ?, ?, ?, ?, 1)", (app_id, rec["status"], rec["date"], rec["id"], rec["detail"], rec["confidence"]))


def insert_review(conn, rec, reason, category):
    conn.execute("INSERT OR IGNORE INTO review_queue (source_email_id, received_date, sender, subject, extracted_company, status, stage, "
                 "confidence, match_company_id, match_score, reason, detail_text, created_at, review_category) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0.0, ?, ?, ?, ?)",
                 (rec["id"], rec["date"], rec["sender"], rec["subject"], rec["company"], rec["status"], rec["stage"], rec["confidence"],
                  (reason or "")[:300], rec["detail"], date.today().isoformat(), category or SR.CAT_OTHER))


def insert_quiet(conn, rec, quiet_name):
    conn.execute("INSERT OR IGNORE INTO quiet_log (message_id, name_key, email_date, sender_domain, llama_company, status, stage, logged_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (rec["id"], SR.discard_key(quiet_name), rec["date"], SR._sender_reg(rec["sender"]), rec["company"], rec["status"],
                  rec["stage"], now()))


def insert_link(conn, kind, value, company_id, method):
    conn.execute("INSERT OR IGNORE INTO sender_links (kind, value, company_id, method, created_at) VALUES (?, ?, ?, ?, ?)",
                 (kind, value, company_id, method, now()))


def company_id_by_name(conn, name):
    row = conn.execute("SELECT id FROM companies WHERE name = ? ORDER BY id LIMIT 1", (name,)).fetchone()
    return row[0] if row else None


def single_application(conn, company_id):
    rows = conn.execute("SELECT id FROM applications WHERE company_id = ?", (company_id,)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def confirm_company(conn, company_id):
    conn.execute("UPDATE companies SET confirmed_by_user = 1 WHERE id = ?", (company_id,))
    conn.execute("UPDATE applications SET confirmed_by_user = 1 WHERE company_id = ?", (company_id,))


def file_resolution(conn, state, rec, res, fx):
    """Write one decided email by method. Returns the method actually filed (a company with no single application is filed as
    a review row). ``state`` may be None (the import); with it, a learned link gets the company's real id."""
    m = res.method
    if m == "skipped":
        return "skipped"
    if m == "quiet":
        insert_quiet(conn, rec, res.quiet_name)
        return "quiet"
    if m in ("review", "search_unavailable"):
        insert_review(conn, rec, res.review_reason or res.reason, res.review_category)
        return "review"
    if res.would_create:                                       # a new company: by sender evidence (confirmed) or by content (unconfirmed)
        confirmed = res.would_create_confirmed
        how = f"sender evidence ({m})" if confirmed else "content checks (unconfirmed)"
        cid = insert_company(conn, res.company, confirmed, how, ",".join(res.passed_checks) if m == "content" else None)
        aid = insert_application(conn, cid, rec, confirmed)
        insert_event(conn, aid, rec)
    else:                                                      # an existing company, tracked or added earlier by this path
        cid = res.company_id if res.company_id is not None else company_id_by_name(conn, res.company)
        if cid is None:
            raise RuntimeError(f"the resolver named a company that is not in the database ({m})")
        aid = single_application(conn, cid)
        if aid is None:
            insert_review(conn, rec, "matched company has no single application to attach to", SR.CAT_OTHER)
            return "review"
        first_signal = conn.execute("SELECT COUNT(*) FROM events WHERE application_id = ?", (aid,)).fetchone()[0] == 0
        insert_event(conn, aid, rec)
        _advance_application(conn, aid, SimpleNamespace(internal_date=rec["epoch"]), SimpleNamespace(status=rec["status"], stage=rec["stage"]),
                             first_signal)
    if res.link_kind and m in ("text", "search"):              # a link learned from sender evidence
        insert_link(conn, res.link_kind, res.link_value, cid, m)
        if state is not None and res.link_value in state["links"][res.link_kind]:
            state["links"][res.link_kind][res.link_value] = (cid, res.company, m)
    if res.confirms and state is not None:                     # this email's sender evidence confirms an earlier content company
        confirm_company(conn, company_id_by_name(conn, state["created"][res.confirms]["name"]))
    return m


def advance_marks(conn, marks, rec):
    """Move the cursor past ``rec`` and keep the ids handled in the last two seconds (inside the caller's transaction).
    Returns the new marks. The cursor never moves back."""
    cursor = max(marks["cursor"], rec["epoch"])
    items = [b for b in marks["boundary"] if b[0] != rec["id"]] + [[rec["id"], rec["epoch"]]]
    boundary = [b for b in items if b[1] >= cursor - 1]
    set_state(conn, KEY_CURSOR, str(cursor))
    set_state(conn, KEY_BOUNDARY, json.dumps(boundary))
    return {"cursor": cursor, "boundary": boundary}


def process_email(conn, state, email, cls, *, content=None, dry_run=False, marks=None):
    """Decide and file ONE email in one transaction (nothing is written in a dry run). ``content`` (a TextChecks) replaces the text
    checks when given (the saved-data tests). ``marks`` {"cursor", "boundary"} is updated in place after the commit. Returns the method."""
    g = gate(cls)
    if g is not None and g[0] == "skip":
        res, fx = SR.Resolution("skipped", reason="unrelated"), {}
    elif g is not None:
        res, fx = SR.Resolution("review", reason=g[1], review_reason=g[1], review_category=g[2]), {}
    else:
        if content is None:
            content = CC.text_checks(email.subject, email.body, cls.company)          # the text is used here and dropped
        res, fx = SR.decide_email(state, email.id, email.sender, _iso(email.internal_date), cls.company, cls.status, content)
    rec = record(email, cls)
    if dry_run:
        return res.method
    if conn.in_transaction:
        raise RuntimeError("process_email needs a connection that is not inside a transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        method = file_resolution(conn, state, rec, res, fx)
        new = advance_marks(conn, marks, rec) if marks is not None else None
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if new is not None:
        marks.update(new)
    return method


def run(dry_run=False, limit=None):
    from ingest.classifier import ClassifierUnavailable, classify_email
    from ingest.gmail_client import DB_PATH
    use_rules()
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 6:
            raise RuntimeError("the database is not migrated (run db/migrate_stage6.py first)")
        cursor, boundary = read_marks(conn)
        emails = fetch_emails(conn, cursor, boundary, min(limit or MAX_PER_RUN, MAX_PER_RUN))
        state = load_state(conn, SearchClient(conn, session=SEARCH_SESSION, replay=dry_run))
        marks, counts = {"cursor": cursor, "boundary": boundary}, Counter()
        for e in emails:
            try:
                cls = classify_email(e.subject, e.body, e.sender)
            except ClassifierUnavailable as exc:
                print(f"\nABORTED: {exc}")
                print("Resolver cursor NOT advanced past this email. Fix the backend and re-run; emails already filed are skipped.")
                return False
            counts[process_email(conn, state, e, cls, dry_run=dry_run, marks=marks)] += 1
        print(f"resolver_run{' (dry run, nothing written)' if dry_run else ''}: {sum(counts.values())} email(s): "
              + (", ".join(f"{m} {n}" for m, n in sorted(counts.items())) or "no new mail") + f" | cursor {marks['cursor']}")
        return True
    finally:
        conn.close()
