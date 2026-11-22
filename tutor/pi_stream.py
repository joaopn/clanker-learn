"""pi's RPC output (`pi --mode rpc`), read into the tutor's events.

The event shapes are the ones step 0's spike recorded from pi and mapped in its normalise.py
(`message_update` with the assistant's text deltas, `message_end` with each finished message
and its usage, `tool_execution_start` and `_end`, `agent_settled` at the end of a prompt),
and pi's own documentation of its RPC mode (docs/rpc.md, rpc-commands.md, json.md in pi's
package). Adapted to the tutor's event kinds, as claude_stream.py does for Claude Code: the
same text deltas, the same tool lines (pi's own bash, read, write and edit shown as Claude
Code's are; the tutor's tools, `mcp__learn__<tool>` in pi too, as `learn`), the same usage.

A turn talks to pi through this reader: before the prompt it asks pi for its state
(`get_state`), which says whether the session it opened has messages and the model's window.
A session that should be resumed but has none is gone (pi made a new empty one with the same
id): the prompt is not sent, the input is closed, and the turn starts a fresh session. Lines
the turn must write go to `outbox`; `close_input` says the turn should close pi's input (after
`agent_settled`, or when the session is gone). pi exits when its input is closed, so the
input stays open until pi says it is done.
"""
import json

from claude_stream import DETAIL, PREVIEW, as_int, flatten, one_line, tool_view

# pi's built-in tools by Claude Code's names, so that tool_view shows them the same way.
AS_CLAUDE = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit", "grep": "Grep", "find": "Glob"}


def pi_tool_view(name, arguments):
    if name == "ls":
        args = arguments if isinstance(arguments, dict) else {}
        path = str(args.get("path") or ".")
        return "search", one_line(path), json.dumps(args, default=str)[:DETAIL]
    return tool_view(AS_CLAUDE.get(name, name), arguments)


def read_usage(raw):
    """One request's usage as pi reports it on the assistant's message: `input` (fresh),
    `cacheRead`, `cacheWrite`, `output`, `cost.total`. A part it does not give stays None."""
    if not isinstance(raw, dict):
        return None
    fresh, write, read = (as_int(raw.get(k)) for k in ("input", "cacheWrite", "cacheRead"))
    parts = [v for v in (fresh, write, read) if v is not None]
    cost = (raw.get("cost") or {}).get("total") if isinstance(raw.get("cost"), dict) else None
    return {"context": sum(parts) if parts else None, "fresh": fresh, "cache_write": write, "cache_read": read,
            "out": as_int(raw.get("output")), "cost": float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None}


class PiReader:
    """Translates pi's RPC lines into events, through `emit(kind, data)`."""

    def __init__(self, emit, prompt, resume, request_id):
        self.emit = emit
        self.prompt = prompt
        self.resume = resume
        self.request_id = request_id
        self.outbox = [{"id": f"state-{request_id}", "type": "get_state"}]
        self.close_input = False
        self.session_seen = False
        self.gone = False
        self.window = None
        self.requests = []          # the usage of each assistant message, in order
        self.streamed = False       # text deltas came for the message being written
        self.error = None           # the last error pi gave: a refused prompt, a request that failed
        self.finished = False       # agent_settled, or a prompt pi refused
        self.aborted = False
        self.prompt_sent = False
        self.cancelled = False      # Stop came: a prompt not sent yet is not sent

    def feed(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "response":
            self._response(event)
        elif kind == "message_update":
            self._update(event.get("assistantMessageEvent"))
        elif kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                self.streamed = False
        elif kind == "message_end":
            self._message_end(event.get("message"))
        elif kind == "tool_execution_start":
            call_id = str(event.get("toolCallId") or "")
            tool, label, detail = pi_tool_view(str(event.get("toolName") or ""), event.get("args"))
            self.emit("tool_call", {"id": call_id, "kind": tool, "label": label, "detail": detail})
        elif kind == "tool_execution_end":
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            self.emit("tool_result", {"id": str(event.get("toolCallId") or ""), "ok": not bool(event.get("isError")),
                                      "output": flatten(result.get("content"))[:PREVIEW]})
        elif kind == "agent_settled":
            self.finished = True
            self.aborted = bool(event.get("aborted"))
            self.close_input = True

    def _response(self, event):
        command = event.get("command")
        if command == "get_state":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            model = data.get("model") if isinstance(data.get("model"), dict) else {}
            window = as_int(model.get("contextWindow"))
            self.window = window if window else None
            count = as_int(data.get("messageCount")) or 0
            if self.cancelled:
                self.close_input = True
                return
            if self.resume and count == 0:
                self.gone = True
                self.close_input = True
                return
            if count:
                self.session_seen = True
            self.outbox.append({"id": f"prompt-{self.request_id}", "type": "prompt", "message": self.prompt})
            self.prompt_sent = True
        elif command == "prompt" and not event.get("success"):
            self.error = str(event.get("error") or "pi refused the message")
            self.finished = True
            self.close_input = True
        elif command == "prompt" and (event.get("data") or {}).get("disposition") == "handled":
            self.finished = True    # nothing was started: no agent_settled comes
            self.close_input = True

    def _update(self, update):
        if not isinstance(update, dict):
            return
        if update.get("type") == "text_delta" and isinstance(update.get("delta"), str) and update["delta"]:
            self.streamed = True
            self.emit("text_delta", {"text": update["delta"]})

    def _message_end(self, message):
        if not isinstance(message, dict):
            return
        role = message.get("role")
        if role == "user":
            self.session_seen = True    # the learner's message is in pi's session now
            return
        if role != "assistant":
            return
        if not self.streamed:
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                    self.emit("text_delta", {"text": block["text"]})
        self.streamed = False
        if message.get("stopReason") == "error":
            self.error = str(message.get("errorMessage") or "the model's request failed")
        elif self.error is not None and self.prompt_sent:
            self.error = None    # pi retried, and this request went through
        usage = read_usage(message.get("usage"))
        # A request that counted nothing (cut by Stop before it began) says nothing of the size.
        if usage is not None and (usage["context"] or usage["out"]):
            self.requests.append(usage)
            self.emit("usage", {"usage": self.usage()})

    def cancel(self):
        self.cancelled = True
        if not self.prompt_sent:
            self.outbox = [obj for obj in self.outbox if obj.get("type") != "prompt"]

    def take(self):
        out, self.outbox = self.outbox, []
        return out

    def usage(self):
        """As claude_stream's: the size is the LAST request's input, the output the turn's sum,
        the window from pi's state (left out when pi gives none), the cost summed."""
        if not self.requests:
            return None
        last = self.requests[-1]
        costs = [r["cost"] for r in self.requests if r["cost"] is not None]
        return {"context_tokens": last["context"], "context_window": self.window, "cache_read": last["cache_read"],
                "cache_write": last["cache_write"], "fresh": last["fresh"],
                "output": sum(r["out"] or 0 for r in self.requests), "requests": len(self.requests),
                "cost_usd": round(sum(costs), 6) if costs else None}
