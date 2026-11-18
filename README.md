# Job tracker

This keeps track of my job applications. It reads application emails from Gmail, works out the company and the status of each one with a local model, and keeps everything in a SQLite database on my Mac. A local web page shows the applications, their history and a few numbers.

No data, keys or sign in files are included in this repository.

This copy is for reading. It has not been run as a fresh install.

## What it does

- Reads new application emails from Gmail. Access is read only.
- Asks a local model, run by Ollama, for the company, the status and the stage of each email. The email text stays on the Mac.
- Links each email to a company it already knows, or adds a new company when the sender confirms the name. Anything unclear goes to a review list.
- Moves each application forward through applied, assessment, interview, offer or rejection. It never moves one backward.
- Fills in an empty role from the stored part of the emails, using the local model.
- Fills in an empty industry from one web search per company (Tavily). Only the company name and the words "company UK" are sent. The sync's own company check also sends company names, with the words "official website".
- Shows everything on a dashboard at http://localhost:8001. Any value can be edited there, and my own edits always win.
- Keeps no email text in its logs. A log line names the message id, the company and the status.

## The pipeline

1. Fetch: Gmail messages newer than the last good run.
2. Classify: the local model reads each email and returns the company, status, stage and confidence.
3. Match: the code, not the model, links the email to a company and an application, or sends it to review.
4. Write: the event goes into the database and the status moves forward.
5. Enrich: a separate job fills empty roles and industries.
6. Show: the dashboard reads the database. It makes no network calls.

## Folders

- scheduler: the jobs. run_sync.py is the sync. housekeeping.py watches the heartbeat, makes a daily backup and trims the log. gmail_renew.py renews the Gmail sign in and gmail_reminder.py reminds me before it runs out. enrich.py fills roles and industries, and tavily_check.py tests the search key with one search. resolver_run.py is the filing path the sync uses and resolver_switch.py turns it on or off. runlock.py holds the run lock and the heartbeat. discard.py removes or restores a company for the dashboard. import_saved.py was a one off import.
- ingest: Gmail, the classifier, the matcher, the sender check, the company check and the search client.
- dashboard: server_v19.py is the server. It builds on server_v14.py to server_v18.py and app.py. index_v19.html is the page and live_v19.js its script. _selfcheck.py is a small test with made up data.
- schema.sql: the table structure, with no rows.
- settings.template: the files and settings the code expects, all left empty.

## What is needed

- A Mac. The jobs run under launchd and the reminders are Mac notifications.
- Python 3.13 and the packages in requirements.txt.
- Ollama, with the llama3.1:8b model pulled.
- A Google client file for a desktop app with read access to Gmail, saved as secrets/credentials.json.
- Your own Google Cloud project, with your own client file and your own keys. Do not use anyone else's.
- A Tavily key in secrets/tavily_key.txt. This is only needed for the industry lookups.

## How to run it

Set up once:

```
python3 -m venv venv
venv/bin/pip install -r requirements.txt
mkdir -p db logs secrets
sqlite3 db/tracker.db < schema.sql
```

Keep the secrets folder private. Only you should be able to open it, and it must never be shared or uploaded. This makes the three folders yours alone:

```
chmod 700 db logs secrets
```

Put the Google client file in secrets, then sign in to Gmail. A browser opens and the sign in is saved as secrets/token.json. When the browser asks, approve only the read only Gmail access. The code asks for nothing more:

```
venv/bin/python -m scheduler.gmail_renew
```

Run one sync, then start the dashboard and open http://localhost:8001:

```
venv/bin/python -m scheduler.run_sync
venv/bin/python dashboard/server_v19.py
```

On my Mac each job runs under launchd. The sync runs every minute through scheduler/launchd_run.sh, housekeeping every five minutes, the Gmail reminder and enrich every 15 minutes, and the dashboard all the time. The launchd files are not included. Replace YOUR_USER in scheduler/launchd_run.sh with your own user name.

The industry lookups run only while the empty file logs/lookups_on exists, and stop at 600 searches a month. This shows how many have been used:

```
venv/bin/python -m scheduler.enrich --status
```

The small test runs without a database:

```
venv/bin/python dashboard/_selfcheck.py
```
