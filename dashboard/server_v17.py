"""Local server for the job-tracker dashboard v17: v16 plus a Calendar view in Timeline and upcoming dates. Port 8001 by default.

It IMPORTS dashboard/server_v16.py (not changed, not copied), which imports v15 and v14, so the data, every earlier button, the panel's save,
the board's Undo and the guards (127.0.0.1 only, Host and Origin checks, JSON POST only, size limit) run their own code. v17 adds:
  1 the page: index_v17.html and live_v17.js (copies of v16's with the List / Calendar switch, the calendar, the Coming up strip and the
    panel's Upcoming dates section). The calendar needs no new data: it is drawn from /api/data's Timeline list, which stays v16's.
  2 GET /api/v17/upcoming: John's upcoming dates that still belong to their application, and the fixed list of kinds. Kept out of
    /api/data on purpose, so every data response v16 sends stays exactly the same.
  3 POST /api/v17/save: v15's panel save plus add_dates and remove_dates, in ONE transaction. The page uses it only when a date is added or
    removed; any other Save still posts to /api/v15/save exactly as v16 does. Upcoming dates never change a status, stage, count or anything
    else v16 shows; they live in their own table (db/migrate_v17.py) and are logged in edit_log like every other change.
Run:  venv/bin/python dashboard/server_v17.py [--db db/tracker.db] [--port 8001] [--heartbeat logs/last_ok] [--confirm-log logs/confirm_all.log]
"""
from __future__ import annotations

import contextlib
import copy
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import server_v16 as X  # noqa: E402

W, V = X.W, X.V
VERSION = X.VERSION = W.VERSION = V.VERSION = "v17"     # meta() of v14 and main() of v15 read it
KINDS = ["Interview", "Phone screen", "Video interview", "Assessment deadline", "Assessment centre", "Other"]   # the panel offers these, Interview first
NOTE_MAX, MAX_DATE_ADDS, MAX_DATE_REMOVES = 200, 10, 50
DATE_KEYS = ("add_dates", "remove_dates")
UP_SQL = ("SELECT u.id, u.application_id, u.kind, u.due_date, u.due_time, u.note FROM upcoming_dates u "
          "JOIN applications a ON a.id = u.application_id JOIN companies c ON c.id = a.company_id "
          "WHERE u.removed_at IS NULL AND c.id = u.company_id AND c.name = u.company_name ORDER BY u.due_date, COALESCE(u.due_time, ''), u.id")


def upcoming():
    with contextlib.closing(V.ro()) as c:
        return [dict(zip(("id", "app_id", "kind", "date", "time", "note"), r)) for r in c.execute(UP_SQL)]


# v15's save, run inside a v17 save, joins the v17 save's transaction (one transaction per save); every other write opens its own, as before
_TX = V.tx


@contextlib.contextmanager
def tx():
    c = getattr(V._tls, "tx", None)
    if c is not None:
        yield c
        return
    with _TX() as c:
        yield c


V.tx = tx


def what(kind, day, time):
    return f"{kind} on {day}" + (f" at {time}" if time else "")


def check_dates(d):
    """Validate the date part of a v17 save before anything is opened. Returns (adds, removes)."""
    adds, removes = d.get("add_dates", []), d.get("remove_dates", [])
    if type(adds) is not list or len(adds) > MAX_DATE_ADDS:
        raise V.Bad(f"'add_dates' must be a list of at most {MAX_DATE_ADDS}")
    for u in adds:
        if type(u) is not dict or set(u) - {"kind", "date", "time", "note"}:
            raise V.Bad("each date needs a kind and a date, and may have a time and a note")
        if type(u.get("kind")) is not str or u["kind"] not in KINDS:
            raise V.Bad("'kind' must be one of: " + ", ".join(KINDS))
        if type(u.get("date")) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", u["date"]):
            raise V.Bad(f"Pick the date for the {u['kind']} (a date like 2026-11-05)")
        try:
            day = date.fromisoformat(u["date"])
        except ValueError:
            raise V.Bad("That date is not a real date")
        if not date(2000, 1, 1) <= day <= date(2099, 12, 31):
            raise V.Bad("The date must be between 2000 and 2099")
        t = u.get("time")
        if t is not None and (type(t) is not str or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t)):
            raise V.Bad("The time must be a time like 14:30, or left empty")
        n = u.get("note")
        if n is not None and (type(n) is not str or len(n) > NOTE_MAX):
            raise V.Bad(f"A date's note must be text of at most {NOTE_MAX} characters")
        u["note"] = (n or "").strip() or None
    if len({(u["kind"], u["date"]) for u in adds}) != len(adds):
        raise V.Bad("The same kind and date is in the request twice")
    if type(removes) is not list or len(removes) > MAX_DATE_REMOVES or len(set(map(repr, removes))) != len(removes):
        raise V.Bad("'remove_dates' must be a list of different date ids")
    for i in removes:
        V.need_id({"id": i})
    return adds, removes


