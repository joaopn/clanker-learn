"""The outer container, started for real in privileged mode."""
import base64
import json
import time

import pytest
from conftest import IMAGE, PEER_PORT, fetch_code, fetch_from, fetched, probe_from, run

pytestmark = pytest.mark.integration

GUARD_FIRSTS = {
    "iptables DOCKER-USER": "-A DOCKER-USER -j CL-GUARD",
    "iptables INPUT": "-A INPUT -j CL-GUARD-IN",
    "iptables FORWARD": "-A FORWARD -j DOCKER-USER",
    "ip6tables DOCKER-USER": "-A DOCKER-USER -j CL-GUARD6",
    "ip6tables INPUT": "-A INPUT -j CL-GUARD6",
    "ip6tables FORWARD": "-A FORWARD -j DOCKER-USER",
}
PATH_WITH_FAKES = "/fake:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def first_rules(outer):
    """The first rule of each chain the guard hangs from."""
    return {
        hook: outer.sh(f"{hook.split()[0]} -S {hook.split()[1]} | grep -m1 '^-A'", check=False).stdout.strip()
        for hook in GUARD_FIRSTS
    }


def processes(outer):
    """The command line of every process in the outer container."""
    code = (
        "import glob\n"
        "for path in glob.glob('/proc/[0-9]*/cmdline'):\n"
        "    try:\n"
        "        line = open(path, 'rb').read().replace(b'\\0', b' ').decode(errors='replace').strip()\n"
        "    except OSError:\n"
        "        continue\n"
        "    if line and 'import glob' not in line:\n"
        "        print(line)\n"
    )
    return run("docker", "exec", outer.name, "python3", "-c", code).stdout.splitlines()


def daemon_runs(outer):
    return "dockerd" in processes(outer)


def sleeper_runs(outer):
    return any(line.endswith("busybox sleep 100000") for line in processes(outer))


def start_sleeper(outer, name="sandbox", extra=()):
    outer.inner("run", "-d", "--name", name, *extra, "cl-selftest", "/bin/busybox", "sleep", "100000")
    assert sleeper_runs(outer)


