"""clanker-learn's tutor service: a conversation per lesson, with Claude Code or pi in the sandbox.

It runs in the inner container `cl-tutor`, on the front network with no published port, with
no capability, and listens on its front-network address only (port 7801): the web server
reaches it there, a sandbox (on whose network it also is, to reach the sandbox's sshd) finds
nothing listening. Every request but GET /health carries the secret it shares with the web
server (X-CL-Secret), compared in constant time; the web server does the login. Its state is
one SQLite file, /data/tutor/tutor.db (store.py), and the Claude token set in Settings, a file
of mode 0600 beside it, never returned and never logged.

A turn is a background task of the service (turn.py): it goes on when the browser goes away,
and its events are stored before they are published, so that a stream that comes back
replays what it missed (sse.py). One running turn per lesson; lessons run side by side, in
the same project too.

A second listener, on every address, port 7802, is the "learn" tool server for the agents in
the sandboxes (mcp.py): it takes only the token of a running answer, and answers none of the
routes below, as this one answers none of its.

Every project has a Goals conversation and a study plan; a tutor's proposal of a plan, or of
a change of it, is a card the learner edits and approves or dismisses here (plans.py). The
knowledge base (knowledge.py) is read and changed here for the Knowledge page: the brief sets
(and the one each project uses), a set's brief, the records, the global proposals, the lesson
summaries, the change log and its undo; and the
learner answers a lesson's question cards and presses Check on its exercises (cards.py).

The HTTP contract (paths, bodies, the envelope) is the one the web server's relay is built
against; see the web server's routes under /broker/tutor/.
"""
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import sys

import cards
import knowledge
import mcp
import plans
import store
import turn
from aiohttp import web
from sse import EventBus, run_stream, sse_response
from tasks import TaskRegistry

log = logging.getLogger("tutor")

PORT = int(os.environ.get("CL_TUTOR_PORT", "7801"))
SECRET_FILE = os.environ.get("CL_TUTOR_SECRET_FILE", "/run/cl-secret/tutor")
DB_FILE = f"{turn.DATA}/tutor.db"
PROJECT = re.compile(r"[a-z0-9]([a-z0-9-]{0,29}[a-z0-9])?")   # the core's project names
CREATED_MAX = 64
TITLE_MAX = 200
TEXT_MAX = 32000
INSTRUCTIONS_MAX = 100000
TOKEN = re.compile(r"[\x21-\x7e]{1,4096}")   # printable, no spaces: it becomes an environment variable
LISTEN_PROBE_PORT = 9
STATUS = {"validation": 400, "unauthorized": 401, "not_found": 404, "busy": 409, "conflict": 409, "failed": 500}


