-- The tutor service's database: one file, /data/tutor/tutor.db, at user_version 4. A file at
-- version 1, 2 or 3 is brought to 4 in place at start (store.MIGRATIONS); any other version is
-- refused.

-- A lesson belongs to a project as the pair (name, created): a new project with an old name
-- starts with no lessons. Every project has one lesson of kind `goals`, always first; the
-- others are ordered by `position`, and those of the study plan hang on a module. `state` is
-- not_started until it has a message, then in_progress; done once a tutor closed it (with its
-- summary and one line); dropped when a change of plan took it out after it was started.
-- `slug` is fixed at creation: it names the lesson's folder in the workspace, which a rename
-- must not move. `agent` is the agent that teaches it (claude or pi), NULL for the default one
-- (the setting `default_agent`); its sessions are in `sessions`, one per agent.
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
    project_removed INTEGER NOT NULL DEFAULT 0,
    agent           TEXT,
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

-- A lesson's session of one agent (Claude Code's or pi's own memory of the conversation, a
-- cache: the lesson's files are the truth). `started` says whether a turn has made it,
-- `text_hash` the hash of the instructions it was made with (a session made with other
-- instructions is replaced by a fresh one, taken up from the lesson's files), `seen_message`
-- the id of the last learner's message it saw: the turns after it were answered by the other
-- agent, and it is told of them when it takes the lesson back.
CREATE TABLE sessions (
    lesson_id    INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    agent        TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    started      INTEGER NOT NULL DEFAULT 0,
    text_hash    TEXT NOT NULL DEFAULT '',
    seen_message INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lesson_id, agent)
);

-- One turn: the learner's message and what the tutor did with it; `agent` answered it.
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    lesson_id   INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT NOT NULL DEFAULT '',
    usage_json  TEXT,
    agent       TEXT NOT NULL DEFAULT 'claude',
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at TEXT
);
CREATE INDEX runs_of_lesson ON runs (lesson_id, id);

-- `context` is what the tutor was told before the learner's own text (the lesson's opening
-- context, the outcome of a proposal): sent with the message, never shown as the learner's.
-- `composed` marks a learner's message whose text the system wrote (a card's answer sent as a
-- message): never the learner's own words for the quote rule.
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY,
    lesson_id  INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    run_id     INTEGER REFERENCES runs (id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    tools_json TEXT NOT NULL DEFAULT '[]',
    context    TEXT NOT NULL DEFAULT '',
    composed   INTEGER NOT NULL DEFAULT 0,
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

-- The knowledge base (knowledge.py), global to the installation. Its ids are never reused
-- (AUTOINCREMENT): the change log and what a record rests on name them. A record is a goal, a
-- preference, the background text (one), a project's insight or a global insight; `project`
-- and `created` name the project it came from (an insight's owner), NULL for what the learner
-- wrote; `project_removed` is set when that project was destroyed and its knowledge kept.
-- `rests_on_json` is {"evidence": [ids], "insights": [ids]}; an insight a tutor changed is
-- `retired`, with `replaced_by` the one that took its place (whose `replaces` names it).
-- `by_json` is who wrote it: {"who": "learner"} or {"who": "tutor", project, created,
-- lesson_id, run_id}.
CREATE TABLE records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    text            TEXT NOT NULL,
    insight_kind    TEXT,
    project         TEXT,
    created         TEXT,
    project_removed INTEGER NOT NULL DEFAULT 0,
    rests_on_json   TEXT NOT NULL DEFAULT '{"evidence": [], "insights": []}',
    state           TEXT NOT NULL DEFAULT 'active',
    replaces        INTEGER,
    replaced_by     INTEGER,
    reason          TEXT NOT NULL DEFAULT '',
    by_json         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX records_of_kind ON records (kind, state);

-- What the system itself saw: a check that ran (`check`), a quoted sentence of the learner's
-- (`quote`), an answered question (`answer`), a closed lesson (`completion`).
CREATE TABLE evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    project         TEXT NOT NULL,
    created         TEXT NOT NULL,
    project_removed INTEGER NOT NULL DEFAULT 0,
    lesson_id       INTEGER REFERENCES lessons (id) ON DELETE SET NULL,
    run_id          INTEGER,
    data_json       TEXT NOT NULL,
    at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX evidence_of_project ON evidence (project, created);

-- Every write to a record, append-only: who, and each record's state before and after (NULL
-- where it did not exist). Undo applies `before` as a new change that names it (`undoes`).
CREATE TABLE changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT NOT NULL,
    by_json    TEXT NOT NULL,
    items_json TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    undoes     INTEGER,
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- A tutor's proposal of a global insight (`add`), or of the replacement or retirement of one
-- in force (`target`): listed for the Knowledge page, applied only by the learner. `text` is
-- as the learner edited it, `proposed_text` as the tutor wrote it; `told` is set once the
-- proposing tutor was told the outcome, at the next message of its lesson.
CREATE TABLE global_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action          TEXT NOT NULL,
    target          INTEGER,
    text            TEXT,
    proposed_text   TEXT,
    reason          TEXT NOT NULL DEFAULT '',
    rests_on_json   TEXT NOT NULL,
    project         TEXT NOT NULL,
    created         TEXT NOT NULL,
    project_removed INTEGER NOT NULL DEFAULT 0,
    lesson_id       INTEGER REFERENCES lessons (id) ON DELETE SET NULL,
    run_id          INTEGER,
    state           TEXT NOT NULL DEFAULT 'open',
    record_id       INTEGER,
    told            INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at      TEXT
);

-- An exercise or a quick question a tutor set (cards.py), shown as a card in the message it
-- came with. `spec_json` is what the tutor gave (a question's right answer among it, kept from
-- the page until it is answered); `result_json` the last check, or the learner's answer;
-- `told` is cleared when the learner acts, set once the tutor is told, at the next message.
CREATE TABLE cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id   INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES runs (id) ON DELETE SET NULL,
    message_id  INTEGER REFERENCES messages (id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,
    spec_json   TEXT NOT NULL,
    state       TEXT NOT NULL,
    result_json TEXT,
    told        INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX cards_of_lesson ON cards (lesson_id, id);

-- The service's settings, by name: `default_agent` (claude or pi; claude when it is not set).
CREATE TABLE settings (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

PRAGMA user_version = 4;
