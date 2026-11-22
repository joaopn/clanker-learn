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
  * Deny by default: a verb that is not an open read (OPEN_VERBS) or one of the two set-up
    verbs needs a session token. `login` checks a proof derived from the password
    (broker_auth) and issues one; it is held only in this process's memory, so a restart of
    the broker ends every session, and so does a change of the password.
  * The first password can be set without a token, with the one-time set-up code the init
    makes at a start with no password set, prints in the container's log and hands to this
    process on its standard input. The code is held only in memory. `setup_needed` says
    whether a first password can be set now; `first_password` sets it, never over an existing
    one, and ten wrong codes within a minute shut it for a minute. Its code and proof reach no
    log, no audit line and no child.
  * `attach` hands out what the web server's terminal logs in to a running sandbox with,
    the project's Shell password among it. It needs a token, is audited (the outcome only)
    and is answered at once, without the project's lock; the password is in the reply and
    nowhere else: no log, no audit line, no error text.
  * Step-up: `destroy` needs the proof again in the request. It is checked after the token,
    so a caller without a token never learns that it exists, and it is taken out of the
    request there: it reaches no log and no child.
  * Each verb takes only its own fields; any other is refused.
  * The ports: `port_detect` reads what listens in a running sandbox from this container's
    side (nothing is run in the sandbox) and is not audited, since the page polls it.
    `port_add`, `port_remove` and `port_list` keep the core's registry of the ports named by
    hand; the first two are audited. All four need a token. Like `limits`, `port_add` and
    `port_remove` are answered at once under the project's lock, taken without waiting.
  * The gates come in this order: a changed password's end of sessions, auth verbs, unknown verb, args an object, the set-up verbs,
    token, step-up, the verb's fields and name, then the verb's own: op_id and the project's
    lock for an operation, the project's lock for limits, port_add and port_remove, nothing
    more for attach, port_detect and port_list (the core checks the rest of every verb's
    fields). A caller without a token learns nothing beyond the three open reads and whether
    a first password can be set.
  * The socket is 0600 and the peer must be this process's own user.
  * A request larger than MAX_REQUEST_BYTES is refused before its body is read, and a
    request that has not arrived whole within READ_TIMEOUT_S is dropped.

Settings -> Software (RS's verbs): `software_status` and `models` are reads,
`agent_refresh_check` and `editor_refresh_check` ask upstream in the request (bounded by the
core), `pin_reset` is answered at once. A project's agents: `agents_model` is answered at
once under the project's lock, like `limits`.

The verbs that can take long (create, start, stop, recreate, destroy, agents_add,
agents_update) run as operations: a child of this process, a fresh interpreter given only the
verb, the checked name, limits and choices and the caller's op_id, which calls the core and
writes RS's progress log,
<the socket's folder>/oplogs/<op_id>.view.log: JSON lines {op_id, project, action, step,
status, msg, ts}, the last one with status "done" or "failed". The reply returns at once.

One operation runs per project, and what says so is the core's own lock on the project: the
broker takes it without waiting ("busy" when something holds it, be it another operation, a
`cl` command or the init), hands the held lock to the child, and lets go. The kernel then
keeps it for as long as the child holds it, across a restart of this broker, with no pid
written anywhere; the child lets go when the work is done, just before its last word, or by
ending. A second lock, on a marker file per operation, held to the child's end, says in the
same way whether the operation still runs.

