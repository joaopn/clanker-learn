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

The tutor's text is the one in force in the database (an editable copy of tutor.md). Claude
Code gets one tool server, "learn" (mcp.py), with --strict-mcp-config; the run's token for it
goes over SSH as CL_RUN_TOKEN and dies with the run. The first message of a new session
carries the lesson's opening context before the learner's text (plans.opening_context), stored
apart from it; a session that is gone, or was made with other instructions, is replaced by a
fresh one told to take the lesson up from its files, transcript.md (written here after every
answer) and notes.md (kept by the tutor).
"""
import asyncio
import contextlib
import json
import logging
import os
import secrets
import shlex
import uuid

import mcp
import plans
import store
from claude_stream import StreamReader

log = logging.getLogger("tutor")

DATA = os.environ.get("CL_TUTOR_DATA", "/data/tutor")
KEYS = f"{DATA}/keys"
TOKEN_FILE = f"{DATA}/claude_token"
TUTOR_TEXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tutor.md")
USER = "learner"
MCP_URL = f"http://cl-tutor:{mcp.PORT}/mcp"
# The one tool server a turn gets. Claude Code expands ${CL_RUN_TOKEN} from its environment,
# where it came over SSH: the token is never on a command line.
MCP_CONFIG = json.dumps({"mcpServers": {"learn": {"type": "http", "url": MCP_URL, "headers": {"Authorization": "Bearer ${CL_RUN_TOKEN}"}}}})
TRANSCRIPT_S = 30
IDLE_S = 30 * 60           # no overall limit (installing software takes long), but a line every half hour
STOP_GRACE_S = 5           # after the interrupt, before the run's processes are ended as root
END_S = 15                 # after the result, for Claude Code to end once its input is closed
CLEANUP_S = 30
LINE_LIMIT = 16 * 1024 * 1024   # a whole file read by a tool comes back on one line
STDERR_KEEP = 4096

START_PROJECT = "Start the project first."
NO_CLAUDE = "This project has no Claude Code: add it in the project's gear, under Agents."
NOT_LOGGED_IN = "Claude Code is not logged in in this project: run claude in its Shell and log in there, or set a token under Settings → Tutor."
SESSION_GONE_NOTE = ("*The tutor's earlier turns of this lesson are gone from the project, so it starts this one afresh "
                     "and takes the lesson up from its files.*\n\n")
SESSION_GONE = "your earlier session of it is gone from the project"
TEXT_CHANGED = "the tutor's instructions changed since your earlier session of it"
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


def shipped_text():
    """The instructions clanker-learn ships: the default of the text in force, which is in the
    database (store.seed_instructions)."""
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
             "--mcp-config", MCP_CONFIG, "--strict-mcp-config",
             "--resume" if resume else "--session-id", session_id]
    # The token, when one came, becomes Claude Code's here, in the environment only. Without
    # ENABLE_TOOL_SEARCH=false Claude Code hides a server's tools behind its tool search, and
    # the model is never told they exist (step 0's spike).
    return ('cd /workspace && if [ -n "${CL_CLAUDE_TOKEN:-}" ]; then export CLAUDE_CODE_OAUTH_TOKEN="$CL_CLAUDE_TOKEN"; fi; '
            "unset CL_CLAUDE_TOKEN; export ENABLE_TOOL_SEARCH=false; exec " + " ".join(shlex.quote(part) for part in flags))


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


async def write_file_as_learner(project, path, text):
    """`text` into `path` in the sandbox, as the sandbox user, whole (a part file moved over the
    old one). Best effort: a sandbox that is not running gets it at the next answer."""
    if not os.path.isfile(f"{KEYS}/{project}/id_ed25519"):
        return False
    folder = shlex.quote(os.path.dirname(path))
    part, final = shlex.quote(os.path.dirname(path) + "/." + os.path.basename(path) + ".part"), shlex.quote(path)
    remote = f"mkdir -p {folder} && cat > {part} && mv -f {part} {final}"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(*ssh_argv(project), remote, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _out, err = await asyncio.wait_for(proc.communicate(text.encode()), TRANSCRIPT_S)
        if proc.returncode:
            log.info("%s in %s was not written: %s", path, project, err.decode(errors="replace").strip()[-200:])
        return proc.returncode == 0
    except (OSError, TimeoutError) as exc:
        log.info("%s in %s was not written (%s)", path, project, type(exc).__name__)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        return False


class Turn:
    """What the service holds of a running turn."""

    def __init__(self, lesson, run_id, token, text):
        self.lesson_id, self.project, self.created = lesson["id"], lesson["project"], lesson["created"]
        self.kind = lesson["kind"]
        self.run_id = run_id
        self.token = token          # the "learn" tools' bearer token of this run, alive while it runs
        self.text = text            # the instructions in force when it started
        self.tool_calls = 0
        self.message_id = None      # the learner's message
        self.notes = []             # the outcomes of proposals it is told, before the learner's text
        self.fresh = None           # why its session is a fresh one taken up from the files, if it is
        self.seq = 0
        self.proc = None
        self.cancel_requested = False
        self.done = asyncio.Event()
        self.stretch = {"text": [], "tools": [], "proposals": []}   # the assistant's message being written
        self.tools = {}                            # call id: (its dict, the stored message's id or None)


class Runner:
    """The running turns, one per lesson at most, and their events."""

    def __init__(self, db, bus, tasks):
        self.db, self.bus, self.tasks = db, bus, tasks
        self.by_lesson = {}
        self.by_run = {}
        self.tokens = {}   # the hash of a running turn's token: the turn

    # --- what the routes ask ---

    def running_run(self, lesson_id):
        turn = self.by_lesson.get(lesson_id)
        return turn.run_id if turn else None

    def is_running(self, run_id):
        return run_id in self.by_run

    def turn_of_token(self, key):
        return self.tokens.get(key)

    def replay(self, run_id, after):
        return [dict(event, run_id=run_id) for event in store.events_since(self.db, run_id, after)]

    def activity(self):
        return [{"project": t.project, "created": t.created, "lesson_id": t.lesson_id, "run_id": t.run_id} for t in self.by_lesson.values()]

    def start(self, lesson, text):
        """The learner's message stored and its turn started, or None when a turn of this
        lesson is running. Nothing awaits between the check and the claim."""
        if lesson["id"] in self.by_lesson:
            return None
        db = self.db
        instructions = store.instructions(db)["text"]
        fresh = None
        if lesson["session_started"] and lesson["session_text"] and lesson["session_text"] != store.text_hash(instructions):
            # The session was made with other instructions, which Claude Code keeps as it first
            # had them: a new one, taken up from the lesson's files.
            fresh = TEXT_CHANGED
            store.set_session(db, lesson["id"], str(uuid.uuid4()), False)
            lesson = store.lesson(db, lesson["id"])
        run = store.start_run(db, lesson["id"])
        token = secrets.token_urlsafe(32)
        turn = Turn(lesson, run["id"], token, instructions)
        turn.fresh = fresh
        outcomes = store.untold_outcomes(db, lesson["id"])
        turn.notes = [plans.outcome_note(row) for row in outcomes]
        store.mark_told(db, [row["id"] for row in outcomes])
        turn.message_id = store.add_message(db, lesson["id"], run["id"], "user", text, context=self.context(turn, lesson))
        store.mark_started(db, lesson["id"])
        store.touch_lesson(db, lesson["id"])
        self.by_lesson[lesson["id"]] = self.by_run[run["id"]] = turn
        self.tokens[mcp.token_key(token)] = turn
        self.tasks.spawn(f"run-{run['id']}", self._turn(turn, text))
        return store.run_out(run), store.message(db, turn.message_id)

    def context(self, turn, lesson):
        """What goes before the learner's text: the opening context on a new session (naming the
        files and the turns it did not see, when it replaces one), the outcomes of proposals."""
        parts = []
        if not lesson["session_started"]:
            earlier = self.db.one("SELECT count(*) FROM messages WHERE lesson_id = ? AND role = 'user' AND id < ?",
                                  (lesson["id"], turn.message_id or 1 << 62))[0]
            parts.append(plans.opening_context(self.db, lesson, turn.fresh, earlier) if turn.fresh or not earlier
                         else plans.opening_context(self.db, lesson, "your earlier session of it did not start", earlier))
        return "\n\n".join(parts + turn.notes)

    async def write_transcript(self, lesson_id):
        """The lesson's conversation into its transcript.md in the workspace, rewritten whole:
        the sandbox can change the file, the database is the truth."""
        row = store.lesson(self.db, lesson_id)
        if row is not None:
            await write_file_as_learner(row["project"], store.folder_of(row) + "/transcript.md", plans.transcript(self.db, row))

    def on_proposal(self, turn, proposal):
        """A proposal a tool of this turn stored: shown now, and part of the message it comes with."""
        turn.stretch["proposals"].append(proposal["id"])
        self.emit(turn, "proposal", {"proposal": proposal})

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
        if text or stretch["tools"] or stretch["proposals"]:
            message_id = store.add_message(self.db, turn.lesson_id, turn.run_id, "assistant", text, stretch["tools"])
            store.link_proposals(self.db, stretch["proposals"], message_id)
            message = store.message(self.db, message_id)
            for tool in stretch["tools"]:
                turn.tools[tool["id"]] = (tool, message["id"])
            self.emit(turn, "message", {"message": message})
        turn.stretch = {"text": [], "tools": [], "proposals": []}

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
            context = store.message(self.db, turn.message_id)["context"]
            if turn.fresh:
                await self.write_transcript(turn.lesson_id)
            for _attempt in range(3):
                sent = f"{context}\n\n{text}" if context else text
                outcome, error, usage, seen = await self._attempt(turn, sent, session_id, resume)
                if seen:
                    store.set_session(self.db, turn.lesson_id, session_id, True, store.text_hash(turn.text))
                if outcome == "session gone":      # its session file is gone: a fresh session, taken up from the files
                    session_id, resume = str(uuid.uuid4()), False
                    store.set_session(self.db, turn.lesson_id, session_id, False)
                    self.on_event(turn, "text_delta", {"text": SESSION_GONE_NOTE})
                    turn.fresh = SESSION_GONE
                    context = self.context(turn, store.lesson(self.db, turn.lesson_id))
                    store.set_context(self.db, turn.message_id, context)
                    await self.write_transcript(turn.lesson_id)
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
                self.tokens.pop(mcp.token_key(turn.token), None)
                turn.done.set()
                self.tasks.spawn(f"transcript-{turn.run_id}", self.write_transcript(turn.lesson_id))

    async def _attempt(self, turn, text, session_id, resume):
        """One launch of Claude Code. Returns (outcome, error, usage, whether its session
        started): outcome is "ok", "cancelled", "failed", or "session gone" / "session exists"
        for the caller to try again."""
        if turn.cancel_requested:
            return "cancelled", "", None, False
        if not os.path.isfile(f"{KEYS}/{turn.project}/id_ed25519"):
            return "failed", START_PROJECT, None, False
        token = read_token()
        env = dict(os.environ, CL_RUN_ID=str(turn.run_id), CL_RUN_TOKEN=turn.token)
        send = ["CL_RUN_ID", "CL_RUN_TOKEN"]
        if token:
            env["CL_CLAUDE_TOKEN"] = token
            send.append("CL_CLAUDE_TOKEN")
        reader = StreamReader(lambda kind, data: self.on_event(turn, kind, data))
        proc = await asyncio.create_subprocess_exec(*ssh_argv(turn.project, send), claude_command(session_id, resume, turn.text), env=env,
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
