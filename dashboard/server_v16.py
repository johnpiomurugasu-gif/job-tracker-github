"""Local server for the job-tracker dashboard v16: v15 plus the Kanban board in Pipeline. Port 8001 by default.

It IMPORTS dashboard/server_v15.py (not changed, not copied), which imports v14, so the data, every v14 and v15 button, the edit panel's
save (/api/v15/save) and the guards (127.0.0.1 only, Host and Origin checks, JSON POST only, size limit) run their own code. v16 adds:
  1 the page: index_v16.html and live_v16.js (copies of v15's with the Normal / Kanban switch and the board). The board needs no new data:
    /api/data is v15's, only its version tag says v16.
  2 a board move IS a v15 panel save of the Status field alone (the page posts to /api/v15/save), so it is stored, checked and logged
    exactly as that save is: application_edits.status dated now, the newest dated entry wins, a newer email takes over again.
  3 POST /api/v16/undo_move: Undo of the newest move. v15's save dates every status "now", so it cannot put back an earlier status of
    John's with its own date; this does, writing the same two columns (application_edits.status, status_at) in ONE transaction and
    logging it in edit_log like any other change. It refuses unless the move is still the newest change on that application and the
    value and date it puts back are ones the edit log shows this application had.
Run:  venv/bin/python dashboard/server_v16.py [--db db/tracker.db] [--port 8001] [--heartbeat logs/last_ok] [--confirm-log logs/confirm_all.log]
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import app, server_v15 as W  # noqa: E402

V = W.V
VERSION = W.VERSION = V.VERSION = "v16"     # meta() of v14 and main() of v15 read it
STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")   # what W.STAMP().isoformat(timespec="seconds") writes


def check_undo(d):
    """Validate the whole request before anything is opened. Returns (id, rev, status, status_at): what John's status was before the move."""
    if set(d) != {"id", "rev", "status", "status_at"}:
        raise V.Bad("the request needs exactly id, rev, status and status_at")
    aid, rev, status, at = V.need_id(d), d["rev"], d["status"], d["status_at"]
    if type(rev) is not int or not 0 <= rev < 2**63:
        raise V.Bad("'rev' must be a whole number")
    if status is not None and (type(status) is not str or status not in app.STATUS_LABEL):
        raise V.Bad("'status' is not a known status")
    if (status is None) != (at is None) or (at is not None and (type(at) is not str or not STAMP_RE.fullmatch(at))):
        raise V.Bad("'status_at' must be the time of John's earlier status, or null with a null status")
    if at is not None:
        try:
            datetime.fromisoformat(at)
        except ValueError:
            raise V.Bad("'status_at' is not a real time")
    return aid, rev, status, at


def act_undo_move(d):
    aid, rev, status, at = check_undo(d)
    when = W.STAMP().isoformat(timespec="seconds")
    with V.tx() as c:
        row = c.execute("SELECT a.company_id, c.name FROM applications a JOIN companies c ON c.id = a.company_id WHERE a.id = ?", (aid,)).fetchone()
        if row is None:
            raise V.Refused("gone", "That application is gone (it was deleted). Nothing was changed.")
        if c.execute("SELECT COUNT(*) FROM edit_log WHERE application_id = ?", (aid,)).fetchone()[0] != rev:
            raise V.Refused("changed", "This application was changed again after the move, so Undo was not done. Nothing was changed.")
        cid, cname = row
        move = c.execute("SELECT id, company_id, company_name, field, old_value, new_value FROM edit_log WHERE application_id = ? ORDER BY id DESC LIMIT 1",
                         (aid,)).fetchone()
        cur = c.execute("SELECT company_id, company_name, status FROM application_edits WHERE application_id = ?", (aid,)).fetchone()
        # the newest change on this application must be a status move to the value it still has, from the value Undo puts back
        if (move is None or tuple(move[1:4]) != (cid, cname, "status") or move[5] is None or move[4] != status
                or cur is None or tuple(cur) != (cid, cname, move[5])):
            raise V.Refused("changed", "There is no move to undo on this application any more. Nothing was changed.")
        # an earlier status of John's comes back with its own date: one this application really had (a status save of that value at that time)
        if status is not None and c.execute("SELECT 1 FROM edit_log WHERE application_id = ? AND company_id = ? AND company_name = ? AND field = 'status' "
                                            "AND new_value = ? AND logged_at = ? AND id < ?", (aid, cid, cname, status, at, move[0])).fetchone() is None:
            raise V.Refused("changed", "The earlier status could not be found in the edit log, so Undo was not done. Nothing was changed.")
        c.execute("UPDATE application_edits SET status = ?, status_at = ?, edited_at = ? WHERE application_id = ?", (status, at, when, aid))
        c.execute("INSERT INTO edit_log (application_id, company_id, company_name, field, old_value, new_value, logged_at) VALUES (?, ?, ?, 'status', ?, ?, ?)",
                  (aid, cid, cname, move[5], status, when))
    return {"application_id": aid}


V.ROUTES["/api/v16/undo_move"] = act_undo_move
V.PAGES.clear()
V.PAGES.update({"/": ("index_v16.html", "text/html; charset=utf-8"), "/index.html": ("index_v16.html", "text/html; charset=utf-8"),
                "/live_v16.js": ("live_v16.js", "application/javascript; charset=utf-8")})
main = W.main                               # v15's: --db (default the live database), --port (default 8001), the same checks before serving


if __name__ == "__main__":
    main()
