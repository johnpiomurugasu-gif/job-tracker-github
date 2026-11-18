"""Discard and Restore for whole companies (build stage 2; the dashboard calls these in build stage 3).

Every function takes an OPEN sqlite3 connection that is not inside a transaction and does its work in ONE transaction
(BEGIN IMMEDIATE ... COMMIT, busy timeout 10 seconds, foreign keys on). On any error the transaction is rolled back and nothing changed.

  discard_company(conn, company_id, remember=True)  -> number of events removed
  restore_company(conn, name_key)                   -> id of the restored company (None for a seeded name)
  discarded_list(conn)                              -> [{name_key, name, discarded_at, emails_since, seed, was_confirmed, events_removed}]
  seed_discarded(conn, names)                       -> number of names added   (add_seeds does the same inside a transaction the caller holds)

The name key is ingest.sender_resolver.discard_key: alias applied, lower case, accents and punctuation removed, corporate endings
removed, spaces removed. Keys must be EQUAL; there is no fuzzy matching ("Acme Ltd" is "Acme"; "Acme Global" is not).
The snapshot (discarded_companies.snapshot, JSON) holds the company, its applications, events, contacts and sender links exactly
as they were, so Restore gives the same rows back. It therefore holds the events' stored text (subject plus 500 characters), just
as the events table did. quiet_log rows stay as history; Restore never files them again. Standard library only.
"""
import contextlib
import json
import sqlite3
from datetime import datetime, timezone

from ingest.sender_resolver import discard_key

BUSY_MS = 10000          # busy timeout, milliseconds


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")     # microseconds: "since" is a strict, unambiguous order


@contextlib.contextmanager
def _tx(conn):
    """BEGIN IMMEDIATE ... COMMIT around the body; ROLLBACK on any error."""
    if conn.in_transaction:
        raise RuntimeError("the connection is inside a transaction; discard functions run in one of their own")
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_MS)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _insert(conn, table, row, keep_id=True):
    """Insert ``row`` (a dict) using only the columns the table has now. Returns the new rowid."""
    have = set(_columns(conn, table))
    cols = [c for c in row if c in have and (keep_id or c != "id")]
    cur = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [row[c] for c in cols])
    return cur.lastrowid


def _free(conn, table, row_id):
    return row_id is not None and conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is None


