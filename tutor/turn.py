"""A turn: the learner's message to the project's Claude Code, in its sandbox, over SSH.

As step 0's spike drove it (its drive.py: the launch over SSH, stream-json in and out, the
session created then resumed, the interrupt as a control message, the run's processes found
by a tag in their environment and ended as root), with gaggiclanker's reader of the stream
(claude_stream.py).

The service starts `ssh` as a child, with the project's key and known_hosts, to
learner@cl-sb-<project>, which runs Claude Code in /workspace with the tutor's text appended
to its system prompt. One Claude Code session per lesson: `--session-id` at its first turn,
`--resume` afterwards. No model is named: the project's own settings decide, and its own login
is used unless a token is set in Settings. The token reaches the turn only through the
environment (sent over SSH as CL_CLAUDE_TOKEN, exported as CLAUDE_CODE_OAUTH_TOKEN by the remote
command before it becomes Claude Code): never on a command line, in a log or in an event.
CL_RUN_ID, the run's id, is in the environment of Claude Code and of every command it runs:
Stop and the clean-up at start end exactly the processes that carry it, and never those of
another lesson's turn in the same sandbox.
"""
import asyncio
import contextlib
import json
import logging
import os
import shlex
import uuid

import store
from claude_stream import StreamReader

log = logging.getLogger("tutor")

DATA = os.environ.get("CL_TUTOR_DATA", "/data/tutor")
KEYS = f"{DATA}/keys"
TOKEN_FILE = f"{DATA}/claude_token"
TUTOR_TEXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tutor.md")
USER = "learner"
IDLE_S = 30 * 60           # no overall limit (installing software takes long), but a line every half hour
STOP_GRACE_S = 5           # after the interrupt, before the run's processes are ended as root
END_S = 15                 # after the result, for Claude Code to end once its input is closed
CLEANUP_S = 30
LINE_LIMIT = 16 * 1024 * 1024   # a whole file read by a tool comes back on one line
STDERR_KEEP = 4096

START_PROJECT = "Start the project first."
NO_CLAUDE = "This project has no Claude Code: add it in the project's gear, under Agents."
NOT_LOGGED_IN = "Claude Code is not logged in in this project: run `claude` in its Shell and log in, or set a token in Settings."
SESSION_GONE_NOTE = ("*The tutor's earlier turns of this lesson are gone from the project, so it starts this one afresh: "
                     "it no longer has what was said before.*\n\n")
UNREACHABLE = ("Could not resolve hostname", "Name or service not known", "Temporary failure in name resolution", "Connection refused",
               "No route to host", "timed out", "Connection closed by", "Connection reset")
LOGIN_SIGNS = ("/login", "not logged in", "invalid api key", "oauth token", "authentication_error", "invalid bearer token",
               "credit balance", "please log in")

# The run's processes, found as the sandbox user and ended as root. Root in a sandbox has no
# CAP_SYS_PTRACE, so it cannot read the environment of the learner's processes, while the
# learner can; and a plain user cannot signal what runs under `sudo` (which also drops the
# tag), so those are found as descendants of a tagged process and ended by root. Printed: the
# pids of every process whose environment holds exactly CL_RUN_ID=<the id>, with all of their
# descendants. This script's own process does not carry the tag: the clean-up's SSH call does
# not send it.
FIND_TAGGED = """
import os, sys
tag = ("CL_RUN_ID=" + sys.argv[1]).encode()
me, children, found = os.getpid(), {}, set()
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            children.setdefault(fh.read().rpartition(")")[2].split()[1], []).append(pid)
        with open(f"/proc/{pid}/environ", "rb") as fh:
            if tag in fh.read().split(b"\\0") and int(pid) != me:
                found.add(pid)
    except OSError:
        pass
todo = list(found)
while todo:
    for child in children.get(todo.pop(), []):
        if child not in found and int(child) != me:
            found.add(child)
            todo.append(child)
print(" ".join(sorted(found)))
"""
# Three passes, so that a child forked meanwhile goes too; prints how many were ended.
KILL_TAGGED = ('n=0; for pass in 1 2 3; do p=$(python3 -c "$1" "$2"); [ -n "$p" ] || break; '
               'sudo -n kill -9 $p 2>/dev/null; n=$((n + $(echo $p | wc -w))); done; echo $n')


def tutor_text():
    with open(TUTOR_TEXT) as fh:
        return fh.read().strip()