The builds (agent_pull, agent_refresh, editor_pull, editor_refresh) are operations that
belong to no project, RS's build lane: what is taken without waiting and handed to the child
is the core's build lane instead of a project's lock, one build at a time ("busy" with the
core's sentence while another runs; RS queues builds of other targets). Their records carry
`project: null`, as RS's; `build_active` lists them, `lifecycle_active` the project ones.

Settings -> Backup: `backup` is an operation that belongs to no project, as a build is, on a
lane of its own (the core's backup lane: one backup or restore at a time); it does not hold
the build lane, since what a build changes (the distributions) is not in a backup. The child
copies each project under that project's lock, waiting for an operation on it to end, so that
a create, destroy or start is never cut through; while it copies a project, that project's
operations are refused as busy. `backup_active` lists the backup in flight. `backup_list` is a
read; `backup_rm` (step-up: a backup may be the only copy of what was destroyed, as RS rules
for its exports) and `backup_fetch` are audited and answered at once. `backup_fetch` links one
backup into the web server's own folder for one download (core.backup_fetch says why not the
folder of backups). A restore is not a verb: it replaces everything, and is the `cl` command
only.
"""
import base64
import contextlib
import fcntl
import hmac
import json
import math
import os
import pathlib
import re
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
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

SETUP_CODE = re.compile(rb"[a-z0-9]{20}\n?")  # as the init makes it; one newline after it is tolerated
SETUP_CODE_BYTES = 256                         # read at most this much of standard input
NO_CODE = "-" * 20                             # compared with when no code is held: the time taken says nothing
WRONG_CODES, WRONG_WINDOW_S, SHUT_S = 10, 60, 60

AUTH_VERBS = ("login", "logout")
SETUP_VERBS = frozenset({"setup_needed", "first_password"})
OPEN_VERBS = frozenset({"list", "status", "op"})
STEP_UP_VERBS = frozenset({"destroy", "backup_rm"})
# Operations on a project: a child holds the project's lock (agents_add and agents_update run
# here as RS runs its `update` and `agents_update`: on its lifecycle lane, with a log).
OP_VERBS = frozenset({"create", "start", "stop", "recreate", "destroy", "agents_add", "agents_update"})
# Operations that belong to no project (RS's build lane): a child holds the core's build lane
# instead. Their log records carry `project: null`, as RS's do (its build ops have no name).
BUILD_VERBS = frozenset({"agent_pull", "agent_refresh", "editor_pull", "editor_refresh"})
# Every operation that belongs to no project: the builds, and the backup (on its own lane).
LANE_VERBS = BUILD_VERBS | {"backup"}
# Audited like the operations: `attach` hands out a credential, the others change something.
AUDITED_VERBS = OP_VERBS | LANE_VERBS | {"limits", "attach", "port_add", "port_remove", "agents_model", "pin_reset", "backup_rm", "backup_fetch"}
# Answered by this process at once, under the project's lock taken without waiting.
LOCKED_VERBS = frozenset({"limits", "port_add", "port_remove", "agents_model"})
LIMITS = ("pids", "memory", "cpus")
CREATE_SOFTWARE = ("agents", "editor", "claude_model", "claude_effort")
FIELDS = {
    "list": frozenset(),
    "status": frozenset({"name"}),
    "op": frozenset({"op_id"}),
    "build_alive": frozenset({"op_id"}),
    "lifecycle_active": frozenset(),
    "build_active": frozenset(),
    "software_status": frozenset(),
    "models": frozenset(),
    "agent_refresh_check": frozenset({"agent"}),
    "editor_refresh_check": frozenset(),
    "agent_pull": frozenset({"agent"}),
    "agent_refresh": frozenset({"agent"}),
    "editor_pull": frozenset(),
    "editor_refresh": frozenset(),
    "pin_reset": frozenset({"software"}),
    "agents_add": frozenset({"name", "agent"}),
    "agents_update": frozenset({"name"}),
    "agents_model": frozenset({"name", "model", "effort"}),
    "create": frozenset({"name", *LIMITS, *CREATE_SOFTWARE}),
    "start": frozenset({"name"}),
    "stop": frozenset({"name"}),
    "recreate": frozenset({"name"}),
    "limits": frozenset({"name", *LIMITS}),
    "destroy": frozenset({"name"}),
    "attach": frozenset({"name"}),
    "port_detect": frozenset({"name"}),
    "port_add": frozenset({"name", "port", "label", "passthrough", "host_port"}),
    "port_remove": frozenset({"name", "port", "passthrough"}),
    "port_list": frozenset({"name"}),
    "setup_needed": frozenset(),
    "first_password": frozenset({"code", "proof"}),
    "backup": frozenset(),
    "backup_active": frozenset(),
    "backup_list": frozenset(),
    "backup_rm": frozenset({"backup"}),
    "backup_fetch": frozenset({"backup"}),
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


def ops_in_flight(builds=False, backups=False):
    """Every operation on a project whose child lives: {op_id, project, verb, state}, oldest
    first; with `builds`, every build instead, in RS's build_active shape: {op_id, verb,
    target, agent?, state}; with `backups`, the backup in flight, in the same shape. A marker
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
            if not builds and told["project"] is not None:
                out.append({"op_id": op_id, "project": told["project"], "verb": told["verb"], "state": "running"})
            elif (builds or backups) and told["project"] is None and (told["verb"] == "backup") == backups:
                op = {"op_id": op_id, "verb": told["verb"], "target": told["target"], "state": "running"}
                if isinstance(told.get("agent"), str):
                    op["agent"] = told["agent"]
                out.append(op)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def build_target(verb, agent):
    """What a build makes, in RS's words: "agent:<agent>" or "editor"."""
    return f"agent:{agent}" if verb.startswith("agent_") else "editor"


