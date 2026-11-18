"""Local server for the job-tracker dashboard v15: v14 plus John's own edits and steps (the edit panel). Port 8001 by default.

It IMPORTS dashboard/server_v14.py (not changed, not copied), so every v14 button (Confirm, Discard, Delete, Mark submitted, Restore,
Confirm all, Add to existing company), the guards (127.0.0.1 only, Host and Origin checks, JSON POST only, size limit) and the header
run v14's own code and behave exactly as before. v15 adds:
  1 the page: index_v15.html and live_v15.js (copies of v14's with the panel; no outside font, so it works offline)
  2 /api/data: v14's own build_payload, fed with the EFFECTIVE values: app.apps_df and app.events_df are replaced (below) so that v14's
    code computes the counts, cards, filters, search, Metrics and Gone quiet from the same values the table shows. Then v15 rebuilds
    Timeline, Just moved and Gone quiet with the application id (for the panel) and the steps, and adds "v15" to every pipeline row.
  3 POST /api/v15/save: ONE transaction that writes only the four v15 tables (db/migrate_v15.py); the existing tables are never written.
The rules:
  company display name, role, industry, notes: John's value (application_edits; NULL = automatic) wins until he resets it.
  status and stage: the newest dated event wins. The emails' value (applications.current_status / current_stage) is dated
    applications.last_updated (the email's day); a step is dated the day John picks; a status or stage edit the day he saved it. The same
    day goes to John. So a later email takes over again, and nothing a real email says stays hidden.
  last activity: the later of last_updated and John's newest step.
  An edit or step counts only while its application still has the company id and name it was made for (ids are reused after a Delete).
With no edits and no steps every value v14 returns is returned unchanged (scratch/v15/test_v15.py compares it with v14 itself).
Run:  venv/bin/python dashboard/server_v15.py [--db db/tracker.db] [--port 8001] [--heartbeat logs/last_ok] [--confirm-log logs/confirm_all.log]
"""
from __future__ import annotations

import argparse
import contextlib
import re
import sys
import threading
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import app, server_v14 as V  # noqa: E402

VERSION = V.VERSION = "v15"                 # meta() of v14 reads it
V14_PAYLOAD = V.build_payload
NEW_TABLES = ("application_edits", "manual_steps", "step_types", "edit_log")
LIMITS = {"company": 120, "role": 150, "industry": 80, "stage": 120, "notes": 2000}
COLUMN = {"company": "display_name", "role": "role", "industry": "industry", "notes": "notes", "status": "status", "stage": "stage"}
STEP_LABEL_MAX, STEP_NOTE_MAX, MAX_ADDS, MAX_REMOVES = 60, 500, 20, 100
# the status list of the panel (the sync's closed set, app.STATUS_LABEL); the two interview statuses share the label "Interview" in the table
STATUS_CHOICES = [[s, {"interview_invite": "Interview (invited)", "interview_complete": "Interview (done)"}.get(s, label)] for s, label in app.STATUS_LABEL.items()]
STATUS_FILTER = list(dict.fromkeys(app.STATUS_LABEL[s] for s in app.STATUS_ORDER))   # the Pipeline status filter, by label
STAMP = lambda: datetime.now().astimezone()  # noqa: E731  (tests replace it to set the day)


