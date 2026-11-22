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
    additionalProperties false, items, min/maxItems, min/maxLength. Returns a sentence, or None."""
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
        except (ToolError, plans.PlanError) as exc:
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
    turn = ctx["turn"]
    row = store.close_lesson(ctx["db"], turn.lesson_id, args["summary"].strip(), " ".join(args["one_line"].split()))
    return {"closed": row["id"], "state": row["state"], "said": "The lesson is done. The learner may still write in it; answer them as before."}


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
