"""Local server for the job-tracker dashboard v18: v17 plus the automatic role and industry. Port 8001 by default.

It IMPORTS dashboard/server_v17.py (not changed, not copied), which imports v16, v15 and v14, so the data, every button, the panel's save,
the board, the calendar and the guards (127.0.0.1 only, Host and Origin checks, JSON POST only, size limit) run their own code. v18 adds:
  1 the page: index_v18.html and live_v18.js (copies of v17's with the "Automatic: ..." line in the panel and the same words as tooltips).
  2 ONE function, fill(), puts the automatic value under the sync's own value: the sync's value (applications.role_title, companies.industry)
    if it has one, else the automatic value (tables auto_roles and company_industries, written by scheduler/enrich.py), else empty. v15's own
    resolve() then puts John's edit on top, unchanged. fill() goes in through v15's three hooks, which every view and count already reads:
    load() (the values v15 calls automatic, so Reset to automatic goes back to them), and the two v13 frames APPS_DF and EVENTS_DF (Pipeline,
    search, filters, Kanban, hover card, Timeline, Just moved, Gone quiet, By industry / role, Metrics). So every view shows the same value.
  3 pipeline rows whose shown automatic value came from those tables carry "v18" (value, date, from) for the page's wording. With the new
    tables empty no row has it and nothing is filled, so every response is v17's exactly.
An automatic value counts only while its application still has the company id and name it was read for, and only while the email it was read
from is still linked to that application and still holds those words; an industry only if it is an item of the fixed list (scheduler/enrich.py).
This server makes no network call (the only search is in scheduler/enrich.py) and writes nothing new: the buttons write exactly as v17's do.
Run:  venv/bin/python dashboard/server_v18.py [--db db/tracker.db] [--port 8001] [--heartbeat logs/last_ok] [--confirm-log logs/confirm_all.log]
"""
from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import server_v17 as Y  # noqa: E402
from scheduler.enrich import INDUSTRIES, NEW_TABLES, company_key  # noqa: E402  (pure helpers; this server never calls the network code)

X, W, V = Y.X, Y.W, Y.V
VERSION = Y.VERSION = X.VERSION = W.VERSION = V.VERSION = "v18"     # meta() of v14 and main() of v15 read it

ROLE_SQL = ("SELECT r.application_id, r.role, r.email_date, e.detail_text FROM auto_roles r JOIN applications a ON a.id = r.application_id "
            "JOIN companies c ON c.id = a.company_id JOIN events e ON e.application_id = r.application_id AND e.source_email_id = r.email_id "
            "WHERE c.id = r.company_id AND c.name = r.company_name")
NAME_SQL = ("SELECT a.id, COALESCE(x.display_name, c.name) FROM applications a JOIN companies c ON c.id = a.company_id "
            "LEFT JOIN application_edits x ON x.application_id = a.id AND x.company_id = c.id AND x.company_name = c.name")
IND_SQL = "SELECT company_key, industry, substr(looked_up_at, 1, 10) FROM company_industries WHERE industry IS NOT NULL"
_squash = lambda s: " ".join(str(s or "").split()).casefold()  # noqa: E731


def automatic(c):
    """{application id: {"role": (value, date, "email"), "industry": (value, date, "web")}}: what the new tables hold for each application."""
    out = {}
    for aid, role, day, text in c.execute(ROLE_SQL):
        if _squash(role) and _squash(role) in _squash(text):          # only ever words that are in that stored email
            out.setdefault(aid, {})["role"] = (role, day, "email")
    inds = {k: (ind, day) for k, ind, day in c.execute(IND_SQL) if ind in INDUSTRIES}
    if inds:
        for aid, shown in c.execute(NAME_SQL):
            hit = inds.get(company_key(shown))
            if hit:
                out.setdefault(aid, {})["industry"] = (hit[0], hit[1], "web")
    return out


def _blank(v):
    return not V._s(v).strip()


def fill(aid, role, industry, auto):
    """THE rule under John's edit, for every view: the sync's value if it has one, else the automatic value, else the empty value as it was."""
    a = auto.get(aid)
    if not a:
        return role, industry
    return (a["role"][0] if "role" in a and _blank(role) else role), (a["industry"][0] if "industry" in a and _blank(industry) else industry)


_tls = threading.local()


def current():
    """The automatic values of the payload being built (set by load), or read now when a frame is asked for outside a payload."""
    auto = getattr(_tls, "auto", None)
    if auto is None:
        with contextlib.closing(V.ro()) as c:
            auto = automatic(c)
    return auto


_LOAD, _APPS, _EVENTS, _PAYLOAD = W.load, W.APPS_DF, W.EVENTS_DF, V.build_payload


def load(c):
    out = _LOAD(c)
    _tls.auto, _tls.sync = automatic(c), {aid: (a["role"], a["industry"]) for aid, a in out[0].items()}
    for aid, a in out[0].items():
        a["role"], a["industry"] = fill(aid, a["role"], a["industry"], _tls.auto)
    return out


def apps_df():
    df, auto = _APPS(), current()
    if auto:
        for i, aid in zip(df.index, df["id"]):
            if int(aid) in auto:
                df.at[i, "role_title"], df.at[i, "industry"] = fill(int(aid), df.at[i, "role_title"], df.at[i, "industry"], auto)
    return df


def events_df():
    df, auto = _EVENTS(), current()
    if auto:
        for i, aid in zip(df.index, df["application_id"]):
            if int(aid) in auto:
                df.at[i, "role_title"], df.at[i, "industry"] = fill(int(aid), df.at[i, "role_title"], df.at[i, "industry"], auto)
    return df


def build_payload() -> dict:
    try:
        p = _PAYLOAD()
        auto, sync = _tls.auto, _tls.sync
        for r in p["pipeline"]:
            a, (role, industry) = auto.get(r["id"]), sync.get(r["id"], (None, None))
            why = {f: {"value": a[f][0], "date": a[f][1], "from": a[f][2]} for f, own in (("role", role), ("industry", industry)) if a and f in a and _blank(own)}
            if why:
                r["v18"] = why
        return p
    finally:
        _tls.auto = _tls.sync = None


W.load, W.APPS_DF, W.EVENTS_DF, V.build_payload = load, apps_df, events_df, build_payload


def check_database() -> None:
    CHECK_V17()
    with contextlib.closing(V.ro()) as c:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if missing := [t for t in NEW_TABLES if t not in have]:
        raise SystemExit(f"The database has no {', '.join(missing)} (run db/migrate_v18.py --apply first). Nothing was started.")


CHECK_V17, W.check_database = W.check_database, check_database   # v15's main() calls it before serving
V.PAGES.clear()
V.PAGES.update({"/": ("index_v18.html", "text/html; charset=utf-8"), "/index.html": ("index_v18.html", "text/html; charset=utf-8"),
                "/live_v18.js": ("live_v18.js", "application/javascript; charset=utf-8")})
main = W.main                               # v15's: --db (default the live database), --port (default 8001), the same checks before serving


if __name__ == "__main__":
    main()