class Refusal(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message


def ok(result):
    return web.json_response({"ok": True, "result": result})


def refused(kind, message):
    return web.json_response({"ok": False, "error": {"kind": kind, "message": message}}, status=STATUS[kind])


def listen_address(subnet):
    """The container's own address inside `subnet`, found as the source the kernel picks toward
    the subnet's first host. Lifted from the web server (web/services.py, listen_address)."""
    if not subnet:
        raise ValueError("no listen subnet given (CL_LISTEN_SUBNET is empty)")
    net = ipaddress.ip_network(subnet, strict=True)
    if net.version != 4:
        raise ValueError(f"listen subnet {subnet} is not IPv4")
    first = next(net.hosts(), None) if net.num_addresses > 1 else net.network_address
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((str(first), LISTEN_PROBE_PORT))
            src = s.getsockname()[0]
    except OSError as e:
        raise ValueError(f"no route toward the listen subnet {subnet} ({e})") from None
    if ipaddress.ip_address(src) not in net:
        raise ValueError(f"this container has no address in the listen subnet {subnet} (the route there leaves from {src})")
    return src


# --- the request's parts ---------------------------------------------------------------------

async def body_of(request):
    try:
        body = await request.json()
    except ValueError:
        raise Refusal("validation", "The body must be JSON.") from None
    if not isinstance(body, dict):
        raise Refusal("validation", "The body must be a JSON object.")
    return body


def project_pair(source):
    project, created = source.get("project"), source.get("created")
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise Refusal("validation", "`project` must be a project's name.")
    if not isinstance(created, str) or not 0 < len(created) <= CREATED_MAX:
        raise Refusal("validation", "`created` must be the project's creation time, as its status gives it.")
    return project, created


def title_of(body):
    title = body.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX:
        raise Refusal("validation", f"A lesson's title is 1 to {TITLE_MAX} characters.")
    return " ".join(title.split())


def objective_of(body):
    objective = body.get("objective", "")
    if not isinstance(objective, str) or len(objective) > plans.OBJECTIVE_MAX:
        raise Refusal("validation", f"A lesson's objective is at most {plans.OBJECTIVE_MAX} characters.")
    return objective.strip()


def number(request, key):
    try:
        return int(request.match_info[key])
    except ValueError:
        raise Refusal("not_found", "There is no such item.") from None


def lesson_or_404(request):
    row = store.lesson(request.app["db"], number(request, "id"))
    if row is None:
        raise Refusal("not_found", "There is no such lesson.")
    return row


def lesson_view(request, row):
    """A lesson with the agent in effect, and why (`agent_source`: "lesson", its own choice;
    "begun", the agent that answered it last, which it keeps when the default agent changes;
    "default", before its first answer)."""
    agent, source = store.agent_choice(request.app["db"], row)
    return dict(store.lesson_out(row, request.app["runner"].running_run(row["id"])), agent_in_effect=agent, agent_source=source)


def agent_in(body, key):
    """An agent's name from a body: claude, pi, or null for the default."""
    agent = body.get(key)
    if agent is not None and agent not in store.AGENTS:
        raise Refusal("validation", f"`{key}` is claude, pi, or null for the default agent.")
    return agent


# --- the routes ------------------------------------------------------------------------------

async def health(_request):
    return ok({"state": "up"})


async def list_lessons(request):
    project, created = project_pair(request.query)
    rows = store.lessons(request.app["db"], project, created)
    return ok({"lessons": [lesson_view(request, row) for row in rows]})


async def add_lesson(request):
    body = await body_of(request)
    project, created = project_pair(body)
    row = store.add_lesson(request.app["db"], project, created, title_of(body), objective_of(body))
    return ok(lesson_view(request, row))


async def get_lesson(request):
    db, runner = request.app["db"], request.app["runner"]
    row = lesson_or_404(request)
    running = runner.running_run(row["id"])
    return ok({"lesson": lesson_view(request, row), "messages": store.messages(db, row["id"]),
               "run": store.run_out(store.run(db, running)) if running else None, "usage": store.latest_usage(db, row["id"]),
               "ended": store.ended_runs(db, row["id"])})


async def edit_lesson(request):
    """A lesson's title, objective, or agent (`agent`: claude, pi, or null for the default one).
    The agent is not changed while an answer is being written in the lesson."""
    db = request.app["db"]
    row = lesson_or_404(request)
    body = await body_of(request)
    if not {"title", "objective", "agent"} & set(body):
        raise Refusal("validation", "Give a `title`, an `objective`, an `agent`, or more than one.")
    if row["kind"] == "goals" and "title" in body:
        raise Refusal("validation", "Goals keeps its name.")
    title = title_of(body) if "title" in body else None
    objective = objective_of(body) if "objective" in body else None
    if "agent" in body:
        agent = agent_in(body, "agent")
        if request.app["runner"].running_run(row["id"]):
            raise Refusal("busy", "The tutor is writing an answer in this lesson: switch agents once it is done, or stop it.")
        store.set_lesson_agent(db, row["id"], agent)
    return ok(lesson_view(request, store.edit_lesson(db, row["id"], title, objective)))


async def delete_lesson(request):
    row = lesson_or_404(request)
    if row["kind"] == "goals":
        raise Refusal("validation", "Goals cannot be deleted: every project has it.")
    if request.app["runner"].running_run(row["id"]):
        raise Refusal("busy", "The tutor is working in this lesson: stop it first.")
    store.delete_lesson(request.app["db"], row["id"])
    return ok({"deleted": row["id"]})


async def post_message(request):
    row = lesson_or_404(request)
    text = (await body_of(request)).get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
        raise Refusal("validation", f"A message is 1 to {TEXT_MAX} characters.")
    started = request.app["runner"].start(row, text)
    if started is None:
        raise Refusal("busy", "The tutor is still working on the last message of this lesson: wait for it, or stop it.")
    run, message = started
    return ok({"run": run, "message": message})


async def stream(request):
    runner = request.app["runner"]
    run_id = number(request, "id")
    if store.run(request.app["db"], run_id) is None:
        raise Refusal("not_found", "There is no such run.")
    try:
        after = max(0, int(request.query.get("after", "0")))
    except ValueError:
        raise Refusal("validation", "`after` must be a sequence number.") from None
    return await sse_response(request, run_stream(request.app["bus"], runner.replay, runner.is_running, run_id, after))


async def cancel(request):
    run = await request.app["runner"].cancel(number(request, "id"))
    if run is None:
        raise Refusal("not_found", "There is no such run.")
    return ok({"run": run})


async def get_plan(request):
    project, created = project_pair(request.query)
    return ok(store.plan_out(request.app["db"], project, created, lambda row: lesson_view(request, row)))


def proposal_or_404(request):
    row = store.proposal(request.app["db"], number(request, "id"))
    if row is None:
        raise Refusal("not_found", "There is no such proposal.")
    return row


async def get_proposal(request):
    return ok({"proposal": store.proposal_out(proposal_or_404(request))})


async def approve_proposal(request):
    """The learner's edited version of what was proposed (or the proposal as it is when the body
    has no `modules`), applied: a plan's lessons made, a change's arrangement put in place."""
    db = request.app["db"]
    row = proposal_or_404(request)
    if row["state"] != "open":
        raise Refusal("validation", f"This proposal was {row['state']} already.")
    body = await body_of(request)
    home = store.lesson(db, row["lesson_id"])
    project, created = home["project"], home["created"]
    proposed = json.loads(row["proposed_json"])
    raw = body.get("modules", proposed["modules"])
    before = plans.plan_snapshot(db, project, created)
    try:
        if row["kind"] == "plan":
            modules = plans.clean_plan(raw)
            changes = plans.describe_changes(proposed["modules"], modules)
            plans.apply_plan(db, project, created, modules)
        else:
            modules, _dropped = plans.clean_change(db, project, created, raw)
            changes = plans.describe_changes(proposed["modules"], modules, proposed.get("dropped", ()))
            plans.apply_change(db, project, created, modules)
    except plans.PlanError as exc:
        raise Refusal("validation", str(exc)) from None
    decided = store.decide_proposal(db, row["id"], "approved", {"modules": modules}, changes)
    # The conversation the proposal came from is told of it as its outcome: not again among the
    # plan's changes since its last turn.
    seen = store.plan_seen(db, row["lesson_id"])
    if seen is not None:
        store.set_plan_seen(db, row["lesson_id"], plans.fold_seen(seen, before, plans.plan_snapshot(db, project, created)))
    return ok({"proposal": store.proposal_out(decided), "plan": store.plan_out(db, project, created, lambda r: lesson_view(request, r))})


async def dismiss_proposal(request):
    row = proposal_or_404(request)
    if row["state"] != "open":
        raise Refusal("validation", f"This proposal was {row['state']} already.")
    return ok({"proposal": store.proposal_out(store.decide_proposal(request.app["db"], row["id"], "dismissed"))})


async def get_instructions(request):
    return ok(store.instructions(request.app["db"]))


async def put_instructions(request):
    text = (await body_of(request)).get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > INSTRUCTIONS_MAX:
        raise Refusal("validation", f"The tutor's instructions are 1 to {INSTRUCTIONS_MAX} characters.")
    return ok(store.set_instructions(request.app["db"], text.strip()))


async def reset_instructions(request):
    return ok(store.reset_instructions(request.app["db"]))


async def activity(request):
    return ok({"running": request.app["runner"].activity()})


def settings_out(db):
    return {"claude_token_set": bool(turn.read_token()), "default_agent": store.default_agent(db)}


async def get_settings(request):
    return ok(settings_out(request.app["db"]))


async def put_settings(request):
    """The Claude token (`claude_token`; empty removes it) and the default agent
    (`default_agent`: claude, pi, or null for claude), either or both."""
    db = request.app["db"]
    body = await body_of(request)
    if "claude_token" not in body and "default_agent" not in body:
        raise Refusal("validation", "Give a `claude_token`, a `default_agent`, or both.")
    token = None
    if "claude_token" in body:
        token = body["claude_token"]
        if not isinstance(token, str):
            raise Refusal("validation", "`claude_token` must be a string (empty removes it).")
        token = token.strip()
        if token and not TOKEN.fullmatch(token):
            raise Refusal("validation", "That is not a token: it is one word of printable characters, as `claude setup-token` gives it.")
    agent = agent_in(body, "default_agent") if "default_agent" in body else None
    if token is not None:
        turn.write_token(token)
    if "default_agent" in body:
        store.set_setting(db, "default_agent", agent)
    return ok(settings_out(db))


# --- the knowledge base, for the Knowledge page (knowledge.py) ---------------------------------

def record_view(db, row):
    return dict(knowledge.record_out(row), rests_on_detail=knowledge.rests_detail(db, json.loads(row["rests_on_json"])))


def text_in(body, key="text", required=True):
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise Refusal("validation", f"`{key}` must be text.")
    return value


def set_in(source, key="set"):
    """A brief set's id from a query or a body (a number, or its text), Default when absent."""
    value = source.get(key)
    if value is None:
        return knowledge.DEFAULT_SET
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        raise Refusal("validation", f"`{key}` must be a brief set's number.")
    return value


async def get_brief(request):
    db = request.app["db"]
    set_id = set_in(request.query)
    text, cut, parts = knowledge.brief(db, set_id)
    return ok({"text": text, "cut": cut, "open_proposals": knowledge.open_global_count(db),
               "set": knowledge.brief_set_out(db, knowledge.brief_set(db, set_id)),
               "parts": {"goals": [knowledge.record_out(r) for r in parts["goals"]],
                         "preferences": [knowledge.record_out(r) for r in parts["preferences"]],
                         "background": knowledge.record_out(parts["background"]) if parts["background"] else None,
                         "global_insights": [knowledge.record_out(r) for r in parts["global_insights"]],
                         "recently": [knowledge.summary_out(r) for r in parts["recently"]]}})


async def list_records(request):
    kind = request.query.get("kind")
    if kind not in ("goal", "preference", "background", "insight", "global"):
        raise Refusal("validation", "`kind` is goal, preference, background, insight or global.")
    project = created = None
    if "project" in request.query:
        project, created = project_pair(request.query)
    rows = knowledge.records(request.app["db"], kind, project, created, request.query.get("all") == "1", set_in(request.query))
    return ok({"records": [knowledge.record_out(row) for row in rows]})


async def add_record(request):
    body = await body_of(request)
    row = knowledge.learner_add(request.app["db"], body.get("kind"), text_in(body), set_in(body))
    return ok({"record": knowledge.record_out(row)})


def record_number(request):
    db = request.app["db"]
    return knowledge.record_or_refuse(db, number(request, "id"))["id"]


async def get_record(request):
    db = request.app["db"]
    return ok({"record": record_view(db, knowledge.record_or_refuse(db, number(request, "id")))})


async def edit_record(request):
    body = await body_of(request)
    text, kind = text_in(body, required=False), text_in(body, "insight_kind", required=False)
    if text is None and kind is None:
        raise Refusal("validation", "Give a `text`, an `insight_kind`, or both.")
    row = knowledge.edit(request.app["db"], record_number(request), knowledge.LEARNER, text, kind)
    return ok({"record": knowledge.record_out(row)})


async def delete_record(request):
    row = knowledge.delete(request.app["db"], record_number(request), knowledge.LEARNER)
    return ok({"deleted": row["id"]})


async def put_background(request):
    body = await body_of(request)
    row = knowledge.set_background(request.app["db"], text_in(body), set_in(body))
    return ok({"record": knowledge.record_out(row) if row else None})


async def list_sets(request):
    db = request.app["db"]
    return ok({"sets": [knowledge.brief_set_out(db, row) for row in knowledge.brief_sets(db)]})


async def add_set(request):
    db = request.app["db"]
    body = await body_of(request)
    copy_from = set_in(body, "copy_from") if body.get("copy_from") is not None else None
    return ok({"set": knowledge.brief_set_out(db, knowledge.create_set(db, text_in(body, "name"), copy_from))})


async def edit_set(request):
    db = request.app["db"]
    body = await body_of(request)
    return ok({"set": knowledge.brief_set_out(db, knowledge.rename_set(db, number(request, "id"), text_in(body, "name")))})


async def delete_set(request):
    return ok(knowledge.delete_set(request.app["db"], number(request, "id")))


async def get_project_set(request):
    db = request.app["db"]
    project, created = project_pair(request.query)
    return ok({"project": project, "created": created, "set": knowledge.brief_set_out(db, knowledge.brief_set(db, knowledge.set_of(db, project, created)))})


async def put_project_set(request):
    """A project's brief set; its lessons are told that set's brief from their next start."""
    db = request.app["db"]
    body = await body_of(request)
    project, created = project_pair(body)
    if body.get("set") is None:
        raise Refusal("validation", "`set` must be a brief set's number.")
    row = knowledge.give_set(db, project, created, set_in(body))
    return ok({"project": project, "created": created, "set": knowledge.brief_set_out(db, row)})


async def get_evidence(request):
    row = knowledge.evidence(request.app["db"], number(request, "id"))
    if row is None:
        raise Refusal("not_found", "There is no such evidence.")
    return ok({"evidence": knowledge.evidence_out(row)})


def global_view(db, row):
    return dict(knowledge.global_proposal_out(db, row), rests_on_detail=knowledge.rests_detail(db, json.loads(row["rests_on_json"])))


async def list_global_proposals(request):
    db = request.app["db"]
    state = request.query.get("state", "open")
    if state not in ("open", "approved", "dismissed", "all"):
        raise Refusal("validation", "`state` is open, approved, dismissed or all.")
    return ok({"proposals": [global_view(db, row) for row in knowledge.global_proposals(db, state)], "open": knowledge.open_global_count(db)})


def global_or_404(request):
    row = knowledge.global_proposal(request.app["db"], number(request, "id"))
    if row is None:
        raise Refusal("not_found", "There is no such proposal.")
    return row


async def get_global_proposal(request):
    return ok({"proposal": global_view(request.app["db"], global_or_404(request))})


async def edit_global_proposal(request):
    db = request.app["db"]
    row = global_or_404(request)
    body = await body_of(request)
    text, reason = text_in(body, required=False), text_in(body, "reason", required=False)
    if text is None and reason is None:
        raise Refusal("validation", "Give a `text`, a `reason`, or both.")
    return ok({"proposal": global_view(db, knowledge.edit_global_proposal(db, row["id"], text, reason))})


async def approve_global_proposal(request):
    db = request.app["db"]
    row = global_or_404(request)
    proposal, made = knowledge.approve_global(db, row["id"], text_in(await body_of(request), required=False))
    return ok({"proposal": global_view(db, proposal), "record": knowledge.record_out(made)})


async def dismiss_global_proposal(request):
    db = request.app["db"]
    return ok({"proposal": global_view(db, knowledge.dismiss_global(db, global_or_404(request)["id"]))})


async def list_summaries(request):
    query = request.query.get("q", "").strip()
    return ok({"lessons": [knowledge.summary_out(row) for row in knowledge.summaries(request.app["db"], query or None)]})


async def list_changes(request):
    try:
        before = int(request.query["before"]) if "before" in request.query else None
        limit = min(200, max(1, int(request.query.get("limit", "50"))))
    except ValueError:
        raise Refusal("validation", "`before` and `limit` are numbers.") from None
    out, after = knowledge.changes(request.app["db"], before, limit)
    return ok({"changes": out, "next": after})


async def undo_change(request):
    return ok({"change": knowledge.undo(request.app["db"], number(request, "id"))})


async def project_removed(request):
    """A destroyed project: its knowledge kept and marked, or forgotten (`forget`); its lessons
    and conversations removed either way. Called by the web server after a destroy."""
    body = await body_of(request)
    project, created = project_pair(body)
    forget = body.get("forget", False)
    if not isinstance(forget, bool):
        raise Refusal("validation", "`forget` is true or false.")
    if request.app["runner"].busy_in(project, created):
        raise Refusal("busy", "The tutor is still answering in this project: stop its answers first.")
    return ok({"project": project, "created": created, "forgotten": forget,
               **knowledge.project_removed(request.app["db"], project, created, forget)})


# --- the learner's side of a card (cards.py) -----------------------------------------------------

def card_or_404(request, kind=None):
    row = store.card(request.app["db"], number(request, "id"))
    if row is None or (kind and row["kind"] != kind):
        raise Refusal("not_found", f"There is no such {kind or 'card'}.")
    return row


async def get_card(request):
    return ok({"card": store.card_out(card_or_404(request))})


def as_message(body):
    value = body.get("as_message", False)
    if not isinstance(value, bool):
        raise Refusal("validation", "`as_message` is true or false.")
    return value


def after_action(request, row, send):
    """The card's new state shown in a running answer's stream; then, when asked and nothing is
    being written in the lesson, the action sent as the learner's message, its text composed by
    the system. Otherwise the tutor is told with the learner's next message."""
    runner = request.app["runner"]
    card = store.card_out(row)
    runner.card_changed(card)
    sent = None
    if send:
        started = runner.start(store.lesson(request.app["db"], row["lesson_id"]), cards.composed_text(row), composed=True)
        if started is not None:
            sent = {"run": started[0], "message": started[1]}
    return ok({"card": store.card_out(store.card(request.app["db"], row["id"])), "sent": sent})


async def check_card(request):
    db = request.app["db"]
    row = card_or_404(request, "exercise")
    send = as_message(await body_of(request))
    home = store.lesson(db, row["lesson_id"])
    try:
        outcome = await request.app["runner"].run_check(home["project"], json.loads(row["spec_json"])["check"]["command"])
    except cards.CheckError as exc:
        raise Refusal("validation", str(exc)) from None
    return after_action(request, cards.record_check(db, store.card(db, row["id"]), outcome, "learner", None, told=False), send)


async def answer_card(request):
    db = request.app["db"]
    row = card_or_404(request, "question")
    body = await body_of(request)
    send = as_message(body)
    if row["state"] == "answered":
        raise Refusal("validation", "This question is answered already.")
    choice, options = body.get("choice"), json.loads(row["spec_json"])["options"]
    if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(options):
        raise Refusal("validation", f"`choice` is the index of an option, 0 to {len(options) - 1}.")
    return after_action(request, cards.record_answer(db, row, choice), send)


# --- the application -------------------------------------------------------------------------

@web.middleware
async def guarded(request, handler):
    """The shared secret on every route but /health, and the envelope for every refusal."""
    try:
        if request.path != "/health":
            given = request.headers.get("X-CL-Secret", "")
            if not hmac.compare_digest(given.encode(), request.app["secret"].encode()):
                raise Refusal("unauthorized", "This request does not carry the tutor service's secret.")
        return await handler(request)
    except Refusal as refusal:
        return refused(refusal.kind, refusal.message)
    except knowledge.KnowledgeError as exc:
        kind = "conflict" if isinstance(exc, knowledge.Conflict) else "not_found" if isinstance(exc, knowledge.Missing) else "validation"
        return refused(kind, str(exc))
    except web.HTTPException as exc:
        if exc.status == 404:
            return refused("not_found", "There is no such route.")
        if exc.status == 405:
            return refused("validation", "That method is not taken here.")
        raise
    except Exception:
        log.exception("%s %s failed", request.method, request.path)
        return refused("failed", "The tutor service failed on this request; its log says why.")


def build_app(db, secret):
    app = web.Application(middlewares=[guarded], client_max_size=256 * 1024)
    app["db"], app["secret"], app["bus"], app["tasks"] = db, secret, EventBus(), TaskRegistry()
    app["runner"] = turn.Runner(db, app["bus"], app["tasks"])
    app.add_routes([
        web.get("/health", health),
        web.get("/lessons", list_lessons), web.post("/lessons", add_lesson),
        web.get("/lessons/{id}", get_lesson), web.patch("/lessons/{id}", edit_lesson), web.delete("/lessons/{id}", delete_lesson),
        web.post("/lessons/{id}/messages", post_message),
        web.get("/runs/{id}/stream", stream), web.post("/runs/{id}/cancel", cancel),
        web.get("/plan", get_plan),
        web.get("/proposals/{id}", get_proposal), web.post("/proposals/{id}/approve", approve_proposal),
        web.post("/proposals/{id}/dismiss", dismiss_proposal),
        web.get("/instructions", get_instructions), web.put("/instructions", put_instructions),
        web.post("/instructions/reset", reset_instructions),
        web.get("/activity", activity),
        web.get("/knowledge/brief", get_brief),
        web.get("/knowledge/sets", list_sets), web.post("/knowledge/sets", add_set),
        web.patch("/knowledge/sets/{id}", edit_set), web.delete("/knowledge/sets/{id}", delete_set),
        web.get("/knowledge/project-set", get_project_set), web.put("/knowledge/project-set", put_project_set),
        web.get("/knowledge/records", list_records), web.post("/knowledge/records", add_record),
        web.get("/knowledge/records/{id}", get_record), web.patch("/knowledge/records/{id}", edit_record),
        web.delete("/knowledge/records/{id}", delete_record),
        web.put("/knowledge/background", put_background),
        web.get("/knowledge/evidence/{id}", get_evidence),
        web.get("/knowledge/proposals", list_global_proposals), web.get("/knowledge/proposals/{id}", get_global_proposal),
        web.patch("/knowledge/proposals/{id}", edit_global_proposal),
        web.post("/knowledge/proposals/{id}/approve", approve_global_proposal),
        web.post("/knowledge/proposals/{id}/dismiss", dismiss_global_proposal),
        web.get("/knowledge/lessons", list_summaries),
        web.get("/knowledge/changes", list_changes), web.post("/knowledge/changes/{id}/undo", undo_change),
        web.post("/projects/removed", project_removed),
        web.get("/cards/{id}", get_card), web.post("/cards/{id}/check", check_card), web.post("/cards/{id}/answer", answer_card),
        web.get("/settings", get_settings), web.put("/settings", put_settings),
    ])

    async def on_shutdown(app):
        # Listening has stopped: the running answers are ended (their processes in the
        # sandboxes too) and marked interrupted, which ends their streams; then the rest.
        await app["runner"].shutdown()
        await app["tasks"].cancel_all(timeout=2)
    app.on_shutdown.append(on_shutdown)

    # The tool server: its own application on its own port, started and stopped with this one.
    async def tool_server(app):
        runner = web.AppRunner(mcp.build_tool_app(db, app["runner"]), access_log=None, shutdown_timeout=1)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", mcp.PORT).start()
        log.info("the learn tool server listens on every address, port %d", mcp.PORT)
        yield
        await runner.cleanup()
    app.cleanup_ctx.append(tool_server)
    return app


def read_secret():
    with open(SECRET_FILE) as fh:
        secret = fh.read().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", secret):
        raise ValueError(f"{SECRET_FILE} does not hold 64 hex characters")
    return secret


def main():
    logging.basicConfig(level=logging.INFO, format="tutor: %(message)s", stream=sys.stderr)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)   # a line per request says nothing worth keeping
    try:
        host = listen_address(os.environ.get("CL_LISTEN_SUBNET", ""))
        secret = read_secret()
        db = store.Database(DB_FILE).connect()
        seeded = store.seed_instructions(db, turn.shipped_text())
    except (OSError, ValueError, store.StoreError) as exc:
        # The reason is in the container's log, where the keeper reads why it ended.
        log.error("cannot start: %s", exc)
        raise SystemExit(1) from None
    if db.migrated:
        log.info("the database was at version %d: brought to version %d in place", db.migrated, store.SCHEMA_VERSION)
    log.info("the tutor's instructions: %s", seeded)
    found = store.reconcile_running(db)
    app = build_app(db, secret)

    async def clean_up(app):
        if found:
            log.info("%d run(s) were running when the service stopped: marked interrupted", len(found))
            app["tasks"].spawn("cleanup-at-start", app["runner"].cleanup_at_start(found))
    app.on_startup.append(clean_up)
    log.info("listening on %s:%d", host, PORT)
    try:
        web.run_app(app, host=host, port=PORT, print=None, handle_signals=True, shutdown_timeout=2)
    finally:
        db.close()


if __name__ == "__main__":
    main()
