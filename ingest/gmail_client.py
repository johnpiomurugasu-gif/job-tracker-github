"""Fetch candidate job-application emails from Gmail (read-only).

This module ONLY fetches. It handles the OAuth handshake, then pulls messages
that pass a cheap prefilter - the sender is a known ATS/company domain AND the
message is newer than the last successful sync - and returns them as plain
(sender, subject, body) records ready to hand straight to
``ingest.classifier.classify_email``. Classification and company matching
happen downstream; chaining the whole pipeline together is run_sync's job (not
built yet). Nothing here classifies, matches, or writes application data.

One-time setup (done by you in a browser):
  1. Create a Google Cloud project and enable the Gmail API.
  2. Configure the OAuth consent screen and add yourself as a test user.
  3. Create an OAuth client ID of type "Desktop app".
  4. Download its JSON and save it as ``secrets/credentials.json`` in the
     project (or point GMAIL_CREDENTIALS_PATH at it).
The first run opens a browser for consent and writes ``token.json``; later runs
reuse it, refreshing silently when it expires. Delete token.json to re-consent.
Both files are secrets - never commit them.
"""

from __future__ import annotations

import base64
import html
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --- Paths & config (env-overridable; can move to config.py later) ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = Path(
    os.environ.get("GMAIL_CREDENTIALS_PATH", PROJECT_ROOT / "secrets" / "credentials.json")
)
TOKEN_PATH = Path(os.environ.get("GMAIL_TOKEN_PATH", PROJECT_ROOT / "secrets" / "token.json"))
DB_PATH = Path(os.environ.get("GMAIL_DB_PATH", PROJECT_ROOT / "db" / "tracker.db"))

# Read-only: we never modify, send, or delete mail. Least privilege.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ATS platforms from the build brief. Company-specific domains can be added here
# as they become known (e.g. "acme.example"); fetch() also takes extras.
ATS_SENDER_DOMAINS = (
    "greenhouse.io",
    "lever.co",
    "myworkday.com",
    "icims.com",
    "smartrecruiters.com",
    "ashbyhq.com",
)
EXTRA_SENDER_DOMAINS: tuple[str, ...] = ()  # known company domains, once known

# First run has no stored cursor; bound the initial fetch to this many days so
# it doesn't scan the whole mailbox.
INITIAL_LOOKBACK_DAYS = int(os.environ.get("GMAIL_INITIAL_LOOKBACK_DAYS", "180"))
# Safety cap so a mis-scoped query can't pull the entire mailbox in one run.
MAX_MESSAGES = int(os.environ.get("GMAIL_MAX_MESSAGES", "500"))

_LAST_SYNC_KEY = "last_synced_epoch"


@dataclass
class FetchedEmail:
    """A plain email record. Fields map onto classify_email(subject, body, sender)."""
    id: str
    sender: str
    subject: str
    body: str
    internal_date: int  # epoch seconds (Gmail internalDate) - for advancing sync_state


# --------------------------------------------------------------------------- #
# sync_state helpers - the last-synced cursor lives in the DB                 #
# --------------------------------------------------------------------------- #

