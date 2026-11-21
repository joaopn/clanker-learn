"""The web server's unit tests: its rules, its routes through a real aiohttp server with a
stand-in for the broker, its certificate, and where it listens. No container is started."""
import asyncio
import ipaddress
import json
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
import broker
import core
import pytest
import server
import services
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

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
    monkeypatch.setattr(server, "LOGIN_LIMITER", server.LoginLimiter(now=stand_in.clock))
    monkeypatch.setattr(server, "SETUP_LIMITER", server.LoginLimiter(now=stand_in.clock))
    (tmp_path / "oplogs").mkdir()
    monkeypatch.setattr(server, "BROKER_OPLOG_DIR", str(tmp_path / "oplogs"))
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
    for verb in ("create", "limits"):
        assert set(server.BROKER_FIELDS[verb]) | {"name"} == broker.FIELDS[verb]
    assert set(server.BROKER_FIELDS) == {"create", "limits"}
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


def test_the_page_is_html_that_runs_nothing_and_is_not_kept(fake):
    async def test(client):
        answer = await client.get("/")
        return answer.status, answer.headers, await answer.text()
    status, headers, text = served(test)
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    policy = [part.strip() for part in headers["Content-Security-Policy"].split(";")]
    assert "default-src 'none'" in policy and not any(part.startswith("script-src") for part in policy)
    assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
    assert "<script" not in text.lower() and fake.calls == []


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
def main_parts(monkeypatch):
    seen = {"tls": [], "subnets": []}
    FakeSite.made = []
    monkeypatch.setattr(server.web, "TCPSite", FakeSite)
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


def test_main_listens_on_its_address_in_the_subnet_and_nowhere_else(monkeypatch, main_parts):
    asked = []
    monkeypatch.setattr(server.services, "listen_address", lambda subnet: asked.append(subnet) or "192.0.2.10")
    monkeypatch.setenv("CL_LISTEN_SUBNET", "192.0.2.0/24")
    server.main()
    assert asked == ["192.0.2.0/24"]
    assert FakeSite.made == [("192.0.2.10", server.WEB_PORT, main_parts["context"])]
    assert main_parts["tls"] == [(server.TLS_DIR / "cert.pem", server.TLS_DIR / "key.pem", server.HOST_BIND)]
    assert server.listen_on("192.0.2.0/24") == [("192.0.2.10", server.WEB_PORT)]


def test_what_main_listens_on_is_never_a_wildcard_or_loopback_beside_the_subnets_address(monkeypatch):
    monkeypatch.setattr(server.services, "listen_address", lambda subnet: "192.0.2.10")
    sites = server.listen_on("192.0.2.0/24")
    assert len(sites) == 1
    for host, _ in sites:
        address = ipaddress.ip_address(host)
        assert not address.is_unspecified and not address.is_loopback and address in ipaddress.ip_network("192.0.2.0/24")
