"""Local server for the job-tracker dashboard v14 (beside v13; v13 is not touched). Port 8001 by default.

A copy of dashboard/server.py, extended. It imports dashboard/app.py (v13's data layer, unchanged) and scheduler/discard.py, nothing else of the project.
v13 differences, by feature:
  1 header: logs/last_ok (second line = epoch seconds) -> /api/data "heartbeat" and /api/heartbeat; "meta" carries the v14 and copy tags
  2 Discarded tab: "discarded" in the payload (scheduler.discard.discarded_list), POST /api/restore
  3 Delete: POST /api/app/delete {id: company id, remember} -> scheduler.discard.discard_company (replaces v13's /api/app/discard)
  4 review: "category" on every review row; POST /api/discard {id, remember} remembers the name through scheduler.discard.add_seeds (seed_discarded inside the transaction)
  5 content checks: "checks" (content_checks_passed as a list) on every pipeline row
  6 Confirm all: "confirm_all" in the payload, POST /api/confirm_all {count} (v13 has no such route)
  7 data rules: reads are read only (app.conn is replaced); every write is ONE transaction (BEGIN IMMEDIATE, 10 s busy timeout) and answers "try again" when the
    database stays busy; a double click or a stale page changes nothing and says so
  8 guards: 127.0.0.1 only; Host must be localhost or 127.0.0.1 with the server's port; Origin, if present, must be the same; actions are POST with a JSON body
  9 (build stage 4) Confirm on a review row of category discarded_company_replied: the Discarded list entry decides. A stored company is brought back
    (scheduler.discard.restore_company, joined to this transaction) and the email is filed on its latest application the way the new path files an attached
    email; a seeded name is confirmed as in v13 and leaves the list. The SQL for the filing is COPIED from scheduler/resolver_run.py (insert_event and the attached
    branch of file_resolution, with run_sync._advance_application's two UPDATEs): this file does not import resolver_run. "replied" on the payload's review rows.
 10 (build stage 5) "Add to existing company": POST /api/attach {id} files the email of a review row of category discarded_company_replied or duplicate_name as an event on the
    latest application of the ONE existing company the row points at (its stored match, or the one company whose name key equals the row's name), and removes the row, in one
    transaction. The server decides the target itself and never uses a company id from the page. It shares file_email() with the restore above. "attach" on the payload's review rows.
Run:  venv/bin/python dashboard/server_v14.py [--db db/tracker.db] [--port 8001] [--heartbeat logs/last_ok] [--confirm-log logs/confirm_all.log]
Standard library only (plus what app.py already needs). No database is opened at import time.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from dashboard import app  # noqa: E402  (v13's data layer: queries, compute_metrics, confirm_review, discard_review)
from scheduler import discard as D  # noqa: E402

VERSION = "v14"
STALE_AFTER_S = 600          # the header turns red from 10 minutes without a good run (the one constant)
FUTURE_TOLERANCE_S = 60      # a last_ok time up to a minute ahead of the clock counts as "now"; further ahead is shown red
BUSY_S = 10                  # seconds to wait for the write lock before answering "try again"
REPLIED = "discarded_company_replied"      # the review category of a reply from a discarded name (ingest.sender_resolver.CAT_DISCARDED_REPLIED)
DUPLICATE = "duplicate_name"               # the review category "looks like a company you already track" (ingest.sender_resolver.CAT_DUP)
ATTACH_CATEGORIES = (REPLIED, DUPLICATE)   # the review rows "Add to existing company" is for
MAX_BODY = 65536
CFG = {"db": ROOT / "db" / "tracker.db", "heartbeat": ROOT / "logs" / "last_ok", "confirm_log": ROOT / "logs" / "confirm_all.log"}
clock = time.time            # tests replace this to freeze the time

# event_type / current_status -> the mock's pill + timeline CSS category (as v13)
STATUS_CAT = {
    "applied": "applied", "assessment_invite": "assessment",
    "interview_invite": "interview", "interview_complete": "interview",
    "offer": "interview", "rejection": "rejected", "withdrawn": "withdrawn",
}


# --- database access -------------------------------------------------------------------------------------------------
def ro() -> sqlite3.Connection:
    """A read only connection (mode=ro, 10 s timeout). Also what v13's app.conn() returns, so v13's queries cannot write."""
    return sqlite3.connect(f"file:{CFG['db']}?mode=ro", uri=True, timeout=BUSY_S)


_tls = threading.local()


def _conn():
    return getattr(_tls, "tx", None) or ro()


app.conn = _conn             # v13's app.apps_df / events_df / review_df / confirm_review / discard_review now go through here


def q(sql, params=()):
    with contextlib.closing(ro()) as c:
        return c.execute(sql, params).fetchall()


class Bad(Exception):        # 400: the request is malformed
    pass


class Busy(Exception):       # 503: the database stayed locked
    pass


class Refused(Exception):    # 409: a stale page, a double click, a name clash, a changed count: nothing was changed
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def _busy(e: sqlite3.OperationalError) -> bool:
    return "locked" in str(e) or "busy" in str(e)


@contextlib.contextmanager
def tx():
    """ONE write transaction: BEGIN IMMEDIATE (waits up to BUSY_S for the sync), COMMIT at the end, ROLLBACK on any error.
    While it is open, v13's app functions use this connection (app.conn)."""
    c = sqlite3.connect(CFG["db"], timeout=BUSY_S, isolation_level=None)
    try:
        c.execute(f"PRAGMA busy_timeout = {int(BUSY_S * 1000)}")
        c.execute("PRAGMA foreign_keys = ON")
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            raise Busy() if _busy(e) else e
        _tls.tx = c
        try:
            yield c
            if c.in_transaction:
                c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            _tls.tx = None
    finally:
        c.close()


