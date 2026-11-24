"""The tutor service's store: one SQLite file, its lessons (with their agent and their sessions,
one per agent), plans, messages, runs, events, proposals, cards, the audit of the "learn"
tools, the tutor's instructions and the service's settings (the default agent). The knowledge
base lives in the same file; its functions are in knowledge.py.

Lifted from gaggiclanker (the maintainer's project, AGPL like this one): the connection and its
pragmas from gaggiclanker/db/connection.py, the runs and the persisted event stream from the
generic classes of gaggiclanker/db/repos/chat.py (ChatRunsRepository, ChatEventsRepository,
reconcile_running among them), the seeding of an editable text from gaggiclanker/llm/prompts.py
(seed_prompts) and gaggiclanker/db/repos/llm.py (PromptsRepository). Adapted: the standard
library's sqlite3, used from the event loop's thread only (every statement here is small, and
one writer is all there is), plain functions over one Database instead of repository classes,
and no models: rows become the dictionaries the HTTP contract names.
"""
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import uuid

SCHEMA_VERSION = 6
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
LESSONS_DIR = "/workspace/.learn/lessons"
GOALS_TITLE = "Goals"
INSTRUCTIONS = "tutor"

# A file of version 1 (step 2: lessons without plans) brought to version 2 in place, in one
# transaction: the new columns and tables added, a lesson's state read from whether it has
# messages, its position from its id. Its slug is filled by `fill_slugs` after.
MIGRATE_1_TO_2 = """
BEGIN;
ALTER TABLE lessons ADD COLUMN kind TEXT NOT NULL DEFAULT 'lesson';
ALTER TABLE lessons ADD COLUMN objective TEXT NOT NULL DEFAULT '';
ALTER TABLE lessons ADD COLUMN module_id INTEGER REFERENCES modules (id) ON DELETE SET NULL;
ALTER TABLE lessons ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE lessons ADD COLUMN summary TEXT NOT NULL DEFAULT '';
ALTER TABLE lessons ADD COLUMN one_line TEXT NOT NULL DEFAULT '';
ALTER TABLE lessons ADD COLUMN closed_at TEXT;
ALTER TABLE lessons ADD COLUMN slug TEXT NOT NULL DEFAULT '';
ALTER TABLE lessons ADD COLUMN session_text TEXT NOT NULL DEFAULT '';
ALTER TABLE messages ADD COLUMN context TEXT NOT NULL DEFAULT '';
UPDATE lessons SET position = id,
    state = CASE WHEN EXISTS (SELECT 1 FROM messages WHERE messages.lesson_id = lessons.id) THEN 'in_progress' ELSE 'not_started' END;
CREATE TABLE modules (id INTEGER PRIMARY KEY, project TEXT NOT NULL, created TEXT NOT NULL, title TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')));
CREATE INDEX modules_of_project ON modules (project, created);
CREATE TABLE proposals (id INTEGER PRIMARY KEY, lesson_id INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES runs (id) ON DELETE SET NULL, message_id INTEGER REFERENCES messages (id) ON DELETE SET NULL,
    kind TEXT NOT NULL, proposed_json TEXT NOT NULL, approved_json TEXT, changes_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'open', told INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), decided_at TEXT);
CREATE INDEX proposals_of_lesson ON proposals (lesson_id, id);
CREATE TABLE tool_calls (id INTEGER PRIMARY KEY, run_id INTEGER, tool TEXT NOT NULL, permission TEXT NOT NULL,
    input_hash TEXT NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, error TEXT,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')));
CREATE TABLE prompts (name TEXT PRIMARY KEY, content TEXT NOT NULL, default_content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')));
PRAGMA user_version = 2;
COMMIT;
"""

# Version 2 (step 3) to 3 (step 4, the knowledge base): the records, evidence, the change log,
# the global proposals and the cards added; a lesson can be kept for its summary when its
# project is destroyed; a learner's message can be one the system composed.
MIGRATE_2_TO_3 = """
BEGIN;
ALTER TABLE lessons ADD COLUMN project_removed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN composed INTEGER NOT NULL DEFAULT 0;
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
CREATE TABLE changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT NOT NULL,
    by_json    TEXT NOT NULL,
    items_json TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    undoes     INTEGER,
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
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
PRAGMA user_version = 3;
COMMIT;
"""
# Version 3 (step 4) to 4 (step 6, two agents): a lesson's agent (NULL for the default one), its
# sessions kept per agent (the one Claude Code session a lesson had is carried over, as having
# seen every message of the lesson so far), the agent that answered each run, and the service's
# settings (the default agent).
MIGRATE_3_TO_4 = """
BEGIN;
ALTER TABLE lessons ADD COLUMN agent TEXT;
ALTER TABLE runs ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude';
CREATE TABLE sessions (
    lesson_id    INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    agent        TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    started      INTEGER NOT NULL DEFAULT 0,
    text_hash    TEXT NOT NULL DEFAULT '',
    seen_message INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lesson_id, agent)
);
INSERT INTO sessions (lesson_id, agent, session_id, started, text_hash, seen_message)
    SELECT id, 'claude', session_id, session_started, session_text,
           CASE WHEN session_started THEN coalesce((SELECT max(messages.id) FROM messages WHERE messages.lesson_id = lessons.id AND role = 'user'), 0) ELSE 0 END
    FROM lessons;
ALTER TABLE lessons DROP COLUMN session_id;
ALTER TABLE lessons DROP COLUMN session_started;
ALTER TABLE lessons DROP COLUMN session_text;
CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
PRAGMA user_version = 4;
COMMIT;
"""
# Version 4 (step 6) to 5 (the polish pass): what each conversation last saw of its project's
# plan, so that its tutor is told at its next message what changed since (plans.plan_note).
MIGRATE_4_TO_5 = """
BEGIN;
ALTER TABLE lessons ADD COLUMN plan_seen TEXT NOT NULL DEFAULT '';
PRAGMA user_version = 5;
COMMIT;
"""
# Version 5 to 6 (brief sets): several briefs, one per project. A set holds goals, preferences
# and the background (`records.set_id`; the other kinds stay global, NULL); every existing one
# goes into set 1, "Default", which a project with no row in `project_sets` uses.
MIGRATE_5_TO_6 = """
BEGIN;
CREATE TABLE brief_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT INTO brief_sets (id, name) VALUES (1, 'Default');
ALTER TABLE records ADD COLUMN set_id INTEGER;
UPDATE records SET set_id = 1 WHERE kind IN ('goal', 'preference', 'background');
CREATE INDEX records_of_set ON records (set_id, kind);
CREATE TABLE project_sets (
    project TEXT NOT NULL,
    created TEXT NOT NULL,
    set_id  INTEGER NOT NULL REFERENCES brief_sets (id),
    PRIMARY KEY (project, created)
);
PRAGMA user_version = 6;
COMMIT;
"""
MIGRATIONS = {1: MIGRATE_1_TO_2, 2: MIGRATE_2_TO_3, 3: MIGRATE_3_TO_4, 4: MIGRATE_4_TO_5, 5: MIGRATE_5_TO_6}
AGENTS = ("claude", "pi")
AGENT_NAMES = {"claude": "Claude Code", "pi": "pi"}
DEFAULT_AGENT = "claude"

