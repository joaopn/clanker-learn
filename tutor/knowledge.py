"""The knowledge base: what every tutor is told about the learner, and what only one project's
tutors are told.

Records (goals, preferences, the background text, a project's insights, global insights), the
evidence the system itself saw (a check that ran, a quote, an answered question, a closed
lesson), the change log with before and after and its undo, the proposals of global insights,
the brief rendered from the records, and what is kept or forgotten of a destroyed project. All
of it is global to the installation; a record remembers the project (name and creation time)
it came from, where it has one.

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
New here: the change log and undo, the brief's rendering and its cap, the word overlap that
finds similar insights.
"""
import contextlib
import json
import re

import store

QUOTE_MIN = 12
LINE_MAX = 500
BACKGROUND_MAX = 4000
REASON_MAX = 1000
BRIEF_MAX = 8000
RECENT_MAX = 10
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


def line_of(text, what="A record", limit=LINE_MAX):
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > limit:
        raise KnowledgeError(f"{what} is 1 to {limit} characters.")
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
            "by": json.loads(row["by_json"]), "created_at": row["created_at"], "updated_at": row["updated_at"]}


def records(db, kind, project=None, created=None, include_retired=False):
    sql, params = "SELECT * FROM records WHERE kind = ?", [kind]
    if project is not None:
        sql += " AND project = ? AND created = ?"
        params += [project, created]
    if not include_retired:
        sql += " AND state = 'active'"
    return db.all(sql + " ORDER BY id", params)


