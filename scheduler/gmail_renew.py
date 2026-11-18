"""The weekly Gmail renewal: one command, a few minutes of clicking in the browser, and the sync job is never stopped.

    cd ~/job-tracker && venv/bin/python -m scheduler.gmail_renew             renew the Gmail sign in
    cd ~/job-tracker && venv/bin/python -m scheduler.gmail_renew --status    when it was renewed and when it runs out (no network, no Google call)

It runs the same Google sign in as ingest/gmail_client.py (same secrets/credentials.json, same read only scope) but forces the consent screen and asks for offline access, so Google hands out a NEW
refresh token and the 7 day clock really restarts. The result goes to a temporary file next to token.json; the live token.json is not touched until the new sign in has passed every check:
it has a refresh token, its scope is exactly the read only Gmail scope, a read only profile call works, it is a different sign in from the old one, and it is the same Google account the tracker has always used.
Then, holding the sync job's own run lock (logs/run.lock) for a moment so that a sync run in flight cannot write the old sign in back over the new one, the old token.json is copied to secrets/token.json.previous
and the new file replaces token.json in one atomic step (token.json is never missing). Finally secrets/gmail_renewal.json (the record the reminder reads) and one line of logs/gmail_renew.log are written.
"""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build

from scheduler.gmail_reminder import COMMAND, END_HOUR, GAP_HOURS, START_HOUR, reminders_start, when

ROOT = Path(__file__).resolve().parent.parent
LIFETIME_DAYS = 7                     # Google lets a sign in of this app lapse about 7 days after the click on Allow
BROWSER_WAIT_SECONDS = 600            # how long to wait for the browser step
SYNC_WAIT_SECONDS = 120               # how long to wait for a sync run in flight before the swap
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]       # the same as ingest/gmail_client.py (a test compares them)


def paths(root=ROOT):
    s, g = Path(root) / "secrets", Path(root) / "logs"
    return SimpleNamespace(credentials=s / "credentials.json", token=s / "token.json", previous=s / "token.json.previous", new=s / "token.json.renewing", record=s / "gmail_renewal.json",
                           lock=s / "gmail_renew.lock", run_lock=g / "run.lock", log=g / "gmail_renew.log")


def fingerprint(refresh_token):
    return hashlib.sha256(refresh_token.encode()).hexdigest()[:8]


def mask(address):
    return (address[:3] + "..." + address[address.index("@"):]) if address and "@" in address else "unknown"


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def write_atomic(path, text):
    tmp = Path(str(path) + ".tmp")
    write_private(tmp, text)
    os.replace(tmp, path)


def read_record(p):
    try:
        rec = json.loads(p.record.read_text())
        return rec if isinstance(rec, dict) and "renewed_at_utc" in rec else None
    except (OSError, ValueError):
        return None


