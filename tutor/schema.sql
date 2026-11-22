-- The tutor service's database: one file, /data/tutor/tutor.db. No upgrade machinery yet: a
-- file at another user_version than store.SCHEMA_VERSION is refused at start.

-- A lesson belongs to a project as the pair (name, created): a new project with an old name
-- starts with no lessons. Its Claude Code session is one per lesson; `session_started` says
-- whether a turn has made it, so the next one resumes it instead of creating it.
CREATE TABLE lessons (
    id              INTEGER PRIMARY KEY,
    project         TEXT NOT NULL,
    created         TEXT NOT NULL,
    title           TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'open',
    session_id      TEXT NOT NULL,
    session_started INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX lessons_of_project ON lessons (project, created);

-- One turn: the learner's message and what the tutor did with it.
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    lesson_id   INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT NOT NULL DEFAULT '',
    usage_json  TEXT,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at TEXT
);
CREATE INDEX runs_of_lesson ON runs (lesson_id, id);

CREATE TABLE messages (
    id         INTEGER PRIMARY KEY,
    lesson_id  INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    run_id     INTEGER REFERENCES runs (id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    tools_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX messages_of_lesson ON messages (lesson_id, id);

-- The persisted stream of a run: append-only, read by sequence (gaggiclanker's chat_events).
CREATE TABLE events (
    run_id INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,
    kind   TEXT NOT NULL,
    data   TEXT NOT NULL,
    at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (run_id, seq)
);

PRAGMA user_version = 1;
