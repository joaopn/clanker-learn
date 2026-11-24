"""The knowledge base: what every tutor is told about the learner, and what only one project's
tutors are told.

Records (goals, preferences, the background text, a project's insights, global insights), the
evidence the system itself saw (a check that ran, a quote, an answered question, a closed
lesson), the change log with before and after and its undo, the proposals of global insights,
the brief rendered from the records, the brief sets, and what is kept or forgotten of a destroyed
project. Goals, preferences and the background belong to a brief set, and each project uses one
set (Default when it was given none); everything else is global to the installation. A record
remembers the project (name and creation time) it came from, where it has one.

The rules are gaggiclanker's (the maintainer's project, AGPL like this one); its knowledge code
is tied to its own domain, so they are taken, not its files:
- the learner's own words are a quote, whitespace collapsed and case folded, of at least 12
  characters, found in a message they typed, a card's composed turn not counting
  (gaggiclanker/tools/builtin.py `_quote_problems`, QUOTE_MIN_CHARS and its two refusal
  sentences; gaggiclanker/db/repos/knowledge_insights.py `collapsed`;
  gaggiclanker/domain/automatic_turns.py);
- an insight rests on what it cites, and an agent changes one only by replacing it (the new one
  takes the old one out, linked) or by retiring it, never by editing it in place
  (record_insight's replaces_insight_id);
- an owner's insights are told only to that owner's conversations (InsightsRepository.for_recipe),
  and a general one only once a person has confirmed it (InsightsRepository.select reads
  confirmed = 1).
New here: the change log and undo, the brief's rendering, the brief sets, the word overlap that
finds similar insights.
"""
import contextlib
import json
import re

import store

QUOTE_MIN = 12
REASON_MAX = 1000
RECENT_MAX = 10
SET_NAME_MAX = 80
DEFAULT_SET = 1
SET_KINDS = ("goal", "preference", "background")   # what a brief set holds; the other kinds are global
SEARCH_MAX = 30
SIMILAR_MAX = 3
INSIGHT_KINDS = ("misconception", "strength", "worked", "did_not_work")
LINE_KINDS = ("goal", "preference", "global")   # what the learner adds as a line of its own
LEARNER = {"who": "learner"}
EMPTY_RESTS = {"evidence": [], "insights": []}

QUOTE_TOO_SHORT = (f"Quote the learner's own words exactly, at least {QUOTE_MIN} characters of them: a sentence they typed in this "
                   "project that says it.")
QUOTE_NOT_FOUND = ("Quote the learner's words exactly as they typed them in this project: a goal, a preference or a quote is saved "
                   "only from what they wrote, never from your own words or from an answer a card composed for them.")


class KnowledgeError(Exception):
    """A write that cannot be taken; the message says why, to a tutor or to a person."""


class Conflict(KnowledgeError):
    """An undo of a record that changed since."""


class Missing(KnowledgeError):
    """No record, change or proposal of that number."""


@contextlib.contextmanager
def atomic(db):
    """One transaction around several statements (the connection is in autocommit)."""
    if db.conn.in_transaction:
        yield
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def collapsed(text):
    """gaggiclanker's: how two sentences are compared for "the same words"."""
    return " ".join(text.split()).casefold()


def one_line(text):
    return " ".join(str(text).split())


def by_tutor(turn):
    return {"who": "tutor", "project": turn.project, "created": turn.created, "lesson_id": turn.lesson_id, "run_id": turn.run_id}


def line_of(text, what="A record", limit=None):
    if not isinstance(text, str) or not text.strip() or (limit is not None and len(text.strip()) > limit):
        raise KnowledgeError(f"{what} is 1 to {limit} characters." if limit is not None else f"{what} must not be empty.")
    return one_line(text)


# --- evidence ---------------------------------------------------------------------------------

def evidence_out(row):
    return {"id": row["id"], "kind": row["kind"], "project": row["project"], "created": row["created"],
            "project_removed": bool(row["project_removed"]), "lesson_id": row["lesson_id"], "run_id": row["run_id"],
            "data": json.loads(row["data_json"]), "at": row["at"]}


def evidence(db, evidence_id):
    return db.one("SELECT * FROM evidence WHERE id = ?", (evidence_id,))


def add_evidence(db, kind, project, created, lesson_id, run_id, data):
    return db.execute("INSERT INTO evidence (kind, project, created, lesson_id, run_id, data_json) VALUES (?, ?, ?, ?, ?, ?)",
                      (kind, project, created, lesson_id, run_id, json.dumps(data, ensure_ascii=False))).lastrowid


