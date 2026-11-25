"""clanker-learn's web server: what the browser talks to.

RS's webui/server.py (RS commit 62788a8), stripped. It runs in a container of the inner
daemon, `cl-web`, with no Docker socket, no project folder and no capability; all it has is
the broker's socket folder, read-only, and a folder for its certificate.

It carries: the login (the proof the browser derives from the password is relayed to the
broker, which authenticates; the broker's session token stays here, server-side, under a
cookie scoped to /broker), the first password (a set-up code from the container's log), the
project list and status, the project actions (its agents among them), Settings -> Software
(the builds of the agents and the editor, followed as operations) and the model catalog,
Settings -> Backup (make one, list them, remove one, download one with the password typed
again), the progress of an
operation, a project's
terminal (a WebSocket relayed to the sandbox's sshd, where it runs tmux), a project's session
cookie (which its web-app tabs are opened with), a project's tabs (the files server, the web
apps named by hand or detected, the raw TCP ports named by hand), opening a web app on an
origin port of its own, the ports named by hand, and the relay to the tutor service (a
lesson's conversation, its run's event stream passed through). Every route on the page's port is refused
without a login but the page and its files (static/), the login and the set-up.

Besides the page's port it listens on every port of two ranges, on the same address: on an
origin port (CL_ORIGIN_PORTS) the origin proxy serves one web app of one sandbox, HTTP and
WebSocket, to a browser holding that project's session cookie; on a pass-through port
(CL_PASSTHROUGH_PORTS) raw TCP is relayed to a sandbox's port named by hand, with no login in
front of it. Which app or port each one serves is this server's own state, in its data folder.

Nothing connects where the browser says: the broker's `attach` names the sandbox's address, its
user, its Shell password and its host key's fingerprint, and the password stays in this
process; the origin proxy and the relay connect to the address the broker's `status` gives and
the port in their own table.

What is kept from RS is kept as RS has it (names, shapes, comments' substance); where the two
differ it is said at the place.
"""
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import ssl
import struct
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import asyncssh
import services
from aiohttp import ClientConnectorError, ClientSession, ClientTimeout, WSMsgType, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from multidict import CIMultiDict
from yarl import URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cl-web")

TLS_DIR = Path(os.environ.get("CL_TLS_DIR", "/data/tls"))
# The page's port. One number all the way: the machine publishes it, clanker-learn's
# container publishes it, and this server listens on it, because cookie names carry it
# (services.broker_cookie_name) and, from the web-app tabs on, addresses are built from the
# port a request arrived on.
WEB_PORT = int(os.environ.get("CL_WEB_PORT", "7788"))
# The address the browser reaches the page at, for the certificate. The run command
# publishes on the machine's loopback.
HOST_BIND = os.environ.get("CL_WEB_BIND", "127.0.0.1")
# The two ranges, (first, last): a web app of a sandbox is served on an origin port of its
# own, and a raw TCP port named by hand is relayed from a pass-through port. Read from the
# environment by main (a range given wrongly stops the server there); these are the defaults.
ORIGIN_RANGE = (11100, 11164)
PASSTHROUGH_RANGE = (11200, 11299)
# This server's own folder (the certificate is in it): the origin table and the pass-through
# claims are kept here, so that a tab keeps its address and a claim its relay across restarts.
DATA_DIR = Path(os.environ.get("CL_DATA_DIR", "/data"))
ORIGIN_PORTS_FILE = DATA_DIR / "origin-ports.json"
PASSTHROUGH_FILE = DATA_DIR / "passthrough-claims.json"

# ===========================================================================
# Broker relay — everything that touches projects goes through the broker.
#
# The web server holds NO docker socket and NO standing authority: it relays the
# login PROOF (the browser's client-side derivation — the password itself never
# reaches the web server) to the broker (which authenticates), then holds the
# broker-issued session token in *process memory*, keyed by a cookie.
# ===========================================================================

BROKER_SOCKET = os.environ.get("CL_BROKER_SOCKET", "/run/cl-broker/broker.sock")
_BROKER_LEN = struct.Struct(">I")
BROKER_CALL_TIMEOUT_S = 30
# cookie token -> {"broker_token": str, "expires": float}
BROKER_SESSIONS: dict[str, dict] = {}
BROKER_COOKIE = services.broker_cookie_name(WEB_PORT)
# Project sessions: cookie token -> {"project", "expires", "login": the login cookie it was
# minted under}. Ended with that login.
SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = 8 * 60 * 60
# The open terminals of each login (keyed by its cookie), closed when the login ends.
TERMINALS: dict[str, set[web.WebSocketResponse]] = {}
_CLOSING: set[asyncio.Task] = set()   # close() tasks in flight, held so they are not collected
# The progress logs of operations, beside the socket (read-only here).
BROKER_OPLOG_DIR = str(Path(BROKER_SOCKET).parent / "oplogs")
# Mirror of the broker's own rule: an op_id names a file there. Matched whole (fullmatch):
# `$` alone lets a trailing newline through.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# What each write verb of the broker takes. The broker refuses any other field, so only
# these are forwarded: a field the page sends by mistake is dropped here, in one place.
BROKER_FIELDS = {
    "create": ("name", "pids", "memory", "cpus", "agents", "editor", "claude_model", "claude_effort", "pi_model", "pi_thinking"),
    "limits": ("pids", "memory", "cpus"),
    "port_add": ("port", "label", "passthrough", "host_port"),
    "port_remove": ("port", "passthrough"),
    "agents_add": ("agent",),
    "agents_model": ("model", "effort", "pi_model", "pi_thinking"),
}
BROKER_FIELDS["project_import"] = (*BROKER_FIELDS["create"], "upload")
# agents_update is RS's action name: an operation, like start (RS runs it on its lifecycle lane).
PROJECT_ACTIONS = ("start", "stop", "archive", "unarchive", "recreate", "destroy", "agents_update")

LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 60
LOGIN_LOCKOUT_SECONDS = 60


def origin_ok(request: web.Request) -> bool:
    """Reject requests whose Origin isn't this server's own."""
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    parsed = urlparse(origin)
    return parsed.netloc == request.host


class LoginLimiter:
    """Global failed-login limiter with a bounded, auto-clearing lockout. `now`
    is injectable so the cooldown is testable without sleeping."""

    def __init__(self, max_failures=LOGIN_MAX_FAILURES,
                 window_s=LOGIN_WINDOW_SECONDS, lockout_s=LOGIN_LOCKOUT_SECONDS,
                 now=time.time):
        self._max = max_failures
        self._window = window_s
        self._lockout = lockout_s
        self._now = now
        self._failures: list[float] = []
        self._locked_until = 0.0

    def retry_after(self) -> int:
        """Seconds remaining on the lockout, or 0 if not currently locked."""
        return max(0, int(self._locked_until - self._now()))

    def record_failure(self) -> None:
        now = self._now()
        self._failures = [t for t in self._failures if now - t < self._window]
        self._failures.append(now)
        if len(self._failures) >= self._max:
            self._locked_until = now + self._lockout
            self._failures.clear()

    def record_success(self) -> None:
        self._failures.clear()
        self._locked_until = 0.0


LOGIN_LIMITER = LoginLimiter()
# The first password has a limiter of its own: wrong set-up codes must not lock the login
# out, nor the other way round.
SETUP_LIMITER = LoginLimiter()


class BrokerUnavailable(Exception):
    """Broker socket missing / unreachable / mid-frame close."""


class BrokerForbidden(Exception):
    """The broker refused this process as a peer (its user is not the broker's)."""


async def broker_call(verb: str, args: dict | None = None, *,
                      token: str | None = None, op_id: str | None = None,
                      timeout: float = BROKER_CALL_TIMEOUT_S) -> dict:
    """Send one framed request to the broker and return the parsed reply.
    Raises BrokerUnavailable / BrokerForbidden; otherwise returns the reply dict
    (which may itself be an {ok:false,...} application error such as
    unauthorized/validation/failed). `op_id` keys the broker's per-operation
    progress log for a verb that runs as an operation."""
    payload = {"verb": verb, "args": args or {}}
    if token is not None:
        payload["token"] = token
    if op_id is not None:
        payload["op_id"] = op_id
    data = json.dumps(payload).encode()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(BROKER_SOCKET), timeout=timeout)
    except (TimeoutError, OSError) as e:
        raise BrokerUnavailable(str(e)) from None
    try:
        writer.write(_BROKER_LEN.pack(len(data)) + data)
        await writer.drain()
        hdr = await asyncio.wait_for(
            reader.readexactly(_BROKER_LEN.size), timeout=timeout)
        (n,) = _BROKER_LEN.unpack(hdr)
        body = await asyncio.wait_for(reader.readexactly(n), timeout=timeout)
    except (asyncio.IncompleteReadError, TimeoutError, OSError) as e:
        raise BrokerUnavailable(str(e)) from None
    finally:
        writer.close()
        with contextlib.suppress(OSError):  # closing is best effort: the reply, or the error, is what counts
            await writer.wait_closed()
    reply = json.loads(body)
    if (not reply.get("ok")
            and reply.get("error", {}).get("kind") == "forbidden"):
        raise BrokerForbidden(reply["error"].get("message", "forbidden"))
    return reply


def _broker_session(request: web.Request) -> dict | None:
    """The live login session for this request, or None. GC's an expired
    entry on lookup."""
    tok = request.cookies.get(BROKER_COOKIE)
    if not tok:
        return None
    s = BROKER_SESSIONS.get(tok)
    if not s:
        return None
    if time.time() > s["expires"]:
        BROKER_SESSIONS.pop(tok, None)
        return None
    return s


def _drop_login(cookie: str | None) -> None:
    """End a login here: its session, the project sessions minted under it, and its open
    terminals (closed in the background, so the caller answers at once)."""
    if not cookie:
        return
    BROKER_SESSIONS.pop(cookie, None)
    for token in [t for t, s in SESSIONS.items() if s.get("login") == cookie]:
        SESSIONS.pop(token, None)
    for ws in TERMINALS.pop(cookie, set()):
        task = asyncio.get_running_loop().create_task(ws.close(message=b"logged out"))
        _CLOSING.add(task)
        task.add_done_callback(_CLOSING.discard)


def _unauthorized() -> web.Response:
    return web.json_response({"ok": False, "error": {"kind": "unauthorized"}}, status=401)


def _bad_request() -> web.Response:
    return web.json_response({"ok": False, "error": {"kind": "bad_request"}}, status=400)


async def _relay(request: web.Request, verb: str,
                 args: dict | None = None, *,
                 timeout: float = BROKER_CALL_TIMEOUT_S) -> tuple[int, dict]:
    """Gated relay: require a login session, call the broker with its
    token, map every failure to an (http_status, body) pair."""
    s = _broker_session(request)
    if s is None:
        return 401, {"ok": False, "error": {"kind": "unauthorized"}}
    try:
        reply = await broker_call(verb, args, token=s["broker_token"], timeout=timeout)
    except BrokerUnavailable:
        return 503, {"ok": False, "error": {"kind": "broker_unavailable"}}
    except BrokerForbidden:
        return 403, {"ok": False, "error": {"kind": "forbidden"}}
    if (not reply.get("ok")
            and reply.get("error", {}).get("kind") == "unauthorized"):
        # Broker token expired/invalid → drop the session here too.
        _drop_login(request.cookies.get(BROKER_COOKIE))
        return 401, {"ok": False, "error": {"kind": "unauthorized"}}
    return 200, reply


async def _json_body(request: web.Request) -> dict | None:
    """The request's body as a JSON object, or None when it is not one."""
    try:
        body = await request.json()
    except web.HTTPException:
        raise  # a body over the size limit is aiohttp's 413, not a body that is not JSON
    except Exception:  # noqa: BLE001 - any body that is not JSON
        return None
    return body if isinstance(body, dict) else None


def _forward(body: dict, verb: str) -> dict:
    """Only the fields the broker takes for `verb`, as the browser sent them."""
    return {k: body[k] for k in BROKER_FIELDS[verb] if k in body}


async def broker_login_handler(request: web.Request) -> web.Response:
    """POST /broker/login {proof} — relay to the broker; on success mint a
    login session cookie holding the broker token server-side. `proof` is
    the browser's client-side login derivation (PBKDF2 over a public
    domain-separation salt) — the password itself never reaches the web server."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    wait = LOGIN_LIMITER.retry_after()
    if wait > 0:
        return web.json_response(
            {"ok": False, "error": {"kind": "rate_limited", "retry_after": wait}},
            status=429, headers={"Retry-After": str(wait)})
    body = await _json_body(request)
    proof = body.get("proof") if body else None
    if not isinstance(proof, str):
        return _bad_request()
    try:
        reply = await broker_call("login", {"proof": proof})
    except BrokerUnavailable:
        return web.json_response(
            {"ok": False, "error": {"kind": "broker_unavailable"}}, status=503)
    except BrokerForbidden:
        return web.json_response(
            {"ok": False, "error": {"kind": "forbidden"}}, status=403)
    if not reply.get("ok"):
        LOGIN_LIMITER.record_failure()
        return web.json_response(
            {"ok": False, "error": {"kind": "auth"}}, status=401)
    LOGIN_LIMITER.record_success()
    result = reply["result"]
    cookie = secrets.token_urlsafe(32)
    expires_at = float(result.get("expires_at", time.time()))
    BROKER_SESSIONS[cookie] = {
        "broker_token": result["token"],
        "expires": expires_at,
    }
    max_age = max(0, int(expires_at - time.time()))
    response = web.json_response({"ok": True})
    response.set_cookie(
        BROKER_COOKIE, cookie, path="/broker",
        httponly=True, secure=True, samesite="Strict", max_age=max_age)
    return response


async def broker_logout_handler(request: web.Request) -> web.Response:
    """POST /broker/logout — revoke the broker token, drop the session (with its project
    sessions and its terminals), clear the cookie. Always 200 (idempotent). Origin-checked
    like every POST: SameSite=Strict still sends the cookie from another port of the same
    host."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    s = _broker_session(request)
    if s is not None:
        try:
            await broker_call("logout", token=s["broker_token"])
        except (BrokerUnavailable, BrokerForbidden):
            pass
    _drop_login(request.cookies.get(BROKER_COOKIE))
    response = web.json_response({"ok": True})
    response.del_cookie(BROKER_COOKIE, path="/broker")
    return response