# --- the effective values ------------------------------------------------------------------------------------------
def resolve(auto, edit, steps):
    """The values shown for one application. auto = {company, role, industry, status, stage, last} from the existing tables; edit = its
    application_edits row as a dict, or None; steps = John's steps (dicts with date, created_at, label, sets_status), any order.
    Returns those six keys, notes, "mine" (the fields where John's value is the one shown) and "by" (email / step / edit for status and stage)."""
    e, out = edit or {}, {}
    for f, col in (("company", "display_name"), ("role", "role"), ("industry", "industry"), ("notes", "notes")):
        out[f] = e[col] if e.get(col) is not None else auto.get(f)
    mine = {f for f, col in (("company", "display_name"), ("role", "role"), ("industry", "industry"), ("notes", "notes")) if e.get(col) is not None}
    day = auto.get("last") or ""
    # (date, John?, when John entered it, value, by): the newest date wins, then John over the emails, then John's later entry over his earlier one
    status, stage = [(day, 0, "", auto.get("status"), "email")], [(day, 0, "", auto.get("stage"), "email")]
    for s in sorted(steps, key=lambda s: (s["date"], s["created_at"])):
        if s.get("sets_status"):
            status.append((s["date"], 1, s["created_at"], s["sets_status"], "step"))
        stage.append((s["date"], 1, s["created_at"], s["label"], "step"))
    if e.get("status") is not None:
        status.append((e["status_at"][:10], 1, e["status_at"], e["status"], "edit"))
    if e.get("stage") is not None:
        stage.append((e["stage_at"][:10], 1, e["stage_at"], e["stage"], "edit"))
    by = {}
    for f, cands in (("status", status), ("stage", stage)):
        win = max(range(len(cands)), key=lambda i: (*cands[i][:3], i))
        out[f], by[f] = cands[win][3], cands[win][4]
        if by[f] != "email":
            mine.add(f)
    out["last"] = max([auto.get("last") or ""] + [s["date"] for s in steps]) or auto.get("last")
    return {**out, "mine": mine, "by": by}


def next_step(types, status, done):
    """The step type the panel offers first: the first type suggested for this status (step_types.suggest_after), or the type after the
    furthest one already done if that is further on, skipping types already done. done = lower case labels of the row's steps and of its current stage text."""
    start = next((i for i, t in enumerate(types) if status in (t["suggest_after"] or "").split(",")), 0)
    start = max([start] + [i + 1 for i, t in enumerate(types) if t["label"].lower() in done])
    return next((t["id"] for t in types[start:] + types[:start] if t["label"].lower() not in done), types[0]["id"] if types else None)


AUTO_SQL = ("SELECT a.id, a.company_id, c.name, a.role_title, c.industry, a.current_status, a.current_stage, a.last_updated "
            "FROM applications a JOIN companies c ON c.id = a.company_id ORDER BY a.id")
EDIT_SQL = ("SELECT x.application_id, x.display_name, x.role, x.industry, x.notes, x.status, x.status_at, x.stage, x.stage_at FROM application_edits x "
            "JOIN applications a ON a.id = x.application_id JOIN companies c ON c.id = a.company_id WHERE c.id = x.company_id AND c.name = x.company_name")
STEP_SQL = ("SELECT s.id, s.application_id, s.label, s.step_date, s.note, s.created_at, t.sets_status, t.counts_as FROM manual_steps s "
            "JOIN step_types t ON t.id = s.step_type_id JOIN applications a ON a.id = s.application_id JOIN companies c ON c.id = a.company_id "
            "WHERE s.removed_at IS NULL AND c.id = s.company_id AND c.name = s.company_name ORDER BY s.step_date, s.created_at, s.id")
TYPE_SQL = "SELECT id, label, sets_status, counts_as, suggest_after FROM step_types WHERE active = 1 ORDER BY sort_order, id"


def load(c):
    """auto values of every application, John's edits and steps that still belong to their application, the step types, edit counts."""
    auto = {r[0]: {"company_id": r[1], "company": r[2], "role": r[3], "industry": r[4], "status": r[5], "stage": r[6], "last": r[7]} for r in c.execute(AUTO_SQL)}
    edits = {r[0]: dict(zip(("display_name", "role", "industry", "notes", "status", "status_at", "stage", "stage_at"), r[1:])) for r in c.execute(EDIT_SQL)}
    steps = {}
    for r in c.execute(STEP_SQL):
        steps.setdefault(r[1], []).append(dict(zip(("id", "app", "label", "date", "note", "created_at", "sets_status", "counts_as"), r)))
    types = [dict(zip(("id", "label", "sets_status", "counts_as", "suggest_after"), r)) for r in c.execute(TYPE_SQL)]
    revs = dict(c.execute("SELECT application_id, COUNT(*) FROM edit_log GROUP BY application_id"))
    # the panel's "Changes you made", newest first: the edit log of the application as it is now (not of a deleted one whose id was reused);
    # "step added" is left out because "Steps you recorded" lists those
    changes = {}
    for aid, cid, cname, field, old, new, at in c.execute("SELECT application_id, company_id, company_name, field, old_value, new_value, logged_at "
                                                          "FROM edit_log WHERE field <> 'step added' ORDER BY id DESC"):
        if aid in auto and (auto[aid]["company_id"], auto[aid]["company"]) == (cid, cname):
            changes.setdefault(aid, []).append({"at": at, "field": field, "old": old, "new": new})
    return auto, edits, steps, types, revs, changes


