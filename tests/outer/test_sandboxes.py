"""Projects and their sandboxes, in a real outer container in privileged mode.

The sandbox image is built once on the test daemon and loaded into the outer container, so the
core finds it and builds nothing; one test (the last) lets the core build it. A "peer" is a
container of the inner daemon on a project's network: it stands in for the tutor service,
which will log in over SSH, and for the web server.
"""
import json
import subprocess
import time

import pytest
from conftest import PROBE, Outer, run
from test_outer_container import FAKE_RESTORE, PATH_WITH_FAKES, fake_tools, in_a_user_namespace, processes

pytestmark = pytest.mark.integration

FRONT = "cl-test-front"
PEER_LISTENS_ON = 9000


@pytest.fixture(scope="module")
def lab(image, peer, sandbox_image):
    """One outer container with the sandbox image loaded, shared by the tests that each use a
    project of their own. At the end: the core must not have built an image in it."""
    one = Outer().start()
    try:
        assert one.wait_sandboxes()["state"] == "ready", one.state()
        one.load_image(sandbox_image)
        one.sandbox_image = sandbox_image
        yield one
        images = one.inner("images", "--format", "{{.Repository}}:{{.Tag}}", "cl-sandbox").stdout.split()
        assert images == [sandbox_image], f"an image was built: {images}"
        assert one.inner("network", "inspect", "cl-build", check=False).returncode != 0, "a build was started"
    finally:
        one.remove()


def a_lab(outer, sandbox_image, **start):
    """An outer container of a test's own, with the sandbox image loaded."""
    one = outer().start(**start)
    assert one.wait_sandboxes()["state"] == "ready", one.state()
    one.load_image(sandbox_image)
    one.sandbox_image = sandbox_image
    return one


def peer_of(lab, project):
    """A container on the project's network, from the sandbox image (it has ssh and python)."""
    name = f"peer-{project}"
    lab.inner("run", "-d", "--name", name, "--network", f"cl-net-{project}", "--entrypoint", "sleep", lab.sandbox_image, "infinity")
    return name


def ssh(lab, peer_name, project, command, known_hosts=None, check=True):
    """Log into the project's sandbox from the peer as the tutor will: with the project's key,
    as the sandbox user, refusing a host whose key is not the one the core stored."""
    keys = f"/data/tutor/keys/{project}"
    lab.sh(f"docker cp {keys}/id_ed25519 {peer_name}:/tmp/key && docker cp {known_hosts or keys + '/known_hosts'} {peer_name}:/tmp/known_hosts")
    return lab.inner("exec", peer_name, "ssh", "-i", "/tmp/key", "-o", "UserKnownHostsFile=/tmp/known_hosts", "-o", "StrictHostKeyChecking=yes",
                     "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", f"learner@cl-sb-{project}", command, check=check)


def reach(lab, container, host, port, kind="tcp"):
    """What a container of the inner daemon gets from host:port, in one word."""
    out = lab.inner("exec", container, "python3", "-c", PROBE, kind, host, str(port)).stdout.strip()
    assert out in ("connected", "answered", "timeout", "refused"), out
    return out


def limits_inside(lab, project):
    read = lab.sandbox(project, "cat", "/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/cpu.max").stdout.split("\n")
    return {"pids": read[0], "memory": read[1], "cpu": read[2]}


def container_id(lab, project):
    return lab.inner("inspect", "-f", "{{.Id}}", f"cl-sb-{project}").stdout.strip()


def left_of_folders(lab, project):
    return lab.sh(f"ls -d /data/core/projects/{project}.json /data/projects/{project} /data/homes/{project} /data/core/provision/{project} "
                  f"/data/core/keys/{project} /data/tutor/keys/{project} 2>/dev/null", check=False).stdout.split()


def left_of(lab, project):
    """What exists under a project's name in the outer container and its daemon."""
    found = left_of_folders(lab, project)
    found += [kind for kind in ("container", "network")
              if lab.inner(kind, "inspect", f"cl-{'sb' if kind == 'container' else 'net'}-{project}", check=False).returncode == 0]
    return found


# --- lifecycle -------------------------------------------------------------------------------

def test_create_gives_a_sandbox_the_tutor_can_enter(lab):
    status = lab.cl("create", "life")
    assert status["state"] == "running" and status["wanted"] == "running" and status["image"] == lab.sandbox_image
    assert status["limits"] == {"pids": 16384, "memory": None, "cpus": None}
    peer_name = peer_of(lab, "life")
    assert ssh(lab, peer_name, "life", "id -un; sudo -n id -un; pwd; ls -a ~").stdout.split()[:3] == ["learner", "root", "/home/learner"]
    assert ".bashrc" in ssh(lab, peer_name, "life", "ls -a ~").stdout.split()   # the new home was filled
    ssh(lab, peer_name, "life", "echo from the sandbox > /workspace/note")
    assert lab.sh("cat /data/projects/life/note").stdout == "from the sandbox\n"
    # The host is checked, not trusted at first sight: with another key on record the login fails.
    lab.sh("ssh-keygen -q -t ed25519 -N '' -f /tmp/other && printf 'cl-sb-life %s\\n' \"$(cut -d' ' -f1,2 /tmp/other.pub)\" > /tmp/wrong_hosts")
    refused = ssh(lab, peer_name, "life", "true", known_hosts="/tmp/wrong_hosts", check=False)
    assert refused.returncode != 0 and "Host key verification failed" in refused.stderr
    assert [p["name"] for p in lab.cl("list")].count("life") == 1