def insert_record(db, kind, text, by, project=None, created=None, insight_kind=None, rests_on=None, replaces=None):
    return db.execute("INSERT INTO records (kind, text, insight_kind, project, created, rests_on_json, replaces, by_json) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (kind, text, insight_kind, project, created, json.dumps(rests_on or EMPTY_RESTS), replaces, json.dumps(by))).lastrowid


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


def learner_add(db, kind, text):
    if kind not in LINE_KINDS:
        raise KnowledgeError(f"The learner adds a {', a '.join(LINE_KINDS)} here (the background has its own route).")
    return add(db, kind, line_of(text), LEARNER)


def edit(db, record_id, by, text=None, insight_kind=None):
    """The learner's edit of a record in place (a tutor never edits one)."""
    with atomic(db):
        before = record_or_refuse(db, record_id)
        if text is not None:
            limit = BACKGROUND_MAX if before["kind"] == "background" else LINE_MAX
            text = text.strip() if before["kind"] == "background" else one_line(text)
            if not text or len(text) > limit:
                raise KnowledgeError(f"The text is 1 to {limit} characters.")
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


def set_background(db, text):
    """The background text, written by the learner only; empty removes it."""
    if not isinstance(text, str) or len(text.strip()) > BACKGROUND_MAX:
        raise KnowledgeError(f"The background is at most {BACKGROUND_MAX} characters.")
    current = records(db, "background")
    if not text.strip():
        if current:
            delete(db, current[0]["id"], LEARNER)
        return None
    if current:
        return edit(db, current[0]["id"], LEARNER, text=text)
    return add(db, "background", text.strip(), LEARNER)


def replace(db, old_id, text, by, rests_on, insight_kind=None, kind="insight", note="", source=None):
    """The old record retired, a new one added that names it: one change, undone as one. The new
    one comes from `source` (a project's name and creation time), or from where the old one did."""
    with atomic(db):
        old = record(db, old_id)
        project, created = source or (old["project"], old["created"])
        new_id = insert_record(db, kind, text, by, project, created, insight_kind or old["insight_kind"], rests_on, old_id)
        db.execute(f"UPDATE records SET state = 'retired', replaced_by = ?, updated_at = {store.NOW} WHERE id = ?", (new_id, old_id))
        log_change(db, "replace", by, [(old_id, snapshot(old), snapshot(record(db, old_id))), (new_id, None, snapshot(record(db, new_id)))], note)
    return record(db, new_id)


def retire(db, record_id, by, reason, note=""):
    with atomic(db):
        old = record(db, record_id)
        db.execute(f"UPDATE records SET state = 'retired', reason = ?, updated_at = {store.NOW} WHERE id = ?", (reason, record_id))
        log_change(db, "retire", by, [(record_id, snapshot(old), snapshot(record(db, record_id)))], note)
    return record(db, record_id)


def own_insight(db, turn, insight_id):
    """An insight a tutor may change: of the token's project, and in force."""
    row = record(db, insight_id)
    if row is None or row["kind"] != "insight" or (row["project"], row["created"]) != (turn.project, turn.created):
        raise KnowledgeError(f"There is no insight {insight_id} of this project (brief_get lists them).")
    if row["state"] != "active":
        raise KnowledgeError(f"The insight {insight_id} is retired already" + (f" (replaced by {row['replaced_by']})." if row["replaced_by"] else "."))
    return row


# --- the change log and undo --------------------------------------------------------------------

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
            if snapshot(record(db, item["record"])) != item["after"]:
                raise Conflict(f"Record {item['record']} changed after this change: undo the later changes first, or edit it by hand.")
        done = []
        for item in items:
            now = snapshot(record(db, item["record"]))
            if item["before"] is None:
                db.execute("DELETE FROM records WHERE id = ?", (item["record"],))
            else:
                columns = list(item["before"])
                db.execute(f"INSERT OR REPLACE INTO records ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                           [item["before"][c] for c in columns])
            done.append((item["record"], now, snapshot(record(db, item["record"]))))
        new_id = log_change(db, "undo", LEARNER, done, f"undid change {change_id} ({row['action']})", change_id)
    return change_out(db.one("SELECT * FROM changes WHERE id = ?", (new_id,)))


# --- lesson summaries -------------------------------------------------------------------------

def summary_out(row):
    return {"lesson_id": row["id"], "project": row["project"], "created": row["created"], "project_removed": bool(row["project_removed"]),
            "title": row["title"], "one_line": row["one_line"], "summary": row["summary"], "closed_at": row["closed_at"]}


def summaries(db, query=None, limit=None):
    """Every project's lessons that are done, newest first; `query` keeps those whose title,
    one line or summary hold every word of it."""
    rows = db.all("SELECT * FROM lessons WHERE state = 'done' AND kind = 'lesson' ORDER BY closed_at DESC, id DESC")
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


def brief(db):
    """The brief, rendered by code from the records, the same records always giving the same
    text: goals, preferences, the background, the global insights, and the last lessons done in
    any project. Over BRIEF_MAX, lines are cut from the end of the least needed part first
    (recently, then the global insights, preferences, goals; the background last), and a line
    counts what was cut. Returns (text, cut, parts)."""
    parts = {"goals": records(db, "goal"), "preferences": records(db, "preference"),
             "background": (records(db, "background") or [None])[0], "global_insights": records(db, "global")}
    done = summaries(db)
    parts["recently"] = done[:RECENT_MAX]
    cut = len(done) - len(parts["recently"])
    sections = {
        "goals": ("Their goals:", [f"- {row['text']}" for row in parts["goals"]], "none recorded yet"),
        "preferences": ("How they want to be taught:", [f"- {row['text']}" for row in parts["preferences"]], "none recorded yet"),
        "background": ("Their background, as they wrote it:", parts["background"]["text"].splitlines() if parts["background"] else [],
                       "not written yet"),
        "global_insights": ("How they learn (insights they approved for every tutor):", [insight_line(row) for row in parts["global_insights"]],
                            "none yet"),
        "recently": ("Recently (lessons done, in any project, newest first):",
                     [f"- {(row['closed_at'] or '')[:10]}, {where(row)}: “{row['title']}”: {row['one_line']}" for row in parts["recently"]],
                     "no lesson done yet"),
    }
    order = ("goals", "preferences", "background", "global_insights", "recently")
    cut_order = ("recently", "global_insights", "preferences", "goals", "background")

    def render():
        out = ["The learner's brief (every tutor of every project is told the same):"]
        for key in order:
            title, lines, empty = sections[key]
            out += [f"{title} {empty}."] if not lines else [title, *lines]
        if cut:
            out.append(f"({cut} more line(s) were cut to keep this short: records_search finds them.)")
        return "\n".join(out)
    text = render()
    while len(text) > BRIEF_MAX:
        key = next((k for k in cut_order if sections[k][1]), None)
        if key is None:
            break
        sections[key][1].pop()
        cut += 1
        text = render()
    return text, cut, parts


def project_insights_text(db, project, created):
    rows = records(db, "insight", project, created)
    if not rows:
        return "This project's insights on how the learner learns: none yet."
    return ("This project's insights on how the learner learns (told only to this project's tutors; change one with insight_replace "
            "or insight_retire, by its number):\n" + "\n".join(insight_line(row, True) for row in rows))


def search(db, turn, query):
    """records_search: every project's lesson summaries, the goals, preferences, background and
    global insights, and THIS project's insights; never another project's insights."""
    def found(rows, *fields):
        return [row for row in rows if matches(query, *(row[f] for f in fields))][:SEARCH_MAX]
    return {"lessons": [{k: v for k, v in summary_out(row).items() if k != "created"} for row in summaries(db, query, SEARCH_MAX)],
            "goals": [record_brief(r) for r in found(records(db, "goal"), "text")],
            "preferences": [record_brief(r) for r in found(records(db, "preference"), "text")],
            "background": [record_brief(r) for r in found(records(db, "background"), "text")],
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
    counts["lessons_before"] = len(lesson_ids)
    return counts