def start_operation(op_id, verb, name, limits, spawn, extra=None):
    """Take the project (or, for a build, the build lane), make the log and the marker, start
    the child. Raises Refuse("busy") when the project or the lane is not free, and
    Refuse("bad_request") for an op_id already used. `extra` is more of the spec, checked
    values only."""
    extra = dict(extra or {})
    if verb == "backup":
        try:
            lane = core.take_backup_lane()
        except core.CoreError as exc:
            raise Refuse("busy", str(exc)) from exc
        handed, told = {"lane_fds": [fh.fileno() for fh in lane]}, {"target": "backup"}
    elif verb in BUILD_VERBS:
        try:
            lane = core.take_build_lane(extra["software"])
        except core.CoreError as exc:
            raise Refuse("busy", str(exc)) from exc
        handed = {"lane_fds": [fh.fileno() for fh in lane]}
        told = {"target": build_target(verb, extra.get("agent")), **({"agent": extra["agent"]} if "agent" in extra else {})}
    else:
        held = core.try_lock(name)
        if held is None:
            raise Refuse("busy", f"Something else is being done with {name}; try again when it is over.")
        lane, handed, told = [held], {"lock_fd": held.fileno()}, {}
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
                json.dump({"op_id": op_id, "project": name, "verb": verb, **told}, mark)
                mark.flush()
                fcntl.flock(mark, fcntl.LOCK_EX)
                spawn({"op_id": op_id, "verb": verb, "name": name, "limits": limits, **extra, **handed,
                       "marker_fd": mark.fileno(), "log_fd": log_fd})
        finally:
            os.close(log_fd)
    finally:
        for fh in lane:
            fh.close()  # the child has its own copy by now; this one is let go


def spawn_child(spec, children):
    """A fresh interpreter for one operation. It gets the three descriptors and the spec, and
    nothing of this process's memory: no token, no proof. It stays in this process's group,
    so that the init can end the broker and its operations together."""
    held = [spec["lock_fd"]] if "lock_fd" in spec else list(spec["lane_fds"])
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "__run-op", json.dumps(spec)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             pass_fds=(*held, spec["marker_fd"], spec["log_fd"]))
    children.append(child)


def what_was_done(verb, name, out):
    """The last word of an operation that went well."""
    if verb == "backup":
        return out["said"]
    if verb in BUILD_VERBS:
        label = core.SOFTWARE[out.get("software", "")]["label"] if out.get("software") in core.SOFTWARE else "The software"
        label = label[0].upper() + label[1:]
        if verb.endswith("_refresh") and not out.get("bumped"):
            return f"{label} {out.get('version')} is in effect and upstream's newest is {out.get('upstream')}: nothing to do."
        return f"{label} {out.get('version')} is pulled."
    if verb in ("agents_add", "agents_update"):
        deployed = ", ".join(f"{agent} {version}" for agent, version in (out.get("deployed") or {}).items())
        return f"Deployed into {name}: {deployed or 'nothing'}."
    notes = " ".join(out.get("notes", []))
    return f"{name} is {out.get('state', 'done')}." + (f" {notes}" if notes else "")


