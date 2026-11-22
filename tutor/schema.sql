-- The tutor service's database: one file, /data/tutor/tutor.db, at user_version 2. A file at
-- version 1 is brought to 2 in place at start (store.MIGRATE_1_TO_2); any other version is
-- refused.

-- A lesson belongs to a project as the pair (name, created): a new project with an old name
-- starts with no lessons. Every project has one lesson of kind `goals`, always first; the
-- others are ordered by `position`, and those of the study plan hang on a module. `state` is
-- not_started until it has a message, then in_progress; done once a tutor closed it (with its
-- summary and one line); dropped when a change of plan took it out after it was started.
-- `slug` is fixed at creation: it names the lesson's folder in the workspace, which a rename
-- must not move. Its Claude Code session is one per lesson; `session_started` says whether a
-- turn has made it, `session_text` the hash of the instructions it was made with (a session
-- made with other instructions is replaced by a fresh one, taken up from the lesson's files).
CREATE TABLE lessons (
    id              INTEGER PRIMARY KEY,
    project         TEXT NOT NULL,
    created         TEXT NOT NULL,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'lesson',
    objective       TEXT NOT NULL DEFAULT '',
    module_id       INTEGER REFERENCES modules (id) ON DELETE SET NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'not_started',
    summary         TEXT NOT NULL DEFAULT '',
    one_line        TEXT NOT NULL DEFAULT '',
    closed_at       TEXT,
    slug            TEXT NOT NULL DEFAULT '',
    session_id      TEXT NOT NULL,
    session_started INTEGER NOT NULL DEFAULT 0,
    session_text    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX lessons_of_project ON lessons (project, created);

-- The study plan's modules, in order. Their lessons point at them.
CREATE TABLE modules (
    id         INTEGER PRIMARY KEY,
    project    TEXT NOT NULL,
    created    TEXT NOT NULL,
    title      TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX modules_of_project ON modules (project, created);

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

-- `context` is what the tutor was told before the learner's own text (the lesson's opening
-- context, the outcome of a proposal): sent with the message, never shown as the learner's.
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY,
    lesson_id  INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    run_id     INTEGER REFERENCES runs (id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    tools_json TEXT NOT NULL DEFAULT '[]',
    context    TEXT NOT NULL DEFAULT '',
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

-- A tutor's proposal (a study plan, or a change of it), shown to the learner as a card in the
-- message it came with; only the learner's approval applies it. `approved_json` is what the
-- learner approved (their edit of the proposal), `changes_json` the sentences that say how it
-- differs; `told` is set once the tutor was told the outcome, at the next message.
CREATE TABLE proposals (
    id            INTEGER PRIMARY KEY,
    lesson_id     INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    run_id        INTEGER REFERENCES runs (id) ON DELETE SET NULL,
    message_id    INTEGER REFERENCES messages (id) ON DELETE SET NULL,
    kind          TEXT NOT NULL,
    proposed_json TEXT NOT NULL,
    approved_json TEXT,
    changes_json  TEXT NOT NULL DEFAULT '[]',
    state         TEXT NOT NULL DEFAULT 'open',
    told          INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at    TEXT
);
CREATE INDEX proposals_of_lesson ON proposals (lesson_id, id);

-- Every call of a "learn" tool, refusals included (gaggiclanker's tool_calls): which run, which
-- tool, a hash of its input (the transcript holds the readable copy), how long, how it ended.
CREATE TABLE tool_calls (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER,
    tool        TEXT NOT NULL,
    permission  TEXT NOT NULL,
    input_hash  TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    error       TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Texts the learner can edit, seeded from the files shipped in the image (gaggiclanker's
-- prompts table): the live text and the shipped one; edited when they differ.
CREATE TABLE prompts (
    name            TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    default_content TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

PRAGMA user_version = 2;
