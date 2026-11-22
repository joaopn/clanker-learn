"""The web server's unit tests: its rules, its routes through a real aiohttp server with a
stand-in for the broker, its certificate, and where it listens. No container is started."""
import asyncio
import ipaddress
import json
import logging
import os
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import aiohttp
import asyncssh
import broker
import core
import pytest
import server
import services
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from yarl import URL

RIGHT_PROOF = "right-proof"
BROKER_TOKEN = "the-brokers-token-for-this-session"
SESSION = "a-cookie-value"
LONGEST_NAME = "p" + "1" * 30


# --- the stand-in for the broker -----------------------------------------------------------

class FakeBroker:
    """Stands in for server.broker_call: records (verb, args, token, op_id) and answers with
    what `replies` holds for the verb (a reply, an exception to raise, or a function of the
    args), else a plain success."""

    def __init__(self):
        self.calls = []
        self.replies = {
            "login": lambda args: broker.ok({"token": BROKER_TOKEN, "expires_at": time.time() + 3600, "principal": "admin"})
            if args.get("proof") == RIGHT_PROOF else broker.err("auth", "invalid credentials"),
        }

    async def __call__(self, verb, args=None, *, token=None, op_id=None, timeout=server.BROKER_CALL_TIMEOUT_S):
        self.calls.append((verb, args, token, op_id))
        reply = self.replies.get(verb, broker.ok({}))
        if callable(reply):
            reply = reply(args or {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    def verbs(self):
        return [call[0] for call in self.calls]


class Clock:
    def __init__(self, at=1000.0):
        self.at = at

    def __call__(self):
        return self.at


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """A fresh server state: no session, fresh limiters on a clock of their own, the broker
    stood in for, and the operations' logs in a temporary folder."""
    stand_in = FakeBroker()
    stand_in.clock = Clock()
    monkeypatch.setattr(server, "broker_call", stand_in)
    monkeypatch.setattr(server, "BROKER_SESSIONS", {})
    monkeypatch.setattr(server, "SESSIONS", {})
    monkeypatch.setattr(server, "TERMINALS", {})
    monkeypatch.setattr(server, "LOGIN_LIMITER", server.LoginLimiter(now=stand_in.clock))
    monkeypatch.setattr(server, "SETUP_LIMITER", server.LoginLimiter(now=stand_in.clock))
    (tmp_path / "oplogs").mkdir()
    monkeypatch.setattr(server, "BROKER_OPLOG_DIR", str(tmp_path / "oplogs"))
    monkeypatch.setattr(server, "ORIGIN_PORTS", {})
    monkeypatch.setattr(server, "PASSTHROUGH_CLAIMS", {})
    monkeypatch.setattr(server, "SANDBOX_ADDRESSES", {})
    monkeypatch.setattr(server, "ORIGIN_PORTS_FILE", tmp_path / "web" / "origin-ports.json")
    monkeypatch.setattr(server, "PASSTHROUGH_FILE", tmp_path / "web" / "passthrough-claims.json")
    return stand_in


def served(test):
    """Run `test(client)` against a real aiohttp server over build_app(), on loopback. The
    client keeps no cookies: each test hands the session's cookie over itself (a Secure cookie
    is not sent back over plain HTTP)."""
    async def run():
        async with TestClient(TestServer(server.build_app()), cookie_jar=aiohttp.DummyCookieJar()) as client:
            return await test(client)
    return asyncio.run(run())


def own_origin(client):
    return {"Origin": f"http://127.0.0.1:{client.port}"}


def with_session(client, expires_in=600.0):
    """A live session, made the way the login makes it, and the headers that carry it."""
    server.BROKER_SESSIONS[SESSION] = {"broker_token": BROKER_TOKEN, "expires": time.time() + expires_in}
    return {**own_origin(client), "Cookie": f"{server.BROKER_COOKIE}={SESSION}"}


# --- the origin, the limiter, the names ---------------------------------------------------------

def request_with(origin=None, host="127.0.0.1:7788"):
    headers = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    return make_mocked_request("POST", "/broker/login", headers=headers)


@pytest.mark.parametrize("origin, accepted", [
    ("http://127.0.0.1:7788", True),
    ("https://127.0.0.1:7788", True),
    ("https://localhost:7788", False),                 # another host
    ("https://192.0.2.1:7788", False),
    ("https://127.0.0.1:7789", False),                 # another port
    ("https://127.0.0.1", False),
    ("https://127.0.0.1:7788.example", False),
    ("null", False),
    ("", False),
    (None, False),                                     # no Origin at all
])
def test_only_the_servers_own_origin_is_accepted(origin, accepted):
    assert server.origin_ok(request_with(origin)) is accepted


def test_ten_failures_in_the_window_lock_for_sixty_seconds_and_the_lock_clears_by_itself():
    clock = Clock()
    limiter = server.LoginLimiter(now=clock)
    for _ in range(9):
        limiter.record_failure()
        clock.at += 1
    assert limiter.retry_after() == 0
    limiter.record_failure()
    assert limiter.retry_after() == 60
    clock.at += 59.5
    assert limiter.retry_after() == 0 or limiter.retry_after() < 1
    clock.at += 0.5
    assert limiter.retry_after() == 0
    limiter.record_failure()                                     # the lock cleared its count too
    assert limiter.retry_after() == 0


def test_failures_outside_the_window_do_not_count():
    clock = Clock()
    limiter = server.LoginLimiter(now=clock)
    for _ in range(9):
        limiter.record_failure()
    clock.at += 60                                               # the nine are a minute old: out
    limiter.record_failure()
    assert limiter.retry_after() == 0
    for _ in range(8):
        limiter.record_failure()
    assert limiter.retry_after() == 0
    limiter.record_failure()                                     # ten within the minute
    assert limiter.retry_after() == 60


def test_a_success_clears_the_failures_and_the_lock():
    clock = Clock()
    limiter = server.LoginLimiter(now=clock)
    for _ in range(9):
        limiter.record_failure()
    limiter.record_success()
    limiter.record_failure()
    assert limiter.retry_after() == 0
    for _ in range(10):
        limiter.record_failure()
    assert limiter.retry_after() == 60
    limiter.record_success()
    assert limiter.retry_after() == 0


def test_the_cookie_names_carry_the_port():
    assert services.broker_cookie_name(7788) == "cl_broker_7788"
    assert services.broker_cookie_name(7788) != services.broker_cookie_name(8443)
    assert services.session_cookie_name(7788, "p1") == "cl_session_7788_p1"
    assert services.session_cookie_name(7788, "p1") != services.session_cookie_name(8443, "p1")
    assert server.BROKER_COOKIE == services.broker_cookie_name(server.WEB_PORT)
    named = subprocess.run([sys.executable, "-c", "import server; print(server.BROKER_COOKIE)"], cwd=server.__file__.rsplit("/", 1)[0],
                           env={**os.environ, "CL_WEB_PORT": "8443", "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True, timeout=60, check=False)
    assert named.stdout.strip() == "cl_broker_8443", named.stderr


@pytest.mark.parametrize("action", ["create", "start", "stop", "recreate", "destroy"])
def test_a_minted_op_id_is_one_the_broker_takes_for_the_longest_name(action):
    core.valid_name(LONGEST_NAME)                                # the longest name there can be
    with pytest.raises(core.CoreError):
        core.valid_name(LONGEST_NAME + "1")
    op_id = server._mint_op_id(LONGEST_NAME, action)
    assert server._OP_ID_RE.fullmatch(op_id)
    assert broker.checked_op_id(op_id) == op_id
    assert server._mint_op_id(LONGEST_NAME, action) != op_id


def test_the_fields_forwarded_are_the_brokers():
    for verb in server.BROKER_FIELDS:
        assert set(server.BROKER_FIELDS[verb]) | {"name"} == broker.FIELDS[verb]
    assert set(server.BROKER_FIELDS) == {"create", "limits", "port_add", "port_remove", "agents_add", "agents_model"}
    assert set(server.PROJECT_ACTIONS) <= broker.OP_VERBS and set(server.PROJECT_ACTIONS) <= set(broker.FIELDS)
    body = {"name": "p1", "pids": 100, "memory": "1g", "cpus": "2", "proof": "x", "token": "t", "op_id": "o"}
    assert server._forward(body, "create") == {"name": "p1", "pids": 100, "memory": "1g", "cpus": "2"}
    assert server._forward(body, "limits") == {"pids": 100, "memory": "1g", "cpus": "2"}
    assert server._forward({"memory": "1g"}, "limits") == {"memory": "1g"}


# --- the routes: what needs a session, and the origin ---------------------------------------------

GATED = [
    ("GET", "/broker/projects", None),
    ("GET", "/broker/lifecycle", None),
    ("GET", "/broker/project/p1", None),
    ("POST", "/broker/project", {"name": "p1"}),
    ("POST", "/broker/project", "not json"),
    ("POST", "/broker/project/p1/limits", {"memory": "1g"}),
    ("POST", "/broker/project/p1/limits", "not json"),
    *[("POST", f"/broker/project/p1/{action}", {"proof": RIGHT_PROOF}) for action in server.PROJECT_ACTIONS],
    ("POST", "/broker/project/p1/frob", None),
    ("POST", "/broker/project/p1/session", None),
    ("GET", "/broker/project/p1/services", None),
    ("POST", "/broker/project/p1/origin", {"port": 7070}),
    ("POST", "/broker/project/p1/origin", "not json"),
    ("GET", "/broker/project/p1/ports", None),
    ("POST", "/broker/project/p1/ports", {"port": 8000, "label": "notebook"}),
    ("POST", "/broker/project/p1/ports", "not json"),
    ("POST", "/broker/project/p1/ports/remove", {"port": 8000}),
    ("GET", "/broker/op/p1-start-1-ab/log", None),
    ("GET", "/broker/op/p1-start-1-ab/alive", None),
    ("GET", "/broker/op/p1-start-1-ab", None),
    ("GET", "/broker/op/-bad/alive", None),
    ("GET", "/broker/op/-bad", None),
    ("GET", "/broker/op/-bad/log", None),
]


async def ask(client, method, path, body, headers):
    if isinstance(body, str):
        return await client.request(method, path, data=body, headers=headers)
    return await client.request(method, path, json=body, headers=headers)


@pytest.mark.parametrize("method, path, body", GATED)
@pytest.mark.parametrize("cookie", [None, "a-cookie-nobody-was-given"])
def test_without_a_session_every_route_but_the_open_ones_is_401_and_the_broker_is_not_asked(fake, method, path, body, cookie):
    async def test(client):
        headers = own_origin(client)
        if cookie:
            headers["Cookie"] = f"{server.BROKER_COOKIE}={cookie}"
        answer = await ask(client, method, path, body, headers)
        return answer.status, await answer.json()
    status, said = served(test)
    assert status == 401 and said == {"ok": False, "error": {"kind": "unauthorized"}}
    assert fake.calls == []


POSTS = [
    ("/broker/login", {"proof": RIGHT_PROOF}),
    ("/broker/logout", None),
    ("/broker/setup", {"code": "c" * 20, "proof": RIGHT_PROOF}),
    ("/broker/project", {"name": "p1"}),
    ("/broker/project/p1/limits", {"memory": "1g"}),
    *[(f"/broker/project/p1/{action}", {"proof": RIGHT_PROOF}) for action in server.PROJECT_ACTIONS],
    ("/broker/project/p1/session", None),
    ("/broker/project/p1/origin", {"port": 7070}),
    ("/broker/project/p1/ports", {"port": 8000, "label": "notebook"}),
    ("/broker/project/p1/ports/remove", {"port": 8000}),
]


@pytest.mark.parametrize("path, body", POSTS)
@pytest.mark.parametrize("origin", [None, "", "https://192.0.2.1:{port}", "http://127.0.0.1:1", "null"])
def test_a_post_from_another_origin_or_none_is_403_and_the_broker_is_not_asked(fake, path, body, origin):
    async def test(client):
        headers = with_session(client)
        del headers["Origin"]
        if origin is not None:
            headers["Origin"] = origin.format(port=client.port)
        answer = await client.post(path, json=body, headers=headers)
        return answer.status
    assert served(test) == 403
    assert fake.calls == [] and SESSION in server.BROKER_SESSIONS


# --- login and logout -----------------------------------------------------------------------------

def test_a_right_proof_sets_a_strict_cookie_for_the_port_and_keeps_the_token_here(fake):
    async def test(client):
        answer = await client.post("/broker/login", json={"proof": RIGHT_PROOF}, headers=own_origin(client))
        text = await answer.text()
        set_cookie = answer.headers.getall("Set-Cookie")
        cookie = answer.cookies[f"cl_broker_{server.WEB_PORT}"]
        listed = await client.get("/broker/projects", headers={"Cookie": f"{cookie.key}={cookie.value}"})
        return answer.status, text, set_cookie, cookie, listed.status, await listed.json()
    status, text, set_cookie, cookie, listed_status, listed = served(test)
    assert status == 200 and json.loads(text) == {"ok": True}
    assert BROKER_TOKEN not in text and BROKER_TOKEN not in "".join(set_cookie) and BROKER_TOKEN != cookie.value
    assert len(set_cookie) == 1
    attributes = {part.strip().split("=")[0].lower(): part.strip() for part in set_cookie[0].split(";")[1:]}
    assert {"httponly", "secure", "samesite", "path"} <= set(attributes)
    assert attributes["samesite"] == "SameSite=Strict" and attributes["path"] == "Path=/broker"
    assert 3500 < int(cookie["max-age"]) <= 3600
    assert server.BROKER_SESSIONS[cookie.value]["broker_token"] == BROKER_TOKEN
    assert fake.calls == [("login", {"proof": RIGHT_PROOF}, None, None), ("list", None, BROKER_TOKEN, None)]
    assert listed_status == 200 and listed == {"ok": True, "result": {}}


def test_a_wrong_proof_is_401_and_counted_and_after_ten_it_is_429_without_asking_the_broker(fake):
    async def test(client):
        statuses = []
        for _ in range(10):
            answer = await client.post("/broker/login", json={"proof": "wrong"}, headers=own_origin(client))
            statuses.append((answer.status, await answer.json(), answer.cookies.get(server.BROKER_COOKIE)))
        locked = await client.post("/broker/login", json={"proof": RIGHT_PROOF}, headers=own_origin(client))
        return statuses, locked.status, locked.headers.get("Retry-After"), await locked.json()
    statuses, status, retry_after, said = served(test)
    assert statuses == [(401, {"ok": False, "error": {"kind": "auth"}}, None)] * 10
    assert status == 429 and retry_after == "60" and said["error"]["kind"] == "rate_limited"
    assert fake.verbs() == ["login"] * 10 and server.BROKER_SESSIONS == {}
    fake.clock.at += 60
    assert served(lambda client: login_status(client)) == 200


async def login_status(client):
    answer = await client.post("/broker/login", json={"proof": RIGHT_PROOF}, headers=own_origin(client))
    return answer.status


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '"proof"', "{}", '{"proof": 3}', '{"proof": null}', '{"code": "x"}'])
def test_a_login_body_that_is_not_an_object_with_a_string_proof_is_400(fake, body):
    async def test(client):
        answer = await client.post("/broker/login", data=body, headers=own_origin(client))
        return answer.status, await answer.json()
    assert served(test) == (400, {"ok": False, "error": {"kind": "bad_request"}})
    assert fake.calls == [] and server.LOGIN_LIMITER.retry_after() == 0 and server.LOGIN_LIMITER._failures == []


@pytest.mark.parametrize("kind, status", [(server.BrokerUnavailable("gone"), 503), (server.BrokerForbidden("peer"), 403)])
def test_a_login_the_broker_cannot_answer_is_not_counted(fake, kind, status):
    fake.replies["login"] = kind
    assert served(login_status) == status
    assert server.LOGIN_LIMITER._failures == []


def test_a_session_past_its_expiry_is_dropped(fake):
    async def test(client):
        headers = with_session(client, expires_in=-1)
        answer = await client.get("/broker/projects", headers=headers)
        return answer.status
    assert served(test) == 401
    assert fake.calls == [] and server.BROKER_SESSIONS == {}


def test_a_login_whose_broker_token_has_expired_opens_nothing(fake):
    fake.replies["login"] = broker.ok({"token": BROKER_TOKEN, "expires_at": time.time() - 5, "principal": "admin"})

    async def test(client):
        answer = await client.post("/broker/login", json={"proof": RIGHT_PROOF}, headers=own_origin(client))
        cookie = answer.cookies[server.BROKER_COOKIE]
        listed = await client.get("/broker/projects", headers={"Cookie": f"{cookie.key}={cookie.value}"})
        return cookie["max-age"], listed.status
    assert served(test) == ("0", 401)
    assert fake.verbs() == ["login"] and server.BROKER_SESSIONS == {}


@pytest.mark.parametrize("method, path, body", [("GET", "/broker/projects", None), ("POST", "/broker/project/p1/start", None),
                                                ("POST", "/broker/project", {"name": "p1"})])
def test_a_broker_that_no_longer_knows_the_token_ends_the_session(fake, method, path, body):
    for verb in ("list", "start", "create"):
        fake.replies[verb] = broker.err("unauthorized", "a valid session token is required; call login")

    async def test(client):
        answer = await ask(client, method, path, body, with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (401, {"ok": False, "error": {"kind": "unauthorized"}})
    assert server.BROKER_SESSIONS == {} and len(fake.calls) == 1


@pytest.mark.parametrize("method, path, body", [("GET", "/broker/projects", None), ("POST", "/broker/project/p1/start", None),
                                                ("POST", "/broker/project", {"name": "p1"}), ("GET", "/broker/op/o1", None)])
@pytest.mark.parametrize("raised, status, kind", [(server.BrokerUnavailable("gone"), 503, "broker_unavailable"),
                                                  (server.BrokerForbidden("peer"), 403, "forbidden")])
def test_a_broker_that_cannot_be_reached_or_refuses_the_peer_is_503_or_403(fake, method, path, body, raised, status, kind):
    for verb in ("list", "start", "create", "op"):
        fake.replies[verb] = raised

    async def test(client):
        answer = await ask(client, method, path, body, with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (status, {"ok": False, "error": {"kind": kind}})
    assert SESSION in server.BROKER_SESSIONS


def test_a_logout_revokes_the_token_drops_the_session_and_clears_the_cookie(fake):
    async def test(client):
        headers = with_session(client)
        answer = await client.post("/broker/logout", headers=headers)
        after = await client.get("/broker/projects", headers=headers)
        return answer.status, await answer.json(), answer.cookies[server.BROKER_COOKIE], after.status
    status, said, cleared, after = served(test)
    assert (status, said, after) == (200, {"ok": True}, 401)
    assert fake.calls == [("logout", None, BROKER_TOKEN, None)] and server.BROKER_SESSIONS == {}
    assert cleared.value == "" and cleared["max-age"] == "0" and cleared["path"] == "/broker"


@pytest.mark.parametrize("cookie", [None, "a-cookie-nobody-was-given"])
def test_a_logout_without_a_session_is_200_and_asks_the_broker_nothing(fake, cookie):
    async def test(client):
        headers = own_origin(client)
        if cookie:
            headers["Cookie"] = f"{server.BROKER_COOKIE}={cookie}"
        answer = await client.post("/broker/logout", headers=headers)
        return answer.status, answer.cookies[server.BROKER_COOKIE]["max-age"]
    assert served(test) == (200, "0")
    assert fake.calls == []


def test_a_logout_the_broker_cannot_hear_still_ends_the_session_here(fake):
    fake.replies["logout"] = server.BrokerUnavailable("gone")

    async def test(client):
        answer = await client.post("/broker/logout", headers=with_session(client))
        return answer.status
    assert served(test) == 200 and server.BROKER_SESSIONS == {}


# --- the first password ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply, needed", [(broker.ok({"needed": True}), True), (broker.ok({"needed": False}), False),
                                           (broker.err("validation", "x"), False)])
def test_the_setup_state_relays_whether_a_first_password_can_be_set(fake, reply, needed):
    fake.replies["setup_needed"] = reply

    async def test(client):
        answer = await client.get("/broker/setup")
        return answer.status, await answer.json()
    assert served(test) == (200, {"ok": True, "needed": needed})
    assert fake.calls == [("setup_needed", None, None, None)]


@pytest.mark.parametrize("raised", [server.BrokerUnavailable("gone"), server.BrokerForbidden("peer")])
def test_the_setup_state_without_a_broker_is_503(fake, raised):
    fake.replies["setup_needed"] = raised

    async def test(client):
        answer = await client.get("/broker/setup")
        return answer.status, await answer.json()
    assert served(test) == (503, {"ok": False, "error": {"kind": "broker_unavailable"}})


async def setup_answer(client, code="c" * 20):
    answer = await client.post("/broker/setup", json={"code": code, "proof": RIGHT_PROOF}, headers=own_origin(client))
    return answer.status, await answer.json()


@pytest.mark.parametrize("reply, status, said, counted", [
    (broker.ok({"password": "set"}), 200, {"ok": True}, False),
    (broker.err("auth", "invalid credentials"), 401, {"ok": False, "error": {"kind": "auth"}}, True),
    (broker.err("rate_limited", "Too many wrong set-up codes; wait 42 seconds and try again."), 429,
     {"ok": False, "error": {"kind": "rate_limited", "message": "Too many wrong set-up codes; wait 42 seconds and try again."}}, False),
    (broker.err("validation", "The password did not arrive in the form a login sends it; nothing was stored."), 400,
     {"ok": False, "error": {"kind": "validation", "message": "The password did not arrive in the form a login sends it; nothing was stored."}}, False),
    (broker.err("bad_request", "first_password takes a code and a proof"), 400, {"ok": False, "error": {"kind": "bad_request"}}, False),
    (broker.err("failed", "No space left on device /secret/path"), 500, {"ok": False, "error": {"kind": "failed"}}, False),
    (broker.err("unknown_verb", "unknown verb 'first_password'"), 500, {"ok": False, "error": {"kind": "failed"}}, False),
    ({"ok": False}, 500, {"ok": False, "error": {"kind": "failed"}}, False),
])
def test_the_setup_answer_says_what_the_broker_said_and_counts_only_a_wrong_code(fake, reply, status, said, counted):
    fake.replies["first_password"] = reply
    assert served(setup_answer) == (status, said)
    assert fake.calls == [("first_password", {"code": "c" * 20, "proof": RIGHT_PROOF}, None, None)]
    assert len(server.SETUP_LIMITER._failures) == (1 if counted else 0)
    assert server.LOGIN_LIMITER._failures == []


def test_the_setup_route_has_a_limiter_of_its_own():
    assert isinstance(server.SETUP_LIMITER, server.LoginLimiter) and server.SETUP_LIMITER is not server.LOGIN_LIMITER


def test_ten_broker_lockouts_do_not_lock_the_setup_route(fake):
    fake.replies["first_password"] = broker.err("rate_limited", "wait")

    async def test(client):
        return [await setup_answer(client) for _ in range(12)]
    assert [status for status, _ in served(test)] == [429] * 12
    assert fake.verbs() == ["first_password"] * 12 and server.SETUP_LIMITER.retry_after() == 0


def test_ten_wrong_codes_lock_the_setup_route_and_not_the_login(fake):
    fake.replies["first_password"] = broker.err("auth", "invalid credentials")

    async def test(client):
        wrong = [await setup_answer(client) for _ in range(10)]
        locked = await client.post("/broker/setup", json={"code": "c" * 20, "proof": RIGHT_PROOF}, headers=own_origin(client))
        return wrong, locked.status, locked.headers.get("Retry-After"), await login_status(client)
    wrong, status, retry_after, login = served(test)
    assert [s for s, _ in wrong] == [401] * 10 and (status, retry_after) == (429, "60")
    assert fake.verbs() == ["first_password"] * 10 + ["login"] and login == 200


def test_ten_wrong_logins_do_not_lock_the_setup_route(fake):
    async def test(client):
        for _ in range(10):
            await client.post("/broker/login", json={"proof": "wrong"}, headers=own_origin(client))
        return await login_status(client), await setup_answer(client)
    assert served(test) == (429, (200, {"ok": True}))
    assert fake.verbs() == ["login"] * 10 + ["first_password"]


@pytest.mark.parametrize("body", ["not json", "[]", "{}", '{"code": "c"}', '{"proof": "p"}', '{"code": 1, "proof": "p"}', '{"code": "c", "proof": null}'])
def test_a_setup_body_without_a_string_code_and_proof_is_400(fake, body):
    async def test(client):
        answer = await client.post("/broker/setup", data=body, headers=own_origin(client))
        return answer.status
    assert served(test) == 400 and fake.calls == [] and server.SETUP_LIMITER._failures == []


# --- projects -------------------------------------------------------------------------------------

def test_a_create_hands_the_broker_its_fields_the_token_and_the_op_id_it_returns(fake):
    async def test(client):
        answer = await client.post("/broker/project", json={"name": "p1", "memory": "1g", "pids": 200, "cpus": "1.5",
                                                            "proof": "x", "token": "forged", "op_id": "mine"},
                                   headers=with_session(client))
        return answer.status, await answer.json()
    status, said = served(test)
    (verb, args, token, op_id), = fake.calls
    assert status == 200 and said == {"ok": True, "op_id": op_id}
    assert (verb, args, token) == ("create", {"name": "p1", "memory": "1g", "pids": 200, "cpus": "1.5"}, BROKER_TOKEN)
    assert broker.checked_op_id(op_id) == op_id and op_id.startswith("p1-create-")


@pytest.mark.parametrize("body", ['{"name": 3}', '{"memory": "1g"}', '{"name": null}', "[]", "not json"])
def test_a_create_without_a_string_name_is_400(fake, body):
    async def test(client):
        answer = await client.post("/broker/project", data=body, headers=with_session(client))
        return answer.status
    assert served(test) == 400 and fake.calls == []


@pytest.mark.parametrize("path", ["/broker/project", "/broker/project/p1/start"])
@pytest.mark.parametrize("reply", [broker.err("busy", "Something else is being done with p1; try again when it is over."),
                                   broker.err("validation", "A project name is 1 to 31 lower-case letters"),
                                   broker.err("step_up_required", "this action requires re-entering your password")])
def test_a_refusal_of_the_broker_comes_back_as_it_said_it_with_200(fake, path, reply):
    fake.replies["create"] = fake.replies["start"] = reply

    async def test(client):
        answer = await client.post(path, json={"name": "p1"}, headers=with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (200, reply)
    assert SESSION in server.BROKER_SESSIONS


def test_a_name_that_makes_no_op_id_is_a_validation_error_without_asking_the_broker(fake):
    async def test(client):
        answer = await client.post("/broker/project", json={"name": "-x"}, headers=with_session(client))
        return answer.status, await answer.json()
    status, said = served(test)
    assert status == 200 and said["error"]["kind"] == "validation" and fake.calls == []


def test_limits_take_the_name_from_the_path_and_only_the_limits(fake):
    fake.replies["limits"] = broker.ok({"memory": "2g"})

    async def test(client):
        answer = await client.post("/broker/project/p1/limits", json={"name": "other", "memory": "2g", "pids": 300, "cpus": "1",
                                                                      "proof": "x", "evil": 1}, headers=with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (200, {"ok": True, "result": {"memory": "2g"}})
    assert fake.calls == [("limits", {"memory": "2g", "pids": 300, "cpus": "1", "name": "p1"}, BROKER_TOKEN, None)]


def test_a_limits_body_that_is_not_an_object_is_400(fake):
    async def test(client):
        answer = await client.post("/broker/project/p1/limits", data="[1]", headers=with_session(client))
        return answer.status
    assert served(test) == 400 and fake.calls == []


@pytest.mark.parametrize("action", ["start", "stop", "recreate", "destroy"])
def test_each_action_reaches_the_broker_under_its_own_verb_with_an_op_id(fake, action):
    async def test(client):
        answer = await client.post(f"/broker/project/p1/{action}", json={"proof": RIGHT_PROOF, "name": "other", "memory": "1g"},
                                   headers=with_session(client))
        return answer.status, await answer.json()
    status, said = served(test)
    (verb, args, token, op_id), = fake.calls
    assert status == 200 and said == {"ok": True, "op_id": op_id}
    assert (verb, token) == (action, BROKER_TOKEN) and broker.checked_op_id(op_id).startswith(f"p1-{action}-")
    assert args == ({"name": "p1", "proof": RIGHT_PROOF} if action == "destroy" else {"name": "p1"})


def test_a_destroy_without_a_proof_is_handed_on_without_one(fake):
    async def test(client):
        answer = await client.post("/broker/project/p1/destroy", data="not json", headers=with_session(client))
        return answer.status
    assert served(test) == 200 and fake.calls[0][:2] == ("destroy", {"name": "p1"})


@pytest.mark.parametrize("action", ["frob", "list", "create", "limit", "status", "Start", "login", "first_password"])
def test_any_other_action_is_400(fake, action):
    async def test(client):
        answer = await client.post(f"/broker/project/p1/{action}", json={}, headers=with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (400, {"ok": False, "error": {"kind": "bad_request"}}) and fake.calls == []


def test_the_project_list_and_a_projects_status_are_relayed(fake):
    fake.replies["list"] = broker.ok([{"name": "p1"}])
    fake.replies["status"] = broker.ok({"name": "p1", "state": "running"})

    async def test(client):
        headers = with_session(client)
        listed = await client.get("/broker/projects", headers=headers)
        one = await client.get("/broker/project/p1", headers=headers)
        return await listed.json(), await one.json()
    assert served(test) == ({"ok": True, "result": [{"name": "p1"}]}, {"ok": True, "result": {"name": "p1", "state": "running"}})
    assert fake.calls == [("list", None, BROKER_TOKEN, None), ("status", {"name": "p1"}, BROKER_TOKEN, None)]


# --- operations -----------------------------------------------------------------------------------

def test_lifecycle_an_ops_state_and_whether_it_is_alive_are_relayed(fake):
    fake.replies["lifecycle_active"] = broker.ok({"ops": [{"op_id": "o1", "project": "p1", "verb": "start", "state": "running"}]})
    fake.replies["op"] = broker.ok({"state": "done", "project": "p1", "verb": "start", "msg": "started"})
    fake.replies["build_alive"] = broker.ok({"alive": True})

    async def test(client):
        headers = with_session(client)
        return [(answer.status, await answer.json()) for answer in [
            await client.get("/broker/lifecycle", headers=headers),
            await client.get("/broker/op/p1-start-1-ab", headers=headers),
            await client.get("/broker/op/p1-start-1-ab/alive", headers=headers)]]
    assert served(test) == [
        (200, {"ok": True, "result": {"ops": [{"op_id": "o1", "project": "p1", "verb": "start", "state": "running"}]}}),
        (200, {"ok": True, "result": {"state": "done", "project": "p1", "verb": "start", "msg": "started"}}),
        (200, {"ok": True, "alive": True}),
    ]
    assert fake.calls == [("lifecycle_active", None, BROKER_TOKEN, None), ("op", {"op_id": "p1-start-1-ab"}, BROKER_TOKEN, None),
                          ("build_alive", {"op_id": "p1-start-1-ab"}, BROKER_TOKEN, None)]


@pytest.mark.parametrize("reply, said", [(broker.ok({"alive": False}), {"ok": True, "alive": False}),
                                         (broker.ok({}), {"ok": True, "alive": False}),
                                         (broker.err("bad_request", "op_id must be"), broker.err("bad_request", "op_id must be"))])
def test_alive_is_a_plain_yes_or_no_or_the_brokers_refusal(fake, reply, said):
    fake.replies["build_alive"] = reply

    async def test(client):
        answer = await client.get("/broker/op/o1/alive", headers=with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (200, said)


BAD_OP_IDS = ["-x", ".hidden", "..", "a" * 65, "a%20b", "a%0A", "%0Aa", "..%2Fsecret", "a%2F..%2F..%2Fsecret", "%2E%2E", "a~b"]


@pytest.mark.parametrize("op_id", BAD_OP_IDS)
@pytest.mark.parametrize("tail", ["", "/alive", "/log"])
def test_an_op_id_that_breaks_the_rule_is_refused_without_asking_the_broker(fake, op_id, tail):
    async def test(client):
        answer = await client.get(f"/broker/op/{op_id}{tail}", headers=with_session(client))
        return answer.status
    assert served(test) in (400, 404)
    assert fake.calls == []


@pytest.mark.parametrize("op_id", ["-x", ".hidden", "a" * 65, "a%20b", "a%0A", "a~b"])
@pytest.mark.parametrize("tail", ["", "/alive", "/log"])
def test_an_op_id_the_route_takes_and_the_rule_refuses_is_400(fake, op_id, tail):
    async def test(client):
        answer = await client.get(f"/broker/op/{op_id}{tail}", headers=with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (400, {"ok": False, "error": {"kind": "bad_request"}})


LOG = b'{"op_id": "o1", "step": "one"}\n{"op_id": "o1", "step": "two"}\n'


@pytest.mark.parametrize("frm, start", [("0", 0), ("31", 31), (str(len(LOG)), len(LOG)), ("abc", 0), ("-5", 0), ("", 0), ("1e3", 0)])
def test_the_ops_log_is_read_from_where_the_follower_is(fake, tmp_path, frm, start):
    (tmp_path / "oplogs" / "o1.view.log").write_bytes(LOG)

    async def test(client):
        answer = await client.get(f"/broker/op/o1/log?from={frm}", headers=with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (200, {"ok": True, "from": start, "next": len(LOG), "data": LOG[start:].decode(), "started": True})
    assert fake.calls == []


def test_the_ops_log_past_its_end_is_empty(fake, tmp_path):
    (tmp_path / "oplogs" / "o1.view.log").write_bytes(LOG)

    async def test(client):
        answer = await client.get("/broker/op/o1/log?from=1000", headers=with_session(client))
        return answer.status, await answer.json()
    status, said = served(test)
    assert status == 200 and said["data"] == "" and said["started"] is True and said["from"] == 1000


def test_a_log_not_written_yet_is_not_started_and_not_an_error(fake):
    async def test(client):
        answer = await client.get("/broker/op/o2/log?from=7", headers=with_session(client))
        return answer.status, await answer.json()
    assert served(test) == (200, {"ok": True, "from": 7, "next": 0, "data": "", "started": False})


def test_an_op_id_cannot_name_a_file_outside_the_logs_folder(fake, tmp_path):
    (tmp_path / "secret.view.log").write_text("the secret")
    (tmp_path / "oplogs" / "o1.view.log").write_bytes(LOG)

    async def test(client):
        headers = with_session(client)
        seen = []
        for op_id in ["..%2Fsecret", "%2E%2E%2Fsecret", "o1%2F..%2F..%2Fsecret", "..%252Fsecret", "%2E%2E/secret"]:
            answer = await client.get(f"/broker/op/{op_id}/log", headers=headers)
            seen.append((answer.status, await answer.text()))
        return seen
    for status, text in served(test):
        assert status in (400, 404) and "secret" not in text, (status, text)


def test_a_log_that_cannot_be_read_is_a_500_without_its_path(fake, tmp_path):
    (tmp_path / "oplogs" / "o1.view.log").mkdir()

    async def test(client):
        answer = await client.get("/broker/op/o1/log", headers=with_session(client))
        return answer.status, await answer.text()
    status, text = served(test)
    assert status == 500 and json.loads(text)["error"]["kind"] == "io_error" and str(tmp_path) not in text


# --- bodies, and the page ----------------------------------------------------------------------------

@pytest.mark.parametrize("path, session", [("/broker/login", False), ("/broker/setup", False), ("/broker/project", True),
                                           ("/broker/project/p1/limits", True), ("/broker/project/p1/destroy", True)])
def test_a_body_larger_than_a_mebibyte_is_413(fake, path, session):
    big = json.dumps({"proof": "x" * (1024 * 1024), "name": "p1", "code": "c"})

    async def test(client):
        headers = with_session(client) if session else own_origin(client)
        answer = await client.post(path, data=big, headers={**headers, "Content-Type": "application/json"})
        return answer.status
    assert served(test) == 413
    assert fake.calls == []


def test_the_page_and_its_files_are_served_without_a_login_with_their_headers(fake):
    async def test(client):
        out = {}
        for path in ("/", "/static/app.js", "/static/style.css", "/static/vendor/xterm.js"):
            answer = await client.get(path)
            out[path] = (answer.status, answer.headers, await answer.read())
        return out
    out = served(test)
    static = server.STATIC_DIR
    for path, file, kind in (("/", "index.html", "text/html"), ("/static/app.js", "app.js", "javascript"),
                             ("/static/style.css", "style.css", "text/css"), ("/static/vendor/xterm.js", "vendor/xterm.js", "javascript")):
        status, headers, body = out[path]
        assert status == 200 and body == (static / file).read_bytes() and kind in headers["Content-Type"]
        # Asked again at every load, so a changed file shows after a reload.
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-cache" and headers["ETag"]
    assert "Content-Security-Policy" in out["/"][1] and b'src="/static/app.js"' in out["/"][2]
    assert fake.calls == []


def directives(policy):
    """A content-security header as {directive: its sources}; a directive given twice fails."""
    out = {}
    for part in policy.split(";"):
        name, *sources = part.split()
        assert name not in out
        out[name] = sources
    return out


def page_policy(host):
    async def test(client):
        answer = await client.get("/", headers={"Host": host})
        return answer.status, answer.headers["Content-Security-Policy"]
    status, policy = served(test)
    assert status == 200
    return policy


THE_PAGES_OWN = {
    "default-src": ["'none'"], "script-src": ["'self'"], "style-src": ["'self'", "'unsafe-inline'"], "img-src": ["'self'"],
    "object-src": ["'none'"], "base-uri": ["'none'"], "form-action": ["'none'"], "frame-ancestors": ["'none'"]}


@pytest.mark.parametrize("host, frames, sockets", [
    ("127.0.0.1:7788", "https://127.0.0.1:*", "wss://127.0.0.1:7788"),
    ("localhost:7788", "https://localhost:*", "wss://localhost:7788"),
    ("learn.example", "https://learn.example:*", "wss://learn.example"),
])
def test_security_the_page_runs_its_own_scripts_only_and_frames_only_the_host_the_browser_used(fake, host, frames, sockets):
    # No inline script, no eval, nothing from another origin; fetches and the terminal to itself;
    # frames from any port of that host (the web-app tabs) and nowhere else.
    assert directives(page_policy(host)) == {**THE_PAGES_OWN, "connect-src": ["'self'", sockets], "frame-src": [frames],
                                             "img-src": ["'self'", frames]}


@pytest.mark.parametrize("host, frames, sockets", [
    ("[::1]:7788", "https://[::1]:*", "wss://[::1]:7788"),
    ("[2001:db8::7]", "https://[2001:db8::7]:*", "wss://[2001:db8::7]"),
])
def test_security_an_ipv6_host_is_framed_in_brackets(fake, host, frames, sockets):
    assert directives(page_policy(host)) == {**THE_PAGES_OWN, "connect-src": ["'self'", sockets], "frame-src": [frames],
                                             "img-src": ["'self'", frames]}


HOSTILE_HOSTS = [
    "evil.example; script-src *", "127.0.0.1:7788 https://evil.example", "127.0.0.1:7788;frame-ancestors *",
    "[::1]:7788; frame-src *", "[::1] evil.example", "[::1]:", "[fe80::1%25eth0]:7788", "[::1", "[1.2.3.4]",
    "learn'example", "*", "*.example", "https://evil.example", "-bad.example", "bad-.example", "a..example",
    "1.2.3.999", "999.1.1.1:7788", "127.0.0.1:99999", "127.0.0.1:", "127.0.0.1:7788:1", "ex ample", "lérn.example", "",
]


@pytest.mark.parametrize("host", HOSTILE_HOSTS)
def test_security_a_host_that_is_not_a_plain_host_frames_nothing_and_adds_nothing_to_the_header(fake, host):
    # Whatever the Host header holds, the header keeps the page's own directives and gains no
    # source from it: no frame source at all, the sockets to 'self' only.
    expected = {**THE_PAGES_OWN, "connect-src": ["'self'"], "frame-src": ["'none'"]}
    assert directives(server.content_security_policy(host)) == expected
    assert directives(server.content_security_policy(None)) == expected


def test_security_a_hostile_host_header_on_the_wire_frames_nothing(fake):
    assert directives(page_policy("evil.example; script-src *")) == {
        **THE_PAGES_OWN, "connect-src": ["'self'"], "frame-src": ["'none'"]}


def test_a_path_that_leaves_the_static_folder_is_404(fake, monkeypatch, tmp_path):
    static = tmp_path / "static"
    (static / "vendor").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>")
    (static / "app.js").write_text("// the page")
    (tmp_path / "secret.txt").write_text("not for the browser")
    (static / "link.js").symlink_to(tmp_path / "secret.txt")
    monkeypatch.setattr(server, "STATIC_DIR", static)

    async def test(client):
        out = []
        for path in ("/static/..%2fsecret.txt", "/static/%2e%2e/secret.txt", "/static/../secret.txt",
                     "/static/vendor/..%2f..%2fsecret.txt", "/static/%2ftmp", "/static/link.js", "/static/vendor",
                     "/static/", "/static/missing.js"):
            answer = await client.get(URL(path, encoded=True))
            out.append((path, answer.status, await answer.text()))
        answer = await client.get("/static/app.js")
        return out, answer.status
    out, fine = served(test)
    assert fine == 200
    for path, status, text in out:
        assert status == 404 and "not for the browser" not in text, path


# --- the real broker_call, against a stand-in on a unix socket ------------------------------------------

def test_broker_call_speaks_the_brokers_frames_and_tells_its_failures(monkeypatch, tmp_path):
    socket_path = str(tmp_path / "b.sock")
    received, behaviour = [], {"now": "answer"}

    async def handle(reader, writer):
        header = await reader.readexactly(4)
        body = await reader.readexactly(struct.unpack(">I", header)[0])
        received.append((header, body))
        request = json.loads(body)
        if behaviour["now"] == "close":
            writer.close()
            return
        if behaviour["now"] == "half":
            writer.write(struct.pack(">I", 100) + b'{"ok"')
            await writer.drain()
            writer.close()
            return
        reply = {"forbidden": broker.err("forbidden", "peer refused"),
                 "unauthorized": broker.err("unauthorized", "a valid session token is required"),
                 "answer": broker.ok({"echo": request})}[behaviour["now"]]
        data = json.dumps(reply).encode()
        writer.write(struct.pack(">I", len(data)) + data)
        await writer.drain()
        writer.close()

    async def run():
        unix = await asyncio.start_unix_server(handle, path=socket_path)
        outcomes = []
        async with unix:
            monkeypatch.setattr(server, "BROKER_SOCKET", socket_path)
            outcomes.append(await server.broker_call("status", {"name": "p1"}, token="t1", op_id="o1"))
            outcomes.append(await server.broker_call("list"))
            for now in ("unauthorized", "forbidden", "half", "close"):
                behaviour["now"] = now
                try:
                    outcomes.append(await server.broker_call("list", timeout=5))
                except (server.BrokerForbidden, server.BrokerUnavailable) as exc:
                    outcomes.append(type(exc).__name__)
        monkeypatch.setattr(server, "BROKER_SOCKET", str(tmp_path / "missing.sock"))
        try:
            await server.broker_call("list", timeout=5)
        except server.BrokerUnavailable:
            outcomes.append("missing: BrokerUnavailable")
        return outcomes
    outcomes = asyncio.run(run())
    assert outcomes[0] == broker.ok({"echo": {"verb": "status", "args": {"name": "p1"}, "token": "t1", "op_id": "o1"}})
    assert outcomes[1] == broker.ok({"echo": {"verb": "list", "args": {}}})       # no token, no op_id: not sent
    assert outcomes[2:] == [broker.err("unauthorized", "a valid session token is required"), "BrokerForbidden",
                            "BrokerUnavailable", "BrokerUnavailable", "missing: BrokerUnavailable"]
    for header, body in received:
        assert struct.unpack(">I", header)[0] == len(body)


# --- the certificate -------------------------------------------------------------------------------------

def read_cert(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def names_of(path):
    san = read_cert(path).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return sorted(san.get_values_for_type(x509.DNSName)), sorted(str(a) for a in san.get_values_for_type(x509.IPAddress))


def write_cert(cert_path, key_path, not_after, ips=("127.0.0.1",)):
    """A self-signed certificate made here, with the notAfter and addresses given."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(not_after - timedelta(days=900)).not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), *(x509.IPAddress(ipaddress.ip_address(a)) for a in ips)]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def test_the_certificate_for_loopback_names_exactly_localhost_and_the_two_loopbacks(tmp_path):
    server.generate_self_signed(tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem", "127.0.0.1")
    assert names_of(tmp_path / "tls" / "cert.pem") == (["localhost"], ["127.0.0.1", "::1"])
    assert stat.S_IMODE(os.stat(tmp_path / "tls" / "key.pem").st_mode) == 0o600
    after = read_cert(tmp_path / "tls" / "cert.pem").not_valid_after_utc
    assert timedelta(days=824) < after - datetime.now(UTC) <= timedelta(days=825)


def test_a_certificate_for_another_bind_names_it_too(tmp_path):
    server.generate_self_signed(tmp_path / "cert.pem", tmp_path / "key.pem", "192.0.2.7")
    assert names_of(tmp_path / "cert.pem") == (["localhost"], ["127.0.0.1", "192.0.2.7", "::1"])
    server.generate_self_signed(tmp_path / "cert.pem", tmp_path / "key.pem", "example.test")
    assert names_of(tmp_path / "cert.pem") == (["example.test", "localhost"], ["127.0.0.1", "::1"])


def test_ensure_tls_makes_a_certificate_when_there_is_none_and_keeps_a_good_one(tmp_path):
    cert, key = tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem"
    assert isinstance(server.ensure_tls(cert, key, "127.0.0.1"), ssl.SSLContext)
    made = (cert.read_bytes(), key.read_bytes())
    server.ensure_tls(cert, key, "127.0.0.1")
    assert (cert.read_bytes(), key.read_bytes()) == made
    write_cert(cert, key, datetime.now(UTC) + timedelta(days=31))      # a month and a day left: kept
    mine = (cert.read_bytes(), key.read_bytes())
    server.ensure_tls(cert, key, "127.0.0.1")
    assert (cert.read_bytes(), key.read_bytes()) == mine


@pytest.mark.parametrize("spoil", ["expired", "renew-due", "garbage", "no key", "other bind"])
def test_ensure_tls_makes_it_again_when_it_will_not_do(tmp_path, spoil):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    if spoil == "expired":
        write_cert(cert, key, datetime.now(UTC) - timedelta(minutes=1))
    elif spoil == "renew-due":
        write_cert(cert, key, datetime.now(UTC) + timedelta(days=29))
    elif spoil == "garbage":
        write_cert(cert, key, datetime.now(UTC) + timedelta(days=400))
        cert.write_bytes(b"not a certificate")
    elif spoil == "no key":
        write_cert(cert, key, datetime.now(UTC) + timedelta(days=400))
        key.unlink()
    else:
        write_cert(cert, key, datetime.now(UTC) + timedelta(days=400), ips=("127.0.0.1",))
    before = cert.read_bytes()
    server.ensure_tls(cert, key, "192.0.2.7" if spoil == "other bind" else "127.0.0.1")
    assert cert.read_bytes() != before
    assert services.cert_expiry_state(read_cert(cert).not_valid_after_utc, datetime.now(UTC)) == "ok"
    assert names_of(cert)[0] == ["localhost"] and ("192.0.2.7" in names_of(cert)[1]) == (spoil == "other bind")


def test_the_context_ensure_tls_returns_completes_a_handshake(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    context = server.ensure_tls(cert, key, "127.0.0.1")
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    served_side = {}

    def serve():
        conn, _ = listener.accept()
        try:
            with context.wrap_socket(conn, server_side=True) as tls:
                served_side["version"] = tls.version()
                tls.sendall(tls.recv(64).upper())
        except Exception as exc:  # noqa: BLE001 - said to the test, which fails on it
            served_side["error"] = repr(exc)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(cert))
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, \
            client_context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
        tls.sendall(b"hello")
        answer = tls.recv(64)
        peer = tls.getpeercert()
    thread.join(10)
    listener.close()
    assert served_side.get("error") is None and served_side["version"] in ("TLSv1.2", "TLSv1.3")
    assert answer == b"HELLO" and ("IP Address", "127.0.0.1") in peer["subjectAltName"]


# --- services --------------------------------------------------------------------------------------

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("not_after, now, state", [
    (None, NOW, "unknown"),
    (NOW - timedelta(days=1), NOW, "expired"),
    (NOW, NOW, "expired"),                                                         # the very second: expired
    (NOW + timedelta(seconds=1), NOW, "renew-due"),
    (NOW + services.CERT_RENEW_BEFORE - timedelta(seconds=1), NOW, "renew-due"),
    (NOW + services.CERT_RENEW_BEFORE, NOW, "ok"),                                 # thirty days exactly: not due
    (NOW + timedelta(days=800), NOW, "ok"),
    ((NOW + timedelta(days=31)).replace(tzinfo=None), NOW, "ok"),                  # naive: read as UTC
    ((NOW + timedelta(days=29)).replace(tzinfo=None), NOW, "renew-due"),
    (NOW.replace(tzinfo=None), NOW, "expired"),
    (NOW + timedelta(days=31), NOW.replace(tzinfo=None), "ok"),
    ((NOW + services.CERT_RENEW_BEFORE).replace(tzinfo=None), NOW.replace(tzinfo=None), "ok"),
    (NOW - timedelta(seconds=1), NOW.replace(tzinfo=None), "expired"),
])
def test_a_certificates_expiry_is_classed(not_after, now, state):
    assert services.cert_expiry_state(not_after, now) == state


@pytest.mark.parametrize("present, covers, expiry, reason", [
    (False, False, "expired", "no certificate yet"),
    (False, True, "ok", "no certificate yet"),
    (True, False, "expired", "certificate expired"),
    (True, False, "renew-due", "certificate expires within 30 days"),
    (True, False, "unknown", "certificate expiry unreadable"),
    (True, False, "ok", "certificate does not cover the bind address"),
    (True, True, "expired", "certificate expired"),
    (True, True, "ok", None),
])
def test_the_reason_to_make_the_certificate_again_comes_in_order(present, covers, expiry, reason):
    assert services.self_signed_regen_reason(present, covers, expiry) == reason


@pytest.mark.parametrize("subnet, words", [
    ("", "no listen subnet given"),
    ("not a subnet", "is not a network"),
    ("10.0.0.1/24", "is not a network"),                                            # host bits
    ("fd00::/64", "is not IPv4"),
    ("198.51.100.0/24", "198.51.100.0/24"),                                         # no address of this machine
])
def test_a_listen_subnet_that_will_not_do_is_a_value_error_naming_it(subnet, words):
    with pytest.raises(ValueError, match=words):
        services.listen_address(subnet)


def test_the_listen_address_is_this_machines_own_in_the_subnet():
    assert services.listen_address("127.0.0.0/8") == "127.0.0.1"


# --- main: where it listens, and when it does not ------------------------------------------------------

class FakeSite:
    """Stands in for web.TCPSite: records where it would listen, and once the sites are up
    stops the server the way the container's stop does."""
    made: ClassVar[list] = []

    def __init__(self, runner, host, port, ssl_context=None):
        FakeSite.made.append((host, port, ssl_context))

    async def start(self):
        loop = asyncio.get_running_loop()

        def stop():
            if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None):
                raise KeyboardInterrupt("main installed no handler for SIGTERM")    # main takes it as a stop
            os.kill(os.getpid(), signal.SIGTERM)
        if len(FakeSite.made) == 1:
            loop.call_soon(stop)


@pytest.fixture
def main_parts(monkeypatch, tmp_path):
    seen = {"tls": [], "subnets": [], "relays": []}
    FakeSite.made = []
    monkeypatch.setattr(server.web, "TCPSite", FakeSite)

    async def start_server(handler, host, port):
        seen["relays"].append((handler, host, port))
        return FakeRelay()
    monkeypatch.setattr(server.asyncio, "start_server", start_server)
    for name in ("ORIGIN_RANGE", "PASSTHROUGH_RANGE"):
        monkeypatch.setattr(server, name, getattr(server, name))     # main sets them
    monkeypatch.setattr(server, "ORIGIN_PORTS", {})
    monkeypatch.setattr(server, "PASSTHROUGH_CLAIMS", {})
    monkeypatch.setattr(server, "ORIGIN_PORTS_FILE", tmp_path / "origin-ports.json")
    monkeypatch.setattr(server, "PASSTHROUGH_FILE", tmp_path / "passthrough-claims.json")
    context = object()
    monkeypatch.setattr(server, "ensure_tls", lambda cert, key, bind: seen["tls"].append((cert, key, bind)) or context)
    seen["context"] = context
    return seen


@pytest.mark.parametrize("subnet", [None, "", "198.51.100.0/24", "fd00::/64"])
def test_main_without_a_listen_subnet_of_its_own_exits_1_saying_why_and_listens_on_nothing(monkeypatch, main_parts, caplog, subnet):
    if subnet is None:
        monkeypatch.delenv("CL_LISTEN_SUBNET", raising=False)
    else:
        monkeypatch.setenv("CL_LISTEN_SUBNET", subnet)
    with pytest.raises(SystemExit) as ended:
        server.main()
    assert ended.value.code == 1
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and errors[0].startswith("cannot choose the listen address: ")
    assert FakeSite.made == [] and main_parts["tls"] == []


class FakeRelay:
    def close(self):
        pass


def test_main_listens_on_its_address_in_the_subnet_on_the_page_and_both_ranges_and_nowhere_else(monkeypatch, main_parts):
    asked = []
    monkeypatch.setattr(server.services, "listen_address", lambda subnet: asked.append(subnet) or "192.0.2.10")
    monkeypatch.setenv("CL_LISTEN_SUBNET", "192.0.2.0/24")
    monkeypatch.setenv("CL_ORIGIN_PORTS", "12000-12002")
    monkeypatch.setenv("CL_PASSTHROUGH_PORTS", "12100-12101")
    server.main()
    assert asked == ["192.0.2.0/24"]
    context = main_parts["context"]
    assert FakeSite.made == [("192.0.2.10", server.WEB_PORT, context), *[("192.0.2.10", port, context) for port in (12000, 12001, 12002)]]
    assert main_parts["relays"] == [(server.passthrough_conn_handler, "192.0.2.10", port) for port in (12100, 12101)]
    assert main_parts["tls"] == [(server.TLS_DIR / "cert.pem", server.TLS_DIR / "key.pem", server.HOST_BIND)]
    assert (server.ORIGIN_RANGE, server.PASSTHROUGH_RANGE) == ((12000, 12002), (12100, 12101))


@pytest.mark.parametrize("key, value, words", [
    ("CL_ORIGIN_PORTS", "11100", "CL_ORIGIN_PORTS is '11100'"),
    ("CL_PASSTHROUGH_PORTS", "11160-11250", "CL_ORIGIN_PORTS and CL_PASSTHROUGH_PORTS overlap"),
])
def test_main_with_a_range_given_wrongly_exits_1_saying_why_and_listens_on_nothing(monkeypatch, main_parts, caplog, key, value, words):
    monkeypatch.setenv("CL_LISTEN_SUBNET", "192.0.2.0/24")
    monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as ended:
        server.main()
    assert ended.value.code == 1
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and errors[0].startswith("cannot choose the ports to listen on: ") and words in errors[0]
    assert FakeSite.made == [] and main_parts["relays"] == [] and main_parts["tls"] == []


def test_what_main_listens_on_is_never_a_wildcard_or_loopback_beside_the_subnets_address(monkeypatch):
    monkeypatch.setattr(server.services, "listen_address", lambda subnet: "192.0.2.10")
    sites = server.listen_on("192.0.2.0/24")
    kinds = [what for _, _, what in sites]
    assert (kinds.count("page"), kinds.count("origin"), kinds.count("passthrough")) == (1, 65, 100) == (1, len(kinds) - 101, 100)
    for host, _, _ in sites:
        address = ipaddress.ip_address(host)
        assert not address.is_unspecified and not address.is_loopback and address in ipaddress.ip_network("192.0.2.0/24")


# --- the terminal ----------------------------------------------------------------------------------------

SHELL_PASSWORD = "the-shell-password-0123456789abc"
HOST_FP = "SHA256:" + "h" * 43
TARGET = {"name": "p1", "host": "192.0.2.10", "port": 22, "username": "learner",
          "password": SHELL_PASSWORD, "fingerprint": HOST_FP}
OTHER_SESSION, OTHER_TOKEN = "another-cookie-value", "another-login-broker-token"


class FakeKey:
    def __init__(self, fp):
        self.fp = fp

    def get_fingerprint(self, hash_name="sha256"):
        return self.fp


class FakeProcess:
    """The remote command: says hello, echoes what it is sent, records its sizes."""

    def __init__(self, kwargs):
        self.kwargs, self.sizes, self.terminated = kwargs, [], False
        self.stdin = self.stdout = self
        self.output = asyncio.Queue()
        self.output.put_nowait(b"hello from the sandbox")

    def write(self, data):
        self.output.put_nowait(b"echo:" + data)

    async def read(self, n):
        return await self.output.get()

    def change_terminal_size(self, width, height):
        self.sizes.append((width, height))

    def terminate(self):
        self.terminated = True
        self.output.put_nowait(b"")


class FakeConnection:
    def __init__(self, ssh):
        self.ssh = ssh

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def close(self):
        pass

    async def create_process(self, **kwargs):
        process = FakeProcess(kwargs)
        self.ssh.processes.append(process)
        return process


class FakeSSH:
    """Stands in for asyncssh.connect: records its arguments, shows the validator a host key
    of fingerprint `key_fp`, and raises `raises` or hands back a connection."""

    def __init__(self):
        self.calls, self.processes, self.key_fp, self.raises = [], [], HOST_FP, None

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        validator = kwargs["client_factory"]()
        if not validator.validate_host_public_key(kwargs["host"], kwargs["host"], kwargs["port"], FakeKey(self.key_fp)):
            raise asyncssh.HostKeyNotVerifiable("Host key is not trusted")
        return FakeConnection(self)


@pytest.fixture
def ssh(monkeypatch, fake):
    stand_in = FakeSSH()
    monkeypatch.setattr(server.asyncssh, "connect", stand_in)
    fake.replies["attach"] = lambda args: broker.ok(dict(TARGET, name=args["name"]))
    return stand_in


TERMINAL = "/broker/project/p1/terminal"
CONNECT = {"type": "connect", "rows": 30, "cols": 100}


async def open_terminal(client, headers, first=CONNECT):
    ws = await client.ws_connect(TERMINAL, headers=headers)
    if isinstance(first, bytes):
        await ws.send_bytes(first)
    elif isinstance(first, str):
        await ws.send_str(first)
    else:
        await ws.send_json(first)
    return ws


async def until_closed(ws):
    """Everything the server sends until it closes: text as parsed JSON, binary as bytes."""
    said = []
    while True:
        msg = await ws.receive(timeout=5)
        if msg.type == aiohttp.WSMsgType.TEXT:
            said.append(json.loads(msg.data))
        elif msg.type == aiohttp.WSMsgType.BINARY:
            said.append(msg.data)
        else:
            return said


def test_a_terminal_runs_tmux_and_relays_both_ways_and_resizes(fake, ssh):
    async def test(client):
        ws = await open_terminal(client, with_session(client))
        said = [json.loads((await ws.receive(timeout=5)).data), (await ws.receive(timeout=5)).data]
        await ws.send_json({"type": "resize", "rows": 40, "cols": 120})
        await ws.send_bytes(b"ls\r")
        said.append((await ws.receive(timeout=5)).data)
        await ws.close()
        return said
    assert served(test) == [{"type": "connected"}, b"hello from the sandbox", b"echo:ls\r"]
    (process,) = ssh.processes
    assert process.kwargs == {"term_type": "xterm-256color", "term_size": (100, 30),
                              "command": "tmux new-session -A -s main", "encoding": None}
    assert process.sizes == [(120, 40)]
    assert fake.calls == [("attach", {"name": "p1"}, BROKER_TOKEN, None)]


@pytest.mark.parametrize("first, error", [(b"\x00", "expected JSON connect message"), ("{not json", "invalid JSON"),
                                          ({"type": "resize"}, "first message must be type=connect")])
def test_a_wrong_first_message_is_said_and_closes_without_asking_anyone(fake, ssh, first, error):
    async def test(client):
        return await until_closed(await open_terminal(client, with_session(client), first))
    assert served(test) == [{"type": "error", "msg": error}]
    assert fake.calls == [] and ssh.calls == []


@pytest.mark.parametrize("kind, session_kept", [("failed", True), ("unauthorized", False)])
def test_an_attach_the_broker_refuses_is_said_and_nothing_is_connected(fake, ssh, kind, session_kept):
    fake.replies["attach"] = broker.err(kind, "the sandbox of p1 is not running")

    async def test(client):
        return await until_closed(await open_terminal(client, with_session(client)))
    assert served(test) == [{"type": "error", "msg": "the sandbox of p1 is not running"}]
    assert ssh.calls == [] and (SESSION in server.BROKER_SESSIONS) is session_kept


@pytest.mark.parametrize("cookie", [None, "a-cookie-nobody-was-given"])
def test_no_terminal_without_a_login_the_socket_is_not_upgraded(fake, ssh, cookie):
    async def test(client):
        headers = own_origin(client)
        if cookie:
            headers["Cookie"] = f"{server.BROKER_COOKIE}={cookie}"
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            await client.ws_connect(TERMINAL, headers=headers)
        return refused.value.status
    assert served(test) == 401
    assert fake.calls == [] and ssh.calls == [] and server.TERMINALS == {}


@pytest.mark.parametrize("origin", [None, "https://192.0.2.1:{port}", "http://127.0.0.1:1"])
def test_a_terminal_from_another_origin_or_none_is_403_the_socket_is_not_upgraded(fake, ssh, origin):
    async def test(client):
        headers = with_session(client)
        del headers["Origin"]
        if origin is not None:
            headers["Origin"] = origin.format(port=client.port)
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            await client.ws_connect(TERMINAL, headers=headers)
        return refused.value.status
    assert served(test) == 403
    assert fake.calls == [] and ssh.calls == [] and server.TERMINALS == {}


def test_the_terminal_connects_exactly_where_attach_said_whatever_the_browser_names(fake, ssh):
    browser_says = {"type": "connect", "rows": 10 ** 6, "cols": 0, "host": "198.51.100.7", "port": 2222,
                    "username": "root", "password": "the-browsers-guess", "fingerprint": "SHA256:" + "b" * 43}

    async def test(client):
        ws = await open_terminal(client, with_session(client), browser_says)
        said = json.loads((await ws.receive(timeout=5)).data)
        await ws.close()
        return said
    assert served(test) == {"type": "connected"}
    (call,) = ssh.calls
    validator = call.pop("client_factory")()
    assert call == {"host": "192.0.2.10", "port": 22, "username": "learner", "password": SHELL_PASSWORD,
                    "known_hosts": ([], [], []), "client_keys": None, "connect_timeout": 10}
    assert validator.expected_fp == HOST_FP
    assert ssh.processes[0].kwargs["term_size"] == (1, 500)          # cols, rows: clamped


def test_the_validator_takes_only_the_expected_key_and_never_trusts_on_first_use():
    def takes(expected, actual):
        return server.HostKeyValidator(expected).validate_host_public_key("h", "192.0.2.10", 22, FakeKey(actual))
    assert takes(HOST_FP, HOST_FP) is True
    assert takes(HOST_FP, "SHA256:" + "o" * 43) is False
    assert takes(None, HOST_FP) is False and takes("", HOST_FP) is False


@pytest.mark.parametrize("fingerprint", [None, ""])
def test_no_connect_is_tried_when_attach_gives_no_fingerprint(fake, ssh, fingerprint):
    fake.replies["attach"] = broker.ok(dict(TARGET, fingerprint=fingerprint))

    async def test(client):
        return await until_closed(await open_terminal(client, with_session(client)))
    assert served(test) == [{"type": "error", "msg": "the sandbox's host key is not known"}]
    assert ssh.calls == []


def test_asyncssh_itself_refuses_a_host_key_other_than_the_pinned_one():
    """Through a real SSH handshake on loopback: the options ssh_connect passes make asyncssh
    ask the validator (with `known_hosts=None` it would not, and would take any key)."""
    async def run():
        host_key, other_key = asyncssh.generate_private_key("ssh-ed25519"), asyncssh.generate_private_key("ssh-ed25519")

        class Sandbox(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                return password == SHELL_PASSWORD

        acceptor = await asyncssh.create_server(Sandbox, "127.0.0.1", 0, server_host_keys=[host_key])
        target = dict(TARGET, host="127.0.0.1", port=acceptor.sockets[0].getsockname()[1])
        outcomes = []
        try:
            for fp in (host_key.get_fingerprint("sha256"), other_key.get_fingerprint("sha256"), None):
                try:
                    conn = await server.ssh_connect(dict(target, fingerprint=fp))
                except asyncssh.HostKeyNotVerifiable:
                    outcomes.append("refused")
                else:
                    conn.close()
                    await conn.wait_closed()
                    outcomes.append("connected")
        finally:
            acceptor.close()
            await acceptor.wait_closed()
        return outcomes
    assert asyncio.run(run()) == ["connected", "refused", "refused"]


@pytest.mark.parametrize("raised, answer", [
    (None, {"type": "connected"}),
    (asyncssh.HostKeyNotVerifiable("Host key is not trusted"), {"type": "fingerprint_mismatch"}),
    (asyncssh.PermissionDenied("Permission denied"), {"type": "auth_failed"}),
    (OSError(f"cannot reach learner:{SHELL_PASSWORD}@192.0.2.10"), {"type": "error", "msg": "connect failed"}),
])
def test_the_shell_password_reaches_neither_the_browser_nor_the_log(fake, ssh, caplog, raised, answer):
    caplog.set_level(logging.DEBUG)
    ssh.raises = raised

    async def test(client):
        ws = await open_terminal(client, with_session(client))
        said = [json.loads((await ws.receive(timeout=5)).data)]
        await ws.close()
        return said
    said = served(test)
    assert said == [answer]
    assert SHELL_PASSWORD not in json.dumps(said) and SHELL_PASSWORD not in caplog.text
    if isinstance(raised, OSError):
        assert "cannot reach learner:<password>@192.0.2.10" in caplog.text     # the detail is logged, redacted


# --- the project session ---------------------------------------------------------------------------------

def test_a_project_session_is_a_strict_cookie_for_the_project_bound_to_the_login(fake):
    async def test(client):
        answer = await client.post("/broker/project/p1/session", headers=with_session(client, expires_in=3600))
        return answer.status, await answer.json(), answer.headers.getall("Set-Cookie")
    status, said, (set_cookie,) = served(test)
    assert (status, said) == (200, {"ok": True})
    assert fake.calls == [("status", {"name": "p1"}, BROKER_TOKEN, None)]
    name, _, token = set_cookie.split(";")[0].partition("=")
    assert name == services.session_cookie_name(server.WEB_PORT, "p1")
    attributes = {part.strip().split("=")[0].lower(): part.strip() for part in set_cookie.split(";")[1:]}
    assert {"httponly", "secure"} <= set(attributes)
    assert attributes["samesite"] == "SameSite=Strict" and attributes["path"] == "Path=/"
    assert 3590 < int(attributes["max-age"].split("=")[1]) <= 3600                   # no longer than the login
    assert server.SESSIONS[token]["login"] == SESSION and server._session_valid(token, "p1")


def test_a_project_session_lives_at_most_eight_hours_and_none_for_a_project_the_broker_refuses(fake):
    async def test(client):
        answer = await client.post("/broker/project/p1/session", headers=with_session(client, expires_in=86400))
        fake.replies["status"] = broker.err("failed", "no project named p2")
        refused = await client.post("/broker/project/p2/session", headers=with_session(client, expires_in=86400))
        return answer.cookies[services.session_cookie_name(server.WEB_PORT, "p1")]["max-age"], refused.status, \
            await refused.json(), refused.headers.getall("Set-Cookie", [])
    max_age, status, said, cookies = served(test)
    assert max_age == str(8 * 3600)
    assert (status, said, cookies) == (200, {"ok": False, "error": {"kind": "failed", "message": "no project named p2"}}, [])
    assert [s["project"] for s in server.SESSIONS.values()] == ["p1"]


def test_a_project_session_is_refused_for_another_project_and_once_expired(fake):
    server.SESSIONS["t1"] = {"project": "p1", "expires": time.time() + 60, "login": SESSION}
    server.SESSIONS["t2"] = {"project": "p1", "expires": time.time() - 1, "login": SESSION}
    assert server._session_valid("t1", "p1") is True
    assert server._session_valid("t1", "p2") is False
    assert server._session_valid("t2", "p1") is False and "t2" not in server.SESSIONS
    assert server._session_valid("unknown", "p1") is False


# --- the end of a login ----------------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["logout", "the broker no longer knows the token"])
def test_the_end_of_a_login_closes_its_terminals_and_drops_its_project_sessions_and_no_others(fake, ssh, how):
    async def test(client):
        mine = with_session(client)
        server.BROKER_SESSIONS[OTHER_SESSION] = {"broker_token": OTHER_TOKEN, "expires": time.time() + 600}
        theirs = {**own_origin(client), "Cookie": f"{server.BROKER_COOKIE}={OTHER_SESSION}"}
        terminals = []
        for headers in (mine, theirs):
            assert (await client.post("/broker/project/p1/session", headers=headers)).status == 200
            ws = await open_terminal(client, headers)
            assert json.loads((await ws.receive(timeout=5)).data) == {"type": "connected"}
            assert (await ws.receive(timeout=5)).data == b"hello from the sandbox"
            terminals.append(ws)
        if how == "logout":
            assert (await client.post("/broker/logout", headers=mine)).status == 200
        else:
            fake.replies["list"] = broker.err("unauthorized", "a valid session token is required; call login")
            assert (await client.get("/broker/projects", headers=mine)).status == 401
        closed = (await terminals[0].receive(timeout=5)).type
        await terminals[1].send_bytes(b"still here")
        echoed = (await terminals[1].receive(timeout=5)).data
        await terminals[1].close()
        return closed, echoed
    closed, echoed = served(test)
    assert closed == aiohttp.WSMsgType.CLOSE and echoed == b"echo:still here"
    assert list(server.BROKER_SESSIONS) == [OTHER_SESSION]
    assert [s["login"] for s in server.SESSIONS.values()] == [OTHER_SESSION]


# --- where a sandbox is ------------------------------------------------------------------------------------

def test_where_a_sandbox_is_comes_from_the_open_status_remembered_and_asked_again_after_a_failed_connect(fake):
    said = {"p1": broker.ok({"state": "running", "address": "192.0.2.10"}), "p2": broker.ok({"state": "stopped", "address": ""}),
            "p3": broker.err("failed", "There is no project named p3.")}
    fake.replies["status"] = lambda args: said[args["name"]]
    fake.replies["list"] = broker.ok([{"name": "p1"}, {"name": "p2"}])
    server.ORIGIN_PORTS.update({11100: {"project": "p3", "port": 3000}, 11101: {"project": "p1", "port": 3000}})
    server.PASSTHROUGH_CLAIMS.update({11200: {"project": "p3", "port": 9092}})

    def age(project, seconds):
        at, address = server.SANDBOX_ADDRESSES[project]
        server.SANDBOX_ADDRESSES[project] = (at - seconds, address)

    async def test():
        seen = [await server.sandbox_address("p1"), await server.sandbox_address("p1"),
                await server.sandbox_address("p1", failed="192.0.2.10")]      # just asked: not again
        calls = len(fake.calls)
        age("p1", 2)
        said["p1"] = broker.ok({"state": "running", "address": "192.0.2.11"})
        seen.append(await server.sandbox_address("p1", failed="192.0.2.10"))   # asked again
        seen.append(await server.sandbox_address("p1"))
        age("p1", 10)
        seen.append(await server.sandbox_address("p1"))                         # remembered no longer
        return seen, calls, await server.sandbox_address("p2"), await server.sandbox_address("p3")
    seen, calls, stopped, gone = asyncio.run(test())
    assert seen == ["192.0.2.10"] * 3 + ["192.0.2.11"] * 3 and calls == 1
    assert fake.calls[:3] == [("status", {"name": "p1"}, None, None)] * 3            # open: no token
    assert stopped is None and gone is None and fake.verbs()[-2:] == ["status", "list"]
    # A project the broker no longer knows loses its origin ports and claims, in the files too.
    assert server.ORIGIN_PORTS == {11101: {"project": "p1", "port": 3000}} and server.PASSTHROUGH_CLAIMS == {}
    assert json.loads(server.ORIGIN_PORTS_FILE.read_text()) == {"11101": {"project": "p1", "port": 3000}}


# --- the service list, opening an app, the ports named by hand ------------------------------------------

NAMED = [{"port": 8000, "label": "notebook"}, {"port": 9092, "label": "kafka", "passthrough": True, "host_port": 11200}]
DETECTED = [{"port": 3000, "inodes": [201], "label": "3000"}, {"port": 8000, "inodes": [202], "label": "8000"},
            {"port": 9092, "inodes": [203], "label": "9092"}]


def ports_known(fake, named=NAMED, detected=DETECTED):
    fake.replies["port_list"] = lambda args: broker.ok({"project": args["name"], "ports": named})
    fake.replies["port_detect"] = lambda args: broker.ok({"project": args["name"], "running": True, "ports": detected})
    fake.replies["status"] = broker.ok({"editor": True})


def test_the_service_list_holds_the_editor_the_files_server_the_named_and_detected_apps_and_raw_tcp(fake):
    # The editor and the files server listen in the sandbox; neither is a detected app.
    ports_known(fake, detected=[*DETECTED, {"port": 8443, "inodes": [204], "label": "8443"}, {"port": 7070, "inodes": [205], "label": "7070"}])
    server.ORIGIN_PORTS[11104] = {"project": "p1", "port": 3000}

    async def test(client):
        running = await (await client.get("/broker/project/p1/services", headers=with_session(client))).json()
        fake.replies["port_detect"] = broker.err("failed", "The sandbox of p1 is not running; start the project first.")
        stopped = await (await client.get("/broker/project/p1/services", headers=with_session(client))).json()
        return running, stopped
    running, stopped = served(test)
    web_row = {"kind": "http", "always_on": False, "renderer": "iframe", "upstream_path": "/"}
    raw = {"label": "kafka", "kind": "http", "always_on": False, "renderer": "launcher", "default_port": 11200,
           "passthrough_port": 11200, "sandbox_port": 9092}
    assert running == {"ok": True, "services": {
        "editor": {**web_row, "label": "Editor", "default_port": 8443, "always_on": True},
        "files": {**web_row, "label": "Files", "default_port": 7070, "always_on": True},
        "port-8000": {**web_row, "label": "notebook", "default_port": 8000},
        "port-3000": {**web_row, "label": "3000", "default_port": 3000, "detected": True, "origin_port": 11104},
        "pass-11200": raw}}
    assert list(running["services"]) == ["editor", "files", "port-8000", "port-3000", "pass-11200"]
    assert stopped == {"ok": True, "services": {"port-8000": {**web_row, "label": "notebook", "default_port": 8000}, "pass-11200": raw}}
    assert fake.calls[:2] == [("port_list", {"name": "p1"}, BROKER_TOKEN, None), ("port_detect", {"name": "p1"}, BROKER_TOKEN, None)]
    assert server.PASSTHROUGH_CLAIMS == {11200: {"project": "p1", "port": 9092}}


def test_an_app_keeps_its_origin_port_two_apps_get_two_and_the_table_survives_a_reload(fake, monkeypatch):
    ports_known(fake)

    async def test(client):
        async def open_app(project, port):
            answer = await client.post(f"/broker/project/{project}/origin", json={"port": port}, headers=with_session(client))
            return await answer.json()
        said = [await open_app("p1", 3000), await open_app("p1", 3000), await open_app("p1", 8000), await open_app("p2", 7070),
                await open_app("p1", 4000)]
        monkeypatch.setattr(server, "ORIGIN_RANGE", (11100, 11102))
        said.append(await open_app("p2", 8000))
        return said
    said = served(test)
    assert said[:4] == [{"ok": True, "origin_port": port} for port in (11100, 11100, 11101, 11102)]
    assert said[4]["error"]["kind"] == "validation" and said[5]["error"] == {"kind": "failed", "message": "every origin port (11100-11102) is taken"}
    table = {11100: {"project": "p1", "port": 3000}, 11101: {"project": "p1", "port": 8000}, 11102: {"project": "p2", "port": 7070}}
    assert server.ORIGIN_PORTS == table
    server.ORIGIN_PORTS.clear()
    server._origin_table_load()
    assert server.ORIGIN_PORTS == table
    # The named port and the files server need no detection.
    assert [verb for verb in fake.verbs()] == ["port_list", "port_detect", "port_list", "port_detect", "port_list", "port_list",
                                               "port_list", "port_detect", "port_list"]


def test_the_editors_port_opens_like_the_files_servers_without_detection(fake):
    ports_known(fake, detected=[])

    async def test(client):
        answer = await client.post("/broker/project/p1/origin", json={"port": 8443}, headers=with_session(client))
        return await answer.json()
    assert served(test) == {"ok": True, "origin_port": 11100}
    assert fake.verbs() == ["port_list", "status"] and server.ORIGIN_PORTS == {11100: {"project": "p1", "port": 8443}}


def test_the_port_routes_relay_their_verbs_with_their_fields_only_and_the_claims_follow(fake):
    claim = {"port": 9092, "label": "kafka", "passthrough": True, "host_port": 11200}
    fake.replies["port_add"] = lambda args: broker.ok({"project": "p1", "port": 11200, "label": "kafka", "ports": [claim]})
    fake.replies["port_remove"] = lambda args: broker.ok({"project": "p1", "port": 11200, "ports": []})
    fake.replies["port_list"] = broker.ok({"project": "p1", "ports": [claim]})
    server.PASSTHROUGH_CLAIMS[11200] = {"project": "p0", "port": 22}      # stale: the broker gave 11200 to p1 since

    async def test(client):
        added = await client.post("/broker/project/p1/ports", headers=with_session(client),
                                  json={**claim, "name": "p2", "box": "b", "count": 3})
        after_add = dict(server.PASSTHROUGH_CLAIMS)
        server.PASSTHROUGH_CLAIMS.clear()
        server._claims_load()
        loaded = dict(server.PASSTHROUGH_CLAIMS)
        listed = await client.get("/broker/project/p1/ports", headers=with_session(client))
        removed = await client.post("/broker/project/p1/ports/remove", headers=with_session(client),
                                    json={"port": 11200, "passthrough": True, "label": "x"})
        return [await answer.json() for answer in (added, listed, removed)], after_add, loaded
    said, after_add, loaded = served(test)
    assert said[1] == {"ok": True, "result": {"project": "p1", "ports": [claim]}}
    assert fake.calls == [
        ("port_add", {"port": 9092, "label": "kafka", "passthrough": True, "host_port": 11200, "name": "p1"}, BROKER_TOKEN, None),
        ("port_list", {"name": "p1"}, BROKER_TOKEN, None),
        ("port_remove", {"port": 11200, "passthrough": True, "name": "p1"}, BROKER_TOKEN, None)]
    assert after_add == loaded == {11200: {"project": "p1", "port": 9092}}
    assert server.PASSTHROUGH_CLAIMS == {} and json.loads(server.PASSTHROUGH_FILE.read_text()) == {}


# --- the origin proxy ----------------------------------------------------------------------------------------

class Upstream:
    """Stands in for a web app in a sandbox: records what reaches it, answers with a header
    of its own and cookies (its own, and two that try to be clanker-learn's)."""

    def __init__(self, name="app"):
        self.name = name
        self.seen = []

    def app(self):
        app = web.Application()
        app.router.add_get("/ws", self.ws)
        app.router.add_route("*", "/{tail:.*}", self.any)
        return app

    async def any(self, request):
        self.seen.append({"method": request.method, "path": request.raw_path, "body": await request.read(),
                          "cookie": request.headers.get("Cookie"), "host": request.headers.get("Host")})
        answer = web.Response(text=f"{self.name}: {request.method} {request.raw_path}",
                              headers={"X-App": self.name, "X-Frame-Options": "DENY"})
        answer.set_cookie("app_cookie", "a")
        answer.set_cookie(server.BROKER_COOKIE, "forged", path="/broker")
        answer.set_cookie(services.session_cookie_name(server.WEB_PORT, "p2"), "forged")
        return answer

    async def ws(self, request):
        self.seen.append({"method": "WS", "cookie": request.headers.get("Cookie")})
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            await ws.send_str(f"echo:{msg.data}")
        return ws


@pytest.fixture
def at_loopback(monkeypatch):
    """Every sandbox is at loopback; records which projects were asked for."""
    asked = []

    async def sandbox_address(project, failed=None):
        asked.append((project, failed))
        return "127.0.0.1"
    monkeypatch.setattr(server, "sandbox_address", sandbox_address)
    return asked


def project_cookie(project="p1", token="tok-p1", under=None):
    """A live session for `project` and the Cookie header part that carries it (under the
    cookie name of the project `under`, its own by default)."""
    server.SESSIONS[token] = {"project": project, "expires": time.time() + 600, "login": SESSION}
    return f"{services.session_cookie_name(server.WEB_PORT, under or project)}={token}"


def through_proxy(test):
    """Run `test(client, app, decoy)` with an origin port (the client's) given to p1's app; the
    decoy is another server on loopback that nothing should reach."""
    async def run():
        app, decoy = Upstream("app"), Upstream("decoy")
        async with TestServer(app.app()) as app_server, TestServer(decoy.app()) as decoy_server, \
                TestClient(TestServer(server.build_origin_app()), cookie_jar=aiohttp.DummyCookieJar()) as client:
            server.ORIGIN_PORTS[client.port] = {"project": "p1", "port": app_server.port}
            decoy.port = decoy_server.port
            return await test(client, app, decoy)
    return asyncio.run(run())


def test_a_get_and_a_post_with_a_body_go_through_the_proxy(fake, at_loopback):
    async def test(client, app, decoy):
        cookie = {"Cookie": project_cookie()}
        got = await client.get("/a/b?x=1&y=a%26b", headers=cookie)
        posted = await client.post("/form", data=b"field=value" * 1000, headers=cookie)
        return [(answer.status, await answer.text(), answer.headers.get("X-App"), answer.headers.get("X-Frame-Options"))
                for answer in (got, posted)]
    said = through_proxy(test)
    assert said == [(200, "app: GET /a/b?x=1&y=a%26b", "app", None), (200, "app: POST /form", "app", None)]
    assert at_loopback == [("p1", None), ("p1", None)]


def test_a_websocket_echoes_through_the_proxy_with_the_apps_cookie_only(fake, at_loopback):
    async def test(client, app, decoy):
        headers = {"Cookie": f"{project_cookie()}; app_cookie=a; {server.BROKER_COOKIE}={SESSION}"}
        async with client.ws_connect("/ws", headers=headers) as ws:
            await ws.send_str("hello")
            echoed = (await ws.receive(timeout=5)).data
        return echoed, app.seen
    echoed, seen = through_proxy(test)
    assert echoed == "echo:hello" and seen == [{"method": "WS", "cookie": "app_cookie=a"}]


def test_a_port_without_an_app_is_404_and_an_app_that_does_not_answer_is_502_after_asking_again(fake, at_loopback):
    async def test(client, app, decoy):
        cookie = {"Cookie": project_cookie()}
        with socket.socket() as probe:      # a port nothing listens on
            probe.bind(("127.0.0.1", 0))
            server.ORIGIN_PORTS[client.port]["port"] = probe.getsockname()[1]
        down = await client.get("/", headers=cookie)
        del server.ORIGIN_PORTS[client.port]
        nothing = await client.get("/", headers=cookie)
        return (down.status, await down.text()), (nothing.status, await nothing.text())
    down, nothing = through_proxy(test)
    assert down[0] == 502 and "does not answer" in down[1] and "127.0.0.1" not in down[1]
    assert at_loopback == [("p1", None), ("p1", "127.0.0.1")]      # the address was asked again once
    assert nothing == (404, "no app is assigned to this port")


def test_security_no_valid_session_cookie_is_401_and_nothing_reaches_the_app(fake, at_loopback):
    server.SESSIONS["expired"] = {"project": "p1", "expires": time.time() - 1, "login": SESSION}
    server.BROKER_SESSIONS[SESSION] = {"broker_token": BROKER_TOKEN, "expires": time.time() + 600}

    async def test(client, app, decoy):
        name = services.session_cookie_name(server.WEB_PORT, "p1")
        said = []
        for cookie in (None, f"{name}=nobody-was-given-this", f"{name}=expired", f"{server.BROKER_COOKIE}={SESSION}"):
            headers = {"Cookie": cookie} if cookie else {}
            answer = await client.post("/x", data=b"body", headers=headers)
            said.append((answer.status, await answer.text()))
            try:
                await client.ws_connect("/ws", headers=headers)
                said.append("upgraded")
            except aiohttp.WSServerHandshakeError as refused:
                said.append(refused.status)
        return said, app.seen
    said, seen = through_proxy(test)
    assert said == [(401, "session required"), 401] * 4
    assert seen == [] and at_loopback == []      # the app's address was not even asked for


def test_security_another_projects_session_does_not_open_an_app(fake, at_loopback):
    async def test(client, app, decoy):
        statuses = []
        for cookie in (project_cookie("p2", "tok-p2"),                  # p2's own session, under p2's name
                       project_cookie("p2", "tok-p2", under="p1")):    # p2's session under p1's name
            statuses.append((await client.get("/", headers={"Cookie": cookie})).status)
        return statuses, app.seen
    statuses, seen = through_proxy(test)
    assert statuses == [401, 401] and seen == [] and at_loopback == []


async def raw_request(port, head):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode())
    await writer.drain()
    answer = await reader.read()
    writer.close()
    return answer.split(b"\r\n", 1)[0].decode()


def test_security_the_upstream_is_the_helpers_address_and_the_tables_port_whatever_the_request_says(fake, at_loopback):
    async def test(client, app, decoy):
        other = f"127.0.0.1:{decoy.port}"
        cookie = project_cookie()
        heads = [f"GET / HTTP/1.1\r\nHost: {other}\r\n",
                 f"GET http://{other}/absolute?q=1 HTTP/1.1\r\nHost: {other}\r\n",
                 f"GET //{other}/twice HTTP/1.1\r\nHost: {other}\r\n",
                 f"GET /@{other}/at HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\n",
                 f"GET /q?url=http://{other}/&host={other}&port={decoy.port} HTTP/1.1\r\nHost: {other}\r\n"]
        statuses = [await raw_request(client.port, f"{head}Cookie: {cookie}\r\nConnection: close\r\n\r\n") for head in heads]
        return statuses, other, app.seen, decoy.seen
    statuses, other, seen, decoy_seen = through_proxy(test)
    assert decoy_seen == [] and at_loopback == [("p1", None)] * 5
    assert statuses == ["HTTP/1.1 200 OK"] * 5
    assert [one["path"] for one in seen] == ["/", "/absolute?q=1", f"//{other}/twice", f"/@{other}/at", f"/q?url=http://{other}/&host={other}&port={other.split(':')[1]}"]


def test_security_clanker_learns_own_cookies_go_neither_up_nor_down(fake, at_loopback):
    async def test(client, app, decoy):
        own = f"{project_cookie()}; {server.BROKER_COOKIE}={SESSION}; cl_session_9999_p3=x"
        answer = await client.get("/", headers={"Cookie": f"{own}; app_cookie=a; other=b"})
        return answer.headers.getall("Set-Cookie"), app.seen
    set_cookies, seen = through_proxy(test)
    assert [one["cookie"] for one in seen] == ["app_cookie=a; other=b"]
    assert [value.split("=")[0] for value in set_cookies] == ["app_cookie"]


# --- the pass-through relay -------------------------------------------------------------------------------

async def tcp_server(greeting, connections):
    """A TCP app on loopback: greets, then echoes; counts its connections."""
    async def serve(reader, writer):
        connections.append(greeting)
        writer.write(greeting)
        await writer.drain()
        while data := await reader.read(1024):
            writer.write(b"echo:" + data)
            await writer.drain()
        writer.close()
    return await asyncio.start_server(serve, "127.0.0.1", 0)


async def relay_listener():
    return await asyncio.start_server(server.passthrough_conn_handler, "127.0.0.1", 0)


def port_of(listener):
    return listener.sockets[0].getsockname()[1]


def test_the_relay_pumps_both_ways(fake, at_loopback):
    async def test():
        connections = []
        app, relay = await tcp_server(b"hello\n", connections), await relay_listener()
        server.PASSTHROUGH_CLAIMS[port_of(relay)] = {"project": "p1", "port": port_of(app)}
        reader, writer = await asyncio.open_connection("127.0.0.1", port_of(relay))
        said = [await reader.readexactly(6)]
        for data in (b"one", b"two"):
            writer.write(data)
            said.append(await reader.readexactly(len(data) + 5))
        writer.close()
        app.close()
        relay.close()
        return said
    assert asyncio.run(test()) == [b"hello\n", b"echo:one", b"echo:two"]
    assert at_loopback == [("p1", None)]


def test_security_an_unclaimed_port_relays_nothing_and_a_claimed_one_reaches_its_own_projects_port_only(fake, at_loopback):
    async def test():
        connections = []
        app1, app2 = await tcp_server(b"p1's app\n", connections), await tcp_server(b"p2's app\n", connections)
        claimed, unclaimed = await relay_listener(), await relay_listener()
        server.PASSTHROUGH_CLAIMS.update({port_of(claimed): {"project": "p2", "port": port_of(app2)}, 1: {"project": "p1", "port": port_of(app1)}})
        reader, writer = await asyncio.open_connection("127.0.0.1", port_of(unclaimed))
        nothing = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        asked_unclaimed = list(at_loopback)
        reader, writer = await asyncio.open_connection("127.0.0.1", port_of(claimed))
        greeting = await reader.readexactly(9)
        writer.close()
        for listener in (app1, app2, claimed, unclaimed):
            listener.close()
        return nothing, asked_unclaimed, greeting, connections
    nothing, asked_unclaimed, greeting, connections = asyncio.run(test())
    assert nothing == b"" and asked_unclaimed == []
    assert greeting == b"p2's app\n" and connections == [b"p2's app\n"] and at_loopback == [("p2", None)]