async def broker_setup_state_handler(request: web.Request) -> web.Response:
    """GET /broker/setup — {needed}: whether a first password can be set now, with the
    set-up code from the container's log. Open: it is asked before anyone can log in."""
    try:
        reply = await broker_call("setup_needed")
    except (BrokerUnavailable, BrokerForbidden):
        return web.json_response(
            {"ok": False, "error": {"kind": "broker_unavailable"}}, status=503)
    return web.json_response(
        {"ok": True, "needed": bool(reply.get("ok") and reply.get("result", {}).get("needed"))})


async def broker_setup_handler(request: web.Request) -> web.Response:
    """POST /broker/setup {code, proof} — set the first password. `code` is the set-up code
    the first start printed in the container's log; `proof` is the same derivation the login
    sends. The broker decides; a wrong code and "a password exists already" read the same."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    wait = SETUP_LIMITER.retry_after()
    if wait > 0:
        return web.json_response(
            {"ok": False, "error": {"kind": "rate_limited", "retry_after": wait}},
            status=429, headers={"Retry-After": str(wait)})
    body = await _json_body(request)
    code, proof = (body.get("code"), body.get("proof")) if body else (None, None)
    if not isinstance(code, str) or not isinstance(proof, str):
        return _bad_request()
    try:
        reply = await broker_call("first_password", {"code": code, "proof": proof})
    except BrokerUnavailable:
        return web.json_response(
            {"ok": False, "error": {"kind": "broker_unavailable"}}, status=503)
    except BrokerForbidden:
        return web.json_response(
            {"ok": False, "error": {"kind": "forbidden"}}, status=403)
    if not reply.get("ok"):
        # Only a wrong code counts toward the lock-out. The broker's own lock-out and a
        # password in the wrong form are said with its words; anything else, without them.
        kind = reply.get("error", {}).get("kind")
        if kind == "auth":
            SETUP_LIMITER.record_failure()
            return web.json_response(
                {"ok": False, "error": {"kind": "auth"}}, status=401)
        if kind == "rate_limited":
            return web.json_response(
                {"ok": False, "error": {"kind": "rate_limited", "message": reply["error"].get("message", "")}},
                status=429)
        if kind == "validation":
            return web.json_response(
                {"ok": False, "error": {"kind": "validation", "message": reply["error"].get("message", "")}},
                status=400)
        if kind == "bad_request":
            return _bad_request()
        return web.json_response(
            {"ok": False, "error": {"kind": "failed"}}, status=500)
    SETUP_LIMITER.record_success()
    return web.json_response({"ok": True})


async def broker_projects_handler(request: web.Request) -> web.Response:
    """GET /broker/projects — the project list (gated).
    The SameSite=Strict session cookie is the CSRF defense for this read."""
    status, body = await _relay(request, "list")
    return web.json_response(body, status=status)


async def broker_project_status_handler(request: web.Request) -> web.Response:
    """GET /broker/project/{name} — one project's status (gated)."""
    status, body = await _relay(request, "status", {"name": request.match_info.get("name", "")})
    return web.json_response(body, status=status)


def _mint_op_id(name: str, action: str) -> str:
    """A safe-basename op_id embedding project/action/ts for a browsable handle,
    plus a random suffix for uniqueness. The broker re-validates it against the
    same rule before it names a file; a valid project name (at most 31
    characters) always yields a valid op_id."""
    return f"{name}-{action}-{int(time.time())}-{secrets.token_hex(4)}"


async def _lane_relay(request: web.Request, verb: str, args: dict,
                      seed: str, started=None, action: str | None = None) -> web.Response:
    """Kick a broker verb that runs as an operation in a child and answers at once:
    mint an op_id, one synchronous broker call carrying it, return {ok, op_id}
    or the broker's refusal. The operation then lives in the broker's progress
    log; the browser follows it there (the log, /alive, and the op's state).
    `action` names the operation in its op_id where the verb is too long for a
    31-character project name to fit in 64 characters."""
    s = _broker_session(request)
    if s is None:
        return _unauthorized()
    op_id = _mint_op_id(seed, action or verb)
    # A name the op_id rule rejects would hand the browser a handle it cannot
    # follow — answer it as a normal validation error.
    if not _OP_ID_RE.fullmatch(op_id):
        return web.json_response(
            {"ok": False, "error": {"kind": "validation",
             "message": "that is not a project name"}},
            status=200)
    try:
        reply = await broker_call(verb, args, token=s["broker_token"],
                                  op_id=op_id, timeout=BROKER_CALL_TIMEOUT_S)
    except BrokerUnavailable:
        return web.json_response(
            {"ok": False, "error": {"kind": "broker_unavailable"}}, status=503)
    except BrokerForbidden:
        return web.json_response(
            {"ok": False, "error": {"kind": "forbidden"}}, status=403)
    if (not reply.get("ok")
            and reply.get("error", {}).get("kind") == "unauthorized"):
        _drop_login(request.cookies.get(BROKER_COOKIE))
        return _unauthorized()
    if not reply.get("ok"):
        return web.json_response(reply, status=200)   # busy / validation / step-up → app error
    if started is not None:
        started(op_id, s["broker_token"])
    return web.json_response({"ok": True, "op_id": op_id})


async def broker_create_handler(request: web.Request) -> web.Response:
    """POST /broker/project {name, pids?, memory?, cpus?} — create a project (gated,
    origin-checked). Returns {op_id} at once; the browser follows the operation. (RS waits
    for its broker here; this broker answers at once and works in a child, since a first
    creation builds an image for minutes.)"""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None or not isinstance(body.get("name"), str):
        return _bad_request() if _broker_session(request) else _unauthorized()
    return await _lane_relay(request, "create", _forward(body, "create"), body["name"])


async def broker_project_import_handler(request: web.Request) -> web.Response:
    """POST /broker/project-import {create's fields, upload} — a new project made from a project
    file taken in by POST /broker/project-file (gated, origin-checked), relayed as create is."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None or not isinstance(body.get("name"), str):
        return _bad_request() if _broker_session(request) else _unauthorized()
    return await _lane_relay(request, "project_import", _forward(body, "project_import"), body["name"], action="import")


async def broker_project_export_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/export {conversations?} — write the project as one file
    (gated, origin-checked): an operation, as start is; its file is then asked for with
    export-download."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    name = request.match_info.get("name", "")
    args = {"name": name}
    if isinstance(body.get("conversations"), bool):
        args["conversations"] = body["conversations"]
    return await _lane_relay(request, "project_export", args, name, action="export")


async def broker_project_limits_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/limits {pids?, memory?, cpus?} — change a project's
    limits (gated, origin-checked); the broker answers directly."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    args = dict(_forward(body, "limits"), name=request.match_info.get("name", ""))
    status, reply = await _relay(request, "limits", args)
    return web.json_response(reply, status=status)


async def broker_project_action_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/{action} — start|stop|archive|unarchive|recreate|destroy (gated,
    origin-checked). Returns {op_id} at once; the browser follows the operation. A refusal
    (busy, validation, the password needed again) comes back in this response, before any
    operation exists. `destroy` carries a step-up `proof` (the client-side login derivation
    of the re-typed password) in the body, which the broker verifies again."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    name = request.match_info.get("name", "")
    action = request.match_info.get("action", "")
    if action not in PROJECT_ACTIONS:
        return _bad_request() if _broker_session(request) else _unauthorized()
    args = {"name": name}
    if action != "destroy":
        return await _lane_relay(request, action, args, name)
    body = await _json_body(request)
    proof = body.get("proof") if body else None
    if isinstance(proof, str):
        args["proof"] = proof
    forget = bool(body.get("forget") is True) if body else False
    # The project's creation time, read before it goes: the tutor service names a project by
    # its name and that time. Without it (no such project) nothing is told after the destroy.
    status, reply = await _relay(request, "status", {"name": name})
    if status == 401:
        return _unauthorized()
    result = (reply.get("result") or {}) if status == 200 and reply.get("ok") else {}
    created = result.get("created") if result.get("name") == name else None

    def started(op_id: str, broker_token: str) -> None:
        if isinstance(created, str):
            _watch_destroy(op_id, broker_token, name, created, forget)
    return await _lane_relay(request, action, args, name, started)


async def broker_project_agents_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/agents {agent} — add an agent to a running project (gated,
    origin-checked). An operation, as RS's `update` with agents: {ok, op_id} at once, or the
    broker's refusal (an agent that is not pulled, busy) in this response."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    name = request.match_info.get("name", "")
    return await _lane_relay(request, "agents_add", dict(_forward(body, "agents_add"), name=name), name)


async def broker_project_model_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/model {model?, effort?, pi_model?, pi_thinking?} — Claude's
    model and effort, or pi's model and thinking level, of a running project (gated,
    origin-checked; RS's Config box, Agents). A field left out keeps what the project has, ""
    clears it. The broker answers directly."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    args = dict(_forward(body, "agents_model"), name=request.match_info.get("name", ""))
    status, reply = await _relay(request, "agents_model", args)
    return web.json_response(reply, status=status)


# ---- Settings -> Software, and the model catalog (RS's handlers) ------------------

async def broker_software_handler(request: web.Request) -> web.Response:
    """GET /broker/software — what is pulled of the agents and the editor, the version in
    effect and the repository's, and whether one is being built (gated). The SameSite=Strict
    cookie is the CSRF defense for this read."""
    status, body = await _relay(request, "software_status")
    return web.json_response(body, status=status)


async def broker_models_handler(request: web.Request) -> web.Response:
    """GET /broker/models — the catalog of Claude's tiers and the efforts each takes, and pi's
    thinking levels and model pattern, for the model pickers (the New project box, the
    project's Config box). Gated read."""
    status, body = await _relay(request, "models")
    return web.json_response(body, status=status)


async def broker_pin_reset_handler(request: web.Request) -> web.Response:
    """POST /broker/software/pin-reset {software} — "Use repo version": the software's pins
    out of the local override (gated, origin-checked). RS sends the pin's key; the core here
    takes the software's id ("claude", "code-server")."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request) or {}
    status, reply = await _relay(request, "pin_reset", {"software": body.get("software")})
    return web.json_response(reply, status=status)


# The fixed set of build verbs the browser may request (the input boundary — the broker
# re-validates the agent pre-spawn). agent_pull/agent_refresh carry the agent; the refreshes
# re-resolve upstream child-side (no client-supplied version crosses).
_BUILD_VERBS = frozenset({"agent_pull", "editor_pull", "agent_refresh", "editor_refresh"})


async def broker_build_handler(request: web.Request) -> web.Response:
    """POST /broker/software/build {verb, agent?} — start a build (gated, origin-checked).
    Returns {ok, op_id} at once; the browser follows it as any operation (/broker/op/<id>/log,
    /alive, /broker/op/<id>) and finds it again after a reload at /broker/software/active. A
    refusal (another build runs: busy; an unknown agent) comes back in this response."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    if _broker_session(request) is None:
        return _unauthorized()
    body = await _json_body(request) or {}
    verb = body.get("verb")
    if verb not in _BUILD_VERBS:
        return web.json_response(
            {"ok": False, "error": {"kind": "validation", "message": "unknown build verb"}}, status=200)
    args = {"agent": body.get("agent", "claude")} if verb in ("agent_pull", "agent_refresh") else {}
    return await _lane_relay(request, verb, args, "software")


_REFRESH_CHECK_VERBS = frozenset({"agent_refresh_check", "editor_refresh_check"})


async def broker_refresh_check_handler(request: web.Request) -> web.Response:
    """POST /broker/software/refresh-check {verb, agent?} — upstream's newest beside the
    version in effect, so the page can show current→latest before Refresh (gated,
    origin-checked; the broker asks upstream, bounded by the core)."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    if _broker_session(request) is None:
        return _unauthorized()
    body = await _json_body(request) or {}
    verb = body.get("verb")
    if verb not in _REFRESH_CHECK_VERBS:
        return web.json_response(
            {"ok": False, "error": {"kind": "validation", "message": "unknown refresh-check verb"}}, status=200)
    args = {"agent": body.get("agent", "claude")} if verb == "agent_refresh_check" else {}
    status, reply = await _relay(request, verb, args)
    return web.json_response(reply, status=status)


async def broker_software_active_handler(request: web.Request) -> web.Response:
    """GET /broker/software/active — every build in flight, {ops:[{op_id, verb, target,
    agent?, state}]} (gated). The page seeds its "pulling…" labels from this after a reload."""
    status, body = await _relay(request, "build_active")
    return web.json_response(body, status=status)


async def broker_lifecycle_handler(request: web.Request) -> web.Response:
    """GET /broker/lifecycle — every operation in flight,
    {ops:[{op_id, project, verb, state}]} (gated). The browser seeds its
    "starting…" labels from this after a reload."""
    status, body = await _relay(request, "lifecycle_active")
    return web.json_response(body, status=status)


async def broker_mode_handler(request: web.Request) -> web.Response:
    """GET /broker/mode — {mode: "sysbox" | "privileged" | null}, the way clanker-learn runs,
    for Settings and the line under the project list (gated read)."""
    status, body = await _relay(request, "mode")
    return web.json_response(body, status=status)


# ---- Settings -> Backup ---------------------------------------------------------------

# A backup's name as the core makes it, matched whole: the browser's name reaches no path
# here; the file opened is the one the broker names, matched against its own rule.
_BACKUP_NAME_RE = re.compile(r"cl-backup-[0-9]{8}T[0-9]{6}Z\.tar\.gz")
_DOWNLOAD_LINK_RE = re.compile(r"[0-9a-f]{16}-(cl-backup-[0-9]{8}T[0-9]{6}Z\.tar\.gz)")
DOWNLOADS_DIR = DATA_DIR / "downloads"   # the broker links a backup here for one download
DOWNLOAD_CHUNK = 256 * 1024
# A project file the core made for one download (outer/core.py's PROJECT_FILE_LINK).
_PROJECT_FILE_LINK_RE = re.compile(r"[0-9a-f]{16}-([a-z0-9-]{1,31}-[0-9]{8}\.clproject)")


async def broker_backups_handler(request: web.Request) -> web.Response:
    """GET /broker/backups — every backup, newest first: [{backup, size, made_at, kind,
    projects, running} or {backup, size, problem}] (gated read)."""
    status, body = await _relay(request, "backup_list")
    return web.json_response(body, status=status)


async def broker_backup_make_handler(request: web.Request) -> web.Response:
    """POST /broker/backups — make a backup (gated, origin-checked). An operation that belongs
    to no project, as a build: {ok, op_id} at once, followed at /broker/op/<id>/log and
    /broker/op/<id>; its last record's msg is the core's sentence (name, size, time, and
    whether running projects may have left torn files). Another backup under way: busy."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    return await _lane_relay(request, "backup", {}, "backups")


async def broker_backup_active_handler(request: web.Request) -> web.Response:
    """GET /broker/backups/active — the backup in flight, {ops:[{op_id, verb, target, state}]}
    (gated), for the page to find it again after a reload."""
    status, body = await _relay(request, "backup_active")
    return web.json_response(body, status=status)


async def broker_backup_remove_handler(request: web.Request) -> web.Response:
    """POST /broker/backups/remove {backup, proof} — remove a backup (gated, origin-checked,
    step-up: the proof of the re-typed password, as destroy has it)."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    args = {"backup": body.get("backup")}
    if isinstance(body.get("proof"), str):
        args["proof"] = body["proof"]
    status, reply = await _relay(request, "backup_rm", args)
    return web.json_response(reply, status=status)


def _open_download(path: Path):
    """Open the link the broker made and remove it at once: the open file is all that is
    needed, and no link is left behind by a download that is cut short. Blocking."""
    fh = open(path, "rb")  # noqa: SIM115 - closed by the handler once it has been sent
    with contextlib.suppress(OSError):
        os.remove(path)
    return fh


# The one-time links handed out after the password was checked again: link -> (the login
# that asked, when it lapses). A link serves one download, to that login only; the broker
# clears a link file nobody took after core.DOWNLOAD_LINK_S.
DOWNLOAD_LINK_S = 600
_DOWNLOADS: dict[str, tuple[str, float]] = {}


async def broker_backup_download_handler(request: web.Request) -> web.Response:
    """POST /broker/backups/{backup}/download {proof} — ask for a backup's download (gated,
    origin-checked, step-up: an archive holds the password's hash, the Claude token and every
    project's keys, so the password is typed again, as for removing one). The broker checks
    the proof and links the one file here; the answer is {ok, url}: a one-time address of this
    login's, which the page then opens as a plain download (the page's content-security
    policy has form-action 'none', and an archive of gigabytes is never held in the page)."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    name = request.match_info.get("backup", "")
    body = await _json_body(request)
    if body is None or _broker_session(request) is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    if not _BACKUP_NAME_RE.fullmatch(name):
        return _bad_request()
    args = {"backup": name}
    if isinstance(body.get("proof"), str):
        args["proof"] = body["proof"]
    status, reply = await _relay(request, "backup_fetch", args)
    if status != 200 or not reply.get("ok"):
        return web.json_response(reply, status=status)
    link = str(reply.get("result", {}).get("file", ""))
    found = _DOWNLOAD_LINK_RE.fullmatch(link)
    if not found or found.group(1) != name:
        return web.json_response({"ok": False, "error": {"kind": "failed", "message": "the broker named no file to send"}}, status=500)
    _hand_out(request, link)
    return web.json_response({"ok": True, "url": f"/broker/backups/file/{link}", "size": reply["result"].get("size")})


