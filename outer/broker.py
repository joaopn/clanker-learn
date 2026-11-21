#!/usr/bin/env python3
"""The broker: the door through which the web server asks for anything that touches projects.

A process of clanker-learn's container, started by the init once the container is ready. It
listens on a unix socket that no sandbox has, and exposes a closed vocabulary of verbs, never
the inner Docker daemon itself.

Written after RS's cli/broker.py (RS commit 62788a8, 2026-11-18). The wire protocol, the
gates, the handler and the progress logs follow it where they are kept, so that RS's web
server, which clanker-learn transplants, finds what it reads today:

    ->  [4-byte big-endian length] [UTF-8 JSON: {"verb": <str>, "args": {...}, "token"?: <str>, "op_id"?: <str>}]
    <-  [4-byte big-endian length] [UTF-8 JSON reply]
        success:  {"ok": true,  "result": <the verb's result>}
        failure:  {"ok": false, "error": {"kind": <str>, "message": <str>}}

One request per connection, handled one at a time. Containment, by construction:

  * A closed table of verbs (FIELDS). A verb not in it is unknown; there is no pass-through.
  * Deny by default: a verb that is not an open read (OPEN_VERBS) needs a session token.
    `login` checks a proof derived from the password (broker_auth) and issues one; it is held
    only in this process's memory, so a restart of the broker ends every session, and so does
    a change of the password.
  * Step-up: `destroy` needs the proof again in the request. It is checked after the token,
    so a caller without a token never learns that it exists, and it is taken out of the
    request there: it reaches no log and no child.
  * Each verb takes only its own fields; any other is refused.
  * The socket is 0600 and the peer must be this process's own user.
  * A request larger than MAX_REQUEST_BYTES is refused before its body is read, and a
    request that has not arrived whole within READ_TIMEOUT_S is dropped.

The five verbs that can take long (create, start, stop, recreate, destroy) run as operations:
a child of this process, a fresh interpreter given only the verb, the checked name and limits
and the caller's op_id, which calls the core and writes RS's progress log,
<the socket's folder>/oplogs/<op_id>.view.log: JSON lines {op_id, project, action, step,
status, msg, ts}, the last one with status "done" or "failed". The reply returns at once.

One operation runs per project, and what says so is the core's own lock on the project: the
broker takes it without waiting ("busy" when something holds it, be it another operation, a
`cl` command or the init), hands the held lock to the child, and lets go. The kernel then
keeps it for as long as the child holds it, across a restart of this broker, with no pid
written anywhere; the child lets go when the work is done, just before its last word, or by
ending. A second lock, on a marker file per operation, held to the child's end, says in the
same way whether the operation still runs.
"""
import contextlib
import fcntl
import json
import os
import re
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import time
import traceback

import broker_auth
import core

RUN_DIR = "/run/cl/broker"            # the socket and the progress logs: what the web server's container gets
SOCKET = RUN_DIR + "/broker.sock"
OPLOGS = RUN_DIR + "/oplogs"
MARKERS = "/run/cl/broker-ops"        # one marker per operation in flight: the broker's alone
MAX_REQUEST_BYTES = 64 * 1024
READ_TIMEOUT_S = 5
CLIENT_TIMEOUT_S = 30
TAIL_BYTES = 64 * 1024
MSG_CHARS = 4000
OP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
LEN = struct.Struct(">I")

AUTH_VERBS = ("login", "logout")
OPEN_VERBS = frozenset({"list", "status", "op"})
STEP_UP_VERBS = frozenset({"destroy"})
OP_VERBS = frozenset({"create", "start", "stop", "recreate", "destroy"})
LIMITS = ("pids", "memory", "cpus")
FIELDS = {
    "list": frozenset(),
    "status": frozenset({"name"}),
    "op": frozenset({"op_id"}),
    "build_alive": frozenset({"op_id"}),
    "lifecycle_active": frozenset(),
    "create": frozenset({"name", *LIMITS}),
    "start": frozenset({"name"}),
    "stop": frozenset({"name"}),
    "recreate": frozenset({"name"}),
    "limits": frozenset({"name", *LIMITS}),
    "destroy": frozenset({"name"}),
}


def log(message):
    print(f"clanker-learn broker: {message}", file=sys.stderr, flush=True)


def err(kind, message):
    return {"ok": False, "error": {"kind": kind, "message": message}}