@contextlib.contextmanager
def plain():
    """A connection for scheduler.discard, whose functions run their own transaction (BEGIN IMMEDIATE, 10 s busy timeout)."""
    c = sqlite3.connect(CFG["db"], timeout=BUSY_S, isolation_level=None)
    try:
        yield c
    finally:
        c.close()


class Joined:
    """The connection of an open tx() for a scheduler.discard function, which opens a transaction of its own: BEGIN, COMMIT and ROLLBACK become no-ops, so
    restore_company joins this transaction and the caller's COMMIT or ROLLBACK covers it (scheduler/discard.py is not changed)."""
    in_transaction = False

    def __init__(self, c):
        self._c = c

    def execute(self, sql, *a):
        return None if sql in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK") else self._c.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._c, name)


# --- the header ------------------------------------------------------------------------------------------------------
def heartbeat() -> dict:
    """logs/last_ok: line 2 = epoch seconds of the last good run. state ok / missing / unreadable / future; red from STALE_AFTER_S."""
    base = {"stale_after": STALE_AFTER_S, "epoch": None, "age_s": None}
    try:
        epoch = int(Path(CFG["heartbeat"]).read_text().splitlines()[1])
    except FileNotFoundError:
        return {**base, "state": "missing", "red": True}
    except (OSError, UnicodeDecodeError, IndexError, ValueError):
        return {**base, "state": "unreadable", "red": True}
    age = clock() - epoch
    if age < -FUTURE_TOLERANCE_S:
        return {**base, "state": "future", "red": True, "epoch": epoch, "age_s": int(age)}
    age = max(0, int(age))
    return {**base, "state": "ok", "red": age >= STALE_AFTER_S, "epoch": epoch, "age_s": age}


def meta() -> dict:
    is_copy = Path(CFG["db"]).resolve() != (ROOT / "db" / "tracker.db").resolve()
    return {"version": VERSION, "copy": is_copy, "db": Path(CFG["db"]).name}


# --- the data --------------------------------------------------------------------------------------------------------
def _s(v) -> str:
    return app._txt(v)  # None / NaN -> "" (never the string "None"/"nan")


def company_maps(pairs):
    """({id: name}, {name key: [(id, name), ...]}) for a list of (id, name)."""
    by_key = {}
    for i, n in pairs:
        by_key.setdefault(D.discard_key(n), []).append((i, n))
    return dict(pairs), by_key