# As gaggiclanker has them: WAL so that a reader never waits for the writer; foreign keys,
# which SQLite leaves off per connection and the schema's ON DELETE CASCADE needs (a lesson
# deleted takes its runs, messages and events); NORMAL, which with WAL risks only the last
# transactions on a power cut; a busy timeout that rides out a checkpoint.
PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
)


class StoreError(Exception):
    """The database cannot be used; the message is for the person who reads the log."""


class Database:
    def __init__(self, path):
        self.path = path
        self.conn = None
        self.migrated = None   # the version the file was at, when it was brought up

    def connect(self):
        """Open the file, apply the pragmas, make the schema in a new file and bring a file of
        an older version to this one, a version at a time. A file at another version is refused."""
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        for pragma in PRAGMAS:
            conn.execute(pragma)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        if version == 0 and tables == 0:
            with open(SCHEMA_FILE) as fh:
                conn.executescript(fh.read())
        elif version in MIGRATIONS:
            self.migrated = version
            while version in MIGRATIONS:
                try:
                    conn.executescript(MIGRATIONS[version])
                except sqlite3.Error as exc:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    conn.close()
                    raise StoreError(f"The tutor's database {self.path} is at version {version} and could not be brought to version "
                                     f"{version + 1} ({exc}); it is left at version {version}.") from None
                version += 1
        elif version != SCHEMA_VERSION:
            conn.close()
            raise StoreError(f"The tutor's database {self.path} is at version {version}; this tutor service knows versions 1 to "
                             f"{SCHEMA_VERSION} only. Move the file away to start with an empty one, or run the version of "
                             "clanker-learn that wrote it.")
        self.conn = conn
        fill_slugs(self)
        return self

    def close(self):
        if self.conn is not None:
            # Fold the WAL back into the main file, so that one self-contained file is left.
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()
            self.conn = None

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()


# --- lessons --------------------------------------------------------------------------------

def slug_of(title):
    """A short folder name from a title: lower-case ASCII words joined by dashes, at most 40."""
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    return (words[:40].rstrip("-")) or "lesson"


def fill_slugs(db):
    for row in db.all("SELECT id, title FROM lessons WHERE slug = ''"):
        db.execute("UPDATE lessons SET slug = ? WHERE id = ?", (slug_of(row["title"]), row["id"]))


def folder_of(row):
    """The lesson's folder in the workspace, where its transcript and the tutor's notes are."""
    return f"{LESSONS_DIR}/{row['id']}-{row['slug'] or slug_of(row['title'])}"


def lesson_out(row, running=None):
    return {"id": row["id"], "project": row["project"], "created": row["created"], "title": row["title"], "kind": row["kind"],
            "project_removed": bool(row["project_removed"]),
            "objective": row["objective"], "module": row["module_id"], "position": row["position"], "state": row["state"],
            "summary": row["summary"], "one_line": row["one_line"], "closed_at": row["closed_at"], "folder": folder_of(row),
            "agent": row["agent"], "created_at": row["created_at"], "updated_at": row["updated_at"], "running": running}


ORDER = "ORDER BY kind = 'goals' DESC, position, id"


def lessons(db, project, created):
    """The project's lessons, Goals first (made here the first time they are asked for), then
    in the order of the plan, lessons added by hand after."""
    ensure_goals(db, project, created)
    return db.all(f"SELECT * FROM lessons WHERE project = ? AND created = ? {ORDER}", (project, created))


def lesson(db, lesson_id):
    return db.one("SELECT * FROM lessons WHERE id = ?", (lesson_id,))


def ensure_goals(db, project, created):
    row = db.one("SELECT * FROM lessons WHERE project = ? AND created = ? AND kind = 'goals'", (project, created))
    if row is None:
        cursor = db.execute("INSERT INTO lessons (project, created, title, kind, state, slug) VALUES (?, ?, ?, 'goals', 'not_started', 'goals')",
                            (project, created, GOALS_TITLE))
        row = lesson(db, cursor.lastrowid)
    return row