def ok(result):
    return {"ok": True, "result": result}


class Refuse(Exception):
    """A request that is not carried out; (kind, message) is the reply."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message


# --- operations: their logs, their markers, their children ---------------------------------

def view_log(op_id):
    return f"{OPLOGS}/{op_id}.view.log"


def marker(op_id):
    return f"{MARKERS}/{op_id}.json"


def record(op_id, project, action, status, step, msg):
    """One line of an operation's progress log, in RS's shape."""
    return json.dumps({"op_id": op_id, "project": project, "action": action, "step": step, "status": status,
                       "msg": str(msg)[:MSG_CHARS], "ts": time.time()}) + "\n"


def checked_op_id(op_id):
    if not isinstance(op_id, str) or not OP_ID.fullmatch(op_id):
        raise Refuse("bad_request", "op_id must be a safe file name of at most 64 characters")
    return op_id


def is_running(op_id):
    """True while the operation's child lives: it holds the lock on the marker for its life."""
    try:
        with open(marker(op_id)) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except OSError:
        pass
    return False


def last_record(op_id):
    """The last whole line of the operation's log, or None when there is no log."""
    try:
        with open(view_log(op_id), "rb") as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - TAIL_BYTES))
            lines = [line for line in fh.read().split(b"\n") if line.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            found = json.loads(line)
            if isinstance(found, dict):
                return found
        except ValueError:
            continue
    return {}


def op_state(op_id):
    """How an operation stands. The log's last record decides; where it is not a last word and
    nothing runs any more, the operation ended without one (it was killed)."""
    last = last_record(op_id)
    if last is None:
        raise Refuse("validation", f"there is no operation {op_id}")
    out = {"op_id": op_id, "project": last.get("project"), "verb": last.get("action"), "msg": str(last.get("msg", ""))[:MSG_CHARS]}
    if last.get("status") in ("done", "failed"):
        return dict(out, state=last["status"])
    if is_running(op_id):
        return dict(out, state="running")
    last = last_record(op_id) or {}  # it may have ended between the two looks
    if last.get("status") in ("done", "failed"):
        return dict(out, state=last["status"], msg=str(last.get("msg", ""))[:MSG_CHARS])
    return dict(out, state="failed", msg="The operation ended without a result.")


def ops_in_flight():
    """Every operation whose child lives: {op_id, project, verb, state}, oldest first. A marker
    whose child is gone is cleared on the way."""
    try:
        found = os.listdir(MARKERS)
    except OSError:
        return []
    aged = []
    for name in found:
        with contextlib.suppress(OSError):  # one that ended between the listing and this look is not in flight
            aged.append((os.stat(f"{MARKERS}/{name}").st_mtime, name))
    out = []
    for _age, name in sorted(aged):
        op_id = name.removesuffix(".json")
        if not OP_ID.fullmatch(op_id):
            continue
        if not is_running(op_id):
            with contextlib.suppress(OSError):
                os.remove(marker(op_id))
            continue
        try:
            with open(marker(op_id)) as fh:
                told = json.load(fh)
            out.append({"op_id": op_id, "project": told["project"], "verb": told["verb"], "state": "running"})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def start_operation(op_id, verb, name, limits, spawn):
    """Take the project, make the log and the marker, start the child. Raises Refuse("busy")
    when the project is not free, and Refuse("bad_request") for an op_id already used."""
    held = core.try_lock(name)
    if held is None:
        raise Refuse("busy", f"Something else is being done with {name}; try again when it is over.")
    try:
        os.makedirs(OPLOGS, mode=0o755, exist_ok=True)
        os.makedirs(MARKERS, mode=0o700, exist_ok=True)
        try:
            log_fd = os.open(view_log(op_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_APPEND, 0o644)
        except FileExistsError:
            raise Refuse("bad_request", f"op_id {op_id} was used before") from None
        try:
            os.write(log_fd, record(op_id, name, verb, "step", "accepted", "").encode())
            with open(marker(op_id), "w") as mark:
                json.dump({"op_id": op_id, "project": name, "verb": verb}, mark)
                mark.flush()
                fcntl.flock(mark, fcntl.LOCK_EX)
                spawn({"op_id": op_id, "verb": verb, "name": name, "limits": limits,
                       "lock_fd": held.fileno(), "marker_fd": mark.fileno(), "log_fd": log_fd})
        finally:
            os.close(log_fd)
    finally:
        held.close()  # the child has its own copy by now; this one is let go


def spawn_child(spec, children):
    """A fresh interpreter for one operation. It gets the three descriptors and the spec, and
    nothing of this process's memory: no token, no proof. It stays in this process's group,
    so that the init can end the broker and its operations together."""
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "__run-op", json.dumps(spec)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             pass_fds=(spec["lock_fd"], spec["marker_fd"], spec["log_fd"]))
    children.append(child)


def run_operation(spec):
    """The child: do the work with the project's lock already in hand, and say how it went."""
    op_id, verb, name = spec["op_id"], spec["verb"], spec["name"]
    core.HELD.add(name)  # handed over by the broker, held through spec["lock_fd"] until let_go(), or this process's end

    def emit(status, step, msg=""):
        os.write(spec["log_fd"], record(op_id, name, verb, status, step, msg).encode())

    def say(message):
        emit("step", "progress", message)

    def let_go():
        """The project is free before the last word is written: whoever reads "done" and asks
        for the next thing at once must not be told "busy"."""
        core.HELD.discard(name)
        with contextlib.suppress(OSError):
            os.close(spec["lock_fd"])
    try:
        if verb == "create":
            out = core.create(name, spec["limits"], say)
        elif verb == "start":
            out = core.start(name, say)
        elif verb == "stop":
            out = core.stop(name)
        elif verb == "recreate":
            out = core.recreate(name, say)
        else:
            out = core.destroy(name)
        notes = " ".join(out.get("notes", []))
        let_go()
        emit("done", "done", f"{name} is {out.get('state', 'done')}." + (f" {notes}" if notes else ""))
        code = 0
    except core.CoreError as exc:
        let_go()
        emit("failed", "failed", str(exc))
        code = 1
    except Exception as exc:  # noqa: BLE001 - whatever it was, the caller is told the operation failed
        traceback.print_exc()
        let_go()
        emit("failed", "failed", f"The operation failed in a way it does not know ({type(exc).__name__}). The container's log says more.")
        code = 1
    with contextlib.suppress(OSError):
        os.remove(marker(op_id))  # after the last word; the marker's lock goes when this process ends
    return code


# --- dispatch --------------------------------------------------------------------------------

def limits_named(args):
    """The limits a request names, as the core takes them: a number, a size as people write
    it ("2g"), or null for no limit. One that is not named is not in the result."""
    given = {}
    for key in LIMITS:
        if key not in args:
            continue
        value = args[key]
        if isinstance(value, bool) or not (value is None or isinstance(value, (int, float, str))):
            raise Refuse("validation", f"{key} must be a number, or null for no limit")
        try:
            given[key] = None if value is None else core.limit_option(value if isinstance(value, str) else repr(value), key)
            core.checked_limits({key: given[key]})
        except core.CoreError as exc:
            raise Refuse("validation", str(exc)) from exc
    return given


def proof_is_right(proof):
    """The proof the browser derives from the password, against the stored hash. With no
    password set, none is right."""
    return isinstance(proof, str) and broker_auth.verify_password(proof, path=broker_auth.PASSWD_FILE)


def dispatch(verb, args, token=None, tokens=None, *, op_id=None, audit=None, spawn=None, sync_tokens=None):
    """Resolve and run one verb, mapping every outcome to a reply. Pure by default: `audit`,
    if given, is called audit(principal, verb, outcome) for logins and writes; `spawn`, if
    given, starts an operation's child (without it an operation is gated and checked but not
    started); `sync_tokens`, if given, is called before any token is looked at, and ends
    every session when the password has changed."""
    def audited(principal, outcome, reply):
        if audit is not None:
            audit(principal, verb, outcome)
        return reply

    if sync_tokens is not None:
        sync_tokens()

    if verb in AUTH_VERBS:
        if not isinstance(args, dict):
            return err("bad_request", "args must be a JSON object")
        if tokens is None:
            return err("auth", "invalid credentials")
        if verb == "login":
            proof = args.get("proof")
            if not proof_is_right(proof):
                return audited(None, "auth_fail", err("auth", "invalid credentials"))
            issued, expires_at = tokens.issue(broker_auth.DEFAULT_PRINCIPAL)
            return audited(broker_auth.DEFAULT_PRINCIPAL, "ok",
                           ok({"token": issued, "expires_at": expires_at, "principal": broker_auth.DEFAULT_PRINCIPAL}))
        principal = tokens.principal_for(token)
        if isinstance(token, str):
            tokens.revoke(token)
        return audited(principal, "logout", ok({"logged_out": True}))

    if not isinstance(verb, str) or verb not in FIELDS:
        return err("unknown_verb", f"unknown verb {verb!r}; allowed: {sorted(FIELDS)}")
    if not isinstance(args, dict):
        return err("bad_request", "args must be a JSON object")

    # Deny by default: anything that is not an open read needs a valid token.
    principal = None
    if verb not in OPEN_VERBS:
        principal = tokens.principal_for(token) if tokens is not None else None
        if principal is None:
            return audited(None, "unauthorized", err("unauthorized", "a valid session token is required; call login"))
    write = verb in OP_VERBS or verb == "limits"

    def answered(outcome, reply):
        return audited(principal, outcome, reply) if write else reply

    # Step-up, after the token gate: a caller without a token never learns it exists. The proof
    # is checked here and taken out of the request; nothing below ever sees it.
    args = dict(args)
    if verb in STEP_UP_VERBS:
        proof = args.pop("proof", None)
        if not proof_is_right(proof):
            return audited(principal, "step_up_fail", err("step_up_required", "this action requires re-entering your password"))

    try:
        unknown = sorted(set(args) - FIELDS[verb])
        if unknown:
            raise Refuse("validation", f"{verb} takes {sorted(FIELDS[verb]) or 'no fields'}; not {unknown}")
        name = None
        if "name" in FIELDS[verb]:
            try:
                name = core.valid_name(args.get("name"))
            except core.CoreError as exc:
                raise Refuse("validation", str(exc)) from exc

        if verb == "list":
            return ok(core.list_projects())
        if verb == "status":
            return ok(core.status(name))
        if verb == "op":
            return ok(op_state(checked_op_id(args.get("op_id"))))
        if verb == "build_alive":
            return ok({"alive": is_running(checked_op_id(args.get("op_id")))})
        if verb == "lifecycle_active":
            return ok({"ops": ops_in_flight()})

        if verb == "limits":
            changed = limits_named(args)
            held = core.try_lock(name)
            if held is None:
                raise Refuse("busy", f"Something else is being done with {name}; try again when it is over.")
            core.HELD.add(name)
            try:
                return answered("ok", ok(core.set_limits(name, **changed)))
            finally:
                core.HELD.discard(name)
                held.close()

        limits = limits_named(args) if verb == "create" else None
        checked_op_id(op_id)
        if spawn is not None:
            start_operation(op_id, verb, name, limits, spawn)
        return answered("ok", ok({"op_id": op_id, "state": "running", "started": True}))
    except Refuse as refused:
        return answered(refused.kind, err(refused.kind, refused.message))
    except core.CoreError as exc:
        return answered("failed", err("failed", str(exc)))
    except OSError as exc:  # a full volume, a child that cannot be started: said without a path, and audited like any failure
        return answered("failed", err("failed", exc.strerror or "an input or output error"))


# --- the socket ------------------------------------------------------------------------------

def peer_is_self(conn):
    """The connecting process's user must be this one's (SO_PEERCRED), on top of the socket's mode."""
    try:
        _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    except OSError:
        return False
    if uid != os.getuid():
        log(f"peer refused: uid {uid} is not {os.getuid()}")
    return uid == os.getuid()


class Handler(socketserver.StreamRequestHandler):
    timeout = READ_TIMEOUT_S  # for writing the reply; reading has a deadline for the whole request, below

    def read_exact(self, count, deadline):
        """`count` bytes of the request, or fewer when the peer closed. The whole request has
        one deadline: a peer that sends half of it, or a byte now and then, does not hold the
        broker, which serves one request at a time."""
        got = b""
        while len(got) < count:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self.connection.settimeout(left)
            chunk = self.connection.recv(count - len(got))
            if not chunk:
                break
            got += chunk
        return got

    def handle(self):
        if not peer_is_self(self.connection):
            return self.send(err("forbidden", "peer is not the owning user"))
        deadline = time.monotonic() + READ_TIMEOUT_S
        try:
            header = self.read_exact(LEN.size, deadline)
            if len(header) != LEN.size:
                return None  # the client closed, or sent nothing
            (length,) = LEN.unpack(header)
            if length > MAX_REQUEST_BYTES:
                return self.send(err("bad_request", f"request too large: {length} > {MAX_REQUEST_BYTES}"))
            body = self.read_exact(length, deadline)
        except (TimeoutError, OSError):
            return None
        finally:
            self.connection.settimeout(READ_TIMEOUT_S)
        if len(body) != length:
            return self.send(err("bad_request", "truncated request body"))
        try:
            message = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            return self.send(err("bad_request", f"invalid JSON: {exc}"))
        if not isinstance(message, dict):
            return self.send(err("bad_request", "request must be a JSON object"))
        server = self.server
        try:
            reply = dispatch(message.get("verb"), message.get("args") or {}, message.get("token"), server.tokens,
                             op_id=message.get("op_id"), audit=broker_auth.audit_event,
                             spawn=lambda spec: spawn_child(spec, server.children), sync_tokens=server.sync_tokens)
        except Exception as exc:  # noqa: BLE001 - one request's fault is that request's failure, not the broker's end
            traceback.print_exc()
            reply = err("failed", f"the broker failed in a way it does not know ({type(exc).__name__})")
        return self.send(reply)

    def send(self, reply):
        data = json.dumps(reply).encode()
        with contextlib.suppress(OSError):
            self.wfile.write(LEN.pack(len(data)) + data)


def password_signature():
    """What changes when the password file is written anew, or None when there is none."""
    try:
        found = os.stat(broker_auth.PASSWD_FILE)
    except OSError:
        return None
    return (found.st_mtime_ns, found.st_ino, found.st_size)


class Server(socketserver.UnixStreamServer):
    def __init__(self, path):
        self.tokens = broker_auth.TokenStore()
        self.children = []
        self.password_seen = password_signature()
        if self.password_seen is None:
            log("no password is set, so nobody can log in: `docker exec -i clanker-learn cl passwd` sets one")
        super().__init__(path, Handler)

    def server_bind(self):
        # A socket left by a broker that was killed is cleared; the new one is 0600 from the
        # start (by the umask, not by a chmod after the bind).
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.server_address)
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)

    def sync_tokens(self):
        """A changed password ends every session."""
        now = password_signature()
        if now != self.password_seen:
            self.password_seen = now
            self.tokens.revoke_all()
            log("the password was changed; every session is ended")

    def service_actions(self):
        """Between requests: collect the operations that ended."""
        self.children[:] = [child for child in self.children if child.poll() is None]


