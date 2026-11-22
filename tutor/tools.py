"""The "learn" tools: declared once, checked, run with a time limit, and audited.

The registry is lifted from gaggiclanker (the maintainer's project, AGPL like this one),
gaggiclanker/tools/registry.py (ToolRegistry, its dispatch order, its audit row and
input_hash). Adapted: no pydantic (the image takes Debian's packages only), so a tool's input
is a JSON Schema checked by the small validator below, which knows the parts these schemas
use; a third permission, `write`, for a tool that changes the tutor's own records directly
(gaggiclanker has read and propose only); the conversation's kind (Goals or a lesson) as the
scope; a cap on the calls of one run and on the size of a result, which neither agent would
enforce for us.

Whose data a call touches is never an argument: `ctx` holds the running turn the caller's
token named, and every tool reads the project and the lesson from it.
"""
import asyncio
import hashlib
import json
import logging
import time

import cards
import knowledge
import plans
import store

log = logging.getLogger("tutor")

PERMISSIONS = ("read", "write", "propose")
KINDS = ("goals", "lesson")
CALLS_PER_RUN = 60
RESULT_MAX = 64 * 1024
DEFAULT_TIMEOUT_S = 10.0
SHOWN = "Shown to the learner as a card. Their decision (approved as proposed, approved with their changes, or dismissed) reaches you at the start of their next message; nothing changes until then."


class ToolError(Exception):
    """A tool refuses what it was asked; the message is for the model."""


TOOLS = {}


def tool(name, permission, description, schema, kinds=KINDS, timeout_s=DEFAULT_TIMEOUT_S):
    """Declare `fn(ctx, args) -> dict` as a tool."""
    if permission not in PERMISSIONS:
        raise ValueError(f"tool {name!r} declares permission {permission!r}; a tool reads, writes or proposes")

    def decorate(fn):
        if name in TOOLS:
            raise RuntimeError(f"tool {name!r} is already declared")
        TOOLS[name] = {"name": name, "permission": permission, "description": description, "schema": schema,
                       "kinds": kinds, "timeout_s": timeout_s, "fn": fn}
        return fn
    return decorate


def listed(kind):
    """The tools a conversation of this kind has, as MCP's tools/list gives them, by name."""
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["schema"]}
            for t in sorted(TOOLS.values(), key=lambda t: t["name"]) if kind in t["kinds"]]


# --- the checks -------------------------------------------------------------------------------