def pick_target(category, name, match_id, by_id, by_key):
    """(id, name) of the ONE existing company a review row of category discarded_company_replied or duplicate_name points at; None when it points at none or at more than one,
    and for any other category. Decided from the row alone: its stored match (match_company_id) if that company exists, else the one company whose name key equals the key of
    the row's company name (exact keys, as everywhere else). The page's request is never consulted."""
    if category not in ATTACH_CATEGORIES:
        return None
    if match_id in by_id:
        return match_id, by_id[match_id]
    key = D.discard_key(name)
    hits = by_key.get(key, []) if key else []
    return hits[0] if len(hits) == 1 else None


def unconfirmed_targets(c) -> list:
    """(company id, content_checks_passed) of every unconfirmed company: the company or any of its applications has confirmed_by_user = 0."""
    return c.execute("SELECT c.id, c.content_checks_passed FROM companies c WHERE c.confirmed_by_user = 0 OR EXISTS "
                     "(SELECT 1 FROM applications a WHERE a.company_id = c.id AND a.confirmed_by_user = 0) ORDER BY c.id").fetchall()


def build_payload() -> dict:
    apps, ev, rq = app.apps_df(), app.events_df(), app.review_df()
    today = date.today()
    m = app.compute_metrics(apps, ev, today)

    with contextlib.closing(ro()) as c:
        submitted_map = {int(r[0]): int(r[1] or 0) for r in
                         c.execute("SELECT id, COALESCE(assessment_submitted, 0) FROM applications")}
        extra = {int(r[0]): r[1:] for r in c.execute(
            "SELECT a.id, a.company_id, c.content_checks_passed, "
            "(SELECT COUNT(*) FROM applications x WHERE x.company_id = a.company_id), "
            "(SELECT COUNT(*) FROM events e JOIN applications y ON y.id = e.application_id WHERE y.company_id = a.company_id) "
            "FROM applications a JOIN companies c ON c.id = a.company_id")}
        pairs = [(r[0], r[1]) for r in c.execute("SELECT id, name FROM companies ORDER BY id")]
        names = [n for _, n in pairs]
        app_counts = dict(c.execute("SELECT company_id, COUNT(*) FROM applications GROUP BY company_id"))
        discarded = D.discarded_list(c)
        targets = unconfirmed_targets(c)
        want = sorted({D.discard_key(_s(r.get("extracted_company"))) for r in rq.to_dict(orient="records") if _s(r.get("review_category")) == REPLIED})
        stored_apps = {k: len(json.loads(sn).get("applications") or []) for k, sn in c.execute(          # how many applications each replied row's stored company has
            f"SELECT name_key, snapshot FROM discarded_companies WHERE name_key IN ({','.join('?' * len(want))})", want)} if want else {}

    counts = apps["current_status"].value_counts().to_dict()
    summary = {
        "total": len(apps),
        "awaiting": int(counts.get("applied", 0)),
        "interview_assessment": int(counts.get("interview_invite", 0)
                                    + counts.get("interview_complete", 0)
                                    + counts.get("assessment_invite", 0)),
        "rejected": int(counts.get("rejection", 0)),
        "unconfirmed": int((apps["confirmed_by_user"] == 0).sum()),
    }

    by_id, by_key = company_maps(pairs)

    def attach_info(r):                                                    # build stage 5: the one existing company "Add to existing company" would file this row's email on
        mid = r.get("match_company_id")
        t = pick_target(_s(r.get("review_category")), _s(r.get("extracted_company")), None if mid is None or mid != mid else int(mid), by_id, by_key)
        return t and {"name": t[1], "apps": int(app_counts.get(t[0], 0))}

    review = [{
        "id": int(r["id"]), "company": _s(r.get("extracted_company")) or "—",
        "sender": _s(r.get("sender")), "subject": _s(r.get("subject")),
        "status": _s(r.get("status")), "reason": _s(r.get("reason")),
        "stage": _s(r.get("stage")),
        "category": _s(r.get("review_category")),                       # v14: "" = filed by the old path
        "date": _s(r.get("received_date")), "status_label": app.STATUS_LABEL.get(_s(r.get("status")), _s(r.get("status"))),
        "attach": attach_info(r),
    } for r in rq.to_dict(orient="records")]

    pipeline = [{
        "id": int(a["id"]), "company": _s(a["company"]) or "—", "role": _s(a["role_title"]),
        "industry": app._or(a["industry"], "Not recorded"),
        "status": _s(a["current_status"]),
        "status_label": app.STATUS_LABEL.get(a["current_status"], _s(a["current_status"])),
        "cat": STATUS_CAT.get(a["current_status"], "applied"),
        "role_type": app.role_type(a["role_title"]),
        "stage": _s(a["current_stage"]) or "—", "last": _s(a["last_updated"]) or "—",
        "unconfirmed": bool(a["confirmed_by_user"] == 0),
        "submitted": bool(submitted_map.get(int(a["id"]), 0)),
        # v14: the company behind the row (Delete), its size (the Delete dialog) and the content checks it passed (the badge)
        "company_id": int(extra[int(a["id"])][0]), "n_apps": int(extra[int(a["id"])][2]), "n_events": int(extra[int(a["id"])][3]),
        "checks": [x for x in _s(extra[int(a["id"])][1]).split(",") if x],
    } for a in apps.to_dict(orient="records")]

    evrecs = ev.to_dict(orient="records")
    timeline = [{
        "company": _s(e["company"]) or "—", "role": _s(e["role_title"]),
        "industry": app._or(e["industry"], "Not recorded"),
        "status": app.STATUS_LABEL.get(e["event_type"], _s(e["event_type"])),
        "cat": STATUS_CAT.get(e["event_type"], "applied"),
        "date": _s(e["event_date"]), "detail": _s(e["detail_text"])[:220],
    } for e in evrecs if app._d(e["event_date"])]

    just_moved = [{
        "company": _s(e["company"]) or "—", "role": _s(e["role_title"]),
        "status": app.STATUS_LABEL.get(e["event_type"], _s(e["event_type"])),
        "cat": STATUS_CAT.get(e["event_type"], "applied"), "date": _s(e["event_date"]),
    } for e in evrecs[-8:][::-1]]

    weekly = []
    if not m["weekly"].empty:
        for row in m["weekly"].reset_index().to_dict(orient="records"):
            d = app._d(row["week"])
            weekly.append({"label": f"{d.day} {d.strftime('%b')}" if d else str(row["week"]),
                           "applied": int(row["Applied"]), "rejected": int(row["Rejected"])})

    last_ev = ev.groupby("application_id")["event_date"].max().to_dict() if not ev.empty else {}
    stale = []
    for a in apps.to_dict(orient="records"):
        if a["current_status"] in app.TERMINAL:
            continue
        ld = app._d(last_ev.get(a["id"])) or app._d(a["last_updated"])
        if ld and (today - ld).days >= app.STALE_DAYS:
            stale.append({"company": _s(a["company"]) or "—", "role": _s(a["role_title"]),
                          "stage": _s(a["current_stage"]) or "—", "last": str(ld),
                          "days": (today - ld).days})

    metrics = {
        "kpis": {k: m[k] for k in ("n", "apps_per_week", "reach", "reach_pct",
                                   "interviewed", "offers", "i2o_pct", "rejected",
                                   "rej_pct", "avg_response", "gone_quiet")},
        "outcome": m["outcome"], "industry": m["industry"], "source": m["source"],
        "rej_stage": m["rej_stage"], "weekly": weekly,
    }
    keys = {D.discard_key(n) for n in names}
    discarded = [{**d, "blocked": (not d["seed"]) and d["name_key"] in keys} for d in discarded]
    by_key = {d["name_key"]: d for d in discarded}
    for r in review:                                                        # build stage 4: what Confirm does on a reply from a discarded name
        d = by_key.get(D.discard_key(r["company"])) if r["category"] == REPLIED else None
        r["replied"] = d and {"name": d["name"], "seed": d["seed"], "was_confirmed": d["was_confirmed"], "events_removed": d["events_removed"],
                              "blocked": d["blocked"], "no_application": not d["seed"] and not stored_apps.get(d["name_key"])}
    return {"summary": summary, "review": review, "pipeline": pipeline,
            "timeline": timeline, "just_moved": just_moved, "stale": stale,
            "metrics": metrics, "today": today.isoformat(),
            "heartbeat": heartbeat(), "meta": meta(), "discarded": discarded,
            "confirm_all": {"count": len(targets), "from_content": sum(1 for t in targets if t[1])}}


