"""The parts of the outer container that are decided without a daemon."""
import os

import guard
import init
import pytest

PRIVILEGED_STATUS = "Name:\tpython3\nCapEff:\t000001ffffffffff\n"
PLAIN_STATUS = "Name:\tpython3\nCapEff:\t00000000a80425fb\n"
IDENTITY_MAP = "         0          0 4294967295\n"
NAMESPACED_MAP = "         0     165536      65536\n"


def test_only_what_comes_from_an_inner_bridge_is_judged():
    assert guard.chains()[guard.FWD_CHAIN] == [
        "-A CL-GUARD -i docker0 -j CL-GUARD-INNER",
        "-A CL-GUARD -i br-+ -j CL-GUARD-INNER",
    ]


def test_private_ranges_are_dropped_after_replies_published_ports_and_inner_bridges():
    rules = guard.chains()[guard.INNER_CHAIN]
    assert rules[:4] == [
        "-A CL-GUARD-INNER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        "-A CL-GUARD-INNER -m conntrack --ctstate DNAT -j DROP",
        "-A CL-GUARD-INNER -o docker0 -j RETURN",
        "-A CL-GUARD-INNER -o br-+ -j RETURN",
    ]
    assert rules[4:] == [f"-A CL-GUARD-INNER -d {r} -j DROP" for r in guard.PRIVATE_RANGES]
    assert "100.64.0.0/10" in guard.PRIVATE_RANGES and len(guard.PRIVATE_RANGES) == 5


def test_no_rule_names_an_interface_of_the_outer_container():
    """The rules hold whatever uplink the container has: none of them is keyed on it."""
    for rules in guard.chains(("192.168.1.1",)).values():
        for rule in rules:
            words = rule.split()
            named = [words[i + 1] for i, w in enumerate(words) if w in ("-i", "-o")]
            assert all(name in guard.INNER_BRIDGES for name in named), rule


def test_only_private_ipv4_nameservers_are_resolvers_to_except():
    text = (
        "# comment\nnameserver 127.0.0.11\nnameserver 192.168.1.1\nnameserver 8.8.8.8\nnameserver fe80::1\n"
        "nameserver 192.168.1.1\nnameserver 10.0.0.2 extra\nsearch lan\nnameserver\nnameserver not-an-address\n"
        "nameserver 100.100.100.100\n"
    )
    assert guard.private_resolvers(text) == ("192.168.1.1", "10.0.0.2", "100.100.100.100")
    assert guard.private_resolvers("nameserver 127.0.0.53\n") == ()


def test_a_private_resolver_is_excepted_on_port_53_before_the_drops():
    rules = guard.chains(("192.168.1.1",))[guard.INNER_CHAIN]
    excepted = [
        "-A CL-GUARD-INNER -d 192.168.1.1/32 -p udp -m udp --dport 53 -j RETURN",
        "-A CL-GUARD-INNER -d 192.168.1.1/32 -p tcp -m tcp --dport 53 -j RETURN",
    ]
    assert rules[4:6] == excepted
    assert rules[6] == "-A CL-GUARD-INNER -d 10.0.0.0/8 -j DROP"
    assert [r for r in rules if "RETURN" in r and " -d " in r] == excepted
    assert guard.chains()[guard.INNER_CHAIN] == rules[:4] + rules[6:]


def test_input_chain_drops_what_comes_from_an_inner_bridge():
    assert guard.chains()[guard.IN_CHAIN] == [
        "-A CL-GUARD-IN -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        "-A CL-GUARD-IN -i docker0 -j DROP",
        "-A CL-GUARD-IN -i br-+ -j DROP",
    ]


def test_ipv6_from_an_inner_bridge_is_dropped_whatever_it_is():
    assert guard.chains6() == {"CL-GUARD6": ["-A CL-GUARD6 -i docker0 -j DROP", "-A CL-GUARD6 -i br-+ -j DROP"]}
    assert guard.restore_text(guard.chains6()).splitlines() == [
        "*filter", ":CL-GUARD6 - [0:0]", "-A CL-GUARD6 -i docker0 -j DROP", "-A CL-GUARD6 -i br-+ -j DROP", "COMMIT"]