def check(schema, value, where="input"):
    """The parts of JSON Schema these tools use: type, properties, required,
    additionalProperties false, items, min/maxItems, min/maxLength, enum. Returns a sentence, or None."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return f"{where} must be an object"
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{where} lacks `{key}`"
        for key, item in value.items():
            if key not in props:
                if schema.get("additionalProperties") is False:
                    return f"{where} has an unknown field `{key}`"
                continue
            said = check(props[key], item, f"{where}.{key}")
            if said:
                return said
    elif kind == "array":
        if not isinstance(value, list):
            return f"{where} must be a list"
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10**9):
            return f"{where} must hold {schema.get('minItems', 0)} to {schema.get('maxItems', 'any number of')} items"
        for index, item in enumerate(value):
            said = check(schema.get("items", {}), item, f"{where}[{index}]")
            if said:
                return said
    elif kind == "string":
        if not isinstance(value, str):
            return f"{where} must be a string"
        if len(value.strip()) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9):
            return f"{where} must be {schema.get('minLength', 0)} to {schema.get('maxLength')} characters"
    elif kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return f"{where} must be a whole number"
    elif kind == "boolean" and not isinstance(value, bool):
        return f"{where} must be true or false"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where} must be one of: {', '.join(map(str, schema['enum']))}"
    return None


def input_hash(arguments):
    """A stable sha256 over the arguments, short enough to read in a log line (gaggiclanker's)."""
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()[:16]


async def dispatch(ctx, name, arguments):
    """Run one tool for the turn in `ctx`. Never raises for anything the tool or the model did:
    returns (ok, payload). gaggiclanker's order: exists, in scope, within the run's budget,
    arguments valid, then run with its time limit; every outcome audited, refusals included."""
    turn, db = ctx["turn"], ctx["db"]
    started = time.monotonic()
    spec = TOOLS.get(name)
    permission = spec["permission"] if spec else "read"
    status, error, payload = "ok", "", None
    if spec is None:
        status, error = "refused", f"There is no tool named {name!r}. This conversation has: {', '.join(t['name'] for t in listed(turn.kind))}."
    elif turn.kind not in spec["kinds"]:
        status, error = "refused", f"The tool {name} is not available in a {turn.kind} conversation."
    elif turn.tool_calls >= CALLS_PER_RUN:
        status, error = "refused", f"This answer has made {CALLS_PER_RUN} calls of the learn tools, the most one answer may make."
    else:
        error = check(spec["schema"], arguments, "the arguments")
        if error:
            status = "error"
    if status == "ok":
        turn.tool_calls += 1
        try:
            async with asyncio.timeout(spec["timeout_s"]):
                payload = await spec["fn"](ctx, arguments)
            text = json.dumps(payload)
            if len(text) > RESULT_MAX:
                status, error, payload = "error", f"The result is larger than {RESULT_MAX} bytes; ask for less.", None
        except TimeoutError:
            status, error = "timeout", f"{name} did not finish within {spec['timeout_s']:g} s."
        except (ToolError, plans.PlanError, knowledge.KnowledgeError, cards.CheckError) as exc:
            status, error = "error", str(exc)
        except Exception as exc:
            log.exception("tool %s failed", name)
            status, error = "error", f"The tool failed ({type(exc).__name__}); the tutor service's log says why."
    try:
        store.record_tool_call(db, turn.run_id, name if spec else str(name)[:100], permission, input_hash(arguments),
                               int((time.monotonic() - started) * 1000), status, error)
    except Exception:
        log.warning("the audit of tool %s could not be written", name, exc_info=True)
    if status == "ok":
        return True, payload
    return False, {"error": status, "detail": error}


# --- the tools --------------------------------------------------------------------------------

def lesson_brief(row, this_id=None):
    out = {"id": row["id"], "title": row["title"], "objective": row["objective"], "state": row["state"]}
    if row["id"] == this_id:
        out["this_lesson"] = True
    return out


NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
TITLE = {"type": "string", "minLength": 1, "maxLength": plans.TITLE_MAX}
OBJECTIVE = {"type": "string", "minLength": 1, "maxLength": plans.OBJECTIVE_MAX}


@tool("plan_get", "read", "The project's study plan: its modules in order, each with its lessons (id, title, objective, state: "
      "not_started, in_progress, done), the lessons outside the plan and the dropped ones; the lesson of this conversation is marked "
      "this_lesson. Also says whether the plan has a proposal the learner has not decided yet.", NO_ARGS)
async def plan_get(ctx, _args):
    turn, db = ctx["turn"], ctx["db"]
    plan = store.plan_out(db, turn.project, turn.created, lambda row: lesson_brief(row, turn.lesson_id))
    return {"has_plan": bool(plan["modules"]), "this_conversation": "goals" if turn.kind == "goals" else turn.lesson_id,
            "modules": [{"title": m["title"], "lessons": m["lessons"]} for m in plan["modules"]],
            "other_lessons": plan["other"], "dropped_lessons": plan["dropped"],
            "open_proposals": [{"id": p["id"], "kind": p["kind"]} for p in plan["proposals"]]}


@tool("lesson_get", "read", "This conversation's lesson: its title, objective, state, its place in the plan, its summary once "
      "closed, and the paths of its files in the workspace (notes.md, kept by you; transcript.md, written by clanker-learn).", NO_ARGS)
async def lesson_get(ctx, _args):
    db, turn = ctx["db"], ctx["turn"]
    row = store.lesson(db, turn.lesson_id)
    folder = store.folder_of(row)
    out = {"id": row["id"], "kind": row["kind"], "title": row["title"], "objective": row["objective"], "state": row["state"],
           "folder": folder, "notes": f"{folder}/notes.md", "transcript": f"{folder}/transcript.md"}
    if row["kind"] == "lesson":
        out["place"] = plans.place_of(db, row)
    if row["state"] == "done":
        out.update(summary=row["summary"], one_line=row["one_line"])
    return out


@tool("lesson_history", "read", "One line on each lesson of this project that is done, oldest first (as their tutors closed them).",
      NO_ARGS)
async def lesson_history(ctx, _args):
    turn = ctx["turn"]
    return {"done": [{"id": r["id"], "title": r["title"], "one_line": r["one_line"], "closed_at": r["closed_at"]}
                     for r in store.done_lessons(ctx["db"], turn.project, turn.created)]}


@tool("lesson_close", "write", "Close this lesson once its objective is met: it shows as done to the learner (who can still write in "
      "it). `summary` is what was covered and how the learner did, a few sentences; `one_line` is one sentence that later lessons' "
      "tutors are given.",
      {"type": "object", "properties": {"summary": {"type": "string", "minLength": 1, "maxLength": plans.SUMMARY_MAX},
                                        "one_line": {"type": "string", "minLength": 1, "maxLength": plans.ONE_LINE_MAX}},
       "required": ["summary", "one_line"], "additionalProperties": False}, kinds=("lesson",))
async def lesson_close(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    row = store.close_lesson(db, turn.lesson_id, args["summary"].strip(), " ".join(args["one_line"].split()))
    evidence_id = knowledge.add_evidence(db, "completion", turn.project, turn.created, row["id"], turn.run_id,
                                         {"title": row["title"], "one_line": row["one_line"]})
    return {"closed": row["id"], "state": row["state"], "evidence": evidence_id,
            "said": "The lesson is done; every tutor's brief lists it under Recently. The learner may still write in it; answer them as before."}


PLAN_MODULES = {"type": "array", "minItems": 1, "maxItems": plans.MODULES_MAX, "items": {
    "type": "object", "properties": {"title": TITLE, "lessons": {"type": "array", "minItems": 1, "maxItems": plans.LESSONS_MAX, "items": {
        "type": "object", "properties": {"title": TITLE, "objective": OBJECTIVE}, "required": ["title", "objective"],
        "additionalProperties": False}}},
    "required": ["title", "lessons"], "additionalProperties": False}}


def show_proposal(ctx, kind, proposed):
    turn = ctx["turn"]
    row = store.add_proposal(ctx["db"], turn.lesson_id, turn.run_id, kind, proposed)
    ctx["runner"].on_proposal(turn, store.proposal_out(row))
    return {"proposal": row["id"], "said": SHOWN}


@tool("plan_propose", "propose", "Propose a study plan to the learner: modules in order, each a list of lessons with a title and an "
      "objective (what the learner will be able to do at its end). Only in the Goals conversation, or when the project has no plan "
      "yet. `note` is a sentence or two to the learner about the plan.",
      {"type": "object", "properties": {"modules": PLAN_MODULES, "note": {"type": "string", "maxLength": plans.NOTE_MAX}},
       "required": ["modules"], "additionalProperties": False})
async def plan_propose(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    if turn.kind != "goals" and store.has_plan(db, turn.project, turn.created):
        raise ToolError("The project has a plan already: propose a change of it with plan_change_propose.")
    return show_proposal(ctx, "plan", {"modules": plans.clean_plan(args["modules"]), "note": (args.get("note") or "").strip()})


@tool("plan_change_propose", "propose", "Propose a change of the project's study plan: the WHOLE new arrangement, modules in order, "
      "each a list of lessons. Name an existing lesson by its `id` (from plan_get; add `title` or `objective` only to change them), a "
      "new lesson by its `title` and `objective`. A lesson of the plan you do not name is dropped (kept as dropped if it was started). "
      "`reason` says to the learner why.",
      {"type": "object", "properties": {
          "modules": {"type": "array", "minItems": 1, "maxItems": plans.MODULES_MAX, "items": {
              "type": "object", "properties": {"title": TITLE, "lessons": {"type": "array", "minItems": 1, "maxItems": plans.LESSONS_MAX, "items": {
                  "type": "object", "properties": {"id": {"type": "integer"}, "title": TITLE, "objective": OBJECTIVE},
                  "additionalProperties": False}}},
              "required": ["title", "lessons"], "additionalProperties": False}},
          "reason": {"type": "string", "minLength": 1, "maxLength": plans.NOTE_MAX}},
       "required": ["modules", "reason"], "additionalProperties": False})
async def plan_change_propose(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    if not store.has_plan(db, turn.project, turn.created):
        raise ToolError("The project has no plan yet: propose one with plan_propose.")
    modules, dropped = plans.clean_change(db, turn.project, turn.created, args["modules"])
    return show_proposal(ctx, "change", {"modules": modules, "dropped": dropped, "reason": args["reason"].strip()})


# --- the knowledge base (knowledge.py) ----------------------------------------------------------

LINE = {"type": "string", "minLength": 1}
QUOTE = {"type": "string", "minLength": 1, "maxLength": 1000}
EVIDENCE_IDS = {"type": "array", "maxItems": 20, "items": {"type": "integer"}}
RESTS_ON = {"type": "object", "properties": {"evidence": EVIDENCE_IDS, "quote": QUOTE}, "additionalProperties": False}
INSIGHT_KIND = {"type": "string", "enum": list(knowledge.INSIGHT_KINDS)}
RESTS_SAID = ("`rests_on` is what the record rests on: `evidence`, ids that exercise_check, answer_grade, lesson_close or the "
              "learner's answers gave you, and/or `quote`, a sentence of at least 12 characters that the learner typed in this project, "
              "copied exactly.")


def this_project_insights(ctx):
    turn = ctx["turn"]
    return [knowledge.record_brief(row) for row in knowledge.records(ctx["db"], "insight", turn.project, turn.created)]


@tool("brief_get", "read", "The learner's brief as it is now (their goals, how they want to be taught, their background, the global "
      "insights they approved, the lessons done recently in the projects that use this project's brief set) and this project's "
      "insights in force, with their numbers. The opening context of this conversation had the brief as it was then.", NO_ARGS)
async def brief_get(ctx, _args):
    turn = ctx["turn"]
    text, cut, _parts = knowledge.brief(ctx["db"], knowledge.set_of(ctx["db"], turn.project, turn.created))
    return {"brief": text, "cut": cut, "this_project_insights": this_project_insights(ctx)}


@tool("records_search", "read", "Search what you may read of the learner's records for every word of `query`: the lesson summaries of "
      "every project, the goals, preferences and background of this project's brief set, the global insights, and this project's "
      "insights (retired ones too).",
      {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}}, "required": ["query"],
       "additionalProperties": False})
async def records_search(ctx, args):
    return knowledge.search(ctx["db"], ctx["turn"], args["query"])


@tool("insight_record", "write", "Record one line on how the learner learns in this project: a misconception, a strength, what worked, "
      "what did not (`kind`). Only this project's tutors are told it. " + RESTS_SAID,
      {"type": "object", "properties": {"text": LINE, "kind": INSIGHT_KIND, "rests_on": RESTS_ON}, "required": ["text", "kind", "rests_on"],
       "additionalProperties": False})
async def insight_record(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    rests_on = knowledge.rests_on_of(db, turn, args["rests_on"])
    row = knowledge.add(db, "insight", knowledge.one_line(args["text"]), knowledge.by_tutor(turn), project=turn.project, created=turn.created,
                        insight_kind=args["kind"], rests_on=rests_on)
    return {"insight": row["id"], "rests_on": rests_on,
            "said": "Recorded. This project's tutors are told it from their next lesson; the learner sees it and can edit or delete it."}


@tool("insight_replace", "write", "Replace an insight of this project (by its number) with a new line: the old one is retired and the "
      "new one names it. You never edit an insight in place. " + RESTS_SAID,
      {"type": "object", "properties": {"id": {"type": "integer"}, "text": LINE, "kind": INSIGHT_KIND, "rests_on": RESTS_ON},
       "required": ["id", "text", "rests_on"], "additionalProperties": False})
async def insight_replace(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    knowledge.own_insight(db, turn, args["id"])
    rests_on = knowledge.rests_on_of(db, turn, args["rests_on"])
    row = knowledge.replace(db, args["id"], knowledge.one_line(args["text"]), knowledge.by_tutor(turn), rests_on, args.get("kind"))
    return {"insight": row["id"], "replaces": args["id"], "rests_on": rests_on, "said": f"Insight {args['id']} is retired; {row['id']} takes its place."}


@tool("insight_retire", "write", "Retire an insight of this project (by its number) that no longer holds, saying why in `reason`.",
      {"type": "object", "properties": {"id": {"type": "integer"}, "reason": {"type": "string", "minLength": 1, "maxLength": knowledge.REASON_MAX}},
       "required": ["id", "reason"], "additionalProperties": False})
async def insight_retire(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    knowledge.own_insight(db, turn, args["id"])
    knowledge.retire(db, args["id"], knowledge.by_tutor(turn), knowledge.one_line(args["reason"]))
    return {"retired": args["id"], "said": "Retired: no tutor is told it any more."}


def in_their_words(ctx, kind, args):
    turn, db = ctx["turn"], ctx["db"]
    text = knowledge.one_line(args["text"])
    set_id = knowledge.set_of(db, turn.project, turn.created)
    same = next((row for row in knowledge.records(db, kind, set_id=set_id) if knowledge.collapsed(row["text"]) == knowledge.collapsed(text)), None)
    if same is not None:
        raise ToolError(f"That {kind} is recorded already ({same['id']}).")
    quote_id = knowledge.quote_evidence(db, turn, args["quote"])
    row = knowledge.add(db, kind, text, knowledge.by_tutor(turn), project=turn.project, created=turn.created,
                        rests_on={"evidence": [quote_id], "insights": []}, set_id=set_id)
    name = knowledge.brief_set(db, set_id)["name"]
    return {kind: row["id"], "quote_evidence": quote_id,
            "said": f"Recorded in the learner's brief set “{name}”: every tutor of every project that uses it is told this {kind}; "
                    "the learner can edit or delete it."}


QUOTED = {"type": "object", "properties": {"text": LINE, "quote": QUOTE}, "required": ["text", "quote"], "additionalProperties": False}


@tool("preference_record", "write", "Record how the learner wants to be taught, as one line (`text`), ONLY when they said it: `quote` is "
      "the sentence they typed in this project that says it, copied exactly (at least 12 characters). It is refused otherwise.", QUOTED)
async def preference_record(ctx, args):
    return in_their_words(ctx, "preference", args)


@tool("goal_record", "write", "Record what the learner wants to reach, as one line (`text`), ONLY when they said it: `quote` is the "
      "sentence they typed in this project that says it, copied exactly (at least 12 characters). It is refused otherwise.", QUOTED)
async def goal_record(ctx, args):
    return in_their_words(ctx, "goal", args)


@tool("global_insight_propose", "propose", "Propose a global insight, told to every tutor of every project once the learner approves "
      "it on their Knowledge page (not in this conversation): `{text, rests_on}` for a new one, `{replaces, text, rests_on}` to replace "
      "one in force, `{retires, reason}` to retire one. `rests_on` cites this project's insights (`insights`, their numbers) and/or a "
      "`quote` of the learner's. Sparingly: only what holds beyond this project.",
      {"type": "object", "properties": {"text": LINE, "rests_on": {"type": "object", "properties": {"insights": EVIDENCE_IDS, "quote": QUOTE},
                                                                  "additionalProperties": False},
                                        "replaces": {"type": "integer"}, "retires": {"type": "integer"},
                                        "reason": {"type": "string", "maxLength": knowledge.REASON_MAX}},
       "additionalProperties": False})
async def global_insight_propose(ctx, args):
    row = knowledge.propose_global(ctx["db"], ctx["turn"], args)
    return {"global_proposal": row["id"], "said": "Listed on the learner's Knowledge page, where they may edit, approve or dismiss it. No tutor "
            "is told it until they approve it; their decision reaches you at the start of their next message in this lesson."}


# --- exercises and quick questions (cards.py) ----------------------------------------------------

@tool("exercise_define", "write", "Set the learner an exercise, shown as a card: a `title`, its `steps`, and a `check`: a shell `command` "
      "run in /workspace as the learner (no credentials, 30 s at most) that exits with `expect_exit` (default 0) once their work is "
      "right. The learner can press Check; so can you, with exercise_check.",
      {"type": "object", "properties": {
          "title": {"type": "string", "minLength": 1, "maxLength": plans.TITLE_MAX},
          "steps": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
          "check": {"type": "object", "properties": {"command": {"type": "string", "minLength": 1, "maxLength": 4000}, "expect_exit": {"type": "integer"}},
                    "required": ["command"], "additionalProperties": False}},
       "required": ["title", "steps", "check"], "additionalProperties": False}, kinds=("lesson",))
async def exercise_define(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    spec = {"title": knowledge.one_line(args["title"]), "steps": [step.strip() for step in args["steps"]],
            "check": {"command": args["check"]["command"].strip(), "expect_exit": args["check"].get("expect_exit", 0)}}
    row = store.add_card(db, turn.lesson_id, turn.run_id, "exercise", spec)
    ctx["runner"].on_card(turn, store.card_out(row))
    return {"exercise": row["id"], "said": "Shown to the learner as a card with a Check button. When they press it, the outcome reaches you "
            "with their next message; run the check yourself with exercise_check."}


def card_of_project(ctx, card_id, kind):
    turn, db = ctx["turn"], ctx["db"]
    row = store.card(db, card_id)
    home = store.lesson(db, row["lesson_id"]) if row else None
    if row is None or row["kind"] != kind or (home["project"], home["created"]) != (turn.project, turn.created):
        raise ToolError(f"There is no {kind} {card_id} in this project.")
    return row


@tool("exercise_check", "write", "Run an exercise's check now, in this project's sandbox (as the learner, in /workspace, 30 s at most). "
      "The command, its exit code and output are stored as evidence (its id is returned) and shown on the card.",
      {"type": "object", "properties": {"exercise": {"type": "integer"}}, "required": ["exercise"], "additionalProperties": False},
      kinds=("lesson",), timeout_s=60)
async def exercise_check(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    row = card_of_project(ctx, args["exercise"], "exercise")
    outcome = await ctx["runner"].run_check(turn.project, json.loads(row["spec_json"])["check"]["command"], turn.run_id)
    row = cards.record_check(db, row, outcome, "tutor", turn.run_id, told=True)
    ctx["runner"].on_card(turn, store.card_out(row))
    result = json.loads(row["result_json"])
    return {key: result[key] for key in ("evidence", "passed", "exit_code", "timed_out", "output", "output_cut")}


@tool("quiz_ask", "write", "Ask the learner a quick question, shown as a card they answer in place: `options` (2 to 8), `answer` the index "
      "of the right one (from 0), `explanation` shown once they have answered. The page does not show the right answer before; do not "
      "give it away in your text. Their choice reaches you with their next message.",
      {"type": "object", "properties": {
          "question": {"type": "string", "minLength": 1, "maxLength": 2000},
          "options": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 500}},
          "answer": {"type": "integer"}, "explanation": {"type": "string", "minLength": 1, "maxLength": 2000}},
       "required": ["question", "options", "answer", "explanation"], "additionalProperties": False}, kinds=("lesson",))
async def quiz_ask(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    if not 0 <= args["answer"] < len(args["options"]):
        raise ToolError(f"`answer` is the index of the right option, 0 to {len(args['options']) - 1}.")
    spec = {"question": args["question"].strip(), "options": [knowledge.one_line(o) for o in args["options"]], "answer": args["answer"],
            "explanation": args["explanation"].strip()}
    row = store.add_card(db, turn.lesson_id, turn.run_id, "question", spec)
    ctx["runner"].on_card(turn, store.card_out(row))
    return {"question": row["id"], "said": "Shown to the learner as a card. Their choice reaches you with their next message."}


@tool("answer_grade", "write", "Grade an answer the learner TYPED (not one chosen on a card, which is graded by itself): `quote` is the "
      "sentence of their answer, copied exactly as they typed it in this project; `right` your grade; `note` why, in a sentence; "
      "`question` what was asked. Stored as evidence; its id is returned, for an insight to rest on.",
      {"type": "object", "properties": {"quote": QUOTE, "right": {"type": "boolean"}, "note": {"type": "string", "minLength": 1, "maxLength": 1000},
                                        "question": {"type": "string", "maxLength": 2000}},
       "required": ["quote", "right", "note"], "additionalProperties": False}, kinds=("lesson",))
async def answer_grade(ctx, args):
    turn, db = ctx["turn"], ctx["db"]
    message = knowledge.find_quote(db, turn.project, turn.created, args["quote"])
    evidence_id = knowledge.add_evidence(db, "answer", turn.project, turn.created, message["lesson_id"], turn.run_id,
                                         {"how": "typed", "quote": knowledge.one_line(args["quote"]), "message_id": message["id"],
                                          "question": (args.get("question") or "").strip(), "right": args["right"], "note": args["note"].strip()})
    return {"evidence": evidence_id}
