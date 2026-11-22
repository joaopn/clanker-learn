"""The tutor service's store: one SQLite file, its lessons, messages, runs and their events.

Lifted from gaggiclanker (the maintainer's project, AGPL like this one): the connection and its
pragmas from gaggiclanker/db/connection.py, the runs and the persisted event stream from the
generic classes of gaggiclanker/db/repos/chat.py (ChatRunsRepository, ChatEventsRepository,
reconcile_running among them). Adapted: the standard library's sqlite3, used from the event
loop's thread only (every statement here is small, and one writer is all there is), plain
functions over one Database instead of repository classes, and no models: rows become the
dictionaries the HTTP contract names.
"""
import json
import os
import sqlite3
import uuid

SCHEMA_VERSION = 1
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

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

    def connect(self):
        """Open the file, apply the pragmas, and make the schema in a new file. A file at
        another version is refused: there is no upgrade machinery yet."""
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
        elif version != SCHEMA_VERSION:
            conn.close()
            raise StoreError(f"The tutor's database {self.path} is at version {version}; this tutor service knows version {SCHEMA_VERSION} only. "
                             "Move the file away to start with an empty one, or run the version of clanker-learn that wrote it.")
        self.conn = conn
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

def lesson_out(row, running=None):
    return {"id": row["id"], "project": row["project"], "created": row["created"], "title": row["title"], "state": row["state"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "running": running}


def lessons(db, project, created):
    return db.all("SELECT * FROM lessons WHERE project = ? AND created = ? ORDER BY id", (project, created))


def lesson(db, lesson_id):
    return db.one("SELECT * FROM lessons WHERE id = ?", (lesson_id,))


def add_lesson(db, project, created, title):
    cursor = db.execute("INSERT INTO lessons (project, created, title, session_id) VALUES (?, ?, ?, ?)",
                        (project, created, title, str(uuid.uuid4())))
    return lesson(db, cursor.lastrowid)


def rename_lesson(db, lesson_id, title):
    db.execute(f"UPDATE lessons SET title = ?, updated_at = {NOW} WHERE id = ?", (title, lesson_id))
    return lesson(db, lesson_id)


def delete_lesson(db, lesson_id):
    db.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))


def touch_lesson(db, lesson_id):
    db.execute(f"UPDATE lessons SET updated_at = {NOW} WHERE id = ?", (lesson_id,))


def set_session(db, lesson_id, session_id, started):
    db.execute("UPDATE lessons SET session_id = ?, session_started = ? WHERE id = ?", (session_id, 1 if started else 0, lesson_id))


# --- messages -------------------------------------------------------------------------------

def message_out(row):
    out = {"id": row["id"], "role": row["role"], "text": row["text"], "run_id": row["run_id"], "created_at": row["created_at"]}
    if row["role"] == "assistant":
        out["tools"] = json.loads(row["tools_json"] or "[]")
    return out


def messages(db, lesson_id):
    return [message_out(row) for row in db.all("SELECT * FROM messages WHERE lesson_id = ? ORDER BY id", (lesson_id,))]


def add_message(db, lesson_id, run_id, role, text, tools=()):
    cursor = db.execute("INSERT INTO messages (lesson_id, run_id, role, text, tools_json) VALUES (?, ?, ?, ?, ?)",
                        (lesson_id, run_id, role, text, json.dumps(list(tools))))
    return message_out(db.one("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)))


# --- runs (gaggiclanker's ChatRunsRepository) -------------------------------------------------

def run_out(row):
    return {"id": row["id"], "lesson_id": row["lesson_id"], "status": row["status"], "error": row["error"],
            "started_at": row["started_at"], "finished_at": row["finished_at"]}


def run(db, run_id):
    return db.one("SELECT * FROM runs WHERE id = ?", (run_id,))


def start_run(db, lesson_id):
    cursor = db.execute("INSERT INTO runs (lesson_id) VALUES (?)", (lesson_id,))
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
    rows = db.all("SELECT id, status, error FROM runs WHERE lesson_id = ? AND status IN ('cancelled', 'failed', 'interrupted') ORDER BY id",
                  (lesson_id,))
    return [{"id": row["id"], "status": row["status"], "error": row["error"]} for row in rows]


INTERRUPTED = "The tutor service restarted while this answer was being written."


def reconcile_running(db):
    """Mark every `running` run interrupted, once, at start (gaggiclanker's rule: a run is only
    running while a process holds it, and none survives a restart). Each gets a last `error`
    event, so that a stream of it ends. Returns the runs, with their project, for the clean-up
    of what they left in a sandbox."""
    found = db.all("SELECT runs.id, lessons.project FROM runs JOIN lessons ON lessons.id = runs.lesson_id WHERE runs.status = 'running'")
    for row in found:
        append_event(db, row["id"], next_seq(db, row["id"]), "error", {"message": INTERRUPTED})
        db.execute(f"UPDATE runs SET status = 'interrupted', error = ?, finished_at = {NOW} WHERE id = ?", (INTERRUPTED, row["id"]))
    return [(row["id"], row["project"]) for row in found]


# --- events (gaggiclanker's ChatEventsRepository) --------------------------------------------

def append_event(db, run_id, seq, kind, data):
    db.execute("INSERT INTO events (run_id, seq, kind, data) VALUES (?, ?, ?, ?)", (run_id, seq, kind, json.dumps(data, default=str)))


def next_seq(db, run_id):
    return (db.one("SELECT max(seq) FROM events WHERE run_id = ?", (run_id,))[0] or 0) + 1


def events_since(db, run_id, after=0):
    rows = db.all("SELECT seq, kind, data FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (run_id, after))
    return [{"seq": row["seq"], "kind": row["kind"], "data": json.loads(row["data"])} for row in rows]