def test_stop_and_start_keep_the_container_and_what_was_installed_in_it(lab):
    lab.cl("create", "keeps")
    lab.sandbox("keeps", "sh", "-c", "echo installed > /opt/marker")
    before = container_id(lab, "keeps")
    assert lab.cl("stop", "keeps")["state"] == "stopped" and lab.cl("status", "keeps")["wanted"] == "stopped"
    assert lab.cl("start", "keeps")["state"] == "running" and lab.cl("status", "keeps")["wanted"] == "running"
    assert container_id(lab, "keeps") == before
    assert lab.sandbox("keeps", "cat", "/opt/marker").stdout == "installed\n"


def test_destroy_leaves_nothing_whatever_the_sandbox_left_behind(lab):
    lab.cl("create", "gone")
    lab.cl("create", "bystander")
    # Root in the sandbox leaves files the outer container must still remove, and a link that
    # points, seen from the outer container, at the core's own folder.
    lab.sandbox("gone", "sh", "-c", "mkdir -p /workspace/locked/deep && echo x > /workspace/locked/deep/file && chmod 000 /workspace/locked "
                "&& ln -s /data/core /workspace/core-link && ln -s /data/core /home/learner/core-link && echo root > /workspace/by-root")
    assert sorted(left_of(lab, "gone")) == sorted(["/data/core/projects/gone.json", "/data/projects/gone", "/data/homes/gone",
                                                   "/data/core/provision/gone", "/data/core/keys/gone", "/data/tutor/keys/gone", "container", "network"])
    # Something else sits on its network, as the web server and the tutor will: it is taken
    # off the network, not removed.
    other = peer_of(lab, "gone")
    assert lab.cl("destroy", "gone") == {"name": "gone", "state": "destroyed"}
    assert left_of(lab, "gone") == []
    assert lab.inner("inspect", "-f", "{{.State.Running}} {{len .NetworkSettings.Networks}}", other).stdout.split() == ["true", "0"]
    assert "/data/core/projects/bystander.json" in left_of(lab, "bystander")  # the link's target is untouched
    assert lab.cl("status", "bystander")["state"] == "running"
    assert "no project named gone" in lab.cl("destroy", "gone", check=False)["error"]


def test_provisioning_is_put_back_at_every_start(lab):
    lab.cl("create", "again")
    peer_name = peer_of(lab, "again")
    lab.sandbox("again", "rm", "/home/learner/.ssh/authorized_keys")
    lab.sh("find /data/core/provision/again -type f -delete")
    assert ssh(lab, peer_name, "again", "true", check=False).returncode != 0
    lab.cl("stop", "again")
    lab.cl("start", "again")
    assert ssh(lab, peer_name, "again", "id -un").stdout.strip() == "learner"


def test_a_start_finishes_a_creation_that_was_cut_short(lab):
    lab.sh("python3 -c \"import sys; sys.path.insert(0, '/opt/cl'); import core; core.write_record(core.new_record('cut'))\"")
    lab.sh("mkdir -p /data/projects/cut")   # made, and the call killed before it was handed to the sandbox user
    assert lab.cl("status", "cut")["state"] == "missing"
    assert lab.cl("start", "cut")["state"] == "running"
    assert lab.sh("stat -c %u /data/projects/cut /data/homes/cut").stdout.split() == ["1000", "1000"]
    assert ssh(lab, peer_of(lab, "cut"), "cut", "id -un").stdout.strip() == "learner"


# --- fencing ---------------------------------------------------------------------------------

def test_a_sandbox_reaches_the_internet_and_its_own_network_and_nothing_else(lab, peer):
    a, b = lab.cl("create", "fence-a"), lab.cl("create", "fence-b")
    # The peer sits on A's network and on another one, as the web server and the tutor will.
    peer_name = peer_of(lab, "fence-a")
    lab.inner("network", "create", FRONT)
    lab.inner("network", "connect", FRONT, peer_name)
    lab.inner("exec", "-d", peer_name, "python3", "-m", "http.server", str(PEER_LISTENS_ON), "--directory", "/opt")
    addresses = lab.inner("inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}", peer_name).stdout.split()
    near, far = dict(x.split("=") for x in addresses)["cl-net-fence-a"], dict(x.split("=") for x in addresses)[FRONT]
    gateway = lab.inner("network", "inspect", "cl-net-fence-a", "-f", "{{(index .IPAM.Config 0).Gateway}}").stdout.strip()
    sandbox = "cl-sb-fence-a"
    deadline = time.monotonic() + 20
    while reach(lab, peer_name, far, PEER_LISTENS_ON) != "connected":
        assert time.monotonic() < deadline, "the peer's listener did not come up"
        time.sleep(0.3)

    assert reach(lab, sandbox, "deb.debian.org", 443) == "connected"
    assert reach(lab, sandbox, near, PEER_LISTENS_ON) == "connected"
    assert reach(lab, sandbox, far, PEER_LISTENS_ON) == "timeout"            # the peer, at its other network's address
    assert reach(lab, "cl-sb-fence-b", b["address"], 22) == "connected"      # B's sshd listens there...
    assert reach(lab, sandbox, b["address"], 22) == "timeout"                # ...and A cannot reach it
    assert reach(lab, sandbox, gateway, 8080) == "timeout"                   # the outer container, at A's gateway
    assert reach(lab, sandbox, lab.address(), 8080) == "timeout"             # and at its own address
    assert reach(lab, sandbox, peer["address"], peer["port"]) == "timeout"   # a neighbour at a private address
    assert a["address"] and a["address"] != b["address"]

    raw = "import socket\ntry:\n    socket.socket(socket.AF_PACKET, socket.SOCK_RAW); print('opened')\nexcept PermissionError:\n    print('refused')\n"
    assert lab.sandbox("fence-a", "python3", "-c", raw).stdout.strip() == "refused"        # as root
    assert lab.inner("exec", peer_name, "python3", "-c", raw).stdout.strip() == "opened"   # a container with Docker's defaults can
    assert lab.sandbox("fence-a", "ping", "-c1", "-W3", near, user="learner", check=False).returncode == 0


