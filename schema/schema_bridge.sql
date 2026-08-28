-- fit-bridge state database
--
-- One database per bridge, not one shared across bridges. Each bridge gets its
-- own pause switch, start date and cap, so its state, its retries and the blast
-- radius of a bug stay its own. `sport` is the canonical name source.py maps
-- every upstream vocabulary onto, because the device an activity is attributed
-- to is chosen by sport.
--
-- The bridge is a cron job that may be killed at any point, so every activity
-- carries its own outcome and nothing is inferred from "what the last run got
-- to". A run that dies halfway costs nothing: the next one picks up exactly the
-- activities that are not yet in a terminal state.
--
-- STATUS VALUES
--   pending    seen, not yet processed (or a run died mid-flight)
--   uploaded   accepted by Garmin, internalId recorded            [terminal]
--   duplicate  Garmin already has it (HTTP 409, or a duplicate
--              failure message on a 2xx)                          [terminal]
--   skipped    deliberately not uploaded — sport type excluded    [terminal]
--   failed     retried until attempts >= BRIDGE_MAX_ATTEMPTS, then left alone
--              so it can be inspected rather than looping forever
--
-- Terminal states are never retried. `failed` is the only state that comes
-- back, and only while it has attempts left.

-- ============================================================
-- Activities
-- ============================================================

CREATE TABLE IF NOT EXISTS bridge_activities (
    source_activity_id TEXT PRIMARY KEY,
    source_date INTEGER,             -- YYYYMMDD, as the source reports it
    source_start_time INTEGER,       -- unix seconds, stamped into time_created
    name TEXT,
    sport_type TEXT,                 -- the upstream's own code or label
    sport TEXT,                      -- canonical: running|cycling|swimming|…
    distance REAL,                   -- metres
    status TEXT NOT NULL,            -- pending|uploaded|duplicate|skipped|failed
    device_product_id INTEGER,       -- the device this was attributed to
    garmin_upload_id TEXT,
    garmin_activity_id TEXT,
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    source_sha256 TEXT,              -- FIT as downloaded from the source
    converted_sha256 TEXT,           -- FIT as uploaded to Garmin
    first_seen_at TEXT,
    uploaded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_bridge_activities_status
    ON bridge_activities(status);
CREATE INDEX IF NOT EXISTS idx_bridge_activities_date
    ON bridge_activities(source_date);

-- ============================================================
-- Runs
-- ============================================================

-- One row per invocation. `considered` counts the activities the source returned
-- inside the window; the others count what the run actually did.
CREATE TABLE IF NOT EXISTS bridge_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    considered INTEGER,
    uploaded INTEGER,
    duplicates INTEGER,
    failed INTEGER,
    skipped INTEGER,
    status TEXT,                     -- ok|error
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_bridge_runs_started
    ON bridge_runs(started_at);

-- ============================================================
-- Views
-- ============================================================
-- Dropped and recreated so a fix here reaches databases that already exist.

DROP VIEW IF EXISTS v_bridge_status;
CREATE VIEW v_bridge_status AS
SELECT status,
       COUNT(*)                          AS activities,
       MIN(source_date)                   AS first_date,
       MAX(source_date)                   AS last_date,
       ROUND(SUM(distance) / 1000.0, 1)  AS total_km
FROM bridge_activities
GROUP BY status
ORDER BY activities DESC;

DROP VIEW IF EXISTS v_bridge_activities;
CREATE VIEW v_bridge_activities AS
SELECT source_activity_id,
       substr(CAST(source_date AS TEXT), 1, 4) || '-' ||
       substr(CAST(source_date AS TEXT), 5, 2) || '-' ||
       substr(CAST(source_date AS TEXT), 7, 2)  AS date,
       name,
       sport_type,
       ROUND(distance / 1000.0, 2)             AS km,
       status,
       garmin_activity_id,
       attempts,
       last_error,
       uploaded_at
FROM bridge_activities
ORDER BY source_date DESC, source_start_time DESC;

-- Activities that need attention: retries exhausted, or stuck pending.
DROP VIEW IF EXISTS v_bridge_failures;
CREATE VIEW v_bridge_failures AS
SELECT source_activity_id, source_date, name, sport_type,
       status, attempts, last_error, first_seen_at
FROM bridge_activities
WHERE status IN ('failed', 'pending')
ORDER BY source_date DESC;

DROP VIEW IF EXISTS v_bridge_runs;
CREATE VIEW v_bridge_runs AS
SELECT run_id, started_at, finished_at, status,
       considered, uploaded, duplicates, failed, skipped, error
FROM bridge_runs
ORDER BY run_id DESC;