# v14's build_payload reads these two; while a v15 payload is being built they return the effective values (_tls.eff), otherwise v14's own
_tls = threading.local()
APPS_DF, EVENTS_DF = app.apps_df, app.events_df
NAME_COLS = (("company", "company"), ("role_title", "role"), ("industry", "industry"))


def effective_apps_df():
    df, eff = APPS_DF(), getattr(_tls, "eff", None) or {}
    pos = {int(a): i for i, a in zip(df.index, df["id"])}
    for aid, e in eff.items():
        if aid in pos:
            for col, key in NAME_COLS + (("current_status", "status"), ("current_stage", "stage"), ("last_updated", "last")):
                df.at[pos[aid], col] = e[key]
    return df


def effective_events_df():
    """The email events with the effective names, plus John's steps that count as an event type (step_types.counts_as), by date."""
    df, eff, steps = EVENTS_DF(), getattr(_tls, "eff", None) or {}, getattr(_tls, "steps", None) or {}
    for aid, e in eff.items():
        m = df["application_id"] == aid
        if m.any():
            for col, key in NAME_COLS:
                df.loc[m, col] = e[key]
    _tls.emails = df
    rows = [{"application_id": aid, "event_type": s["counts_as"], "event_date": s["date"], "detail_text": s["note"] or "",
             "role_title": eff[aid]["role"], "company": eff[aid]["company"], "industry": eff[aid]["industry"]}
            for aid, ss in steps.items() if aid in eff for s in ss if s["counts_as"]]
    if rows:
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True).sort_values("event_date", kind="mergesort", ignore_index=True)
    return df


app.apps_df, app.events_df = effective_apps_df, effective_events_df