def read_token():
    try:
        with open(TOKEN_FILE) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


def write_token(token):
    """The token in a file of mode 0600, or no file when it is empty."""
    if not token:
        with contextlib.suppress(FileNotFoundError):
            os.remove(TOKEN_FILE)
        return
    tmp = TOKEN_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write(token + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)


def ssh_argv(project, send=()):
    """ssh to the project's sandbox with the project's key, its host key checked against the
    known_hosts the core wrote, and nothing asked of anyone."""
    keys = f"{KEYS}/{project}"
    argv = ["ssh", "-F", "/dev/null", "-T", "-i", f"{keys}/id_ed25519", "-o", "IdentitiesOnly=yes",
            "-o", f"UserKnownHostsFile={keys}/known_hosts", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", "-o", "LogLevel=ERROR"]
    for name in send:
        argv += ["-o", f"SendEnv={name}"]
    return [*argv, f"{USER}@cl-sb-{project}"]


def claude_command(session_id, resume, text):
    flags = ["claude", "--print", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", "--permission-mode", "bypassPermissions", "--append-system-prompt", text,
             "--resume" if resume else "--session-id", session_id]
    # The token, when one came, becomes Claude Code's here, in the environment only.
    return ('cd /workspace && if [ -n "${CL_CLAUDE_TOKEN:-}" ]; then export CLAUDE_CODE_OAUTH_TOKEN="$CL_CLAUDE_TOKEN"; fi; '
            "unset CL_CLAUDE_TOKEN; exec " + " ".join(shlex.quote(part) for part in flags))


async def kill_tagged(project, run_id):
    """End every process in the sandbox that carries this run's CL_RUN_ID, and its descendants. Best effort:
    a sandbox that is gone has nothing left to end."""
    if not os.path.isfile(f"{KEYS}/{project}/id_ed25519"):
        return
    remote = "sh -c " + " ".join(shlex.quote(part) for part in (KILL_TAGGED, "kill-tagged", FIND_TAGGED, str(run_id)))
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(*ssh_argv(project), remote, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), CLEANUP_S)
        log.info("run %s: ended %s process(es) in %s%s", run_id, out.decode(errors="replace").strip() or "no",
                 project, f" ({err.decode(errors='replace').strip()[-200:]})" if proc.returncode else "")
    except (OSError, TimeoutError) as exc:
        log.info("run %s: the clean-up in %s did not run (%s)", run_id, project, type(exc).__name__)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


class Turn:
    """What the service holds of a running turn."""

    def __init__(self, lesson, run_id):
        self.lesson_id, self.project, self.created = lesson["id"], lesson["project"], lesson["created"]
        self.run_id = run_id
        self.seq = 0
        self.proc = None
        self.cancel_requested = False
        self.done = asyncio.Event()
        self.stretch = {"text": [], "tools": []}   # the assistant's message being written
        self.tools = {}                            # call id: (its dict, the stored message's id or None)