def act_save(d):
    if set(d) - {"id", "rev", "fields", "add_steps", "remove_steps", *DATE_KEYS}:
        raise V.Bad("unknown key in the request")
    aid, rev = V.need_id(d), d.get("rev")
    if type(rev) is not int or not 0 <= rev < 2**63:
        raise V.Bad("'rev' must be a whole number")
    adds, removes = check_dates(d)
    rest = {k: v for k, v in d.items() if k not in DATE_KEYS}
    v15 = bool(rest.get("fields") or rest.get("add_steps") or rest.get("remove_steps"))
    if v15:
        W.check_save(copy.deepcopy(rest))                    # v15's checks, before anything is opened (act_save runs them again)
    elif not (adds or removes):
        raise V.Bad("There is nothing to save")
    when = W.STAMP().isoformat(timespec="seconds")
    with V.tx() as c:
        if v15:
            W.act_save(rest)                                  # v15's own save: its checks of the application and rev, its writes, its log
        row = c.execute("SELECT a.company_id, c.name FROM applications a JOIN companies c ON c.id = a.company_id WHERE a.id = ?", (aid,)).fetchone()
        if row is None:
            raise V.Refused("gone", "That application is gone (it was deleted). Nothing was saved.")
        if not v15 and c.execute("SELECT COUNT(*) FROM edit_log WHERE application_id = ?", (aid,)).fetchone()[0] != rev:
            raise V.Refused("changed", "This application was changed somewhere else after the panel opened. Nothing was saved.")
        cid, cname = row

        def log(field, old, new):
            c.execute("INSERT INTO edit_log (application_id, company_id, company_name, field, old_value, new_value, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (aid, cid, cname, field, old, new, when))

        for i in removes:
            r = c.execute("SELECT kind, due_date, due_time FROM upcoming_dates WHERE id = ? AND application_id = ? AND company_id = ? AND company_name = ? "
                          "AND removed_at IS NULL", (i, aid, cid, cname)).fetchone()
            if r is None:
                raise V.Refused("changed", "A date you removed is already gone. Nothing was saved.")
            c.execute("UPDATE upcoming_dates SET removed_at = ? WHERE id = ?", (when, i))
            log("date removed", what(*r), None)
        for u in adds:
            if c.execute("SELECT 1 FROM upcoming_dates WHERE application_id = ? AND company_id = ? AND company_name = ? AND kind = ? AND due_date = ? "
                         "AND removed_at IS NULL", (aid, cid, cname, u["kind"], u["date"])).fetchone():
                raise V.Refused("duplicate", f"This application already has {u['kind']} on {u['date']}. Nothing was saved.")
            c.execute("INSERT INTO upcoming_dates (application_id, company_id, company_name, kind, due_date, due_time, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (aid, cid, cname, u["kind"], u["date"], u.get("time"), u["note"], when))
            log("date added", None, what(u["kind"], u["date"], u.get("time")))
    return {"application_id": aid}


# GET /api/v17/upcoming: the same guards as every other request (v14's _allowed), read only
_GET = V.Handler.do_GET


def do_GET(self):
    if self.path.split("?")[0] != "/api/v17/upcoming":
        return _GET(self)
    if not self._allowed():
        return
    try:
        self._json(200, {"upcoming": upcoming(), "kinds": KINDS})
    except sqlite3.OperationalError as e:
        self._json(503 if V._busy(e) else 500, {"ok": False, "error": "The database is busy. Try again in a few seconds." if V._busy(e) else "Could not read the database."})


V.Handler.do_GET = do_GET


def check_database() -> None:
    CHECK_V15()
    with contextlib.closing(V.ro()) as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'upcoming_dates'").fetchone() is None:
            raise SystemExit("The database has no upcoming_dates (run db/migrate_v17.py --apply first). Nothing was started.")


CHECK_V15, W.check_database = W.check_database, check_database   # v15's main() calls it before serving
V.ROUTES["/api/v17/save"] = act_save
V.PAGES.clear()
V.PAGES.update({"/": ("index_v17.html", "text/html; charset=utf-8"), "/index.html": ("index_v17.html", "text/html; charset=utf-8"),
                "/live_v17.js": ("live_v17.js", "application/javascript; charset=utf-8")})
main = W.main                               # v15's: --db (default the live database), --port (default 8001), the same checks before serving


if __name__ == "__main__":
    main()
