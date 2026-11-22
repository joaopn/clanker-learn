"""The broker's gates and its bookkeeping of operations, without a daemon or a socket."""
import base64
import contextlib
import json
import os
import pathlib
import pty
import subprocess
import sys
import threading

import broker
import broker_auth
import core
import pytest

PROOF = "the-right-proof"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A broker with a stand-in for the core, its folders in a temp folder, a clock of its own,
    and records of what was audited and what would have been started."""
    monkeypatch.setattr(broker, "OPLOGS", str(tmp_path / "run" / "oplogs"))
    monkeypatch.setattr(broker, "MARKERS", str(tmp_path / "markers"))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.setattr(core, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(broker, "proof_is_right", lambda proof: proof == PROOF)
    monkeypatch.setattr(core, "list_projects", lambda: [{"name": "kafka", "state": "running"}])
    monkeypatch.setattr(core, "status", lambda name: {"name": name, "state": "running"})
    limits_set = []
    monkeypatch.setattr(core, "set_limits", lambda name, **changed: limits_set.append((name, changed, set(core.HELD))) or {"name": name, "notes": []})
    ports_asked = []   # (what, the arguments, the locks held then)
    for what in ("attach", "port_detect", "port_add", "port_remove", "port_list"):
        monkeypatch.setattr(core, what, lambda *given, what=what: ports_asked.append((what, given, set(core.HELD))) or {"asked": what})

    class World:
        clock = Clock()
        tokens = broker_auth.TokenStore(now=clock)
        audited, spawned = [], []

        def call(self, verb, args=None, token=None, op_id=None, **more):
            return broker.dispatch(verb, {} if args is None else args, token, self.tokens, op_id=op_id,
                                   audit=lambda principal, v, outcome: self.audited.append((principal, v, outcome)),
                                   spawn=self.spawned.append, **more)

        def login(self):
            return self.call("login", {"proof": PROOF})["result"]["token"]

    one = World()
    one.limits_set = limits_set
    one.ports_asked = ports_asked
    return one


def kind(reply):
    return "ok" if reply["ok"] else reply["error"]["kind"]


def read(path):
    with open(path) as fh:
        return fh.read()


# --- the envelope and the closed table -------------------------------------------------------

def test_a_verb_that_is_not_in_the_table_is_unknown_and_nothing_passes_through(world):
    for verb in ("docker", "exec", "update", "passwd", "", None, 7, "__run-op"):
        assert kind(world.call(verb)) == "unknown_verb"
    assert sorted(broker.FIELDS) == ["agent_pull", "agent_refresh", "agent_refresh_check", "agents_add", "agents_model", "agents_update", "attach",
                                     "backup", "backup_active", "backup_fetch", "backup_list", "backup_rm", "build_active", "build_alive", "create", "destroy", "editor_pull", "editor_refresh", "editor_refresh_check",
                                     "first_password", "lifecycle_active", "limits", "list", "mode", "models", "op", "pin_reset", "port_add", "port_detect",
                                     "port_list", "port_remove", "recreate", "setup_needed", "software_status", "start", "status", "stop"]


def test_arguments_that_are_not_an_object_are_a_bad_request(world):
    token = world.login()
    for verb in ("list", "create", "login", "logout"):
        assert kind(broker.dispatch(verb, ["kafka"], token, world.tokens)) == "bad_request"


def test_the_open_reads_need_no_token_and_answer_from_the_core(world):
    assert world.call("list") == {"ok": True, "result": [{"name": "kafka", "state": "running"}]}
    assert world.call("status", {"name": "kafka"})["result"] == {"name": "kafka", "state": "running"}
    assert world.audited == []   # reads leave no audit line


# --- login, tokens, logout -------------------------------------------------------------------

def test_login_takes_the_right_proof_and_nothing_else(world):
    for proof in ("wrong", "", None, 7, ["the-right-proof"]):
        reply = world.call("login", {"proof": proof})
        assert reply == {"ok": False, "error": {"kind": "auth", "message": "invalid credentials"}}
    reply = world.call("login", {"proof": PROOF})
    assert reply["ok"] and reply["result"]["principal"] == "operator" and reply["result"]["expires_at"] == 1000.0 + 8 * 3600
    assert world.audited[-2:] == [(None, "login", "auth_fail"), ("operator", "login", "ok")]


def test_with_no_password_set_login_fails_like_a_wrong_one(monkeypatch, tmp_path):
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "none.json")   # the real check, and no file
    reply = broker.dispatch("login", {"proof": PROOF}, None, broker_auth.TokenStore())
    assert reply == {"ok": False, "error": {"kind": "auth", "message": "invalid credentials"}}


@pytest.mark.parametrize("verb, args", [("create", {"name": "kafka"}), ("start", {"name": "kafka"}), ("stop", {"name": "kafka"}),
                                        ("recreate", {"name": "kafka"}), ("limits", {"name": "kafka", "pids": 100}),
                                        ("destroy", {"name": "kafka", "proof": PROOF}), ("build_alive", {"op_id": "a"}), ("lifecycle_active", {}),
                                        ("attach", {"name": "kafka"}), ("port_detect", {"name": "kafka"}), ("port_list", {"name": "kafka"}),
                                        ("port_add", {"name": "kafka", "port": 8000, "label": "app"}), ("port_remove", {"name": "kafka", "port": 8000})])
def test_everything_but_the_open_reads_needs_a_token(world, verb, args):
    for token in (None, "", "made-up", 7):
        reply = world.call(verb, args, token=token, op_id="op-1")
        assert kind(reply) == "unauthorized" and world.audited[-1] == (None, verb, "unauthorized")
    assert world.spawned == [] and world.limits_set == [] and world.ports_asked == []
    assert kind(world.call(verb, args, token=world.login(), op_id="op-1")) == "ok"


def test_a_token_ends_with_time_with_logout_and_with_a_new_password(world):
    token = world.login()
    assert kind(world.call("lifecycle_active", token=token)) == "ok"
    world.clock.now += 8 * 3600 - 1
    assert kind(world.call("lifecycle_active", token=token)) == "ok"
    world.clock.now += 1
    assert kind(world.call("lifecycle_active", token=token)) == "unauthorized"

    token = world.login()
    assert world.call("logout", token=token) == {"ok": True, "result": {"logged_out": True}}
    assert world.audited[-1] == ("operator", "logout", "logout")
    assert kind(world.call("lifecycle_active", token=token)) == "unauthorized"
    assert kind(world.call("logout", token=token)) == "ok"   # asked twice: harmless
    assert world.audited[-1] == (None, "logout", "logout")

    token, other = world.login(), world.login()
    changed = []
    assert kind(world.call("lifecycle_active", token=token, sync_tokens=lambda: None)) == "ok"
    assert kind(world.call("lifecycle_active", token=token, sync_tokens=lambda: changed.append(True) or world.tokens.revoke_all())) == "unauthorized"
    assert kind(world.call("lifecycle_active", token=other)) == "unauthorized" and changed == [True]


def test_the_server_ends_every_session_when_the_password_file_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "passwd.json")

    class Sessions:
        tokens = broker_auth.TokenStore()
        password_seen = broker.password_signature()
    one = Sessions()
    assert one.password_seen is None
    token, _ = one.tokens.issue("operator")
    broker.Server.sync_tokens(one)
    assert one.tokens.principal_for(token) == "operator"          # nothing changed: the session stays
    broker_auth.set_password("a first proof", path=broker_auth.PASSWD_FILE)
    broker.Server.sync_tokens(one)
    assert one.tokens.principal_for(token) is None                # a password was set: it ends
    token, _ = one.tokens.issue("operator")
    broker.Server.sync_tokens(one)
    assert one.tokens.principal_for(token) == "operator"
    broker_auth.set_password("a second proof", path=broker_auth.PASSWD_FILE)
    broker.Server.sync_tokens(one)
    assert one.tokens.principal_for(token) is None


# --- the step-up -----------------------------------------------------------------------------

def test_destroy_needs_the_proof_again_and_the_proof_goes_no_further(world):
    token = world.login()
    for args in ({"name": "kafka"}, {"name": "kafka", "proof": "wrong"}, {"name": "kafka", "proof": None}):
        reply = world.call("destroy", args, token=token, op_id="op-1")
        assert reply == {"ok": False, "error": {"kind": "step_up_required", "message": "this action requires re-entering your password"}}
    assert world.spawned == [] and world.audited[-1] == ("operator", "destroy", "step_up_fail")
    assert kind(world.call("destroy", {"name": "kafka", "proof": PROOF}, token=token, op_id="op-1")) == "ok"
    (spec,) = world.spawned
    assert {k: spec[k] for k in ("op_id", "verb", "name", "limits")} == {"op_id": "op-1", "verb": "destroy", "name": "kafka", "limits": None}
    everything_written = json.dumps(spec) + read(broker.view_log("op-1")) + read(broker.marker("op-1")) + repr(world.audited)
    assert PROOF not in everything_written


def test_without_a_token_nothing_is_told_apart(world):
    """The step-up, a bad name, an unknown field: a caller without a token hears of none."""
    for args in ({"name": "kafka"}, {"name": "../x", "proof": PROOF}, {"name": "kafka", "proof": PROOF, "extra": 1}, {}):
        assert kind(world.call("destroy", args, token=None, op_id="../x")) == "unauthorized"
    for args in ({"name": "../x"}, {"name": "kafka", "extra": 1}, {}):
        assert kind(world.call("create", args, token=None, op_id=None)) == "unauthorized"


def test_no_other_verb_takes_a_proof(world):
    token = world.login()
    for verb in ("create", "start", "stop", "recreate", "limits"):
        assert kind(world.call(verb, {"name": "kafka", "proof": PROOF}, token=token, op_id="op-1")) == "validation"
    assert world.spawned == []


# --- fields, names, limits -------------------------------------------------------------------

@pytest.mark.parametrize("verb, args", [("list", {"name": "kafka"}), ("status", {"name": "kafka", "verbose": True}), ("create", {"name": "kafka", "image": "evil"}),
                                        ("start", {"name": "kafka", "all": True}), ("limits", {"name": "kafka", "disk": 5}), ("op", {"op_id": "a", "name": "kafka"}),
                                        ("attach", {"name": "kafka", "host": "203.0.113.9"}), ("port_detect", {"name": "kafka", "deadline": 5}),
                                        ("port_add", {"name": "kafka", "port": 8000, "label": "app", "count": 2}),
                                        ("port_remove", {"name": "kafka", "port": 8000, "label": "app"})])
def test_a_field_that_is_not_the_verbs_is_refused_not_dropped(world, verb, args):
    reply = world.call(verb, args, token=world.login(), op_id="op-1")
    assert kind(reply) == "validation" and "not [" in reply["error"]["message"]
    assert world.spawned == [] and world.limits_set == [] and world.ports_asked == []
    if verb in ("create", "start", "limits", "attach", "port_add", "port_remove"):
        assert world.audited[-1] == ("operator", verb, "validation")   # a refused write is on record; a refused read is not
    else:
        assert world.audited[-1] == ("operator", "login", "ok")


@pytest.mark.parametrize("name", ["../../xtables", "a/b", "Kafka", "", None, 7, ["kafka"], "kafka-"])
def test_a_name_that_is_not_one_reaches_nothing(world, name):
    token = world.login()
    for verb in ("status", "create", "start", "stop", "recreate", "limits"):
        reply = world.call(verb, {"name": name}, token=token, op_id="op-1")
        assert kind(reply) == "validation" and "A project name is" in reply["error"]["message"], verb
    assert kind(world.call("destroy", {"name": name, "proof": PROOF}, token=token, op_id="op-1")) == "validation"
    assert world.spawned == [] and world.limits_set == [] and not os.path.exists(core.LOCKS)


def test_limits_reach_the_core_as_numbers_and_only_the_ones_named(world):
    token = world.login()
    assert kind(world.call("limits", {"name": "kafka", "memory": "2g", "cpus": 1.5}, token=token)) == "ok"
    assert kind(world.call("limits", {"name": "kafka", "pids": None, "memory": 1024 ** 3}, token=token)) == "ok"
    assert [(name, changed) for name, changed, _ in world.limits_set] == [
        ("kafka", {"memory": 2 * 1024 ** 3, "cpus": 1.5}), ("kafka", {"pids": None, "memory": 1024 ** 3})]
    assert all(held == {"kafka"} for _, _, held in world.limits_set) and core.HELD == set()   # the lock is in hand while the core works, and let go after
    for bad in ({"pids": 3}, {"memory": "lots"}, {"cpus": -1}, {"pids": True}, {"memory": [1]}, {"cpus": {"a": 1}}, {"pids": 1.5}):
        assert kind(world.call("limits", dict(bad, name="kafka"), token=token)) == "validation", bad
    assert len(world.limits_set) == 2
    assert kind(world.call("create", {"name": "kafka", "memory": "1g", "pids": 200}, token=token, op_id="op-1")) == "ok"
    assert world.spawned[0]["limits"] == {"memory": 1024 ** 3, "pids": 200}
    assert kind(world.call("create", {"name": "other", "memory": 5}, token=token, op_id="op-2")) == "validation"


def test_the_shell_login_and_the_port_verbs_reach_the_core_with_their_fields_and_are_audited_as_writes(world):
    token = world.login()
    calls = [("attach", {"name": "kafka"}), ("port_detect", {"name": "kafka"}), ("port_list", {"name": "kafka"}),
             ("port_add", {"name": "kafka", "port": 5432, "label": "pg", "passthrough": True, "host_port": 11200}),
             ("port_add", {"name": "kafka", "port": 8000, "label": "app"}),
             ("port_remove", {"name": "kafka", "port": 11200, "passthrough": True}), ("port_remove", {"name": "kafka", "port": 8000})]
    for verb, args in calls:
        assert world.call(verb, args, token=token) == {"ok": True, "result": {"asked": verb}}
    assert world.ports_asked == [("attach", ("kafka",), set()), ("port_detect", ("kafka",), set()), ("port_list", ("kafka",), set()),
                                 ("port_add", ("kafka", 5432, "pg", True, 11200), {"kafka"}), ("port_add", ("kafka", 8000, "app", False, None), {"kafka"}),
                                 ("port_remove", ("kafka", 11200, True), {"kafka"}), ("port_remove", ("kafka", 8000, False), {"kafka"})]
    assert world.audited[1:] == [("operator", "attach", "ok"), ("operator", "port_add", "ok"), ("operator", "port_add", "ok"),
                                 ("operator", "port_remove", "ok"), ("operator", "port_remove", "ok")]
    assert core.HELD == set()
    held = core.try_lock("kafka")
    try:
        assert kind(world.call("port_add", {"name": "kafka", "port": 8001, "label": "app"}, token=token)) == "busy"
        assert kind(world.call("attach", {"name": "kafka"}, token=token)) == "ok"   # answered without the lock
    finally:
        held.close()


def test_what_the_core_refuses_is_a_failure_with_its_message(world, monkeypatch):
    def refuses(name):
        raise core.CoreError("There is no project named kafka.")
    monkeypatch.setattr(core, "status", refuses)
    assert world.call("status", {"name": "kafka"}) == {"ok": False, "error": {"kind": "failed", "message": "There is no project named kafka."}}


def test_a_write_that_cannot_be_done_is_a_failure_on_record_and_names_no_path(world, monkeypatch):
    """A full volume under `limits`, a child that cannot be started: the caller hears "failed"
    and why, without a path of the container, and the audit log has the line."""
    token = world.login()

    def full(name, **changed):
        raise OSError(28, "No space left on device", "/data/core/projects/kafka.json.tmp")
    monkeypatch.setattr(core, "set_limits", full)
    reply = world.call("limits", {"name": "kafka", "pids": 100}, token=token)
    assert reply == {"ok": False, "error": {"kind": "failed", "message": "No space left on device"}}
    assert world.audited[-1] == ("operator", "limits", "failed") and core.HELD == set()
    assert core.try_lock("kafka") is not None   # the project is not left taken

    def cannot_start(spec):
        raise OSError(12, "Cannot allocate memory")
    reply = broker.dispatch("start", {"name": "other"}, token, world.tokens, op_id="op-1", spawn=cannot_start,
                            audit=lambda principal, verb, outcome: world.audited.append((principal, verb, outcome)))
    assert reply == {"ok": False, "error": {"kind": "failed", "message": "Cannot allocate memory"}}
    assert world.audited[-1] == ("operator", "start", "failed") and core.try_lock("other") is not None
    assert broker.op_state("op-1")["state"] == "failed"   # what was begun is answered for


# --- operations ------------------------------------------------------------------------------

def test_an_operation_is_accepted_logged_and_started_with_what_it_needs_and_no_more(world):
    token = world.login()
    reply = world.call("create", {"name": "kafka", "cpus": 2}, token=token, op_id="op-1")
    assert reply == {"ok": True, "result": {"op_id": "op-1", "state": "running", "started": True}}
    (spec,) = world.spawned
    assert sorted(spec) == ["agents", "claude_effort", "claude_model", "editor", "limits", "lock_fd", "log_fd", "marker_fd", "name", "op_id", "verb"]
    first = json.loads(read(broker.view_log("op-1")))
    assert {k: first[k] for k in ("op_id", "project", "action", "step", "status", "msg")} == {
        "op_id": "op-1", "project": "kafka", "action": "create", "step": "accepted", "status": "step", "msg": ""}
    assert isinstance(first["ts"], float) and sorted(first) == ["action", "msg", "op_id", "project", "status", "step", "ts"]   # RS's record
    assert json.loads(read(broker.marker("op-1"))) == {"op_id": "op-1", "project": "kafka", "verb": "create"}
    assert world.audited[-1] == ("operator", "create", "ok")


@pytest.mark.parametrize("op_id", [None, "", "../x", "a/b", ".hidden", "a" * 65, 7, "op 1", "op\n"])
def test_an_op_id_that_is_not_a_safe_file_name_is_a_bad_request(world, op_id):
    token = world.login()
    assert kind(world.call("create", {"name": "kafka"}, token=token, op_id=op_id)) == "bad_request"
    assert world.audited[-1] == ("operator", "create", "bad_request")
    assert kind(world.call("op", {"op_id": op_id})) == "bad_request"
    assert world.spawned == [] and not os.path.exists(broker.OPLOGS)


def test_an_op_id_is_used_once_and_a_link_in_its_place_is_not_followed(world, tmp_path):
    token = world.login()
    assert kind(world.call("start", {"name": "kafka"}, token=token, op_id="op-1")) == "ok"
    again = world.call("start", {"name": "other"}, token=token, op_id="op-1")
    assert kind(again) == "bad_request" and "used before" in again["error"]["message"]
    target = tmp_path / "elsewhere"
    os.symlink(target, broker.view_log("planted"))
    assert kind(world.call("start", {"name": "third"}, token=token, op_id="planted")) == "bad_request"
    assert not target.exists() and len(world.spawned) == 1
    assert core.try_lock("other") is not None and core.try_lock("third") is not None   # the refused ones hold nothing


def test_a_project_that_something_holds_is_busy_at_once(world):
    token = world.login()
    held = core.try_lock("kafka")   # a `cl` command, the init, or another operation
    for verb, args in (("start", {"name": "kafka"}), ("create", {"name": "kafka"}), ("limits", {"name": "kafka", "pids": 100}),
                       ("destroy", {"name": "kafka", "proof": PROOF})):
        reply = world.call(verb, args, token=token, op_id=f"op-{verb}")
        assert kind(reply) == "busy", verb
    assert world.spawned == [] and world.limits_set == [] and not os.path.exists(broker.OPLOGS)
    assert world.audited[-1] == ("operator", "destroy", "busy")
    held.close()
    assert kind(world.call("start", {"name": "kafka"}, token=token, op_id="op-2")) == "ok"
    assert kind(world.call("start", {"name": "other"}, token=token, op_id="op-3")) == "ok"   # another project is never in the way


def test_the_lock_is_handed_to_the_child_and_let_go_by_the_broker(world):
    """With a child that keeps its copies, the project stays taken and the operation is running;
    when the child is gone, both end."""
    kept = []
    token = world.login()
    reply = broker.dispatch("stop", {"name": "kafka"}, token, world.tokens, op_id="op-1",
                            spawn=lambda spec: kept.extend(os.dup(spec[key]) for key in ("lock_fd", "marker_fd")))
    assert kind(reply) == "ok"
    assert core.try_lock("kafka") is None and broker.is_running("op-1")
    assert broker.ops_in_flight() == [{"op_id": "op-1", "project": "kafka", "verb": "stop", "state": "running"}]
    assert broker.op_state("op-1") == {"op_id": "op-1", "project": "kafka", "verb": "stop", "msg": "", "state": "running"}
    for fd in kept:
        os.close(fd)                    # the child ends
    again = core.try_lock("kafka")
    assert again is not None and not broker.is_running("op-1")
    again.close()
    assert broker.ops_in_flight() == [] and not os.path.exists(broker.marker("op-1"))   # the stale marker is cleared
    assert broker.op_state("op-1")["state"] == "failed" and "ended without a result" in broker.op_state("op-1")["msg"]


def test_an_operation_that_ends_during_the_listing_does_not_hide_the_others(world, monkeypatch):
    token = world.login()
    kept = []
    for name in ("kafka", "other"):
        broker.dispatch("start", {"name": name}, token, world.tokens, op_id=f"op-{name}",
                        spawn=lambda spec: kept.extend(os.dup(spec[key]) for key in ("lock_fd", "marker_fd")))
    assert [op["op_id"] for op in broker.ops_in_flight()] == ["op-kafka", "op-other"]
    really_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path).endswith("op-kafka.json"):
            raise FileNotFoundError(path)   # it finished between the listing and this look
        return really_stat(path, *args, **kwargs)
    monkeypatch.setattr(broker.os, "stat", stat)
    assert [op["op_id"] for op in broker.ops_in_flight()] == ["op-other"]
    for fd in kept:
        os.close(fd)


def test_the_project_is_free_before_the_child_says_it_is_done(world, monkeypatch):
    """Whoever reads "done" and asks for the next thing at once must not be told "busy"."""
    free_at_the_last_word = []
    really_record = broker.record

    def record(op_id, project, action, status, step, msg):
        if status in ("done", "failed"):
            probe = core.try_lock(project)
            free_at_the_last_word.append(probe is not None)
            if probe:
                probe.close()
        return really_record(op_id, project, action, status, step, msg)
    monkeypatch.setattr(broker, "record", record)
    monkeypatch.setattr(core, "HELD", set())
    os.makedirs(broker.OPLOGS)
    os.makedirs(broker.MARKERS)
    during_the_work = []
    for verb, outcome in (("stop", {"state": "stopped"}), ("start", core.CoreError("no")), ("recreate", RuntimeError("no"))):
        def work(name, *rest, outcome=outcome):
            during_the_work.append((core.try_lock(name) is None, name in core.HELD))   # taken, and in hand, while the work is done
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(core, verb, work)
        held = core.try_lock("kafka")
        lock_fd = os.dup(held.fileno())
        held.close()
        log_fd = os.open(broker.view_log(f"op-{verb}"), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        broker.run_operation({"op_id": f"op-{verb}", "verb": verb, "name": "kafka", "limits": None, "lock_fd": lock_fd, "marker_fd": -1, "log_fd": log_fd})
        os.close(log_fd)
        assert core.HELD == set()
    assert free_at_the_last_word == [True, True, True]
    assert during_the_work == [(True, True)] * 3


def test_how_an_operation_stands_is_its_logs_last_word(world):
    token = world.login()
    world.call("start", {"name": "kafka"}, token=token, op_id="op-1")
    with open(broker.view_log("op-1"), "a") as fh:
        fh.write(broker.record("op-1", "kafka", "start", "step", "progress", "building the sandbox image"))
        fh.write(broker.record("op-1", "kafka", "start", "done", "done", "kafka is running."))
    assert world.call("op", {"op_id": "op-1"})["result"] == {"op_id": "op-1", "project": "kafka", "verb": "start", "state": "done", "msg": "kafka is running."}
    world.call("start", {"name": "other"}, token=token, op_id="op-2")
    with open(broker.view_log("op-2"), "a") as fh:
        fh.write(broker.record("op-2", "other", "start", "failed", "failed", "x" * 10000))
        fh.write("half a line that a kill cut")
    stands = world.call("op", {"op_id": "op-2"})["result"]
    assert stands["state"] == "failed" and len(stands["msg"]) == broker.MSG_CHARS
    unknown = world.call("op", {"op_id": "never"})
    assert kind(unknown) == "validation" and "no operation never" in unknown["error"]["message"]
    assert world.call("build_alive", {"op_id": "never"}, token=token) == {"ok": True, "result": {"alive": False}}


# --- the child -------------------------------------------------------------------------------

@pytest.mark.parametrize("verb, outcome, last", [
    ("create", {"state": "running", "notes": []}, ("done", "kafka is running.")),
    ("start", {"state": "running", "notes": ["The limits could not be applied."]}, ("done", "kafka is running. The limits could not be applied.")),
    ("destroy", {"state": "destroyed"}, ("done", "kafka is destroyed.")),
    ("stop", core.CoreError("The sandbox would not stop."), ("failed", "The sandbox would not stop.")),
    ("recreate", RuntimeError("a fault nobody planned for"), ("failed", "The operation failed in a way it does not know (RuntimeError). The container's log says more.")),
])
def test_the_child_does_the_work_with_the_lock_in_hand_and_says_how_it_went(world, monkeypatch, tmp_path, verb, outcome, last):
    seen = []

    def work(name, *rest):
        seen.append((name, set(core.HELD)))
        if rest and callable(rest[-1]):
            rest[-1]("a step of the work")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(core, {"limits": "set_limits"}.get(verb, verb), work)
    monkeypatch.setattr(core, "HELD", set())
    os.makedirs(broker.OPLOGS)
    os.makedirs(broker.MARKERS)
    open(broker.marker("op-1"), "w").close()
    log_fd = os.open(broker.view_log("op-1"), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    code = broker.run_operation({"op_id": "op-1", "verb": verb, "name": "kafka", "limits": {"pids": 100}, "lock_fd": -1, "marker_fd": -1, "log_fd": log_fd})
    os.close(log_fd)
    assert seen == [("kafka", {"kafka"})] and code == (1 if isinstance(outcome, Exception) else 0)
    records = [json.loads(line) for line in read(broker.view_log("op-1")).splitlines()]
    assert (records[-1]["status"], records[-1]["msg"]) == last and records[-1]["action"] == verb and records[-1]["project"] == "kafka"
    if verb in ("create", "start", "recreate"):
        assert (records[0]["status"], records[0]["step"], records[0]["msg"]) == ("step", "progress", "a step of the work")
    assert not os.path.exists(broker.marker("op-1"))


# --- cl passwd -------------------------------------------------------------------------------

class Typed:
    def __init__(self, text):
        self.text = text

    def isatty(self):
        return False

    def read(self):
        return self.text


@pytest.mark.parametrize("typed, password", [("correct horse\n", "correct horse"), ("correct horse", "correct horse"), ("two\n\n", "two\n"), ("  spaces kept \n", "  spaces kept "),
                                             ("from windows\r\n", "from windows"), ("inner\rreturn\n", "inner\rreturn"), ("ends in a return\r", "ends in a return\r")])
def test_the_new_password_is_standard_input_less_one_newline(typed, password):
    assert core.read_new_password(Typed(typed)) == password


def test_setting_a_password_stores_what_the_browser_will_send(monkeypatch, tmp_path):
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "broker" / "passwd.json")
    for short in ("", "1234567"):
        with pytest.raises(core.CoreError, match="at least 8"):
            core.set_password(short)
    assert not (tmp_path / "broker").exists()
    assert core.set_password("correct horse") == {"password": "set"}
    assert oct((tmp_path / "broker" / "passwd.json").stat().st_mode & 0o777) == "0o600"
    assert "correct horse" not in (tmp_path / "broker" / "passwd.json").read_text()
    assert broker.proof_is_right(broker_auth.derive_login_proof("correct horse"))
    assert not broker.proof_is_right(broker_auth.derive_login_proof("correct horse\n"))
    assert not broker.proof_is_right("correct horse")   # the password itself is never what is sent


# --- the first password, with the set-up code --------------------------------------------------

CODE = "k3x9q2m7p4w8z1v6b5n0"                                   # the form the init makes: 20 of [a-z0-9]
GOOD = base64.b64encode(bytes(range(32))).decode()             # the form derive_login_proof makes
REFUSED = {"ok": False, "error": {"kind": "auth", "message": "invalid credentials"}}


@pytest.fixture
def first(monkeypatch, tmp_path):
    """The real password check on a password file in a temp folder, a set-up code held, and
    a clock for the shut door."""
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "broker" / "passwd.json")

    class First:
        clock = Clock()
        setup = broker.SetupCode(CODE, now=clock)
        tokens = broker_auth.TokenStore()
        passwd = tmp_path / "broker" / "passwd.json"

        def call(self, verb, args=None, **more):
            more.setdefault("setup", self.setup)
            return broker.dispatch(verb, {} if args is None else args, None, self.tokens,
                                   audit=lambda principal, v, outcome: self.audited.append((principal, v, outcome)), **more)

        def set(self, code=CODE, proof=GOOD, **more):
            return self.call("first_password", {"code": code, "proof": proof}, **more)

    one = First()
    one.audited = []
    return one


def test_the_right_code_sets_the_first_password_and_a_login_with_the_same_proof_succeeds(first):
    proof = broker_auth.derive_login_proof("correct horse")   # what the browser sends
    assert first.set(proof=proof) == {"ok": True, "result": {"password": "set"}}
    assert first.audited == [(None, "first_password", "ok")] and first.setup.code is None   # spent
    assert oct(first.passwd.stat().st_mode & 0o777) == "0o600" and oct(first.passwd.parent.stat().st_mode & 0o777) == "0o700"
    assert proof not in first.passwd.read_text() and os.listdir(first.passwd.parent) == ["passwd.json"]
    login = first.call("login", {"proof": proof})
    assert login["ok"] and login["result"]["principal"] == "operator"
    assert first.set(proof=GOOD) == REFUSED and first.passwd.read_text() and broker.proof_is_right(proof)   # once only


def test_a_wrong_code_or_none_held_gets_the_logins_refusal_and_stores_nothing(first, monkeypatch):
    compared = []
    really_compare = broker.hmac.compare_digest
    monkeypatch.setattr(broker.hmac, "compare_digest", lambda a, b: compared.append((type(a), type(b))) or really_compare(a, b))
    for code in ("wrong", CODE.upper(), CODE[:-1], CODE + "x", CODE + "\n", "", "\ud800" * 20):
        assert first.set(code=code) == REFUSED, code
    for setup in (broker.SetupCode(None), None):   # none held, and no set-up object at all
        for code in (CODE, broker.NO_CODE):
            assert first.set(code=code, setup=setup) == REFUSED
    assert first.audited == [(None, "first_password", "auth_fail")] * 11
    assert compared == [(bytes, bytes)] * 11     # the same comparison, held or not
    assert not first.passwd.parent.exists()


def test_ten_wrong_codes_in_a_minute_shut_the_door_for_a_minute(first):
    for _ in range(9):
        assert first.set(code="wrong") == REFUSED
        first.clock.now += 1
    first.clock.now += 51                          # the first wrong code is out of the minute now: nine within it
    assert first.set(code="wrong") == REFUSED
    first.clock.now += 0.5
    assert first.set(code="wrong") == REFUSED      # the tenth within the minute
    for code in ("wrong", CODE):
        shut = first.set(code=code)
        assert shut == {"ok": False, "error": {"kind": "rate_limited", "message": "Too many wrong set-up codes; wait 60 seconds and try again."}}
    first.clock.now += 59.5
    assert "wait 1 seconds" in first.set()["error"]["message"]
    assert not first.passwd.exists()
    assert first.audited[-4:] == [(None, "first_password", "auth_fail")] + [(None, "first_password", "rate_limited")] * 3
    first.clock.now += 0.5
    assert first.set() == {"ok": True, "result": {"password": "set"}}


def test_with_a_password_set_the_right_code_is_refused_and_the_file_is_untouched(first):
    broker_auth.set_password("an earlier proof", path=broker_auth.PASSWD_FILE)   # `cl passwd`
    before = first.passwd.read_bytes()
    for proof in (GOOD, "not in the form"):   # the same refusal: whether a password is set is not told here
        assert first.set(proof=proof) == REFUSED
    assert first.passwd.read_bytes() == before and os.listdir(first.passwd.parent) == ["passwd.json"]
    assert first.audited == [(None, "first_password", "refused")] * 2


def test_a_password_file_that_appears_just_before_the_link_is_not_replaced(first, monkeypatch):
    really_link = os.link

    def cl_passwd_first(source, target):
        broker_auth.set_password("set in the same moment", path=pathlib.Path(target))
        return really_link(source, target)
    monkeypatch.setattr(broker.os, "link", cl_passwd_first)
    assert first.set() == REFUSED
    assert broker.proof_is_right("set in the same moment") and not broker.proof_is_right(GOOD)
    assert os.listdir(first.passwd.parent) == ["passwd.json"]   # and no temporary file is left
    assert first.audited == [(None, "first_password", "refused")]


def test_a_proof_not_in_the_form_a_login_sends_stores_nothing(first):
    last = GOOD[-2]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    not_canonical = GOOD[:-2] + alphabet[alphabet.index(last) + 1] + "="   # decodes to the same bytes, but a login never sends it
    for proof in ("correct horse", "", base64.b64encode(bytes(31)).decode(), base64.b64encode(bytes(33)).decode(), GOOD[:-1], GOOD + "\n",
                  " " + GOOD, GOOD.replace("A", "-"), base64.urlsafe_b64encode(bytes([251] * 32)).decode(), not_canonical, "é" * 44):
        reply = first.set(proof=proof)
        assert kind(reply) == "validation" and "nothing was stored" in reply["error"]["message"], proof
    assert not first.passwd.exists() and first.audited == [(None, "first_password", "refused")] * 11
    assert kind(first.set(proof=base64.b64encode(bytes([251] * 32)).decode())) == "ok"


def test_setup_needed_says_whether_a_first_password_can_be_set_and_is_not_audited(first):
    assert first.call("setup_needed") == {"ok": True, "result": {"needed": True}}
    assert first.call("setup_needed", setup=broker.SetupCode(None)) == {"ok": True, "result": {"needed": False}}
    assert broker.dispatch("setup_needed", {}) == {"ok": True, "result": {"needed": False}}   # no set-up object
    assert kind(first.call("setup_needed", {"code": CODE})) == "validation"
    assert first.audited == []
    broker_auth.set_password("an earlier proof", path=broker_auth.PASSWD_FILE)
    assert first.call("setup_needed") == {"ok": True, "result": {"needed": False}}
    assert first.audited == []


def test_the_set_up_verbs_take_an_object_and_first_password_takes_two_strings(first):
    for verb in ("setup_needed", "first_password"):
        for args in (["x"], "x", 7):
            assert kind(first.call(verb, args)) == "bad_request"
    for args in ({"code": CODE}, {"proof": GOOD}, {"code": 7, "proof": GOOD}, {"code": CODE, "proof": None}, {"code": CODE, "proof": GOOD, "name": "kafka"}):
        assert kind(first.call("first_password", args)) == "bad_request", args
    assert not first.passwd.exists() and first.setup.wrong == []
    assert first.audited == [(None, "first_password", "bad_request")] * 5


def test_the_unknown_verbs_message_names_every_verb(first):
    message = first.call("passwd")["error"]["message"]
    assert all(repr(verb) in message for verb in broker.FIELDS) and "'first_password'" in message and "'setup_needed'" in message


# --- the set-up code at the broker's start, and through the socket ------------------------------

def fed(data):
    """A pipe holding `data`, its writing end closed: what the init hands over."""
    read_end, write_end = os.pipe()
    os.write(write_end, data)
    os.close(write_end)
    return read_end


def is_dev_null(fd):
    return os.path.samestat(os.fstat(fd), os.stat(os.devnull))


@pytest.mark.parametrize("given, taken", [(CODE.encode(), CODE), (CODE.encode() + b"\n", CODE), (b"", None), (CODE.encode() + b"\n\n", None),
                                          (CODE.upper().encode(), None), (CODE[:-1].encode(), None), (CODE.encode() + b"x", None),
                                          (b" " + CODE.encode(), None), (CODE.encode() + b"\r\n", None), (CODE.encode() * 50, None)])
def test_the_code_is_read_once_from_standard_input_and_a_malformed_one_is_not_logged(given, taken, capfd):
    fd = fed(given)
    try:
        assert broker.take_setup_code(fd) == taken
        assert is_dev_null(fd)   # nothing inherits the pipe
    finally:
        os.close(fd)
    said = capfd.readouterr().err
    assert ("malformed" in said) == (given != b"" and taken is None)
    assert CODE not in said and CODE[:10] not in said and CODE.upper()[:10] not in said


def test_no_code_from_dev_null_a_closed_input_or_a_terminal(capfd):
    fd = os.open(os.devnull, os.O_RDONLY)
    assert broker.take_setup_code(fd) is None and is_dev_null(fd)
    os.close(fd)
    assert broker.take_setup_code(fd) is None and is_dev_null(fd)   # closed: /dev/null takes its place
    os.close(fd)
    typing, terminal = pty.openpty()
    os.write(typing, CODE.encode() + b"\n")   # someone typed it: still not read
    try:
        assert broker.take_setup_code(terminal) is None and is_dev_null(terminal)
    finally:
        os.close(typing)
        os.close(terminal)
    assert capfd.readouterr().err == ""


def test_the_read_is_bounded(monkeypatch):
    really_read, got = os.read, []
    monkeypatch.setattr(broker.os, "read", lambda fd, count: got.append(len(chunk := really_read(fd, count))) or chunk)
    fd = fed(b"x" * 1000)
    try:
        assert broker.take_setup_code(fd) is None
    finally:
        os.close(fd)
    assert 0 < sum(got) <= broker.SETUP_CODE_BYTES


SERVE_WITH = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
import broker
seen = []
broker.serve = lambda **given: seen.append(given) or 0
broker.main(["serve"])
print(json.dumps({"code": seen[0].get("setup_code"), "stdin_is_null": os.path.samestat(os.fstat(0), os.stat(os.devnull))}))
"""