def db_counts() -> dict:
    return {t: q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("companies", "applications", "review_queue")}


# --- validation ------------------------------------------------------------------------------------------------------
def need_id(d, key="id") -> int:
    v = d.get(key)
    if type(v) is not int or not 0 < v < 2**63:   # a bool, a string, a float, zero, a negative number and one too big for SQLite are all refused
        raise Bad(f"'{key}' must be a positive whole number")
    return v


def need_flag(d, key, default) -> bool:
    v = d.get(key, default)
    if type(v) is not bool:
        raise Bad(f"'{key}' must be true or false")
    return v


def need_text(d, key, maxlen=200, required=False) -> str:
    v = d.get(key, "")
    if v is None:
        v = ""
    if type(v) is not str or len(v) > maxlen:
        raise Bad(f"'{key}' must be text of at most {maxlen} characters")
    v = v.strip()
    if required and not v:
        raise Bad(f"'{key}' is required")
    return v


# --- actions: each is one transaction ----------------------------------------------------------------------------------
def already_filed(c, message_id):
    return bool(message_id) and c.execute("SELECT 1 FROM events WHERE source_email_id = ?", (message_id,)).fetchone() is not None


def latest_application(c, company_id):
    """The company's most recently updated application (the higher id on a tie), or None."""
    r = c.execute("SELECT id FROM applications WHERE company_id = ? ORDER BY COALESCE(last_updated, '') DESC, id DESC LIMIT 1", (company_id,)).fetchone()
    return r[0] if r else None