def run_operation(spec):
    """The child: do the work with the project's lock (or the build lane) already in hand, and
    say how it went."""
    op_id, verb, name = spec["op_id"], spec["verb"], spec["name"]
    build = verb in LANE_VERBS
    if verb == "backup":
        core.BACKUP_HELD = True  # handed over by the broker, held through spec["lane_fds"]
        handed = list(spec.get("lane_fds", []))
    elif build:
        core.LANE_HELD = spec["software"]  # handed over by the broker, held through spec["lane_fds"]
        handed = list(spec.get("lane_fds", []))
    else:
        core.HELD.add(name)  # handed over by the broker, held through spec["lock_fd"] until let_go(), or this process's end
        handed = [spec["lock_fd"]]

    def emit(status, step, msg=""):
        os.write(spec["log_fd"], record(op_id, name, verb, status, step, msg).encode())

    def say(message):
        emit("step", "progress", message)

    def let_go():
        """The project is free before the last word is written: whoever reads "done" and asks
        for the next thing at once must not be told "busy"."""
        if build:
            core.LANE_HELD, core.BACKUP_HELD = None, False
        else:
            core.HELD.discard(name)
        for fd in handed:
            with contextlib.suppress(OSError):
                os.close(fd)
    try:
        if verb == "create":
            chosen = {key: spec[key] for key in CREATE_SOFTWARE if key in spec}
            out = core.create(name, spec["limits"], say, **chosen)
        elif verb == "backup":
            out = core.backup(say)
        elif verb == "agent_pull":
            out = core.agent_pull(spec["agent"], say)
        elif verb == "agent_refresh":
            out = core.agent_refresh(spec["agent"], say)
        elif verb == "editor_pull":
            out = core.editor_pull(say)
        elif verb == "editor_refresh":
            out = core.editor_refresh(say)
        elif verb == "agents_add":
            out = core.agents_add(name, spec["agent"])
        elif verb == "agents_update":
            out = core.agents_update(name)
        elif verb == "start":
            out = core.start(name, say)
        elif verb == "stop":
            out = core.stop(name)
        elif verb == "recreate":
            out = core.recreate(name, say)
        else:
            out = core.destroy(name)
        let_go()
        emit("done", "done", what_was_done(verb, name, out))
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


def checked(check, value):
    """A field checked by the core: its refusal is the request's answer."""
    try:
        return check(value)
    except core.CoreError as exc:
        raise Refuse("validation", str(exc)) from exc


def operation_extra(verb, args):
    """What an operation's child is handed besides the name and the limits: checked values
    only, refused before the operation is accepted where the core can say so (an agent that
    is not pulled is the request's answer, not a failed operation)."""
    if verb == "create":
        agents = checked(core.checked_agents, args.get("agents", []))
        editor = checked(core.checked_editor, args.get("editor", False))
        model, effort = (("" if args.get(key) is None else args[key]) for key in ("claude_model", "claude_effort"))
        model, effort = checked(lambda pair: core.checked_claude_pair(*pair), (model, effort))
        checked(lambda chosen: core.refuse_unpulled(*chosen), (agents, editor))
        return {"agents": agents, "editor": editor, "claude_model": model, "claude_effort": effort}
    if verb == "agents_add":
        agent = checked(core.known_agent, args.get("agent", "claude"))
        checked(lambda one: core.refuse_unpulled([one], False), agent)
        return {"agent": agent}
    if verb in ("agent_pull", "agent_refresh"):
        agent = checked(core.known_agent, args.get("agent", "claude"))
        return {"agent": agent, "software": agent}
    if verb in ("editor_pull", "editor_refresh"):
        return {"software": "code-server"}
    return {}


def refresh_check_answer(check):
    """The core's refresh check, in RS's shape ({current, latest, downgrade}, and for an
    agent ext_current, ext_latest, ext_downgrade), with the core's own keys kept beside."""
    out = dict(check, current=check["version"], latest=check["upstream"],
               downgrade=core.is_newer(check["version"], check["upstream"]))
    if "ext_version" in check:
        out.update(ext_current=check["ext_version"], ext_latest=check["ext_upstream"],
                   ext_downgrade=core.is_newer(check["ext_version"], check["ext_upstream"]))
    return out


def models_payload():
    """RS's `models` answer, of what clanker-learn has: the efforts and the tiers (alias,
    label, blurb, the efforts each takes), from the core's catalog file."""
    core.model_catalog()  # the core's check of the file: a refusal in its words
    with open(core.MODELS_FILE) as fh:
        catalog = json.load(fh)
    return {"efforts": list(catalog["efforts"]),
            "models": [{key: m.get(key) for key in ("alias", "label", "blurb", "efforts")} for m in catalog["models"]]}


def proof_is_right(proof):
    """The proof the browser derives from the password, against the stored hash. With no
    password set, none is right."""
    return isinstance(proof, str) and broker_auth.verify_password(proof, path=broker_auth.PASSWD_FILE)


# --- the first password ----------------------------------------------------------------------

class SetupCode:
    """The one-time set-up code, held only here, and the count of wrong codes. `now` is
    injectable so that the shut door can be tested without waiting."""

    def __init__(self, code=None, now=time.monotonic):
        self.code = code
        self.now = now
        self.wrong = []           # when each recent wrong code came
        self.shut_until = None

    def wait_left(self):
        """Seconds until a code is listened to again; 0 when it is now."""
        if self.shut_until is None:
            return 0
        left = self.shut_until - self.now()
        if left <= 0:
            self.shut_until = None
            return 0
        return math.ceil(left)

    def is_right(self, given):
        """Constant-time, on bytes; with no code held, against a dummy that never matches."""
        same = hmac.compare_digest(given.encode("utf-8", "surrogatepass"), (self.code or NO_CODE).encode())
        return same and self.code is not None

    def count_wrong(self):
        now = self.now()
        self.wrong = [when for when in self.wrong if now - when < WRONG_WINDOW_S] + [now]
        if len(self.wrong) >= WRONG_CODES:
            self.shut_until, self.wrong = now + SHUT_S, []


