"""The "learn" tool server: MCP over HTTP, for Claude Code and pi in a project's sandbox.

The transport is lifted from research-sandbox (the maintainer's project),
container/role-mcp/daemon.py (handle_mcp, _rpc_result, _rpc_error, _tool_text_result): one
POST /mcp endpoint answering JSON-RPC (initialize, tools/list, tools/call, ping) with a single
JSON response each, no event stream, a notification answered 202. That is all Claude Code's
http transport needs. The token check is step 0's spike's (spike/tutor/learn_server.py).

It listens on every address, port 7802, because a sandbox reaches the service on its project's
network by the name cl-tutor; the API (port 7801, with the secret shared with the web server)
stays on the front network only, and neither listener answers the other's routes. Every
request carries `Authorization: Bearer <token>`, the token of a run that is running now (made
when the run starts, gone when it ends): a missing, unknown or dead one is refused before
anything else is looked at. The token names the project, the lesson and the run; no argument
of a call does.
"""
import hashlib
import json
import logging

import tools
from aiohttp import web

log = logging.getLogger("tutor")

PORT = 7802
PROTOCOL_VERSION = "2024-11-05"   # RS's; Claude Code takes it


def token_key(token):
    """The tokens are held by their hash: a lookup by the token itself would time its prefix."""
    return hashlib.sha256(token.encode()).hexdigest()


def rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def tool_result(payload, is_error=False):
    """MCP's result envelope: one text part holding the JSON payload."""
    out = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
    if is_error:
        out["isError"] = True
    return out


@web.middleware
async def token_required(request, handler):
    header = request.headers.get("Authorization", "")
    turn = request.app["runner"].turn_of_token(token_key(header[7:])) if header.startswith("Bearer ") and len(header) > 7 else None
    if turn is None:
        log.info("tool server: refused %s %s from %s (%s)", request.method, request.path, request.remote,
                 "no token" if not header else "a token of no running answer")
        return web.json_response({"error": "This request does not carry the token of a running answer."}, status=401)
    request["turn"] = turn
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status in (404, 405):
            return web.json_response({"error": "There is no such route on the tool server."}, status=exc.status)
        raise


async def handle_mcp(request):
    try:
        body = await request.json()
    except ValueError:
        return web.json_response(rpc_error(None, -32700, "parse error"), status=400)
    rid = body.get("id") if isinstance(body, dict) else None
    method = body.get("method") if isinstance(body, dict) else None
    params = body.get("params") if isinstance(body, dict) else {}
    if rid is None:   # a notification: no answer expected
        return web.Response(status=202)
    turn = request["turn"]
    if method == "initialize":
        return web.json_response(rpc_result(rid, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                                                  "serverInfo": {"name": "learn", "version": "1"}}))
    if method == "tools/list":
        return web.json_response(rpc_result(rid, {"tools": tools.listed(turn.kind)}))
    if method == "tools/call":
        if not isinstance(params, dict):
            return web.json_response(rpc_error(rid, -32602, "invalid params"))
        args = params.get("arguments") or {}
        ctx = {"db": request.app["db"], "runner": request.app["runner"], "turn": turn}
        ok, payload = await tools.dispatch(ctx, str(params.get("name") or ""), args)
        return web.json_response(rpc_result(rid, tool_result(payload, not ok)))
    if method in ("ping", "logging/setLevel"):
        return web.json_response(rpc_result(rid, {}))
    return web.json_response(rpc_error(rid, -32601, f"unknown method {method!r}"))


def build_tool_app(db, runner):
    app = web.Application(middlewares=[token_required], client_max_size=256 * 1024)
    app["db"], app["runner"] = db, runner
    app.add_routes([web.post("/mcp", handle_mcp)])
    return app
