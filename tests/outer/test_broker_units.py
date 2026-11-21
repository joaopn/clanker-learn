"""The broker's gates and its bookkeeping of operations, without a daemon or a socket."""
import json
import os

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
    assert sorted(broker.FIELDS) == ["build_alive", "create", "destroy", "lifecycle_active", "limits", "list", "op", "recreate", "start", "status", "stop"]


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
                                        ("destroy", {"name": "kafka", "proof": PROOF}), ("build_alive", {"op_id": "a"}), ("lifecycle_active", {})])
def test_everything_but_the_open_reads_needs_a_token(world, verb, args):
    for token in (None, "", "made-up", 7):
        reply = world.call(verb, args, token=token, op_id="op-1")
        assert kind(reply) == "unauthorized" and world.audited[-1] == (None, verb, "unauthorized")
    assert world.spawned == [] and world.limits_set == []
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
                                        ("start", {"name": "kafka", "all": True}), ("limits", {"name": "kafka", "disk": 5}), ("op", {"op_id": "a", "name": "kafka"})])
def test_a_field_that_is_not_the_verbs_is_refused_not_dropped(world, verb, args):
    reply = world.call(verb, args, token=world.login(), op_id="op-1")
    assert kind(reply) == "validation" and "not [" in reply["error"]["message"]
    assert world.spawned == [] and world.limits_set == []
    if verb in ("create", "start", "limits"):
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
    assert sorted(spec) == ["limits", "lock_fd", "log_fd", "marker_fd", "name", "op_id", "verb"]
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