def test_every_jump_that_must_be_first():
    v4 = [("iptables", "DOCKER-USER", "CL-GUARD"), ("iptables", "INPUT", "CL-GUARD-IN")]
    v6 = [("ip6tables", "DOCKER-USER", "CL-GUARD6"), ("ip6tables", "INPUT", "CL-GUARD6")]
    assert guard.jumps(daemon_up=False, ipv6=False) == v4
    assert guard.jumps(daemon_up=False, ipv6=True) == v4 + v6
    assert guard.jumps(daemon_up=True, ipv6=True) == v4 + v6 + [("iptables", "FORWARD", "DOCKER-USER"), ("ip6tables", "FORWARD", "DOCKER-USER")]
    assert guard.jumps(daemon_up=True, ipv6=False) == v4 + [("iptables", "FORWARD", "DOCKER-USER")]


def test_restore_text_is_one_transaction_declaring_every_chain():
    text = guard.restore_text(guard.chains()).splitlines()
    assert text[0] == "*filter" and text[-1] == "COMMIT"
    assert text[1:4] == [":CL-GUARD - [0:0]", ":CL-GUARD-INNER - [0:0]", ":CL-GUARD-IN - [0:0]"]
    assert text.count("COMMIT") == 1


@pytest.mark.parametrize("mode", ["sysbox", "privileged"])
def test_a_declared_mode_is_taken_when_the_container_can_run_a_daemon(mode):
    assert init.declared_mode({"CL_MODE": mode}, NAMESPACED_MAP, PRIVILEGED_STATUS) == mode


@pytest.mark.parametrize("env", [{}, {"CL_MODE": ""}, {"CL_MODE": "docker"}])
def test_an_undeclared_mode_is_refused_naming_both_ways(env):
    with pytest.raises(init.Refused) as refused:
        init.declared_mode(env, IDENTITY_MAP, PRIVILEGED_STATUS)
    assert "--runtime=sysbox-runc" in str(refused.value) and "--privileged" in str(refused.value)


@pytest.mark.parametrize("mode", ["sysbox", "privileged"])
def test_a_plain_container_is_refused_naming_both_ways(mode):
    with pytest.raises(init.Refused) as refused:
        init.declared_mode({"CL_MODE": mode}, IDENTITY_MAP, PLAIN_STATUS)
    assert "neither on sysbox nor privileged" in str(refused.value)
    assert "--runtime=sysbox-runc" in str(refused.value) and "--privileged" in str(refused.value)


def test_sysbox_declared_without_a_user_namespace_is_refused():
    with pytest.raises(init.Refused) as refused:
        init.declared_mode({"CL_MODE": "sysbox"}, IDENTITY_MAP, PRIVILEGED_STATUS)
    assert "not in a user namespace" in str(refused.value)
    assert "--runtime=sysbox-runc" in str(refused.value) and "--privileged" in str(refused.value)


def test_privileged_declared_without_a_user_namespace_is_taken():
    assert init.declared_mode({"CL_MODE": "privileged"}, IDENTITY_MAP, PRIVILEGED_STATUS) == "privileged"


def test_inner_containers_run_on_crun_only_under_sysbox():
    assert init.daemon_config("sysbox")["default-runtime"] == "crun"
    assert "default-runtime" not in init.daemon_config("privileged")
    for mode in ("sysbox", "privileged"):
        assert init.daemon_config(mode)["default-address-pools"] == [init.ADDRESS_POOL]


