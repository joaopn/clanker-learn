"""A turn: the learner's message to the project's agent, Claude Code or pi, in its sandbox, over SSH.

As step 0's spike drove both (its drive.py: the launch over SSH, the session created then
resumed, the interrupt, the run's processes found by a tag in their environment and ended as
root), with gaggiclanker's reader of Claude Code's stream (claude_stream.py) and a reader of
pi's (pi_stream.py). What differs between the two agents is in agents.py; the rest is here.

The service starts `ssh` as a child, with the project's key and known_hosts, to
learner@cl-sb-<project>, which runs the lesson's agent in /workspace with the tutor's text
appended to its system prompt. One session per lesson and agent: made at that agent's first
turn of the lesson, resumed afterwards. No model is named: the project's own settings of that
agent decide, and its own login is used (for Claude Code, unless a token is set in Settings).
The token reaches the turn only through the environment (sent over SSH as CL_CLAUDE_TOKEN,
exported as CLAUDE_CODE_OAUTH_TOKEN by the remote command before it becomes Claude Code):
never on a command line, in a log or in an event. CL_RUN_ID, the run's id, is in the
environment of the agent and of every command it runs: Stop and the clean-up at start end
exactly the processes that carry it, and never those of another lesson's turn in the same
sandbox.

The tutor's text is the one in force in the database (an editable copy of tutor.md), the same
for both agents. Each gets one tool server, "learn" (mcp.py); the run's token for it goes over
SSH as CL_RUN_TOKEN and dies with the run. The first message of a new session carries the
lesson's opening context before the learner's text (plans.opening_context), stored apart from
it; a session that is gone, was made with other instructions, or is the first of an agent
taking up a lesson the other one taught, is a fresh one told to take the lesson up from its
files, transcript.md (written here after every answer) and notes.md (kept by the tutor). An
agent whose session is current but missed turns the other agent answered resumes it, told
which turns it missed and where they are (plans.missed_note).

An exercise's check (run_check) is run by the service over its own SSH channel, not through
the agent: as the sandbox user, in /workspace, with no credential sent, for at most CHECK_S,
tagged CL_CHECK_ID (its time limit and Stop end it by that tag) and, during a turn, CL_RUN_ID.
"""
import asyncio
import contextlib
import json
import logging
import os
import secrets
import shlex
import uuid

import agents
import cards
import knowledge
import mcp
import plans
import store
from agents import START_PROJECT, UNREACHABLE

log = logging.getLogger("tutor")

DATA = os.environ.get("CL_TUTOR_DATA", "/data/tutor")
KEYS = f"{DATA}/keys"
TOKEN_FILE = f"{DATA}/claude_token"
TUTOR_TEXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tutor.md")
USER = "learner"
TRANSCRIPT_S = 30
IDLE_S = 30 * 60           # no overall limit (installing software takes long), but a line every half hour
STOP_GRACE_S = 5           # after the interrupt, before the run's processes are ended as root
END_S = 15                 # after the end of the answer, for the agent to end once its input is closed
CLEANUP_S = 30
SHUTDOWN_KILL_S = 3        # at a stop of the service, for the running answers' processes to be ended
SHUTDOWN_END_S = 2         # then for the turns to be stored as interrupted
CHECK_S = 30               # an exercise's check command, before it is ended
CHECK_KEEP = 4000          # of a check's output, the end of it, what is stored
LINE_LIMIT = 16 * 1024 * 1024   # a whole file read by a tool comes back on one line
STDERR_KEEP = 4096

SESSION_GONE_NOTE = ("*The tutor's earlier turns of this lesson are gone from the project, so it starts this one afresh "
                     "and takes the lesson up from its files.*\n\n")
SESSION_GONE = "your earlier session of it is gone from the project"
TEXT_CHANGED = "the tutor's instructions changed since your earlier session of it"
TAKEN_OVER = "until now another agent taught this lesson"