@pytest.mark.parametrize("stdin, taken", [(CODE + "\n", CODE), ("", None), (subprocess.DEVNULL, None), ("nope\n", None), ("closed", None)])
def test_serve_is_given_the_code_from_standard_input_which_is_dev_null_afterwards(stdin, taken):
    how = {"input": stdin} if isinstance(stdin, str) and stdin != "closed" else {"stdin": stdin} if stdin != "closed" else {"preexec_fn": lambda: os.close(0)}
    done = subprocess.run([sys.executable, "-c", SERVE_WITH, os.path.dirname(broker.__file__)], capture_output=True, text=True, timeout=60, check=False, **how)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"code": taken, "stdin_is_null": True}
    assert ("malformed" in done.stderr) == (stdin == "nope\n") and "nope" not in done.stderr


@contextlib.contextmanager
def running(tmp_path, code):
    """A real broker server on a socket in the temp folder, served from a thread."""
    path = str(tmp_path / "b.sock")
    server = broker.Server(path, code)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield lambda verb, args=None, **more: broker.client_call(verb, args, socket_path=path, timeout=20, **more)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_neither_code_nor_proof_reaches_a_log_the_audit_file_or_any_file(monkeypatch, tmp_path, capfd):
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "broker" / "passwd.json")
    audit_file = tmp_path / "broker" / "audit.log"
    really_audit, really_set = broker_auth.audit_event, broker_auth.set_password
    monkeypatch.setattr(broker_auth, "audit_event", lambda principal, verb, outcome: really_audit(principal, verb, outcome, path=audit_file))

    def fails_saying_what_it_was_given(proof, *, path):
        raise RuntimeError(f"could not store {proof}")
    replies = []
    with running(tmp_path, CODE) as call:
        replies.append(call("setup_needed"))
        replies.append(call("first_password", {"code": CODE[::-1], "proof": GOOD}))
        replies.append(call("first_password", {"code": CODE, "proof": "made of " + CODE}))
        monkeypatch.setattr(broker_auth, "set_password", fails_saying_what_it_was_given)
        replies.append(call("first_password", {"code": CODE, "proof": GOOD}))
        monkeypatch.setattr(broker_auth, "set_password", really_set)
        replies.append(call("first_password", {"code": CODE, "proof": GOOD}))
        replies.append(call("login", {"proof": GOOD}))
        replies.append(call("setup_needed"))
    assert [kind(reply) for reply in replies] == ["ok", "auth", "validation", "failed", "ok", "ok", "ok"]
    assert replies[0]["result"] == {"needed": True} and replies[-1]["result"] == {"needed": False}
    said = capfd.readouterr()
    assert "with the set-up code printed in this container's log" in said.err and "RuntimeError" in said.err
    audited = audit_file.read_text()
    assert [json.loads(line)["outcome"] for line in audited.splitlines()] == ["auth_fail", "refused", "ok", "ok"]   # the unknown failure did not reach its audit line
    everything = said.out + said.err + json.dumps(replies[:-2]) + "".join(p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file())
    for secret in (CODE, CODE[::-1], GOOD):
        assert secret not in everything


