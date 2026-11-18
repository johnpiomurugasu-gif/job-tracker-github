"""Sync orchestrator: Gmail -> classifier -> matcher -> database.

Fetches candidate emails newer than the last sync, classifies each with the
local LLM, matches the extracted company against the companies table, then:

  * AUTO_WRITE -> append a confirmed event (confirmed=1) to the matched
    EXISTING company's application, and move that application's current
    status/stage FORWARD only (a historical backfill never regresses a known
    current status - the event log still captures everything).
  * AUTO_ADD   -> a confidently, cleanly identified company that is not in the
    table yet AND verifies as a real company online (see ingest.verifier):
    create the company + application (both flagged confirmed_by_user=0) and
    append the event, so real new companies appear without gating on manual
    approval - just marked unconfirmed until reviewed. A name that does not
    verify (unfindable, or ambiguous across several companies) routes to
    review instead of being created on a guess.
  * REVIEW     -> genuine classifier uncertainty only (no company extracted,
    ambiguous match between two existing companies, low confidence, or a parse
    failure): record in review_queue for manual triage, write nothing else.
  * SKIP       -> ignore (classifier said the email is "unrelated").

The sync cursor (sync_state.last_synced_epoch) advances ONLY after the whole
batch is processed, so a mid-run failure (e.g. Ollama down) re-fetches rather
than skips mail. Re-processing is safe: emails already handled (by Gmail
message id, in events or review_queue) are skipped.

Run from the project root:
    python -m scheduler.run_sync              # real run
    python -m scheduler.run_sync --dry-run    # classify + match + print, write nothing
    python -m scheduler.run_sync --limit 5    # cap emails processed (handy for a test)
"""

from __future__ import annotations

import argparse
import importlib
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow direct invocation

from ingest.classifier import ClassifierUnavailable, classify_email
from ingest.gmail_client import (
    DB_PATH,
    ensure_sync_state_table,
    fetch_candidate_emails,
    get_last_sync,
    set_last_sync,
)
from ingest.matcher import (
    AUTO_ADD,
    AUTO_WRITE,
    REVIEW,
    SKIP,
    load_companies,
    match_and_decide,
)
from ingest.sender_check import CONFIRMED, MISMATCH, check_sender
from ingest.verifier import ERROR, VERIFIED, VerificationResult, verify_company
from scheduler.resolver_switch import get_mode
from scheduler.runlock import try_lock, write_heartbeat

DETAIL_SNIPPET_CHARS = 500
VERIFY_DELAY_SECONDS = 2.0   # short pause between live verification searches
# The new-company gate is the sender check (ingest.sender_check). The old web-
# search gate is retired behind this flag: while False (default), verify_company
# is never called and no search request is sent. Enabling it is a separate
# design decision - it is intentionally NOT combined with the sender check.
VERIFY_WEB_SEARCH = False


def ensure_review_queue_table(conn: sqlite3.Connection) -> None:
    """Create review_queue if missing (additive; never touches core tables)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS review_queue (
               id INTEGER PRIMARY KEY,
               source_email_id TEXT UNIQUE,
               received_date TEXT,
               sender TEXT,
               subject TEXT,
               extracted_company TEXT,
               status TEXT,
               stage TEXT,
               confidence REAL,
               match_company_id INTEGER,
               match_score REAL,
               reason TEXT,
               detail_text TEXT,
               created_at TEXT
           )"""
    )
    conn.commit()


def ensure_confirmed_columns(conn: sqlite3.Connection) -> None:
    """Add confirmed_by_user to companies/applications if missing (additive).

    Existing (hand-seeded) rows get DEFAULT 1 = confirmed; auto-added rows are
    inserted with 0. Never restructures or rewrites existing data.
    """
    for table in ("companies", "applications"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "confirmed_by_user" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN confirmed_by_user INTEGER DEFAULT 1"
            )
    conn.commit()


