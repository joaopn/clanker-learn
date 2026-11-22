"""The two agents a lesson can be taught by, Claude Code and pi, as adapters of one turn.

A turn (turn.py) is the same for both: ssh to the project's sandbox as the sandbox user, the
run's tags and the tool server's token in the environment, the events stored and published,
Stop's clean-up by tag, the transcript. What differs is here, one class per agent: the
remote command line, the reader of its output (which also says what to write to its input:
the learner's message, and for pi the question of its state first), the interrupt, how a
session is started and resumed, and how an outcome is judged into a sentence.

Claude Code is as it was (lifted from step 0's spike into turn.py in step 2): stream-json in
and out, `--session-id` then `--resume`, the tool server given by `--mcp-config`. pi is as the
spike drove it (its drive.py): `pi --mode rpc` with `--session-id` (pi opens the session of
that id in /workspace, or makes it), the learner's message as pi's `prompt` command, `abort`
for Stop; the tool server is an entry `learn` in pi's own mcp.json, written by the launch
command when it is not there or not as wanted (PI_MCP_ENTRY).
"""
import json
import shlex

import mcp
from claude_stream import StreamReader
from pi_stream import PiReader

MCP_URL = f"http://cl-tutor:{mcp.PORT}/mcp"
# The one tool server a turn gets. Each agent expands ${CL_RUN_TOKEN} from its environment,
# where it came over SSH: the token is never on a command line nor in a file.
MCP_HEADERS = {"Authorization": "Bearer ${CL_RUN_TOKEN}"}
MCP_CONFIG = json.dumps({"mcpServers": {"learn": {"type": "http", "url": MCP_URL, "headers": MCP_HEADERS}}})
PI_MCP_ENTRY = {"url": MCP_URL, "headers": MCP_HEADERS, "exposure": "direct"}

START_PROJECT = "The project is not running: start it from its gear (⚙) beside its name in the project list."
UNREACHABLE = ("Could not resolve hostname", "Name or service not known", "Temporary failure in name resolution", "Connection refused",
               "No route to host", "timed out", "Connection closed by", "Connection reset")


def ssh_failure(code, stderr):
    """The sentence for an SSH that did not get in, or None."""
    if code == 255 and any(sign in stderr for sign in UNREACHABLE):
        return START_PROJECT
    if code == 255:
        return ("The tutor could not log in to the project's sandbox (" + (stderr.splitlines()[-1][:300] if stderr else "ssh failed")
                + "): stop and start the project from its gear (⚙) in the project list, then send your message again.")
    return None


class Claude:
    name = "claude"
    label = "Claude Code"
    NO_AGENT = "This project has no Claude Code: add it from the project's gear (⚙) in the project list, under Agents."
    NOT_LOGGED_IN = ("Claude Code is not logged in in this project: run claude in its Shell and log in there, or set a token under "
                     "Settings → Tutor.")
    LOGIN_SIGNS = ("/login", "not logged in", "invalid api key", "oauth token", "authentication_error", "invalid bearer token",
                   "credit balance", "please log in")

    def command(self, session_id, resume, text):
        flags = ["claude", "--print", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                 "--include-partial-messages", "--permission-mode", "bypassPermissions", "--append-system-prompt", text,
                 "--mcp-config", MCP_CONFIG, "--strict-mcp-config",
                 "--resume" if resume else "--session-id", session_id]
        # The token, when one came, becomes Claude Code's here, in the environment only. Without
        # ENABLE_TOOL_SEARCH=false Claude Code hides a server's tools behind its tool search, and
        # the model is never told they exist (step 0's spike).
        return ('cd /workspace && if [ -n "${CL_CLAUDE_TOKEN:-}" ]; then export CLAUDE_CODE_OAUTH_TOKEN="$CL_CLAUDE_TOKEN"; fi; '
                "unset CL_CLAUDE_TOKEN; export ENABLE_TOOL_SEARCH=false; exec " + " ".join(shlex.quote(part) for part in flags))

    def uses_token(self):
        return True    # the Claude token from Settings, when one is set

    def reader(self, emit, prompt, resume, request_id):
        return ClaudeReader(emit, prompt)

    def interrupt(self, run_id):
        return {"type": "control_request", "request_id": f"stop-{run_id}", "request": {"subtype": "interrupt"}}

    def judge(self, reader, code, stderr, cancelled, idle):
        """(outcome, error) of a launch, from its result line, its exit code and what it said on
        standard error: a sentence a person can act on where there is one."""
        if cancelled:
            return "cancelled", ""
        if idle:
            return "failed", "The tutor's Claude Code wrote nothing for 30 minutes, so the answer was ended: send your message again to go on."
        result = reader.result
        said = " ".join(str(part) for part in ((result or {}).get("result"), *((result or {}).get("errors") or []), stderr) if part)
        lowered = said.lower()
        if "no conversation found" in lowered:
            return "session gone", ""
        if "already in use" in lowered and "session" in lowered:
            return "session exists", ""
        if result is not None and not result.get("is_error"):
            return "ok", ""
        if any(sign in lowered for sign in self.LOGIN_SIGNS) or (result or {}).get("api_error_status") in (401, 403):
            return "failed", self.NOT_LOGGED_IN
        if result is not None:
            return "failed", ("Claude Code reported an error (" + (said[:300] or str(result.get("subtype") or "with no reason"))
                              + "): send your message again; if it comes back, run claude in the project's Shell to see why.")
        if code == 127 and "no claude code" in lowered:
            return "failed", self.NO_AGENT
        if ssh_failure(code, stderr):
            return "failed", ssh_failure(code, stderr)
        last = stderr.splitlines()[-1][:300] if stderr else "it said nothing"
        return "failed", (f"Claude Code ended without an answer (exit {code}: {last}): send your message again; if it comes back, "
                          "run claude in the project's Shell to see why.")


