#!/bin/bash
# launchd wrapper for the Gmail->classifier->matcher->DB sync.
# Runs run_sync from the project root using the project venv, and prints a
# timestamp so each scheduled run is visible in logs/sync.log. Scheduling only;
# run_sync's own logic is untouched.
cd /Users/YOUR_USER/job-tracker || exit 1
echo "===== $(/bin/date '+%Y-%m-%d %H:%M:%S %z') sync run ====="
exec venv/bin/python -m scheduler.run_sync
