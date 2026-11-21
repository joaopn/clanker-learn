"""The broker, asked through its socket from inside a real outer container in privileged mode.

Every request goes through `broker.py call`, the broker's own small client, run in the outer
container as the web server will later reach it from its own. A `docker` stand-in first on the
path makes `docker start` slow while /tmp/slow exists, so that an operation can be caught
under way.
"""
import json
import subprocess
import time

import broker_auth
import pytest
from conftest import Outer, run
from test_outer_container import PATH_WITH_FAKES, fake_tools, processes
from test_sandboxes import container_id, left_of

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SLOW_DOCKER = "#!/bin/sh\nif [ \"$1\" = start ] && [ -e /tmp/slow ]; then sleep 12; fi\nexec /usr/bin/docker \"$@\"\n"
ENV = {"CL_MODE": "privileged", "CL_WATCH_S": "1", "PATH": PATH_WITH_FAKES}
RECORD_KEYS = ["action", "msg", "op_id", "project", "status", "step", "ts"]   # RS's progress record


def call(lab, verb, args=None, token=None, op_id=None, user=None):
    """One request to the broker, from inside the outer container."""
    command = ["docker", "exec", *(["-u", user] if user else []), lab.name, "python3", "/opt/cl/broker.py", "call", verb,
               json.dumps(args or {}), token or "", *([op_id] if op_id else [])]
    done = run(*command, check=False)
    assert done.returncode == 0, f"{verb}: {done.stderr[-600:]}"
    return json.loads(done.stdout)


def kind(reply):
    return "ok" if reply["ok"] else reply["error"]["kind"]