def find_quote(db, project, created, quote):
    """The newest message the learner typed in this project that holds `quote`; a composed one
    (a card's answer sent as a message) never counts. Raises with gaggiclanker's sentences."""
    wanted = collapsed(quote or "")
    if len(wanted) < QUOTE_MIN:
        raise KnowledgeError(QUOTE_TOO_SHORT)
    rows = db.all("SELECT messages.id, messages.text, messages.lesson_id FROM messages JOIN lessons ON lessons.id = messages.lesson_id "
                  "WHERE lessons.project = ? AND lessons.created = ? AND messages.role = 'user' AND messages.composed = 0 "
                  "ORDER BY messages.id DESC", (project, created))
    for row in rows:
        if wanted in collapsed(row["text"]):
            return row
    raise KnowledgeError(QUOTE_NOT_FOUND)


def quote_evidence(db, turn, quote):
    row = find_quote(db, turn.project, turn.created, quote)
    return add_evidence(db, "quote", turn.project, turn.created, row["lesson_id"], turn.run_id,
                        {"sentence": one_line(quote), "message_id": row["id"]})


def rests_on_of(db, turn, given, cite=("evidence",)):
    """What a tutor's record rests on, checked against the token's project: evidence ids of
    this project (or, for a global proposal, active insights of this project) and/or a quote,
    which becomes evidence of its own. The quote is looked at last, so that nothing is stored
    for a call that is refused."""
    given = given or {}
    out = {"evidence": [], "insights": []}
    for evidence_id in given.get("evidence") or []:
        row = evidence(db, evidence_id) if "evidence" in cite else None
        if row is None or (row["project"], row["created"]) != (turn.project, turn.created):
            raise KnowledgeError(f"There is no evidence {evidence_id} of this project: cite the ids that exercise_check, answer_grade, "
                                 "lesson_close or the learner's answers gave you.")
        out["evidence"].append(evidence_id)
    for insight_id in given.get("insights") or []:
        row = record(db, insight_id) if "insights" in cite else None
        if row is None or row["kind"] != "insight" or row["state"] != "active" or (row["project"], row["created"]) != (turn.project, turn.created):
            raise KnowledgeError(f"{insight_id} is not an insight of this project in force (brief_get lists them).")
        out["insights"].append(insight_id)
    if given.get("quote"):
        out["evidence"].append(quote_evidence(db, turn, given["quote"]))
    if not out["evidence"] and not out["insights"]:
        what = "this project's insights (`insights`) or a quote" if "insights" in cite else "evidence ids (`evidence`) or a quote"
        raise KnowledgeError(f"`rests_on` must cite {what}: what the record rests on.")
    return out


# --- records ----------------------------------------------------------------------------------

def record(db, record_id):
    return db.one("SELECT * FROM records WHERE id = ?", (record_id,))


def rests_detail(db, rests_on):
    """What a record or a proposal rests on, in full: the evidence and the insights it cites."""
    found = [evidence(db, i) for i in rests_on.get("evidence", [])]
    cited = [record(db, i) for i in rests_on.get("insights", [])]
    return {"evidence": [evidence_out(row) for row in found if row is not None],
            "insights": [record_out(row) for row in cited if row is not None]}


def snapshot(row):
    return dict(row) if row is not None else None


def record_out(row):
    return {"id": row["id"], "kind": row["kind"], "text": row["text"], "insight_kind": row["insight_kind"], "project": row["project"],
            "created": row["created"], "project_removed": bool(row["project_removed"]), "rests_on": json.loads(row["rests_on_json"]),
            "state": row["state"], "replaces": row["replaces"], "replaced_by": row["replaced_by"], "reason": row["reason"],
            "by": json.loads(row["by_json"]), "created_at": row["created_at"], "updated_at": row["updated_at"], "set_id": row["set_id"]}


def records(db, kind, project=None, created=None, include_retired=False, set_id=None):
    """A kind's records; a goal's, a preference's or the background's of one brief set
    (`set_id`, Default when it is not given)."""
    sql, params = "SELECT * FROM records WHERE kind = ?", [kind]
    if kind in SET_KINDS:
        sql += " AND set_id = ?"
        params.append(DEFAULT_SET if set_id is None else set_id)
    if project is not None:
        sql += " AND project = ? AND created = ?"
        params += [project, created]
    if not include_retired:
        sql += " AND state = 'active'"
    return db.all(sql + " ORDER BY id", params)


def insert_record(db, kind, text, by, project=None, created=None, insight_kind=None, rests_on=None, replaces=None, set_id=None):
    if kind in SET_KINDS and set_id is None:
        set_id = DEFAULT_SET
    return db.execute("INSERT INTO records (kind, text, insight_kind, project, created, rests_on_json, replaces, by_json, set_id) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (kind, text, insight_kind, project, created, json.dumps(rests_on or EMPTY_RESTS), replaces, json.dumps(by),
                       set_id if kind in SET_KINDS else None)).lastrowid


def log_change(db, action, by, items, note="", undoes=None):
    """One entry of the change log: (record id, before, after) for each record it touched."""
    payload = [{"record": record_id, "before": before, "after": after} for record_id, before, after in items]
    return db.execute("INSERT INTO changes (action, by_json, items_json, note, undoes) VALUES (?, ?, ?, ?, ?)",
                      (action, json.dumps(by), json.dumps(payload, ensure_ascii=False), note, undoes)).lastrowid


