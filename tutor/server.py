"""clanker-learn's tutor service: a conversation per lesson, with Claude Code in the sandbox.

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

A second listener, on every address, port 7802, is the "learn" tool server for Claude Code in
the sandboxes (mcp.py): it takes only the token of a running answer, and answers none of the
routes below, as this one answers none of its.

Every project has a Goals conversation and a study plan; a tutor's proposal of a plan, or of
a change of it, is a card the learner edits and approves or dismisses here (plans.py).

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
STATUS = {"validation": 400, "unauthorized": 401, "not_found": 404, "busy": 409, "failed": 500}


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
    return store.lesson_out(row, request.app["runner"].running_run(row["id"]))


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
    row = lesson_or_404(request)
    body = await body_of(request)
    if "title" not in body and "objective" not in body:
        raise Refusal("validation", "Give a `title`, an `objective`, or both.")
    if row["kind"] == "goals" and "title" in body:
        raise Refusal("validation", "Goals keeps its name.")
    title = title_of(body) if "title" in body else None
    objective = objective_of(body) if "objective" in body else None
    return ok(lesson_view(request, store.edit_lesson(request.app["db"], row["id"], title, objective)))


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
    try:
        if row["kind"] == "plan":
            modules = plans.clean_plan(raw)
            changes = plans.describe_changes(proposed["modules"], modules)
            plans.apply_plan(db, project, created, modules)
        else:
            modules, _dropped = plans.clean_change(db, project, created, raw)
            changes = plans.describe_changes(proposed["modules"], modules)
            plans.apply_change(db, project, created, modules)
    except plans.PlanError as exc:
        raise Refusal("validation", str(exc)) from None
    decided = store.decide_proposal(db, row["id"], "approved", {"modules": modules}, changes)
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


async def get_settings(_request):
    return ok({"claude_token_set": bool(turn.read_token())})


async def put_settings(request):
    token = (await body_of(request)).get("claude_token")
    if not isinstance(token, str):
        raise Refusal("validation", "`claude_token` must be a string (empty removes it).")
    token = token.strip()
    if token and not TOKEN.fullmatch(token):
        raise Refusal("validation", "That is not a token: it is one word of printable characters, as `claude setup-token` gives it.")
    turn.write_token(token)
    return ok({"claude_token_set": bool(token)})


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
        web.get("/settings", get_settings), web.put("/settings", put_settings),
    ])

    async def on_shutdown(app):
        await app["tasks"].cancel_all()
    app.on_shutdown.append(on_shutdown)

    # The tool server: its own application on its own port, started and stopped with this one.
    async def tool_server(app):
        runner = web.AppRunner(mcp.build_tool_app(db, app["runner"]), access_log=None)
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
        log.info("the database was at version 1: brought to version %d in place", store.SCHEMA_VERSION)
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
        web.run_app(app, host=host, port=PORT, print=None, handle_signals=True, shutdown_timeout=5)
    finally:
        db.close()


if __name__ == "__main__":
    main()