def write_record(p, account, refresh_token, source, now=None):
    """The only writer of secrets/gmail_renewal.json."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out = now + timedelta(days=LIFETIME_DAYS)
    rec = {"renewed_at_utc": now.isoformat(timespec="seconds"), "renewed_at_local": now.astimezone().isoformat(timespec="seconds"),
           "runs_out_estimate_utc": out.isoformat(timespec="seconds"), "runs_out_estimate_local": out.astimezone().isoformat(timespec="seconds"),
           "account": account, "fingerprint": fingerprint(refresh_token) if refresh_token else None, "source": source}
    write_atomic(p.record, json.dumps(rec, indent=1))
    return rec


def log(p, text):
    with open(p.log, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def lock_now(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def lock_wait(path, seconds):
    end = time.monotonic() + seconds
    while True:
        f = lock_now(path)
        if f or time.monotonic() >= end:
            return f
        time.sleep(0.25)


def run_signin(p):
    """The browser step. Returns the new sign in as JSON text. Raises on a cancel, a timeout or a refusal."""
    flow = InstalledAppFlow.from_client_secrets_file(str(p.credentials), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline", open_browser=True, timeout_seconds=BROWSER_WAIT_SECONDS,
                                  authorization_prompt_message="If the browser did not open, copy this link into it: {url}")
    return creds.to_json()


def fetch_profile(info):
    """The Gmail address of a sign in (users.getProfile, read only). Works on the dict in memory: nothing is written."""
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()["emailAddress"]


def check_new(p, info):
    """(address, None) if the new sign in passes every check, else (None, the reason in plain words)."""
    if not info.get("refresh_token"):
        return None, "Google did not hand out a refresh token, so this sign in would not last."
    if set(info.get("scopes") or []) != set(SCOPES):
        return None, "The new sign in asks for more than read only access to Gmail. The tracker must only ever read."
    try:
        old = json.loads(p.token.read_text()).get("refresh_token")
    except (OSError, ValueError):
        old = None
    if old and old == info["refresh_token"]:
        return None, "Google handed back the same sign in as before, so the 7 day clock did not restart."
    try:
        return fetch_profile(info), None
    except Exception as exc:
        return None, f"The test read of your Gmail did not work ({type(exc).__name__})."


def known_account(p):
    """(address, where it came from) for the Google account the tracker uses now: the record, else the live sign in if it still works, else (None, None)."""
    rec = read_record(p)
    if rec and rec.get("account"):
        return rec["account"], "the record of the last renewal"
    try:
        return fetch_profile(json.loads(p.token.read_text())), "the sign in the tracker is using now"
    except Exception:
        return None, None


def swap(p, wait=None):
    """Keep the old token.json as token.json.previous and put the new file in its place, holding the run lock so no run in flight can write the old sign in back. False if the lock never came free."""
    run_lock = lock_wait(p.run_lock, SYNC_WAIT_SECONDS if wait is None else wait)
    if run_lock is None:
        return False
    try:
        if p.token.exists():
            keep = Path(str(p.previous) + ".tmp")
            shutil.copyfile(p.token, keep)
            os.chmod(keep, 0o600)
            os.replace(keep, p.previous)
        os.replace(p.new, p.token)
    finally:
        run_lock.close()
    return True


def renew(root=ROOT, say=print, ask=input):
    p = paths(root)
    lock = lock_now(p.lock)
    if lock is None:
        say("Another renewal is already running. Wait for it to finish (or close its Terminal window) and try again. Nothing was changed.")
        return 1

    def fail(why, code):
        say(why)
        say("Nothing was changed: the tracker still uses the sign in it had before.")
        log(p, f"failed: {code}")
        return 1

    try:
        p.new.unlink(missing_ok=True)                              # only this command's own leftover from a run that was cut off
        rec = read_record(p)
        if rec:
            say(f"Your Gmail sign in was renewed on {when(datetime.fromisoformat(rec['renewed_at_local']))} and runs out about {when(datetime.fromisoformat(rec['runs_out_estimate_local']))}.")
        else:
            say("There is no record yet of when the Gmail sign in was renewed.")
        say("A browser window will open. Choose the SAME Google account as last time (you may be signed in to more than one).")
        say("If Google says it has not verified the app: click Advanced, then the 'Go to ... (unsafe)' link, then Continue or Allow.")
        say(f"I will wait up to {BROWSER_WAIT_SECONDS // 60} minutes for you to finish in the browser. To cancel, press Control and C together.")
        try:
            text = run_signin(p)
        except WSGITimeoutError:
            return fail(f"No answer came back from the browser within {BROWSER_WAIT_SECONDS // 60} minutes.", "browser timed out")
        except KeyboardInterrupt:
            return fail("Stopped by you.", "cancelled")
        except Exception as exc:
            if "access_denied" in str(exc).lower() or "access blocked" in str(exc).lower():
                return fail("Google refused access (access_denied). Do not try anything else: tell the planner what Google showed on the screen.", "access_denied")
            return fail(f"The browser step did not finish ({type(exc).__name__}).", "browser step failed")
        write_private(p.new, text)                                 # a temporary file next to token.json; token.json itself is not touched
        info = json.loads(text)
        address, why = check_new(p, info)
        if why:
            return fail(why, "new sign in refused")
        known, source = known_account(p)
        if known is None:
            say(f"I cannot tell which Google account the tracker has used until now. The new sign in is for {address}.")
            try:
                answer = ask("Is that the account you always use for the job tracker? Type yes to go on: ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "yes":
                return fail("You did not type yes.", "account not confirmed")
        elif known.strip().lower() != address.strip().lower():
            return fail(f"You signed in with {address}, but the tracker uses a different Google account ({mask(known)}, from {source}). Run the command again and choose the same account as last time.", "different account")
        if not swap(p):
            return fail(f"A sync run has been going for more than {SYNC_WAIT_SECONDS // 60} minutes, so I could not swap safely.", "run lock busy")
        if fingerprint(json.loads(p.token.read_text())["refresh_token"]) != fingerprint(info["refresh_token"]):
            say("The new sign in was put in place but token.json does not hold it now. Run the command again.")
            log(p, "failed: token.json changed after the swap")
            return 1
        r = write_record(p, address, info["refresh_token"], "renewal")
        log(p, f"renewed: account {mask(address)}, fingerprint {r['fingerprint']}, runs out about {r['runs_out_estimate_local']}")
        out = datetime.fromisoformat(r["runs_out_estimate_local"])
        say("")
        say(f"Renewed. The tracker is signed in as {address}.")
        say(f"It runs out about {when(out)}.")
        say(f"Reminders start {when(reminders_start(out))}.")
        say("The sync job was not stopped. It will use the new sign in on its next run, within about a minute.")
        say("The old sign in is kept as secrets/token.json.previous.")
        say("To see these dates again, type: cd ~/job-tracker && venv/bin/python -m scheduler.gmail_renew --status")
        return 0
    finally:
        p.new.unlink(missing_ok=True)                              # nothing left behind (after a swap the file has already been moved)
        lock.close()


def span(td):
    days, hours = td.days, td.seconds // 3600
    return (f"{days} day{'s' if days != 1 else ''} and " if days else "") + f"{hours} hour{'s' if hours != 1 else ''}"


def status(root=ROOT, say=print, now=None):
    p = paths(root)
    rec = read_record(p)
    if rec is None:
        say("There is no record yet. The first renewal creates it. To renew, type: " + COMMAND)
        return 0
    now = now or datetime.now().astimezone()
    out = datetime.fromisoformat(rec["runs_out_estimate_local"])
    left = out - now
    say(f"Gmail sign in renewed: {when(datetime.fromisoformat(rec['renewed_at_local']))}" + (" (estimated: the date of the last renewal done by hand)" if rec.get("source") == "seeded" else "") + ".")
    say(f"It runs out about: {when(out)} " + (f"(in {span(left)})." if left > timedelta(0) else f"(that was {span(-left)} ago; Google has not cut it off yet, but renew now)."))
    start = reminders_start(out)
    say(f"Reminders {'start' if start > now else 'started'}: {when(start)}, between {START_HOUR}:00 and {END_HOUR}:00, every {GAP_HOURS} hours, until you renew.")
    say("Account: " + (rec["account"] if rec.get("account") else "not recorded yet (the first real renewal records it)") + ".")
    say("To renew, type: " + COMMAND)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Renew the Gmail sign in (a few minutes of clicking in the browser; the sync job keeps running).")
    ap.add_argument("--status", action="store_true", help="show when it was renewed and when it runs out (no network)")
    a = ap.parse_args(argv)
    return status() if a.status else renew()


if __name__ == "__main__":
    sys.exit(main())
