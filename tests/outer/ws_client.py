"""A small WebSocket client for the terminal's tests, run by `python3 -c <this file's text>` in
a container of the web server's image (the one image at hand with aiohttp): one connection per
run, described as JSON on standard input, answered as JSON on standard output. It does not
verify the certificate (the server's own, self-signed).

The request: {"url", "origin"?, "cookies"?: {name: value}, "first"?: the first message (a JSON
object), "send"?: text sent as one binary frame after {"type": "connected"}, "until"?: text
that ends the wait once the binary frames carry it, "seconds"?}. "origin" is the header's
value, the address's own origin when it is missing. The answer: {"status": the upgrade's HTTP
status (101 when it was upgraded, 0 when nothing answered), "messages": the JSON messages, in
order, "output": the binary frames as text, "closed": whether the server closed}.
"""
import asyncio
import json
import sys
import time
import urllib.parse

import aiohttp


async def talk(asked):
    parts = urllib.parse.urlsplit(asked["url"])
    headers = {"Origin": asked.get("origin", f"https://{parts.netloc}")}
    cookies = asked.get("cookies") or {}
    if cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
    seen = {"status": 0, "messages": [], "output": "", "closed": False}
    deadline = time.monotonic() + asked.get("seconds", 20)
    async with aiohttp.ClientSession() as session:
        try:
            ws = await session.ws_connect(asked["url"], headers=headers, ssl=False, timeout=10)
        except aiohttp.WSServerHandshakeError as exc:
            seen["status"] = exc.status
            return seen
        except (OSError, aiohttp.ClientError) as exc:
            seen["output"] = repr(exc)
            return seen
        seen["status"] = 101
        if asked.get("first") is not None:
            await ws.send_json(asked["first"])
        output, typed, connected_at = b"", False, None
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = await ws.receive(timeout=min(left, 0.5))
            except TimeoutError:
                msg = None
            if msg is not None and msg.type == aiohttp.WSMsgType.TEXT:
                seen["messages"].append(json.loads(msg.data))
                if seen["messages"][-1] == {"type": "connected"}:
                    connected_at = time.monotonic()
            elif msg is not None and msg.type == aiohttp.WSMsgType.BINARY:
                output += msg.data
            elif msg is not None:
                seen["closed"] = True
                break
            # Typed once the terminal drew something (or after a few seconds): the shell is then there to read it.
            if asked.get("send") and not typed and connected_at is not None and (output or time.monotonic() - connected_at > 3):
                await ws.send_bytes(asked["send"].encode())
                typed = True
            if asked.get("until") and asked["until"].encode() in output:
                break
        seen["output"] = output.decode(errors="replace")
        await ws.close()
    return seen


print(json.dumps(asyncio.run(talk(json.load(sys.stdin)))))
