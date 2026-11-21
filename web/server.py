"""clanker-learn's web server: what the browser talks to.

RS's webui/server.py (RS commit 62788a8), stripped. It runs in a container of the inner
daemon, `cl-web`, with no Docker socket, no project folder and no capability; all it has is
the broker's socket folder, read-only, and a folder for its certificate.

So far it carries: the login (the proof the browser derives from the password is relayed to
the broker, which authenticates; the broker's session token stays here, server-side, under a
cookie scoped to /broker), the first password (a set-up code from the container's log), the
project list and status, the project actions, and the progress of an operation. Every route
is refused without a login but the placeholder page, the login and the set-up. The page itself,
terminals and web-app tabs come with later parts.

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
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import services
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

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
# The progress logs of operations, beside the socket (read-only here).
BROKER_OPLOG_DIR = str(Path(BROKER_SOCKET).parent / "oplogs")
# Mirror of the broker's own rule: an op_id names a file there. Matched whole (fullmatch):
# `$` alone lets a trailing newline through.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# What each write verb of the broker takes. The broker refuses any other field, so only
# these are forwarded: a field the page sends by mistake is dropped here, in one place.
BROKER_FIELDS = {
    "create": ("name", "pids", "memory", "cpus"),
    "limits": ("pids", "memory", "cpus"),
}
PROJECT_ACTIONS = ("start", "stop", "recreate", "destroy")

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
        BROKER_SESSIONS.pop(request.cookies.get(BROKER_COOKIE), None)
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
    """POST /broker/logout — revoke the broker token, drop the session, clear
    the cookie. Always 200 (idempotent). Origin-checked like every POST: SameSite=Strict
    still sends the cookie from another port of the same host."""
    if not origin_ok(request):
        return web.Response(status=403, text="origin rejected")
    s = _broker_session(request)
    if s is not None:
        try:
            await broker_call("logout", token=s["broker_token"])
        except (BrokerUnavailable, BrokerForbidden):
            pass
    tok = request.cookies.get(BROKER_COOKIE)
    if tok:
        BROKER_SESSIONS.pop(tok, None)
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
                      seed: str) -> web.Response:
    """Kick a broker verb that runs as an operation in a child and answers at once:
    mint an op_id, one synchronous broker call carrying it, return {ok, op_id}
    or the broker's refusal. The operation then lives in the broker's progress
    log; the browser follows it there (the log, /alive, and the op's state)."""
    s = _broker_session(request)
    if s is None:
        return _unauthorized()
    op_id = _mint_op_id(seed, verb)
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
        BROKER_SESSIONS.pop(request.cookies.get(BROKER_COOKIE), None)
        return _unauthorized()
    if not reply.get("ok"):
        return web.json_response(reply, status=200)   # busy / validation / step-up → app error
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
    """POST /broker/project/{name}/{action} — start|stop|recreate|destroy (gated,
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
    if action == "destroy":
        body = await _json_body(request)
        proof = body.get("proof") if body else None
        if isinstance(proof, str):
            args["proof"] = proof
    return await _lane_relay(request, action, args, name)


async def broker_lifecycle_handler(request: web.Request) -> web.Response:
    """GET /broker/lifecycle — every operation in flight,
    {ops:[{op_id, project, verb, state}]} (gated). The browser seeds its
    "starting…" labels from this after a reload."""
    status, body = await _relay(request, "lifecycle_active")
    return web.json_response(body, status=status)


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


PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>clanker-learn</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}</style>
</head><body>
<h1>clanker-learn</h1>
<p>The web server is running. The page that belongs here is not built yet.</p>
<p>Until it is, projects are driven from the machine clanker-learn runs on:
<code>docker exec clanker-learn cl list</code>.</p>
</body></html>
"""


async def index_handler(request: web.Request) -> web.Response:
    """GET / — a placeholder until the page exists. It loads nothing and runs nothing."""
    return web.Response(
        text=PLACEHOLDER, content_type="text/html",
        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


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


def ensure_tls(cert_path: Path, key_path: Path, bind: str) -> ssl.SSLContext:
    """The TLS context, with the self-signed certificate made, or made again when it is
    missing, expired, due for renewal, unreadable, or does not cover `bind`. (RS also takes a
    certificate from a tailnet or one supplied by hand; this server is published on the
    machine's loopback only, where neither applies.)"""
    present = cert_path.exists() and key_path.exists()
    expiry = services.cert_expiry_state(
        cert_not_after(cert_path) if present else None, datetime.now(UTC))
    reason = services.self_signed_regen_reason(
        present, present and cert_covers_bind(cert_path, bind), expiry)
    if reason:
        log.info(f"regenerating the self-signed certificate: {reason}")
        generate_self_signed(cert_path, key_path, bind)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


def build_app() -> web.Application:
    """The application and its routes. Request bodies keep aiohttp's default size limit:
    nothing here takes a large one."""
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_post("/broker/login", broker_login_handler)
    app.router.add_post("/broker/logout", broker_logout_handler)
    app.router.add_get("/broker/setup", broker_setup_state_handler)
    app.router.add_post("/broker/setup", broker_setup_handler)
    app.router.add_get("/broker/projects", broker_projects_handler)
    app.router.add_get("/broker/lifecycle", broker_lifecycle_handler)
    app.router.add_post("/broker/project", broker_create_handler)
    app.router.add_get("/broker/project/{name}", broker_project_status_handler)
    # Before the action route, which would otherwise take "limits" for an action.
    app.router.add_post("/broker/project/{name}/limits", broker_project_limits_handler)
    app.router.add_post("/broker/project/{name}/{action}", broker_project_action_handler)
    app.router.add_get("/broker/op/{op_id}/log", broker_op_log_handler)
    app.router.add_get("/broker/op/{op_id}/alive", broker_op_alive_handler)
    app.router.add_get("/broker/op/{op_id}", broker_op_status_handler)
    return app


def listen_on(subnet: str) -> list[tuple[str, int]]:
    """Where the server listens: its address in the listen subnet (the front network's), on
    the page's port, and nothing else a browser or a sandbox could reach: a connection that
    arrives on a project's network finds nothing listening. The outer container asks at that
    same address whether it answers. Raises ValueError, as services.listen_address does."""
    return [(services.listen_address(subnet), WEB_PORT)]


def main() -> None:
    try:
        sites = listen_on(os.environ.get("CL_LISTEN_SUBNET", ""))
    except ValueError as e:
        # Nothing can answer a browser here; the reason is in the container's log, which
        # clanker-learn's init reads into its state.
        log.error(f"cannot choose the listen address: {e}")
        raise SystemExit(1) from None
    ssl_ctx = ensure_tls(TLS_DIR / "cert.pem", TLS_DIR / "key.pem", HOST_BIND)
    app = build_app()

    async def _serve() -> None:
        runner = web.AppRunner(app, access_log=log)
        await runner.setup()
        for host, port in sites:
            await web.TCPSite(runner, host, port, ssl_context=ssl_ctx).start()
            log.info(f"clanker-learn's web server listening on https://{host}:{port}")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