def test_a_docker_command_that_cannot_be_run_is_a_refusal_not_a_crash(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(init.Refused):
        init.docker("ps", "-q")


def test_a_docker_command_that_hangs_is_a_refusal_not_a_crash(monkeypatch):
    def hangs(*args, **kwargs):
        raise init.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(init.subprocess, "run", hangs)
    with pytest.raises(init.Refused) as refused:
        init.docker("stop", "x", timeout=1)
    assert "did not answer `docker stop`" in str(refused.value)


@pytest.mark.parametrize("parent, pid1, mine", [
    (1, "tini\n", True),
    (1, "systemd\n", False),   # --pid=host: pid 1 is the machine's
    (0, "python3\n", False),   # another entrypoint: the init is pid 1 itself
    (4242, "tini\n", False),
])
def test_only_a_container_with_its_own_namespace_and_init_may_end_other_processes(parent, pid1, mine):
    assert init.own_process_namespace(parent, pid1) is mine


class StopAlreadyAsked:
    def is_set(self):
        return True

    def wait(self, seconds):
        return True


def main_with(monkeypatch, tmp_path, interfaces, own_processes):
    """Run the whole of main() with a stop already asked for, on a network that holds the
    given interfaces. Nothing real is served, brought up or ended: every such call is only
    recorded, with the state it was made in. Returns the record."""
    calls = []
    net = tmp_path / "net"
    net.mkdir()
    for name in interfaces:
        (net / name).touch()
    monkeypatch.setattr(init, "Stop", StopAlreadyAsked)
    monkeypatch.setattr(init, "serve_status", lambda: calls.append("serve"))
    monkeypatch.setattr(init, "bring_up", lambda stop: calls.append("bring up") or (None, None))
    monkeypatch.setattr(init, "end_everything_else", lambda: calls.append("end while " + init.state["state"]) or True)
    monkeypatch.setattr(init, "own_process_namespace", lambda parent, pid1: own_processes)
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(init, "NET_DIR", str(net))
    init.main()
    return calls


def test_a_stop_in_someone_elses_namespace_ends_nothing(monkeypatch, tmp_path):
    """The whole of main() where the process namespace is not the container's own: refused at
    the start, then stopped. Nothing may be ended on either path."""
    calls = main_with(monkeypatch, tmp_path, ["lo", "eth0"], own_processes=False)
    assert calls == ["serve"]
    assert init.state["state"] == "refused" and "its own process namespace" in init.state["reason"]


def test_a_start_on_someone_elses_network_opens_no_port_and_starts_nothing(monkeypatch, tmp_path):
    """--network=host: the machine's bridges are there. No port, no start, and the refusal ends
    nothing; the one ending is the stop's, of this container's own processes."""
    calls = main_with(monkeypatch, tmp_path, ["lo", "eth0", "docker0", "br-0a1b2c3d4e5f"], own_processes=True)
    assert calls == ["end while refused"]
    assert init.state["state"] == "refused"
    assert "--network=host" in init.state["reason"] and "(br-0a1b2c3d4e5f, docker0)" in init.state["reason"]


def test_a_start_with_neither_its_own_namespace_nor_network_opens_no_port(monkeypatch, tmp_path):
    """--pid=host with --network=host: the first reason is given, and the port stays shut."""
    calls = main_with(monkeypatch, tmp_path, ["lo", "docker0"], own_processes=False)
    assert calls == []
    assert init.state["state"] == "refused" and "its own process namespace" in init.state["reason"]


def test_a_start_on_its_own_network_opens_the_port_and_goes_on(monkeypatch, tmp_path):
    calls = main_with(monkeypatch, tmp_path, ["lo", "eth0", "eth1"], own_processes=True)
    assert calls[:2] == ["serve", "bring up"]


def test_a_network_that_cannot_be_listed_is_refused_without_a_port(monkeypatch, tmp_path):
    assert init.network_refusal(str(tmp_path / "eth0-only")) != ""
    calls = main_with(monkeypatch, tmp_path, [], own_processes=True)
    assert calls[:2] == ["serve", "bring up"]  # an empty listing is a network of its own
    monkeypatch.setattr(init, "NET_DIR", str(tmp_path / "missing"))
    calls.clear()
    init.main()
    assert calls == ["end while refused"] and "could not be listed" in init.state["reason"]


def test_the_child_that_starts_sandboxes_is_ended_before_they_are_stopped(monkeypatch, tmp_path):
    """Ready, then a stop: the projects are started by a child, never inside the steps whose
    failure is a refusal, and at the stop that child is ended before the inner containers are
    stopped, so that it starts none behind the stop's back."""
    class StopAtTheFirstWait:
        def __init__(self):
            self.asked = False

        def is_set(self):
            return self.asked

        def wait(self, seconds):
            self.asked = True
            return True

    class Running:
        returncode = None

        def poll(self):
            return None

    calls = []
    net = tmp_path / "net"
    net.mkdir()
    daemon, child, broker = Running(), Running(), Running()
    monkeypatch.setattr(init, "Stop", StopAtTheFirstWait)
    monkeypatch.setattr(init, "serve_status", lambda: None)
    monkeypatch.setattr(init, "own_process_namespace", lambda parent, pid1: True)
    monkeypatch.setattr(init, "bring_up", lambda stop: calls.append("bring up") or (daemon, ()))
    monkeypatch.setattr(init, "start_wanted_sandboxes", lambda: calls.append("start the projects") or child)
    monkeypatch.setattr(init, "end_child", lambda c, wait_s=5: calls.append("end the child" if c is child else "end something else"))
    monkeypatch.setattr(init, "clear_broker_folders", lambda: calls.append("clear the broker's folders"))
    monkeypatch.setattr(init, "start_broker", lambda: calls.append("start the broker") or broker)
    monkeypatch.setattr(init, "broker_answers", lambda: True)
    monkeypatch.setattr(init, "end_group", lambda leader, wait_s=5: calls.append("end the broker's group" if leader is broker else "end another group"))
    monkeypatch.setattr(init, "stop_inner_containers", lambda timeout=0: calls.append("stop the containers"))
    monkeypatch.setattr(init, "stop_daemon", lambda d, wait_s=0: calls.append("stop the daemon"))
    monkeypatch.setattr(init, "end_everything_else", lambda: calls.append("end the rest") or True)
    monkeypatch.setattr(init, "watch", lambda d, dns: calls.append("watch"))   # never the real one: it would write rules here
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(init, "NET_DIR", str(net))
    init.main()
    assert calls == ["bring up", "clear the broker's folders", "start the projects", "start the broker",
                     "end the child", "end the broker's group", "stop the containers", "stop the daemon", "end the rest"]
    assert init.state["sandboxes"] == "starting" and init.state["broker"] == "down"


def test_the_broker_is_started_again_when_it_ends_but_not_in_a_tight_loop(monkeypatch):
    class Process:
        def __init__(self):
            self.ended = False

        def poll(self):
            return 0 if self.ended else None

    started, empty_groups = [], set()

    def start():
        started.append(Process())
        started[-1].pid = 1000 + len(started)
        return started[-1]
    monkeypatch.setattr(init, "start_broker", start)
    monkeypatch.setattr(init, "group_is_gone", lambda pgid: pgid in empty_groups)
    clock = [100.0]
    brokers = []
    tried = init.keep_broker(brokers, float("-inf"), now=lambda: clock[0])
    assert len(started) == 1 and brokers == started and tried == 100.0
    assert init.keep_broker(brokers, tried, now=lambda: clock[0]) == tried and len(started) == 1      # it runs: left alone
    started[0].ended = True
    clock[0] += init.BROKER_RESTART_S - 1
    assert init.keep_broker(brokers, tried, now=lambda: clock[0]) == tried and len(started) == 1      # ended, but tried a moment ago
    clock[0] += 1
    tried = init.keep_broker(brokers, tried, now=lambda: clock[0])
    assert len(started) == 2 and brokers == started and tried == clock[0]     # the ended one is kept: its operations still run in its group
    empty_groups.add(started[0].pid)                                          # its last operation ends: the group's number is free for others
    assert init.keep_broker(brokers, tried, now=lambda: clock[0]) == tried and brokers == [started[1]]   # forgotten, so a stop signals no stranger
    monkeypatch.setattr(init, "start_broker", lambda: None)                   # from now on it cannot be started
    started[1].ended = True
    empty_groups.add(started[1].pid)
    for _ in range(5):                                                        # tried again and again: nothing piles up
        clock[0] += init.BROKER_RESTART_S
        assert init.keep_broker(brokers, tried, now=lambda: clock[0]) == clock[0] and brokers == []
        tried = clock[0]


def test_the_loop_hurries_for_the_projects_and_for_a_broker_that_was_answering_and_not_for_ever(monkeypatch):
    monkeypatch.setattr(init, "WATCH_S", 10.0)
    child = object()
    assert init.loop_wait(child, True, "up", 0, 100) == 1                 # the projects are being started
    assert init.loop_wait(None, True, "down", 130, 100) == 1              # ready a moment ago: is the broker answering yet?
    assert init.loop_wait(None, True, "down", 130, 131) == 10             # the hurry is over, answering or not
    assert init.loop_wait(None, True, "up", 130, 100) == 10               # it answers: nothing to hurry for
    assert init.loop_wait(None, False, "down", 130, 100) == 10            # refused: nothing is hurried
    monkeypatch.setattr(init, "WATCH_S", 0.2)
    assert init.loop_wait(child, True, "up", 0, 100) == 0.2               # never slower than the watch itself

    hurry = init.BROKER_HURRY_S
    assert init.hurry_after("up", "down", 130, 200) == 200 + hurry        # it was answering and no longer is: hurry anew
    assert init.hurry_after("up", "up", 130, 200) == 130                  # still answering: unchanged
    assert init.hurry_after("down", "up", 130, 200) == 130                # it answers now: unchanged (and nothing left to hurry for)
    assert init.hurry_after("down", "down", 130, 200) == 130              # it cannot be started, or never answers: no new hurry
    assert init.hurry_after(None, "down", 130, 200) == 130                # the state did not name a broker yet


def test_a_group_is_ended_whether_or_not_its_leader_lives(tmp_path):
    """A broker and the operations in its group; and an earlier broker that ended, whose
    operation still runs: at a stop both groups must be gone before the containers are stopped."""
    import subprocess
    import time

    def member_of(mark):
        deadline = time.monotonic() + 10
        while not mark.exists() or not mark.read_text().strip():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        return int(mark.read_text())

    def runs(pid):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().split(")")[-1].split()[0] != "Z"
        except OSError:
            return False

    alive, orphaned = tmp_path / "alive", tmp_path / "orphaned"
    with_leader = subprocess.Popen(["sh", "-c", f"(trap '' TERM; exec sleep 300) & echo $! > {alive}; wait"], start_new_session=True)
    leader_gone = subprocess.Popen(["sh", "-c", f"sleep 300 & echo $! > {orphaned}"], start_new_session=True)
    members = [member_of(alive), member_of(orphaned)]
    leader_gone.wait()
    assert all(runs(pid) for pid in members)
    started = time.monotonic()
    init.end_group(with_leader, wait_s=1)     # its member ignores the request: killed after the wait
    init.end_group(leader_gone, wait_s=1)     # the leader is long gone; the group is not
    deadline = time.monotonic() + 10
    while any(runs(pid) for pid in members):
        assert time.monotonic() < deadline, "a member of an ended group still runs"
        time.sleep(0.05)
    assert with_leader.poll() is not None and time.monotonic() - started < 10
    init.end_group(leader_gone)               # nothing left: nothing to do


def test_ending_the_child_ends_the_command_it_is_waiting_on(tmp_path):
    """The child that starts sandboxes waits on a `docker start`; ended alone, it would leave
    that command to go on. A stand-in child with a command of its own, in a group of its own."""
    import subprocess
    import time
    mark = tmp_path / "grandchild.pid"
    # The command ignores the first signal, as a `docker` waiting on the daemon may.
    child = subprocess.Popen(["sh", "-c", f"(trap '' TERM; exec sleep 300) & echo $! > {mark}; wait"], start_new_session=True)
    deadline = time.monotonic() + 10
    while not mark.exists() or not mark.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    grandchild = int(mark.read_text())
    assert os.path.exists(f"/proc/{grandchild}")
    init.end_child(child)
    assert child.poll() is not None
    def still_runs(pid):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().split(")")[-1].split()[0] != "Z"   # a zombie is ended, only not reaped
        except OSError:
            return False

    deadline = time.monotonic() + 10
    while still_runs(grandchild):
        assert time.monotonic() < deadline, "the command the child was waiting on is still running"
        time.sleep(0.05)
    init.end_child(child)   # already ended: nothing to do
    init.end_child(None)
    alone = subprocess.Popen(["sleep", "300"])   # not in a group of its own: ended all the same
    init.end_child(alone)
    assert alone.poll() is not None


def test_the_child_that_starts_sandboxes_is_given_a_group_of_its_own(monkeypatch):
    """What end_child relies on to end the child's `docker` commands with it."""
    seen = []
    monkeypatch.setattr(init.subprocess, "Popen", lambda *args, **kwargs: seen.append((args, kwargs)) or "the child")
    assert init.start_wanted_sandboxes() == "the child"
    (args, kwargs), = seen
    assert args[0] == ["python3", init.CORE, "start-wanted"] and kwargs["start_new_session"] is True


@pytest.mark.parametrize("name, inner", [
    ("docker0", True), ("br-0a1b2c3d4e5f", True),
    ("eth0", False), ("eth1", False), ("lo", False), ("bridge0", False), ("docker1", False), ("br0", False), ("veth0a1b2c3", False),
])
def test_only_the_names_the_rules_match_count_as_inner_bridges(name, inner):
    assert guard.is_inner_bridge(name) is inner
    assert init.foreign_bridges(["lo", name]) == ([name] if inner else [])


@pytest.mark.parametrize("cgroup_text, own", [
    ("0::/\n", True),
    ("0::/init.scope\n", True),                                 # where sysbox starts a container, cgroup v2
    ("0::/docker/0ed3dd9baf43\n", False),                       # --cgroupns=host, cgroup v2
    ("0::/system.slice/docker-0ed3dd9baf43.scope\n", False),    # the same under systemd's driver
    ("0::/init.scope/more\n", False),
    ("0::/init\n", False),
    ("12:memory:/\n11:pids:/\n0::/\n", True),                   # cgroup v1, a namespace of its own
    ("12:memory:/docker/0ed3dd9baf43\n11:pids:/\n", False),     # cgroup v1, the machine's
])
def test_control_groups_are_its_own_only_where_a_container_starts(cgroup_text, own):
    assert init.own_cgroup_namespace(cgroup_text) is own


def test_nothing_is_done_before_the_control_groups_are_known_to_be_its_own(monkeypatch, tmp_path):
    """bring_up with the machine's control groups: refused before a rule is applied, a group is
    made or moved into, or a daemon started."""
    done = []
    monkeypatch.setattr(init, "declared_mode", lambda env, uid_map, status: "privileged")
    monkeypatch.setitem(init.state, "mode", None)
    monkeypatch.setattr(init, "own_cgroup_namespace", lambda text: False)
    monkeypatch.setattr(init, "guard_up", lambda: done.append("rules") or ())
    monkeypatch.setattr(init, "delegate_cgroups", lambda: done.append("groups") or True)
    monkeypatch.setattr(init, "start_daemon", lambda mode, stop: done.append("daemon") or (None, True))
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    with pytest.raises(init.Refused) as refused:
        init.bring_up(StopAlreadyAsked())
    assert done == [] and "--cgroupns=private" in str(refused.value)
    assert init.state["mode"] == "privileged"  # the refusal still says which mode was declared
    monkeypatch.setattr(init, "own_cgroup_namespace", lambda text: True)
    init.bring_up(StopAlreadyAsked())
    assert done == ["rules", "daemon"]


def test_a_refusal_in_someone_elses_namespace_ends_nothing(monkeypatch, tmp_path):
    ended = []
    monkeypatch.setattr(init, "end_everything_else", lambda: ended.append(True) or True)
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    init.refuse("not mine", mine=False)
    assert ended == [] and (init.state["state"], init.state["reason"]) == ("refused", "not mine")
    init.refuse("mine")
    assert ended == [True] and (init.state["state"], init.state["reason"]) == ("refused", "mine")