def _hand_out(request: web.Request, link: str) -> None:
    """Register a link the broker made as one download for this login, lapsed ones dropped."""
    now = time.time()
    for old in [k for k, (_, until) in _DOWNLOADS.items() if until < now]:
        _DOWNLOADS.pop(old, None)
    _DOWNLOADS[link] = (request.cookies.get(BROKER_COOKIE, ""), now + DOWNLOAD_LINK_S)


async def broker_project_export_download_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/export-download {op_id} — the file a finished export of this
    project made, as a one-time address of this login's (gated, origin-checked). The file is
    the one the broker's record of that operation names, matched against the core's rule."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None or _broker_session(request) is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    name = request.match_info.get("name", "")
    op_id = body.get("op_id")
    if not isinstance(op_id, str) or not _OP_ID_RE.fullmatch(op_id):
        return _bad_request()
    status, reply = await _relay(request, "op", {"op_id": op_id})
    if status != 200 or not reply.get("ok"):
        return web.json_response(reply, status=status)
    op = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    made = op.get("result") if isinstance(op.get("result"), dict) else {}
    link = made.get("file")
    if (op.get("state") != "done" or op.get("verb") != "project_export" or op.get("project") != name
            or not isinstance(link, str) or not _PROJECT_FILE_LINK_RE.fullmatch(link)):
        return web.json_response({"ok": False, "error": {"kind": "validation", "message": f"That operation made no project file of {name} to download."}},
                                 status=400)
    _hand_out(request, link)
    return web.json_response({"ok": True, "url": f"/broker/project-file/file/{link}", "size": made.get("size")})


async def broker_backup_file_handler(request: web.Request) -> web.StreamResponse:
    """GET /broker/backups/file/{link} and /broker/project-file/file/{link} — the archive or
    the project file, as a file to save (gated): the one-time address a download POST gave
    this login. Its file is opened and its link removed at once, so a second request finds
    nothing."""
    link = request.match_info.get("link", "")
    if _broker_session(request) is None:
        return _unauthorized()
    found = _DOWNLOAD_LINK_RE.fullmatch(link) or _PROJECT_FILE_LINK_RE.fullmatch(link)
    given = _DOWNLOADS.pop(link, None) if found else None
    if given is None or given[1] < time.time() or not secrets.compare_digest(given[0], request.cookies.get(BROKER_COOKIE, "")):
        return web.json_response({"ok": False, "error": {"kind": "not_found", "message": "That download link is used or lapsed: ask for the download again."}},
                                 status=404)
    try:
        fh = await asyncio.to_thread(_open_download, DOWNLOADS_DIR / link)
    except OSError as e:
        return web.json_response({"ok": False, "error": {"kind": "io_error", "message": e.strerror or "open failed"}}, status=500)
    try:
        response = web.StreamResponse(headers={
            "Content-Type": "application/gzip", "Content-Length": str(os.fstat(fh.fileno()).st_size),
            "Content-Disposition": f'attachment; filename="{found.group(1)}"', "Cache-Control": "no-store"})
        await response.prepare(request)
        while chunk := await asyncio.to_thread(fh.read, DOWNLOAD_CHUNK):
            await response.write(chunk)
        await response.write_eof()
        return response
    finally:
        fh.close()


# ---- a project file taken in, for an import -------------------------------------------

UPLOADS_DIR = DATA_DIR / "uploads"   # the core takes <upload>.clproject from here (the outer container's /data/web/uploads)
UPLOAD_KEEP_S = 3600                 # an upload never imported is removed at a later upload
MANIFEST_MAX = 1024 * 1024
PROJECT_FILE_FORMAT = "clanker-learn project"
NOT_A_PROJECT_FILE = "This is not a clanker-learn project file: pick a .clproject file that a project's Export made."


def _uploads_ready() -> None:
    """The folder of uploads made, and the uploads older than UPLOAD_KEEP_S removed. Blocking."""
    UPLOADS_DIR.mkdir(mode=0o700, exist_ok=True)
    now = time.time()
    for left in UPLOADS_DIR.iterdir():
        with contextlib.suppress(OSError):
            if now - left.lstat().st_mtime > UPLOAD_KEEP_S:
                left.unlink()


def _upload_manifest(path: Path) -> dict | None:
    """The project file's manifest, its first member, read without reading further; None when
    it is not a clanker-learn project file. Blocking."""
    try:
        with tarfile.open(path, "r|gz") as tar:
            first = tar.next()
            if first is None or first.name != "manifest.json" or not first.isreg() or first.size > MANIFEST_MAX:
                return None
            manifest = json.loads(tar.extractfile(first).read())
    except (OSError, EOFError, tarfile.TarError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    return manifest if isinstance(manifest, dict) and manifest.get("format") == PROJECT_FILE_FORMAT else None


async def broker_project_file_handler(request: web.Request) -> web.Response:
    """POST /broker/project-file — a project file to import, the raw file as the body (gated,
    origin-checked). Streamed to the folder of uploads (aiohttp's body limit applies to a
    body read whole, not to one read as a stream), its manifest checked; the answer is
    {ok, upload, manifest}, the upload's id being what POST /broker/project-import names."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    if _broker_session(request) is None:
        return _unauthorized()
    await asyncio.to_thread(_uploads_ready)
    upload = secrets.token_hex(16)
    part, path = UPLOADS_DIR / f".{upload}.part", UPLOADS_DIR / f"{upload}.clproject"
    fh = await asyncio.to_thread(open, part, "xb")
    try:
        try:
            while chunk := await request.content.read(DOWNLOAD_CHUNK):
                await asyncio.to_thread(fh.write, chunk)
        finally:
            fh.close()
        await asyncio.to_thread(os.rename, part, path)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
    manifest = await asyncio.to_thread(_upload_manifest, path)
    if manifest is None:
        with contextlib.suppress(OSError):
            path.unlink()
        return web.json_response({"ok": False, "error": {"kind": "validation", "message": NOT_A_PROJECT_FILE}}, status=400)
    return web.json_response({"ok": True, "upload": upload, "manifest": manifest})


def _read_from(path: Path, frm: int) -> tuple[bytes, int]:
    """The file's bytes from `frm` to its end, and where its end is. Blocking: run in a thread."""
    with open(path, "rb") as f:
        f.seek(frm)
        chunk = f.read()
        return chunk, f.tell()


async def broker_op_log_handler(request: web.Request) -> web.Response:
    """GET /broker/op/{op_id}/log?from=<n> — the progress log's byte-slice since `n`
    plus the new EOF. Reads the read-only-mounted file directly, gated by the login
    session cookie. A not-yet-created file is "0 bytes, not started" — never a 404."""
    if _broker_session(request) is None:
        return _unauthorized()
    op_id = request.match_info.get("op_id", "")
    if not _OP_ID_RE.fullmatch(op_id):
        return _bad_request()
    try:
        frm = max(0, int(request.query.get("from", "0")))
    except ValueError:
        frm = 0
    path = Path(BROKER_OPLOG_DIR) / f"{op_id}.view.log"
    if not path.exists():
        return web.json_response(
            {"ok": True, "from": frm, "next": 0, "data": "", "started": False})
    try:
        chunk, nxt = await asyncio.to_thread(_read_from, path, frm)
    except OSError as e:
        return web.json_response(
            {"ok": False, "error": {"kind": "io_error", "message": e.strerror or "read failed"}},
            status=500)
    # The log is JSON lines, each written whole, so every EOF is a line boundary.
    return web.json_response({
        "ok": True, "from": frm, "next": nxt,
        "data": chunk.decode("utf-8", "replace"), "started": True})


async def broker_op_status_handler(request: web.Request) -> web.Response:
    """GET /broker/op/{op_id} — how the operation stands, as the broker says it:
    {state: running|done|failed, project, verb, msg} (gated). RS answers this from its
    own memory; here the broker's log is the one source."""
    op_id = request.match_info.get("op_id", "")
    if not _OP_ID_RE.fullmatch(op_id):
        return _bad_request() if _broker_session(request) else _unauthorized()
    status, body = await _relay(request, "op", {"op_id": op_id})
    result = body.get("result") if status == 200 and body.get("ok") else None
    if isinstance(result, dict) and result.get("state") == "done" and op_id in _DESTROYS:
        told = await _destroy_told(op_id)
        if told is not None:
            body = {**body, "result": {**result, "knowledge": told}}
    return web.json_response(body, status=status)


async def broker_op_alive_handler(request: web.Request) -> web.Response:
    """GET /broker/op/{op_id}/alive — {alive}: whether the operation's child still
    runs (relays build_alive), so that a follower does not wait for ever on one
    that was killed before its last record."""
    op_id = request.match_info.get("op_id", "")
    if not _OP_ID_RE.fullmatch(op_id):
        return _bad_request() if _broker_session(request) else _unauthorized()
    status, body = await _relay(request, "build_alive", {"op_id": op_id})
    if status != 200 or not body.get("ok"):
        return web.json_response(body, status=status)
    return web.json_response(
        {"ok": True, "alive": bool(body.get("result", {}).get("alive", False))})


# ---- the terminal: a WebSocket relayed to the sandbox's sshd ---------------------

# One tmux session per project: a second tab, or a reload, picks up the same shell.
TERMINAL_COMMAND = "tmux new-session -A -s main"
TERM_MIN, TERM_MAX = 1, 500
# No trusted keys at all, so asyncssh asks the validator about every host key. RS passes
# `known_hosts=None`, which makes asyncssh skip host-key checking altogether: the validator is
# then never called and nothing is pinned.
PINNED_ONLY = ([], [], [])


class HostKeyValidator(asyncssh.SSHClient):
    """Capture host key during handshake; reject if it doesn't match expected.
    Unlike RS, no expected fingerprint rejects the key: never trust on first use."""

    def __init__(self, expected_fp: str | None):
        super().__init__()
        self.expected_fp = expected_fp
        self.actual_fp: str | None = None

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        self.actual_fp = key.get_fingerprint("sha256")
        if not self.expected_fp:
            return False
        return self.actual_fp == self.expected_fp


async def ssh_connect(target: dict) -> asyncssh.SSHClientConnection:
    """Connect to the sandbox `attach` named, as it said, its host key pinned to the
    fingerprint it gave."""
    validator = HostKeyValidator(target["fingerprint"])
    return await asyncssh.connect(
        host=target["host"], port=int(target["port"]),
        username=target["username"], password=target["password"],
        client_factory=lambda: validator,
        known_hosts=PINNED_ONLY,
        client_keys=None,
        connect_timeout=10,
    )


def _term_dim(value, default: int) -> int:
    """A row or column count from the browser, as an int within TERM_MIN..TERM_MAX."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(TERM_MIN, min(TERM_MAX, n))


async def _ws_say(ws: web.WebSocketResponse, message: dict) -> None:
    """Send one control message; a socket a logout closed meanwhile takes none."""
    with contextlib.suppress(ConnectionResetError, RuntimeError):
        await ws.send_json(message)


async def ws_handler(request: web.Request) -> web.WebSocketResponse | web.Response:
    """GET /broker/project/{name}/terminal — a terminal in the project's sandbox (RS's
    /ws/<project>/<service>, under /broker so the login cookie comes with it).

    The browser's first frame is {type: "connect", rows, cols}; only the size is read from it.
    Where to connect, as whom, with which password and which host key come from the broker's
    `attach`, asked with this login's token. Then {type: "connected"} and RS's relay: binary
    frames both ways, {type: "resize", rows, cols} from the browser."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    s = _broker_session(request)
    if s is None:
        return _unauthorized()
    login = request.cookies.get(BROKER_COOKIE)
    name = request.match_info.get("name", "")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    # Remembered under its login, which closes it when it ends.
    TERMINALS.setdefault(login, set()).add(ws)
    try:
        if login in BROKER_SESSIONS:
            await _terminal(ws, name, s["broker_token"], login)
    finally:
        mine = TERMINALS.get(login)
        if mine is not None:
            mine.discard(ws)
            if not mine:
                TERMINALS.pop(login, None)
        if not ws.closed:
            await ws.close()
    return ws