def file_email(c, rid, row, aid):
    """File a review row's email on application ``aid`` and drop the row, in the caller's transaction. SHARED by Confirm on a reply from a discarded company (restore_and_file)
    and by Add to existing company (act_attach), so the event and the application end up exactly the same either way. ``row`` = (received_date, source_email_id, status, stage,
    confidence, detail_text). The statements shared: the first-signal count, the INSERT INTO events, the two UPDATE applications (first-signal and forward-only) and the DELETE
    from review_queue; latest_application() and already_filed() are shared too."""
    day, status = row[0] or date.today().isoformat(), row[2] if row[2] in app.STATUS_LABEL else "applied"
    # --- copied from scheduler/resolver_run.py (file_resolution, attached branch; insert_event) and scheduler/run_sync.py (_advance_application) ---
    first_signal = c.execute("SELECT COUNT(*) FROM events WHERE application_id = ?", (aid,)).fetchone()[0] == 0
    c.execute("INSERT INTO events (application_id, event_type, event_date, source_email_id, detail_text, confidence, confirmed) VALUES (?, ?, ?, ?, ?, ?, 1)",
              (aid, status, day, row[1], row[5], row[4]))
    if first_signal:
        c.execute("UPDATE applications SET current_status = ?, current_stage = COALESCE(?, current_stage), last_updated = ? WHERE id = ?", (status, row[3], day, aid))
    else:
        c.execute("UPDATE applications SET current_status = ?, current_stage = COALESCE(?, current_stage), last_updated = ? "
                  "WHERE id = ? AND (last_updated IS NULL OR last_updated <= ?)", (status, row[3], day, aid, day))
    # --- end of the copied SQL ---
    c.execute("DELETE FROM review_queue WHERE id = ?", (rid,))


def restore_and_file(c, rid, row, key, entry):
    """Confirm on a reply from a discarded company whose Discarded entry holds the stored company (build stage 4). In the caller's transaction: bring the company
    back, file this email on its latest application, drop the review row. Refuses, changing nothing, when a company with that name key exists now, when the stored
    company has no application, or when the email is already filed. ``row`` = (received_date, source_email_id, status, stage, confidence, detail_text)."""
    name, snap = entry
    clash = [n for (n,) in c.execute("SELECT name FROM companies") if D.discard_key(n) == key]
    if clash:
        raise Refused("exists", f"A company called {clash[0]} already exists, so {name} cannot be restored. Delete or rename that one first. Nothing was changed.")
    if not snap.get("applications"):
        raise Refused("no_application", f"The stored copy of {name} has no application to add this email to, so it cannot be restored from here. Nothing was changed.")
    if already_filed(c, row[1]):
        raise Refused("done", "That email is already filed under a company. Nothing was changed.")
    try:
        cid = D.restore_company(Joined(c), key)
    except ValueError as e:
        raise Refused("exists", f"{name} cannot be restored: {e}")
    aid = latest_application(c, cid)
    file_email(c, rid, row, aid)
    return {"company": name, "restored": True, "company_id": cid, "application_id": aid}