def build_payload() -> dict:
    with contextlib.closing(V.ro()) as c:
        auto, edits, steps, types, revs, changes = load(c)
    eff = {aid: resolve(auto[aid], edits.get(aid), steps.get(aid, [])) for aid in set(edits) | set(steps) if aid in auto}
    _tls.eff, _tls.steps = eff, steps
    try:
        p = V14_PAYLOAD()
        emails = _tls.emails
    finally:
        _tls.eff = _tls.steps = _tls.emails = None

    def shown(aid, key):
        return eff[aid][key] if aid in eff else auto[aid][key]

    for r in p["pipeline"]:
        aid, a, e = r["id"], auto[r["id"]], edits.get(r["id"]) or {}
        done = {s["label"].lower() for s in steps.get(aid, [])} | {(shown(aid, "stage") or "").strip().lower()}
        r["v15"] = {
            "mine": sorted(eff[aid]["mine"]) if aid in eff else [], "by": eff[aid]["by"] if aid in eff else {"status": "email", "stage": "email"},
            "auto": {"company": V._s(a["company"]), "role": V._s(a["role"]), "industry": V._s(a["industry"]), "status": V._s(a["status"]),
                     "status_label": app.STATUS_LABEL.get(a["status"], V._s(a["status"])), "stage": V._s(a["stage"]), "last": V._s(a["last"])},
            "edits": {f: e.get(col) for f, col in COLUMN.items()} | {"status_at": e.get("status_at"), "stage_at": e.get("stage_at")},
            "notes": V._s(shown(aid, "notes")) if aid in eff else "",
            "stage_raw": V._s(shown(aid, "stage")),
            "steps": [{k: s[k] for k in ("id", "label", "date", "note")} for s in steps.get(aid, [])],
            "rev": int(revs.get(aid, 0)), "next": next_step(types, shown(aid, "status"), done), "changes": changes.get(aid, []),
        }

    # Timeline, Just moved and Gone quiet: v14's lists, rebuilt with the application id, and Timeline with John's steps ("source": "you")
    evrecs = emails.to_dict(orient="records")
    timeline = [{"company": V._s(x["company"]) or "—", "role": V._s(x["role_title"]), "industry": app._or(x["industry"], "Not recorded"),
                 "status": app.STATUS_LABEL.get(x["event_type"], V._s(x["event_type"])), "cat": V.STATUS_CAT.get(x["event_type"], "applied"),
                 "date": V._s(x["event_date"]), "detail": V._s(x["detail_text"])[:220], "app_id": int(x["application_id"]), "source": "email"}
                for x in evrecs if app._d(x["event_date"])]
    timeline += [{"company": V._s(eff[aid]["company"]) or "—", "role": V._s(eff[aid]["role"]), "industry": app._or(eff[aid]["industry"], "Not recorded"),
                  "status": s["label"], "cat": V.STATUS_CAT.get(s["counts_as"], "other"), "date": s["date"], "detail": V._s(s["note"]),
                  "app_id": aid, "source": "you"} for aid, ss in steps.items() if aid in eff for s in ss]
    p["timeline"] = sorted(timeline, key=lambda t: t["date"])            # stable: with no steps, v14's order
    p["just_moved"] = [{"company": V._s(x["company"]) or "—", "role": V._s(x["role_title"]),
                        "status": app.STATUS_LABEL.get(x["event_type"], V._s(x["event_type"])), "cat": V.STATUS_CAT.get(x["event_type"], "applied"),
                        "date": V._s(x["event_date"]), "app_id": int(x["application_id"])} for x in evrecs[-8:][::-1]]
    last_signal = {}
    for x in evrecs:
        last_signal.setdefault(int(x["application_id"]), []).append(V._s(x["event_date"]))
    for aid, ss in steps.items():
        if aid in eff:
            last_signal.setdefault(aid, []).extend(s["date"] for s in ss)
    today, stale = date.today(), []
    for aid in auto:
        if shown(aid, "status") in app.TERMINAL:
            continue
        ld = app._d(max(last_signal[aid])) if last_signal.get(aid) else app._d(shown(aid, "last"))
        if ld and (today - ld).days >= app.STALE_DAYS:
            stale.append({"company": V._s(shown(aid, "company")) or "—", "role": V._s(shown(aid, "role")), "stage": V._s(shown(aid, "stage")) or "—",
                          "last": str(ld), "days": (today - ld).days, "app_id": aid})
    p["stale"] = stale
    p["step_types"] = [{k: t[k] for k in ("id", "label", "sets_status")} for t in types]
    p["status_choices"], p["status_filter"] = STATUS_CHOICES, STATUS_FILTER
    return p


V.build_payload = build_payload


# --- the save: one transaction, the four v15 tables only ----------------------------------------------------------------
def need_date(v, today) -> str:
    if type(v) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise V.Bad("'date' must be a date like 2026-10-25")
    try:
        d = date.fromisoformat(v)
    except ValueError:
        raise V.Bad("'date' is not a real date")
    if not date(2000, 1, 1) <= d <= today:
        raise V.Bad("'date' must be between 2000 and today")
    return v