def ensure_sync_state_table(conn: sqlite3.Connection) -> None:
    """Create sync_state if it's missing.

    Safe on the already-seeded database: it only adds a new table and never
    touches companies/applications/events/contacts or their rows.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()


def get_last_sync(conn: sqlite3.Connection) -> int | None:
    """Return the last-synced time (epoch seconds), or None if never synced."""
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (_LAST_SYNC_KEY,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def set_last_sync(conn: sqlite3.Connection, epoch_seconds: int) -> None:
    """Store the sync cursor.

    run_sync should call this only AFTER successfully processing the fetched
    mail, so a mid-run failure re-fetches rather than skipping messages. It is
    provided here (with the fetch that reads it) but is not called by fetch.
    """
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_LAST_SYNC_KEY, str(int(epoch_seconds))),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# OAuth                                                                        #
# --------------------------------------------------------------------------- #

def get_credentials() -> Credentials:
    """Load stored credentials, refreshing or running the consent flow as needed.

    First run opens a browser for consent (Desktop-app OAuth client) and writes
    token.json; later runs reuse it. Raises FileNotFoundError with guidance if
    credentials.json is missing.
    """
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not CREDENTIALS_PATH.exists():
            raise FileNotFoundError(
                f"Gmail OAuth client secrets not found at {CREDENTIALS_PATH}. "
                "Create a Desktop-app OAuth client in Google Cloud, download its "
                "JSON, and save it there (or set GMAIL_CREDENTIALS_PATH). See this "
                "module's docstring for the one-time setup steps."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)

    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # v19: readable by you only, as gmail_renew.py writes it
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(TOKEN_PATH, 0o600)                                             # also when the file was already there with wider rights
    return creds


def build_service(creds: Credentials | None = None):
    """Build the Gmail API client, obtaining credentials if none are passed."""
    return build("gmail", "v1", credentials=creds or get_credentials())


# --------------------------------------------------------------------------- #
# Fetch                                                                        #
# --------------------------------------------------------------------------- #

def build_query(after_epoch: int | None, extra_domains: tuple[str, ...] = ()) -> str:
    """Gmail search query: from any known sender domain, newer than after_epoch."""
    domains = list(ATS_SENDER_DOMAINS) + list(EXTRA_SENDER_DOMAINS) + list(extra_domains)
    from_clause = " OR ".join(f"from:{d}" for d in domains)
    query = f"({from_clause})"
    if after_epoch is not None:
        query += f" after:{int(after_epoch)}"
    return query


def fetch_candidate_emails(
    after_epoch: int | None = None,
    extra_domains: tuple[str, ...] = (),
    service=None,
    max_messages: int = MAX_MESSAGES,
) -> list[FetchedEmail]:
    """Fetch emails passing the sender-domain + after-timestamp prefilter.

    after_epoch: only mail strictly newer than this (epoch seconds). If None,
    defaults to INITIAL_LOOKBACK_DAYS ago so a first run does not scan the whole
    mailbox. Returns plain FetchedEmail records; does not classify or write.
    """
    if service is None:
        service = build_service()
    if after_epoch is None:
        after_epoch = int(time.time()) - INITIAL_LOOKBACK_DAYS * 86400

    query = build_query(after_epoch, extra_domains)
    message_ids = _list_message_ids(service, query, max_messages)
    return [_get_email(service, mid) for mid in message_ids]


def _list_message_ids(service, query: str, max_messages: int) -> list[str]:
    ids: list[str] = []
    page_token = None
    while len(ids) < max_messages:
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                pageToken=page_token,
                maxResults=min(500, max_messages - len(ids)),
            )
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_messages]


def _get_email(service, message_id: str) -> FetchedEmail:
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    body = _extract_body(payload) or msg.get("snippet", "")
    return FetchedEmail(
        id=message_id,
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        body=body,
        internal_date=int(msg.get("internalDate", "0")) // 1000,  # ms -> s
    )


def _extract_body(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped text/html. Walks MIME parts."""
    plain = _find_part(payload, "text/plain")
    if plain:
        return plain.strip()
    html_body = _find_part(payload, "text/html")
    if html_body:
        return _strip_html(html_body)
    return ""


def _find_part(part: dict, mime_type: str) -> str:
    if part.get("mimeType") == mime_type:
        data = part.get("body", {}).get("data")
        if data:
            return _decode(data)
    for sub in part.get("parts", []) or []:
        found = _find_part(sub, mime_type)
        if found:
            return found
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*", "\n\n", text).strip()


if __name__ == "__main__":
    # Manual smoke test. Runs the OAuth flow (browser on first run) and prints
    # how many emails it would hand to classify_email() (v19: counts only, never
    # a sender, subject, body or the query). Requires a real credentials.json;
    # does NOT classify or write to the database.
    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_sync_state_table(conn)
        after = get_last_sync(conn)
    finally:
        conn.close()

    effective_after = after if after is not None else int(time.time()) - INITIAL_LOOKBACK_DAYS * 86400
    emails = fetch_candidate_emails(after_epoch=effective_after)
    print(f"Fetched {len(emails)} candidate email(s); {sum(1 for e in emails if e.body)} with a body.")
