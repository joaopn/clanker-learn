"""Server-sent events: a fan-out bus, and the stream of one run, replayed then live.

Lifted from gaggiclanker (the maintainer's project, AGPL like this one): EventBus from
gaggiclanker/infra/sse.py, and the stream with replay from gaggiclanker/api/chat.py
(`_run_stream`). Adapted: aiohttp instead of sse-starlette (the response is written here, a
ping comment every 15 seconds), the standard library's logging, and events as plain
dictionaries {"run_id", "seq", "kind", "data"}.
"""
import asyncio
import contextlib
import json
from dataclasses import dataclass, field

from aiohttp import web

# Proxies and browsers drop an idle event stream; 15 s is under the usual 30-60 s idle timeouts.
PING_SECONDS = 15
# Per subscriber. A stream that stops reading must not stall a turn or grow the process's
# memory, so its queue drops the oldest event; a stream that reconnects replays from the table.
QUEUE_SIZE = 1024
TERMINAL = ("completed", "error", "cancelled")


@dataclass
class EventBus:
    """An in-process publish/subscribe hub. Publishing never blocks and never raises."""

    queue_size: int = QUEUE_SIZE
    _subscribers: set = field(default_factory=set, repr=False)
    dropped: int = 0

    def publish(self, event):
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                    self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextlib.contextmanager
    def subscribe(self):
        queue = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


async def run_stream(bus, replay, is_running, run_id, after):
    """The events of one run with `seq` > `after`: replayed from the table, then followed live,
    until a terminal kind. The subscription is taken before the replay, so that nothing that
    happens between the two is missed; what the replay gave is skipped on the bus by its
    sequence number. `replay(run_id, after)` reads the table; `is_running(run_id)` says whether
    the run still goes on (its events are stored before they are published)."""
    highest = after
    with bus.subscribe() as queue:
        for event in replay(run_id, after):
            highest = max(highest, event["seq"])
            yield event
            if event["kind"] in TERMINAL:
                return
        if not is_running(run_id):
            # It finished while we were replaying: its last events are in the table by now.
            for event in replay(run_id, highest):
                yield event
            return
        while True:
            event = await queue.get()
            if event["run_id"] != run_id or event["seq"] <= highest:
                continue
            highest = event["seq"]
            yield event
            if event["kind"] in TERMINAL:
                return


async def sse_response(request, events):
    """Write `events` (an async iterator of {"seq", "kind", "data"}) as a text/event-stream,
    with a ping comment every PING_SECONDS. Ends at the end of `events`, or when the client has
    gone (a write fails); the run itself goes on either way."""
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await response.prepare(request)
    iterator = events.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=PING_SECONDS)
            if not done:
                await response.write(b": ping\n\n")
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            pending = None
            body = json.dumps({"seq": event["seq"], "kind": event["kind"], "data": event["data"]})
            await response.write(f"id: {event['seq']}\nevent: event\ndata: {body}\n\n".encode())
    except ConnectionError:   # the client went away
        pass
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        with contextlib.suppress(Exception):
            await iterator.aclose()
    with contextlib.suppress(ConnectionError, RuntimeError):
        await response.write_eof()
    return response