async def _terminal(ws: web.WebSocketResponse, name: str, broker_token: str, login: str) -> None:
    """The terminal's life after the upgrade; the caller closes the socket."""
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=10)
    except TimeoutError:
        return
    if first.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
        return
    if first.type != WSMsgType.TEXT:
        await _ws_say(ws, {"type": "error", "msg": "expected JSON connect message"})
        return
    try:
        connect = json.loads(first.data)
    except json.JSONDecodeError:
        await _ws_say(ws, {"type": "error", "msg": "invalid JSON"})
        return
    if not isinstance(connect, dict) or connect.get("type") != "connect":
        await _ws_say(ws, {"type": "error", "msg": "first message must be type=connect"})
        return
    # The size is all the browser says here: a host, user, password or fingerprint it sends
    # is not read.
    rows = _term_dim(connect.get("rows"), 24)
    cols = _term_dim(connect.get("cols"), 80)

    try:
        reply = await broker_call("attach", {"name": name}, token=broker_token)
    except BrokerUnavailable:
        await _ws_say(ws, {"type": "error", "msg": "broker unavailable"})
        return
    except BrokerForbidden:
        await _ws_say(ws, {"type": "error", "msg": "the broker refused this server"})
        return
    if not reply.get("ok"):
        error = reply.get("error") or {}
        if error.get("kind") == "unauthorized":
            _drop_login(login)
        await _ws_say(ws, {"type": "error", "msg": error.get("message") or error.get("kind") or "refused"})
        return
    target = reply.get("result") or {}
    password = target.get("password")
    if not target.get("fingerprint"):
        # Nothing to hold the host key to: no connection at all, rather than trust what answers.
        await _ws_say(ws, {"type": "error", "msg": "the sandbox's host key is not known"})
        return
    if not target.get("host") or not isinstance(password, str) or not password:
        await _ws_say(ws, {"type": "error", "msg": "the broker named no sandbox to connect to"})
        return
    if ws.closed:   # the login ended while the broker answered
        return

    try:
        conn = await ssh_connect(target)
    except asyncssh.HostKeyNotVerifiable:
        # The actual fingerprint stays here (RS sends it): the browser has no use for it.
        await _ws_say(ws, {"type": "fingerprint_mismatch"})
        return
    except asyncssh.PermissionDenied:
        await _ws_say(ws, {"type": "auth_failed"})
        return
    except Exception as e:  # noqa: BLE001 - any failure reads the same to the browser
        detail = f"{type(e).__name__}: {e}".replace(password, "<password>")
        log.warning(f"terminal of {name}: SSH connect to {target.get('host')}:{target.get('port')} failed: {detail}")
        await _ws_say(ws, {"type": "error", "msg": "connect failed"})
        return
    if ws.closed:   # the login ended while connecting
        conn.close()
        return

    await _ws_say(ws, {"type": "connected"})

    async with conn:
        proc = await conn.create_process(
            term_type="xterm-256color",
            term_size=(cols, rows),
            command=TERMINAL_COMMAND,
            encoding=None,
        )

        async def from_browser():
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    proc.stdin.write(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        ctrl = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                        proc.change_terminal_size(
                            width=_term_dim(ctrl.get("cols"), cols),
                            height=_term_dim(ctrl.get("rows"), rows),
                        )

        async def to_browser():
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                await ws.send_bytes(chunk)

        _done, pending = await asyncio.wait(
            [asyncio.create_task(from_browser()),
             asyncio.create_task(to_browser())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        try:
            proc.terminate()
        except OSError:
            pass


# ---- the project session: the cookie the web-app tabs will be opened with ---------

def _session_valid(token: str, project: str) -> bool:
    """Look up `token` in SESSIONS, validate project + TTL. Garbage-collects
    expired entries on lookup."""
    s = SESSIONS.get(token)
    if not s:
        return False
    if s["project"] != project:
        return False
    if time.time() > s["expires"]:
        SESSIONS.pop(token, None)
        return False
    return True


async def session_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/session — issue the project's session cookie (gated,
    origin-checked). RS proves here that the browser holds the project's SSH password; in
    clanker-learn the login is the credential, and the broker's `status`, asked with its
    token, says the project exists.

    The cookie is host-only with Path=/ : cookies ignore the port, so the one cookie reaches
    every port the project's web apps will be served on. It lives at most 8 hours, and no
    longer than the login it was minted under, which ends it too. Answers {ok: true}, or the
    broker's refusal as it said it."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    project = request.match_info.get("name", "")
    status, reply = await _relay(request, "status", {"name": project})
    if status != 200 or not reply.get("ok"):
        return web.json_response(reply, status=status)
    login = request.cookies.get(BROKER_COOKIE)
    s = BROKER_SESSIONS.get(login)
    ttl = int(min(SESSION_TTL_SECONDS, s["expires"] - time.time())) if s else 0
    if ttl <= 0:   # the login ended, or is ending, while the broker answered
        return _unauthorized()

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "project": project,
        "expires": time.time() + ttl,
        "login": login,
    }
    response = web.json_response({"ok": True})
    response.set_cookie(
        services.session_cookie_name(WEB_PORT, project), token,
        path="/",
        httponly=True, secure=True, samesite="Strict",
        max_age=ttl,
    )
    return response


# ===========================================================================
# Web apps of the sandboxes (origin ports) and ports named by hand (pass-through)
#
# Both tables are this server's own state, kept in its data folder: no request or
# connection path asks the broker for them. Only where a sandbox is comes from the broker,
# remembered a few seconds.
# ===========================================================================

FILES_SERVER_PORT = 7070    # the sandbox's files server: always there in a running sandbox
EDITOR_PORT = 8443          # the sandbox's editor (code-server): started with the sandbox, like the files server
# project -> (monotonic time the broker was asked, the sandbox's address or None).
SANDBOX_ADDRESSES: dict[str, tuple[float, str | None]] = {}
ADDRESS_TTL_S = 5.0
# A connect that fails at a remembered address asks the broker again, but at most once a
# second per project: an app that is down must not turn every request into a broker call.
ADDRESS_RECHECK_S = 1.0
PASSTHROUGH_CONNECT_TIMEOUT_S = 10
# origin port -> {"project", "port": the sandbox's port}. (RS keys an entry by service id
# and re-derives the port per request from files it reads; here the port is checked when
# the tab is opened, by the broker, and kept.)
ORIGIN_PORTS: dict[int, dict] = {}
# host port -> {"project", "port": the sandbox's port}: the raw TCP ports named by hand.
PASSTHROUGH_CLAIMS: dict[int, dict] = {}

HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",
})
# Response-direction drop set: hop-by-hop PLUS X-Frame-Options. A per-app origin port is
# cross-origin to the page, which frames it; the proxied content is session-gated and framed
# only by our own page, so the clickjacking protection XFO provided has no meaning inside
# that boundary.
RESPONSE_DROP_HEADERS = HOP_BY_HOP_HEADERS | frozenset({"x-frame-options"})
# clanker-learn's own cookies (services.broker_cookie_name, services.session_cookie_name).
# They never go upstream and upstream cannot set them: the login cookie has Path=/broker,
# and a page served from a sandbox can make the browser ask /broker/... on its origin port.
OWN_COOKIE_PREFIXES = ("cl_broker_", "cl_session_")