def serve(path=SOCKET):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    os.makedirs(OPLOGS, mode=0o755, exist_ok=True)
    os.makedirs(MARKERS, mode=0o700, exist_ok=True)
    os.makedirs(str(broker_auth.BROKER_DIR), mode=0o700, exist_ok=True)
    server = Server(path)

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    log(f"listening on {path}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with contextlib.suppress(OSError):
            os.unlink(path)
    log("stopped")
    return 0


# --- a client, for tests and for someone with a shell in the container ---------------------

def recv_exact(sock, count):
    chunks, got = [], 0
    while got < count:
        chunk = sock.recv(count - got)
        if not chunk:
            raise ConnectionError("the broker closed the connection mid-frame")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def client_call(verb, args=None, *, token=None, op_id=None, socket_path=SOCKET, timeout=CLIENT_TIMEOUT_S):
    """Send one framed request and return the reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        payload = {"verb": verb, "args": args or {}}
        if token is not None:
            payload["token"] = token
        if op_id is not None:
            payload["op_id"] = op_id
        request = json.dumps(payload).encode()
        sock.sendall(LEN.pack(len(request)) + request)
        (length,) = LEN.unpack(recv_exact(sock, LEN.size))
        return json.loads(recv_exact(sock, length))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["serve"]:
        return serve()
    if argv[:1] == ["__run-op"] and len(argv) == 2:
        return run_operation(json.loads(argv[1]))
    if argv[:1] == ["call"] and 2 <= len(argv) <= 5:
        # call <verb> [<args as JSON> [<token> [<op_id>]]]
        args = json.loads(argv[2]) if len(argv) > 2 else {}
        print(json.dumps(client_call(argv[1], args, token=(argv[3] or None) if len(argv) > 3 else None, op_id=argv[4] if len(argv) > 4 else None)))
        return 0
    print("usage: broker.py serve | broker.py call <verb> [<args as JSON> [<token> [<op_id>]]]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