def add(db, kind, text, by, action="add", note="", **fields):
    """A new record, logged. Returns its row."""
    with atomic(db):
        record_id = insert_record(db, kind, text, by, **fields)
        row = record(db, record_id)
        log_change(db, action, by, [(record_id, None, snapshot(row))], note)
    return row


def learner_add(db, kind, text, set_id=None):
    """A line of the learner's: a goal or a preference into a brief set (Default when none is
    given), a global insight into no set."""
    if kind not in LINE_KINDS:
        raise KnowledgeError(f"The learner adds a {', a '.join(LINE_KINDS)} here (the background has its own route).")
    if kind in SET_KINDS:
        set_id = brief_set_or_refuse(db, DEFAULT_SET if set_id is None else set_id)["id"]
    return add(db, kind, line_of(text), LEARNER, set_id=set_id)


def edit(db, record_id, by, text=None, insight_kind=None):
    """The learner's edit of a record in place (a tutor never edits one)."""
    with atomic(db):
        before = record_or_refuse(db, record_id)
        if text is not None:
            text = text.strip() if before["kind"] == "background" else one_line(text)
            if not text:
                raise KnowledgeError("The text must not be empty.")
            db.execute(f"UPDATE records SET text = ?, updated_at = {store.NOW} WHERE id = ?", (text, record_id))
        if insight_kind is not None:
            if insight_kind not in INSIGHT_KINDS:
                raise KnowledgeError(f"An insight's kind is one of {', '.join(INSIGHT_KINDS)}.")
            db.execute(f"UPDATE records SET insight_kind = ?, updated_at = {store.NOW} WHERE id = ?", (insight_kind, record_id))
        after = record(db, record_id)
        log_change(db, "edit", by, [(record_id, snapshot(before), snapshot(after))])
    return after


def delete(db, record_id, by):
    with atomic(db):
        before = record_or_refuse(db, record_id)
        db.execute("DELETE FROM records WHERE id = ?", (record_id,))
        log_change(db, "delete", by, [(record_id, snapshot(before), None)])
    return before


def record_or_refuse(db, record_id):
    row = record(db, record_id)
    if row is None:
        raise Missing(f"There is no record {record_id}.")
    return row


def set_background(db, text, set_id=None):
    """A brief set's background text (one per set; Default when no set is given), written by
    the learner only, of any length; empty removes it."""
    if not isinstance(text, str):
        raise KnowledgeError("The background must be text.")
    set_id = brief_set_or_refuse(db, DEFAULT_SET if set_id is None else set_id)["id"]
    current = records(db, "background", set_id=set_id)
    if not text.strip():
        if current:
            delete(db, current[0]["id"], LEARNER)
        return None
    if current:
        return edit(db, current[0]["id"], LEARNER, text=text)
    return add(db, "background", text.strip(), LEARNER, set_id=set_id)


def replace(db, old_id, text, by, rests_on, insight_kind=None, kind="insight", note="", source=None):
    """The old record retired, a new one added that names it: one change, undone as one. The new
    one comes from `source` (a project's name and creation time), or from where the old one did."""
    with atomic(db):
        old = record(db, old_id)
        project, created = source or (old["project"], old["created"])
        new_id = insert_record(db, kind, text, by, project, created, insight_kind or old["insight_kind"], rests_on, old_id, old["set_id"])
        db.execute(f"UPDATE records SET state = 'retired', replaced_by = ?, updated_at = {store.NOW} WHERE id = ?", (new_id, old_id))
        log_change(db, "replace", by, [(old_id, snapshot(old), snapshot(record(db, old_id))), (new_id, None, snapshot(record(db, new_id)))], note)
    return record(db, new_id)


def retire(db, record_id, by, reason, note=""):
    with atomic(db):
        old = record(db, record_id)
        db.execute(f"UPDATE records SET state = 'retired', reason = ?, updated_at = {store.NOW} WHERE id = ?", (reason, record_id))
        log_change(db, "retire", by, [(record_id, snapshot(old), snapshot(record(db, record_id)))], note)
    return record(db, record_id)


def set_goal_state(db, record_id, action, was, now):
    """A goal put aside (`archived`: out of the brief and the search, kept on the Knowledge page)
    or brought back, by the learner only; one change, undone as any other."""
    with atomic(db):
        old = record_or_refuse(db, record_id)
        if old["kind"] != "goal":
            raise KnowledgeError("Only a goal can be archived or restored.")
        if old["state"] != was:
            raise KnowledgeError(f"This goal is {old['state']}, not {was}: there is nothing to {action}.")
        db.execute(f"UPDATE records SET state = ?, updated_at = {store.NOW} WHERE id = ?", (now, record_id))
        log_change(db, action, LEARNER, [(record_id, snapshot(old), snapshot(record(db, record_id)))])
    return record(db, record_id)