def set_password(lab, password, expect=0):
    done = subprocess.run(["docker", "exec", "-i", lab.name, "cl", "passwd"], input=password + "\n", capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == expect, done.stderr
    return done


def login(lab, password=PASSWORD):
    return call(lab, "login", {"proof": broker_auth.derive_login_proof(password)})


def finished(lab, op_id, timeout=120):
    """How an operation ended, once it is no longer running."""
    deadline = time.monotonic() + timeout
    while True:
        stands = call(lab, "op", {"op_id": op_id})["result"]
        if stands["state"] != "running":
            return stands
        assert time.monotonic() < deadline, f"{op_id} is still running: {stands}"
        time.sleep(0.3)


def do(lab, token, verb, name, op_id, **more):
    """Start an operation and wait for its end."""
    assert call(lab, verb, dict(more, name=name), token, op_id) == {"ok": True, "result": {"op_id": op_id, "state": "running", "started": True}}
    return finished(lab, op_id)


def records(lab, op_id):
    return [json.loads(line) for line in lab.sh(f"cat /run/cl/broker/oplogs/{op_id}.view.log").stdout.splitlines()]


def a_desk(sandbox_image, tmp_path_factory=None, tools=None):
    one = Outer().start(env=ENV, extra=("-v", f"{tools}:/fake:ro"))
    assert one.wait_sandboxes()["state"] == "ready", one.state()
    one.load_image(sandbox_image)
    one.sandbox_image = sandbox_image
    one.wait_broker()
    set_password(one, PASSWORD)
    one.token = login(one)["result"]["token"]
    return one


@pytest.fixture(scope="module")
def desk(image, peer, sandbox_image, tmp_path_factory):
    """One outer container with the sandbox image, a password and a session, shared by the
    tests that each use projects of their own."""
    tools = fake_tools(tmp_path_factory.mktemp("desk"), {"docker": SLOW_DOCKER})
    one = a_desk(sandbox_image, tools=tools)
    try:
        yield one
    finally:
        one.remove()


# --- the lifecycle, through the socket -------------------------------------------------------

def test_a_project_is_made_stopped_started_recreated_and_limited_through_the_broker(desk):
    made = do(desk, desk.token, "create", "life", "op-create", memory="512m", cpus=1.5)
    assert made == {"op_id": "op-create", "project": "life", "verb": "create", "state": "done", "msg": "life is running."}
    status = call(desk, "status", {"name": "life"})["result"]
    assert status["state"] == "running" and status["limits"] == {"pids": 16384, "memory": 512 * 1024 ** 2, "cpus": 1.5}
    assert [p["name"] for p in call(desk, "list")["result"]] == ["life"]
    # The progress log is where and what RS's web server reads: JSON lines, the last one a "done".
    lines = records(desk, "op-create")
    assert [sorted(line) for line in lines] == [RECORD_KEYS] * len(lines)
    assert (lines[0]["step"], lines[0]["status"]) == ("accepted", "step") and (lines[-1]["status"], lines[-1]["msg"]) == ("done", "life is running.")
    assert {line["op_id"] for line in lines} == {"op-create"} and {line["action"] for line in lines} == {"create"}

    limited = call(desk, "limits", {"name": "life", "memory": "1g", "pids": 300}, desk.token)
    assert limited["ok"] and limited["result"]["limits"] == {"pids": 300, "memory": 1024 ** 3, "cpus": 1.5} and limited["result"]["notes"] == []
    assert desk.sandbox("life", "cat", "/sys/fs/cgroup/pids.max").stdout.strip() == "300"

    before = container_id(desk, "life")
    assert do(desk, desk.token, "stop", "life", "op-stop")["msg"] == "life is stopped."
    assert call(desk, "status", {"name": "life"})["result"]["state"] == "stopped"
    assert do(desk, desk.token, "start", "life", "op-start")["msg"] == "life is running."
    assert container_id(desk, "life") == before
    assert do(desk, desk.token, "recreate", "life", "op-recreate")["state"] == "done"
    assert container_id(desk, "life") != before and call(desk, "status", {"name": "life"})["result"]["state"] == "running"
    assert call(desk, "build_alive", {"op_id": "op-recreate"}, desk.token) == {"ok": True, "result": {"alive": False}}
    assert call(desk, "lifecycle_active", {}, desk.token) == {"ok": True, "result": {"ops": []}}
    assert desk.sh("ls /run/cl/broker-ops").stdout.split() == []   # no marker outlives its operation


def test_destroy_needs_the_password_again_and_the_proof_is_written_nowhere(desk):
    proof = broker_auth.derive_login_proof(PASSWORD)
    do(desk, desk.token, "create", "doomed", "d-create")
    for args in ({"name": "doomed"}, {"name": "doomed", "proof": broker_auth.derive_login_proof("not the password")}):
        assert kind(call(desk, "destroy", args, desk.token, "d-refused")) == "step_up_required"
    assert call(desk, "status", {"name": "doomed"})["result"]["state"] == "running"
    assert desk.sh("ls /run/cl/broker/oplogs/d-refused.view.log", check=False).returncode != 0   # a refused request leaves no log
    assert call(desk, "destroy", {"name": "doomed", "proof": proof}, desk.token, "d-destroy")["ok"]
    assert finished(desk, "d-destroy")["msg"] == "doomed is destroyed." and left_of(desk, "doomed") == []
    searched = desk.sh(f"grep -rc -- '{proof}' /run/cl /data/broker", check=False).stdout.splitlines()
    assert "/data/broker/audit.log:0" in searched and "/run/cl/broker/oplogs/d-destroy.view.log:0" in searched   # the search ran, on these files
    assert [line for line in searched if not line.endswith(":0")] == []
    assert proof not in run("docker", "logs", desk.name).stderr
    audit = [json.loads(line) for line in desk.sh("cat /data/broker/audit.log").stdout.splitlines()]
    assert [(a["verb"], a["outcome"]) for a in audit if a["verb"] == "destroy"] == [("destroy", "step_up_fail")] * 2 + [("destroy", "ok")]
    assert all(sorted(a) == ["outcome", "principal", "ts", "verb"] for a in audit)


def test_an_operation_that_fails_says_what_the_core_said(desk):
    do(desk, desk.token, "create", "twice", "t-1")
    failed = do(desk, desk.token, "create", "twice", "t-2")
    assert failed["state"] == "failed" and "A project named twice exists." in failed["msg"]
    assert records(desk, "t-2")[-1]["status"] == "failed"
    assert kind(call(desk, "create", {"name": "twice"}, desk.token, "t-2")) == "bad_request"           # an op_id is used once
    assert kind(call(desk, "create", {"name": "twice"}, None, "t-3")) == "unauthorized"
    assert kind(call(desk, "create", {"name": "twice", "image": "other"}, desk.token, "t-3")) == "validation"
    assert desk.sh("ls /run/cl/broker/oplogs/t-3.view.log", check=False).returncode != 0


def test_two_projects_are_worked_on_at_once_and_one_project_takes_one_thing_at_a_time(desk):
    for name in ("left", "right"):
        do(desk, desk.token, "create", name, f"{name}-create")
        do(desk, desk.token, "stop", name, f"{name}-stop")
    desk.sh("touch /tmp/slow")
    try:
        started = time.monotonic()
        assert call(desk, "start", {"name": "left"}, desk.token, "left-start")["ok"]
        assert call(desk, "start", {"name": "right"}, desk.token, "right-start")["ok"]
        flying = call(desk, "lifecycle_active", {}, desk.token)["result"]["ops"]
        assert flying == [{"op_id": "left-start", "project": "left", "verb": "start", "state": "running"},
                          {"op_id": "right-start", "project": "right", "verb": "start", "state": "running"}]
        assert call(desk, "build_alive", {"op_id": "left-start"}, desk.token)["result"] == {"alive": True}
        assert call(desk, "op", {"op_id": "left-start"})["result"]["state"] == "running"
        # The project is taken: anything else on it is refused at once, not queued behind it.
        for verb, args in (("stop", {"name": "left"}), ("limits", {"name": "left", "pids": 200}), ("recreate", {"name": "left"})):
            assert kind(call(desk, verb, args, desk.token, f"left-{verb}-refused")) == "busy", verb
        assert time.monotonic() - started < 10, "the broker waited for a project"
    finally:
        desk.sh("rm -f /tmp/slow")
    assert finished(desk, "left-start")["state"] == "done" and finished(desk, "right-start")["state"] == "done"
    assert time.monotonic() - started < 22, "the two starts ran one after the other"

    # A `cl` command (or the init) at work on a project is in the way just the same.
    holder = "import os, sys, time; sys.path.insert(0, '/opt/cl'); import core; ctx = core.locked('left'); ctx.__enter__(); print(os.getpid(), flush=True); time.sleep(1000)"
    run("docker", "exec", "-d", desk.name, "sh", "-c", f"python3 -c \"{holder}\" > /tmp/holder.out 2>&1")
    deadline = time.monotonic() + 20
    while not desk.sh("cat /tmp/holder.out", check=False).stdout.strip().isdigit():
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert kind(call(desk, "stop", {"name": "left"}, desk.token, "left-behind-cl")) == "busy"
    desk.sh(f"kill -9 {desk.sh('cat /tmp/holder.out').stdout.strip()}")
    assert do(desk, desk.token, "stop", "left", "left-after-cl")["state"] == "done"


RAW = (
    "import json, socket, struct, sys, time\n"
    "s = socket.socket(socket.AF_UNIX); s.settimeout(20); s.connect('/run/cl/broker/broker.sock')\n"
    "kind = sys.argv[1]\n"
    "started = time.monotonic()\n"
    "if kind == 'big': s.sendall(struct.pack('>I', 70000) + b'x' * 10)\n"
    "elif kind == 'not-json': s.sendall(struct.pack('>I', 5) + b'{nope')\n"
    "elif kind == 'a-list': s.sendall(struct.pack('>I', 2) + b'[]')\n"
    "elif kind == 'half': s.sendall(struct.pack('>I', 100) + b'{\"verb\"')\n"
    "elif kind == 'drip':\n"
    "    s.sendall(struct.pack('>I', 100))\n"
    "    try:\n"
    "        for _ in range(15):\n"
    "            time.sleep(1); s.sendall(b'x')\n"
    "    except OSError:\n"
    "        pass\n"
    "elif kind == 'nothing': pass\n"
    "try:\n"
    "    data = s.recv(65536)\n"
    "except Exception as exc:\n"
    "    data = repr(exc).encode()\n"
    "print(json.dumps({'took': time.monotonic() - started, 'reply': json.loads(data[4:]) if data[:1] == b'\\x00' and len(data) > 4 else data.decode()}))\n"
)


def test_what_is_not_a_request_is_refused_or_dropped_and_the_broker_goes_on(desk):
    def raw(what):
        return json.loads(run("docker", "exec", desk.name, "python3", "-c", RAW, what).stdout)

    assert raw("big")["reply"] == {"ok": False, "error": {"kind": "bad_request", "message": "request too large: 70000 > 65536"}}
    assert raw("not-json")["reply"]["error"]["kind"] == "bad_request"
    assert raw("a-list")["reply"] == {"ok": False, "error": {"kind": "bad_request", "message": "request must be a JSON object"}}
    for silent in ("half", "nothing", "drip"):
        dropped = raw(silent)
        assert "ok" not in dropped["reply"] and 3 < dropped["took"] < 12, dropped   # dropped at the request's deadline, without an answer
    assert kind(call(desk, "list")) == "ok"
    assert kind(call(desk, "docker", {"args": ["ps"]}, desk.token)) == "unknown_verb"


def test_only_root_in_the_container_reaches_the_broker(desk):
    def as_nobody():
        return run("docker", "exec", "-u", "65534", desk.name, "python3", "/opt/cl/broker.py", "call", "list", check=False)

    assert desk.sh("stat -c %a /run/cl/broker/broker.sock").stdout.strip() == "600"
    refused = as_nobody()
    assert refused.returncode != 0 and "PermissionError" in refused.stderr          # the socket's mode
    desk.sh("chmod 666 /run/cl/broker/broker.sock")
    try:
        answered = as_nobody()
        assert answered.returncode == 0
        assert json.loads(answered.stdout) == {"ok": False, "error": {"kind": "forbidden", "message": "peer is not the owning user"}}   # the peer check
    finally:
        desk.sh("chmod 600 /run/cl/broker/broker.sock")
    assert "peer refused: uid 65534" in run("docker", "logs", desk.name).stderr
    assert kind(call(desk, "list")) == "ok"


# --- the broker and its operations end, and the init picks up -------------------------------

def test_the_broker_and_its_operations_survive_each_other_and_a_restart_clears_them(outer, sandbox_image, tmp_path):
    tools = fake_tools(tmp_path, {"docker": SLOW_DOCKER})
    one = outer().start(env=ENV, extra=("-v", f"{tools}:/fake:ro"))
    assert one.wait_sandboxes()["state"] == "ready"
    one.load_image(sandbox_image)
    one.sandbox_image = sandbox_image
    one.wait_broker()
    set_password(one, PASSWORD)
    token = login(one)["result"]["token"]
    do(one, token, "create", "p", "p-create")
    do(one, token, "stop", "p", "p-stop")

    # The broker is killed while an operation runs: the init starts it again, the operation
    # goes on and is still answered for, the project is still taken, and the sessions are gone.
    one.sh("touch /tmp/slow")
    assert call(one, "start", {"name": "p"}, token, "p-start")["ok"]
    run("docker", "exec", one.name, "pkill", "-f", "broker.py serve")
    deadline = time.monotonic() + 30
    while "the broker is not running; starting it again" not in run("docker", "logs", one.name).stderr:
        assert time.monotonic() < deadline, "the init did not start the broker again"
        time.sleep(0.3)
    while run("docker", "exec", one.name, "python3", "/opt/cl/broker.py", "call", "list", check=False).returncode != 0:
        assert time.monotonic() < deadline, "the new broker does not answer"
        time.sleep(0.3)
    one.wait_broker()
    assert kind(call(one, "lifecycle_active", {}, token)) == "unauthorized"
    token = login(one)["result"]["token"]
    assert call(one, "lifecycle_active", {}, token)["result"]["ops"] == [{"op_id": "p-start", "project": "p", "verb": "start", "state": "running"}]
    assert kind(call(one, "stop", {"name": "p"}, token, "p-stop-refused")) == "busy"
    one.sh("rm -f /tmp/slow")
    assert finished(one, "p-start") == {"op_id": "p-start", "project": "p", "verb": "start", "state": "done", "msg": "p is running."}

    # An operation's child is killed: the operation is failed, not running for ever, and the
    # project is free again.
    do(one, token, "stop", "p", "p-stop-2")
    one.sh("touch /tmp/slow")
    assert call(one, "start", {"name": "p"}, token, "p-killed")["ok"]
    run("docker", "exec", one.name, "pkill", "-9", "-f", "__run-op")
    assert finished(one, "p-killed", timeout=20) == {"op_id": "p-killed", "project": "p", "verb": "start", "state": "failed",
                                                     "msg": "The operation ended without a result."}
    assert call(one, "build_alive", {"op_id": "p-killed"}, token)["result"] == {"alive": False}
    assert call(one, "lifecycle_active", {}, token)["result"]["ops"] == []

    # clanker-learn itself is stopped in the middle of an operation: it stops cleanly, and at
    # the next start nothing of the operation is left, neither its hold on the project nor its log.
    assert call(one, "start", {"name": "p"}, token, "p-cut")["ok"]
    boot = one.state()["boot"]
    run("docker", "stop", "-t", "60", one.name)
    assert run("docker", "inspect", "-f", "{{.State.ExitCode}}", one.name).stdout.strip() == "0"
    assert "could not be stopped" not in run("docker", "logs", one.name).stderr
    run("docker", "start", one.name)
    assert one.wait_sandboxes(after_boot=boot)["state"] == "ready"
    one.wait_broker()
    gone = call(one, "op", {"op_id": "p-cut"})
    assert kind(gone) == "validation" and "no operation p-cut" in gone["error"]["message"]
    assert one.sh("ls /run/cl/broker/oplogs /run/cl/broker-ops").stdout.split() == ["/run/cl/broker-ops:", "/run/cl/broker/oplogs:"]
    token = login(one)["result"]["token"]                                        # the password is in the data volume
    assert do(one, token, "stop", "p", "p-after")["state"] == "done"             # the project is free


def test_passwords_sessions_and_a_broker_that_cannot_run(outer):
    one = outer().start()
    assert one.wait_sandboxes()["state"] == "ready"
    assert one.wait_broker()["broker"] == "up"
    assert any("broker.py serve" in line for line in processes(one))

    # Before a password is set nobody logs in, and the answer does not say why; the log does.
    assert login(one) == {"ok": False, "error": {"kind": "auth", "message": "invalid credentials"}}
    assert "no password is set" in run("docker", "logs", one.name).stderr
    assert "at least 8" in set_password(one, "short", expect=1).stderr
    assert login(one, "short")["ok"] is False
    set_password(one, PASSWORD)
    assert one.sh("stat -c '%a %U' /data/broker/passwd.json").stdout.split() == ["600", "root"]
    assert PASSWORD not in one.sh("cat /data/broker/passwd.json").stdout
    token = login(one)["result"]["token"]
    assert kind(call(one, "lifecycle_active", {}, token)) == "ok"
    assert kind(login(one, "not the password")) == "auth" and kind(login(one, PASSWORD + "\n")) == "auth"

    # A new password ends every session, and the old one no longer logs in.
    set_password(one, "another password")
    assert kind(call(one, "lifecycle_active", {}, token)) == "unauthorized"
    assert kind(login(one)) == "auth"
    token = login(one, "another password")["result"]["token"]
    assert call(one, "logout", {}, token) == {"ok": True, "result": {"logged_out": True}}
    assert kind(call(one, "lifecycle_active", {}, token)) == "unauthorized"

    # A broker that cannot run is not a refusal: the container stays ready, and says so.
    one.sh("rm -rf /data/broker && touch /data/broker")
    run("docker", "exec", one.name, "pkill", "-f", "broker.py serve")
    state = one.wait_broker("down", timeout=30)
    assert state["state"] == "ready" and state["sandboxes"] == "started"
    time.sleep(8)                                                                # it is tried again, and still cannot
    assert one.state()["broker"] == "down" and one.state()["state"] == "ready"
    assert one.cl("list") == []
    one.sh("rm -f /data/broker")
    assert one.wait_broker("up", timeout=30)["state"] == "ready"                  # and comes back by itself

    # After a refusal nothing is started again: no broker runs, and the state does not name one.
    one.sh("kill -9 $(pidof dockerd)")
    state = one.wait(wanted=("refused",), timeout=60)
    time.sleep(7)
    assert not any("broker.py" in line for line in processes(one))
    assert "broker" not in one.state() and "sandboxes" not in one.state()