def run_sync(dry_run: bool = False, limit: int | None = None) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        ensure_sync_state_table(conn)
        ensure_review_queue_table(conn)
        ensure_confirmed_columns(conn)
        companies = load_companies(conn)
        after = get_last_sync(conn)

        if limit:
            emails = fetch_candidate_emails(after_epoch=after, max_messages=limit)
        else:
            emails = fetch_candidate_emails(after_epoch=after)
        emails.sort(key=lambda e: e.internal_date)  # oldest first: newest state wins

        stats = {"auto_write": 0, "auto_add": 0, "review": 0, "skip": 0, "duplicate": 0}
        max_internal = after or 0
        # Companies auto-added THIS run, so a repeat email for the same new
        # company matches it (AUTO_WRITE) instead of adding a duplicate. Maps
        # company id -> its one application id; dry runs use negative sentinel
        # ids (never written) so the dry breakdown mirrors a real run.
        auto_added: dict[int, int] = {}
        dry_seq = 0
        # Per-run verification state: dedupe searches, and latch to error after
        # the first failed search so an outage can't fan out into a burst.
        verify_state = {"cache": {}, "searches": 0, "errored": False}

        for e in emails:
            max_internal = max(max_internal, e.internal_date)

            if _already_handled(conn, e.id):
                stats["duplicate"] += 1
                print(f"  [dup]    {_who(e)}  already handled")
                continue

            classification = classify_email(e.subject, e.body, e.sender)
            decision = match_and_decide(classification, companies)

            if decision.outcome == SKIP:
                stats["skip"] += 1
                print(f"  [skip]   {_who(e)}  unrelated")
                continue

            if decision.outcome == REVIEW:
                if not dry_run:
                    _enqueue_review(conn, e, classification, decision, decision.reason)
                    conn.commit()
                stats["review"] += 1
                print(f"  [review] {_who(e)}  {str(classification.status):<18} "
                      f"-- {decision.reason}")
                continue

            if decision.outcome == AUTO_ADD:
                if VERIFY_WEB_SEARCH:
                    # Retired web-search gate, kept switched off behind the flag
                    # (adds companies unconfirmed). Not combined with the sender
                    # check; left intact so the search path can be revived.
                    verified = _verify_for_run(decision.company_name, verify_state)
                    print(_verify_log(decision.company_name, verified))
                    if verified.status == VERIFIED:
                        note, confirmed_flag, review_reason = verified.notes(), 0, None
                    elif verified.status == ERROR:
                        note, confirmed_flag, review_reason = "verification unavailable", 0, None
                    else:
                        note, confirmed_flag = None, 0
                        review_reason = f"unverified company — {verified.brief}"
                else:
                    # Sender check: does the employer named in the email sender
                    # agree with the company Llama extracted? Agreement -> add as
                    # confirmed; a disagreement, or a sender that names no
                    # employer, -> manual review.
                    check = check_sender(e.sender, decision.company_name)
                    print(f"  [check]  {_who(e)}  {_sender_log(decision.company_name, check)}")
                    if check.result == CONFIRMED:
                        note = f"auto confirmed by sender check: {check.matched} <{check.address}>"
                        confirmed_flag, review_reason = 1, None
                    elif check.result == MISMATCH:
                        note, confirmed_flag = None, 0
                        review_reason = (f"sender names '{check.primary}' but the "
                                         f"classifier extracted '{decision.company_name}'")
                    else:
                        note, confirmed_flag = None, 0
                        review_reason = "no employer name in sender"

                if review_reason is not None:
                    if not dry_run:
                        _enqueue_review(conn, e, classification, decision, review_reason)
                        conn.commit()
                    stats["review"] += 1
                    print(f"  [review] {_who(e)}  {classification.status:<18} -- {decision.company_name!r}: "   # v19: never the sender's own words
                          f"{'the sender names another employer' if review_reason.startswith('sender names') else review_reason}")
                    continue

                if dry_run:
                    dry_seq -= 1
                    new_company_id = new_app_id = dry_seq
                else:
                    new_company_id = _create_company(
                        conn, decision.company_name, notes=note, confirmed=confirmed_flag
                    )
                    new_app_id = _create_application(
                        conn, new_company_id, classification, e, confirmed=confirmed_flag
                    )
                    _append_event(conn, new_app_id, e, classification, confirmed=1)
                    conn.commit()
                companies.append((new_company_id, decision.company_name))
                auto_added[new_company_id] = new_app_id
                stats["auto_add"] += 1
                label = "confirmed by sender check" if confirmed_flag else note
                print(f"  [add]    {_who(e)}  {classification.status:<18} "
                      f"-> NEW '{decision.company_name}' ({label})")
                continue

            # AUTO_WRITE: matched an existing (or already auto-added) company.
            app_id = _resolve_application(conn, decision.company_id, auto_added)
            if app_id is None:
                reason = (f"{decision.reason}; matched company has no single "
                          f"application to attach to")
                if not dry_run:
                    _enqueue_review(conn, e, classification, decision, reason)
                    conn.commit()
                stats["review"] += 1
                print(f"  [review] {_who(e)}  {classification.status}  -- {reason}")
                continue
            if not dry_run:
                first_signal = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE application_id = ?", (app_id,)
                ).fetchone()[0] == 0
                _append_event(conn, app_id, e, classification, confirmed=1)
                _advance_application(conn, app_id, e, classification, first_signal)
                conn.commit()
            stats["auto_write"] += 1
            print(f"  [write]  {_who(e)}  {classification.status:<18} "
                  f"-> {decision.company_name} (app {app_id})")

        if emails and not dry_run:
            set_last_sync(conn, max_internal)

        _print_summary(stats, len(emails), dry_run, max_internal)

    except ClassifierUnavailable as exc:
        # Systemic failure: leave the cursor where it was so the next run
        # re-fetches. Emails already written this run are committed per-email
        # and will be skipped as duplicates next time.
        print(f"\nABORTED: {exc}")
        print("Sync cursor NOT advanced. Fix the backend and re-run; already-"
              "processed emails will be skipped.")
        raise SystemExit(1)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# DB helpers                                                                   #