def archive(db, record_id):
    return set_goal_state(db, record_id, "archive", "active", "archived")


def restore(db, record_id):
    return set_goal_state(db, record_id, "restore", "archived", "active")


def own_insight(db, turn, insight_id):
    """An insight a tutor may change: of the token's project, and in force."""
    row = record(db, insight_id)
    if row is None or row["kind"] != "insight" or (row["project"], row["created"]) != (turn.project, turn.created):
        raise KnowledgeError(f"There is no insight {insight_id} of this project (brief_get lists them).")
    if row["state"] != "active":
        raise KnowledgeError(f"The insight {insight_id} is retired already" + (f" (replaced by {row['replaced_by']})." if row["replaced_by"] else "."))
    return row


# --- the change log and undo --------------------------------------------------------------------

def same_record(now, then):
    """A record as it is now against a snapshot of it in the change log; a snapshot taken before
    a column was added (brief sets' `set_id`) is compared on the columns it has."""
    if now is None or then is None:
        return now is then
    return all(now.get(column) == value for column, value in then.items())


def change_out(row):
    items = json.loads(row["items_json"])
    by = json.loads(row["by_json"])
    kinds = sorted({(item["after"] or item["before"])["kind"] for item in items if item["after"] or item["before"]})
    return {"id": row["id"], "action": row["action"], "kinds": kinds, "records": [item["record"] for item in items], "by": by,
            "items": items, "note": row["note"], "undoes": row["undoes"], "at": row["at"]}


def changes(db, before=None, limit=50):
    """Newest first; `before` is the id to continue below (the last page's `next`)."""
    rows = db.all("SELECT * FROM changes WHERE id < ? ORDER BY id DESC LIMIT ?", (before or 1 << 62, limit + 1))
    undone = {row["undoes"] for row in db.all("SELECT undoes FROM changes WHERE undoes IS NOT NULL")}
    out = [dict(change_out(row), undone=row["id"] in undone) for row in rows[:limit]]
    return out, (rows[limit - 1]["id"] if len(rows) > limit else None)