def check_save(d):
    """Validate the whole request before anything is opened. Returns (id, rev, fields, adds, removes)."""
    if set(d) - {"id", "rev", "fields", "add_steps", "remove_steps"}:
        raise V.Bad("unknown key in the request")
    aid, rev = V.need_id(d), d.get("rev")
    if type(rev) is not int or not 0 <= rev < 2**63:
        raise V.Bad("'rev' must be a whole number")
    fields, adds, removes = d.get("fields", {}), d.get("add_steps", []), d.get("remove_steps", [])
    if type(fields) is not dict or set(fields) - set(COLUMN):
        raise V.Bad("'fields' may only hold company, role, industry, notes, status and stage")
    for f, v in fields.items():
        if v is None:
            continue
        if f == "status":
            if v not in app.STATUS_LABEL:
                raise V.Bad("'status' is not a known status")
        elif type(v) is not str or len(v) > LIMITS[f]:
            raise V.Bad(f"'{f}' must be text of at most {LIMITS[f]} characters")
        else:
            fields[f] = v.strip()
            if f == "company" and not fields[f]:
                raise V.Bad("The company name cannot be empty (use Reset to automatic instead)")
            if f == "notes" and not fields[f]:
                fields[f] = None
    today = STAMP().date()
    if type(adds) is not list or len(adds) > MAX_ADDS:
        raise V.Bad(f"'add_steps' must be a list of at most {MAX_ADDS}")
    for s in adds:
        if type(s) is not dict or set(s) - {"type_id", "new_type", "date", "note"} or (("type_id" in s) == ("new_type" in s)):
            raise V.Bad("each step needs a date and either type_id or new_type")
        if "type_id" in s:
            V.need_id(s, "type_id")
        else:
            t = s["new_type"]
            if type(t) is not dict or set(t) - {"label", "sets_status"} or type(t.get("label")) is not str or not 0 < len(t["label"].strip()) <= STEP_LABEL_MAX:
                raise V.Bad(f"a new step type needs a name of 1 to {STEP_LABEL_MAX} characters")
            if t.get("sets_status") is not None and t["sets_status"] not in app.STATUS_LABEL:
                raise V.Bad("'sets_status' is not a known status")
            t["label"] = " ".join(t["label"].split())
        need_date(s.get("date"), today)
        if s.get("note") is not None and (type(s["note"]) is not str or len(s["note"]) > STEP_NOTE_MAX):
            raise V.Bad(f"a step note must be text of at most {STEP_NOTE_MAX} characters")
    if type(removes) is not list or len(removes) > MAX_REMOVES or len(set(map(repr, removes))) != len(removes):
        raise V.Bad("'remove_steps' must be a list of different step ids")
    for i in removes:
        V.need_id({"id": i})
    if not (fields or adds or removes):
        raise V.Bad("There is nothing to save")
    return aid, rev, fields, adds, removes