class Runner:
    """The running turns, one per lesson at most, and their events."""

    def __init__(self, db, bus, tasks):
        self.db, self.bus, self.tasks = db, bus, tasks
        self.by_lesson = {}
        self.by_run = {}
        self.text = tutor_text()

    # --- what the routes ask ---

    def running_run(self, lesson_id):
        turn = self.by_lesson.get(lesson_id)
        return turn.run_id if turn else None

    def is_running(self, run_id):
        return run_id in self.by_run

    def replay(self, run_id, after):
        return [dict(event, run_id=run_id) for event in store.events_since(self.db, run_id, after)]

    def activity(self):
        return [{"project": t.project, "created": t.created, "lesson_id": t.lesson_id, "run_id": t.run_id} for t in self.by_lesson.values()]

    def start(self, lesson, text):
        """The learner's message stored and its turn started, or None when a turn of this
        lesson is running. Nothing awaits between the check and the claim."""
        if lesson["id"] in self.by_lesson:
            return None
        run = store.start_run(self.db, lesson["id"])
        message = store.add_message(self.db, lesson["id"], run["id"], "user", text)
        store.touch_lesson(self.db, lesson["id"])
        turn = Turn(lesson, run["id"])
        self.by_lesson[lesson["id"]] = self.by_run[run["id"]] = turn
        self.tasks.spawn(f"run-{run['id']}", self._turn(turn, text))
        return store.run_out(run), message

    async def cancel(self, run_id):
        turn = self.by_run.get(run_id)
        if turn is not None and not turn.cancel_requested:
            turn.cancel_requested = True
            # Claude Code's own interrupt first; it ends the turn with a result line.
            await self._write(turn, {"type": "control_request", "request_id": f"stop-{run_id}", "request": {"subtype": "interrupt"}})
            self.tasks.spawn(f"stop-{run_id}", self._force_stop(turn))
        row = store.run(self.db, run_id)
        return store.run_out(row) if row else None

    async def cleanup_at_start(self, found):
        """What the runs found running at start left in their sandboxes, ended (best effort)."""
        for run_id, project in found:
            await kill_tagged(project, run_id)

    # --- the turn ---

    def emit(self, turn, kind, data):
        """Stored with its sequence number, then published: a stream that replays finds it."""
        turn.seq += 1
        store.append_event(self.db, turn.run_id, turn.seq, kind, data)
        self.bus.publish({"run_id": turn.run_id, "seq": turn.seq, "kind": kind, "data": data})

    def on_event(self, turn, kind, data):
        stretch = turn.stretch
        if kind == "text_delta":
            if stretch["tools"]:   # text after tools: the message so far is one stretch of the answer
                self.flush(turn)
            turn.stretch["text"].append(data["text"])
        elif kind == "tool_call":
            tool = {"id": data["id"], "kind": data["kind"], "label": data["label"], "detail": data["detail"], "ok": None, "output": ""}
            stretch["tools"].append(tool)
            turn.tools[data["id"]] = (tool, None)
        elif kind == "tool_result" and data["id"] in turn.tools:
            tool, stored = turn.tools[data["id"]]
            tool.update(ok=data["ok"], output=data["output"])
            if stored is not None:   # its message was stored already: keep it up to date
                row = self.db.one("SELECT tools_json FROM messages WHERE id = ?", (stored,))
                tools = [dict(t, ok=tool["ok"], output=tool["output"]) if t["id"] == tool["id"] else t for t in json.loads(row["tools_json"])]
                self.db.execute("UPDATE messages SET tools_json = ? WHERE id = ?", (json.dumps(tools), stored))
        self.emit(turn, kind, data)

    def flush(self, turn):
        stretch = turn.stretch
        text = "".join(stretch["text"])
        if text or stretch["tools"]:
            message = store.add_message(self.db, turn.lesson_id, turn.run_id, "assistant", text, stretch["tools"])
            for tool in stretch["tools"]:
                turn.tools[tool["id"]] = (tool, message["id"])
            self.emit(turn, "message", {"message": message})
        turn.stretch = {"text": [], "tools": []}

    async def _write(self, turn, obj):
        proc = turn.proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            return
        try:
            proc.stdin.write((json.dumps(obj) + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionError, RuntimeError):
            pass

    async def _force_stop(self, turn):
        """If the interrupt did not end the turn in STOP_GRACE_S, its processes are ended as root
        in the sandbox and the SSH child is ended."""
        try:
            await asyncio.wait_for(turn.done.wait(), STOP_GRACE_S)
            return
        except TimeoutError:
            pass
        await kill_tagged(turn.project, turn.run_id)
        if turn.proc is not None and turn.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                turn.proc.kill()

    async def _turn(self, turn, text):
        status, error, usage = "failed", "", None
        try:
            lesson = store.lesson(self.db, turn.lesson_id)
            session_id, resume = lesson["session_id"], bool(lesson["session_started"])
            for _attempt in range(3):
                outcome, error, usage, seen = await self._attempt(turn, text, session_id, resume)
                if seen:
                    store.set_session(self.db, turn.lesson_id, session_id, True)
                if outcome == "session gone":      # its files are gone: a fresh session, said in the conversation
                    session_id, resume = str(uuid.uuid4()), False
                    store.set_session(self.db, turn.lesson_id, session_id, False)
                    self.on_event(turn, "text_delta", {"text": SESSION_GONE_NOTE})
                    continue
                if outcome == "session exists":    # made by a turn that failed after creating it
                    resume = True
                    store.set_session(self.db, turn.lesson_id, session_id, True)
                    continue
                status = outcome
                break
            else:
                status, error = "failed", "The tutor's session for this lesson could not be started."
        except Exception as exc:
            log.exception("run %s failed", turn.run_id)
            status, error = "failed", f"The tutor service failed during this turn ({type(exc).__name__})."
        finally:
            try:
                self.flush(turn)
                if status == "ok":
                    self.emit(turn, "completed", {"usage": usage})
                elif status == "cancelled":
                    self.emit(turn, "cancelled", {})
                else:
                    self.emit(turn, "error", {"message": error})
                store.finish_run(self.db, turn.run_id, status, error if status == "failed" else "", usage)
                store.touch_lesson(self.db, turn.lesson_id)
            finally:
                self.by_lesson.pop(turn.lesson_id, None)
                self.by_run.pop(turn.run_id, None)
                turn.done.set()

    async def _attempt(self, turn, text, session_id, resume):
        """One launch of Claude Code. Returns (outcome, error, usage, whether its session
        started): outcome is "ok", "cancelled", "failed", or "session gone" / "session exists"
        for the caller to try again."""
        if turn.cancel_requested:
            return "cancelled", "", None, False
        if not os.path.isfile(f"{KEYS}/{turn.project}/id_ed25519"):
            return "failed", START_PROJECT, None, False
        token = read_token()
        env = dict(os.environ, CL_RUN_ID=str(turn.run_id))
        send = ["CL_RUN_ID"]
        if token:
            env["CL_CLAUDE_TOKEN"] = token
            send.append("CL_CLAUDE_TOKEN")
        reader = StreamReader(lambda kind, data: self.on_event(turn, kind, data))
        proc = await asyncio.create_subprocess_exec(*ssh_argv(turn.project, send), claude_command(session_id, resume, self.text), env=env,
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT)
        turn.proc = proc
        stderr = bytearray()

        async def drain_stderr():
            while chunk := await proc.stderr.read(4096):
                stderr.extend(chunk)
                del stderr[:-STDERR_KEEP]
        err_task = asyncio.ensure_future(drain_stderr())
        idle = False
        try:
            await self._write(turn, {"type": "user", "message": {"role": "user", "content": text}})
            if turn.cancel_requested:   # Stop came while it was starting: the interrupt goes now
                await self._write(turn, {"type": "control_request", "request_id": f"stop-{turn.run_id}", "request": {"subtype": "interrupt"}})
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), END_S if reader.result else IDLE_S)
                except ValueError:      # a line longer than LINE_LIMIT: skipped
                    continue
                except TimeoutError:
                    idle = reader.result is None
                    break
                if not line:
                    break
                reader.feed(line)
                if reader.result is not None and proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()   # the turn is over: Claude Code ends once its input is closed
        finally:
            if idle:
                await kill_tagged(turn.project, turn.run_id)
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), END_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(err_task, 5)
            turn.proc = None
        said = stderr.decode(errors="replace").strip()
        return (*judge(reader, proc.returncode, said, turn.cancel_requested, idle), reader.usage(), reader.session_seen)


