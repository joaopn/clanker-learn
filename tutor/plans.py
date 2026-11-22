"""Study plans and what a tutor is told: proposals checked and applied, the sentences that say
how the learner changed one, a lesson's opening context (with the learner's brief and the
project's own insights, knowledge.py), and its transcript.

New for clanker-learn; the rule that a proposal is applied only by the learner's action, and
that the tutor is told the outcome with the next message, is gaggiclanker's (its proposal
cards, web/src/components/chat/tellAgent.ts).

A plan is a list of modules, each a list of lessons: `{"modules": [{"title", "lessons":
[{"title", "objective"}]}]}`. A change of the plan is the whole new arrangement in the same
shape, an existing lesson named by its `id` (with a new title or objective if it changes) and
a new one by its title and objective; a lesson of the project that it does not name is
dropped. Goals is never part of either.
"""
import json

import cards
import knowledge
import store

TITLE_MAX = 200
OBJECTIVE_MAX = 1000
NOTE_MAX = 2000
MODULES_MAX = 20
LESSONS_MAX = 30
SUMMARY_MAX = 4000
ONE_LINE_MAX = 300


class PlanError(Exception):
    """A plan or a change that cannot be taken; the message says why, to a person or a tutor."""


def text_field(value, what, limit, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise PlanError(f"{what} is 1 to {limit} characters.")
    return " ".join(value.split()) if limit <= TITLE_MAX else value.strip()


def modules_of(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MODULES_MAX:
        raise PlanError(f"`modules` is a list of 1 to {MODULES_MAX} modules.")
    for number, module in enumerate(raw, 1):
        if not isinstance(module, dict) or not isinstance(module.get("lessons"), list) or not 1 <= len(module["lessons"]) <= LESSONS_MAX:
            raise PlanError(f"Module {number} must have a `title` and `lessons`, a list of 1 to {LESSONS_MAX} lessons.")
        if not all(isinstance(item, dict) for item in module["lessons"]):
            raise PlanError(f"Each lesson of module {number} is an object.")
    return raw


def clean_plan(raw):
    """A proposed (or approved) plan, checked and tidied: every lesson new."""
    out = []
    for module in modules_of(raw):
        lessons = [{"title": text_field(item.get("title"), "A lesson's title", TITLE_MAX),
                    "objective": text_field(item.get("objective"), "A lesson's objective", OBJECTIVE_MAX)} for item in module["lessons"]]
        out.append({"title": text_field(module.get("title"), "A module's title", TITLE_MAX), "lessons": lessons})
    return out


def clean_change(db, project, created, raw):
    """A proposed (or approved) arrangement, checked against the project's lessons as they are
    now. Each existing lesson comes back with its title, objective and state (changed where the
    arrangement changes them); returns (modules, dropped)."""
    known = {row["id"]: row for row in store.lessons(db, project, created) if row["kind"] == "lesson"}
    named, out = set(), []
    for module in modules_of(raw):
        lessons = []
        for item in module["lessons"]:
            lesson_id = item.get("id")
            if lesson_id is None:
                lessons.append({"title": text_field(item.get("title"), "A new lesson's title", TITLE_MAX),
                                "objective": text_field(item.get("objective"), "A new lesson's objective", OBJECTIVE_MAX)})
                continue
            if isinstance(lesson_id, bool) or not isinstance(lesson_id, int) or lesson_id not in known:
                raise PlanError(f"There is no lesson {lesson_id!r} in this project's plan (Goals is not part of it).")
            if lesson_id in named:
                raise PlanError(f"The lesson {lesson_id} is named twice.")
            named.add(lesson_id)
            row = known[lesson_id]
            title = text_field(item.get("title"), "A lesson's title", TITLE_MAX, required=False) or row["title"]
            objective = text_field(item.get("objective"), "A lesson's objective", OBJECTIVE_MAX, required=False) or row["objective"]
            lessons.append({"id": lesson_id, "title": title, "objective": objective, "state": row["state"]})
        out.append({"title": text_field(module.get("title"), "A module's title", TITLE_MAX), "lessons": lessons})
    dropped = [{"id": row["id"], "title": row["title"], "state": row["state"]} for lesson_id, row in known.items()
               if lesson_id not in named and row["state"] != "dropped"]
    return out, dropped


def apply_plan(db, project, created, modules):
    """An approved plan: its modules and lessons made after whatever the project has, in order,
    each lesson not started."""
    module_at = (db.one("SELECT max(position) FROM modules WHERE project = ? AND created = ?", (project, created))[0] or 0) + 1
    lesson_at = store.next_position(db, project, created)
    for module in modules:
        module_id = store.add_module(db, project, created, module["title"], module_at)
        module_at += 1
        for item in module["lessons"]:
            store.add_lesson(db, project, created, item["title"], item["objective"], module_id, lesson_at)
            lesson_at += 1


def apply_change(db, project, created, modules):
    """An approved arrangement: the modules made anew, every lesson it names placed (renamed,
    brought back if it was dropped), the new ones made, and the project's other lessons
    dropped: kept as dropped when they have messages, removed when they have none."""
    named = {item["id"] for module in modules for item in module["lessons"] if item.get("id")}
    for row in store.lessons(db, project, created):
        if row["kind"] != "lesson" or row["id"] in named or row["state"] == "dropped":
            continue
        if store.has_messages(db, row["id"]):
            db.execute(f"UPDATE lessons SET state = 'dropped', module_id = NULL, updated_at = {store.NOW} WHERE id = ?", (row["id"],))
        else:
            store.delete_lesson(db, row["id"])
    db.execute("DELETE FROM modules WHERE project = ? AND created = ?", (project, created))
    position = 1
    for module_at, module in enumerate(modules, 1):
        module_id = store.add_module(db, project, created, module["title"], module_at)
        for item in module["lessons"]:
            if item.get("id"):
                state = store.lesson(db, item["id"])["state"]
                if state == "dropped":
                    state = "in_progress" if store.has_messages(db, item["id"]) else "not_started"
                db.execute(f"UPDATE lessons SET title = ?, objective = ?, module_id = ?, position = ?, state = ?, updated_at = {store.NOW} "
                           "WHERE id = ?", (item["title"], item["objective"], module_id, position, state, item["id"]))
            else:
                store.add_lesson(db, project, created, item["title"], item["objective"], module_id, position)
            position += 1
    # Lessons outside the arrangement that stay (the dropped ones) come after it.
    for row in db.all("SELECT id FROM lessons WHERE project = ? AND created = ? AND kind = 'lesson' AND module_id IS NULL ORDER BY position, id",
                      (project, created)):
        db.execute("UPDATE lessons SET position = ? WHERE id = ?", (position, row["id"]))
        position += 1


def describe_changes(proposed, approved, dropped=()):
    """How the learner's approved version differs from what was proposed, as sentences for the
    tutor, also shown to the learner on the card after "You" (so they name neither reader as
    "you" otherwise). Lessons are matched by id, else by title, else by objective; one whose
    title and objective both changed reads as removed and added. `dropped` is what a change
    proposed to drop: a lesson of it the learner placed is kept, and an existing lesson the
    proposal placed that the learner left out is dropped."""
    to_drop = {item.get("id") for item in dropped if item.get("id")}
    def flat(modules):
        return [dict(item, module=index, module_title=module["title"]) for index, module in enumerate(modules) for item in module["lessons"]]
    before, after = flat(proposed), flat(approved)
    same_shape = len(proposed) == len(approved)
    sentences, pairs, used = [], [], set()
    for item in after:
        match = None
        for key in ("id", "title", "objective"):
            if key == "id" and not item.get("id"):
                continue
            match = next((i for i, old in enumerate(before) if i not in used and old.get(key) == item.get(key)
                          and (key == "id" or not old.get("id"))), None)
            if match is not None:
                break
        if match is None:
            if item.get("id") in to_drop:
                sentences.append(f"kept the lesson “{item['title']}”, which the proposal dropped, in the module “{item['module_title']}”")
            else:
                sentences.append(f"added the lesson “{item['title']}” (objective: {item['objective']})")
            continue
        used.add(match)
        old = before[match]
        pairs.append((match, item))
        if old["title"] != item["title"]:
            sentences.append(f"renamed the lesson “{old['title']}” to “{item['title']}”")
        if old["objective"] != item["objective"]:
            sentences.append(f"changed the objective of “{item['title']}” to: {item['objective']}")
        moved = old["module"] != item["module"] if same_shape else old["module_title"] != item["module_title"]
        if moved:
            sentences.append(f"moved “{item['title']}” to the module “{item['module_title']}”")
    for i, old in enumerate(before):
        if i not in used:
            sentences.append(f"dropped the lesson “{old['title']}”, which the proposal kept" if old.get("id")
                             else f"removed the lesson “{old['title']}”")
    if same_shape:
        for old, new in zip(proposed, approved, strict=True):
            if old["title"] != new["title"]:
                sentences.append(f"renamed the module “{old['title']}” to “{new['title']}”")
    else:
        sentences.append("arranged the modules as: " + ", ".join(f"“{m['title']}”" for m in approved))
    order = [index for index, _item in pairs]
    if order != sorted(order):
        # The whole approved order, added lessons among it: a list without them reads as if they were left out.
        sentences.append("changed the order of the lessons to: " + ", ".join(f"“{item['title']}”" for item in after))
    return sentences


def outcome_note(row):
    """What the tutor is told of a proposal the learner decided, at the next message."""
    what = "study plan" if row["kind"] == "plan" else "change of the study plan"
    if row["state"] == "dismissed":
        return f"[clanker-learn: the learner dismissed your proposed {what} (proposal {row['id']}); nothing was changed.]"
    changes = json.loads(row["changes_json"])
    if not changes:
        return f"[clanker-learn: the learner approved your proposed {what} (proposal {row['id']}) as you proposed it; plan_get shows the plan now.]"
    return (f"[clanker-learn: the learner approved your proposed {what} (proposal {row['id']}) with these changes: "
            + "; ".join(changes) + ". plan_get shows the plan now.]")


# --- what changed in the plan since a conversation's last turn ----------------------------------

PLAN_NOTE_ITEMS = 8
PLAN_NOTE_TITLE = 80


def plan_snapshot(db, project, created):
    """What a tutor knows of its project's plan, to compare with later: each lesson's title,
    objective, state and module (by title: modules are made anew at every change), the modules
    and the order of the lessons."""
    lessons, order = {}, []
    titles = {m["id"]: m["title"] for m in store.modules(db, project, created)}
    for row in store.lessons(db, project, created):
        if row["kind"] != "lesson":
            continue
        lessons[str(row["id"])] = {"t": row["title"], "o": row["objective"], "s": row["state"], "m": titles.get(row["module_id"])}
        if row["state"] != "dropped":
            order.append(str(row["id"]))
    return {"lessons": lessons, "modules": list(titles.values()), "order": order}


def fold_seen(seen, before, after):
    """`seen` with what changed from `before` to `after` taken as seen: the change a
    conversation's own proposal made, which its tutor is told as the proposal's outcome."""
    if seen is None:
        return None
    lessons = dict(seen["lessons"])
    for key in set(before["lessons"]) | set(after["lessons"]):
        if before["lessons"].get(key) != after["lessons"].get(key):
            if key in after["lessons"]:
                lessons[key] = after["lessons"][key]
            else:
                lessons.pop(key, None)
    return {"lessons": lessons, "modules": after["modules"] if before["modules"] != after["modules"] else seen["modules"],
            "order": after["order"] if before["order"] != after["order"] else seen["order"]}


def plan_changes(seen, now, this_id=None):
    """The changes from `seen` to `now`, as short phrases."""
    def name(key, entry):
        title = entry["t"] if len(entry["t"]) <= PLAN_NOTE_TITLE else entry["t"][: PLAN_NOTE_TITLE - 1] + "…"
        return f"“{title}”" + (" (this lesson)" if key == str(this_id) else "")
    out = []
    modules_changed = seen["modules"] != now["modules"]
    for key, new in now["lessons"].items():
        old = seen["lessons"].get(key)
        if old is None:
            if new["s"] != "dropped":
                out.append(f"added {name(key, new)}" + (f" to the module “{new['m']}”" if new["m"] else " outside the plan"))
            continue
        if old["t"] != new["t"]:
            out.append(f"renamed {name(key, old)} to “{new['t']}”")
        if old["s"] != "dropped" and new["s"] == "dropped":
            out.append(f"dropped {name(key, new)}")
            continue
        if old["s"] == "dropped" and new["s"] != "dropped":
            out.append(f"brought back {name(key, new)}")
        if new["s"] == "done" and old["s"] != "done":
            out.append(f"closed {name(key, new)} (done)")
        if old["o"] != new["o"]:
            out.append(f"changed the objective of {name(key, new)}")
        if not modules_changed and old["m"] != new["m"] and new["s"] != "dropped" and old["s"] != "dropped":
            out.append(f"moved {name(key, new)} to " + (f"the module “{new['m']}”" if new["m"] else "outside the plan"))
    for key, old in seen["lessons"].items():
        if key not in now["lessons"] and old["s"] != "dropped":
            out.append(f"removed {name(key, old)}")
    if modules_changed:
        out.append("arranged the modules as: " + ", ".join(f"“{m}”" for m in now["modules"]) if now["modules"] else "removed every module")
    common = [k for k in seen["order"] if k in now["order"]]
    if common != [k for k in now["order"] if k in seen["order"]]:
        out.append("changed the order of the lessons")
    return out


def plan_note(seen, now, this_id=None):
    """Before the learner's text: what changed in the plan since this conversation's last turn
    (from any conversation, or by hand), capped; None when nothing did."""
    changes = plan_changes(seen, now, this_id)
    if not changes:
        return None
    shown = changes[:PLAN_NOTE_ITEMS]
    more = len(changes) - len(shown)
    return ("[clanker-learn: since your last turn here, the project's study plan was changed outside this conversation (from another "
            "conversation, or by the learner in the list): " + "; ".join(shown)
            + (f"; and {more} more change(s)" if more else "") + ". plan_get shows the plan now.]")


# --- a lesson's opening context ----------------------------------------------------------------

def place_of(db, row):
    """Where a lesson stands in its project's plan, as a sentence."""
    mods = store.modules(db, row["project"], row["created"])
    planned = [r for r in store.lessons(db, row["project"], row["created"]) if r["kind"] == "lesson" and r["state"] != "dropped"]
    if row["module_id"] is None:
        return f"it was added by hand, outside the study plan (the project has {len(planned)} lesson(s) in all)"
    index = next((i for i, m in enumerate(mods, 1) if m["id"] == row["module_id"]), None)
    in_module = [r for r in planned if r["module_id"] == row["module_id"]]
    at = next((i for i, r in enumerate(planned, 1) if r["id"] == row["id"]), "?")
    at_module = next((i for i, r in enumerate(in_module, 1) if r["id"] == row["id"]), "?")
    title = mods[index - 1]["title"] if index else "?"
    return (f"lesson {at} of {len(planned)} in the plan; lesson {at_module} of {len(in_module)} in the module “{title}” "
            f"(module {index} of {len(mods)})")


def plan_lines(db, row):
    lines = []
    for module in store.modules(db, row["project"], row["created"]):
        lines.append(f"- Module “{module['title']}”:")
        for r in store.lessons(db, row["project"], row["created"]):
            if r["module_id"] == module["id"] and r["state"] != "dropped":
                mark = " (this lesson)" if r["id"] == row["id"] else ""
                lines.append(f"  - “{r['title']}”, {r['state'].replace('_', ' ')}{mark}")
    return lines


def opening_context(db, row, fresh_reason=None, earlier=0, taught_by=None):
    """The first message of a new session carries this before the learner's text. With
    `fresh_reason`, the session replaces one the tutor no longer has, or takes the lesson up
    from the other agent (`taught_by`, its name): it is told which turns it did not see, and
    where to read them."""
    folder = store.folder_of(row)
    if row["kind"] == "goals":
        parts = [(f"[clanker-learn: this is the Goals conversation of the project “{row['project']}”, where the learner and you agree on what "
                 "they will learn. Ask what they want to be able to do, what they already know, and how much time they have: a few "
                 "questions, not a questionnaire. Then call the tool plan_propose with a study plan (modules, each a list of lessons "
                 "with a title and an objective). The learner sees it as a card they can edit, then approve or dismiss; their decision "
                 f"reaches you with their next message. This conversation's files are in {folder}/: keep notes.md there current.")]
    else:
        parts = [f"[clanker-learn: this conversation is the lesson “{row['title']}” of the project “{row['project']}”.",
                 f"Its objective: {row['objective'].rstrip('.') or '(none was given: ask the learner what they want from it)'}.",
                 f"Its place: {place_of(db, row)}."]
        plan = plan_lines(db, row)
        if plan:
            parts.append("The plan:\n" + "\n".join(plan))
        done = store.done_lessons(db, row["project"], row["created"])
        if done:
            parts.append("The lessons done so far, oldest first:\n" + "\n".join(f"- “{r['title']}”: {r['one_line']}" for r in done))
        else:
            parts.append("No lesson of this project is done yet.")
        parts.append(f"This lesson's files are in {folder}/: keep notes.md there current as you teach (what was covered, the exercise in "
                     "progress, what is open); transcript.md is the conversation, written by clanker-learn after every answer. When the "
                     "objective is met, close the lesson with lesson_close.")
    # What every tutor is told of the learner, then what only this project's tutors are told:
    # another project's insights never enter it.
    parts.append(knowledge.brief(db)[0])
    parts.append(knowledge.project_insights_text(db, row["project"], row["created"]))
    if fresh_reason and taught_by:
        parts.append(f"You are taking this conversation up afresh ({fresh_reason}): the learner's messages 1 to {earlier} here, and the "
                     f"answers {taught_by} gave them, are not in this session of yours. Before you answer, read "
                     f"{folder}/transcript.md (the whole conversation so far) and {folder}/notes.md (the notes kept on it, if any), and "
                     "say in a sentence that you took the lesson up from its files.")
    elif fresh_reason:
        parts.append(f"You are taking this conversation up afresh ({fresh_reason}): the learner's messages 1 to {earlier} here, and your "
                     "answers to them, were in an earlier session of yours that you do not have. Before you answer, read "
                     f"{folder}/transcript.md (the whole conversation so far) and {folder}/notes.md (your notes, if you kept them), and "
                     "say in a sentence that you took the lesson up from its files.")
    return "\n\n".join(parts) + "]"


def missed_note(row, first, last, who):
    """Before the learner's text, for a session resumed after turns the other agent answered."""
    folder = store.folder_of(row)
    one = first == last
    which, verb, them = (f"message {first}", "was", "it") if one else (f"messages {first} to {last}", "were", "them")
    by = f"{verb} answered by {who}, another tutor, in a session of its own" if who else "did not reach this session of yours"
    return (f"[clanker-learn: while you were away from this lesson, the learner's {which} here {by}: you have not seen {them}. "
            f"Before you answer, read {them} in {folder}/transcript.md (the whole conversation) and {folder}/notes.md, and go on "
            "from where the lesson stands now.]")


# --- a lesson's transcript ---------------------------------------------------------------------

def proposal_lines(p):
    what = "A study plan" if p["kind"] == "plan" else "A change of the study plan"
    state = {"open": "open: the learner has not decided", "approved": "approved", "dismissed": "dismissed"}[p["state"]]
    lines = [f"> **Card: {what} proposed** ({state})", ">"]
    content = p["approved"] or p["proposed"]
    for module in content.get("modules", []):
        lines.append(f"> - {module['title']}")
        for item in module["lessons"]:
            lines.append(f">   - {item['title']}: {item.get('objective', '')}")
    for item in p["proposed"].get("dropped", []):
        lines.append(f"> - dropped: {item['title']}")
    for key in ("note", "reason"):
        if p["proposed"].get(key):
            lines.append(f"> {key.capitalize()}: {p['proposed'][key]}")
    if p["changes"]:
        lines.append("> The learner changed: " + "; ".join(p["changes"]) + ".")
    return lines


def transcript(db, row):
    """The conversation in full, as Markdown: each message, each tool line with its command,
    each card with its state."""
    lines = [f"# {row['title']}", "",
             f"Project “{row['project']}”, lesson {row['id']} ({row['kind']}), state: {row['state'].replace('_', ' ')}."]
    if row["objective"]:
        lines.append(f"Objective: {row['objective']}")
    if row["summary"]:
        lines += ["", f"Summary at its close: {row['summary']}"]
    lines += ["", "*Written by clanker-learn after every answer, from its own record: an edit here is overwritten.*", ""]
    number = 0
    for m in store.messages(db, row["id"]):
        # Which agent wrote an answer (a lesson can change hands, and the one taking it up reads
        # this), and the learner's messages numbered as the notes to a tutor count them.
        if m["role"] != "user":
            who = f"The tutor ({store.AGENT_NAMES.get(m.get('agent'), 'Claude Code')})"
        else:
            number += 1
            who = f"The learner, message {number}" + (" (a card's answer, written by clanker-learn)" if m.get("composed") else "")
        lines += [f"## {who} · {m['created_at'][:16].replace('T', ' ')}", ""]
        if m["text"].strip():
            lines += [m["text"].strip(), ""]
        for tool in m.get("tools", []):
            ok = "not finished" if tool.get("unfinished") else {True: "ok", False: "failed", None: "no result"}[tool["ok"]]
            lines.append(f"- {tool['kind']}: `{tool['label']}` ({ok})")
            if tool["kind"] == "shell" and tool["detail"] != tool["label"]:
                lines += ["  ```", *("  " + line for line in tool["detail"].splitlines()), "  ```"]
        if m.get("tools"):
            lines.append("")
        for p in m.get("proposals", []):
            lines += [*proposal_lines(p), ""]
        for card in m.get("cards", []):
            lines += [*cards.transcript_lines(card), ""]
    return "\n".join(lines).rstrip() + "\n"