def discard_company(conn, company_id, remember=True):
    """Delete a company and everything under it (children first). With ``remember`` the name is recorded in
    discarded_companies with a full snapshot, so later emails for that name go to the quiet log and Restore works.
    Returns the number of events removed (0 if there is no such company: a second click does nothing)."""
    with _tx(conn):
        company = _rows(conn, "SELECT * FROM companies WHERE id = ?", (company_id,))
        if not company:
            return 0
        company = company[0]
        apps = _rows(conn, "SELECT * FROM applications WHERE company_id = ? ORDER BY id", (company_id,))
        marks = ",".join("?" * len(apps))
        app_ids = [a["id"] for a in apps]
        events = _rows(conn, f"SELECT * FROM events WHERE application_id IN ({marks}) ORDER BY id", app_ids) if apps else []
        contacts = _rows(conn, f"SELECT * FROM contacts WHERE application_id IN ({marks}) ORDER BY id", app_ids) if apps else []
        links = _rows(conn, "SELECT * FROM sender_links WHERE company_id = ? ORDER BY id", (company_id,))
        if remember:
            snapshot = {"company": company, "applications": apps, "events": events, "contacts": contacts, "sender_links": links,
                        "confirmed": company.get("confirmed_by_user"), "events_removed": len(events)}
            conn.execute("INSERT OR REPLACE INTO discarded_companies (name_key, name, discarded_at, snapshot) VALUES (?,?,?,?)",
                         (discard_key(company["name"]), company["name"], now(), json.dumps(snapshot)))
        if apps:
            conn.execute(f"DELETE FROM events WHERE application_id IN ({marks})", app_ids)
            conn.execute(f"DELETE FROM contacts WHERE application_id IN ({marks})", app_ids)
        conn.execute("DELETE FROM sender_links WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM applications WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    return len(events)


def restore_company(conn, name_key):
    """Rebuild a discarded company from its snapshot (confirmed if it was confirmed when discarded) and remove the discarded
    row. The original ids are used when they are free, otherwise new ones (children follow). Refuses, changing nothing, when
    nothing is discarded under ``name_key`` (LookupError), when a company with that key exists now, or when a stored email id is
    already filed elsewhere (ValueError). A sender link that has since been linked to another company is not restored.
    A seeded name has nothing to rebuild: its row is removed and None is returned."""
    with _tx(conn):
        row = conn.execute("SELECT id, snapshot FROM discarded_companies WHERE name_key = ?", (name_key,)).fetchone()
        if row is None:
            raise LookupError(f"nothing is discarded under the key {name_key!r}")
        snap = json.loads(row[1])
        if snap.get("seed"):
            conn.execute("DELETE FROM discarded_companies WHERE name_key = ?", (name_key,))
            return None
        for cid, name in conn.execute("SELECT id, name FROM companies").fetchall():
            if discard_key(name) == name_key:
                raise ValueError(f"a company with this name key exists now (id {cid}); restore refused")
        try:
            company = dict(snap["company"])
            cid = _insert(conn, "companies", company, keep_id=_free(conn, "companies", company.get("id")))
            app_map = {}
            for a in snap["applications"]:
                a = dict(a, company_id=cid)
                app_map[a["id"]] = _insert(conn, "applications", a, keep_id=_free(conn, "applications", a.get("id")))
            for table in ("events", "contacts"):
                for r in snap[table]:
                    r = dict(r, application_id=app_map[r["application_id"]])
                    _insert(conn, table, r, keep_id=_free(conn, table, r.get("id")))
            for l in snap["sender_links"]:
                l = dict(l, company_id=cid)
                have = conn.execute("SELECT 1 FROM sender_links WHERE kind = ? AND value = ?", (l["kind"], l["value"])).fetchone()
                if have is None:
                    _insert(conn, "sender_links", l, keep_id=_free(conn, "sender_links", l.get("id")))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"restore refused: {exc}") from exc
        conn.execute("DELETE FROM discarded_companies WHERE name_key = ?", (name_key,))
    return cid


def discarded_list(conn):
    """Every discarded name, newest first: when, how many quiet_log rows have been filed for it since (logged_at at or after the
    discard; a seeded name starts at 0), whether it was a seed, whether the company was confirmed, how many events were removed."""
    out = []
    for key, name, at, snapshot, n in conn.execute(
            "SELECT d.name_key, d.name, d.discarded_at, d.snapshot, "
            "(SELECT COUNT(*) FROM quiet_log q WHERE q.name_key = d.name_key AND q.logged_at >= d.discarded_at) "
            "FROM discarded_companies d ORDER BY d.discarded_at DESC, d.id DESC").fetchall():
        snap = json.loads(snapshot)
        out.append({"name_key": key, "name": name, "discarded_at": at, "emails_since": n, "seed": bool(snap.get("seed")),
                    "was_confirmed": None if snap.get("seed") else bool(snap.get("confirmed")),
                    "events_removed": None if snap.get("seed") else snap.get("events_removed")})
    return out


def add_seeds(conn, names):
    """seed_discarded's work, for a caller that already has a transaction open (scheduler/import_saved.py). Returns how many were added."""
    added = 0
    for name in names:
        cur = conn.execute("INSERT OR IGNORE INTO discarded_companies (name_key, name, discarded_at, snapshot) VALUES (?,?,?,?)",
                           (discard_key(name), name, now(), json.dumps({"seed": True})))
        added += cur.rowcount
    return added


def seed_discarded(conn, names):
    """Add names to the discarded list with a marker snapshot, skipping any key already there. Returns how many were added."""
    with _tx(conn):
        return add_seeds(conn, names)