def act_attach(d):
    """Add to existing company (build stage 5): see the header, feature 10. Only the review row's id is read from the request."""
    rid = need_id(d)
    with tx() as c:
        row = c.execute("SELECT review_category, extracted_company, match_company_id, received_date, source_email_id, status, stage, confidence, detail_text FROM review_queue WHERE id = ?",
                        (rid,)).fetchone()
        if row is None:
            raise Refused("gone", "That review item is already gone. Nothing was changed.")
        if row[0] not in ATTACH_CATEGORIES:
            raise Bad("That review item is not one that can be added to an existing company")
        target = pick_target(row[0], row[1] or "", row[2], *company_maps(c.execute("SELECT id, name FROM companies ORDER BY id").fetchall()))
        if target is None:
            raise Refused("no_target", "The company this item belongs to is no longer there, or is no longer the only company with that name. Nothing was changed.")
        cid, name = target
        aid = latest_application(c, cid)
        if aid is None:
            raise Refused("no_application", f"{name} has no application to add this email to. Nothing was changed.")
        if already_filed(c, row[4]):
            raise Refused("done", "That email is already filed under a company. Nothing was changed.")
        file_email(c, rid, row[3:], aid)
    return {"company": name, "company_id": cid, "application_id": aid}


def act_review_confirm(d):
    rid, company = need_id(d), need_text(d, "company", required=True)
    role, industry, stage = need_text(d, "role"), need_text(d, "industry"), need_text(d, "stage") or None
    status = need_text(d, "status") or "applied"
    if status not in app.STATUS_LABEL:
        raise Bad("'status' is not a known status")
    with tx() as c:
        row = c.execute("SELECT review_category, extracted_company, received_date, source_email_id, status, stage, confidence, detail_text FROM review_queue WHERE id = ?",
                        (rid,)).fetchone()
        if row is None:
            raise Refused("gone", "That review item is already gone. Nothing was changed.")
        key = entry = None
        if row[0] == REPLIED:                                                # a reply from a discarded name: the Discarded list entry decides
            key = D.discard_key(row[1])
            found = c.execute("SELECT name, snapshot FROM discarded_companies WHERE name_key = ?", (key,)).fetchone()
            entry = found and (found[0], json.loads(found[1]))
        if entry and not entry[1].get("seed"):
            return restore_and_file(c, rid, row[2:], key, entry)
        app.confirm_review(rid, company, role, industry, status, stage)      # v13's own function, inside this transaction
        if entry:                                                            # a seeded name (no stored company): it leaves the list with this Confirm
            c.execute("DELETE FROM discarded_companies WHERE name_key = ?", (key,))
    return {"company": company}


def act_review_discard(d):
    rid, remember = need_id(d), need_flag(d, "remember", True)
    with tx() as c:
        row = c.execute("SELECT extracted_company FROM review_queue WHERE id = ?", (rid,)).fetchone()
        if row is None:
            raise Refused("gone", "That review item is already gone. Nothing was changed.")
        name = (row[0] or "").strip()
        remembered = bool(remember and name and D.discard_key(name))
        if remembered:
            D.add_seeds(c, [name])                                           # seed_discarded's work, inside this transaction
        app.discard_review(rid)                                              # v13's own function
    return {"company": name, "remembered": remembered}


def act_app_confirm(d):
    aid = need_id(d)
    with tx() as c:
        row = c.execute("SELECT company_id, confirmed_by_user FROM applications WHERE id = ?", (aid,)).fetchone()
        if row is None:
            raise Refused("gone", "That company is already gone. Nothing was changed.")
        if row[1] != 0:
            raise Refused("done", "That company is already confirmed. Nothing was changed.")
        c.execute("UPDATE companies SET confirmed_by_user = 1 WHERE id = ?", (row[0],))        # as v13: the company and this application
        c.execute("UPDATE applications SET confirmed_by_user = 1 WHERE id = ?", (aid,))
    return {}