def proof_has_its_form(proof):
    """What derive_login_proof makes: standard base64, padded, of LOGIN_PROOF_DKLEN bytes. A
    first password stored in another form could never be logged in with."""
    try:
        raw = base64.b64decode(proof.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return False
    return len(raw) == broker_auth.LOGIN_PROOF_DKLEN and base64.b64encode(raw).decode("ascii") == proof


def create_password_file(proof):
    """Write the record beside the password file and link it into place: a link never replaces
    a file, so a password set in the same moment (`cl passwd`) is kept. False when one was."""
    target = pathlib.Path(broker_auth.PASSWD_FILE)
    os.makedirs(target.parent, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".passwd-", suffix=".tmp")  # 0600 from its creation
    try:
        os.close(fd)
        broker_auth.set_password(proof, path=pathlib.Path(temporary))
        with open(temporary, "rb") as fh:
            os.fsync(fh.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def first_password(args, setup):
    """Set the first password with the set-up code: (audit outcome, reply). A wrong code, no
    code held and a password that exists already get the login's own refusal, so that this
    verb does not tell whether a password is set."""
    wait = setup.wait_left()
    if wait:
        return "rate_limited", err("rate_limited", f"Too many wrong set-up codes; wait {wait} seconds and try again.")
    code, proof = args.get("code"), args.get("proof")
    if set(args) - FIELDS["first_password"] or not isinstance(code, str) or not isinstance(proof, str):
        return "bad_request", err("bad_request", "first_password takes a code and a proof, both strings, and nothing else")
    if not setup.is_right(code):
        setup.count_wrong()
        return "auth_fail", err("auth", "invalid credentials")
    if broker_auth.password_is_set(path=broker_auth.PASSWD_FILE):
        return "refused", err("auth", "invalid credentials")
    if not proof_has_its_form(proof):
        return "refused", err("validation", "The password did not arrive in the form a login sends it; nothing was stored.")
    try:
        made = create_password_file(proof)
    except OSError as exc:  # said without a path, as elsewhere
        return "failed", err("failed", exc.strerror or "an input or output error")
    if not made:
        return "refused", err("auth", "invalid credentials")
    setup.code = None  # one-time: it has done its work
    return "ok", ok({"password": "set"})


def take_setup_code(fd=0):
    """The set-up code the init hands over on standard input, or None. Read once, bounded, and
    never from a terminal; the descriptor is then /dev/null, so that no child inherits the
    pipe. A malformed code is ignored, and not repeated in the log."""
    raw = b""
    try:
        if not os.isatty(fd):
            deadline = time.monotonic() + READ_TIMEOUT_S
            while len(raw) < SETUP_CODE_BYTES:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([fd], [], [], left)[0]:
                    break
                chunk = os.read(fd, SETUP_CODE_BYTES - len(raw))
                if not chunk:
                    break
                raw += chunk
    except (OSError, ValueError):  # closed, or not something that can be read
        raw = b""
    with contextlib.suppress(OSError):
        null = os.open(os.devnull, os.O_RDONLY)
        if null != fd:  # with the descriptor closed, /dev/null takes its number by itself
            os.dup2(null, fd)
            os.close(null)
    if not raw:
        return None
    if not SETUP_CODE.fullmatch(raw):
        log("a malformed set-up code was given on standard input and is ignored")
        return None
    return raw.decode("ascii").removesuffix("\n")


def dispatch(verb, args, token=None, tokens=None, *, op_id=None, audit=None, spawn=None, sync_tokens=None, setup=None):
    """Resolve and run one verb, mapping every outcome to a reply. Pure by default: `audit`,
    if given, is called audit(principal, verb, outcome) for logins, writes and the first
    password; `spawn`, if given, starts an operation's child (without it an operation is
    gated and checked but not started); `sync_tokens`, if given, is called before any token
    is looked at, and ends every session when the password has changed; `setup`, if given, is
    the SetupCode (without it no first password can be set)."""
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

    # The set-up verbs: open, since they serve before any password exists.
    if verb == "setup_needed":
        if args:
            return err("validation", f"setup_needed takes no fields; not {sorted(args)}")
        return ok({"needed": setup is not None and setup.code is not None and not broker_auth.password_is_set(path=broker_auth.PASSWD_FILE)})
    if verb == "first_password":
        return audited(None, *first_password(args, setup if setup is not None else SetupCode()))

    # Deny by default: anything that is not an open read needs a valid token.
    principal = None
    if verb not in OPEN_VERBS:
        principal = tokens.principal_for(token) if tokens is not None else None
        if principal is None:
            return audited(None, "unauthorized", err("unauthorized", "a valid session token is required; call login"))
    write = verb in AUDITED_VERBS

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
        if verb == "build_active":
            return ok({"ops": ops_in_flight(builds=True)})
        if verb == "backup_active":
            return ok({"ops": ops_in_flight(backups=True)})

        # Settings -> Backup: the list, a removal, a backup made readable for one download.
        if verb == "backup_list":
            return ok(core.backups_list())
        if verb == "backup_rm":
            return answered("ok", ok(core.backup_rm(args.get("backup"))))
        if verb == "backup_fetch":
            return answered("ok", ok(core.backup_fetch(args.get("backup"))))

        # Settings -> Software: reads, the two that ask upstream answered in the request
        # (bounded by the core), and "Use repo version".
        if verb == "software_status":
            return ok(core.software_status())
        if verb == "models":
            return ok(models_payload())
        if verb == "agent_refresh_check":
            return ok(refresh_check_answer(core.agent_refresh_check(checked(core.known_agent, args.get("agent", "claude")))))
        if verb == "editor_refresh_check":
            return ok(refresh_check_answer(core.editor_refresh_check()))
        if verb == "pin_reset":
            checked(core.known_software, args.get("software"))
            return answered("ok", ok(core.pin_reset(args["software"])))

        if verb == "attach":
            return answered("ok", ok(core.attach(name)))
        if verb == "port_detect":
            return ok(core.port_detect(name))
        if verb == "port_list":
            return ok(core.port_list(name))

        if verb in LOCKED_VERBS:
            changed = limits_named(args) if verb == "limits" else None
            held = core.try_lock(name)
            if held is None:
                raise Refuse("busy", f"Something else is being done with {name}; try again when it is over.")
            core.HELD.add(name)
            try:
                if verb == "limits":
                    return answered("ok", ok(core.set_limits(name, **changed)))
                if verb == "agents_model":
                    return answered("ok", ok(core.agents_model(name, args.get("model"), args.get("effort"))))
                if verb == "port_add":
                    return answered("ok", ok(core.port_add(name, args.get("port"), args.get("label"), args.get("passthrough", False), args.get("host_port"))))
                return answered("ok", ok(core.port_remove(name, args.get("port"), args.get("passthrough", False))))
            finally:
                core.HELD.discard(name)
                held.close()

        limits = limits_named(args) if verb == "create" else None
        extra = operation_extra(verb, args)
        checked_op_id(op_id)
        if spawn is not None:
            start_operation(op_id, verb, name, limits, spawn, extra)
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
                             spawn=lambda spec: spawn_child(spec, server.children), sync_tokens=server.sync_tokens,
                             setup=server.setup)
        except Exception as exc:  # noqa: BLE001 - one request's fault is that request's failure, not the broker's end
            if message.get("verb") in ("first_password", "attach"):
                # Its text may hold the set-up code, the proof or the Shell password.
                log(f"a {message.get('verb')} request failed in a way it does not know ({type(exc).__name__})")
            else:
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
    def __init__(self, path, setup_code=None):
        self.tokens = broker_auth.TokenStore()
        self.children = []
        self.setup = SetupCode(setup_code)
        self.password_seen = password_signature()
        if self.password_seen is None and setup_code is not None:
            log("no password is set: set the first one on the page with the set-up code printed in this container's log,"
                " or with `docker exec -i clanker-learn cl passwd`")
        elif self.password_seen is None:
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


def serve(path=SOCKET, setup_code=None):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    os.makedirs(OPLOGS, mode=0o755, exist_ok=True)
    os.makedirs(MARKERS, mode=0o700, exist_ok=True)
    os.makedirs(str(broker_auth.BROKER_DIR), mode=0o700, exist_ok=True)
    server = Server(path, setup_code)

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
        return serve(setup_code=take_setup_code())
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