def judge(reader, code, stderr, cancelled, idle):
    """(outcome, error) of a launch, from its result line, its exit code and what it said on
    standard error: a sentence a person can act on where there is one."""
    if cancelled:
        return "cancelled", ""
    if idle:
        return "failed", "The tutor's Claude Code wrote nothing for 30 minutes, so the turn was ended."
    result = reader.result
    said = " ".join(str(part) for part in ((result or {}).get("result"), *((result or {}).get("errors") or []), stderr) if part)
    lowered = said.lower()
    if "no conversation found" in lowered:
        return "session gone", ""
    if "already in use" in lowered and "session" in lowered:
        return "session exists", ""
    if result is not None and not result.get("is_error"):
        return "ok", ""
    if any(sign in lowered for sign in LOGIN_SIGNS) or (result or {}).get("api_error_status") in (401, 403):
        return "failed", NOT_LOGGED_IN
    if result is not None:
        return "failed", "Claude Code reported an error: " + (said[:300] or str(result.get("subtype") or "with no reason"))
    if code == 127 and "no claude code" in lowered:
        return "failed", NO_CLAUDE
    if code == 255 and any(sign in stderr for sign in UNREACHABLE):
        return "failed", START_PROJECT
    if code == 255:
        return "failed", "The tutor could not log in to the project's sandbox: " + (stderr.splitlines()[-1][:300] if stderr else "ssh failed")
    last = stderr.splitlines()[-1][:300] if stderr else "it said nothing"
    return "failed", f"Claude Code ended without an answer (exit {code}): {last}"