def act_mark_submitted(d):
    aid = need_id(d)
    with tx() as c:
        row = c.execute("SELECT assessment_submitted FROM applications WHERE id = ?", (aid,)).fetchone()
        if row is None:
            raise Refused("gone", "That application is already gone. Nothing was changed.")
        if row[0]:
            raise Refused("done", "That assessment is already marked as submitted. Nothing was changed.")
        c.execute("UPDATE applications SET assessment_submitted = 1, current_stage = 'Assessment submitted' WHERE id = ?", (aid,))
    return {}


def act_delete(d):
    cid, remember = need_id(d), need_flag(d, "remember", True)
    row = q("SELECT name FROM companies WHERE id = ?", (cid,))
    if not row:
        raise Refused("gone", "That company is already gone. Nothing was changed.")
    try:
        with plain() as c:
            n = D.discard_company(c, cid, remember)
    except sqlite3.OperationalError as e:
        raise Busy() if _busy(e) else e
    return {"company": row[0][0], "events_removed": n, "remembered": remember}


def act_restore(d):
    key = need_text(d, "name_key", required=True)
    row = q("SELECT name, snapshot FROM discarded_companies WHERE name_key = ?", (key,))
    if not row:
        raise Refused("gone", "That name is no longer on the Discarded list. Nothing was changed.")
    name, seed = row[0][0], bool(json.loads(row[0][1]).get("seed"))
    if not seed:
        clash = [n for (n,) in q("SELECT name FROM companies") if D.discard_key(n) == key]
        if clash:                                                           # refused before restore_company is called
            raise Refused("exists", f"A company called {clash[0]} already exists, so {name} cannot be restored. Delete or rename that one first.")
    try:
        with plain() as c:
            cid = D.restore_company(c, key)
    except sqlite3.OperationalError as e:
        raise Busy() if _busy(e) else e
    except LookupError:
        raise Refused("gone", "That name is no longer on the Discarded list. Nothing was changed.")
    except ValueError as e:
        raise Refused("exists", f"{name} cannot be restored: {e}")
    return {"company": name, "restored": cid is not None, "company_id": cid}


