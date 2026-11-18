"""Tavily web-search second check for sender->company linking.

Safety first:
  * The API key is read from secrets/tavily_key.txt and stripped, only at the moment of a live call; it is never
    printed, logged, written to any file or database, kept on the client, put in an error note or put in a URL (it
    travels only in the POST JSON body).
  * Only a company name or a spelling/legal-suffix variant is ever sent - never
    email subjects, bodies, or personal data (the caller passes the query).
  * basic depth, <=5 results, a 1-2s delay between live searches.
  * Every query + its results are cached, so a query is never sent twice, including across runs.
  * Credits are counted per calendar month (key YYYY-MM) and hard-capped at MONTHLY_CAP. A 432 (plan limit) writes the
    cap into the month's row, so every later run that month makes no call. A 429, a timeout or any other failure stops
    searching for the rest of this run (this client object) and returns search_unavailable (never "not found", never a
    confirmation, never a credit).
  * SEARCH_ENABLED turns all searching off.

BUILD STAGE 2: the cache and the credit counter now live in tracker.db (made by db/migrate_stage6.py). Pass an OPEN
sqlite3 connection and the client uses its search_cache / search_credits tables (it never creates tables on a connection
it was given). A path still works for the scratch databases, as before. The client commits its cache and credit writes
as one small transaction of its own, so search() must not be called inside an open transaction of the caller.
This module never writes to tracker.db unless the caller hands it a connection to it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests

SEARCH_ENABLED = True          # master switch: False -> no request ever sent
TOP_N = 5                      # confirmation must appear within the first N results
MAX_RESULTS = 5               # Tavily results requested (basic returns <=5)
SEARCH_DEPTH = "basic"        # 1 credit per basic search
MONTHLY_CAP = 300             # hard cap on credits per calendar month
DELAY_SECONDS = 1.5           # pause between live searches (1-2s)
_ENDPOINT = "https://api.tavily.com/search"
_KEY_PATH = Path(__file__).resolve().parent.parent / "secrets" / "tavily_key.txt"

OK = "ok"
UNAVAILABLE = "search_unavailable"   # disabled / over-budget / 429 / 432 / timeout / error


@dataclass
class SearchOutcome:
    status: str                       # OK / UNAVAILABLE
    results: list = field(default_factory=list)   # [{url,title,content}]
    http_status: int | None = None
    credits_used: int = 0
    note: str = ""


class SearchClient:
    """Cached, budgeted Tavily client.

    ``db`` is an open sqlite3 connection (tracker.db) or a path (scratch databases). ``session`` is an injection seam
    for tests (simulated 432/429/timeout); it defaults to the real ``requests`` module.
    """

    def __init__(self, db, enabled: bool = SEARCH_ENABLED, session=None, replay: bool = False):
        if isinstance(db, sqlite3.Connection):
            self.db, self._owns = db, False       # the tables already exist (migration); never create them here
        else:
            self.db, self._owns = sqlite3.connect(db), True
            self._ensure_tables()
        self.enabled = enabled
        self._session = session or requests
        self._stopped = False
        self._stop_note = ""
        self.replay = replay        # replay: use cache only, never send a request
        self.live_calls = 0         # count of actual network requests sent

    def _ensure_tables(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS search_cache ("
            "query TEXT PRIMARY KEY, results_json TEXT, http_status INTEGER, created_at TEXT)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS search_credits (month TEXT PRIMARY KEY, credits INTEGER)")
        self.db.commit()

    # --- key (never stored, never exposed) ---
    @staticmethod
    def _key_value() -> str:
        return _KEY_PATH.read_text().strip()

    # --- small transactions of the client's own ---
    def _write(self, *statements) -> None:
        """Run (sql, params) statements as ONE transaction and commit it at once."""
        if not self._owns and self.db.in_transaction:
            raise RuntimeError("SearchClient writes in a transaction of its own: do not call search() inside one")
        if self.db.isolation_level is None:          # autocommit connection: open the transaction explicitly
            self.db.execute("BEGIN IMMEDIATE")
        try:
            for sql, params in statements:
                self.db.execute(sql, params)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _stop(self, note: str) -> None:
        self._stopped, self._stop_note = True, note

    # --- credit accounting ---
    @staticmethod
    def _month() -> str:
        return date.today().strftime("%Y-%m")

    def credits_used(self) -> int:
        row = self.db.execute(
            "SELECT credits FROM search_credits WHERE month=?", (self._month(),)).fetchone()
        return row[0] if row else 0

    def _credit_sql(self, n: int = 1):
        return ("INSERT INTO search_credits(month,credits) VALUES(?,?) "
                "ON CONFLICT(month) DO UPDATE SET credits=credits+?", (self._month(), n, n))

    def _add_credit(self, n: int = 1) -> None:
        self._write(self._credit_sql(n))

    def _write_cap(self) -> None:
        """432: this month's row becomes the cap, so no run makes another call until the month changes."""
        self._write(("INSERT INTO search_credits(month,credits) VALUES(?,?) "
                     "ON CONFLICT(month) DO UPDATE SET credits=MAX(credits,?)", (self._month(), MONTHLY_CAP, MONTHLY_CAP)))

    def cache_hits_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]

    def search(self, query: str) -> SearchOutcome:
        """Return cached results if present; otherwise, if enabled and within
        budget, send ONE live basic search and cache it. Never sends twice."""
        row = self.db.execute(
            "SELECT results_json, http_status FROM search_cache WHERE query=?", (query,)).fetchone()
        if row is not None:
            return SearchOutcome(OK, json.loads(row[0]), http_status=row[1], credits_used=0, note="cache")

        if self.replay:
            # replay mode: a query not already in the cache is never sent.
            return SearchOutcome(UNAVAILABLE, note="not cached (replay)")
        if not self.enabled:
            return SearchOutcome(UNAVAILABLE, note="disabled")
        if self._stopped:
            return SearchOutcome(UNAVAILABLE, note=self._stop_note)
        if self.credits_used() >= MONTHLY_CAP:
            self._stop("budget cap reached")
            return SearchOutcome(UNAVAILABLE, note="budget cap reached")

        if not self._owns and self.db.in_transaction:  # check BEFORE spending a credit, not after
            raise RuntimeError("SearchClient writes in a transaction of its own: do not call search() inside one")
        self.live_calls += 1
        try:
            resp = self._session.post(
                _ENDPOINT,
                json={"api_key": self._key_value(), "query": query,
                      "search_depth": SEARCH_DEPTH, "max_results": MAX_RESULTS},
                timeout=20,
            )
        except Exception as exc:                       # timeout / connection error / unreadable key file
            self._stop(f"error:{type(exc).__name__}")  # the class name only: an exception text could echo the request
            return SearchOutcome(UNAVAILABLE, note=self._stop_note)

        code = getattr(resp, "status_code", None)
        if code == 432:                                # plan limit -> stop for the month, remembered in the database
            self._write_cap()
            self._stop("432 plan limit")
            return SearchOutcome(UNAVAILABLE, http_status=432, note="432 plan limit")
        if code == 429:                                # rate limited -> stop for this run
            self._stop("429 rate limited")
            return SearchOutcome(UNAVAILABLE, http_status=429, note="429 rate limited")
        if code != 200:
            self._stop(f"http {code}")
            return SearchOutcome(UNAVAILABLE, http_status=code, note=f"http {code}")

        try:
            data = resp.json()
            results = [{"url": r.get("url", ""), "title": r.get("title", ""),
                        "content": r.get("content", "")}
                       for r in data.get("results", [])][:MAX_RESULTS]
        except Exception as exc:                       # an answer we cannot read costs nothing and caches nothing
            self._stop(f"error:{type(exc).__name__}")
            return SearchOutcome(UNAVAILABLE, http_status=200, note=self._stop_note)
        self._write(self._credit_sql(1),               # basic search = 1 credit; credit and cache in one transaction
                    ("INSERT OR REPLACE INTO search_cache(query,results_json,http_status,created_at) VALUES(?,?,?,?)",
                     (query, json.dumps(results), 200, date.today().isoformat())))
        time.sleep(DELAY_SECONDS)
        return SearchOutcome(OK, results, http_status=200, credits_used=1, note="live")