# --------------------------------------------------------------------------- #

def _verify_for_run(name: str | None, state: dict) -> VerificationResult:
    """Verify a company at most once per sync run, and stop after an outage.

    Cuts request volume so we don't trip the search endpoint's throttle:
      * cache by name, so a repeated company is searched only once;
      * a short delay between distinct live searches;
      * after the FIRST error this run, skip every remaining search and treat
        it as error too - one refusal means the endpoint is down for now, so
        hammering it just deepens the throttle.
    """
    key = (name or "").strip().lower()
    if key in state["cache"]:
        return state["cache"][key]
    if state["errored"]:
        result = VerificationResult(ERROR, error="search skipped after an earlier error this run")
    else:
        if state["searches"]:
            time.sleep(VERIFY_DELAY_SECONDS)
        result = verify_company(name)
        state["searches"] += 1
        if result.status == ERROR:
            state["errored"] = True
    state["cache"][key] = result
    return result


def _verify_log(name: str | None, result: VerificationResult) -> str:
    """One log line per verification, e.g. verify 'Acme' -> verified (acme.example)."""
    if result.evidence:
        detail = f" ({', '.join(result.evidence)})"
    elif result.error:
        detail = f" ({result.error})"
    else:
        detail = ""
    return f"verify {name!r} -> {result.status}{detail}"


def _sender_log(name: str | None, check) -> str:
    """One log line per new-company decision, e.g. sender check 'Acme' -> confirmed.
    v19: the company and the result only, never the sender's address or name."""
    return f"sender check {name!r} -> {check.result}"


def _already_handled(conn: sqlite3.Connection, email_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE source_email_id = ? "
        "UNION SELECT 1 FROM review_queue WHERE source_email_id = ? LIMIT 1",
        (email_id, email_id),
    ).fetchone()
    return row is not None