def test_the_code_reaches_no_child(monkeypatch, tmp_path):
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "broker" / "passwd.json")
    monkeypatch.setattr(broker_auth, "audit_event", lambda *given: None)
    monkeypatch.setattr(broker, "OPLOGS", str(tmp_path / "oplogs"))
    monkeypatch.setattr(broker, "MARKERS", str(tmp_path / "markers"))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    broker_auth.set_password(GOOD, path=broker_auth.PASSWD_FILE)   # so that the code is still held
    started = []

    class Child:
        def __init__(self, argv, **given):
            started.append((argv, given, dict(os.environ)))

        def poll(self):
            return 0
    monkeypatch.setattr(broker.subprocess, "Popen", Child)
    with running(tmp_path, CODE) as call:
        token = call("login", {"proof": GOOD})["result"]["token"]
        assert kind(call("create", {"name": "kafka"}, token=token, op_id="op-1")) == "ok"
    ((argv, given, environment),) = started
    assert argv[2] == "__run-op" and json.loads(argv[3])["name"] == "kafka"
    assert CODE not in json.dumps(argv) and "env" not in given and CODE not in json.dumps(environment)


def test_the_start_says_how_a_first_password_can_be_set(monkeypatch, tmp_path, capfd):
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "passwd.json")
    for code in (CODE, None):
        broker.Server(str(tmp_path / "b.sock"), code).server_close()
    said = capfd.readouterr().err.splitlines()
    assert said == [("clanker-learn broker: no password is set: set the first one on the page with the set-up code printed in this container's log,"
                     " or with `docker exec -i clanker-learn cl passwd`"),
                    "clanker-learn broker: no password is set, so nobody can log in: `docker exec -i clanker-learn cl passwd` sets one"]


