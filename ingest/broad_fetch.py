"""Broad Gmail fetch for the dry-run linker (read only; NOT used by run_sync).

Asks Gmail for everything after a start date, minus Gmail's Promotions/Social/
Forums categories and the user's own sent mail. Every message that survives the
category exclusion is handed to classify_email.

NO bulk prefilter: an earlier List-Unsubscribe/Precedence prefilter was removed
by decision on 2026-09-22 because real Workday and iCIMS application mail carries
List-Unsubscribe (it would have dropped 48 of 103 known application emails). The
Gmail category exclusion is the only filter; the local LLM then judges relevance
(status "unrelated" is skipped downstream).

The live sync still uses gmail_client.py's fixed sender list; this module is the
proposed broad alternative, exercised only in the dry run.
"""
from __future__ import annotations

# Gmail categories excluded at query level, plus the user's own sent mail.
QUERY_EXCLUSIONS = "-category:promotions -category:social -category:forums -from:me"


def broad_query(after_date: str) -> str:
    """Gmail search string for everything after ``after_date`` (YYYY/MM/DD)."""
    return f"after:{after_date} {QUERY_EXCLUSIONS}"


def domain_query(domains, after_date: str) -> str:
    """Gmail query for mail from any of ``domains`` after a date.

    Used only by the item-8 targeted check (acme.example, globex.example), the
    one place the replay is allowed to touch Gmail.
    """
    froms = " OR ".join(f"from:{d}" for d in domains)
    return f"after:{after_date} ({froms})"


# --- build stage 2: the live listing used by scheduler/resolver_run.py ---------------------------------------------
MAX_LISTED = 5000          # more ids than this after the cursor is an error, never a silent cut (the oldest would be lost)


def oldest_unhandled(service, after_epoch, handled, limit, domains=(), list_ids=None, get_email=None):
    """The ``limit`` oldest messages dated at or after ``after_epoch`` whose id is not in ``handled``, oldest first.

    ``handled`` is a set of Gmail ids already dealt with (events, review_queue, quiet_log, and the ids judged in the cursor's
    second). The query starts one second early, so mail that arrived in the cursor's own second is still seen.
    Gmail lists newest first, so the oldest ids are at the end of the list. Only those (and the few ``domains`` messages the
    broad query missed) are fetched in full, then sorted by their internal date, so the caller can move its cursor past
    exactly the messages it has handled. A list that reaches MAX_LISTED raises instead of dropping the oldest mail.
    ``list_ids`` and ``get_email`` default to ingest.gmail_client's; tests inject fakes. Read only."""
    if list_ids is None or get_email is None:
        from ingest.gmail_client import _get_email, _list_message_ids
        list_ids, get_email = list_ids or _list_message_ids, get_email or _get_email
    start = str(max(int(after_epoch) - 1, 0))
    broad = list_ids(service, broad_query(start), MAX_LISTED)                       # newest first
    if len(broad) >= MAX_LISTED:
        raise RuntimeError(f"{len(broad)} messages after the cursor: raise ingest.broad_fetch.MAX_LISTED")
    extra = list_ids(service, domain_query(domains, start), MAX_LISTED) if domains else []
    known = set(broad)
    extras = [i for i in extra if i not in known and i not in handled]               # old-path senders the broad query missed
    oldest = [i for i in reversed(broad) if i not in handled][:limit]
    mails = [get_email(service, i) for i in oldest + extras]
    return sorted(mails, key=lambda e: (e.internal_date, e.id))[:limit]