def _is_port(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _save_json(path: Path, data: dict) -> None:
    """Write atomically (a temporary file, then a rename). A failure is logged: the table
    still works in memory, but is lost at the next start."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(p): e for p, e in sorted(data.items())}, indent=2))
        tmp.replace(path)
    except OSError as e:
        log.error(f"cannot keep {path.name}: {e}")


def _load_table(path: Path, low: int, high: int) -> dict[int, dict]:
    """{port: {"project", "port"}} from `path`, the sound entries on a port of low..high only.
    A missing or unreadable file is an empty table."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[int, dict] = {}
    for key, entry in (data.items() if isinstance(data, dict) else ()):
        if (key.isascii() and key.isdigit() and low <= int(key) <= high and isinstance(entry, dict)
                and isinstance(entry.get("project"), str) and entry["project"] and _is_port(entry.get("port"))):
            out[int(key)] = {"project": entry["project"], "port": entry["port"]}
    return out


def _origin_table_load() -> None:
    ORIGIN_PORTS.clear()
    ORIGIN_PORTS.update(_load_table(ORIGIN_PORTS_FILE, *ORIGIN_RANGE))


def _claims_load() -> None:
    PASSTHROUGH_CLAIMS.clear()
    PASSTHROUGH_CLAIMS.update(_load_table(PASSTHROUGH_FILE, *PASSTHROUGH_RANGE))


def _origin_port_lookup(project: str, port: int) -> int | None:
    """The origin port ALREADY given to (project, port), or None. Never allocates."""
    for origin, entry in ORIGIN_PORTS.items():
        if entry["project"] == project and entry["port"] == port:
            return origin
    return None


def _origin_port_for(project: str, port: int) -> int | None:
    """The stable origin port for (project, port), the lowest free one on first sight; None
    when the range is exhausted. MUST stay synchronous (no awaits): a read-modify-write that
    spans one would give one port twice under concurrent requests."""
    held = _origin_port_lookup(project, port)
    if held is not None:
        return held
    low, high = ORIGIN_RANGE
    for origin in range(low, high + 1):
        if origin not in ORIGIN_PORTS:
            ORIGIN_PORTS[origin] = {"project": project, "port": port}
            _save_json(ORIGIN_PORTS_FILE, ORIGIN_PORTS)
            return origin
    log.error(f"origin-port range {low}-{high} exhausted ({project}:{port} gets no origin)")
    return None


def _claims_take(project: str, entries: list[dict]) -> None:
    """Make `project`'s claims the raw TCP entries of its port list as the broker just gave
    it. The broker gives a host port to one project at most, so another project's claim on
    the same host port is stale and is replaced."""
    claims = {hp: c for hp, c in PASSTHROUGH_CLAIMS.items() if c["project"] != project}
    for e in entries:
        if e.get("passthrough") is True and _is_port(e.get("host_port")):
            claims[e["host_port"]] = {"project": project, "port": e["port"]}
    if claims != PASSTHROUGH_CLAIMS:
        PASSTHROUGH_CLAIMS.clear()
        PASSTHROUGH_CLAIMS.update(claims)
        _save_json(PASSTHROUGH_FILE, PASSTHROUGH_CLAIMS)


def _forget_project(project: str) -> None:
    """Drop the origin ports and the claims of a project the broker no longer knows."""
    for table, path in ((ORIGIN_PORTS, ORIGIN_PORTS_FILE), (PASSTHROUGH_CLAIMS, PASSTHROUGH_FILE)):
        stale = [p for p, e in table.items() if e["project"] == project]
        for p in stale:
            del table[p]
        if stale:
            _save_json(path, table)


async def _project_gone(project: str) -> bool:
    """Whether the broker's open `list` no longer names the project. Not knowing is not gone."""
    try:
        reply = await broker_call("list")
    except (BrokerUnavailable, BrokerForbidden):
        return False
    if not reply.get("ok") or not isinstance(reply.get("result"), list):
        return False
    return project not in {p.get("name") for p in reply["result"] if isinstance(p, dict)}


async def sandbox_address(project: str, failed: str | None = None) -> str | None:
    """Where the project's sandbox is: its address on the project's network as the broker's
    OPEN `status` says it (no token: the proxy and the relay have no login in hand), or None
    when the project does not exist or does not run. Remembered ADDRESS_TTL_S seconds, since
    the broker serves one request at a time. `failed` is an address a connect just failed
    at: the broker is asked again unless it said that address within ADDRESS_RECHECK_S. The
    address comes from the broker only, never from a request."""
    held = SANDBOX_ADDRESSES.get(project)
    if held is not None:
        age = time.monotonic() - held[0]
        if failed is None and age < ADDRESS_TTL_S:
            return held[1]
        if failed is not None and (held[1] != failed or age < ADDRESS_RECHECK_S):
            return held[1]
    address = None
    try:
        reply = await broker_call("status", {"name": project})
    except (BrokerUnavailable, BrokerForbidden):
        reply = None
    if reply is not None and reply.get("ok"):
        result = reply.get("result") or {}
        if result.get("state") == "running" and isinstance(result.get("address"), str):
            with contextlib.suppress(ValueError):
                address = str(ipaddress.ip_address(result["address"]))
    elif reply is not None and await _project_gone(project):
        _forget_project(project)
    SANDBOX_ADDRESSES[project] = (time.monotonic(), address)
    return address


def _refusal(kind: str, message: str) -> web.Response:
    """A refusal in the broker's shape, answered like the broker's own (200)."""
    return web.json_response({"ok": False, "error": {"kind": kind, "message": message}})


def _port_entries(reply: dict) -> list[dict]:
    """The sound entries of a port_list / port_add / port_remove reply."""
    entries = (reply.get("result") or {}).get("ports")
    return [e for e in (entries if isinstance(entries, list) else [])
            if isinstance(e, dict) and _is_port(e.get("port")) and isinstance(e.get("label"), str)]


def _detected_rows(reply: dict) -> list[dict] | None:
    """The rows of a port_detect reply, or None when the broker refused (the sandbox does not
    run)."""
    if not reply.get("ok"):
        return None
    rows = (reply.get("result") or {}).get("ports")
    return [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and _is_port(r.get("port"))]


def _assemble_services(project: str, entries: list[dict], detected: list[dict] | None, editor: bool = True) -> dict:
    """The project's tabs, in RS's shape (id -> row, in tab order). Of RS's rows only the
    kinds clanker-learn has: the editor, the files server, web apps named by hand, detected apps, raw TCP
    ports. A web row carries its origin port once the app was opened (never allocated here)."""
    def web_row(port: int, label: str, **extra) -> dict:
        row = {"label": label, "kind": "http", "always_on": False, "renderer": "iframe",
               "default_port": port, "upstream_path": "/", **extra}
        origin = _origin_port_lookup(project, port)
        if origin is not None:
            row["origin_port"] = origin
        return row

    out: dict[str, dict] = {}
    if detected is not None:
        if editor:   # only a project given the editor has the Editor tab
            out["editor"] = web_row(EDITOR_PORT, "Editor", always_on=True)
        out["files"] = web_row(FILES_SERVER_PORT, "Files", always_on=True)
    named = {e["port"] for e in entries}
    for e in entries:
        if not e.get("passthrough"):
            out[f"port-{e['port']}"] = web_row(e["port"], e["label"])
    for r in detected or []:
        if r["port"] not in named and r["port"] not in (EDITOR_PORT, FILES_SERVER_PORT):
            label = r["label"] if isinstance(r.get("label"), str) else str(r["port"])
            out[f"port-{r['port']}"] = web_row(r["port"], label, detected=True)
    # Raw TCP: a launcher keyed on its host port, no origin port.
    for e in entries:
        if e.get("passthrough") is True and _is_port(e.get("host_port")):
            out[f"pass-{e['host_port']}"] = {
                "label": e["label"], "kind": "http", "always_on": False, "renderer": "launcher",
                "default_port": e["host_port"], "passthrough_port": e["host_port"], "sandbox_port": e["port"]}
    return out


async def project_services_handler(request: web.Request) -> web.Response:
    """GET /broker/project/{name}/services — the project's tabs (gated): {ok, services: {id:
    row}}. The ports named by hand from `port_list`, what listens now from `port_detect`, both
    asked with the login's token when the list is asked for (RS's probing, caching, sweeps and
    switches are not kept). A project that does not run has its named entries only."""
    project = request.match_info.get("name", "")
    status, listed = await _relay(request, "port_list", {"name": project})
    if status != 200 or not listed.get("ok"):
        return web.json_response(listed, status=status)
    entries = _port_entries(listed)
    _claims_take(project, entries)
    status, detected = await _relay(request, "port_detect", {"name": project})
    if status != 200:
        return web.json_response(detected, status=status)
    rows = _detected_rows(detected)
    editor = rows is not None and await _has_editor(request, project)
    return web.json_response(
        {"ok": True, "services": _assemble_services(project, entries, rows, editor)})


async def _has_editor(request: web.Request, project: str) -> bool:
    """Whether the project was given the editor, as the broker's `status` says."""
    status, reply = await _relay(request, "status", {"name": project})
    return status == 200 and bool(reply.get("ok")) and (reply.get("result") or {}).get("editor") is True


async def project_origin_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/origin {port} — open a web app (gated, origin-checked):
    give (project, port) an origin port, the one it has or the lowest free one, and answer
    {ok, origin_port}. The port must be the editor's, the files server's, a web port named by hand or one
    listening now, as the broker says."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    if _broker_session(request) is None:
        return _unauthorized()
    body = await _json_body(request)
    if body is None:
        return _bad_request()
    project = request.match_info.get("name", "")
    port = body.get("port")
    status, listed = await _relay(request, "port_list", {"name": project})
    if status != 200 or not listed.get("ok"):
        return web.json_response(listed, status=status)
    entries = _port_entries(listed)
    _claims_take(project, entries)
    if not _is_port(port):
        return _refusal("validation", "port must be a port number from 1 to 65535")
    allowed = (port == FILES_SERVER_PORT or (port == EDITOR_PORT and await _has_editor(request, project))
               or port in {e["port"] for e in entries if not e.get("passthrough")})
    if not allowed:
        status, detected = await _relay(request, "port_detect", {"name": project})
        if status != 200:
            return web.json_response(detected, status=status)
        allowed = port in {r["port"] for r in _detected_rows(detected) or []}
    if not allowed:
        return _refusal("validation", f"port {port} of {project} is not the editor, not the files server, not named by hand and not listening")
    origin = _origin_port_for(project, port)
    if origin is None:
        low, high = ORIGIN_RANGE
        return _refusal("failed", f"every origin port ({low}-{high}) is taken")
    return web.json_response({"ok": True, "origin_port": origin})


async def _port_relay(request: web.Request, verb: str, args: dict) -> web.Response:
    """Relay a port verb; the claims follow the project's entries in the broker's reply."""
    project = request.match_info.get("name", "")
    status, reply = await _relay(request, verb, dict(args, name=project))
    if status == 200 and reply.get("ok"):
        _claims_take(project, _port_entries(reply))
    return web.json_response(reply, status=status)


async def broker_port_list_handler(request: web.Request) -> web.Response:
    """GET /broker/project/{name}/ports — the ports named by hand (gated), as `port_list`."""
    return await _port_relay(request, "port_list", {})


async def broker_port_add_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/ports {port, label, passthrough?, host_port?} — name a port
    by hand (gated, origin-checked); only those fields are forwarded."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    return await _port_relay(request, "port_add", _forward(body, "port_add"))


async def broker_port_remove_handler(request: web.Request) -> web.Response:
    """POST /broker/project/{name}/ports/remove {port, passthrough?} — take a port named by
    hand out (gated, origin-checked); for raw TCP `port` is the host port."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    body = await _json_body(request)
    if body is None:
        return _bad_request() if _broker_session(request) else _unauthorized()
    return await _port_relay(request, "port_remove", _forward(body, "port_remove"))


# ---- the tutor: a relay to the tutor service (cl-tutor) -----------------------------
#
# The tutor service has no login of its own: it trusts this server's session check and a
# secret the two share (a file mounted into both). Nothing of the browser's request goes
# upstream but the method, the route's own path (built here from parts checked against
# _TUTOR_ID_RE), the query and a JSON object; no cookie, no other header. The host is
# CL_TUTOR_URL's, never the request's. The event stream of a run is passed through as it
# comes, as _proxy_http passes an app's body (RS's streaming proxy, cut to one GET).

TUTOR_URL = URL(os.environ.get("CL_TUTOR_URL", "http://cl-tutor:7801"))
TUTOR_SECRET_FILE = Path("/run/cl-secret/tutor")
TUTOR_CALL_TIMEOUT_S = 60
# What the tutor service makes ids of: letters, digits, dashes. Matched whole.
_TUTOR_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")


def _tutor_unavailable() -> web.Response:
    return web.json_response(
        {"ok": False, "error": {"kind": "unavailable", "message": "The tutor service is not running."}},
        status=502)


def _tutor_secret() -> str | None:
    """The secret shared with the tutor service, read at every request (the file may come
    after this server starts), or None when there is none. Never logged, never answered."""
    try:
        secret = TUTOR_SECRET_FILE.read_text().strip()
    except OSError:
        return None
    return secret or None


async def _tutor_gate(request: web.Request) -> web.Response | None:
    """The checks every tutor route makes before anything else, in the order the other
    routes make them: the origin for anything but GET, then the login. None when both pass."""
    if request.method != "GET" and not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    if _broker_session(request) is None:
        return _unauthorized()
    return None


async def _tutor_call(method: str, path: str, *, query=None, body: dict | None = None) -> web.Response:
    """One call to the tutor service, its answer passed back with its status and JSON body.
    The service down, absent, or refusing the secret is the contract's 502."""
    got = await _tutor_fetch(method, path, query=query, body=body)
    if got is None:
        return _tutor_unavailable()
    return web.json_response(got[1], status=got[0])


async def _tutor_fetch(method: str, path: str, *, query=None, body: dict | None = None) -> tuple[int, dict] | None:
    """One call to the tutor service: (status, its JSON answer), or None when it is down,
    absent, or refuses the secret."""
    secret = _tutor_secret()
    if secret is None:
        return None
    url = TUTOR_URL.with_path(path).with_query(query or {})
    timeout = ClientTimeout(total=TUTOR_CALL_TIMEOUT_S, sock_connect=PASSTHROUGH_CONNECT_TIMEOUT_S)
    try:
        async with ClientSession(timeout=timeout) as sess, sess.request(
                method, url, headers={"X-CL-Secret": secret}, allow_redirects=False,
                **({"json": body} if body is not None else {})) as upstream:
            status = upstream.status
            text = await upstream.text()
    except Exception as e:  # noqa: BLE001 - whatever went wrong on the way, the service is not there
        log.info(f"tutor service: {method} {path} failed: {type(e).__name__}")
        return None
    if status == 401:
        # The service does not take this server's secret: nothing the browser can mend, and
        # not the login's 401 either.
        log.warning("tutor service: the shared secret was refused")
        return None
    try:
        answer = json.loads(text)
    except ValueError:
        return None
    return (status, answer) if isinstance(answer, dict) else None


async def _tutor_body(request: web.Request) -> dict | None | web.Response:
    """The request's JSON object to forward, None when it sent no body, or a 400. Bounded by
    aiohttp's request size limit (1 MiB; a larger body is its 413)."""
    if not request.body_exists:
        return None
    body = await _json_body(request)
    return _bad_request() if body is None else body


async def tutor_project_lessons_handler(request: web.Request) -> web.Response:
    """GET|POST /broker/tutor/project/{name}/lessons — a project's lessons, and a new one
    ({title, objective}); GET /broker/tutor/project/{name}/plan — its plan; GET
    /broker/tutor/project/{name}/insights[?all=1] — its insights; GET|PUT
    /broker/tutor/project/{name}/brief-set — the brief set it uses, and another ({set}). The project is
    named upstream by the name and the creation time of the broker's `status`, never by what
    the browser sends; an unknown project is the broker's refusal, answered as the project
    routes answer it."""
    refused = await _tutor_gate(request)
    if refused is not None:
        return refused
    body = None
    if request.method in ("POST", "PUT"):
        body = await _json_body(request)
        if body is None:
            return _bad_request()
    status, reply = await _relay(request, "status", {"name": request.match_info.get("name", "")})
    if status != 200 or not reply.get("ok"):
        return web.json_response(reply, status=status)
    result = reply.get("result") or {}
    project, created = result.get("name"), result.get("created")
    if not isinstance(project, str) or not isinstance(created, str):
        return web.json_response({"ok": False, "error": {"kind": "failed"}}, status=500)
    if request.path.endswith("/brief-set"):
        if request.method == "GET":
            return await _tutor_call("GET", "/knowledge/project-set", query={"project": project, "created": created})
        return await _tutor_call("PUT", "/knowledge/project-set", body={"set": body.get("set"), "project": project, "created": created})
    if request.method == "GET" and request.path.endswith("/insights"):
        # The project's insights (with `all=1`, the retired ones too).
        query = {"kind": "insight", "project": project, "created": created}
        if request.rel_url.query.get("all") == "1":
            query["all"] = "1"
        return await _tutor_call("GET", "/knowledge/records", query=query)
    if request.method == "GET":
        what = "/plan" if request.path.endswith("/plan") else "/lessons"
        return await _tutor_call("GET", what, query={"project": project, "created": created})
    return await _tutor_call("POST", "/lessons", body={**{k: body[k] for k in ("title", "objective") if k in body},
                                                        "project": project, "created": created})


# A project is named upstream only by the routes under /broker/tutor/project/{name}/, from the
# broker's status: these keys are taken out of what the other routes forward.
_TUTOR_PROJECT_KEYS = ("project", "created")


def _tutor_route(template: str):
    """A handler forwarding a route of the contract as it is: `template` is the upstream path,
    with `{id}` filled from the request's path after the id is checked. A project named in the
    query or the body is left out (_TUTOR_PROJECT_KEYS)."""
    async def handler(request: web.Request) -> web.Response:
        refused = await _tutor_gate(request)
        if refused is not None:
            return refused
        path = template
        if "{id}" in template:
            ident = request.match_info.get("id", "")
            if not _TUTOR_ID_RE.fullmatch(ident):
                return web.json_response(
                    {"ok": False, "error": {"kind": "not_found", "message": "There is nothing with that id."}},
                    status=404)
            path = template.replace("{id}", ident)
        body = await _tutor_body(request)
        if isinstance(body, web.Response):
            return body
        if body is not None:
            body = {k: v for k, v in body.items() if k not in _TUTOR_PROJECT_KEYS}
        query = {k: v for k, v in request.rel_url.query.items() if k not in _TUTOR_PROJECT_KEYS}
        return await _tutor_call(request.method, path, query=query, body=body)
    return handler


async def tutor_stream_handler(request: web.Request) -> web.StreamResponse:
    """GET /broker/tutor/runs/{id}/stream — a run's events, passed through as they come:
    each chunk written as it arrives, no total timeout (a turn can take many minutes), and
    the upstream request closed when the browser goes away."""
    refused = await _tutor_gate(request)
    if refused is not None:
        return refused
    ident = request.match_info.get("id", "")
    if not _TUTOR_ID_RE.fullmatch(ident):
        return web.json_response(
            {"ok": False, "error": {"kind": "not_found", "message": "There is no such run."}}, status=404)
    secret = _tutor_secret()
    if secret is None:
        return _tutor_unavailable()
    url = TUTOR_URL.with_path(f"/runs/{ident}/stream").with_query(request.rel_url.query)
    timeout = ClientTimeout(total=None, sock_read=None, sock_connect=PASSTHROUGH_CONNECT_TIMEOUT_S)
    async with ClientSession(timeout=timeout, auto_decompress=False) as sess:
        try:
            req_ctx = sess.get(url, headers={"X-CL-Secret": secret}, allow_redirects=False)
            upstream = await req_ctx.__aenter__()
        except Exception as e:  # noqa: BLE001 - as _proxy_http's connect guard
            log.info(f"tutor service: stream {ident} failed: {type(e).__name__}")
            return _tutor_unavailable()
        try:
            if upstream.status != 200 or not upstream.content_type.startswith("text/event-stream"):
                status, text = upstream.status, await upstream.text()
                if status == 401:
                    log.warning("tutor service: the shared secret was refused")
                    return _tutor_unavailable()
                try:
                    return web.json_response(json.loads(text), status=status)
                except ValueError:
                    return _tutor_unavailable()
            response = web.StreamResponse(status=200, headers={
                "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            await response.prepare(request)

            async def pump():
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)

            # aiohttp does not cancel a handler whose browser went away, and a write notices
            # it only at the next event: look at the connection every second meanwhile.
            task = asyncio.ensure_future(pump())
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=1)
                    if request.transport is None or request.transport.is_closing():
                        task.cancel()
                        break
                # The browser gone, or the service gone mid-stream: either way the stream ends.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                with contextlib.suppress(ConnectionError):
                    await response.write_eof()
            finally:
                task.cancel()
            return response
        finally:
            upstream.close()   # the connection, not returned to the pool: upstream sees the close
            await req_ctx.__aexit__(None, None, None)


# ---- a destroyed project, told to the tutor service ------------------------------------
#
# The broker runs a destroy as an operation in a child and answers at once. Once that
# operation is done (the broker's `op`, asked with the login's token as the page asks it), this
# server tells the tutor service the project is gone, with the creation time read from its
# status before the destroy and the learner's choice to forget what was learned there. A
# failure here leaves the destroy as it is: it is logged, and the operation's status says it
# (`knowledge`) to a page that follows it.

DESTROY_POLL_S = 1.0
DESTROY_WATCH_S = 3600          # a destroy not over in an hour is not waited for
TUTOR_REMOVED_TRIES = 40        # the tutor still answering in the project ("busy"), or away
TUTOR_REMOVED_RETRY_S = 3.0
_DESTROYS: dict[str, asyncio.Task] = {}   # op_id -> the task that tells the tutor service


def _watch_destroy(op_id: str, broker_token: str, project: str, created: str, forget: bool) -> None:
    loop = asyncio.get_running_loop()
    task = loop.create_task(_after_destroy(op_id, broker_token, project, created, forget))
    _DESTROYS[op_id] = task
    task.add_done_callback(lambda _t: loop.call_later(DESTROY_WATCH_S, _DESTROYS.pop, op_id, None))


async def _after_destroy(op_id: str, broker_token: str, project: str, created: str, forget: bool) -> dict | None:
    """Wait for the destroy to end; when it is done, tell the tutor service. Returns what the
    page is told ({told: true, forgotten} or {told: false, message}), or None when the destroy
    did not happen (failed: the project is still there)."""
    deadline = time.monotonic() + DESTROY_WATCH_S
    while True:
        try:
            reply = await broker_call("op", {"op_id": op_id}, token=broker_token)
        except (BrokerUnavailable, BrokerForbidden):
            reply = {}
        state = (reply.get("result") or {}).get("state") if reply.get("ok") else None
        if state == "done":
            break
        if state == "failed":
            return None
        if not reply.get("ok") and (reply.get("error") or {}).get("kind") == "unauthorized" or time.monotonic() > deadline:
            log.warning(f"destroy of {project}: its end was not seen; the tutor service is not told")
            return {"told": False, "message": "The end of the destroy was not seen, so the tutor was not told the project is gone."}
        await asyncio.sleep(DESTROY_POLL_S)
    message = "The tutor service did not answer."
    for _ in range(TUTOR_REMOVED_TRIES):
        got = await _tutor_fetch("POST", "/projects/removed", body={"project": project, "created": created, "forget": forget})
        if got is not None and got[1].get("ok"):
            log.info(f"destroy of {project}: the tutor service is told ({'forgotten' if forget else 'kept'})")
            return {"told": True, "forgotten": forget}
        if got is not None:
            error = got[1].get("error") or {}
            message = str(error.get("message") or error.get("kind") or "refused")
            if error.get("kind") != "busy":
                break
        await asyncio.sleep(TUTOR_REMOVED_RETRY_S)
    log.warning(f"destroy of {project}: the tutor service was not told: {message}")
    return {"told": False, "message": f"The tutor service was not told the project is gone: {message}"}


async def _destroy_told(op_id: str) -> dict | None:
    """What the tutor service was told after this destroy, once the operation is done: waited
    for a little (the call follows the operation's end), None when this server is not telling."""
    task = _DESTROYS.get(op_id)
    if task is None:
        return None
    await asyncio.wait({task}, timeout=20)
    if not task.done() or task.cancelled() or task.exception() is not None:
        return {"told": False, "message": "The tutor service is still being told the project is gone."}
    return task.result()


# ---- the origin proxy: HTTP and WebSocket on every origin port ----------------------

class _UpstreamDown(Exception):
    """The connect to the upstream failed (nothing sent yet): the address may be stale."""


def _own_cookie(name: str) -> bool:
    return name.strip().startswith(OWN_COOKIE_PREFIXES)


def _app_cookies(headers) -> str:
    """The request's Cookie headers without clanker-learn's own cookies, as one value."""
    return "; ".join(part.strip() for value in headers.getall("Cookie", []) for part in value.split(";")
                     if part.strip() and not _own_cookie(part.partition("=")[0]))


def _filter_headers(headers, drop: frozenset[str]) -> CIMultiDict[str]:
    # Preserve DUPLICATE headers (a plain dict collapses them): an upstream can send
    # multiple Set-Cookie in one response; .items() yields each duplicate pair, add() keeps
    # them all.
    out: CIMultiDict[str] = CIMultiDict()
    for k, v in headers.items():
        if k.lower() not in drop:
            out.add(k, v)
    return out


def _upstream_down() -> web.Response:
    return web.Response(status=502, text="The app does not answer. Is it running in the sandbox?")


async def _proxy_ws(request: web.Request, upstream_url: URL,
                    forward: dict[str, str] | None = None) -> web.StreamResponse:
    """WS pass-through. Opens upstream first so a failure returns a clean
    502 to the browser instead of a bare close frame after handshake.
    `forward` is sent as upstream headers."""
    client_subprotocols: list[str] = []
    sec_proto = request.headers.get("Sec-WebSocket-Protocol")
    if sec_proto:
        client_subprotocols = [
            p.strip() for p in sec_proto.split(",") if p.strip()
        ]

    timeout = ClientTimeout(total=None, sock_read=None, sock_connect=PASSTHROUGH_CONNECT_TIMEOUT_S)
    sess = ClientSession(timeout=timeout)
    try:
        try:
            upstream_ws = await sess.ws_connect(
                upstream_url,
                protocols=client_subprotocols,
                heartbeat=30,
                max_msg_size=0,
                **({"headers": forward} if forward else {}),
            )
        except (ClientConnectorError, TimeoutError):
            raise _UpstreamDown() from None
        except Exception as e:  # noqa: BLE001 - whatever the upstream did, the browser gets a 502
            log.info(f"WS upstream connect failed: {type(e).__name__}")
            return _upstream_down()

        chosen = upstream_ws.protocol
        client_ws = web.WebSocketResponse(
            protocols=[chosen] if chosen else (),
            heartbeat=30,
            max_msg_size=0,
        )
        await client_ws.prepare(request)

        async def c2u():
            async for msg in client_ws:
                if msg.type == WSMsgType.TEXT:
                    await upstream_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await upstream_ws.send_bytes(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                  WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        async def u2c():
            async for msg in upstream_ws:
                if msg.type == WSMsgType.TEXT:
                    await client_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await client_ws.send_bytes(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                  WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        try:
            _, pending = await asyncio.wait(
                [asyncio.create_task(c2u()), asyncio.create_task(u2c())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        finally:
            if not upstream_ws.closed:
                await upstream_ws.close()
            if not client_ws.closed:
                await client_ws.close()
        return client_ws
    finally:
        await sess.close()


async def _proxy_http(request: web.Request, upstream_url: URL,
                      forward: dict[str, str] | None = None) -> web.StreamResponse:
    """HTTP pass-through. Streams body both directions; preserves status,
    headers (minus hop-by-hop), and the app's own Set-Cookie from upstream.
    `forward` REPLACES those request headers upstream. clanker-learn's own cookies
    are taken out both ways."""
    headers_in = _filter_headers(request.headers, HOP_BY_HOP_HEADERS | {"cookie"})
    cookies = _app_cookies(request.headers)
    if cookies:
        headers_in["Cookie"] = cookies
    for k, v in (forward or {}).items():
        headers_in[k] = v

    # Stream the body upstream rather than buffering. GET/HEAD have no body.
    data = None
    if request.method not in ("GET", "HEAD"):
        data = request.content

    # No request timeout — WebSocket-like long polls need to live as long as the user holds
    # the tab. Connection close is the source of truth for "done", not a wall clock.
    timeout = ClientTimeout(total=None, sock_read=None, sock_connect=PASSTHROUGH_CONNECT_TIMEOUT_S)
    async with ClientSession(timeout=timeout, auto_decompress=False) as sess:
        try:
            req_ctx = sess.request(
                request.method,
                upstream_url,
                headers=headers_in,
                data=data,
                allow_redirects=False,
            )
            upstream_resp = await req_ctx.__aenter__()
        except (ClientConnectorError, TimeoutError):
            raise _UpstreamDown() from None
        except Exception:  # noqa: BLE001 - mirror _proxy_ws's connect guard
            return _upstream_down()
        try:
            headers_out = _filter_headers(
                upstream_resp.headers, RESPONSE_DROP_HEADERS | {"set-cookie"})
            for value in upstream_resp.headers.getall("Set-Cookie", []):
                if not _own_cookie(value.partition("=")[0]):
                    headers_out.add("Set-Cookie", value)
            response = web.StreamResponse(
                status=upstream_resp.status,
                reason=upstream_resp.reason,
                headers=headers_out,
            )
            await response.prepare(request)
            async for chunk in upstream_resp.content.iter_any():
                if not chunk:
                    break
                await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            await req_ctx.__aexit__(None, None, None)


async def origin_proxy_handler(request: web.Request) -> web.StreamResponse:
    """Catch-all for the origin ports (CL_ORIGIN_PORTS): which app this port serves comes
    from the origin table, by the port the request ARRIVED on (the socket's own); the gate is
    the project's session cookie (host-keyed and port-blind, so the mint on the page's port
    reaches us); the upstream is the sandbox's address from the broker and the table's port.
    Reverse-proxied at port root, no path prefix."""
    sock = request.transport.get_extra_info("sockname") if request.transport else None
    arrival_port = int(sock[1]) if sock else 0
    entry = ORIGIN_PORTS.get(arrival_port)
    if entry is None:
        return web.Response(status=404, text="no app is assigned to this port")
    project, port = entry["project"], entry["port"]

    cookie = request.cookies.get(services.session_cookie_name(WEB_PORT, project))
    if not cookie or not _session_valid(cookie, project):
        return web.Response(status=401, text="session required")

    is_ws = (request.headers.get("Upgrade", "").lower() == "websocket")
    # The app sees the address the browser used, the way a standard reverse proxy presents
    # it: apps that admit only their public URL match on the Host, and build links and
    # websocket URLs (wss://) from the scheme. Where we connect does not depend on it.
    forward = {"X-Forwarded-Proto": "https"}
    if request.headers.get("Host"):
        forward["Host"] = request.headers["Host"]
    if is_ws:
        cookies = _app_cookies(request.headers)
        if cookies:
            forward["Cookie"] = cookies

    failed = None
    while True:
        address = await sandbox_address(project, failed)
        if address is None or address == failed:
            return _upstream_down()
        # Host and port are the broker's address and the table's port, nothing else: the
        # request gives only its path and query, from its relative form (an absolute-form
        # request line names a host, which is not read).
        upstream_url = URL.build(
            scheme="ws" if is_ws else "http", host=address, port=port,
            path=request.rel_url.raw_path, query_string=request.rel_url.raw_query_string, encoded=True)
        try:
            if is_ws:
                return await _proxy_ws(request, upstream_url, forward)
            return await _proxy_http(request, upstream_url, forward)
        except _UpstreamDown:
            failed = address


# ---- the pass-through relay: raw TCP on the claimed ports ------------------------

async def _pt_pump(reader: asyncio.StreamReader,
                   writer: asyncio.StreamWriter) -> None:
    """One-direction byte pump for the pass-through relay. 64 KiB is read granularity, not a
    cap — the OS enforces its own socket bounds."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def passthrough_conn_handler(reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter) -> None:
    """Raw-TCP pass-through: forward the connection UNTOUCHED to the claiming project's
    sandbox at the claimed port. Deliberately NO login, cookie or session gate (raw TCP, TLS
    perhaps, cannot carry one: the app's own auth is the only lock), no origin check (this is
    not HTTP), and no payload logging; these ports are published on the machine's loopback
    only. The arrival port is the ONLY routing key; an unclaimed port, or a project without
    an address, closes with ZERO bytes written."""
    sock = writer.get_extra_info("sockname")
    arrival = int(sock[1]) if sock else 0
    claim = PASSTHROUGH_CLAIMS.get(arrival)
    if claim is None:
        writer.close()
        return
    failed = None
    while True:
        address = await sandbox_address(claim["project"], failed)
        if address is None or address == failed:
            writer.close()
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(address, claim["port"]), PASSTHROUGH_CONNECT_TIMEOUT_S)
            break
        except (OSError, TimeoutError):
            failed = address
    await asyncio.gather(_pt_pump(reader, up_writer),
                         _pt_pump(up_reader, writer))


# ---- the page and its files -----------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
# Every answer of the page's files is checked again with the server (an ETag and the time it
# changed): a changed file shows at the next reload, an unchanged one costs a 304.
STATIC_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
# A host name the browser used: DNS labels of letters, digits and dashes, or an IPv4 address
# (checked below), and an optional port. A bracketed IPv6 address is matched apart.
_HOST_NAME = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
_HOST_PORT = re.compile(r"[0-9]{1,5}")
# The page's content-security header, but for the two directives that name the host. Styles
# may be inline because xterm's renderer writes <style> elements into the terminal (its
# colours, its font, the cell grid); with style-src 'self' alone the browser refuses them.
PAGE_SOURCES = ("default-src 'none'", "script-src 'self'", "style-src 'self' 'unsafe-inline'")
PAGE_REFUSES = ("object-src 'none'", "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'")


def browser_host(header: str | None) -> tuple[str, str] | None:
    """The Host header split into (host, port), the host as it goes into a URL (an IPv6
    address bracketed), the port "" when none was given; None when the header is not a host
    name, an IPv4 address or a bracketed IPv6 address, with an optional port. (RS's
    `_request_host` takes the header as it comes; here it goes into the page's
    content-security header, so nothing but those three forms passes.)"""
    if not header or not header.isascii():
        return None
    if header.startswith("["):
        inside, bracket, port = header[1:].partition("]")
        if not bracket or "%" in inside or (port and (port == ":" or not port.startswith(":"))):
            return None
        try:
            ipaddress.IPv6Address(inside)
        except ValueError:
            return None
        host, port = f"[{inside}]", port[1:]
    else:
        host, colon, port = header.partition(":")
        if (colon and not port) or not _HOST_NAME.fullmatch(host):
            return None
        if host.replace(".", "").isdigit():
            try:
                ipaddress.IPv4Address(host)
            except ValueError:
                return None
    if port and not (_HOST_PORT.fullmatch(port) and 1 <= int(port) <= 65535):
        return None
    return host, port


def content_security_policy(host_header: str | None) -> str:
    """The page's content-security header for the host the browser used: its own scripts,
    styles and images only, nothing inline but styles (RS allows them too), fetches and the
    terminal's WebSocket to itself, frames and images from any port of that host (the web-app
    tabs; a lesson's pages and pictures, from the project's files server on its own origin
    port) and from nowhere when the Host header is not a plain host. No form is ever sent by the browser
    itself (form-action 'none'): the page's forms are read by its script, so a password never
    lands in an address even if the script did not load."""
    found = browser_host(host_header)
    images = "img-src 'self'"
    if found is None:
        frames, sockets = "frame-src 'none'", "connect-src 'self'"
    else:
        host, port = found
        frames = f"frame-src https://{host}:*"
        images = f"img-src 'self' https://{host}:*"
        # 'self' admits wss: to the same host and port in browsers that follow CSP 3; the
        # address is also named, for those that do not.
        sockets = f"connect-src 'self' wss://{host}{':' + port if port else ''}"
    return "; ".join((*PAGE_SOURCES, images, sockets, frames, *PAGE_REFUSES))


async def index_handler(request: web.Request) -> web.StreamResponse:
    """GET / — the page, open (the login screen is on it), with its content-security header
    computed for the host the browser used (RS's index_handler, stricter)."""
    return web.FileResponse(
        STATIC_DIR / "index.html",
        headers={"Content-Security-Policy": content_security_policy(request.headers.get("Host")), **STATIC_HEADERS})


async def static_handler(request: web.Request) -> web.StreamResponse:
    """GET /static/{path} — the page's files, open. A path that leads out of the folder (by
    `..`, a link or anything else), a folder or a file that is not there is 404. (RS uses
    aiohttp's add_static, which sends no nosniff and no cache header.)"""
    root = STATIC_DIR.resolve()
    try:
        path = (root / request.match_info.get("path", "")).resolve()
    except (OSError, ValueError, RuntimeError):
        raise web.HTTPNotFound() from None
    if not path.is_relative_to(root) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers=STATIC_HEADERS)


# ---- the certificate ----------------------------------------------------------

def cert_covers_bind(cert_path: Path, bind: str) -> bool:
    """Check whether the existing cert's SAN already includes `bind`."""
    if not cert_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
    except Exception:  # noqa: BLE001 - whatever is wrong with it, it does not cover
        return False
    try:
        bind_ip = ipaddress.ip_address(bind)
        return any(
            isinstance(e, x509.IPAddress) and e.value == bind_ip for e in san
        )
    except ValueError:
        return any(isinstance(e, x509.DNSName) and e.value == bind for e in san)


def cert_not_after(cert_path: Path) -> datetime | None:
    """The certificate's notAfter (aware UTC), or None when it cannot be read."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception:  # noqa: BLE001 - unreadable is an answer here
        return None
    after = getattr(cert, "not_valid_after_utc", None)
    if after is None:
        after = cert.not_valid_after   # older cryptography: naive UTC
    return after


def generate_self_signed(cert_path: Path, key_path: Path, bind: str) -> None:
    """Write a fresh self-signed cert+key covering localhost and `bind`."""
    log.info(f"Generating self-signed TLS cert at {cert_path} (bind={bind})")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "clanker-learn")])

    san_entries = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]
    try:
        bind_ip = ipaddress.ip_address(bind)
        if not any(isinstance(e, x509.IPAddress) and e.value == bind_ip
                   for e in san_entries):
            san_entries.append(x509.IPAddress(bind_ip))
    except ValueError:
        if bind != "localhost":
            san_entries.append(x509.DNSName(bind))

    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256()))

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.chmod(0o644)
    key_path.chmod(0o600)


def _custom_provider(tls_dir: Path) -> str | None:
    """Who supplied the certificate, from the `.custom` marker beside it (`./setup
    cert-tailscale` writes `tailscale:<name>`), or None when there is no marker: the
    certificate is then the self-signed one this server makes."""
    marker = tls_dir / ".custom"
    if not marker.exists():
        return None
    try:
        return marker.read_text().strip() or "user-supplied"
    except OSError:
        return "user-supplied"


def ensure_tls(cert_path: Path, key_path: Path, bind: str) -> ssl.SSLContext:
    """The TLS context. Without a `.custom` marker beside the certificate, the self-signed
    certificate is made, or made again when it is missing, expired, due for renewal,
    unreadable, or does not cover `bind`. With the marker (RS's: `./setup cert-tailscale`
    stages a Tailscale certificate for the machine's tailnet name with it), the certificate
    is never replaced, even expired: it may name no address of `bind`, which is right when
    the browser opens the page by the tailnet name, and falling back would swap a trusted
    certificate for an untrusted one. It is renewed on the machine; the log says when that
    is due. Only a marker whose files are missing falls back to a self-signed certificate."""
    present = cert_path.exists() and key_path.exists()
    expiry = services.cert_expiry_state(
        cert_not_after(cert_path) if present else None, datetime.now(UTC))
    provider = _custom_provider(cert_path.parent)
    if provider is not None:
        log.info(f"using the {provider} certificate (no self-signed one is made)")
        if not present:
            log.error(f".custom marker present but the certificate or its key is missing in "
                      f"{cert_path.parent}: falling back to a self-signed certificate")
            generate_self_signed(cert_path, key_path, bind)
        elif expiry in ("expired", "renew-due"):
            after = cert_not_after(cert_path)
            when = after.strftime("%Y-%m-%d %H:%M UTC") if after else "unknown"
            remedy = ("run ./setup cert-tailscale on the machine"
                      if provider.startswith("tailscale:")
                      else "replace the certificate in the web server's folder")
            if expiry == "expired":
                log.error(f"the {provider} certificate EXPIRED on {when}; browsers refuse it: {remedy}")
            else:
                log.warning(f"the {provider} certificate expires on {when}, "
                            f"within {services.CERT_RENEW_BEFORE.days} days: {remedy}")
    else:
        reason = services.self_signed_regen_reason(
            present, present and cert_covers_bind(cert_path, bind), expiry)
        if reason:
            log.info(f"regenerating the self-signed certificate: {reason}")
            generate_self_signed(cert_path, key_path, bind)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


def cert_info(tls_dir: Path) -> dict:
    """The certificate in `tls_dir`, read now from its files: {provider: "self-signed" or the
    marker's text, not_after: ISO time or None, state: ok | renew-due | expired | unknown}."""
    cert_path = tls_dir / "cert.pem"
    after = cert_not_after(cert_path) if cert_path.exists() else None
    if after is not None and after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    return {"provider": _custom_provider(tls_dir) or "self-signed",
            "not_after": after.isoformat() if after else None,
            "state": services.cert_expiry_state(after, datetime.now(UTC))}


async def cert_handler(request: web.Request) -> web.Response:
    """GET /broker/cert — the certificate this server uses, for Settings → How it runs
    (gated read, answered here: the broker has no part in it)."""
    if _broker_session(request) is None:
        return _unauthorized()
    return web.json_response({"ok": True, "result": cert_info(TLS_DIR)})


def build_app() -> web.Application:
    """The application and its routes. Request bodies keep aiohttp's default size limit:
    the one large body, a project file, is read as a stream, which the limit does not cover."""
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/static/{path:.*}", static_handler)
    app.router.add_post("/broker/login", broker_login_handler)
    app.router.add_post("/broker/logout", broker_logout_handler)
    app.router.add_get("/broker/setup", broker_setup_state_handler)
    app.router.add_post("/broker/setup", broker_setup_handler)
    app.router.add_get("/broker/projects", broker_projects_handler)
    app.router.add_get("/broker/lifecycle", broker_lifecycle_handler)
    app.router.add_get("/broker/models", broker_models_handler)
    app.router.add_get("/broker/software", broker_software_handler)
    app.router.add_get("/broker/software/active", broker_software_active_handler)
    app.router.add_post("/broker/software/build", broker_build_handler)
    app.router.add_post("/broker/software/refresh-check", broker_refresh_check_handler)
    app.router.add_post("/broker/software/pin-reset", broker_pin_reset_handler)
    app.router.add_get("/broker/mode", broker_mode_handler)
    app.router.add_get("/broker/cert", cert_handler)
    app.router.add_get("/broker/backups", broker_backups_handler)
    app.router.add_post("/broker/backups", broker_backup_make_handler)
    app.router.add_get("/broker/backups/active", broker_backup_active_handler)
    app.router.add_post("/broker/backups/remove", broker_backup_remove_handler)
    app.router.add_post("/broker/backups/{backup}/download", broker_backup_download_handler)
    app.router.add_get("/broker/backups/file/{link}", broker_backup_file_handler)
    app.router.add_post("/broker/project-file", broker_project_file_handler)
    app.router.add_get("/broker/project-file/file/{link}", broker_backup_file_handler)
    app.router.add_post("/broker/project-import", broker_project_import_handler)
    app.router.add_post("/broker/project", broker_create_handler)
    app.router.add_get("/broker/project/{name}", broker_project_status_handler)
    app.router.add_get("/broker/project/{name}/terminal", ws_handler)
    app.router.add_get("/broker/project/{name}/services", project_services_handler)
    app.router.add_get("/broker/project/{name}/ports", broker_port_list_handler)
    app.router.add_post("/broker/project/{name}/ports/remove", broker_port_remove_handler)
    # Before the action route, which would otherwise take these for an action.
    app.router.add_post("/broker/project/{name}/limits", broker_project_limits_handler)
    app.router.add_post("/broker/project/{name}/session", session_handler)
    app.router.add_post("/broker/project/{name}/origin", project_origin_handler)
    app.router.add_post("/broker/project/{name}/ports", broker_port_add_handler)
    app.router.add_post("/broker/project/{name}/agents", broker_project_agents_handler)
    app.router.add_post("/broker/project/{name}/model", broker_project_model_handler)
    app.router.add_post("/broker/project/{name}/export", broker_project_export_handler)
    app.router.add_post("/broker/project/{name}/export-download", broker_project_export_download_handler)
    app.router.add_post("/broker/project/{name}/{action}", broker_project_action_handler)
    app.router.add_get("/broker/op/{op_id}/log", broker_op_log_handler)
    app.router.add_get("/broker/op/{op_id}/alive", broker_op_alive_handler)
    app.router.add_get("/broker/op/{op_id}", broker_op_status_handler)
    # The tutor (the contract with cl-tutor's routes).
    app.router.add_get("/broker/tutor/project/{name}/lessons", tutor_project_lessons_handler)
    app.router.add_post("/broker/tutor/project/{name}/lessons", tutor_project_lessons_handler)
    lesson = _tutor_route("/lessons/{id}")
    app.router.add_get("/broker/tutor/lessons/{id}", lesson)
    app.router.add_patch("/broker/tutor/lessons/{id}", lesson)
    app.router.add_delete("/broker/tutor/lessons/{id}", lesson)
    app.router.add_post("/broker/tutor/lessons/{id}/messages", _tutor_route("/lessons/{id}/messages"))
    app.router.add_post("/broker/tutor/lessons/{id}/build", _tutor_route("/lessons/{id}/build"))
    app.router.add_post("/broker/tutor/lessons/{id}/build-all", _tutor_route("/lessons/{id}/build-all"))
    app.router.add_post("/broker/tutor/lessons/{id}/build-all/stop", _tutor_route("/lessons/{id}/build-all/stop"))
    app.router.add_get("/broker/tutor/runs/{id}/stream", tutor_stream_handler)
    app.router.add_post("/broker/tutor/runs/{id}/cancel", _tutor_route("/runs/{id}/cancel"))
    app.router.add_get("/broker/tutor/activity", _tutor_route("/activity"))
    settings = _tutor_route("/settings")
    app.router.add_get("/broker/tutor/settings", settings)
    app.router.add_put("/broker/tutor/settings", settings)
    # Plans: a project's plan, a proposal and the learner's decision on it; the tutor's instructions.
    app.router.add_get("/broker/tutor/project/{name}/plan", tutor_project_lessons_handler)
    app.router.add_get("/broker/tutor/proposals/{id}", _tutor_route("/proposals/{id}"))
    app.router.add_post("/broker/tutor/proposals/{id}/approve", _tutor_route("/proposals/{id}/approve"))
    app.router.add_post("/broker/tutor/proposals/{id}/dismiss", _tutor_route("/proposals/{id}/dismiss"))
    instructions = _tutor_route("/instructions")
    app.router.add_get("/broker/tutor/instructions", instructions)
    app.router.add_put("/broker/tutor/instructions", instructions)
    app.router.add_post("/broker/tutor/instructions/reset", _tutor_route("/instructions/reset"))
    app.router.add_get("/broker/tutor/skills", _tutor_route("/skills"))
    # The knowledge base: the brief, the records, the global proposals, the lessons done, the
    # change log with undo; a project's insights; the cards of a conversation. (POST
    # /projects/removed is this server's own call after a destroy, never the browser's.)
    app.router.add_get("/broker/tutor/project/{name}/insights", tutor_project_lessons_handler)
    app.router.add_get("/broker/tutor/knowledge/brief", _tutor_route("/knowledge/brief"))
    brief_sets = _tutor_route("/knowledge/sets")
    app.router.add_get("/broker/tutor/knowledge/sets", brief_sets)
    app.router.add_post("/broker/tutor/knowledge/sets", brief_sets)
    brief_set = _tutor_route("/knowledge/sets/{id}")
    app.router.add_patch("/broker/tutor/knowledge/sets/{id}", brief_set)
    app.router.add_delete("/broker/tutor/knowledge/sets/{id}", brief_set)
    app.router.add_get("/broker/tutor/project/{name}/brief-set", tutor_project_lessons_handler)
    app.router.add_put("/broker/tutor/project/{name}/brief-set", tutor_project_lessons_handler)
    records = _tutor_route("/knowledge/records")
    app.router.add_get("/broker/tutor/knowledge/records", records)
    app.router.add_post("/broker/tutor/knowledge/records", records)
    record = _tutor_route("/knowledge/records/{id}")
    app.router.add_get("/broker/tutor/knowledge/records/{id}", record)
    app.router.add_patch("/broker/tutor/knowledge/records/{id}", record)
    app.router.add_delete("/broker/tutor/knowledge/records/{id}", record)
    app.router.add_put("/broker/tutor/knowledge/background", _tutor_route("/knowledge/background"))
    app.router.add_get("/broker/tutor/knowledge/evidence/{id}", _tutor_route("/knowledge/evidence/{id}"))
    app.router.add_get("/broker/tutor/knowledge/proposals", _tutor_route("/knowledge/proposals"))
    proposal = _tutor_route("/knowledge/proposals/{id}")
    app.router.add_get("/broker/tutor/knowledge/proposals/{id}", proposal)
    app.router.add_patch("/broker/tutor/knowledge/proposals/{id}", proposal)
    app.router.add_post("/broker/tutor/knowledge/proposals/{id}/approve", _tutor_route("/knowledge/proposals/{id}/approve"))
    app.router.add_post("/broker/tutor/knowledge/proposals/{id}/dismiss", _tutor_route("/knowledge/proposals/{id}/dismiss"))
    app.router.add_get("/broker/tutor/knowledge/lessons", _tutor_route("/knowledge/lessons"))
    app.router.add_get("/broker/tutor/knowledge/changes", _tutor_route("/knowledge/changes"))
    app.router.add_post("/broker/tutor/knowledge/changes/{id}/undo", _tutor_route("/knowledge/changes/{id}/undo"))
    app.router.add_get("/broker/tutor/cards/{id}", _tutor_route("/cards/{id}"))
    app.router.add_post("/broker/tutor/cards/{id}/check", _tutor_route("/cards/{id}/check"))
    app.router.add_post("/broker/tutor/cards/{id}/answer", _tutor_route("/cards/{id}/answer"))
    return app


def build_origin_app() -> web.Application:
    """What every origin port serves: the origin proxy, for any method and path. No body
    limit: the body is streamed upstream, never read here."""
    app = web.Application(client_max_size=0)
    app.router.add_route("*", "/{tail:.*}", origin_proxy_handler)
    return app


def port_range(text: str, called: str) -> tuple[int, int]:
    """A range as the run command names it, `first-last`, of ports from 1024 to 65535
    (outer/core.py checks it the same way before the container is made). Raises ValueError
    naming the setting."""
    low, dash, high = str(text).strip().partition("-")
    if dash and low.isascii() and low.isdigit() and high.isascii() and high.isdigit():
        first, last = int(low), int(high)
        if 1024 <= first <= last <= 65535:
            return first, last
    raise ValueError(f"{called} is {text!r}: it must be a range of ports from 1024 to 65535, as 11100-11164")


def port_ranges(env) -> tuple[tuple[int, int], tuple[int, int]]:
    """The origin range and the pass-through range from the environment, checked: neither
    overlaps the other or the page's port. Raises ValueError."""
    origin = port_range(env.get("CL_ORIGIN_PORTS", "{}-{}".format(*ORIGIN_RANGE)), "CL_ORIGIN_PORTS")
    passthrough = port_range(env.get("CL_PASSTHROUGH_PORTS", "{}-{}".format(*PASSTHROUGH_RANGE)), "CL_PASSTHROUGH_PORTS")
    spans = [("CL_WEB_PORT", (WEB_PORT, WEB_PORT)), ("CL_ORIGIN_PORTS", origin), ("CL_PASSTHROUGH_PORTS", passthrough)]
    for i, (first, (a_low, a_high)) in enumerate(spans):
        for second, (b_low, b_high) in spans[i + 1:]:
            if a_low <= b_high and b_low <= a_high:
                raise ValueError(f"{first} and {second} overlap")
    return origin, passthrough


def listen_on(subnet: str) -> list[tuple[str, int, str]]:
    """Where the server listens, as (address, port, what): its address in the listen subnet
    (the front network's) on the page's port ("page"), on every origin port ("origin") and on
    every pass-through port ("passthrough"), and nothing else a browser or a sandbox could
    reach: a connection that arrives on a project's network finds nothing listening. The
    outer container asks at that same address whether it answers. Raises ValueError, as
    services.listen_address does."""
    host = services.listen_address(subnet)
    return [(host, WEB_PORT, "page"),
            *[(host, port, "origin") for port in range(ORIGIN_RANGE[0], ORIGIN_RANGE[1] + 1)],
            *[(host, port, "passthrough") for port in range(PASSTHROUGH_RANGE[0], PASSTHROUGH_RANGE[1] + 1)]]


def main() -> None:
    global ORIGIN_RANGE, PASSTHROUGH_RANGE
    try:
        ORIGIN_RANGE, PASSTHROUGH_RANGE = port_ranges(os.environ)
    except ValueError as e:
        log.error(f"cannot choose the ports to listen on: {e}")
        raise SystemExit(1) from None
    try:
        sites = listen_on(os.environ.get("CL_LISTEN_SUBNET", ""))
    except ValueError as e:
        # Nothing can answer a browser here; the reason is in the container's log, which
        # clanker-learn's init reads into its state.
        log.error(f"cannot choose the listen address: {e}")
        raise SystemExit(1) from None
    ssl_ctx = ensure_tls(TLS_DIR / "cert.pem", TLS_DIR / "key.pem", HOST_BIND)
    _origin_table_load()
    _claims_load()
    app = build_app()

    async def _serve() -> None:
        # Two runners: the page's app on its port, the origin proxy on every origin port,
        # all with the one certificate.
        runner = web.AppRunner(app, access_log=log)
        await runner.setup()
        # No access log for the apps: a query string there is often the app's own token.
        origin_runner = web.AppRunner(build_origin_app(), access_log=None)
        await origin_runner.setup()
        relays = []
        for host, port, what in sites:
            if what == "passthrough":
                # Plain TCP, deliberately: the traffic is forwarded untouched; TLS, if any,
                # is the app's own.
                relays.append(await asyncio.start_server(passthrough_conn_handler, host, port))
            else:
                await web.TCPSite(runner if what == "page" else origin_runner, host, port, ssl_context=ssl_ctx).start()
        log.info(f"clanker-learn's web server listening on https://{sites[0][0]}:{WEB_PORT} "
                 f"(origin ports {ORIGIN_RANGE[0]}-{ORIGIN_RANGE[1]}, pass-through {PASSTHROUGH_RANGE[0]}-{PASSTHROUGH_RANGE[1]})")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            for relay in relays:
                relay.close()
            await origin_runner.cleanup()
            await runner.cleanup()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