def next_position(db, project, created):
    return (db.one("SELECT max(position) FROM lessons WHERE project = ? AND created = ?", (project, created))[0] or 0) + 1


def add_lesson(db, project, created, title, objective="", module_id=None, position=None):
    ensure_goals(db, project, created)
    if position is None:
        position = next_position(db, project, created)
    # The state is given: a file brought from version 1 keeps that version's default ('open').
    cursor = db.execute("INSERT INTO lessons (project, created, title, objective, module_id, position, state, slug) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'not_started', ?)",
                        (project, created, title, objective, module_id, position, slug_of(title)))
    return lesson(db, cursor.lastrowid)


def edit_lesson(db, lesson_id, title=None, objective=None):
    if title is not None:
        db.execute(f"UPDATE lessons SET title = ?, updated_at = {NOW} WHERE id = ?", (title, lesson_id))
    if objective is not None:
        db.execute(f"UPDATE lessons SET objective = ?, updated_at = {NOW} WHERE id = ?", (objective, lesson_id))
    return lesson(db, lesson_id)


def delete_lesson(db, lesson_id):
    db.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))


def touch_lesson(db, lesson_id):
    db.execute(f"UPDATE lessons SET updated_at = {NOW} WHERE id = ?", (lesson_id,))


def mark_started(db, lesson_id):
    db.execute("UPDATE lessons SET state = 'in_progress' WHERE id = ? AND state = 'not_started'", (lesson_id,))


def close_lesson(db, lesson_id, summary, one_line):
    db.execute(f"UPDATE lessons SET state = 'done', summary = ?, one_line = ?, closed_at = {NOW}, updated_at = {NOW} WHERE id = ?",
               (summary, one_line, lesson_id))
    return lesson(db, lesson_id)


def has_messages(db, lesson_id):
    return db.one("SELECT 1 FROM messages WHERE lesson_id = ? LIMIT 1", (lesson_id,)) is not None


def set_lesson_agent(db, lesson_id, agent):
    """The lesson's own agent; None for the default one. A lesson under way that is set back to
    the default is given the default agent in force by name, since it no longer follows it."""
    if agent is None and answered_by(db, lesson_id) not in (None, default_agent(db)):
        agent = default_agent(db)
    db.execute(f"UPDATE lessons SET agent = ?, updated_at = {NOW} WHERE id = ?", (agent, lesson_id))
    return lesson(db, lesson_id)


# --- a lesson's sessions, one per agent --------------------------------------------------------
# `started`: a turn of that agent made it; `text_hash`: the instructions it was made with;
# `seen_message`: the id of the last learner's message that session saw (the turns after it,
# answered by the other agent, are the ones it missed).

def session(db, lesson_id, agent):
    """The lesson's session of `agent`, made (not started) the first time it is asked for."""
    row = db.one("SELECT * FROM sessions WHERE lesson_id = ? AND agent = ?", (lesson_id, agent))
    if row is None:
        db.execute("INSERT INTO sessions (lesson_id, agent, session_id) VALUES (?, ?, ?)", (lesson_id, agent, str(uuid.uuid4())))
        row = db.one("SELECT * FROM sessions WHERE lesson_id = ? AND agent = ?", (lesson_id, agent))
    return row


def set_session(db, lesson_id, agent, session_id, started, text_hash=None, seen_message=None):
    session(db, lesson_id, agent)
    db.execute("UPDATE sessions SET session_id = ?, started = ? WHERE lesson_id = ? AND agent = ?",
               (session_id, 1 if started else 0, lesson_id, agent))
    if not started:
        db.execute("UPDATE sessions SET seen_message = 0 WHERE lesson_id = ? AND agent = ?", (lesson_id, agent))
    if text_hash is not None:
        db.execute("UPDATE sessions SET text_hash = ? WHERE lesson_id = ? AND agent = ?", (text_hash, lesson_id, agent))
    if seen_message is not None:
        db.execute("UPDATE sessions SET seen_message = ? WHERE lesson_id = ? AND agent = ?", (seen_message, lesson_id, agent))


# --- the service's settings ---------------------------------------------------------------------

def setting(db, name, default=None):
    row = db.one("SELECT value FROM settings WHERE name = ?", (name,))
    return row["value"] if row else default


def set_setting(db, name, value):
    if value is None:
        db.execute("DELETE FROM settings WHERE name = ?", (name,))
    else:
        db.execute("INSERT INTO settings (name, value) VALUES (?, ?) ON CONFLICT (name) DO UPDATE SET value = excluded.value", (name, value))


def default_agent(db):
    agent = setting(db, "default_agent")
    return agent if agent in AGENTS else DEFAULT_AGENT


def answered_by(db, lesson_id):
    """The agent that answered the lesson last (a run that ended well, or wrote something), or
    None before its first answer."""
    row = db.one("SELECT agent FROM runs WHERE lesson_id = ? AND (status = 'ok' OR EXISTS "
                 "(SELECT 1 FROM messages WHERE messages.run_id = runs.id AND messages.role = 'assistant')) ORDER BY id DESC LIMIT 1",
                 (lesson_id,))
    return row["agent"] if row and row["agent"] in AGENTS else None


def agent_choice(db, row):
    """(the agent that teaches a lesson, why): its own choice ("lesson"); else, once it has
    been answered, the agent that answered it last ("begun": a change of the default agent does
    not move a lesson under way); else the default one ("default")."""
    if row["agent"] in AGENTS:
        return row["agent"], "lesson"
    begun = answered_by(db, row["id"])
    return (begun, "begun") if begun else (default_agent(db), "default")


def agent_of(db, row):
    return agent_choice(db, row)[0]


