-- Table structure of the job tracker database. No rows.
-- Make an empty database with: sqlite3 db/tracker.db < schema.sql

PRAGMA user_version = 6;

CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    industry TEXT,
    notes TEXT
, confirmed_by_user INTEGER DEFAULT 1, content_checks_passed TEXT);

CREATE TABLE applications (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    role_title TEXT,
    date_applied TEXT,
    source TEXT,                -- LinkedIn, company site, referral, etc.
    current_status TEXT,
    current_stage TEXT,
    last_updated TEXT
, confirmed_by_user INTEGER DEFAULT 1, assessment_submitted INTEGER DEFAULT 0);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    event_type TEXT,            -- closed status set (see ingest/classifier.py)
    event_date TEXT,
    source_email_id TEXT,
    detail_text TEXT,
    confidence REAL,
    confirmed INTEGER DEFAULT 0  -- 0 = pending review, 1 = confirmed/auto-applied
);

CREATE TABLE contacts (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    name TEXT,
    role TEXT,
    email TEXT
);

CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE review_queue (
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
           , review_category TEXT);

CREATE TABLE sender_links (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('address','domain')),
    value       TEXT NOT NULL,
    company_id  INTEGER REFERENCES companies(id),   -- NULL only for method 'conflict'
    method      TEXT NOT NULL CHECK (method IN ('existing','user','text','search','conflict')),
    note        TEXT,                               -- the reason, for 'conflict' rows
    created_at  TEXT,
    UNIQUE (kind, value));

CREATE TABLE discarded_companies (
    id            INTEGER PRIMARY KEY,
    name_key      TEXT NOT NULL UNIQUE,             -- exact-match key
    name          TEXT NOT NULL,                    -- as shown on the Discarded list
    discarded_at  TEXT NOT NULL,
    snapshot      TEXT NOT NULL);

CREATE TABLE quiet_log (
    id             INTEGER PRIMARY KEY,
    message_id     TEXT NOT NULL UNIQUE,
    name_key       TEXT NOT NULL,
    email_date     TEXT,
    sender_domain  TEXT,
    llama_company  TEXT,
    status         TEXT,
    stage          TEXT,
    logged_at      TEXT NOT NULL);

CREATE TABLE search_cache (query TEXT PRIMARY KEY, results_json TEXT, http_status INTEGER, created_at TEXT);

CREATE TABLE search_credits (month TEXT PRIMARY KEY, credits INTEGER);

CREATE TABLE step_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL UNIQUE COLLATE NOCASE,
        sets_status TEXT,                  -- NULL: the step only sets the Stage text; else the status it sets
        counts_as TEXT,                    -- the email event type it counts as in Metrics and Timeline (NULL: none)
        suggest_after TEXT,                -- comma separated statuses whose rows suggest this step first in the panel
        sort_order INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL);

CREATE TABLE application_edits (
        application_id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL,       -- the application this row was made for: an id can be reused after a Delete
        company_name TEXT NOT NULL,
        display_name TEXT, role TEXT, industry TEXT, notes TEXT,   -- NULL = automatic
        status TEXT, status_at TEXT,       -- a status or stage edit is a dated event: local time it was saved
        stage TEXT, stage_at TEXT,
        edited_at TEXT NOT NULL);

CREATE TABLE manual_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,
        company_name TEXT NOT NULL,
        step_type_id INTEGER NOT NULL REFERENCES step_types(id),
        label TEXT NOT NULL,               -- copied from the step type when the step is recorded
        step_date TEXT NOT NULL,           -- YYYY-MM-DD, the day John did it
        note TEXT,
        created_at TEXT NOT NULL,
        removed_at TEXT);

CREATE TABLE edit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        company_id INTEGER,                -- with company_name: the application the change was made on (ids are reused after a Delete)
        company_name TEXT,
        field TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        logged_at TEXT NOT NULL);

CREATE TABLE upcoming_dates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,       -- with company_name: the application the date was added to (ids are reused after a Delete)
        company_name TEXT NOT NULL,
        kind TEXT NOT NULL,                -- from the fixed list in dashboard/server_v17.py
        due_date TEXT NOT NULL,            -- YYYY-MM-DD as typed, no time zone
        due_time TEXT,                     -- HH:MM as written in the invite, or NULL
        note TEXT,
        created_at TEXT NOT NULL,
        removed_at TEXT);

CREATE TABLE auto_roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,       -- with company_name: the application the role was read for (ids are reused after a Delete)
        company_name TEXT NOT NULL,
        role TEXT NOT NULL,                -- words copied from the stored email text, as they appear there
        email_id TEXT NOT NULL,            -- events.source_email_id of the email it was read from
        email_date TEXT,                   -- that email's date, YYYY-MM-DD
        disagreements INTEGER NOT NULL DEFAULT 0,   -- later emails of this application that named a different role
        found_at TEXT NOT NULL,
        UNIQUE (application_id, company_id, company_name));

CREATE TABLE auto_role_tries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,
        company_name TEXT NOT NULL,
        email_id TEXT NOT NULL,            -- each email is sent to the local model once only
        tried_at TEXT NOT NULL,
        outcome TEXT NOT NULL,             -- found, agree, disagree, none, refused: <why>
        UNIQUE (application_id, company_id, company_name, email_id));

CREATE TABLE company_industries (
        company_key TEXT PRIMARY KEY,      -- the company name as the dashboard shows it, case and punctuation ignored
        company_name TEXT NOT NULL,        -- the name that was searched
        industry TEXT,                     -- one item of scheduler/enrich.py INDUSTRIES, or NULL (Unknown)
        outcome TEXT NOT NULL,             -- found or unknown
        looked_up_at TEXT NOT NULL,
        tries INTEGER NOT NULL);

CREATE TABLE search_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        searched_at TEXT NOT NULL,         -- local time the request was sent (the monthly cap counts these rows)
        company_key TEXT NOT NULL,
        http_status INTEGER);

CREATE INDEX ix_quiet_log_name_key ON quiet_log (name_key);

CREATE UNIQUE INDEX ux_events_source_email_id ON events (source_email_id) WHERE source_email_id IS NOT NULL;

CREATE INDEX manual_steps_app ON manual_steps(application_id);

CREATE INDEX upcoming_dates_app ON upcoming_dates(application_id);

CREATE INDEX auto_role_tries_app ON auto_role_tries(application_id);

CREATE INDEX search_log_at ON search_log(searched_at);

CREATE TRIGGER edit_log_no_update BEFORE UPDATE ON edit_log BEGIN SELECT RAISE(ABORT, 'edit_log is append only'); END;

CREATE TRIGGER edit_log_no_delete BEFORE DELETE ON edit_log BEGIN SELECT RAISE(ABORT, 'edit_log is append only'); END;
