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

The HTTP contract (paths, bodies, the envelope) is the one the web server's relay is built
against; see the web server's routes under /broker/tutor/.
"""
import hmac
import ipaddress
import logging
import os
import re
import socket
import sys

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
    row = store.add_lesson(request.app["db"], project, created, title_of(body))
    return ok(lesson_view(request, row))


async def get_lesson(request):
    db, runner = request.app["db"], request.app["runner"]
    row = lesson_or_404(request)
    running = runner.running_run(row["id"])
    return ok({"lesson": lesson_view(request, row), "messages": store.messages(db, row["id"]),
               "run": store.run_out(store.run(db, running)) if running else None, "usage": store.latest_usage(db, row["id"])})


async def rename_lesson(request):
    row = lesson_or_404(request)
    title = title_of(await body_of(request))
    return ok(lesson_view(request, store.rename_lesson(request.app["db"], row["id"], title)))


async def delete_lesson(request):
    row = lesson_or_404(request)
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
        web.get("/lessons/{id}", get_lesson), web.patch("/lessons/{id}", rename_lesson), web.delete("/lessons/{id}", delete_lesson),
        web.post("/lessons/{id}/messages", post_message),
        web.get("/runs/{id}/stream", stream), web.post("/runs/{id}/cancel", cancel),
        web.get("/activity", activity),
        web.get("/settings", get_settings), web.put("/settings", put_settings),
    ])

    async def on_shutdown(app):
        await app["tasks"].cancel_all()
    app.on_shutdown.append(on_shutdown)
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
    except (OSError, ValueError, store.StoreError) as exc:
        # The reason is in the container's log, where the keeper reads why it ended.
        log.error("cannot start: %s", exc)
        raise SystemExit(1) from None
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