def done_lessons(db, project, created):
    """The project's lessons that are done, in the order they were closed."""
    return db.all("SELECT * FROM lessons WHERE project = ? AND created = ? AND state = 'done' ORDER BY closed_at, id", (project, created))


# --- the plan -------------------------------------------------------------------------------

def modules(db, project, created):
    return db.all("SELECT * FROM modules WHERE project = ? AND created = ? ORDER BY position, id", (project, created))


def has_plan(db, project, created):
    return db.one("SELECT 1 FROM modules WHERE project = ? AND created = ? LIMIT 1", (project, created)) is not None


def add_module(db, project, created, title, position):
    return db.execute("INSERT INTO modules (project, created, title, position) VALUES (?, ?, ?, ?)",
                      (project, created, title, position)).lastrowid


def plan_out(db, project, created, view):
    """The project's plan: Goals, the modules with their lessons in order, the lessons outside
    the plan (added by hand), the dropped ones, and the proposals still open. `view(row)` makes
    a lesson's dictionary (it knows which are running)."""
    rows = lessons(db, project, created)
    goals = next(row for row in rows if row["kind"] == "goals")
    out = {"goals": view(goals), "modules": [], "other": [], "dropped": [],
           "proposals": [proposal_out(row) for row in open_proposals(db, project, created)]}
    by_module = {}
    for row in modules(db, project, created):
        entry = {"id": row["id"], "title": row["title"], "position": row["position"], "lessons": []}
        out["modules"].append(entry)
        by_module[row["id"]] = entry
    for row in rows:
        if row["kind"] == "goals":
            continue
        if row["state"] == "dropped":
            out["dropped"].append(view(row))
        elif row["module_id"] in by_module:
            by_module[row["module_id"]]["lessons"].append(view(row))
        else:
            out["other"].append(view(row))
    return out


# --- proposals ------------------------------------------------------------------------------

def proposal_out(row):
    return {"id": row["id"], "kind": row["kind"], "lesson_id": row["lesson_id"], "run_id": row["run_id"], "message_id": row["message_id"],
            "state": row["state"], "proposed": json.loads(row["proposed_json"]),
            "approved": json.loads(row["approved_json"]) if row["approved_json"] else None, "changes": json.loads(row["changes_json"]),
            "created_at": row["created_at"], "decided_at": row["decided_at"]}


def proposal(db, proposal_id):
    return db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))


def add_proposal(db, lesson_id, run_id, kind, proposed):
    cursor = db.execute("INSERT INTO proposals (lesson_id, run_id, kind, proposed_json) VALUES (?, ?, ?, ?)",
                        (lesson_id, run_id, kind, json.dumps(proposed)))
    return proposal(db, cursor.lastrowid)


def decide_proposal(db, proposal_id, state, approved=None, changes=()):
    db.execute(f"UPDATE proposals SET state = ?, approved_json = ?, changes_json = ?, told = 0, decided_at = {NOW} WHERE id = ?",
               (state, json.dumps(approved) if approved is not None else None, json.dumps(list(changes)), proposal_id))
    return proposal(db, proposal_id)


def open_proposals(db, project, created):
    return db.all("SELECT proposals.* FROM proposals JOIN lessons ON lessons.id = proposals.lesson_id "
                  "WHERE lessons.project = ? AND lessons.created = ? AND proposals.state = 'open' ORDER BY proposals.id", (project, created))


def untold_outcomes(db, lesson_id):
    """The proposals of this conversation that the learner decided and its tutor was not told of yet."""
    return db.all("SELECT * FROM proposals WHERE lesson_id = ? AND state != 'open' AND told = 0 ORDER BY id", (lesson_id,))


def mark_told(db, proposal_ids):
    for proposal_id in proposal_ids:
        db.execute("UPDATE proposals SET told = 1 WHERE id = ?", (proposal_id,))


def link_proposals(db, proposal_ids, message_id):
    for proposal_id in proposal_ids:
        db.execute("UPDATE proposals SET message_id = ? WHERE id = ?", (message_id, proposal_id))


# --- messages -------------------------------------------------------------------------------

# A message with the agent of its run: the one that wrote an assistant's message.
MESSAGE_ROWS = "SELECT messages.*, runs.agent AS agent FROM messages LEFT JOIN runs ON runs.id = messages.run_id"


def message_out(row, proposals=(), cards=()):
    out = {"id": row["id"], "role": row["role"], "text": row["text"], "run_id": row["run_id"], "created_at": row["created_at"]}
    if row["role"] == "assistant":
        out["agent"] = row["agent"]
        out["tools"] = json.loads(row["tools_json"] or "[]")
        out["proposals"] = [proposal_out(p) for p in proposals]
        out["cards"] = [card_out(c) for c in cards]
    else:
        out["context"] = row["context"]
        out["composed"] = bool(row["composed"])
    return out


def messages(db, lesson_id):
    by_message, cards_of = {}, {}
    for row in db.all("SELECT * FROM proposals WHERE lesson_id = ? AND message_id IS NOT NULL ORDER BY id", (lesson_id,)):
        by_message.setdefault(row["message_id"], []).append(row)
    for row in db.all("SELECT * FROM cards WHERE lesson_id = ? AND message_id IS NOT NULL ORDER BY id", (lesson_id,)):
        cards_of.setdefault(row["message_id"], []).append(row)
    return [message_out(row, by_message.get(row["id"], ()), cards_of.get(row["id"], ()))
            for row in db.all(f"{MESSAGE_ROWS} WHERE messages.lesson_id = ? ORDER BY messages.id", (lesson_id,))]