def _resolve_application(
    conn: sqlite3.Connection, company_id: int | None, auto_added: dict[int, int]
) -> int | None:
    """Return the application id for a company, but only if it has exactly one.

    Zero or multiple applications is ambiguous (which role is the email about?),
    so the caller routes those to review rather than attaching to a guess. A
    company auto-added earlier this run resolves via ``auto_added`` to its one
    fresh application - which also lets a dry run resolve its sentinel ids.
    """
    if company_id is None:
        return None
    rows = conn.execute(
        "SELECT id FROM applications WHERE company_id = ?", (company_id,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows and company_id in auto_added:
        return auto_added[company_id]
    return None


def _append_event(conn, app_id, email, classification, confirmed) -> None:
    conn.execute(
        "INSERT INTO events (application_id, event_type, event_date, "
        "source_email_id, detail_text, confidence, confirmed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (app_id, classification.status, _iso(email.internal_date), email.id,
         _detail(email), classification.confidence, confirmed),
    )


def _advance_application(conn, app_id, email, classification, first_signal=False) -> None:
    # Forward-only: update the current snapshot only when this email is at least
    # as new as what's recorded, so backfilling old mail can't regress it.
    #
    # Seed-shield override (giving the "protection" an expiry): a hand-seeded row
    # carries the seed date as last_updated, which would otherwise permanently
    # block any real email dated before the seed from ever updating it. When this
    # is the application's FIRST real email event, the seed placeholder yields to
    # it regardless of date, so a seed company's first genuine signal always
    # lands. Applies to all 10 seeds uniformly; only affects new mail going
    # forward — it never re-processes history.
    stamp = _iso(email.internal_date)
    if first_signal:
        conn.execute(
            "UPDATE applications SET current_status = ?, "
            "current_stage = COALESCE(?, current_stage), last_updated = ? WHERE id = ?",
            (classification.status, classification.stage, stamp, app_id),
        )
    else:
        conn.execute(
            "UPDATE applications SET current_status = ?, "
            "current_stage = COALESCE(?, current_stage), last_updated = ? "
            "WHERE id = ? AND (last_updated IS NULL OR last_updated <= ?)",
            (classification.status, classification.stage, stamp, app_id, stamp),
        )


def _create_company(conn, name, notes=None, confirmed=0) -> int:
    """Insert an auto-added company.

    ``confirmed`` sets confirmed_by_user: 0 = unconfirmed (pending review), or 1
    when the sender check confirms it (the same value the dashboard Confirm
    action sets). ``notes`` records why it was auto-added.
    """
    cur = conn.execute(
        "INSERT INTO companies (name, industry, notes, confirmed_by_user) "
        "VALUES (?, NULL, ?, ?)",
        (name, notes, confirmed),
    )
    return cur.lastrowid


def _create_application(conn, company_id, classification, email, confirmed=0) -> int:
    """Insert an auto-added application. ``confirmed`` -> confirmed_by_user."""
    cur = conn.execute(
        "INSERT INTO applications (company_id, role_title, date_applied, source, "
        "current_status, current_stage, last_updated, confirmed_by_user) "
        "VALUES (?, NULL, NULL, NULL, ?, ?, ?, ?)",
        (company_id, classification.status, classification.stage,
         _iso(email.internal_date), confirmed),
    )
    return cur.lastrowid


def _enqueue_review(conn, email, classification, decision, reason) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO review_queue "
        "(source_email_id, received_date, sender, subject, extracted_company, "
        " status, stage, confidence, match_company_id, match_score, reason, "
        " detail_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (email.id, _iso(email.internal_date), email.sender, email.subject,
         classification.company, classification.status, classification.stage,
         classification.confidence, decision.company_id, decision.match_score,
         reason, _detail(email), date.today().isoformat()),
    )


# --------------------------------------------------------------------------- #
# Formatting                                                                   #
# --------------------------------------------------------------------------- #

def _iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).date().isoformat()


def _detail(email) -> str:
    body = " ".join(email.body.split())
    return f"{email.subject}\n\n{body[:DETAIL_SNIPPET_CHARS]}"


def _who(email) -> str:
    return f"{email.id:<16}"          # v19: the message id, never the sender


def _print_summary(stats, total, dry_run, max_internal) -> None:
    print("\n" + "=" * 64)
    print(f"{'DRY RUN (nothing written)' if dry_run else 'SYNC COMPLETE'}: "
          f"{total} email(s) processed")
    print(f"  auto-written (existing company): {stats['auto_write']}")
    print(f"  auto-added (new, unconfirmed):   {stats['auto_add']}")
    print(f"  routed to review_queue:          {stats['review']}")
    print(f"  skipped (unrelated):             {stats['skip']}")
    print(f"  duplicates (already handled):    {stats['duplicate']}")
    if dry_run:
        print("  sync cursor:                     unchanged (dry run)")
    elif total:
        print(f"  sync cursor:                     advanced to {_iso(max_internal)} "
              f"({max_internal})")
    else:
        print("  sync cursor:                     unchanged (no new mail)")
    print("=" * 64)


if __name__ == "__main__":
    lock = try_lock()  # one run at a time; held until this process ends
    if lock is None:
        print("SKIPPED: previous run still going")
        raise SystemExit(0)
    parser = argparse.ArgumentParser(
        description="Sync Gmail -> classifier -> matcher -> database."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="classify and match and print, but write nothing and don't advance the cursor",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="process at most N emails (handy for a quick test run)",
    )
    args = parser.parse_args()
    if get_mode(DB_PATH) == "on":  # the new filing path; anything but 'on' is today's path
        try:
            resolver_run = importlib.import_module("scheduler.resolver_run")
        except ModuleNotFoundError as exc:
            if exc.name != "scheduler.resolver_run":
                raise
            print("WARNING: resolver_mode is on but scheduler/resolver_run.py is not installed; running today's path")
            run_sync(dry_run=args.dry_run, limit=args.limit)
        else:
            # run() returns True for a good run, False for ABORTED or a handled failure, and raises on anything unexpected
            if not resolver_run.run(dry_run=args.dry_run, limit=args.limit):
                raise SystemExit(1)
    else:
        run_sync(dry_run=args.dry_run, limit=args.limit)
    if not args.dry_run:
        write_heartbeat()  # reached only by a good run: ABORTED and tracebacks exit before this line
