"""The Gmail renewal reminder: a Mac banner that tells John to renew the Gmail sign in before it runs out. Run by its own launchd job every 15 minutes. Standard library only.
It reads secrets/gmail_renewal.json (written only by scheduler/gmail_renew.py), makes no network call and no Google call, and writes only its own state file secrets/gmail_reminder_state.json.

A banner is sent only if all of these hold (Mac local time):
  - it is from 10:00 on the day BEFORE the day the sign in is estimated to run out, until the record shows a newer renewal (it keeps going, with "overdue" wording, after the run out time);
  - it is between 10:00 and 22:00 (22:00 itself is too late);
  - the last banner was at least 3 hours ago.
If the refresh token in the live token.json no longer matches the fingerprint in the record, the renewal did not stick: the banner says so (same hours and gap, but no waiting for the day before).

  venv/bin/python -m scheduler.gmail_reminder                       what the job does
  venv/bin/python -m scheduler.gmail_reminder --dry-run             say what it would do now and why; send nothing, write nothing
  venv/bin/python -m scheduler.gmail_reminder --now "2026-10-21 10:05"   pretend it is that time (this is always a dry run)
  venv/bin/python -m scheduler.gmail_reminder --test-notify         send one test banner now
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START_HOUR, END_HOUR, GAP_HOURS = 10, 22, 3              # banners from 10:00 up to (not including) 22:00, at most one every 3 hours
TITLE = "Job tracker: renew Gmail sign in"
COMMAND = "cd ~/job-tracker && venv/bin/python -m scheduler.gmail_renew"


def paths(root=ROOT):
    s = Path(root) / "secrets"
    return {"record": s / "gmail_renewal.json", "state": s / "gmail_reminder_state.json", "token": s / "token.json"}


def fmt_day(d):
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def fmt_time(d):
    return f"{d.hour % 12 or 12}{':%02d' % d.minute if d.minute else ''}{'am' if d.hour < 12 else 'pm'}"


def when(d):
    return f"{fmt_day(d)} {d.year}, {fmt_time(d)}"


def reminders_start(runs_out):
    """10:00 on the day before the day the sign in runs out."""
    return runs_out.replace(hour=START_HOUR, minute=0, second=0, microsecond=0) - timedelta(days=1)


def read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def live_fingerprint(token_path):
    """The first 8 characters of the SHA256 of the refresh token in token.json (never the token), or None."""
    info = read_json(token_path)
    return hashlib.sha256(info["refresh_token"].encode()).hexdigest()[:8] if info and info.get("refresh_token") else None


def decide(rec, state, live_fp, now):
    """(kind, why, state): kind is None (send nothing), "normal", "overdue" or "stuck"; state is the state to keep (reset when the record is a newer renewal)."""
    if not rec:
        return None, "there is no record of a renewal", state
    seen = rec.get("renewed_at_utc")
    if state.get("renewed_at_seen") != seen:
        state = {"renewed_at_seen": seen, "last_sent": None}
    out = datetime.fromisoformat(rec["runs_out_estimate_utc"]).astimezone(now.tzinfo)
    start = reminders_start(out)
    stuck = bool(rec.get("fingerprint")) and bool(live_fp) and rec["fingerprint"] != live_fp
    if now < start and not stuck:
        return None, f"it is too early: the reminders start {when(start)}", state
    if not START_HOUR <= now.hour < END_HOUR:
        return None, f"it is outside {START_HOUR}:00 to {END_HOUR}:00", state
    last = datetime.fromisoformat(state["last_sent"]).astimezone(now.tzinfo) if state.get("last_sent") else None
    if last and now - last < timedelta(hours=GAP_HOURS):
        return None, f"the last banner was at {when(last)}, the next is allowed from {when(last + timedelta(hours=GAP_HOURS))}", state
    kind = "stuck" if stuck else "overdue" if now >= out else "normal"
    why = {"stuck": "the saved sign in is not the one that was renewed", "overdue": f"the sign in was due to run out on {when(out)}", "normal": f"the sign in runs out {when(out)}"}[kind]
    return kind, why, state


A_BANNER = {"normal": "a reminder", "overdue": "an overdue reminder", "stuck": "a 'did not stick' reminder"}


def text_for(kind, runs_out):
    if kind == "overdue":
        return f"The renewal is overdue. In Terminal type: {COMMAND}"
    if kind == "stuck":
        return f"The last renewal did not stick. In Terminal type: {COMMAND}"
    return f"It runs out {fmt_day(runs_out)}, {fmt_time(runs_out)}. In Terminal type: {COMMAND}"


def notify(title, text):
    """A Mac banner through osascript, the way scheduler/housekeeping.py sends its banner. The text is passed as arguments, never pasted into the AppleScript, so a quote in it cannot break it."""
    script = ["on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run"]
    try:
        subprocess.run(["/usr/bin/osascript"] + [x for line in script for x in ("-e", line)] + [text, title], check=True, capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def write_state(path, state):
    tmp = Path(str(path) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(state))
    os.replace(tmp, path)


def run(root=ROOT, now=None, dry_run=False, send=notify, say=print):
    p = paths(root)
    now = now or datetime.now().astimezone()
    rec, state = read_json(p["record"]), read_json(p["state"]) or {}
    if rec is None or "runs_out_estimate_utc" not in rec:
        say(f"{now:%Y-%m-%d %H:%M} no record of a Gmail renewal ({p['record'].name}): nothing to do")
        return None
    kind, why, new_state = decide(rec, state, live_fingerprint(p["token"]), now)
    out = datetime.fromisoformat(rec["runs_out_estimate_utc"]).astimezone(now.tzinfo)
    if dry_run:
        say(f"{now:%a %Y-%m-%d %H:%M}: " + (f"would send {A_BANNER[kind]} ({why}): {text_for(kind, out)}" if kind else f"would send nothing: {why}") + ("; it would also note this renewal in its state file" if new_state != state else ""))
        return kind
    if kind:
        if send(TITLE, text_for(kind, out)):
            new_state = {**new_state, "last_sent": now.isoformat(timespec="seconds")}
            say(f"{now:%Y-%m-%d %H:%M} sent {A_BANNER[kind]} ({why})")
        else:
            say(f"{now:%Y-%m-%d %H:%M} could not send the banner")
    if new_state != state:
        write_state(p["state"], new_state)
    return kind


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remind John to renew the Gmail sign in.")
    ap.add_argument("--dry-run", action="store_true", help="say what it would do now and why; send and write nothing")
    ap.add_argument("--now", metavar="'YYYY-MM-DD HH:MM'", help="pretend it is this time (always a dry run)")
    ap.add_argument("--test-notify", action="store_true", help="send one test banner now")
    a = ap.parse_args(argv)
    if a.test_notify:
        ok = notify(TITLE, "This is a test banner. If you can read it, the Gmail renewal reminders will reach you.")
        print("sent the test banner" if ok else "could not send the test banner")
        return 0 if ok else 1
    now = datetime.strptime(a.now, "%Y-%m-%d %H:%M").astimezone() if a.now else None
    run(now=now, dry_run=a.dry_run or bool(a.now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