RECORDING_DOCKER = "#!/bin/sh\necho \"$@\" >> /tmp/docker-calls\nexec /usr/bin/docker \"$@\"\n"


def test_a_refusal_ends_the_sandboxes_and_nothing_is_destroyed_or_started_blind(outer, sandbox_image, tmp_path):
    one = a_lab(outer, sandbox_image)
    assert one.cl("create", "wanted")["state"] == "running"
    assert one.cl("create", "doomed")["state"] == "running"
    assert len([line for line in processes(one) if "sandbox_init" in line and "tini" not in line]) == 2

    # The daemon dies: the sandboxes' processes would outlive it. They are ended with it.
    one.sh("kill -9 $(pidof dockerd)")
    state = one.wait(wanted=("refused",), timeout=60)
    assert "The inner Docker daemon stopped" in state["reason"]
    assert not any("sandbox_init" in line for line in processes(one))
    assert "sandboxes" not in state                                    # nothing is starting or started any more
    # With no daemon to say whether the container is gone, a destroy removes nothing.
    assert "error" in one.cl("destroy", "doomed", check=False)
    assert "error" in one.cl("status", "doomed", check=False)
    assert {"/data/core/projects/doomed.json", "/data/projects/doomed", "/data/homes/doomed"} <= set(left_of_folders(one, "doomed"))

    # Started again where the rules cannot be applied: "wanted" is recorded as running, and the
    # daemon is not even asked.
    run("docker", "stop", "-t", "60", one.name)
    one.remove(volume=False)
    tools = fake_tools(tmp_path, {"iptables-restore": FAKE_RESTORE["fails"], "docker": RECORDING_DOCKER})
    one.start(env={"CL_MODE": "privileged", "CL_WATCH_S": "1", "PATH": PATH_WITH_FAKES}, extra=("-v", f"{tools}:/fake:ro"))
    assert one.wait()["state"] == "refused"
    time.sleep(3)
    assert json.loads(one.sh("cat /data/core/projects/wanted.json").stdout)["wanted"] == "running"
    one.sh("test ! -e /tmp/docker-calls")
    state = one.state()
    assert state["state"] == "refused" and "sandboxes" not in state

    # With the rules back, the start brings back what should run, and not the project whose
    # destroy failed: that one is finished by running destroy again.
    one.remove(volume=False)
    (tools / "iptables-restore").unlink()
    one.start(env={"CL_MODE": "privileged", "CL_WATCH_S": "1", "PATH": PATH_WITH_FAKES}, extra=("-v", f"{tools}:/fake:ro"))
    assert one.wait_sandboxes()["sandboxes"] == "started"
    calls = one.sh("cat /tmp/docker-calls").stdout
    assert "start cl-sb-wanted" in calls and "start cl-sb-doomed" not in calls
    assert one.cl("status", "wanted")["state"] == "running"
    assert one.cl("status", "doomed")["wanted"] == "destroying"
    assert "is being destroyed" in one.cl("start", "doomed", check=False)["error"]
    assert one.cl("destroy", "doomed")["state"] == "destroyed" and left_of(one, "doomed") == []


# --- limits ----------------------------------------------------------------------------------

