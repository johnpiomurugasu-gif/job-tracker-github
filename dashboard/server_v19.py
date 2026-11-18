"""Local server for the job-tracker dashboard v19: v18 plus three guards. Port 8001 by default.

It IMPORTS dashboard/server_v18.py (not changed, not copied), which imports v17, v16, v15 and v14, so the data and every button run their own
code. v19 adds:
  1 on every answer (pages, data and error pages alike): X-Frame-Options DENY, Content-Security-Policy frame-ancestors 'none' and
    X-Content-Type-Options nosniff, so no other site can show the dashboard inside a frame and no file is read as another type.
  2 every POST must carry the header X-Job-Tracker: 1 (the page's own script sends it) or an Origin of this server, else 403 and nothing
    is read or changed. A web page on another site cannot add that header without the browser asking this server first, and this server
    never says yes. v14's own checks (Host, Origin, JSON only, size limit) still run after it.
  3 the page: index_v19.html and live_v19.js (copies of v18's: the escape helper also escapes the single quote, and post() sends the header).
/api/data is v18's exactly, and the buttons write exactly as v18's do. This server makes no network call.
Run:  venv/bin/python dashboard/server_v19.py [--db db/tracker.db] [--port 8001] [--heartbeat logs/last_ok] [--confirm-log logs/confirm_all.log]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import server_v18 as Z  # noqa: E402

Y, X, W, V = Z.Y, Z.X, Z.W, Z.V
VERSION = Z.VERSION = Y.VERSION = X.VERSION = W.VERSION = V.VERSION = "v19"     # meta() of v14 and main() of v15 read it

HEADER = "X-Job-Tracker"
SECURITY_HEADERS = (("X-Frame-Options", "DENY"), ("Content-Security-Policy", "frame-ancestors 'none'"), ("X-Content-Type-Options", "nosniff"))
_END, _POST = V.Handler.end_headers, V.Handler.do_POST


def end_headers(self):
    """Every answer passes here (BaseHTTPRequestHandler's own error pages too), so every answer carries the three headers."""
    for k, v in SECURITY_HEADERS:
        self.send_header(k, v)
    _END(self)


def do_POST(self):
    port = self.server.server_address[1]
    own = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    if self.headers.get(HEADER) != "1" and (self.headers.get("Origin") or "").lower() not in own:
        return self._json(403, {"ok": False, "error": "This request did not come from the dashboard page. Nothing was changed."})
    return _POST(self)


V.Handler.end_headers, V.Handler.do_POST = end_headers, do_POST
JS_TYPE = V.PAGES["/live_v18.js"][1]          # v18's type for the script, kept
V.PAGES.clear()
V.PAGES.update({"/": ("index_v19.html", "text/html; charset=utf-8"), "/index.html": ("index_v19.html", "text/html; charset=utf-8"),
                "/live_v19.js": ("live_v19.js", JS_TYPE)})
main = Z.main                               # v15's: --db (default the live database), --port (default 8001), the same checks before serving


if __name__ == "__main__":
    main()