def message(db, message_id):
    return message_out(db.one(f"{MESSAGE_ROWS} WHERE messages.id = ?", (message_id,)),
                       db.all("SELECT * FROM proposals WHERE message_id = ? ORDER BY id", (message_id,)),
                       db.all("SELECT * FROM cards WHERE message_id = ? ORDER BY id", (message_id,)))


def add_message(db, lesson_id, run_id, role, text, tools=(), context="", composed=False):
    cursor = db.execute("INSERT INTO messages (lesson_id, run_id, role, text, tools_json, context, composed) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (lesson_id, run_id, role, text, json.dumps(list(tools)), context, 1 if composed else 0))
    return cursor.lastrowid


# --- cards: exercises and quick questions (cards.py says what they mean) ----------------------

def card(db, card_id):
    return db.one("SELECT * FROM cards WHERE id = ?", (card_id,))


def card_out(row, reveal=False):
    """A card as the page gets it. A question's right answer and its explanation are left out
    until it is answered (`reveal` gives them, for the tutor)."""
    spec = json.loads(row["spec_json"])
    result = json.loads(row["result_json"]) if row["result_json"] else None
    out = {"id": row["id"], "kind": row["kind"], "lesson_id": row["lesson_id"], "run_id": row["run_id"], "message_id": row["message_id"],
           "state": row["state"], "created_at": row["created_at"], "updated_at": row["updated_at"]}
    if row["kind"] == "exercise":
        out.update(title=spec["title"], steps=spec["steps"], check=spec["check"], last_check=result)
    else:
        out.update(question=spec["question"], options=spec["options"], answer=result)
        if reveal or row["state"] == "answered":
            out.update(right_choice=spec["answer"], explanation=spec["explanation"])
    return out


def add_card(db, lesson_id, run_id, kind, spec):
    state = "unchecked" if kind == "exercise" else "open"
    cursor = db.execute("INSERT INTO cards (lesson_id, run_id, kind, spec_json, state) VALUES (?, ?, ?, ?, ?)",
                        (lesson_id, run_id, kind, json.dumps(spec, ensure_ascii=False), state))
    return card(db, cursor.lastrowid)


def set_card(db, card_id, state, result, told):
    db.execute(f"UPDATE cards SET state = ?, result_json = ?, told = ?, updated_at = {NOW} WHERE id = ?",
               (state, json.dumps(result, ensure_ascii=False), 1 if told else 0, card_id))
    return card(db, card_id)


def link_cards(db, card_ids, message_id):
    for card_id in card_ids:
        db.execute("UPDATE cards SET message_id = ? WHERE id = ?", (message_id, card_id))


def untold_cards(db, lesson_id):
    return db.all("SELECT * FROM cards WHERE lesson_id = ? AND told = 0 ORDER BY updated_at, id", (lesson_id,))


def mark_cards_told(db, card_ids):
    for card_id in card_ids:
        db.execute("UPDATE cards SET told = 1 WHERE id = ?", (card_id,))


def set_message(db, message_id, text, tools):
    db.execute("UPDATE messages SET text = ?, tools_json = ? WHERE id = ?", (text, json.dumps(list(tools)), message_id))


UNFINISHED = "(not finished: the answer ended while this was running)"


def mark_unfinished(db, run_id):
    """The tool lines of a run that never got a result, marked `unfinished`: the run ended (it
    was stopped, failed, or the service stopped) while they ran."""
    for row in db.all("SELECT id, tools_json FROM messages WHERE run_id = ? AND role = 'assistant'", (run_id,)):
        tools = json.loads(row["tools_json"] or "[]")
        if any(t.get("ok") is None and not t.get("unfinished") for t in tools):
            tools = [dict(t, unfinished=True, output=t.get("output") or UNFINISHED) if t.get("ok") is None else t for t in tools]
            db.execute("UPDATE messages SET tools_json = ? WHERE id = ?", (json.dumps(tools), row["id"]))


def plan_seen(db, lesson_id):
    row = db.one("SELECT plan_seen FROM lessons WHERE id = ?", (lesson_id,))
    return json.loads(row["plan_seen"]) if row and row["plan_seen"] else None


def set_plan_seen(db, lesson_id, snapshot):
    db.execute("UPDATE lessons SET plan_seen = ? WHERE id = ?", (json.dumps(snapshot, ensure_ascii=False), lesson_id))


def set_context(db, message_id, context):
    db.execute("UPDATE messages SET context = ? WHERE id = ?", (context, message_id))


# --- runs (gaggiclanker's ChatRunsRepository) -------------------------------------------------

def run_out(row):
    return {"id": row["id"], "lesson_id": row["lesson_id"], "agent": row["agent"], "status": row["status"], "error": row["error"],
            "started_at": row["started_at"], "finished_at": row["finished_at"]}


def run(db, run_id):
    return db.one("SELECT * FROM runs WHERE id = ?", (run_id,))


def start_run(db, lesson_id, agent):
    cursor = db.execute("INSERT INTO runs (lesson_id, agent) VALUES (?, ?)", (lesson_id, agent))
    return run(db, cursor.lastrowid)


def finish_run(db, run_id, status, error="", usage=None):
    db.execute(f"UPDATE runs SET status = ?, error = ?, usage_json = ?, finished_at = {NOW} WHERE id = ?",
               (status, error, json.dumps(usage) if usage else None, run_id))
    return run(db, run_id)


def latest_usage(db, lesson_id):
    row = db.one("SELECT usage_json FROM runs WHERE lesson_id = ? AND status != 'running' AND usage_json IS NOT NULL ORDER BY id DESC LIMIT 1",
                 (lesson_id,))
    return json.loads(row["usage_json"]) if row else None


def ended_runs(db, lesson_id):
    """The runs of a lesson that did not end well (stopped, failed, interrupted), for the
    conversation to say so where they stand."""
    rows = db.all("SELECT id, agent, status, error FROM runs WHERE lesson_id = ? AND status IN ('cancelled', 'failed', 'interrupted') ORDER BY id",
                  (lesson_id,))
    return [{"id": row["id"], "agent": row["agent"], "status": row["status"], "error": row["error"]} for row in rows]


INTERRUPTED = "The tutor service restarted while this answer was being written: send your message again to go on."
STOPPED = "The tutor service stopped while this answer was being written: send your message again to go on."


def reconcile_running(db):
    """Mark every `running` run interrupted, once, at start (gaggiclanker's rule: a run is only
    running while a process holds it, and none survives a restart). Each gets a last `error`
    event, so that a stream of it ends. Returns the runs, with their project, for the clean-up
    of what they left in a sandbox."""
    found = db.all("SELECT runs.id, lessons.project FROM runs JOIN lessons ON lessons.id = runs.lesson_id WHERE runs.status = 'running'")
    for row in found:
        append_event(db, row["id"], next_seq(db, row["id"]), "error", {"message": INTERRUPTED})
        db.execute(f"UPDATE runs SET status = 'interrupted', error = ?, finished_at = {NOW} WHERE id = ?", (INTERRUPTED, row["id"]))
        mark_unfinished(db, row["id"])
    return [(row["id"], row["project"]) for row in found]


# --- events (gaggiclanker's ChatEventsRepository) --------------------------------------------

def append_event(db, run_id, seq, kind, data):
    db.execute("INSERT INTO events (run_id, seq, kind, data) VALUES (?, ?, ?, ?)", (run_id, seq, kind, json.dumps(data, default=str)))


def next_seq(db, run_id):
    return (db.one("SELECT max(seq) FROM events WHERE run_id = ?", (run_id,))[0] or 0) + 1


def events_since(db, run_id, after=0):
    rows = db.all("SELECT seq, kind, data FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (run_id, after))
    return [{"seq": row["seq"], "kind": row["kind"], "data": json.loads(row["data"])} for row in rows]


# --- the audit of the "learn" tools (gaggiclanker's ToolCallsRepository) ---------------------

def record_tool_call(db, run_id, tool, permission, input_hash, duration_ms, status, error=""):
    db.execute("INSERT INTO tool_calls (run_id, tool, permission, input_hash, duration_ms, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (run_id, tool, permission, input_hash, duration_ms, status, error or None))


# --- the tutor's instructions (gaggiclanker's prompts) ---------------------------------------

def seed_instructions(db, shipped):
    """gaggiclanker's seeding rule, for the one text: no row, insert it; the shipped text
    unchanged, nothing; changed and the row unedited, take the new text; changed and the row
    edited, update the default only (the learner's text stands, and "back to the shipped
    text" converges on the new one). Returns what was done, for the log."""
    row = db.one("SELECT * FROM prompts WHERE name = ?", (INSTRUCTIONS,))
    if row is None:
        db.execute("INSERT INTO prompts (name, content, default_content) VALUES (?, ?, ?)", (INSTRUCTIONS, shipped, shipped))
        return "inserted"
    if row["default_content"] == shipped:
        return "unchanged"
    if row["content"] != row["default_content"]:
        db.execute("UPDATE prompts SET default_content = ? WHERE name = ?", (shipped, INSTRUCTIONS))
        return "default updated (the text in force is edited and stays)"
    db.execute(f"UPDATE prompts SET content = ?, default_content = ?, updated_at = {NOW} WHERE name = ?", (shipped, shipped, INSTRUCTIONS))
    return "updated to the shipped text"


def instructions(db):
    row = db.one("SELECT * FROM prompts WHERE name = ?", (INSTRUCTIONS,))
    return {"text": row["content"], "edited": row["content"] != row["default_content"], "updated_at": row["updated_at"]}


def set_instructions(db, text):
    db.execute(f"UPDATE prompts SET content = ?, updated_at = {NOW} WHERE name = ?", (text, INSTRUCTIONS))
    return instructions(db)


def reset_instructions(db):
    db.execute(f"UPDATE prompts SET content = default_content, updated_at = {NOW} WHERE name = ?", (INSTRUCTIONS,))
    return instructions(db)


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- a project to a file and back (the core's project_export / project_import) -----------------
# What travels: the modules and the lessons, each under a key of the file's own (its id where it
# was made), used only for the references inside the file; with the conversations, the lessons'
# states and summaries, their runs (what the conversation shows of them: no session, no token),
# messages (without the context the tutor was told: it holds the learner's brief), cards and
# proposals. Never sessions, tool calls, evidence, knowledge records or brief sets.

class ImportRefused(Exception):
    """The file's plan and lessons cannot be put in; the message says why."""


LESSON_STATES = ("not_started", "in_progress", "done", "dropped")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def project_export(db, project, created, conversations):
    """The project's plan and lessons, and with `conversations` what was said in them, as the
    file's project.json holds them."""
    rows = [row for row in lessons(db, project, created) if conversations or row["state"] != "dropped"]
    out = {"conversations": conversations,
           "modules": [{"key": row["id"], "title": row["title"], "position": row["position"]} for row in modules(db, project, created)],
           "lessons": []}
    for row in rows:
        entry = {"key": row["id"], "kind": row["kind"], "title": row["title"], "objective": row["objective"], "slug": row["slug"],
                 "position": row["position"], "module": row["module_id"], "agent": row["agent"]}
        if conversations:
            entry.update(state=row["state"], summary=row["summary"], one_line=row["one_line"], closed_at=row["closed_at"])
        out["lessons"].append(entry)
    if not conversations:
        return out
    of_project = "lesson_id IN (SELECT id FROM lessons WHERE project = ? AND created = ?)"
    pair = (project, created)
    out["runs"] = [{"key": r["id"], "lesson": r["lesson_id"], "status": r["status"], "error": r["error"], "agent": r["agent"],
                    "usage": json.loads(r["usage_json"]) if r["usage_json"] else None, "started_at": r["started_at"], "finished_at": r["finished_at"]}
                   for r in db.all(f"SELECT * FROM runs WHERE {of_project} ORDER BY id", pair)]
    out["messages"] = [{"key": r["id"], "lesson": r["lesson_id"], "run": r["run_id"], "role": r["role"], "text": r["text"],
                        "tools": json.loads(r["tools_json"] or "[]"), "composed": bool(r["composed"]), "created_at": r["created_at"]}
                       for r in db.all(f"SELECT * FROM messages WHERE {of_project} ORDER BY id", pair)]
    out["cards"] = [{"lesson": r["lesson_id"], "run": r["run_id"], "message": r["message_id"], "kind": r["kind"],
                     "spec": json.loads(r["spec_json"]), "state": r["state"], "result": json.loads(r["result_json"]) if r["result_json"] else None,
                     "created_at": r["created_at"], "updated_at": r["updated_at"]}
                    for r in db.all(f"SELECT * FROM cards WHERE {of_project} ORDER BY id", pair)]
    out["proposals"] = [{"lesson": r["lesson_id"], "run": r["run_id"], "message": r["message_id"], "kind": r["kind"],
                         "proposed": json.loads(r["proposed_json"]), "approved": json.loads(r["approved_json"]) if r["approved_json"] else None,
                         "changes": json.loads(r["changes_json"]), "state": r["state"], "created_at": r["created_at"], "decided_at": r["decided_at"]}
                        for r in db.all(f"SELECT * FROM proposals WHERE {of_project} ORDER BY id", pair)]
    return out


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _items(data, key, required=True):
    items = data.get(key, [] if not required else None)
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ImportRefused(f"The file's `{key}` is not a list of objects.")
    return items


def _text(item, key, limit=None, blank=True, what="an item"):
    value = item.get(key, "")
    if not isinstance(value, str) or (not blank and not value.strip()) or (limit and len(value) > limit):
        raise ImportRefused(f"The file has {what} whose `{key}` is not {'a text' if blank else 'a non-empty text'}"
                            + (f" of at most {limit} characters." if limit else "."))
    return value


def _time(item, key):
    value = item.get(key)
    return value if isinstance(value, str) and len(value) <= 64 else None


def _ref(item, key, known, what, optional=True):
    value = item.get(key)
    if value is None and optional:
        return None
    if not _is_int(value) or value not in known:
        raise ImportRefused(f"The file has {what} that refers to {key} {value!r}, which is not in the file.")
    return known[value]


def _keyed(items, what):
    keys = set()
    for item in items:
        if not _is_int(item.get("key")) or item["key"] in keys:
            raise ImportRefused(f"The file has {what} without a key of its own (a whole number, given once).")
        keys.add(item["key"])
    return sorted(items, key=lambda item: item["key"])


def _remap_plan(plan, lesson_ids):
    """A proposal's plan with the lessons it names (by the file's keys) named by their new ids."""
    if not isinstance(plan, dict):
        raise ImportRefused("The file has a proposal whose plan is not an object.")
    plan = json.loads(json.dumps(plan))
    places = [item for module in plan.get("modules", []) if isinstance(module, dict) for item in module.get("lessons", [])]
    places += plan.get("dropped", []) if isinstance(plan.get("dropped"), list) else []
    for item in places:
        if isinstance(item, dict) and item.get("id") is not None:
            item["id"] = lesson_ids.get(item["id"]) if _is_int(item["id"]) else None
    return plan


def project_import(db, project, created, data):
    """The file's plan and lessons (and conversations, when it has them) put into a new project,
    in one transaction, with new ids. Goals is the project's own Goals lesson. No session is
    written: a lesson that comes with its conversation is taken up from its files at its next
    message. Returns the lesson folders to rename in the workspace: [[the file's, the new]]."""
    if not isinstance(data, dict) or not isinstance(data.get("conversations"), bool):
        raise ImportRefused("The file's project.json is not the shape clanker-learn writes.")
    talk = data["conversations"]
    there = db.all("SELECT * FROM lessons WHERE project = ? AND created = ?", (project, created))
    if any(row["kind"] != "goals" or row["state"] != "not_started" or has_messages(db, row["id"]) for row in there) or has_plan(db, project, created):
        raise ImportRefused("This project has lessons already: a file is put only into a new project.")
    mods = _keyed(_items(data, "modules"), "a module")
    rows = _keyed(_items(data, "lessons"), "a lesson")
    if sum(1 for row in rows if row.get("kind") == "goals") > 1:
        raise ImportRefused("The file has more than one Goals lesson.")
    db.execute("BEGIN")
    try:
        module_ids, lesson_ids, run_ids, message_ids, folders = {}, {}, {}, {}, []
        for item in mods:
            if not _is_int(item.get("position")):
                raise ImportRefused("The file has a module without a position.")
            module_ids[item["key"]] = add_module(db, project, created, _text(item, "title", 200, False, "a module"), item["position"])
        goals = ensure_goals(db, project, created)
        for item in rows:
            kind, agent = item.get("kind"), item.get("agent")
            if kind not in ("lesson", "goals") or not _is_int(item.get("position")):
                raise ImportRefused("The file has a lesson that is not a lesson or Goals, or has no position.")
            title, objective = _text(item, "title", 200, False, "a lesson"), _text(item, "objective", 1000, what="a lesson")
            slug = item.get("slug") if isinstance(item.get("slug"), str) and SLUG.fullmatch(item["slug"]) else slug_of(title)
            state, summary, one_line, closed_at = "not_started", "", "", None
            if talk:
                state = item.get("state")
                if state not in LESSON_STATES:
                    raise ImportRefused(f"The file has a lesson whose state is {state!r}.")
                summary, one_line = _text(item, "summary", what="a lesson"), _text(item, "one_line", what="a lesson")
                closed_at = _time(item, "closed_at")
            values = (state, summary, one_line, closed_at, agent if talk and agent in AGENTS else None)   # without the conversations, no sharer's choice
            if kind == "goals":
                db.execute("UPDATE lessons SET state = ?, summary = ?, one_line = ?, closed_at = ?, agent = ? WHERE id = ?", (*values, goals["id"]))
                new, slug = goals["id"], goals["slug"]
            else:
                new = db.execute("INSERT INTO lessons (project, created, title, kind, objective, module_id, position, slug, state, summary, "
                                 "one_line, closed_at, agent) VALUES (?, ?, ?, 'lesson', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (project, created, title, objective, _ref(item, "module", module_ids, "a lesson"), item["position"], slug,
                                  *values)).lastrowid
            lesson_ids[item["key"]] = new
            old_slug = item.get("slug") if isinstance(item.get("slug"), str) and SLUG.fullmatch(item["slug"]) else None
            if old_slug:
                folders.append([f"{item['key']}-{old_slug}", f"{new}-{slug}"])
        if talk:
            for item in _keyed(_items(data, "runs"), "a run"):
                status = item.get("status") if item.get("status") in ("ok", "cancelled", "failed", "interrupted") else "interrupted"
                usage = item.get("usage") if isinstance(item.get("usage"), dict) else None
                run_ids[item["key"]] = db.execute(
                    f"INSERT INTO runs (lesson_id, status, error, usage_json, agent, started_at, finished_at) VALUES (?, ?, ?, ?, ?, coalesce(?, {NOW}), ?)",
                    (_ref(item, "lesson", lesson_ids, "a run", False), status, _text(item, "error", what="a run"),
                     json.dumps(usage) if usage else None, item.get("agent") if item.get("agent") in AGENTS else DEFAULT_AGENT,
                     _time(item, "started_at"), _time(item, "finished_at"))).lastrowid
            for item in _keyed(_items(data, "messages"), "a message"):
                tools = item.get("tools", [])
                if item.get("role") not in ("user", "assistant") or not isinstance(tools, list) or not all(
                        isinstance(t, dict) and all(isinstance(t.get(k), str) for k in ("kind", "label", "detail")) and t.get("ok") in (True, False, None)
                        for t in tools):
                    raise ImportRefused("The file has a message that is not the learner's or the tutor's, or whose tools are not a list.")
                message_ids[item["key"]] = db.execute(
                    f"INSERT INTO messages (lesson_id, run_id, role, text, tools_json, composed, created_at) VALUES (?, ?, ?, ?, ?, ?, coalesce(?, {NOW}))",
                    (_ref(item, "lesson", lesson_ids, "a message", False), _ref(item, "run", run_ids, "a message"), item["role"],
                     _text(item, "text", what="a message"), json.dumps(tools), 1 if item.get("composed") is True else 0,
                     _time(item, "created_at"))).lastrowid
            for item in _items(data, "cards"):
                spec, result, kind = item.get("spec"), item.get("result"), item.get("kind")
                needs = {"exercise": ("title", "steps", "check"), "question": ("question", "options", "answer", "explanation")}.get(kind)
                if not needs or not isinstance(spec, dict) or not all(key in spec for key in needs) or not (result is None or isinstance(result, dict)):
                    raise ImportRefused("The file has a card that is not an exercise or a question as clanker-learn makes them.")
                if result is not None:
                    result = dict(result, evidence=None)   # the evidence stays where it was made
                db.execute(f"INSERT INTO cards (lesson_id, run_id, message_id, kind, spec_json, state, result_json, told, created_at, updated_at) "
                           f"VALUES (?, ?, ?, ?, ?, ?, ?, 1, coalesce(?, {NOW}), coalesce(?, {NOW}))",
                           (_ref(item, "lesson", lesson_ids, "a card", False), _ref(item, "run", run_ids, "a card"),
                            _ref(item, "message", message_ids, "a card"), kind, json.dumps(spec, ensure_ascii=False),
                            _text(item, "state", 40, False, "a card"), json.dumps(result, ensure_ascii=False) if result is not None else None,
                            _time(item, "created_at"), _time(item, "updated_at")))
            for item in _items(data, "proposals"):
                changes = item.get("changes", [])
                if item.get("kind") not in ("plan", "change") or item.get("state") not in ("open", "approved", "dismissed") or not isinstance(changes, list):
                    raise ImportRefused("The file has a proposal that is not a plan or a change as clanker-learn makes them.")
                approved = item.get("approved")
                db.execute(f"INSERT INTO proposals (lesson_id, run_id, message_id, kind, proposed_json, approved_json, changes_json, state, told, "
                           f"created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, coalesce(?, {NOW}), ?)",
                           (_ref(item, "lesson", lesson_ids, "a proposal", False), _ref(item, "run", run_ids, "a proposal"),
                            _ref(item, "message", message_ids, "a proposal"), item["kind"], json.dumps(_remap_plan(item.get("proposed"), lesson_ids)),
                            None if approved is None else json.dumps(_remap_plan(approved, lesson_ids)), json.dumps(changes), item["state"],
                            _time(item, "created_at"), _time(item, "decided_at")))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return folders