def test_the_shell_password_reaches_no_log_and_no_audit_line(monkeypatch, tmp_path, capfd):
    """attach through a real request, to the real core with the daemon stood in for: a success,
    a sandbox that is not running, and a failure the broker does not know whose text holds the
    password. The password is in the first reply and nowhere else."""
    password = "TheShellPasswordOf32Characters00"
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "broker" / "passwd.json")
    audit_file = tmp_path / "broker" / "audit.log"
    really_audit = broker_auth.audit_event
    monkeypatch.setattr(broker_auth, "audit_event", lambda principal, verb, outcome: really_audit(principal, verb, outcome, path=audit_file))
    monkeypatch.setattr(core, "DATA", str(tmp_path / "data"))
    broker_auth.set_password(GOOD, path=broker_auth.PASSWD_FILE)
    core.write_record(core.new_record("kafka"))
    keys = tmp_path / "data" / "core" / "keys" / "kafka"
    keys.mkdir(parents=True)
    (keys / "shell_password").write_text(password + "\n")
    (keys / "ssh_host_ed25519_key.pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFuQY96dMIi969HDZQuBi0c3I/01XhZ9te8rSEQfKPZD cl-sb-kafka\n")
    sandbox = {"State": {"Running": True}, "NetworkSettings": {"Networks": {"cl-net-kafka": {"IPAddress": "172.18.0.2"}}}}
    monkeypatch.setattr(core, "inspect_container", lambda name: sandbox)
    replies = []
    with running(tmp_path, None) as call:
        token = call("login", {"proof": GOOD})["result"]["token"]
        replies.append(call("attach", {"name": "kafka"}, token=token))
        sandbox["State"]["Running"] = False
        replies.append(call("attach", {"name": "kafka"}, token=token))
        sandbox["State"]["Running"] = True

        def fails_saying_what_it_read(name):
            raise RuntimeError(f"could not read the key beside {core.shell_password(name)}")
        monkeypatch.setattr(core, "host_fingerprint", fails_saying_what_it_read)
        replies.append(call("attach", {"name": "kafka"}, token=token))
    assert [kind(reply) for reply in replies] == ["ok", "failed", "failed"]
    assert replies[0]["result"]["password"] == password
    said = capfd.readouterr()
    assert "RuntimeError" in said.err
    audited = [json.loads(line) for line in audit_file.read_text().splitlines()]
    assert [(line["verb"], line["outcome"]) for line in audited] == [("login", "ok"), ("attach", "ok"), ("attach", "failed")]
    everything = said.out + said.err + json.dumps(replies[1:]) + "".join(
        p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file() and p.name != "shell_password")
    assert password not in everything