class ClaudeReader(StreamReader):
    """claude_stream's reader with what the turn needs of any reader: the learner's message to
    write first, whether the turn is over (the result line), when to close the input (then:
    Claude Code ends once its input is closed)."""

    def __init__(self, emit, prompt):
        super().__init__(emit)
        self.outbox = [{"type": "user", "message": {"role": "user", "content": prompt}}]

    @property
    def finished(self):
        return self.result is not None

    @property
    def close_input(self):
        return self.result is not None

    def take(self):
        out, self.outbox = self.outbox, []
        return out

    def cancel(self):
        pass    # the interrupt ends the turn with a result line


# Run in the sandbox before pi starts, as the sandbox user: the entry `learn` of pi's mcp.json
# made what the turn needs when it is not, every other entry left as it is. pi's agent dir is
# PI_CODING_AGENT_DIR, or where research-sandbox's launcher puts it (~/.local/share/pi-agent/
# agent, the launcher's default). A file that is not JSON is left alone (pi says so itself).
# Says on standard error only what it did: standard output is pi's protocol.
PI_MCP_SYNC = """
import json, os, sys
home = os.path.expanduser("~")
given = os.environ.get("PI_CODING_AGENT_DIR") or ""
agent = home if given == "~" else os.path.join(home, given[2:]) if given.startswith("~/") else given or os.path.join(home, ".local/share/pi-agent/agent")
path, wanted = os.path.join(agent, "mcp.json"), json.loads(sys.argv[1])
try:
    with open(path) as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
except (OSError, ValueError) as exc:
    sys.exit(f"clanker-learn: {path} is not read ({exc}); the tutor's tools are not wired")
if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
    sys.exit(f"clanker-learn: {path} has no mcpServers object; the tutor's tools are not wired")
servers = data.setdefault("mcpServers", {})
if servers.get("learn") != wanted:
    servers["learn"] = wanted
    os.makedirs(agent, exist_ok=True)
    part = path + ".clanker-learn.part"
    with open(part, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\\n")
    os.replace(part, path)
    print(f"clanker-learn: wrote the tutor's tools into {path}", file=sys.stderr)
"""
NO_PI_SAID = "clanker-learn: no pi in this project"


class Pi:
    name = "pi"
    label = "pi"
    NO_AGENT = "This project has no pi: add it from the project's gear (⚙) in the project list, under Agents."
    NOT_LOGGED_IN = "pi is not logged in in this project: run pi in its Shell and log in there."
    # What pi says when it has no credential for its model, or the provider refuses the one it has.
    LOGIN_SIGNS = ("no api key", "/login", "no model", "not logged in", "authentication_error", "invalid x-api-key", "invalid api key",
                   "unauthorized", "401", "403", "oauth")

    def command(self, session_id, resume, text):
        # No model is named: pi's own settings in the project decide, as Claude Code's do.
        flags = ["--mode", "rpc", "--session-id", session_id, "--append-system-prompt", text]
        sync = "python3 -c " + shlex.quote(PI_MCP_SYNC) + " " + shlex.quote(json.dumps(PI_MCP_ENTRY)) + " </dev/null >&2"
        return ('cd /workspace && p="$HOME/.local/bin/pi"; [ -x "$p" ] || p=$(command -v pi) || { echo ' + shlex.quote(NO_PI_SAID)
                + f" >&2; exit 127; }}; {{ {sync} || true; }}; " + 'exec "$p" ' + " ".join(shlex.quote(part) for part in flags))

    def uses_token(self):
        return False   # pi uses the project's own pi login, made in its Shell

    def reader(self, emit, prompt, resume, request_id):
        return PiReader(emit, prompt, resume, request_id)

    def interrupt(self, run_id):
        return {"id": f"stop-{run_id}", "type": "abort"}

    def judge(self, reader, code, stderr, cancelled, idle):
        if cancelled:
            return "cancelled", ""
        if idle:
            return "failed", "The tutor's pi wrote nothing for 30 minutes, so the answer was ended: send your message again to go on."
        if reader.gone:
            return "session gone", ""
        if code == 127 and (NO_PI_SAID in stderr or "has no pi" in stderr):   # the sandbox's stub `pi` says it too
            return "failed", self.NO_AGENT
        if ssh_failure(code, stderr):
            return "failed", ssh_failure(code, stderr)
        if reader.error is not None:
            if any(sign in reader.error.lower() for sign in self.LOGIN_SIGNS):
                return "failed", self.NOT_LOGGED_IN
            return "failed", ("pi reported an error (" + reader.error[:300] + "): send your message again; if it comes back, run pi in "
                              "the project's Shell to see why.")
        if reader.finished and reader.aborted:
            return "cancelled", ""
        if reader.finished:
            return "ok", ""
        last = stderr.splitlines()[-1][:300] if stderr else "it said nothing"
        return "failed", (f"pi ended without an answer (exit {code}: {last}): send your message again; if it comes back, run pi in "
                          "the project's Shell to see why.")


ADAPTERS = {"claude": Claude(), "pi": Pi()}
