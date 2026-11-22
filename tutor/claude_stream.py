"""Claude Code's stream-json output, read into the tutor's events.

Lifted from gaggiclanker (the maintainer's project, AGPL like this one): `_StreamReader` of
gaggiclanker/llm/providers/claude_code.py, with `_read_usage`, `_model_window`, `_flatten` and
`_as_int` beside it, and the per-request usage of gaggiclanker/llm/types.py (`request_usage`,
`Usage.context_tokens`). Adapted to the tutor's event kinds (text_delta, tool_call with a
display kind, tool_result, usage) and to plain dictionaries; the reader no longer raises on
an error result, it keeps the result line for the turn to judge.

Four shapes of line matter: `stream_event` (the raw content-block events, where the text
deltas are), `assistant` (one whole content block of an assistant message: the tool calls),
`user` (the tool results Claude Code produced), `result` (the end of the turn, with the cost,
the model's window and `is_error`). Anything else (`system`, a line that is not JSON) is
ignored but for the session's start: a Claude Code that prints something new must not break a
conversation.
"""
import json

PREVIEW = 2000      # of a tool's output, what the stream and the message keep
LABEL = 200         # of a tool's label: one line
DETAIL = 4000


def as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def read_usage(raw):
    """One request's usage. Its size is fresh plus cache-write plus cache-read input: reporting
    only `input_tokens` on a cached request understates it by an order of magnitude."""
    if not isinstance(raw, dict):
        return None
    fresh, write, read = (as_int(raw.get(k)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    parts = [v for v in (fresh, write, read) if v is not None]
    return {"context": sum(parts) if parts else None, "fresh": fresh, "cache_write": write, "cache_read": read, "out": as_int(raw.get("output_tokens"))}


def model_window(model_usage):
    """`contextWindow` of the model that answered (the first entry, normally the only one)."""
    if not isinstance(model_usage, dict):
        return None
    for entry in model_usage.values():
        if isinstance(entry, dict) and as_int(entry.get("contextWindow")) is not None:
            return as_int(entry.get("contextWindow"))
    return None


def flatten(content):
    """A tool_result's content, which is a string or a list of blocks (text kept, the rest named)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if b.get("type") == "text" else f"[{b.get('type')}]" for b in content if isinstance(b, dict))
    return json.dumps(content, default=str) if content is not None else ""


def one_line(text, limit=LABEL):
    first = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return first if len(first) <= limit else first[: limit - 1] + "…"


def tool_view(name, arguments):
    """(kind, label, detail) of a tool call, as the conversation shows it: a command, the path
    of a file read, written or edited, a search, or another tool by its name."""
    args = arguments if isinstance(arguments, dict) else {}
    path = args.get("file_path") or args.get("notebook_path") or args.get("path") or ""
    if name == "Bash":
        command = str(args.get("command", ""))
        return "shell", one_line(command), command[:DETAIL]
    if name == "Read":
        return "read", one_line(path), str(path)
    if name == "Write":
        return "write", one_line(path), str(path)
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "edit", one_line(path), str(path)
    if name in ("Grep", "Glob"):
        pattern = str(args.get("pattern", ""))
        return "search", one_line(f"{pattern} in {path}" if path else pattern), json.dumps(args, default=str)[:DETAIL]
    return "other", one_line(name or "a tool"), json.dumps(args, default=str)[:DETAIL]


class StreamReader:
    """Translates Claude Code's stream-json lines into events, through `emit(kind, data)`."""

    def __init__(self, emit):
        self.emit = emit
        self.streamed = False
        # One entry per API request, keyed by `message.id`, in arrival order. Claude Code emits
        # an `assistant` line per content block, several sharing one id and one copy of the
        # usage: counting per line multiplied the input.
        self.requests = {}
        self.order = []
        self.current = ""
        self.tool_names = {}
        self.session_seen = False
        self.result = None
        self.window = None
        self.cost = None

    def feed(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "stream_event":
            self._stream_event(event.get("event"))
        elif kind == "assistant":
            self._assistant(event.get("message"))
        elif kind == "user":
            self._user(event.get("message"))
        elif kind == "result":
            self._result(event)
        elif kind == "system" and event.get("subtype") == "init":
            self.session_seen = True

    def _request(self, message_id):
        if message_id not in self.requests:
            self.requests[message_id] = {"start": None, "delta": None, "copy": None}
            self.order.append(message_id)
        self.current = message_id
        return self.requests[message_id]

    def _stream_event(self, event):
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start":
            # The input side is final here; the output is a stub.
            message = event.get("message")
            if isinstance(message, dict):
                self._request(str(message.get("id") or "")).update(start=message.get("usage"))
            return
        if kind == "message_delta":
            # No id on this one: it closes the request that started last, with its final output.
            if self.current:
                self.requests[self.current]["delta"] = event.get("usage")
                self.emit("usage", {"usage": self.usage()})
            return
        if kind != "content_block_delta":
            return
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str) and delta["text"]:
            self.streamed = True
            self.emit("text_delta", {"text": delta["text"]})

    def _assistant(self, message):
        if not isinstance(message, dict):
            return
        # Which request this block belongs to, and a fallback for a stream with no
        # `message_start`. Its usage is a copy of the request's, repeated per block: never summed.
        request = self._request(str(message.get("id") or f"anon-{len(self.order)}"))
        if isinstance(message.get("usage"), dict):
            request["copy"] = message["usage"]
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and not self.streamed and isinstance(block.get("text"), str) and block["text"]:
                self.emit("text_delta", {"text": block["text"]})
            elif block.get("type") == "tool_use":
                call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
                self.tool_names[call_id] = name
                kind, label, detail = tool_view(name, block.get("input"))
                self.emit("tool_call", {"id": call_id, "kind": kind, "label": label, "detail": detail})

    def _user(self, message):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            return
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self.emit("tool_result", {"id": str(block.get("tool_use_id") or ""), "ok": not bool(block.get("is_error")),
                                          "output": flatten(block.get("content"))[:PREVIEW]})

    def _result(self, event):
        self.result = event
        self.window = model_window(event.get("modelUsage"))
        cost = event.get("total_cost_usd")
        self.cost = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None

    def per_request(self):
        """Input from `message_start` (or its copy), output from `message_delta`. A request cut
        before its `message_delta` reports the stub output, a lower bound."""
        out = []
        for key in self.order:
            request = self.requests[key]
            base = read_usage(request["start"] or request["copy"])
            final = read_usage(request["delta"])
            if base is None:
                continue
            if final is not None and final["out"] is not None:
                base["out"] = final["out"]
            out.append(base)
        return out

    def usage(self):
        """The turn's usage: the size of the conversation is the LAST request's input, never the
        result's totals (those sum every request of the turn). The cache figures and the fresh
        input are the last request's too, so that the share cached is theirs; the output is
        the turn's sum. The window and the cost come from the result line, when it is there."""
        requests = self.per_request()
        if not requests:
            return None
        last = requests[-1]
        return {"context_tokens": last["context"], "context_window": self.window, "cache_read": last["cache_read"],
                "cache_write": last["cache_write"], "fresh": last["fresh"],
                "output": sum(r["out"] or 0 for r in requests), "requests": len(requests), "cost_usd": self.cost}