def test_limits_are_the_ones_the_sandbox_runs_under_and_can_be_changed(lab):
    lab.cl("create", "lim", "--pids", "512", "--memory", "256m", "--cpus", "0.5")
    assert limits_inside(lab, "lim") == {"pids": "512", "memory": str(256 * 1024 ** 2), "cpu": "50000 100000"}
    changed = lab.cl("limits", "lim", "--pids", "1024", "--memory", "1g", "--cpus", "2")   # memory beyond twice what it was
    assert changed["notes"] == [] and changed["limits"] == {"pids": 1024, "memory": 1024 ** 3, "cpus": 2.0}
    assert limits_inside(lab, "lim") == {"pids": "1024", "memory": str(1024 ** 3), "cpu": "200000 100000"}
    assert lab.cl("limits", "lim", "--pids", "none", "--cpus", "none")["notes"] == []
    assert limits_inside(lab, "lim") == {"pids": "max", "memory": str(1024 ** 3), "cpu": "max 100000"}
    lab.cl("limits", "lim", "--cpus", "1.5", "--pids", "300")
    lab.cl("stop", "lim")
    lab.cl("limits", "lim", "--memory", "512m")   # while stopped
    lab.cl("start", "lim")
    assert limits_inside(lab, "lim") == {"pids": "300", "memory": str(512 * 1024 ** 2), "cpu": "150000 100000"}
    # A start applies what the record says, whatever the container was left with (an earlier
    # change that could not be applied, say).
    lab.cl("stop", "lim")
    lab.sh("python3 -c \"import sys; sys.path.insert(0, '/opt/cl'); import core; r = core.read_record('lim'); r['limits']['pids'] = 400; core.write_record(r)\"")
    lab.cl("start", "lim")
    assert limits_inside(lab, "lim")["pids"] == "400"
    waits = lab.cl("limits", "lim", "--memory", "none")
    assert len(waits["notes"]) == 1 and "next recreated" in waits["notes"][0] and waits["limits"]["memory"] is None
    assert limits_inside(lab, "lim")["memory"] == str(512 * 1024 ** 2)
    assert "at least" in lab.cl("limits", "lim", "--pids", "3", check=False)["error"]


# --- the files server ------------------------------------------------------------------------

RAW_GET = (
    "import socket, sys\n"
    "s = socket.create_connection((sys.argv[1], 7070), timeout=5)\n"
    "s.sendall((sys.argv[2] + ' ' + sys.argv[3] + ' HTTP/1.0\\r\\n\\r\\n').encode())\n"
    "data = b''\n"
    "while True:\n"
    "    chunk = s.recv(65536)\n"
    "    if not chunk:\n"
    "        break\n"
    "    data += chunk\n"
    "head, _, body = data.partition(b'\\r\\n\\r\\n')\n"
    "print(head.split()[1].decode()); print(body.decode(errors='replace'))\n"
)


def test_the_files_server_serves_the_workspace_and_nothing_else(lab):
    lab.cl("create", "files")
    peer_name = peer_of(lab, "files")
    lab.sandbox("files", "sh", "-c", "mkdir /workspace/sub && echo hello > /workspace/sub/hello.txt && ln -s /etc/hostname /workspace/leak "
                "&& echo top-secret > /srv/outside && chmod 644 /srv/outside && ln -s /srv/outside /workspace/leak2", user="root")

    def get(path, method="GET"):
        out = lab.inner("exec", peer_name, "python3", "-c", RAW_GET, "cl-sb-files", method, path).stdout.split("\n", 1)
        return out[0], out[1].strip()

    assert get("/sub/hello.txt") == ("200", "hello")
    assert lab.sandbox("files", "cat", "/workspace/leak2", user="learner").stdout == "top-secret\n"   # the server's user can read the target
    for refused in ("/leak", "/leak2", "/../srv/outside", "/sub/../../srv/outside", "/%2e%2e/srv/outside", "/sub", "/"):
        assert get(refused)[0] == "404", refused
        assert "top-secret" not in get(refused)[1]
    # Ended (a learner's pkill, say), it is started again.
    lab.sandbox("files", "pkill", "-f", "files_server")
    deadline = time.monotonic() + 30
    while lab.inner("exec", peer_name, "python3", "-c", RAW_GET, "cl-sb-files", "GET", "/sub/hello.txt", check=False).stdout.split("\n", 1)[0] != "200":
        assert time.monotonic() < deadline, "the files server did not come back"
        time.sleep(0.5)
    assert get("/sub/hello.txt", "POST")[0] == "501"
    assert get("/sub/hello.txt", "PUT")[0] == "501"
    assert lab.sandbox("files", "cat", "/workspace/sub/hello.txt").stdout == "hello\n"
    owner = lab.sandbox("files", "sh", "-c", "ps -eo user:20,args | grep '[f]iles_server'").stdout.split()[0]
    assert owner == "learner"


# --- names and locks -------------------------------------------------------------------------

def test_a_name_is_not_reused_while_something_of_it_is_left(lab):
    lab.cl("create", "left")
    lab.sh("rm /data/core/projects/left.json")   # as if a destroy had stopped half-way
    refused = lab.cl("create", "left", check=False)["error"]
    assert "is left" in refused and "destroy left" in refused
    assert lab.cl("destroy", "left")["state"] == "destroyed" and left_of(lab, "left") == []
    assert lab.cl("create", "left")["state"] == "running"
    assert "exists" in lab.cl("create", "left", check=False)["error"]
    assert "A project name is" in lab.cl("create", "Not_A_Name", check=False)["error"]