def act_confirm_all(d):
    shown = d.get("count")
    if type(shown) is not int or shown < 0:
        raise Bad("'count' must be a whole number")
    with tx() as c:
        targets = unconfirmed_targets(c)
        if len(targets) != shown:                                           # the page was out of date: change nothing
            raise Refused("count", f"The number of unconfirmed companies is now {len(targets)}, not {shown}. Nothing was confirmed.", count=len(targets))
        ids = [t[0] for t in targets]
        if ids:
            marks = ",".join("?" * len(ids))
            c.execute(f"UPDATE companies SET confirmed_by_user = 1 WHERE id IN ({marks})", ids)
            c.execute(f"UPDATE applications SET confirmed_by_user = 1 WHERE company_id IN ({marks})", ids)
    logged = True
    try:                                                                    # one line per use: time, count, company ids, no text
        log = Path(CFG["confirm_log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            f.write(f"{datetime.fromtimestamp(clock()).astimezone().isoformat(timespec='seconds')}\tcount={len(ids)}\tids={','.join(map(str, ids))}\n")
    except OSError:
        logged = False
    return {"confirmed": len(ids), "logged": logged}


ROUTES = {"/api/confirm": act_review_confirm, "/api/discard": act_review_discard, "/api/app/confirm": act_app_confirm,
          "/api/app/delete": act_delete, "/api/app/mark_submitted": act_mark_submitted, "/api/restore": act_restore,
          "/api/confirm_all": act_confirm_all, "/api/attach": act_attach}
PAGES = {"/": ("index_v14.html", "text/html; charset=utf-8"), "/index.html": ("index_v14.html", "text/html; charset=utf-8"),
         "/live_v14.js": ("live_v14.js", "application/javascript; charset=utf-8")}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", extra=()):
        b = body if isinstance(body, (bytes, bytearray)) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _json(self, code, obj, extra=()):
        self._send(code, json.dumps(obj), extra=extra)

    def _allowed(self) -> bool:
        """Host must be localhost or 127.0.0.1 with this server's port; an Origin header, if there is one, must be the same page."""
        port = self.server.server_address[1]
        ok = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if self.headers.get("Host", "").lower() not in ok:
            self._json(403, {"ok": False, "error": f"Use http://localhost:{port}"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {f"http://{h}" for h in ok}:
            self._json(403, {"ok": False, "error": "Request from another page refused"})
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        p = self.path.split("?")[0]
        try:
            if p in PAGES:
                name, ctype = PAGES[p]
                self._send(200, (HERE / name).read_bytes(), ctype)
            elif p == "/api/data":
                self._json(200, build_payload())
            elif p == "/api/heartbeat":
                self._json(200, heartbeat())
            elif p in ROUTES:
                self._json(405, {"ok": False, "error": "POST only"}, extra=[("Allow", "POST")])
            else:
                self._json(404, {"error": "not found"})
        except sqlite3.OperationalError as e:
            self._json(503 if _busy(e) else 500, {"ok": False, "error": "The database is busy. Try again in a few seconds." if _busy(e) else "Could not read the database."})

    def do_POST(self):
        if not self._allowed():
            return
        fn = ROUTES.get(self.path.split("?")[0])
        if fn is None:
            return self._json(404, {"ok": False, "error": "not found"})
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"ok": False, "error": "send JSON"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad Content-Length"})
        if n > MAX_BODY:
            return self._json(413, {"ok": False, "error": "request too large"})
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "the request body is not a JSON object"})
        try:
            out = fn(data)
        except Bad as e:
            return self._json(400, {"ok": False, "error": str(e)})
        except Refused as e:
            return self._json(409, {"ok": False, "error": e.message, "code": e.code, **e.extra})
        except Busy:
            return self._json(503, {"ok": False, "busy": True, "error": "The database is busy (the sync may be writing). Nothing was changed. Try again in a few seconds."})
        except sqlite3.OperationalError as e:
            if _busy(e):
                return self._json(503, {"ok": False, "busy": True, "error": "The database is busy (the sync may be writing). Nothing was changed. Try again in a few seconds."})
            sys.stderr.write(f"v14: {type(e).__name__} on {self.path}\n")
            return self._json(500, {"ok": False, "error": "Something went wrong. Nothing was changed."})
        except Exception as e:  # noqa: BLE001  (class name only: an exception text could hold data)
            sys.stderr.write(f"v14: {type(e).__name__} on {self.path}\n")
            return self._json(500, {"ok": False, "error": "Something went wrong. Nothing was changed."})
        self._json(200, {"ok": True, "counts": db_counts(), **out})

    def log_message(self, *a):  # quiet
        pass


def configure(db=None, heartbeat_path=None, confirm_log=None) -> None:
    for key, val in (("db", db), ("heartbeat", heartbeat_path), ("confirm_log", confirm_log)):
        if val is not None:
            CFG[key] = Path(val)


def check_database() -> None:
    """Refuse a database this page cannot work with (not migrated, or a v13 column missing) before serving anything."""
    with contextlib.closing(ro()) as c:
        if c.execute("PRAGMA user_version").fetchone()[0] < 6:
            raise SystemExit("The database is not migrated (run db/migrate_stage6.py first). Nothing was started.")
        for table, col in (("companies", "content_checks_passed"), ("review_queue", "review_category"), ("applications", "assessment_submitted")):
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                raise SystemExit(f"The database has no {table}.{col}. Nothing was started.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Job Tracker dashboard v14")
    ap.add_argument("--db", default=str(CFG["db"]))
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--heartbeat", default=str(CFG["heartbeat"]))
    ap.add_argument("--confirm-log", default=str(CFG["confirm_log"]))
    a = ap.parse_args(argv)
    if not Path(a.db).is_file():
        raise SystemExit(f"{a.db} is not a file. Nothing was started.")
    configure(a.db, a.heartbeat, a.confirm_log)
    check_database()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        raise SystemExit(f"Port {a.port} is not available ({e.strerror}). Nothing was started or stopped.")
    print(f"Job Tracker dashboard {VERSION} → http://localhost:{a.port}   database: {a.db}   heartbeat file: {a.heartbeat}   (Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