# The run's processes, found as the sandbox user and ended as root. Root in a sandbox has no
# CAP_SYS_PTRACE, so it cannot read the environment of the learner's processes, while the
# learner can; and a plain user cannot signal what runs under `sudo` (which also drops the
# tag), so those are found as descendants of a tagged process and ended by root. Printed: the
# pids of every process whose environment holds exactly the tag (CL_RUN_ID=<the id>, or a
# check's CL_CHECK_ID=<its id>), with all of their descendants. This script's own process does
# not carry the tag: the clean-up's SSH call does not send it.
FIND_TAGGED = """
import os, sys
tag = sys.argv[1].encode()
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


async def kill_tagged(project, run_id, tag=None):
    """End every process in the sandbox that carries this run's CL_RUN_ID (or `tag`, a whole
    NAME=value), and its descendants. Best effort: a sandbox that is gone has nothing left to end."""
    if not os.path.isfile(f"{KEYS}/{project}/id_ed25519"):
        return True
    tag = tag or f"CL_RUN_ID={run_id}"
    remote = "sh -c " + " ".join(shlex.quote(part) for part in (KILL_TAGGED, "kill-tagged", FIND_TAGGED, tag))
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(*ssh_argv(project), remote, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), CLEANUP_S)
        log.info("%s: ended %s process(es) in %s%s", tag, out.decode(errors="replace").strip() or "no",
                 project, f" ({err.decode(errors='replace').strip()[-200:]})" if proc.returncode else "")
        return proc.returncode == 0
    except (OSError, TimeoutError, asyncio.CancelledError) as exc:
        log.info("%s: the clean-up in %s did not run (%s)", tag, project, type(exc).__name__)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return False


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


async def run_check(project, command, run_id=None, check_id=None):
    """An exercise's check command, run by the service itself over its own SSH channel (not
    through the agent): as the sandbox user, in /workspace, with no credential in its
    environment (nothing is sent but the tags), for at most CHECK_S. It carries CL_CHECK_ID, by
    which it is ended at its time limit, and during a turn the turn's CL_RUN_ID, so that Stop
    ends it with the rest of the turn. Returns {exit_code (None when it was ended), output (the
    end of stdout and stderr together), cut (whether more came before), timed_out, seconds}."""
    if not os.path.isfile(f"{KEYS}/{project}/id_ed25519"):
        raise cards.CheckError(START_PROJECT)
    check_id = check_id or secrets.token_hex(8)
    env, send = dict(os.environ, CL_CHECK_ID=check_id), ["CL_CHECK_ID"]
    if run_id is not None:
        env["CL_RUN_ID"] = str(run_id)
        send.append("CL_RUN_ID")
    remote = ("unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY CL_CLAUDE_TOKEN CL_RUN_TOKEN; cd /workspace && exec sh -c "
              + shlex.quote(command) + " 2>&1")
    started = asyncio.get_running_loop().time()
    proc = await asyncio.create_subprocess_exec(*ssh_argv(project, send), remote, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, total, timed_out = bytearray(), 0, False

    async def read():
        nonlocal total
        while chunk := await proc.stdout.read(65536):
            total += len(chunk)
            out.extend(chunk)
            del out[:-CHECK_KEEP * 2]
        return await proc.wait()
    try:
        await asyncio.wait_for(read(), CHECK_S)
    except TimeoutError:
        timed_out = True
        await kill_tagged(project, None, f"CL_CHECK_ID={check_id}")
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
    err = (await proc.stderr.read()).decode(errors="replace").strip()
    if not timed_out and proc.returncode == 255 and not out and err:
        raise cards.CheckError(START_PROJECT if any(sign in err for sign in UNREACHABLE) else
                               f"The check could not reach the sandbox ({err.splitlines()[-1][:300]}): stop and start the project from its "
                               "gear (⚙) in the project list, then press Check again.")
    text = out.decode(errors="replace")
    return {"exit_code": None if timed_out else proc.returncode, "output": text[-CHECK_KEEP:], "cut": total > CHECK_KEEP or len(text) > CHECK_KEEP,
            "timed_out": timed_out, "seconds": round(asyncio.get_running_loop().time() - started, 1)}


def new_stretch():
    return {"text": [], "tools": [], "proposals": [], "cards": [], "stored": None}


class Turn:
    """What the service holds of a running turn."""

    def __init__(self, lesson, run_id, token, text, agent):
        self.lesson_id, self.project, self.created = lesson["id"], lesson["project"], lesson["created"]
        self.kind = lesson["kind"]
        self.agent = agent          # "claude" or "pi": the one answering
        self.adapter = agents.ADAPTERS[agent]
        self.run_id = run_id
        self.token = token          # the "learn" tools' bearer token of this run, alive while it runs
        self.text = text            # the instructions in force when it started
        self.tool_calls = 0
        self.message_id = None      # the learner's message
        self.notes = []             # the outcomes of proposals it is told, before the learner's text
        self.fresh = None           # why its session is a fresh one taken up from the files, if it is
        self.missed = None          # the learner's messages its resumed session did not see: (first, last, who answered)
        self.seq = 0
        self.proc = None
        self.reader = None          # the reader of the agent's output, while it runs
        self.cancel_requested = False
        self.shutdown = False       # the service is stopping: the turn ends as interrupted
        self.cleaned = False        # its processes in the sandbox were ended by the stop
        self.usage = None           # its usage, when the stop ended it
        self.done = asyncio.Event()
        # The assistant's message being written; `stored` is its row once a tool line made it
        # stored (so that a restart keeps the line), updated until the stretch ends.
        self.stretch = new_stretch()
        self.tools = {}                            # call id: (its dict, the stored message's id or None)


class Runner:
    """The running turns, one per lesson at most, and their events."""

    def __init__(self, db, bus, tasks):
        self.db, self.bus, self.tasks = db, bus, tasks
        self.by_lesson = {}
        self.by_run = {}
        self.tokens = {}   # the hash of a running turn's token: the turn
        self.checks = {}   # a run's id: {the CL_CHECK_ID of a check its tutor is running: the project}

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
        return [{"project": t.project, "created": t.created, "lesson_id": t.lesson_id, "run_id": t.run_id, "agent": t.agent}
                for t in self.by_lesson.values()]

    def start(self, lesson, text, composed=False):
        """The learner's message stored and its turn started, or None when a turn of this
        lesson is running. Nothing awaits between the check and the claim. A `composed` message
        is one the system wrote for the learner (a card's answer sent as a message)."""
        if lesson["id"] in self.by_lesson:
            return None
        db = self.db
        instructions = store.instructions(db)["text"]
        agent = store.agent_of(db, lesson)
        session = store.session(db, lesson["id"], agent)
        fresh = missed = None
        if session["started"] and session["text_hash"] and session["text_hash"] != store.text_hash(instructions):
            # The session was made with other instructions, which the agent keeps as it first
            # had them: a new one, taken up from the lesson's files.
            fresh = TEXT_CHANGED
            store.set_session(db, lesson["id"], agent, str(uuid.uuid4()), False)
        elif session["started"]:
            missed = self.missed(lesson["id"], agent, session["seen_message"])
        run = store.start_run(db, lesson["id"], agent)
        token = secrets.token_urlsafe(32)
        turn = Turn(lesson, run["id"], token, instructions, agent)
        turn.fresh, turn.missed = fresh, missed
        # What the learner decided or did since the last message, outside the conversation: the
        # proposals, the global insights proposed here, the cards they answered or checked.
        outcomes = store.untold_outcomes(db, lesson["id"])
        globals_ = knowledge.untold_global(db, lesson["id"])
        acted = store.untold_cards(db, lesson["id"])
        # What changed in the plan since this conversation's last turn (in another one, or by
        # hand); its own proposal's change is its outcome, taken as seen when it was approved.
        now = plans.plan_snapshot(db, lesson["project"], lesson["created"])
        seen = store.plan_seen(db, lesson["id"])
        plan_note = plans.plan_note(seen, now, lesson["id"]) if seen is not None else None
        store.set_plan_seen(db, lesson["id"], now)
        turn.notes = ([plans.outcome_note(row) for row in outcomes] + ([plan_note] if plan_note else [])
                      + [knowledge.global_outcome_note(row) for row in globals_] + [cards.note(row) for row in acted])
        store.mark_told(db, [row["id"] for row in outcomes])
        knowledge.mark_global_told(db, [row["id"] for row in globals_])
        store.mark_cards_told(db, [row["id"] for row in acted])
        turn.message_id = store.add_message(db, lesson["id"], run["id"], "user", text, context=self.context(turn, lesson), composed=composed)
        store.mark_started(db, lesson["id"])
        store.touch_lesson(db, lesson["id"])
        self.by_lesson[lesson["id"]] = self.by_run[run["id"]] = turn
        self.tokens[mcp.token_key(token)] = turn
        self.tasks.spawn(f"run-{run['id']}", self._turn(turn, text))
        return store.run_out(run), store.message(db, turn.message_id)

    def missed(self, lesson_id, agent, seen_message):
        """The learner's messages after `seen_message` (the last one this agent's session saw),
        as (first, last, the agents that answered them), numbered as the conversation counts
        them; None when it missed none."""
        rows = self.db.all("SELECT messages.id, runs.agent FROM messages LEFT JOIN runs ON runs.id = messages.run_id "
                           "WHERE messages.lesson_id = ? AND messages.role = 'user' ORDER BY messages.id", (lesson_id,))
        after = [(number, row["agent"]) for number, row in enumerate(rows, 1) if row["id"] > seen_message]
        if not after:
            return None
        who = sorted({a for _n, a in after if a and a != agent})
        return after[0][0], after[-1][0], " and ".join(store.AGENT_NAMES.get(a, a) for a in who) or None

    def context(self, turn, lesson):
        """What goes before the learner's text: the opening context on a new session (naming the
        files and the turns it did not see, when it replaces one or takes the lesson up from
        the other agent), a note of the turns a resumed session missed, the outcomes of proposals."""
        parts = []
        session = store.session(self.db, lesson["id"], turn.agent)
        if not session["started"]:
            earlier = self.db.all("SELECT runs.agent FROM messages LEFT JOIN runs ON runs.id = messages.run_id "
                                  "WHERE messages.lesson_id = ? AND messages.role = 'user' AND messages.id < ?",
                                  (lesson["id"], turn.message_id or 1 << 62))
            others = sorted({row["agent"] for row in earlier if row["agent"] and row["agent"] != turn.agent})
            taught_by = " and ".join(store.AGENT_NAMES.get(a, a) for a in others) or None
            if turn.fresh or not earlier:
                parts.append(plans.opening_context(self.db, lesson, turn.fresh, len(earlier), taught_by))
            elif taught_by:
                parts.append(plans.opening_context(self.db, lesson, TAKEN_OVER, len(earlier), taught_by))
            else:
                parts.append(plans.opening_context(self.db, lesson, "your earlier session of it did not start", len(earlier)))
        elif turn.missed:
            parts.append(plans.missed_note(lesson, *turn.missed))
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

    def on_card(self, turn, card):
        """A card a tool of this turn made, or checked: shown now, and part of the message it comes with."""
        if card["id"] not in turn.stretch["cards"] and card["message_id"] is None:
            turn.stretch["cards"].append(card["id"])
        self.emit(turn, card["kind"], {"card": card})

    def card_changed(self, card):
        """A card the learner acted on: when its lesson has an answer being written, its stream
        carries the card's new state too (another tab of the same conversation sees it)."""
        turn = self.by_lesson.get(card["lesson_id"])
        if turn is not None:
            self.emit(turn, card["kind"], {"card": card})

    def busy_in(self, project, created):
        return any((t.project, t.created) == (project, created) for t in self.by_lesson.values())

    async def run_check(self, project, command, run_id=None):
        """A check, kept by its run while it runs, so that Stop ends it too (the interrupt ends the
        turn, not a check the tool server is running for it)."""
        check_id = secrets.token_hex(8)
        if run_id is not None:
            self.checks.setdefault(run_id, {})[check_id] = project
        try:
            return await run_check(project, command, run_id, check_id)
        finally:
            self.checks.get(run_id, {}).pop(check_id, None)
            if not self.checks.get(run_id):
                self.checks.pop(run_id, None)

    async def cancel(self, run_id):
        turn = self.by_run.get(run_id)
        if turn is not None and not turn.cancel_requested:
            turn.cancel_requested = True
            if turn.reader is not None:
                turn.reader.cancel()
            # The agent's own interrupt first (Claude Code's control message, pi's abort); it ends
            # the turn with the agent's last word on it.
            await self._write(turn, turn.adapter.interrupt(run_id))
            self.tasks.spawn(f"stop-{run_id}", self._force_stop(turn))
            for check_id, project in list(self.checks.get(run_id, {}).items()):
                self.tasks.spawn(f"stop-check-{check_id}", kill_tagged(project, run_id, f"CL_CHECK_ID={check_id}"))
        row = store.run(self.db, run_id)
        return store.run_out(row) if row else None

    def refresh_plan_seen(self, turn):
        """What the conversation saw of its own lesson, brought up to date at the end of its
        turn: its tutor closed it, or knows of it."""
        seen = store.plan_seen(self.db, turn.lesson_id)
        if seen is None:
            return
        now = plans.plan_snapshot(self.db, turn.project, turn.created)
        key = str(turn.lesson_id)
        if key in now["lessons"]:
            seen["lessons"][key] = now["lessons"][key]
            store.set_plan_seen(self.db, turn.lesson_id, seen)

    async def shutdown(self):
        """The service is stopping (listening has stopped): every running turn ended, its
        processes in the sandbox (the agent, its commands, a check) ended as root by their tag,
        within SHUTDOWN_KILL_S, its SSH child ended, and the turn marked interrupted. A turn
        whose processes could not be ended in time stays `running`, for the clean-up at the
        next start."""
        turns = list(self.by_run.values())
        if not turns:
            return
        log.info("stopping: ending %d running answer(s)", len(turns))
        for t in turns:
            t.shutdown = True
            if t.reader is not None:
                t.reader.cancel()

        async def end(t):
            kills = [kill_tagged(t.project, t.run_id)]
            kills += [kill_tagged(project, t.run_id, f"CL_CHECK_ID={check_id}") for check_id, project in self.checks.get(t.run_id, {}).items()]
            t.cleaned = all(await asyncio.gather(*kills))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*(end(t) for t in turns)), SHUTDOWN_KILL_S)
        for t in turns:
            if t.proc is not None and t.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    t.proc.kill()
        await asyncio.wait([asyncio.ensure_future(t.done.wait()) for t in turns], timeout=SHUTDOWN_END_S)
        for t in turns:
            if t.cleaned and t.done.is_set():
                store.finish_run(self.db, t.run_id, "interrupted", store.STOPPED, t.usage)
        log.info("stopping: %s", ", ".join(f"run {t.run_id} {'ended' if t.done.is_set() else 'still ending'}"
                                           f"{'' if t.cleaned else ' (its processes are left to the clean-up at the next start)'}" for t in turns))

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
            # Stored now, not at the end of the stretch: a tool line running when the service
            # stops or restarts stays in the conversation. The page hears of the message at the
            # end of the stretch, as before.
            text = "".join(stretch["text"])
            if stretch["stored"] is None:
                stretch["stored"] = store.add_message(self.db, turn.lesson_id, turn.run_id, "assistant", text, stretch["tools"])
            else:
                store.set_message(self.db, stretch["stored"], text, stretch["tools"])
            turn.tools[data["id"]] = (tool, stretch["stored"])
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
        if stretch["stored"] is not None:
            message_id = stretch["stored"]
            store.set_message(self.db, message_id, text, stretch["tools"])
        elif text or stretch["tools"] or stretch["proposals"] or stretch["cards"]:
            message_id = store.add_message(self.db, turn.lesson_id, turn.run_id, "assistant", text, stretch["tools"])
        else:
            message_id = None
        if message_id is not None:
            store.link_proposals(self.db, stretch["proposals"], message_id)
            store.link_cards(self.db, stretch["cards"], message_id)
            message = store.message(self.db, message_id)
            for tool in stretch["tools"]:
                turn.tools[tool["id"]] = (tool, message["id"])
            self.emit(turn, "message", {"message": message})
        turn.stretch = new_stretch()

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

    async def _send(self, turn, reader):
        """What the reader has for the agent's input, written; the input closed when the reader
        says so."""
        for obj in reader.take():
            await self._write(turn, obj)
        proc = turn.proc
        if reader.close_input and proc is not None and proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()

    async def _turn(self, turn, text):
        status, error, usage = "failed", "", None
        try:
            session = store.session(self.db, turn.lesson_id, turn.agent)
            session_id, resume = session["session_id"], bool(session["started"])
            context = store.message(self.db, turn.message_id)["context"]
            if turn.fresh or turn.missed:
                await self.write_transcript(turn.lesson_id)
            for _attempt in range(3):
                sent = f"{context}\n\n{text}" if context else text
                outcome, error, usage, seen = await self._attempt(turn, sent, session_id, resume)
                if seen:
                    store.set_session(self.db, turn.lesson_id, turn.agent, session_id, True, store.text_hash(turn.text), turn.message_id)
                if turn.shutdown:
                    break
                if outcome == "session gone":      # its session is gone: a fresh session, taken up from the files
                    session_id, resume = str(uuid.uuid4()), False
                    store.set_session(self.db, turn.lesson_id, turn.agent, session_id, False)
                    self.on_event(turn, "text_delta", {"text": SESSION_GONE_NOTE})
                    turn.fresh, turn.missed = SESSION_GONE, None
                    context = self.context(turn, store.lesson(self.db, turn.lesson_id))
                    store.set_context(self.db, turn.message_id, context)
                    await self.write_transcript(turn.lesson_id)
                    continue
                if outcome == "session exists":    # made by a turn that failed after creating it
                    resume = True
                    store.set_session(self.db, turn.lesson_id, turn.agent, session_id, True)
                    continue
                status = outcome
                break
            else:
                status, error = "failed", "The tutor's session for this lesson could not be started."
        except asyncio.CancelledError:
            turn.shutdown = True
            raise
        except Exception as exc:
            log.exception("run %s failed", turn.run_id)
            status, error = "failed", (f"The tutor service failed during this answer ({type(exc).__name__}; its log says more): "
                                       "send your message again.")
        finally:
            if turn.shutdown:
                status, error = "interrupted", store.STOPPED
            try:
                # A tool line still without a result when the answer ended never gets one.
                for tool, _stored in turn.tools.values():
                    if tool["ok"] is None:
                        tool.update(unfinished=True, output=tool["output"] or store.UNFINISHED)
                        self.emit(turn, "tool_result", {"id": tool["id"], "ok": None, "output": tool["output"], "unfinished": True})
                self.flush(turn)
                store.mark_unfinished(self.db, turn.run_id)
                self.refresh_plan_seen(turn)
                if status == "ok":
                    self.emit(turn, "completed", {"usage": usage, "agent": turn.agent})
                elif status == "cancelled":
                    self.emit(turn, "cancelled", {"agent": turn.agent})
                else:
                    self.emit(turn, "error", {"message": error, "agent": turn.agent})
                # At a stop of the service the run is marked interrupted by `shutdown`, once its
                # processes in the sandbox are known to be ended; else it stays `running`, and the
                # next start marks it and ends them (store.reconcile_running).
                if status == "interrupted":
                    turn.usage = usage
                else:
                    store.finish_run(self.db, turn.run_id, status, error if status == "failed" else "", usage)
                store.touch_lesson(self.db, turn.lesson_id)
            finally:
                self.by_lesson.pop(turn.lesson_id, None)
                self.by_run.pop(turn.run_id, None)
                self.tokens.pop(mcp.token_key(turn.token), None)
                turn.done.set()
                if not turn.shutdown:
                    self.tasks.spawn(f"transcript-{turn.run_id}", self.write_transcript(turn.lesson_id))

    async def _attempt(self, turn, text, session_id, resume):
        """One launch of the agent. Returns (outcome, error, usage, whether its session
        started): outcome is "ok", "cancelled", "failed", or "session gone" / "session exists"
        for the caller to try again."""
        if turn.cancel_requested or turn.shutdown:
            return "cancelled", "", None, False
        if not os.path.isfile(f"{KEYS}/{turn.project}/id_ed25519"):
            return "failed", START_PROJECT, None, False
        adapter = turn.adapter
        env = dict(os.environ, CL_RUN_ID=str(turn.run_id), CL_RUN_TOKEN=turn.token)
        send = ["CL_RUN_ID", "CL_RUN_TOKEN"]
        token = read_token() if adapter.uses_token() else ""
        if token:
            env["CL_CLAUDE_TOKEN"] = token
            send.append("CL_CLAUDE_TOKEN")
        reader = turn.reader = adapter.reader(lambda kind, data: self.on_event(turn, kind, data), text, resume, turn.run_id)
        proc = await asyncio.create_subprocess_exec(*ssh_argv(turn.project, send), adapter.command(session_id, resume, turn.text), env=env,
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
            await self._send(turn, reader)
            if turn.cancel_requested:   # Stop came while it was starting: the interrupt goes now
                await self._write(turn, adapter.interrupt(turn.run_id))
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), END_S if reader.finished else IDLE_S)
                except ValueError:      # a line longer than LINE_LIMIT: skipped
                    continue
                except TimeoutError:
                    idle = not reader.finished
                    break
                if not line:
                    break
                reader.feed(line)
                # The answer is over when the agent says so (Claude Code's result line, pi's
                # agent_settled), never because its input was closed: pi ends when it is.
                await self._send(turn, reader)
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
            turn.proc = turn.reader = None
        said = stderr.decode(errors="replace").strip()
        return (*adapter.judge(reader, proc.returncode, said, turn.cancel_requested, idle), reader.usage(), reader.session_seen)
