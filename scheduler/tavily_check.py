"""Dashboard v18: check the search key the sync's company check already uses, with ONE test search. Run it in Terminal:

    venv/bin/python -m scheduler.tavily_check

No paste step and nothing saved: it reads the sync's own key file (scheduler/enrich.py KEY_FILE, read only), makes one test search for a fixed
public company name (TEST_NAME, nothing about John) through enrich's own web_search, and prints OK with the credits used, or the problem in plain
words. The key is never printed or logged. The test search is counted in search_log when the database has that table. It ignores the
lookups switch (logs/lookups_on): it is one search asked for by hand, not the job.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scheduler import enrich as E  # noqa: E402

TEST_NAME = "Rolls-Royce"
PLAIN = {401: "The key was rejected. That would also stop the sync's company check. Nothing was changed.",
         429: "The search service says too many searches right now (rate limited). Wait a minute and run this again.",
         432: "This month's free credits are used up, for the sync's company check too. Searches start again on the 1st of next month.",
         433: "The account's spending limit is reached. Searches start again when it resets.",
         None: "There was no answer from the search service. Check the internet connection and run this again."}


def count(db, status):
    """Record the test search in search_log if this database has it (the live one has it only after db/migrate_v18.py; before that it is only read)."""
    try:
        with E.ro(db) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_log'").fetchone() is None:
                return False
        with E.write(db) as c:
            c.execute("INSERT INTO search_log (searched_at, company_key, http_status) VALUES (?, ?, ?)", (E.now(), E.company_key(TEST_NAME), status))
        return True
    except sqlite3.Error:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the search key with one test search (nothing is saved)")
    ap.add_argument("--db", default=str(E.DB))
    db = ap.parse_args(argv).db
    key = E.read_key()
    if key is None:
        print("There is no search key file, or it is empty, so no search was made.")
        return 1
    status, _, credits = E.web_search(TEST_NAME, key)
    over = status == 200 and credits != 1
    counted = count(db, E.OVER_CREDIT if over else status)
    if status == 200 and not over:
        print("OK: the test search worked (1 credit used)." + ("" if counted else " It is not in the lookups' monthly count (that starts after the switch to v18)."))
        return 0
    if over:
        print(f"The test search answered, but reported {credits if credits is not None else 'no'} credits used instead of 1, so the lookups would stop for the day.")
        return 1
    print(PLAIN.get(status, f"The search service answered with an error ({status}). Run this again later."))
    return 1


if __name__ == "__main__":
    sys.exit(main())