def act_save(d):
    aid, rev, fields, adds, removes = check_save(d)
    when = STAMP().isoformat(timespec="seconds")
    with V.tx() as c:
        row = c.execute("SELECT a.company_id, c.name FROM applications a JOIN companies c ON c.id = a.company_id WHERE a.id = ?", (aid,)).fetchone()
        if row is None:
            raise V.Refused("gone", "That application is gone (it was deleted). Nothing was saved.")
        if c.execute("SELECT COUNT(*) FROM edit_log WHERE application_id = ?", (aid,)).fetchone()[0] != rev:
            raise V.Refused("changed", "This application was changed somewhere else after the panel opened. Nothing was saved.")
        cid, cname = row

        def log(field, old, new):
            c.execute("INSERT INTO edit_log (application_id, company_id, company_name, field, old_value, new_value, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (aid, cid, cname, field, old, new, when))

        if fields:
            old = c.execute("SELECT company_id, company_name, " + ", ".join(COLUMN.values()) + " FROM application_edits WHERE application_id = ?", (aid,)).fetchone()
            if old and tuple(old[:2]) != (cid, cname):         # left by a deleted application whose id was reused: not this application's
                c.execute("DELETE FROM application_edits WHERE application_id = ?", (aid,))
                old = None
            if old is None:
                c.execute("INSERT INTO application_edits (application_id, company_id, company_name, edited_at) VALUES (?, ?, ?, ?)", (aid, cid, cname, when))
            stored = dict(zip(COLUMN, old[2:])) if old else {}
            for f, v in fields.items():
                col = COLUMN[f]                                  # from the fixed table above, never from the request
                if f in ("status", "stage") and (v is not None or stored.get(f) is not None):   # a dated event, even when the value is the same
                    c.execute(f"UPDATE application_edits SET {col} = ?, {col}_at = ?, edited_at = ? WHERE application_id = ?",
                              (v, None if v is None else when, when, aid))
                elif v != stored.get(f):
                    c.execute(f"UPDATE application_edits SET {col} = ?, edited_at = ? WHERE application_id = ?", (v, when, aid))
                else:
                    continue
                log(f, stored.get(f), v)

        new_types = {}
        for s in adds:
            if "type_id" in s:
                t = c.execute("SELECT id, label FROM step_types WHERE id = ? AND active = 1", (s["type_id"],)).fetchone()
                if t is None:
                    raise V.Refused("changed", "That step type is no longer offered. Nothing was saved.")
            else:
                label, sets = s["new_type"]["label"], s["new_type"].get("sets_status")
                t = new_types.get(label.lower()) or c.execute("SELECT id, label FROM step_types WHERE label = ?", (label,)).fetchone()
                if t is None:
                    order = c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM step_types").fetchone()[0]
                    tid = c.execute("INSERT INTO step_types (label, sets_status, counts_as, suggest_after, sort_order, active, created_at) "
                                    "VALUES (?, ?, ?, NULL, ?, 1, ?)", (label, sets, sets, order, when)).lastrowid
                    t = (tid, label)
                    log("step type added", None, label + (f" (sets status {sets})" if sets else ""))
                else:
                    c.execute("UPDATE step_types SET active = 1 WHERE id = ?", (t[0],))
                new_types[label.lower()] = t
            c.execute("INSERT INTO manual_steps (application_id, company_id, company_name, step_type_id, label, step_date, note, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (aid, cid, cname, t[0], t[1], s["date"], (s.get("note") or "").strip() or None, when))
            log("step added", None, f"{t[1]} on {s['date']}")

        for sid in removes:
            r = c.execute("SELECT label, step_date FROM manual_steps WHERE id = ? AND application_id = ? AND company_id = ? AND company_name = ? AND removed_at IS NULL",
                          (sid, aid, cid, cname)).fetchone()
            if r is None:
                raise V.Refused("changed", "A step you removed is already gone. Nothing was saved.")
            c.execute("UPDATE manual_steps SET removed_at = ? WHERE id = ?", (when, sid))
            log("step removed", f"{r[0]} on {r[1]}", None)
    return {"application_id": aid}


V.ROUTES["/api/v15/save"] = act_save
V.PAGES.clear()
V.PAGES.update({"/": ("index_v15.html", "text/html; charset=utf-8"), "/index.html": ("index_v15.html", "text/html; charset=utf-8"),
                "/live_v15.js": ("live_v15.js", "application/javascript; charset=utf-8")})


def check_database() -> None:
    V.check_database()
    with contextlib.closing(V.ro()) as c:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if missing := [t for t in NEW_TABLES if t not in have]:
        raise SystemExit(f"The database has no {', '.join(missing)} (run db/migrate_v15.py --apply first). Nothing was started.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Job Tracker dashboard v15")
    ap.add_argument("--db", default=str(V.CFG["db"]))
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--heartbeat", default=str(V.CFG["heartbeat"]))
    ap.add_argument("--confirm-log", default=str(V.CFG["confirm_log"]))
    a = ap.parse_args(argv)
    if not Path(a.db).is_file():
        raise SystemExit(f"{a.db} is not a file. Nothing was started.")
    V.configure(a.db, a.heartbeat, a.confirm_log)
    check_database()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", a.port), V.Handler)
    except OSError as e:
        raise SystemExit(f"Port {a.port} is not available ({e.strerror}). Nothing was started or stopped.")
    print(f"Job Tracker dashboard {VERSION} → http://localhost:{a.port}   database: {a.db}   heartbeat file: {a.heartbeat}   (Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