def undo(db, change_id):
    """The `before` of one change applied as a new change, by the learner; refused when any of
    its records changed since (its state now is not that change's `after`)."""
    row = db.one("SELECT * FROM changes WHERE id = ?", (change_id,))
    if row is None:
        raise Missing(f"There is no change {change_id}.")
    items = json.loads(row["items_json"])
    with atomic(db):
        for item in items:
            if not same_record(snapshot(record(db, item["record"])), item["after"]):
                raise Conflict(f"Record {item['record']} changed after this change: undo the later changes first, or edit it by hand.")
        done = []
        for item in items:
            now = snapshot(record(db, item["record"]))
            if item["before"] is None:
                db.execute("DELETE FROM records WHERE id = ?", (item["record"],))
            else:
                before = dict(item["before"])
                if "set_id" not in before:   # a change logged before brief sets: its goals and the like were Default's
                    before["set_id"] = DEFAULT_SET if before["kind"] in SET_KINDS else None
                if before["set_id"] is not None and brief_set(db, before["set_id"]) is None:
                    raise Conflict(f"Record {item['record']} was in a brief set that is deleted.")
                columns = list(before)
                db.execute(f"INSERT OR REPLACE INTO records ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                           [before[c] for c in columns])
            done.append((item["record"], now, snapshot(record(db, item["record"]))))
        new_id = log_change(db, "undo", LEARNER, done, f"undid change {change_id} ({row['action']})", change_id)
    return change_out(db.one("SELECT * FROM changes WHERE id = ?", (new_id,)))


# --- lesson summaries -------------------------------------------------------------------------

def summary_out(row):
    return {"lesson_id": row["id"], "project": row["project"], "created": row["created"], "project_removed": bool(row["project_removed"]),
            "title": row["title"], "one_line": row["one_line"], "summary": row["summary"], "closed_at": row["closed_at"]}


def summaries(db, query=None, limit=None, set_id=None):
    """Every project's lessons that are done, newest first (with `set_id`, only those of the
    projects that use that brief set); `query` keeps those whose title, one line or summary
    hold every word of it."""
    sql, params = "SELECT * FROM lessons WHERE state = 'done' AND kind = 'lesson'", []
    if set_id is not None:
        sql += f" AND {SET_OF_LESSON} = ?"
        params.append(set_id)
    rows = db.all(sql + " ORDER BY closed_at DESC, id DESC", params)
    if query:
        rows = [row for row in rows if matches(query, row["title"], row["one_line"], row["summary"])]
    return rows[:limit] if limit else rows


def matches(query, *texts):
    words = collapsed(query).split()
    text = collapsed(" ".join(t or "" for t in texts))
    return bool(words) and all(word in text for word in words)


# --- the brief ----------------------------------------------------------------------------------

def where(row):
    removed = " (removed)" if row["project_removed"] else ""
    return f"project “{row['project']}”{removed}"


def insight_line(row, with_id=False):
    kind = f"({row['insight_kind'].replace('_', ' ')}) " if row["insight_kind"] else ""
    number = f"[{row['id']}] " if with_id else ""
    return f"- {number}{kind}{row['text']}"


def brief(db, set_id=DEFAULT_SET):
    """A brief set's brief, rendered by code from the records, the same records always giving the
    same text: the set's goals, preferences and background, the global insights (every set's),
    and the last RECENT_MAX lessons done in the projects that use the set. Never cut: the rest
    of the lessons is found by records_search. Returns (text, cut, parts); `cut` is always 0,
    kept for the callers that show it."""
    the_set = brief_set_or_refuse(db, set_id)
    parts = {"goals": records(db, "goal", set_id=set_id), "preferences": records(db, "preference", set_id=set_id),
             "background": (records(db, "background", set_id=set_id) or [None])[0], "global_insights": records(db, "global"),
             "recently": summaries(db, limit=RECENT_MAX, set_id=set_id)}
    sections = (
        ("Their goals:", [f"- {row['text']}" for row in parts["goals"]], "none recorded yet"),
        ("How they want to be taught:", [f"- {row['text']}" for row in parts["preferences"]], "none recorded yet"),
        ("Their background, as they wrote it:", parts["background"]["text"].splitlines() if parts["background"] else [], "not written yet"),
        ("How they learn (insights they approved for every tutor):", [insight_line(row) for row in parts["global_insights"]], "none yet"),
        ("Recently (lessons done in the projects that use this brief, newest first):",
         [f"- {(row['closed_at'] or '')[:10]}, {where(row)}: “{row['title']}”: {row['one_line']}" for row in parts["recently"]],
         "no lesson done yet"),
    )
    out = [f"The learner's brief, from their brief set “{the_set['name']}” (every tutor of every project that uses this set is told the same):"]
    for title, lines, empty in sections:
        out += [f"{title} {empty}."] if not lines else [title, *lines]
    return "\n".join(out), 0, parts


def project_insights_text(db, project, created):
    rows = records(db, "insight", project, created)
    if not rows:
        return "This project's insights on how the learner learns: none yet."
    return ("This project's insights on how the learner learns (told only to this project's tutors; change one with insight_replace "
            "or insight_retire, by its number):\n" + "\n".join(insight_line(row, True) for row in rows))


def search(db, turn, query):
    """records_search: every project's lesson summaries, the goals, preferences and background
    of the brief set this project uses, the global insights, and THIS project's insights; never
    another project's insights, nor another set's records."""
    set_id = set_of(db, turn.project, turn.created)

    def found(rows, *fields):
        return [row for row in rows if matches(query, *(row[f] for f in fields))][:SEARCH_MAX]
    return {"lessons": [{k: v for k, v in summary_out(row).items() if k != "created"} for row in summaries(db, query, SEARCH_MAX)],
            "goals": [record_brief(r) for r in found(records(db, "goal", set_id=set_id), "text")],
            "preferences": [record_brief(r) for r in found(records(db, "preference", set_id=set_id), "text")],
            "background": [record_brief(r) for r in found(records(db, "background", set_id=set_id), "text")],
            "global_insights": [record_brief(r) for r in found(records(db, "global"), "text")],
            "this_project_insights": [record_brief(r) for r in found(records(db, "insight", turn.project, turn.created, True), "text")]}


def record_brief(row):
    """A record as a tutor is given it: no detail of another project's evidence."""
    out = {"id": row["id"], "text": row["text"]}
    if row["insight_kind"]:
        out["kind"] = row["insight_kind"]
    if row["state"] != "active":
        out["state"] = row["state"]
    return out


# --- global insights, proposed ------------------------------------------------------------------

WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset(("that", "this", "with", "when", "what", "they", "their", "them", "from", "have", "does", "into", "than", "then",
                  "very", "more", "most", "much", "only", "also", "just", "about", "after", "before", "learner", "learns", "learning",
                  "likes"))


def words(text):
    return {w for w in WORD.findall(text.casefold()) if len(w) > 3 and w not in STOP}


def similar(db, text, project, created):
    """Insights of the learner's OTHER projects that share words with `text` (two or more, or a
    quarter of them), best first: shown to the learner beside a proposal, never to a tutor."""
    mine, out = words(text or ""), []
    for row in db.all("SELECT * FROM records WHERE kind = 'insight' AND state = 'active' AND NOT (project = ? AND created = ?) ORDER BY id",
                      (project, created)):
        theirs = words(row["text"])
        shared = mine & theirs
        score = len(shared) / len(mine | theirs) if mine | theirs else 0
        if len(shared) >= 2 or score >= 0.25:
            out.append(dict(record_out(row), score=round(score, 2), shared=sorted(shared)))
    return sorted(out, key=lambda r: (-r["score"], r["id"]))[:SIMILAR_MAX]


def global_proposal(db, proposal_id):
    return db.one("SELECT * FROM global_proposals WHERE id = ?", (proposal_id,))


def global_proposal_out(db, row):
    target = record(db, row["target"]) if row["target"] else None
    return {"id": row["id"], "action": row["action"], "target": record_out(target) if target else None, "text": row["text"],
            "proposed_text": row["proposed_text"], "edited": row["text"] != row["proposed_text"], "reason": row["reason"],
            "rests_on": json.loads(row["rests_on_json"]), "project": row["project"], "created": row["created"],
            "project_removed": bool(row["project_removed"]), "lesson_id": row["lesson_id"], "run_id": row["run_id"], "state": row["state"],
            "record_id": row["record_id"], "created_at": row["created_at"], "decided_at": row["decided_at"],
            "similar": similar(db, row["text"] or (target["text"] if target else ""), row["project"], row["created"])}


def global_proposals(db, state="open"):
    if state == "all":
        return db.all("SELECT * FROM global_proposals ORDER BY id DESC")
    return db.all("SELECT * FROM global_proposals WHERE state = ? ORDER BY id DESC", (state,))


def open_global_count(db):
    return db.one("SELECT count(*) FROM global_proposals WHERE state = 'open'")[0]


def propose_global(db, turn, args):
    """A tutor's proposal of a global insight: a new one, a replacement or a retirement of one in
    force. Stored only; told to no tutor until the learner approves it."""
    target = args.get("replaces") if "replaces" in args else args.get("retires")
    if "replaces" in args and "retires" in args:
        raise KnowledgeError("Give `replaces` or `retires`, not both.")
    if target is not None:
        row = record(db, target)
        if row is None or row["kind"] != "global" or row["state"] != "active":
            raise KnowledgeError(f"{target} is not a global insight in force (the brief lists them; brief_get gives it).")
    if "retires" in args:
        action, text = "retire", None
        reason = line_of(args.get("reason"), "`reason`", REASON_MAX)
        rests_on = EMPTY_RESTS
    else:
        action = "replace" if "replaces" in args else "add"
        text = line_of(args.get("text"), "`text`")
        reason = one_line(args.get("reason") or "")
        rests_on = rests_on_of(db, turn, args.get("rests_on"), cite=("insights",))
    cursor = db.execute("INSERT INTO global_proposals (action, target, text, proposed_text, reason, rests_on_json, project, created, lesson_id, "
                        "run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (action, target, text, text, reason, json.dumps(rests_on), turn.project, turn.created, turn.lesson_id, turn.run_id))
    return global_proposal(db, cursor.lastrowid)


def open_proposal_or_refuse(db, proposal_id):
    row = global_proposal(db, proposal_id)
    if row is None:
        raise Missing(f"There is no proposal {proposal_id}.")
    if row["state"] != "open":
        raise KnowledgeError(f"This proposal was {row['state']} already.")
    return row


def edit_global_proposal(db, proposal_id, text=None, reason=None):
    row = open_proposal_or_refuse(db, proposal_id)
    if text is not None:
        if row["action"] == "retire":
            raise KnowledgeError("A proposed retirement has no text; edit its `reason`.")
        db.execute("UPDATE global_proposals SET text = ? WHERE id = ?", (line_of(text, "The insight"), proposal_id))
    if reason is not None:
        db.execute("UPDATE global_proposals SET reason = ? WHERE id = ?", (one_line(reason)[:REASON_MAX], proposal_id))
    return global_proposal(db, proposal_id)


def approve_global(db, proposal_id, text=None):
    """The proposal (as the learner edited it, or with `text` now) applied by the learner: a
    global insight added, one replaced, or one retired; logged as the learner's."""
    with atomic(db):
        row = open_proposal_or_refuse(db, proposal_id)
        if text is not None:
            row = edit_global_proposal(db, proposal_id, text=text)
        note = f"approved global proposal {proposal_id} from {where(row)}"
        rests_on = json.loads(row["rests_on_json"])
        if row["action"] == "add":
            made = add(db, "global", row["text"], LEARNER, "approve", note, project=row["project"], created=row["created"], rests_on=rests_on)
        else:
            target = record(db, row["target"])
            if target is None or target["state"] != "active":
                raise KnowledgeError(f"The global insight {row['target']} is not in force any more: dismiss this proposal.")
            if row["action"] == "replace":
                # The replacement rests on the proposing project, not on where the old one came from.
                made = replace(db, row["target"], row["text"], LEARNER, rests_on, kind="global", note=note,
                               source=(row["project"], row["created"]))
            else:
                made = retire(db, row["target"], LEARNER, row["reason"], note)
        db.execute(f"UPDATE global_proposals SET state = 'approved', record_id = ?, told = 0, decided_at = {store.NOW} WHERE id = ?",
                   (made["id"], proposal_id))
    return global_proposal(db, proposal_id), record(db, made["id"])


def dismiss_global(db, proposal_id):
    open_proposal_or_refuse(db, proposal_id)
    db.execute(f"UPDATE global_proposals SET state = 'dismissed', told = 0, decided_at = {store.NOW} WHERE id = ?", (proposal_id,))
    return global_proposal(db, proposal_id)


def untold_global(db, lesson_id):
    return db.all("SELECT * FROM global_proposals WHERE lesson_id = ? AND state != 'open' AND told = 0 ORDER BY id", (lesson_id,))


def mark_global_told(db, ids):
    for proposal_id in ids:
        db.execute("UPDATE global_proposals SET told = 1 WHERE id = ?", (proposal_id,))


def global_outcome_note(row):
    what = {"add": "global insight", "replace": "replacement of a global insight", "retire": "retirement of a global insight"}[row["action"]]
    if row["state"] == "dismissed":
        return f"[clanker-learn: the learner dismissed your proposed {what} (global proposal {row['id']}); nothing was changed.]"
    if row["action"] == "retire":
        return f"[clanker-learn: the learner approved your proposed {what} (global proposal {row['id']}): it is told to no tutor any more.]"
    edited = (f" with their wording: “{row['text']}”" if row["text"] != row["proposed_text"] else " as you proposed it")
    return (f"[clanker-learn: the learner approved your proposed {what} (global proposal {row['id']}){edited}. Every tutor of every "
            "project is told it now.]")


# --- a destroyed project ------------------------------------------------------------------------

def project_removed(db, project, created, forget):
    """What a destroyed project leaves. Kept (`forget` false): its records, evidence and
    proposals marked as from a removed project, its lessons that are done kept for their
    summaries (their conversations removed), the others removed. Forgotten: its insights, lesson
    summaries, the goals and preferences that rest on its messages, its evidence, the global
    insights that came from it (each rests only on its own project), its proposals, and the
    change log's entries about any of them. Its lessons and conversations go either way."""
    pair = (project, created)
    with atomic(db):
        lesson_ids = [row["id"] for row in db.all("SELECT id FROM lessons WHERE project = ? AND created = ?", pair)]
        # The audit of the tools names runs by id only; the runs go, and their ids can be given again.
        db.execute("DELETE FROM tool_calls WHERE run_id IN (SELECT runs.id FROM runs JOIN lessons ON lessons.id = runs.lesson_id "
                   "WHERE lessons.project = ? AND lessons.created = ?)", pair)
        if forget:
            gone = {row["id"] for row in db.all("SELECT id FROM records WHERE project = ? AND created = ?", pair)}
            counts = {"records": len(gone),
                      "evidence": db.execute("DELETE FROM evidence WHERE project = ? AND created = ?", pair).rowcount,
                      "global_proposals": db.execute("DELETE FROM global_proposals WHERE project = ? AND created = ?", pair).rowcount}
            db.execute("DELETE FROM records WHERE project = ? AND created = ?", pair)
            dropped = 0
            for row in db.all("SELECT id, items_json FROM changes"):
                if any(item["record"] in gone for item in json.loads(row["items_json"])):
                    db.execute("DELETE FROM changes WHERE id = ?", (row["id"],))
                    dropped += 1
            counts["changes"] = dropped
            counts["lessons"] = db.execute("DELETE FROM lessons WHERE project = ? AND created = ?", pair).rowcount
        else:
            counts = {table: db.execute(f"UPDATE {table} SET project_removed = 1 WHERE project = ? AND created = ?", pair).rowcount
                      for table in ("records", "evidence", "global_proposals")}
            counts["lessons"] = db.execute("DELETE FROM lessons WHERE project = ? AND created = ? AND NOT (state = 'done' AND kind = 'lesson')",
                                           pair).rowcount
            kept = [row["id"] for row in db.all("SELECT id FROM lessons WHERE project = ? AND created = ?", pair)]
            for lesson_id in kept:
                db.execute("DELETE FROM messages WHERE lesson_id = ?", (lesson_id,))
                db.execute("DELETE FROM runs WHERE lesson_id = ?", (lesson_id,))
                db.execute("DELETE FROM proposals WHERE lesson_id = ?", (lesson_id,))
                db.execute("DELETE FROM cards WHERE lesson_id = ?", (lesson_id,))
            db.execute("UPDATE lessons SET project_removed = 1, module_id = NULL WHERE project = ? AND created = ?", pair)
            counts["summaries_kept"] = len(kept)
        db.execute("DELETE FROM modules WHERE project = ? AND created = ?", pair)
        # Its brief set stays named while its done lessons are kept (they stay in that set's
        # Recently); the set's count of projects leaves it out (set_users).
        if forget or not counts.get("summaries_kept"):
            db.execute("DELETE FROM project_sets WHERE project = ? AND created = ?", pair)
    counts["lessons_before"] = len(lesson_ids)
    return counts


# --- brief sets ---------------------------------------------------------------------------------
# A set holds goals, preferences and a background (records.set_id); each project uses one
# (project_sets; Default, set 1, when it has no row there). The global insights are every set's.

SET_OF_LESSON = (f"coalesce((SELECT set_id FROM project_sets WHERE project_sets.project = lessons.project "
                 f"AND project_sets.created = lessons.created), {DEFAULT_SET})")


def brief_set(db, set_id):
    return db.one("SELECT * FROM brief_sets WHERE id = ?", (set_id,))


def brief_set_or_refuse(db, set_id):
    row = brief_set(db, set_id) if isinstance(set_id, int) and not isinstance(set_id, bool) else None
    if row is None:
        raise Missing(f"There is no brief set {set_id}.")
    return row


def set_users(db, set_id):
    """The projects given this set, as (name, created) rows; a removed project whose lessons are
    kept (its lesson rows carry project_removed) is not one."""
    return db.all("SELECT project, created FROM project_sets WHERE set_id = ? AND NOT EXISTS (SELECT 1 FROM lessons WHERE "
                  "lessons.project = project_sets.project AND lessons.created = project_sets.created AND lessons.project_removed = 1) "
                  "ORDER BY project", (set_id,))


def brief_set_out(db, row):
    return {"id": row["id"], "name": row["name"], "default": row["id"] == DEFAULT_SET, "created_at": row["created_at"],
            "projects": len(set_users(db, row["id"]))}


def brief_sets(db):
    return db.all("SELECT * FROM brief_sets ORDER BY id")


def set_name_of(db, name, but=None):
    if not isinstance(name, str) or not name.strip() or len(one_line(name)) > SET_NAME_MAX:
        raise KnowledgeError(f"A brief set's name is 1 to {SET_NAME_MAX} characters.")
    name = one_line(name)
    same = db.one("SELECT id FROM brief_sets WHERE name = ? COLLATE NOCASE", (name,))
    if same is not None and same["id"] != but:
        raise KnowledgeError(f"There is a brief set called “{name}” already.")
    return name


def create_set(db, name, copy_from=None):
    """A new brief set: empty, or with a copy of another set's goals, preferences and
    background (each copy logged as an addition, so the History can undo it)."""
    with atomic(db):
        source = brief_set_or_refuse(db, copy_from) if copy_from is not None else None
        set_id = db.execute("INSERT INTO brief_sets (name) VALUES (?)", (set_name_of(db, name),)).lastrowid
        if source is not None:
            for kind in SET_KINDS:
                for row in records(db, kind, set_id=source["id"]):
                    add(db, kind, row["text"], json.loads(row["by_json"]), note=f"copied from the brief set “{source['name']}”",
                        project=row["project"], created=row["created"], rests_on=json.loads(row["rests_on_json"]), set_id=set_id)
    return brief_set(db, set_id)


def rename_set(db, set_id, name):
    row = brief_set_or_refuse(db, set_id)
    db.execute("UPDATE brief_sets SET name = ? WHERE id = ?", (set_name_of(db, name, but=row["id"]), row["id"]))
    return brief_set(db, row["id"])


def delete_set(db, set_id):
    """A brief set no project uses, with its records and the change log's entries about them
    (they cannot be undone: their set is gone). Default is never deleted."""
    row = brief_set_or_refuse(db, set_id)
    if row["id"] == DEFAULT_SET:
        raise KnowledgeError("The brief set “Default” cannot be deleted: every project that was given no other set uses it.")
    users = set_users(db, row["id"])
    if users:
        names = ", ".join(f"“{u['project']}”" for u in users)
        raise KnowledgeError(f"The brief set “{row['name']}” is used by {len(users)} project(s) ({names}): give them another brief set "
                             "in their Config box first.")
    with atomic(db):
        gone = {r["id"] for r in db.all("SELECT id FROM records WHERE set_id = ?", (row["id"],))}
        db.execute("DELETE FROM records WHERE set_id = ?", (row["id"],))
        # Removed projects that used it: their kept lessons fall to Default.
        db.execute("DELETE FROM project_sets WHERE set_id = ?", (row["id"],))
        for change in db.all("SELECT id, items_json FROM changes"):
            if any(item["record"] in gone for item in json.loads(change["items_json"])):
                db.execute("DELETE FROM changes WHERE id = ?", (change["id"],))
        db.execute("DELETE FROM brief_sets WHERE id = ?", (row["id"],))
    return {"deleted": row["id"], "records": len(gone)}


def set_of(db, project, created):
    """The brief set a project uses: the one it was given, Default otherwise."""
    row = db.one("SELECT set_id FROM project_sets WHERE project = ? AND created = ?", (project, created))
    return row["set_id"] if row is not None else DEFAULT_SET


def give_set(db, project, created, set_id):
    """A project's brief set chosen; its lessons are told that set's brief from their next start."""
    row = brief_set_or_refuse(db, set_id)
    db.execute("INSERT OR REPLACE INTO project_sets (project, created, set_id) VALUES (?, ?, ?)", (project, created, row["id"]))
    return row
