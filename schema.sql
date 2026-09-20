-- PlainWatch SQLite schema
-- Schema version: 1

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    note_id TEXT NOT NULL UNIQUE,
    to_track_hash TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'IDLE'
        CHECK (status IN (
            'IDLE',
            'WAITING_TO_HANDOFF',
            'IN_PROGRESS',
            'STOPPED'
        )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    -- last_run_at stores the UTC timestamp when the activity completed.
    last_run_at TEXT,
    -- Despite its name, next_run_at stores the schedule interval in seconds as text.
    next_run_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_topics_dispatch
    ON topics (status, next_run_at);

CREATE TABLE IF NOT EXISTS feed_items (
    id INTEGER PRIMARY KEY,
    plainapp_feed_item_id TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feed_items_published_at
    ON feed_items (published_at);

CREATE TABLE IF NOT EXISTS activity_notification_log (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL,
    note_id TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('COMPLETED', 'FAILED', 'WORKING')),
    message TEXT,
    -- JSON metadata produced during the activity run.
    extra TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(extra)),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_activity_log_topic
    ON activity_notification_log (topic_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_activity_log_note
    ON activity_notification_log (note_id, started_at DESC);