def fake_tools(tmp_path, scripts):
    """A directory to put first on PATH, holding the given scripts."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    tools.chmod(0o755)
    for name, text in scripts.items():
        (tools / name).write_text(text)
        (tools / name).chmod(0o755)
    return tools


# --- a good start --------------------------------------------------------------------------

def test_comes_up_ready_and_says_so_on_its_port(ready, peer):
    ready.wait_sandboxes()
    state = ready.wait_broker()
    assert daemon_runs(ready)
    assert len(state.pop("boot")) == 32
    assert state == {"state": "ready", "mode": "privileged", "reason": "", "limits": True, "sandboxes": "started", "broker": "up"}
    status, body = fetch_from(peer["name"], f"http://{ready.address()}:8080/")
    assert (status, body.strip()) == (200, "clanker-learn: ready")
    status, body = fetch_from(peer["name"], f"http://{ready.address()}:8080/state.json")
    assert status == 200 and '"mode": "privileged"' in body


def test_daemon_runs_containers_on_runc_under_var_lib_docker(ready):
    info = ready.inner("info", "--format", "{{.DefaultRuntime}} {{.DockerRootDir}}").stdout.split()
    assert info == ["runc", "/var/lib/docker"]
    assert ready.inner("run", "--rm", "--network", "none", "cl-selftest", "/bin/busybox", "echo", "ran").stdout.strip() == "ran"


def test_image_has_no_zfs_tool_and_containerd_boots_at_once(ready):
    assert ready.sh("command -v zfs", check=False).returncode != 0
    boot = ready.sh("grep -o 'containerd successfully booted in [0-9.]*' /var/log/dockerd.log | tail -1").stdout.split()
    assert float(boot[-1]) < 2.0


def test_limits_reach_an_inner_container(ready):
    out = ready.inner("run", "--rm", "--network", "none", "-m", "64m", "--pids-limit", "50", "cl-selftest",
                      "/bin/busybox", "cat", "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/pids.max").stdout.split()
    assert out == ["67108864", "50"]


def test_guard_rules_are_first_in_their_hooks(ready):
    assert first_rules(ready) == GUARD_FIRSTS
    inner = ready.sh("iptables -S CL-GUARD-INNER").stdout
    for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"):
        assert f"-d {private} -j DROP" in inner
    assert ready.sh("ip6tables -S CL-GUARD6 | grep -c -- '-j DROP'").stdout.strip() == "2"


def test_rules_are_in_place_before_the_daemon_starts(outer, tmp_path):
    """A stand-in for dockerd notes the rules it finds, one file per chain, then becomes the
    daemon."""
    noted = {"iptables DOCKER-USER": "v4-user", "iptables INPUT": "v4-input", "ip6tables DOCKER-USER": "v6-user", "ip6tables INPUT": "v6-input"}
    script = "#!/bin/sh\nmkdir -p /run/at-daemon-start\n"
    for hook, name in noted.items():
        script += f"{hook.split()[0]} -S {hook.split()[1]} > /run/at-daemon-start/{name} 2>&1\n"
    script += "iptables -S CL-GUARD-INNER > /run/at-daemon-start/v4-inner 2>&1\n"
    script += 'exec /usr/bin/dockerd "$@"\n'
    tools = fake_tools(tmp_path, {"dockerd": script})
    one = outer().start(env={"CL_MODE": "privileged", "PATH": PATH_WITH_FAKES}, extra=("-v", f"{tools}:/fake:ro"))
    assert one.wait()["state"] == "ready", one.state()
    for hook, name in noted.items():
        appended = [line for line in one.sh(f"cat /run/at-daemon-start/{name}").stdout.splitlines() if line.startswith("-A ")]
        assert appended[:1] == [GUARD_FIRSTS[hook]], hook
    assert "-A CL-GUARD-INNER -d 10.0.0.0/8 -j DROP" in one.sh("cat /run/at-daemon-start/v4-inner").stdout.splitlines()
    # and the daemon adopted the chains it found: its own jump to them is first, ours still inside
    assert first_rules(one) == GUARD_FIRSTS


# --- what a sandbox can reach ---------------------------------------------------------------

def test_inner_container_cannot_reach_the_outer_container_or_its_neighbours(ready, peer):
    # the neighbour answers the outer container itself on each port: the timeouts below are the guard's
    for kind, port, answer in (("tcp", peer["port"], "connected"), ("udp", peer["port"], "answered"), ("tcp", 53, "connected"), ("udp", 53, "answered")):
        assert probe_from(ready.name, kind, peer["address"], port) == answer
        assert ready.reach(peer["address"], port, kind) == "timeout", (kind, port)
    docker0 = ready.sh("ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1").stdout.strip()
    assert docker0
    for own_address in (ready.sandbox_gateway, docker0, ready.address()):
        assert probe_from(ready.name, "tcp", own_address, 8080) == "connected"
        assert ready.reach(own_address, 8080) == "timeout", own_address
    # the same from the inner daemon's default bridge, which the rules name separately
    assert ready.reach(peer["address"], peer["port"], network="bridge") == "timeout"
    assert ready.reach(docker0, 8080, network="bridge") == "timeout"


def test_inner_container_resolves_a_name_and_reaches_the_internet(ready):
    code = "import urllib.request; print(urllib.request.urlopen('http://deb.debian.org/debian/dists/trixie/Release', timeout=10).status)"
    done = ready.run_in_sandbox("python3", "-c", code, check=False)
    assert done.stdout.strip() == "200", done.stderr[-400:]


def test_a_published_port_answers_from_outside_and_not_from_another_inner_container(ready, peer):
    """The DNAT rule: what the outer container publishes is for its outside, not a way for
    one sandbox to reach another through the outer container's address."""
    ready.inner("run", "-d", "--name", "published", "--network", "sandbox-net", "-p", "8123:8000", "--entrypoint", "python3",
                "cl-outer:test", "-m", "http.server", "8000", "--directory", "/opt/cl/selftest")
    try:
        deadline = time.monotonic() + 20
        while fetch_from(peer["name"], f"http://{ready.address()}:8123/")[0] != 200:
            assert time.monotonic() < deadline, "the published port did not answer from outside"
            time.sleep(0.3)
        # from the same network the port is the outer container's own listener (the INPUT chain);
        # from another network of the inner daemon it is a DNAT'd connection (the DNAT rule)
        assert ready.reach(ready.address(), 8123) == "timeout"
        assert ready.reach(ready.sandbox_gateway, 8123) == "timeout"
        assert ready.reach(ready.address(), 8123, network="bridge") == "timeout"
        # and the container behind the port is not reachable from another network directly either
        own = ready.inner("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "published").stdout.strip()
        assert ready.reach(own, 8000) == "connected"
        assert ready.reach(own, 8000, network="bridge") == "timeout"
    finally:
        ready.inner("rm", "-f", "published", check=False)


def test_a_private_resolver_is_reachable_on_port_53_only(outer, peer, tmp_path):
    """A machine whose resolver is on a private address (a home router): the sandbox may ask it
    names and nothing else."""
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(f"nameserver {peer['address']}\n")
    resolv.chmod(0o644)
    one = outer().start(extra=("-v", f"{resolv}:/etc/resolv.conf:ro"))
    assert one.wait()["state"] == "ready", one.state()
    one.load_probe()
    assert one.reach(peer["address"], 53, "tcp") == "connected"
    assert one.reach(peer["address"], 53, "udp") == "answered"
    assert one.reach(peer["address"], PEER_PORT, "tcp") == "timeout"
    assert one.reach(peer["address"], PEER_PORT, "udp") == "timeout"


def in_a_user_namespace():
    with open("/proc/self/uid_map") as fh:
        return fh.read().split()[:3] != ["0", "0", "4294967295"]


@pytest.mark.skipif(not in_a_user_namespace(), reason="the tests themselves are not inside a sysbox container")
def test_declared_sysbox_runs_inner_containers_on_crun(outer):
    """Not the sysbox mode itself, which needs a sysbox host: a privileged container nested in a
    sysbox one cannot be told from sysbox, so declaring it here runs the configuration the
    sysbox mode uses (crun for inner containers)."""
    one = outer().start(env={"CL_MODE": "sysbox"})
    state = one.wait()
    assert (state["state"], state["mode"]) == ("ready", "sysbox"), state
    assert one.inner("info", "--format", "{{.DefaultRuntime}}").stdout.strip() == "crun"
    assert one.inner("run", "--rm", "--network", "none", "cl-selftest", "/bin/busybox", "echo", "ran").stdout.strip() == "ran"


# --- refusals --------------------------------------------------------------------------------

def test_refuses_without_a_declared_mode_and_answers_with_the_reason(outer, peer):
    one = outer().start(env={})
    state = one.wait()
    assert state["state"] == "refused"
    assert "CL_MODE is not set" in state["reason"]
    assert "--runtime=sysbox-runc" in state["reason"] and "--privileged" in state["reason"]
    assert not daemon_runs(one)
    status, body = fetch_from(peer["name"], f"http://{one.address()}:8080/")
    assert status == 503 and state["reason"] in body
    assert state["reason"] in run("docker", "logs", one.name).stderr


def test_refuses_when_neither_on_sysbox_nor_privileged(outer):
    one = outer().start(privileged=False)
    state = one.wait()
    assert state["state"] == "refused"
    assert "neither on sysbox nor privileged" in state["reason"]
    assert not daemon_runs(one)


FAKE_RESTORE = {
    "fails": "#!/bin/sh\necho 'fake iptables-restore: refusing' >&2\nexit 1\n",
    # reports success and leaves every chain empty
    "empty-chains": "#!/bin/sh\ncat >/dev/null\nfor c in CL-GUARD CL-GUARD-INNER CL-GUARD-IN; do /usr/sbin/iptables -N $c; done\nexit 0\n",
}


@pytest.mark.parametrize("kind, said", [
    ("fails", "could not be applied"),
    ("empty-chains", "are not in place after applying them"),
])
def test_starts_nothing_when_the_guard_rules_fail(outer, tmp_path, kind, said):
    tools = fake_tools(tmp_path, {"iptables-restore": FAKE_RESTORE[kind]})
    one = outer().start(env={"CL_MODE": "privileged", "PATH": PATH_WITH_FAKES}, extra=("-v", f"{tools}:/fake:ro"))
    state = one.wait()
    assert state["state"] == "refused"
    assert said in state["reason"] and "nothing was started" in state["reason"]
    # no daemon was ever started, so no container can have run
    assert not daemon_runs(one)
    assert one.sh("test -e /var/log/dockerd.log", check=False).returncode != 0


def test_refuses_without_its_own_init_and_ends_nothing(outer, peer):
    """Another entrypoint (as --pid=host would): the init cannot know the processes it sees are
    its own, so it starts nothing and ends nothing. A bystander process shows the second half
    for the refusal; the stop is covered by test_a_stop_in_someone_elses_namespace_ends_nothing.
    The network is the container's own, so the port still says why."""
    one = outer().start(extra=("--entrypoint", "sh"),
                        command=("-c", "/bin/busybox sleep 100000 & exec python3 /opt/cl/init.py"))
    state = one.wait()
    assert state["state"] == "refused"
    assert "its own process namespace and init" in state["reason"]
    assert not daemon_runs(one)
    assert sleeper_runs(one)
    assert "refusing" not in run("docker", "logs", one.name).stderr
    status, body = fetch_from(peer["name"], f"http://{one.address()}:8080/")
    assert status == 503 and state["reason"] in body


# The two flags that would make the container act on the machine itself. A start with one of
# them cannot be tried on the test daemon: if the refusal were broken, the dev container's own
# network or control groups would be the ones acted on. So an ordinary outer container stands
# in for the machine, and the candidate is started by its inner daemon.

RECORDING_TOOL = (
    '#!/bin/sh\n'
    'echo "$(basename "$0") $*" >> /ran/log\n'
    'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin exec "$(basename "$0")" "$@"\n'
)
TOOLS_THAT_ACT = ("iptables", "iptables-restore", "ip6tables", "ip6tables-restore", "dockerd")
CANDIDATE = "candidate"


def machine_with_candidate(outer, *flags):
    """A ready outer container as the machine, and inside it the image started with `flags`.
    Every tool by which the candidate could act on a network or start a daemon is replaced by
    one that writes its name to the machine's /cl-ran/log first."""
    machine = outer().start()
    assert machine.wait()["state"] == "ready", machine.state()
    machine.load_probe()
    stub = base64.b64encode(RECORDING_TOOL.encode()).decode()
    machine.sh("mkdir -p /cl-fake /cl-ran && " + " && ".join(
        f"echo {stub} | base64 -d > /cl-fake/{tool} && chmod 755 /cl-fake/{tool}" for tool in TOOLS_THAT_ACT))
    machine.inner("run", "-d", "--name", CANDIDATE, "--privileged", *flags, "-e", "CL_MODE=privileged",
                  "-e", f"PATH={PATH_WITH_FAKES}", "-v", "/cl-fake:/fake:ro", "-v", "/cl-ran:/ran", IMAGE)
    return machine


def candidate_state(machine, timeout=90):
    deadline = time.monotonic() + timeout
    state = {"state": "unknown"}
    while time.monotonic() < deadline:
        done = machine.inner("exec", CANDIDATE, "cat", "/run/cl/state.json", check=False)
        try:
            state = json.loads(done.stdout)
        except ValueError:
            state = {"state": "unknown", "reason": done.stderr}
        if state["state"] in ("ready", "refused"):
            return state
        time.sleep(0.5)
    logs = machine.inner("logs", "--tail", "30", CANDIDATE, check=False)
    raise AssertionError(f"the candidate reached neither ready nor refused; state {state}; log:\n{logs.stderr[-2000:]}")


def tools_the_candidate_ran(machine):
    return machine.sh("cat /cl-ran/log 2>/dev/null", check=False).stdout


def assert_the_recording_works(machine):
    """An empty record means something only if the tools do record."""
    machine.inner("exec", CANDIDATE, "iptables", "-S", "INPUT")
    assert tools_the_candidate_ran(machine) == "iptables -S INPUT\n"


def test_refuses_the_machines_network_and_touches_nothing(outer):
    """--network=host: nothing is run that could change a rule or start a daemon, and no port
    is opened on the machine. The container stays up and its log says why."""
    machine = machine_with_candidate(outer, "--network=host", "-e", "CL_UI_PORT=8081")
    state = candidate_state(machine)
    assert state["state"] == "refused", state
    assert "--network=host" in state["reason"] and "docker0" in state["reason"]
    assert tools_the_candidate_ran(machine) == ""
    listening = machine.sh("ss -ltnH").stdout
    assert ":8080 " in listening and ":8081 " not in listening, listening
    assert machine.inner("inspect", "-f", "{{.State.Status}}", CANDIDATE).stdout.strip() == "running"
    assert state["reason"] in machine.inner("logs", CANDIDATE).stderr
    assert_the_recording_works(machine)


def test_refuses_the_machines_control_groups_and_starts_nothing(outer):
    """--cgroupns=host: refused before a rule is applied or a daemon started. The network is
    the candidate's own, so its port says why."""
    machine = machine_with_candidate(outer, "--cgroupns=host")
    state = candidate_state(machine)
    assert state["state"] == "refused", state
    assert "--cgroupns=private" in state["reason"] and state["mode"] == "privileged"
    assert tools_the_candidate_ran(machine) == ""
    status, body = fetched(machine.inner("exec", CANDIDATE, "python3", "-c", fetch_code("http://127.0.0.1:8080/")).stdout)
    assert status == 503 and state["reason"] in body
    assert_the_recording_works(machine)


def test_an_unforeseen_fault_is_a_refusal_with_a_reason(outer, tmp_path, peer):
    """Something nobody planned for (here the daemon's configuration cannot be written): the
    port still answers with a reason, and the traceback is in the log."""
    blocker = tmp_path / "daemon.json"
    blocker.mkdir()
    one = outer().start(extra=("-v", f"{blocker}:/etc/docker/daemon.json"))
    state = one.wait()
    assert state["state"] == "refused" and "IsADirectoryError" in state["reason"], state
    assert fetch_from(peer["name"], f"http://{one.address()}:8080/")[0] == 503
    assert "Traceback" in run("docker", "logs", one.name).stderr
    assert not daemon_runs(one)


def test_refuses_instead_of_crashing_when_the_daemon_does_not_answer(outer, tmp_path):
    """A `docker` that hangs: the start is refused with a reason on the port, not ended by a
    traceback."""
    tools = fake_tools(tmp_path, {"docker": "#!/bin/sh\nexec sleep 100000\n"})
    one = outer().start(env={"CL_MODE": "privileged", "CL_DAEMON_WAIT_S": "25", "PATH": PATH_WITH_FAKES},
                        extra=("-v", f"{tools}:/fake:ro"))
    state = one.wait(timeout=150)
    assert state["state"] == "refused" and "was not ready after 25 seconds" in state["reason"], state
    assert not daemon_runs(one)
    assert "Traceback" not in run("docker", "logs", one.name).stderr


# --- while ready -----------------------------------------------------------------------------

@pytest.mark.parametrize("damage", [
    "iptables -D DOCKER-USER -j CL-GUARD && iptables -F CL-GUARD-IN",
    "iptables -I DOCKER-USER 1 -j ACCEPT",
    "iptables -I INPUT 1 -j ACCEPT",
    "iptables -I FORWARD 1 -j ACCEPT",
    "iptables -D CL-GUARD-INNER -d 10.0.0.0/8 -j DROP",
    "ip6tables -F CL-GUARD6",
    "ip6tables -I INPUT 1 -j ACCEPT",
    "ip6tables -D DOCKER-USER -j CL-GUARD6",
])
def test_guard_rules_come_back_when_they_are_damaged(ready, damage):
    try:
        ready.sh(damage)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            intact = (
                first_rules(ready) == GUARD_FIRSTS
                and "-i docker0 -j DROP" in ready.sh("iptables -S CL-GUARD-IN").stdout
                and "-d 10.0.0.0/8 -j DROP" in ready.sh("iptables -S CL-GUARD-INNER").stdout
                and "-i docker0 -j DROP" in ready.sh("ip6tables -S CL-GUARD6").stdout
            )
            if intact:
                break
            time.sleep(0.5)
        else:
            raise AssertionError(f"the guard rules were not restored after: {damage}")
        assert ready.state()["state"] == "ready"
    finally:
        # a stray rule that outranked ours is left where it is, below the jump; take it out
        ready.sh("for t in iptables ip6tables; do for c in DOCKER-USER INPUT FORWARD; do while $t -D $c -j ACCEPT 2>/dev/null; do :; done; done; done")


def test_ends_every_inner_container_when_the_rules_cannot_be_restored(outer, tmp_path, peer):
    tools = fake_tools(tmp_path, {})
    one = outer().start(env={"CL_MODE": "privileged", "CL_WATCH_S": "1", "PATH": PATH_WITH_FAKES},
                        extra=("-v", f"{tools}:/fake:ro"))
    assert one.wait()["state"] == "ready"
    start_sleeper(one)
    # from now on the rules cannot be written, and they are gone
    fake_tools(tmp_path, {"iptables-restore": FAKE_RESTORE["fails"]})
    one.sh("iptables -F CL-GUARD-INNER")
    state = one.wait(wanted=("refused",), timeout=60)
    assert "every sandbox was stopped" in state["reason"]
    assert not sleeper_runs(one)
    assert not daemon_runs(one)
    assert fetch_from(peer["name"], f"http://{one.address()}:8080/")[0] == 503


def test_ends_every_inner_container_when_the_daemon_dies(outer, peer):
    """A container's processes outlive the daemon that started them; they must not."""
    one = outer().start()
    assert one.wait()["state"] == "ready"
    start_sleeper(one)
    one.sh("kill -9 $(pidof dockerd)")
    state = one.wait(wanted=("refused",), timeout=60)
    assert "The inner Docker daemon stopped" in state["reason"]
    assert not sleeper_runs(one)
    assert [line for line in processes(one) if "containerd" in line] == []
    status, body = fetch_from(peer["name"], f"http://{one.address()}:8080/")
    assert status == 503 and "The inner Docker daemon stopped" in body
    # and a restart recovers
    assert one.restart()["state"] == "ready"


# --- restarts --------------------------------------------------------------------------------

def test_inner_state_survives_a_stop_and_start_and_a_recreate(outer):
    one = outer().start()
    assert one.wait()["state"] == "ready"
    one.inner("run", "-d", "--name", "sandbox", "cl-selftest", "/bin/busybox", "sh", "-c", "echo installed > /marker; sleep 100000")
    # clanker-learn never sets a restart policy; this container stands for one that got one anyway
    one.inner("run", "-d", "--name", "stray", "--restart", "always", "cl-selftest", "/bin/busybox", "sleep", "100000")
    time.sleep(1)

    assert one.restart()["state"] == "ready"
    # nothing runs that the init did not start: the daemon brought "stray" back, and the init
    # stopped it before it declared itself ready
    assert one.inner("ps", "-q").stdout.strip() == ""
    assert "stopping 1 container" in run("docker", "logs", one.name).stderr
    assert first_rules(one) == GUARD_FIRSTS
    one.inner("update", "--restart", "no", "stray")
    one.inner("start", "sandbox")
    assert one.inner("exec", "sandbox", "/bin/busybox", "cat", "/marker").stdout.strip() == "installed"

    one.remove(volume=False)
    one.start()
    assert one.wait()["state"] == "ready"
    assert one.inner("ps", "-q").stdout.strip() == ""
    one.inner("start", "sandbox")
    assert one.inner("exec", "sandbox", "/bin/busybox", "cat", "/marker").stdout.strip() == "installed"


def test_stop_ends_many_containers_within_the_stop_timeout(outer):
    """Containers are stopped together, not one after another: eight that ignore TERM take one
    grace period, not eight."""
    one = outer().start()
    assert one.wait()["state"] == "ready"
    for number in range(8):
        one.inner("run", "-d", "--name", f"deaf-{number}", "cl-selftest", "/bin/busybox", "sh", "-c", "trap '' TERM; while :; do sleep 1; done")
    started = time.monotonic()
    run("docker", "stop", "-t", "60", one.name)
    assert time.monotonic() - started < 40
    assert run("docker", "inspect", "-f", "{{.State.ExitCode}}", one.name).stdout.strip() == "0"


def test_starts_again_after_being_killed(outer, tmp_path):
    """A kill leaves the daemon's pid file behind. If the process id it names is in use at the
    next start, dockerd refuses to start; whether it is depends on timing, so the test plants
    the case: a pid file naming pid 1, which always runs."""
    one = outer().start()
    boot = one.wait()["boot"]
    start_sleeper(one)
    run("docker", "kill", one.name)
    stale = tmp_path / "docker.pid"
    stale.write_text("1")
    run("docker", "cp", str(stale), f"{one.name}:/run/docker.pid")
    run("docker", "start", one.name)
    state = one.wait(after_boot=boot)
    assert state["state"] == "ready", state
    assert one.inner("ps", "-q").stdout.strip() == ""


def test_ten_starts_in_a_row(outer):
    one = outer().start()
    assert one.wait()["state"] == "ready"
    for start in range(2, 11):
        state = one.restart()
        assert state["state"] == "ready", f"start {start}: {state}"