def test_two_creations_of_one_name_give_one_project(lab):
    both = [subprocess.Popen(["docker", "exec", lab.name, "cl", "create", "twin"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = [p.communicate(timeout=300) + (p.returncode,) for p in both]
    assert sorted(code for _, _, code in results) == [0, 1], results
    assert "exists" in next(err for _, err, code in results if code == 1)
    assert lab.cl("status", "twin")["state"] == "running"


def test_a_killed_operation_leaves_no_lock_behind(lab):
    lab.cl("create", "lock")
    holder = "import os, sys, time; sys.path.insert(0, '/opt/cl'); import core; ctx = core.locked('lock'); ctx.__enter__(); print(os.getpid(), flush=True); time.sleep(1000)"
    run("docker", "exec", "-d", lab.name, "sh", "-c", f"python3 -c \"{holder}\" > /tmp/holder.out 2>&1")
    deadline = time.monotonic() + 20
    while not lab.sh("cat /tmp/holder.out", check=False).stdout.strip().isdigit():
        assert time.monotonic() < deadline, lab.sh("cat /tmp/holder.out", check=False).stdout
        time.sleep(0.2)
    holder_pid = lab.sh("cat /tmp/holder.out").stdout.strip()
    waiting = subprocess.Popen(["docker", "exec", lab.name, "cl", "stop", "lock"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(3)
    assert waiting.poll() is None, "the lock was not held"   # it waits for the holder...
    assert lab.inner("inspect", "-f", "{{.State.Running}}", "cl-sb-lock").stdout.strip() == "true"   # ...having stopped nothing
    lab.sh(f"kill -9 {holder_pid}")
    out, err = waiting.communicate(timeout=60)                # ...and goes on once the holder is killed
    assert waiting.returncode == 0 and '"state": "stopped"' in out, err


# --- the outer container stops and starts ----------------------------------------------------

def test_a_restart_of_clanker_learn_brings_back_what_was_running(outer, sandbox_image):
    one = a_lab(outer, sandbox_image)
    one.cl("create", "up")
    one.cl("create", "down")
    one.cl("stop", "down")
    one.sandbox("up", "sh", "-c", "echo installed > /opt/marker")
    before = container_id(one, "up")
    # A destroy that was cut short after the container went: it must not come back as if it
    # were a creation that was cut short.
    one.cl("create", "half")
    one.sh("python3 -c \"import sys; sys.path.insert(0, '/opt/cl'); import core; core.write_record(dict(core.read_record('half'), wanted='destroying'))\"")
    one.inner("rm", "-f", "cl-sb-half")

    def as_before():
        assert one.cl("status", "up")["state"] == "running" and container_id(one, "up") == before
        assert one.sandbox("up", "cat", "/opt/marker").stdout == "installed\n"
        assert one.cl("status", "down")["state"] == "stopped" and one.cl("status", "down")["wanted"] == "stopped"
        assert one.cl("status", "half")["state"] == "missing"

    boot = one.state()["boot"]
    one.restart()
    assert one.wait_sandboxes(after_boot=boot)["sandboxes"] == "started"
    as_before()

    boot = one.state()["boot"]
    run("docker", "kill", one.name)
    run("docker", "start", one.name)
    assert one.wait_sandboxes(after_boot=boot)["sandboxes"] == "started"
    as_before()

    # A project that cannot be started is its own trouble: the others start, and the state says so.
    one.sh("echo '{' > /data/core/projects/broken.json")
    boot = one.state()["boot"]
    one.restart()
    state = one.wait_sandboxes(after_boot=boot)
    assert state["state"] == "ready" and state["sandboxes"] == "some failed"
    as_before()
    assert "broken could not be started" in run("docker", "logs", one.name).stderr


# --- recreate, the setup recipe, the listed services, cl-expose -------------------------------

def setup_becomes(lab, project, wanted, timeout=90):
    """Wait for the sandbox's init to say how the setup recipe went."""
    deadline = time.monotonic() + timeout
    while True:
        state = lab.cl("status", project)["setup"]
        if state == wanted:
            return state
        assert time.monotonic() < deadline, f"the setup recipe of {project} is {state!r}, not {wanted!r}"
        time.sleep(0.5)


def as_learner(lab, project, script):
    return lab.sandbox(project, "bash", "-lc", script, user="learner")


def test_a_recreate_gives_a_new_container_and_runs_the_recipe_once(lab):
    lab.cl("create", "rec", "--memory", "256m")
    assert setup_becomes(lab, "rec", "none") == "none"                      # no recipe yet: nothing to run
    assert "next recreated" in lab.cl("limits", "rec", "--memory", "none")["notes"][0]
    assert limits_inside(lab, "rec")["memory"] == str(256 * 1024 ** 2)      # still there: only a recreate removes it
    lab.sandbox("rec", "sh", "-c", "echo installed > /opt/marker")
    as_learner(lab, "rec", "mkdir -p /workspace/.learn && echo mine > ~/in-home && echo work > /workspace/work && "
               "printf 'echo run >> /workspace/.learn/runs\\nsudo touch /opt/from-recipe\\nid -un > /workspace/.learn/ran-as\\n' > /workspace/.learn/setup.sh")
    before = container_id(lab, "rec")

    lab.cl("stop", "rec")
    lab.cl("start", "rec")                                                  # a plain stop and start: the recipe is not run
    time.sleep(2)                                                           # the state still says "none" from before: give a wrong init time to act
    assert setup_becomes(lab, "rec", "none") == "none"
    assert lab.sh("cat /data/projects/rec/.learn/runs", check=False).stdout == ""

    assert lab.cl("recreate", "rec")["state"] == "running"
    assert container_id(lab, "rec") != before
    assert limits_inside(lab, "rec")["memory"] == "max"
    setup_becomes(lab, "rec", "done")
    assert lab.sh("cat /data/projects/rec/.learn/runs /data/projects/rec/.learn/ran-as").stdout.split() == ["run", "learner"]
    assert lab.sandbox("rec", "ls", "/opt").stdout.split() == ["cl", "from-recipe"]           # the recipe's file; the earlier marker is gone
    assert as_learner(lab, "rec", "cat ~/in-home /workspace/work").stdout.split() == ["mine", "work"]
    assert "running the setup recipe" in lab.inner("logs", "cl-sb-rec").stderr

    lab.cl("stop", "rec")
    lab.cl("start", "rec")
    setup_becomes(lab, "rec", "done")
    time.sleep(2)
    assert lab.sh("cat /data/projects/rec/.learn/runs").stdout.split() == ["run"]              # still once

    # A recreate that was cut short after the old container went is completed by running it again.
    lab.inner("rm", "-f", "cl-sb-rec")
    assert lab.cl("status", "rec")["state"] == "missing"
    assert lab.cl("recreate", "rec")["state"] == "running"
    setup_becomes(lab, "rec", "done")
    assert lab.sh("cat /data/projects/rec/.learn/runs").stdout.split() == ["run", "run"]


def test_a_recipe_that_fails_says_so_and_the_services_still_start(lab):
    lab.cl("create", "bad")
    peer_name = peer_of(lab, "bad")
    as_learner(lab, "bad", "mkdir -p /workspace/.learn && printf 'echo before the fault\\nwhile [ ! -e /workspace/go ]; do sleep 0.2; done\\ntouch /workspace/recipe-over\\nexit 3\\n' > /workspace/.learn/setup.sh && "
               "printf 'web: python3 -m http.server 8123 --bind 0.0.0.0 --directory /workspace\\n"
               "order: test -e /workspace/recipe-over && echo after > /workspace/order || echo before > /workspace/order\\n' > /workspace/.learn/services")
    address = lab.cl("recreate", "bad")["address"]
    setup_becomes(lab, "bad", "running")                                    # the recreate did not wait for the recipe
    assert reach(lab, peer_name, address, 8123) == "refused"                # and the services wait for it
    as_learner(lab, "bad", "touch /workspace/go")
    setup_becomes(lab, "bad", "failed (exit 3)")
    assert "before the fault" in lab.sandbox("bad", "cat", "/var/log/cl/setup.log").stdout
    deadline = time.monotonic() + 30
    while reach(lab, peer_name, address, 8123) != "connected":
        assert time.monotonic() < deadline, "the listed service did not start after the failed recipe"
        time.sleep(0.5)
    deadline = time.monotonic() + 30
    while lab.sh("test -e /data/projects/bad/order", check=False).returncode != 0:
        assert time.monotonic() < deadline, "the second listed service did not run"
        time.sleep(0.5)
    assert lab.sh("cat /data/projects/bad/order").stdout == "after\n"       # the services wait for the recipe


def test_a_services_file_that_is_a_pipe_does_not_hold_the_sandbox_up(lab):
    """What the sandbox user puts at that path is read as that user, with a time limit: the
    init goes on, and still hears a stop."""
    lab.cl("create", "pipe")
    as_learner(lab, "pipe", "mkdir -p /workspace/.learn && mkfifo /workspace/.learn/services")
    lab.cl("stop", "pipe")
    lab.cl("start", "pipe")
    time.sleep(8)                                                           # past the time limit of the read
    started = time.monotonic()
    lab.cl("stop", "pipe")
    assert time.monotonic() - started < 8, "the init did not hear the stop"
    assert lab.inner("inspect", "-f", "{{.State.ExitCode}}", "cl-sb-pipe").stdout.strip() == "0"


def test_listed_services_come_back_at_every_start_and_are_asked_to_end_at_a_stop(lab):
    lab.cl("create", "serv")
    peer_name = peer_of(lab, "serv")
    services = ("# what the tutor started\n"
                "web: python3 -m http.server 8123 --bind 0.0.0.0 --directory /workspace\n"
                "polite: trap 'echo ended > /workspace/polite-ended; exit 0' TERM; while true; do sleep 1; done\n"
                "not a service line\n")
    as_learner(lab, "serv", f"mkdir -p /workspace/.learn && printf '%s' \"{services}\" > /workspace/.learn/services")

    def web_answers():
        address = lab.cl("status", "serv")["address"]
        deadline = time.monotonic() + 30
        while reach(lab, peer_name, address, 8123) != "connected":
            assert time.monotonic() < deadline, "the listed service does not answer"
            time.sleep(0.5)
        return lab.sandbox("serv", "sh", "-c", "ps -eo user:20,args | grep '[h]ttp.server 8123'").stdout.split()[0]

    assert reach(lab, peer_name, lab.cl("status", "serv")["address"], 8123) == "refused"      # listed, not started yet
    lab.cl("stop", "serv")
    lab.cl("start", "serv")
    assert web_answers() == "learner"
    assert "started the service web" in lab.inner("logs", "cl-sb-serv").stderr and "not a service line, skipped" in lab.inner("logs", "cl-sb-serv").stderr
    assert lab.sh("ls /data/projects/serv/.learn/logs").stdout.split() == ["polite.log", "web.log"]

    assert lab.sh("cat /data/projects/serv/polite-ended", check=False).stdout == ""
    lab.cl("stop", "serv")
    assert lab.sh("cat /data/projects/serv/polite-ended").stdout == "ended\n"                # asked, not just killed
    lab.cl("start", "serv")
    assert web_answers() == "learner"
    lab.cl("recreate", "serv")
    assert web_answers() == "learner"


def test_a_loopback_only_server_is_reachable_after_cl_expose_and_again_after_a_restart(lab):
    lab.cl("create", "loop")
    peer_name = peer_of(lab, "loop")
    address = lab.cl("status", "loop")["address"]
    as_learner(lab, "loop", "mkdir -p /workspace/.learn /workspace/site && echo from loopback > /workspace/site/index.html")
    lab.inner("exec", "-d", "-u", "learner", "cl-sb-loop", "python3", "-m", "http.server", "8456", "--bind", "127.0.0.1", "--directory", "/workspace/site")
    deadline = time.monotonic() + 20
    while "from loopback" not in as_learner(lab, "loop", "curl -s --max-time 3 http://127.0.0.1:8456/ || true").stdout:
        assert time.monotonic() < deadline, "the loopback server did not come up"
        time.sleep(0.3)
    assert reach(lab, peer_name, address, 8456) == "refused"                                 # nothing outside the sandbox reaches it

    said = as_learner(lab, "loop", "cl-expose 8456 7070").stdout
    assert "port 8456: now reachable" in said and "port 7070:" in said and "reachable already" in said   # 7070 is the files server's
    assert reach(lab, peer_name, address, 8456) == "connected"
    fetched = lab.inner("exec", peer_name, "curl", "-s", "--max-time", "5", f"http://{address}:8456/").stdout
    assert fetched == "from loopback\n"
    assert "port 8456: already taken" in as_learner(lab, "loop", "cl-expose 8456").stdout     # asked twice: harmless
    assert as_learner(lab, "loop", "cl-expose nonsense; echo $?").stdout.strip() == "2"

    # Listed, both come back at the next start.
    as_learner(lab, "loop", "printf 'site: python3 -m http.server 8456 --bind 127.0.0.1 --directory /workspace/site\\ntab: cl-expose 8456\\n' > /workspace/.learn/services")
    lab.cl("stop", "loop")
    lab.cl("start", "loop")
    deadline = time.monotonic() + 30
    while lab.inner("exec", peer_name, "curl", "-s", "--max-time", "3", f"http://{address}:8456/", check=False).stdout != "from loopback\n":
        assert time.monotonic() < deadline, "the exposed server did not come back"
        time.sleep(0.5)
    # `tab` ended as soon as it had detached its relay: the init collects it, it does not stay as a zombie.
    time.sleep(2)
    assert "Z" not in lab.sandbox("loop", "ps", "-eo", "stat=").stdout


WRITE_A_LONG_SERVICES_FILE = (
    "head = 'ok: touch /workspace/ok-ran\\n'\n"
    "cut = 'cut: touch /workspace/cut-ran'\n"                      # the size limit falls right after this...
    "rest = ' /workspace/never-named\\n'\n"                        # ...so the line as read would be another command
    "padding = '#' * (64 * 1024 - len(head) - len(cut) - 1) + '\\n'\n"
    "open('/workspace/.learn/services', 'w').write(head + padding + cut + rest)\n"
)


def test_a_services_file_cut_in_a_sandbox_runs_no_half_command(lab):
    lab.cl("create", "cutf")
    as_learner(lab, "cutf", "mkdir -p /workspace/.learn")
    lab.sandbox("cutf", "python3", "-c", WRITE_A_LONG_SERVICES_FILE, user="learner")
    assert int(lab.sh("stat -c %s /data/projects/cutf/.learn/services").stdout) > 64 * 1024
    lab.cl("stop", "cutf")
    lab.cl("start", "cutf")
    deadline = time.monotonic() + 30
    while lab.sh("test -e /data/projects/cutf/ok-ran", check=False).returncode != 0:
        assert time.monotonic() < deadline, "the first listed service did not run"
        time.sleep(0.5)
    time.sleep(2)
    assert lab.sh("ls /data/projects/cutf").stdout.split() == ["ok-ran"]
    assert "longer than 65536 bytes" in lab.inner("logs", "cl-sb-cutf").stderr


FAKE_DOCKER_BUILD_FAILS = "#!/bin/sh\nif [ \"$1\" = build ]; then echo 'fake docker: build fails' >&2; exit 1; fi\nexec /usr/bin/docker \"$@\"\n"


def test_a_recreate_that_cannot_get_its_image_removes_nothing(outer, sandbox_image, tmp_path):
    tools = fake_tools(tmp_path, {"docker": FAKE_DOCKER_BUILD_FAILS})
    one = a_lab(outer, sandbox_image, env={"CL_MODE": "privileged", "CL_WATCH_S": "1", "PATH": PATH_WITH_FAKES}, extra=("-v", f"{tools}:/fake:ro"))
    one.cl("create", "stays")
    one.sandbox("stays", "sh", "-c", "echo installed > /opt/marker")
    before = container_id(one, "stays")
    one.inner("rmi", "-f", sandbox_image)                 # the image is gone; the running sandbox keeps what it was made from
    assert one.inner("image", "inspect", sandbox_image, check=False).returncode != 0
    failed = one.cl("recreate", "stays", check=False)["error"]
    assert "could not be built" in failed and "fake docker: build fails" in failed
    assert container_id(one, "stays") == before and one.cl("status", "stays")["state"] == "running"
    assert one.sandbox("stays", "cat", "/opt/marker").stdout == "installed\n"


# --- a failed creation, the other runtime, the build -----------------------------------------

FAKE_DOCKER_CREATE_FAILS = (
    "#!/bin/sh\n"
    "if [ \"$1\" = create ]; then\n"
    "    /usr/bin/docker network ls --format '{{.Name}}' > /tmp/networks-at-create\n"
    "    echo 'fake docker: create fails' >&2\n"
    "    exit 1\n"
    "fi\n"
    "exec /usr/bin/docker \"$@\"\n"
)


def test_a_creation_that_fails_leaves_nothing(outer, sandbox_image, tmp_path):
    tools = fake_tools(tmp_path, {"docker": FAKE_DOCKER_CREATE_FAILS})
    one = a_lab(outer, sandbox_image, env={"CL_MODE": "privileged", "CL_WATCH_S": "1", "PATH": PATH_WITH_FAKES}, extra=("-v", f"{tools}:/fake:ro"))
    failed = one.cl("create", "fails", check=False)["error"]
    assert "fake docker: create fails" in failed
    assert "cl-net-fails" in one.sh("cat /tmp/networks-at-create").stdout.split()   # it had got as far as the network
    assert left_of(one, "fails") == []
    assert one.cl("list") == []


@pytest.mark.skipif(not in_a_user_namespace(), reason="the tests themselves are not inside a sysbox container")
def test_a_sandbox_also_lives_on_the_runtime_of_the_sysbox_mode(outer, sandbox_image):
    """CL_MODE=sysbox in a privileged container: the inner containers run on crun, as they will
    on sysbox. One pass of the lifecycle and the limits."""
    one = a_lab(outer, sandbox_image, env={"CL_MODE": "sysbox", "CL_WATCH_S": "1"})
    one.cl("create", "crun", "--pids", "256", "--memory", "256m", "--cpus", "0.5")
    assert one.inner("inspect", "-f", "{{.HostConfig.Runtime}}", "cl-sb-crun").stdout.strip() == "crun"
    assert limits_inside(one, "crun") == {"pids": "256", "memory": str(256 * 1024 ** 2), "cpu": "50000 100000"}
    one.cl("limits", "crun", "--memory", "1g", "--cpus", "1.5", "--pids", "none")
    assert limits_inside(one, "crun") == {"pids": "max", "memory": str(1024 ** 3), "cpu": "150000 100000"}
    # crun does not lift a processor limit from a running container: the core says so, and it
    # is gone after the next start.
    waits = one.cl("limits", "crun", "--cpus", "none")
    assert len(waits["notes"]) == 1 and "next starts" in waits["notes"][0]
    assert limits_inside(one, "crun")["cpu"] == "150000 100000"
    assert ssh(one, peer_of(one, "crun"), "crun", "sudo -n id -un").stdout.strip() == "root"
    one.cl("stop", "crun")
    assert one.cl("start", "crun")["state"] == "running"
    assert limits_inside(one, "crun") == {"pids": "max", "memory": str(1024 ** 3), "cpu": "max 100000"}
    assert one.cl("destroy", "crun")["state"] == "destroyed" and left_of(one, "crun") == []


def test_the_first_project_builds_the_sandbox_image_and_the_second_does_not(outer, sandbox_image, tmp_path):
    tools = fake_tools(tmp_path, {"docker": RECORDING_DOCKER})
    one = outer().start(env={"CL_MODE": "privileged", "CL_WATCH_S": "1", "PATH": PATH_WITH_FAKES}, extra=("-v", f"{tools}:/fake:ro"))
    assert one.wait_sandboxes()["state"] == "ready"
    assert one.inner("images", "-q", "cl-sandbox").stdout.strip() == ""
    done = run("docker", "exec", one.name, "cl", "create", "first", timeout=2400)
    assert "building the sandbox image" in done.stderr and '"state": "running"' in done.stdout
    builds = [line for line in one.sh("cat /tmp/docker-calls").stdout.splitlines() if line.startswith("build ")]
    assert builds == [f"build --network cl-build -t {sandbox_image} /opt/cl/sandbox"]   # once, on the fenced network made for it
    assert one.inner("images", "--format", "{{.Repository}}:{{.Tag}}", "cl-sandbox").stdout.split() == [sandbox_image]
    built = one.inner("images", "-q", "cl-sandbox").stdout.strip()
    started = time.monotonic()
    again = run("docker", "exec", one.name, "cl", "create", "second", timeout=300)
    assert "building" not in again.stderr and time.monotonic() - started < 60
    assert one.inner("images", "-q", "cl-sandbox").stdout.strip() == built
    assert one.sandbox("first", "id", "-un", user="learner").stdout.strip() == "learner"
