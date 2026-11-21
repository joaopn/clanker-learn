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
    keeper = Running()
    keeper.pid, keeper.ended, keeper.reported = 4242, False, False
    keeper.read = list   # it says nothing
    codes = []
    monkeypatch.setattr(init, "Stop", StopAtTheFirstWait)
    monkeypatch.setattr(init, "serve_status", lambda: None)
    monkeypatch.setattr(init, "own_process_namespace", lambda parent, pid1: True)
    monkeypatch.setattr(init, "bring_up", lambda stop: calls.append("bring up") or (daemon, ()))
    monkeypatch.setattr(init, "start_wanted_sandboxes", lambda: calls.append("start the projects") or child)
    monkeypatch.setattr(init, "end_child", lambda c, wait_s=5: calls.append("end the child" if c is child else "end something else"))
    monkeypatch.setattr(init, "clear_broker_folders", lambda: calls.append("clear the broker's folders"))
    monkeypatch.setattr(init, "start_broker", lambda code=None: calls.append("start the broker") or codes.append(code) or broker)
    monkeypatch.setattr(init, "start_web_keeper", lambda: calls.append("start the web keeper") or keeper)
    monkeypatch.setattr(init.broker_auth, "PASSWD_FILE", tmp_path / "no-passwd.json")
    monkeypatch.setattr(init, "broker_answers", lambda: True)
    groups = {id(broker): "end the broker's group", id(keeper): "end the web keeper's group"}
    monkeypatch.setattr(init, "end_group", lambda leader, wait_s=5: calls.append(groups.get(id(leader), "end another group")))
    monkeypatch.setattr(init, "stop_inner_containers", lambda timeout=0: calls.append("stop the containers"))
    monkeypatch.setattr(init, "stop_daemon", lambda d, wait_s=0: calls.append("stop the daemon"))
    monkeypatch.setattr(init, "end_everything_else", lambda: calls.append("end the rest") or True)
    monkeypatch.setattr(init, "watch", lambda d, dns: calls.append("watch"))   # never the real one: it would write rules here
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(init, "NET_DIR", str(net))
    init.main()
    assert calls == ["bring up", "clear the broker's folders", "start the projects", "start the broker", "start the web keeper",
                     "end the child", "end the broker's group", "end the web keeper's group", "stop the containers", "stop the daemon",
                     "end the rest"]
    assert init.state["sandboxes"] == "starting" and init.state["broker"] == "down"
    assert len(codes) == 1 and len(codes[0]) == init.SETUP_CODE_LENGTH   # no password yet: the broker was given a code


def test_the_broker_is_started_again_when_it_ends_but_not_in_a_tight_loop(monkeypatch):
    class Process:
        def __init__(self):
            self.ended = False

        def poll(self):
            return 0 if self.ended else None

    started, empty_groups = [], set()

    def start(code=None):
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
    monkeypatch.setattr(init, "start_broker", lambda code=None: None)         # from now on it cannot be started
    started[1].ended = True
    empty_groups.add(started[1].pid)
    for _ in range(5):                                                        # tried again and again: nothing piles up
        clock[0] += init.BROKER_RESTART_S
        assert init.keep_broker(brokers, tried, now=lambda: clock[0]) == clock[0] and brokers == []
        tried = clock[0]


def test_the_loop_hurries_for_the_projects_and_for_a_broker_that_was_answering_and_not_for_ever(monkeypatch):
    monkeypatch.setattr(init, "WATCH_S", 10.0)
    child = object()
    assert init.loop_wait(child, True, "up", 0, 100, "up") == 1                 # the projects are being started
    assert init.loop_wait(None, True, "down", 130, 100, "up") == 1              # ready a moment ago: is the broker answering yet?
    assert init.loop_wait(None, True, "down", 130, 131, "up") == 10             # the hurry is over, answering or not
    assert init.loop_wait(None, True, "up", 130, 100, "up") == 10               # it answers: nothing to hurry for
    assert init.loop_wait(None, False, "down", 130, 100, "up") == 10            # refused: nothing is hurried
    monkeypatch.setattr(init, "WATCH_S", 0.2)
    assert init.loop_wait(child, True, "up", 0, 100, "up") == 0.2               # never slower than the watch itself

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


# --- the status port -----------------------------------------------------------------------

@pytest.mark.parametrize("env, port", [({}, 8080), ({"CL_STATUS_PORT": "9090"}, 9090), ({"CL_STATUS_PORT": "1"}, 1),
                                       ({"CL_STATUS_PORT": "65535"}, 65535), ({"CL_STATUS_PORT": " 8181\n"}, 8181)])
def test_the_status_port_is_8080_or_the_whole_number_given(env, port):
    assert init.status_port_setting(env) == (port, "")


@pytest.mark.parametrize("value", ["0", "65536", "99999999", "", "abc", "-1", "+80", "80.5", "8080x", "80 80", "²", "٣", "1e3", "0x50"])
def test_a_status_port_that_is_not_one_is_a_refusal_naming_the_setting_and_served_on_8080(value):
    port, problem = init.status_port_setting({"CL_STATUS_PORT": value})
    assert port == 8080
    assert problem.startswith(f"CL_STATUS_PORT is {value!r}") and "1 to 65535" in problem


def test_a_wrong_status_port_does_not_crash_the_init_at_import(tmp_path):
    """It is read when the init is imported: a value that is not a port must still leave a
    page that says why, on 8080."""
    import subprocess
    import sys
    env = dict(os.environ, CL_STATUS_PORT="eighty", PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", "import init; print(init.STATUS_PORT); print(init.STATUS_PORT_PROBLEM)"],
                          cwd=os.path.dirname(init.__file__), env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    port, problem = done.stdout.split("\n", 1)
    assert port == "8080" and "CL_STATUS_PORT is 'eighty'" in problem


def test_the_status_page_is_served_on_the_status_port(monkeypatch):
    bound = []

    class Server:
        def __init__(self, address, handler):
            bound.append(address)

        def serve_forever(self):
            pass
    monkeypatch.setattr(init, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(init, "STATUS_PORT", 9191)
    init.serve_status()
    assert bound == [("0.0.0.0", 9191)]


def test_a_wrong_status_port_refuses_the_start_before_anything_is_done(monkeypatch, tmp_path):
    done = []
    net = tmp_path / "net"
    net.mkdir()
    problem = "CL_STATUS_PORT is 'eighty': it must be a whole number from 1 to 65535."
    monkeypatch.setattr(init, "STATUS_PORT_PROBLEM", problem)
    monkeypatch.setattr(init, "Stop", StopAlreadyAsked)
    monkeypatch.setattr(init, "serve_status", lambda: done.append("serve"))
    monkeypatch.setattr(init, "own_process_namespace", lambda parent, pid1: True)
    monkeypatch.setattr(init, "declared_mode", lambda env, uid_map, status: done.append("mode") or "privileged")
    monkeypatch.setattr(init, "guard_up", lambda: done.append("rules") or ())
    monkeypatch.setattr(init, "end_everything_else", lambda: True)
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(init, "NET_DIR", str(net))
    init.main()
    assert done == ["serve"]                      # the page says why; nothing else was done
    assert (init.state["state"], init.state["reason"]) == ("refused", problem)
    assert problem in (tmp_path / "state.json").read_text()


# --- the set-up code -------------------------------------------------------------------------

def test_a_setup_code_is_twenty_characters_of_the_alphabet_and_new_each_time():
    codes = [init.new_setup_code() for _ in range(200)]
    assert all(len(code) == 20 and set(code) <= set("abcdefghijklmnopqrstuvwxyz0123456789") for code in codes)
    assert len(set(codes)) == 200
    assert set("".join(codes)) == set("abcdefghijklmnopqrstuvwxyz0123456789")   # every character can be drawn


def test_a_setup_code_is_drawn_with_secrets(monkeypatch):
    drawn = []
    monkeypatch.setattr(init.secrets, "choice", lambda alphabet: drawn.append(alphabet) or "q")
    assert init.new_setup_code() == "q" * 20 and drawn == ["abcdefghijklmnopqrstuvwxyz0123456789"] * 20


def test_the_password_file_is_the_brokers(monkeypatch, tmp_path):
    import broker_auth
    assert init.broker_auth is broker_auth
    monkeypatch.setattr(broker_auth, "PASSWD_FILE", tmp_path / "passwd.json")
    assert init.password_is_set() is False
    (tmp_path / "passwd.json").write_text("{}")
    assert init.password_is_set() is True


def test_a_setup_code_is_made_and_said_only_while_no_password_is_set(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init.broker_auth, "PASSWD_FILE", tmp_path / "passwd.json")
    code = init.setup_code()
    said = capsys.readouterr().err
    assert len(code) == 20 and said.count(code) == 1
    for words in ("No password is set", "first password", "until this container stops or a password is set",
                  "docker exec -i clanker-learn cl passwd"):
        assert words in said
    assert code not in json_of(os.environ) and code not in json_of(init.state)
    (tmp_path / "passwd.json").write_text("{}")
    assert init.setup_code() is None and capsys.readouterr().err == ""


@pytest.mark.parametrize("switch", ["off", " OFF ", "Off\n"])
def test_with_the_web_server_off_no_setup_code_is_made_or_said(monkeypatch, tmp_path, capsys, switch):
    monkeypatch.setattr(init.broker_auth, "PASSWD_FILE", tmp_path / "passwd.json")
    drawn = []
    monkeypatch.setattr(init, "new_setup_code", lambda: drawn.append(1) or "q" * 20)
    assert init.setup_code({"CL_WEB": switch}) is None
    monkeypatch.setenv("CL_WEB", switch)
    assert init.setup_code() is None                                             # what main() asks: the environment
    assert drawn == [] and capsys.readouterr().err == ""


@pytest.mark.parametrize("env", [{}, {"CL_WEB": "on"}, {"CL_WEB": "maybe"}])
def test_with_the_web_server_not_off_a_setup_code_is_made(monkeypatch, tmp_path, capsys, env):
    monkeypatch.setattr(init.broker_auth, "PASSWD_FILE", tmp_path / "passwd.json")
    code = init.setup_code(env)
    assert len(code) == 20 and capsys.readouterr().err.count(code) == 1


def json_of(value):
    import json
    return json.dumps(dict(value))


BROKER_STAND_IN = '''
import json, os, sys
data = sys.stdin.read()
with open({record!r}, "w") as fh:
    json.dump({{"argv": sys.argv, "env": dict(os.environ), "stdin": data, "stdin_is": os.readlink("/proc/self/fd/0")}}, fh)
'''


def broker_stand_in(monkeypatch, tmp_path):
    record = tmp_path / "record.json"
    script = tmp_path / "broker.py"
    script.write_text(BROKER_STAND_IN.format(record=str(record)))
    monkeypatch.setattr(init, "BROKER", str(script))
    return record


def test_the_code_reaches_the_broker_on_its_standard_input_and_nowhere_else(monkeypatch, tmp_path):
    import json
    record = broker_stand_in(monkeypatch, tmp_path)
    code = init.new_setup_code()
    broker = init.start_broker(code)
    assert broker.wait(30) == 0
    seen = json.loads(record.read_text())
    assert seen["stdin"] == code + "\n"
    assert seen["argv"][1:] == ["serve"]
    assert code not in json.dumps(seen["argv"]) and code not in json.dumps(seen["env"])
    assert code not in json_of(os.environ)


def test_without_a_code_the_brokers_standard_input_is_dev_null(monkeypatch, tmp_path):
    import json
    record = broker_stand_in(monkeypatch, tmp_path)
    broker = init.start_broker()
    assert broker.wait(30) == 0
    seen = json.loads(record.read_text())
    assert seen["stdin"] == "" and seen["stdin_is"] == "/dev/null"


def test_a_broker_that_ends_before_it_reads_the_code_does_not_crash_the_init(monkeypatch):
    class Closed:
        def write(self, data):
            raise BrokenPipeError(32, "Broken pipe")

        def close(self):
            raise BrokenPipeError(32, "Broken pipe")

    class Ended:
        stdin = Closed()
    monkeypatch.setattr(init.subprocess, "Popen", lambda *args, **kwargs: Ended())
    assert isinstance(init.start_broker("abc"), Ended)


def test_a_broker_started_again_is_given_the_code_again(monkeypatch):
    class Process:
        pid = 4242
        ended = False

        def poll(self):
            return 0 if self.ended else None

    given = []
    monkeypatch.setattr(init, "start_broker", lambda code=None: given.append(code) or Process())
    monkeypatch.setattr(init, "group_is_gone", lambda pgid: True)
    brokers, clock = [], [100.0]
    tried = init.keep_broker(brokers, float("-inf"), now=lambda: clock[0], code="thecode")
    brokers[-1].ended = True
    clock[0] += init.BROKER_RESTART_S
    init.keep_broker(brokers, tried, now=lambda: clock[0], code="thecode")
    assert given == ["thecode", "thecode"]


def test_the_brokers_folder_is_made_again_at_once_with_the_brokers_mode(monkeypatch, tmp_path):
    socket_folder, markers = tmp_path / "broker", tmp_path / "broker-ops"
    for folder in (socket_folder, markers):
        (folder / "old").mkdir(parents=True)
    monkeypatch.setattr(init, "BROKER_FOLDERS", (str(socket_folder), str(markers)))
    before = os.umask(0o077)                     # the mode does not hang on the umask
    try:
        init.clear_broker_folders()
    finally:
        os.umask(before)
    assert list(socket_folder.iterdir()) == [] and oct(socket_folder.stat().st_mode & 0o777) == "0o755"
    assert not markers.exists()


# --- the web keeper --------------------------------------------------------------------------

class KeeperProcess:
    """A stand-in for the keeper's process: what is written to `write` is its output."""

    def __init__(self, pid=5000):
        read, self.write = os.pipe()
        self.stdout = os.fdopen(read, "rb", buffering=0)
        self.pid = pid
        self.code = None

    def poll(self):
        return self.code

    def say(self, data):
        os.write(self.write, data)

    def end(self, code):
        self.code = code
        if self.write is not None:
            os.close(self.write)
            self.write = None


@pytest.fixture
def keeper_process():
    made = []

    def make(pid=5000):
        made.append(KeeperProcess(pid))
        return made[-1]
    yield make
    for one in made:
        if one.write is not None:
            os.close(one.write)
        one.stdout.close()


@pytest.mark.parametrize("line, report", [
    (b'{"web": "up"}', ("web", "up")),
    (b'{"sandbox_image": "ready"}', ("sandbox_image", "ready")),
    (b'{"web": "down: ' + b"x" * 1000 + b'"}', ("web", "down: " + "x" * 594)),       # cut to a sane length
    (b'{"other": "up"}', None),
    (b'{"web": "up", "sandbox_image": "ready"}', None),
    (b'{"web": 3}', None), (b'{"web": null}', None), (b'{"web": ["up"]}', None),
    (b'["web", "up"]', None), (b'"up"', None), (b"{}", None),
    (b'{"web": "up"', None), (b"not json", None), (b"", None), (b"\xff\xfe", None),
    (b'{"web": "' + b"x" * init.KEEPER_LINE_MAX + b'"}', None),                    # longer than a report can be
])
def test_only_a_report_of_the_keeper_is_taken_from_its_lines(line, report):
    assert init.keeper_report(line) == report


def test_a_line_nested_deeper_than_the_parser_goes_is_no_report(monkeypatch):
    """Within KEEPER_LINE_MAX the parser of Python 3.13 never runs out of depth; with a larger
    bound it would, and that must not end the init."""
    monkeypatch.setattr(init, "KEEPER_LINE_MAX", 400000)
    assert init.keeper_report(b"[" * 100000 + b"]" * 100000) is None
    assert init.keeper_report(b"[" * 2048 + b"]" * 2048) is None


class Within:
    """Fails the test, instead of hanging it, when what it guards takes longer than `seconds`."""

    def __init__(self, seconds):
        self.seconds = seconds

    def __enter__(self):
        import signal

        def late(*_):
            raise AssertionError(f"took longer than {self.seconds} s")   # not an OSError: read() would take that for an end
        self.before = signal.signal(signal.SIGALRM, late)
        signal.alarm(self.seconds)

    def __exit__(self, *exc):
        import signal
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.before)
        return False


def test_the_keepers_output_is_read_line_by_line_without_waiting(keeper_process):
    process = keeper_process()
    keeper = init.WebKeeper(process)
    with Within(10):
        read_line_by_line(process, keeper)


def read_line_by_line(process, keeper):
    assert keeper.read() == []                                                     # nothing yet: no wait
    process.say(b'{"web": "preparing"}\n{"other": 1}\nbroken\n{"web": ')
    assert keeper.read() == [("web", "preparing")]
    process.say(b'"up"}\n{"sandbox_image": "ready"}\n')                           # the rest of a line comes later
    assert keeper.read() == [("web", "up"), ("sandbox_image", "ready")]
    process.say(b"x" * (init.KEEPER_LINE_MAX + 10))                                 # too long, and not ended yet
    assert keeper.read() == []
    process.say(b'{"web": "up, says the tail"}\n{"web": "down: x"}\n')              # its tail is dropped; the next line counts
    assert keeper.read() == [("web", "down: x")]
    process.say(b'{"web": "down: y"}\n{"web": "up"}')                              # a last line cut short by its end
    process.end(1)
    assert keeper.read() == [("web", "down: y")]
    assert keeper.read() == []


@pytest.fixture
def keeping(monkeypatch, tmp_path, keeper_process):
    """keep_web_keeper with stand-in keepers and a clock: what it started, and the state."""
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    for key in ("web", "sandbox_image"):
        monkeypatch.delitem(init.state, key, raising=False)
    started, gone, clock = [], set(), [100.0]

    def start():
        started.append(keeper_process(6000 + len(started)))
        return init.WebKeeper(started[-1])
    monkeypatch.setattr(init, "start_web_keeper", start)
    monkeypatch.setattr(init, "group_is_gone", lambda pgid: pgid in gone)
    keepers = []

    def turn(tried, advance=0.0):
        clock[0] += advance
        return init.keep_web_keeper(keepers, tried, now=lambda: clock[0])
    return {"started": started, "gone": gone, "clock": clock, "keepers": keepers, "turn": turn}


def test_what_the_keeper_says_is_the_state(keeping):
    tried = keeping["turn"](float("-inf"))
    assert len(keeping["started"]) == 1 and "web" not in init.state
    keeping["started"][0].say(b'{"web": "preparing"}\n')
    tried = keeping["turn"](tried, 1)
    assert init.state["web"] == "preparing"
    keeping["started"][0].say(b'{"web": "up"}\n{"sandbox_image": "preparing: building"}\n')
    keeping["turn"](tried, 1)
    assert (init.state["web"], init.state["sandbox_image"]) == ("up", "preparing: building")
    assert len(keeping["started"]) == 1


def test_what_the_keeper_says_is_said_in_the_log_a_line_a_report(keeping, capsys):
    tried = keeping["turn"](float("-inf"))
    capsys.readouterr()
    keeping["started"][0].say(b'{"web": "preparing"}\n{"web": "down: ' + b"x" * 1000 + b'"}\n{"sandbox_image": "failed: no network"}\n')
    keeping["turn"](tried, 1)
    assert capsys.readouterr().err.splitlines() == ["clanker-learn: web: preparing", "clanker-learn: web: down: " + "x" * 594,
                                                   "clanker-learn: sandbox_image: failed: no network"]
    assert (init.state["state"], init.state["web"], init.state["sandbox_image"]) == ("ready", "down: " + "x" * 594, "failed: no network")


@pytest.mark.parametrize("code, said, web", [
    (0, b'{"web": "off"}\n', "off"),
    (3, b'{"web": "down: CL_WEB is \'maybe\'"}\n', "down: CL_WEB is 'maybe'"),
    (3, b"", "down: the web server's keeper ended without saying why"),
    (3, b'{"sandbox_image": "ready"}\n', "down: the web server's keeper ended without saying why"),
])
def test_a_keeper_that_ended_with_0_or_3_is_not_started_again(keeping, code, said, web):
    tried = keeping["turn"](float("-inf"))
    keeping["started"][0].say(said)                  # said just before it ended: read all the same
    keeping["started"][0].end(code)
    tried = keeping["turn"](tried, 1)
    assert tried is None and init.state["web"] == web
    for _ in range(5):
        keeping["gone"].add(keeping["started"][0].pid)
        assert keeping["turn"](tried, init.KEEPER_RESTART_S) is None
    assert len(keeping["started"]) == 1 and keeping["keepers"] == []


@pytest.mark.parametrize("code", [1, 2, -9])
def test_a_keeper_that_ended_otherwise_is_started_again_not_in_a_tight_loop(keeping, code, capsys):
    tried = keeping["turn"](float("-inf"))
    keeping["started"][0].say(b'{"web": "up"}\n')
    keeping["turn"](tried, 1)
    keeping["started"][0].end(code)
    capsys.readouterr()
    tried = keeping["turn"](tried, 1)
    assert init.state["web"] == f"down: the web server's keeper ended unexpectedly (exit {code})"
    assert capsys.readouterr().err.splitlines() == [f"clanker-learn: web: down: the web server's keeper ended unexpectedly (exit {code})"]
    assert len(keeping["started"]) == 1                                          # started 2 s ago: not yet
    tried = keeping["turn"](tried, init.KEEPER_RESTART_S - 2.5)
    assert len(keeping["started"]) == 1
    tried = keeping["turn"](tried, 0.5)
    assert len(keeping["started"]) == 2 and keeping["keepers"][-1].pid == keeping["started"][1].pid
    assert keeping["keepers"][0].pid == keeping["started"][0].pid                # its group still has a member: kept for the stop
    keeping["gone"].add(keeping["started"][0].pid)
    keeping["turn"](tried, 1)
    assert [k.pid for k in keeping["keepers"]] == [keeping["started"][1].pid]   # forgotten once its group is empty


def test_a_keeper_that_cannot_be_started_is_said_and_tried_again_later(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))

    def cannot(*args, **kwargs):
        raise OSError(2, "No such file or directory")
    monkeypatch.setattr(init.subprocess, "Popen", cannot)
    keepers, clock = [], [100.0]
    tried = init.keep_web_keeper(keepers, float("-inf"), now=lambda: clock[0])
    assert keepers == [] and init.state["web"].startswith("down: the web server's keeper could not be started")
    assert capsys.readouterr().err.startswith("clanker-learn: web: down: the web server's keeper could not be started")
    assert init.keep_web_keeper(keepers, tried, now=lambda: clock[0] + 1) == tried
    assert init.keep_web_keeper(keepers, tried, now=lambda: clock[0] + init.KEEPER_RESTART_S) == clock[0] + init.KEEPER_RESTART_S


def test_the_keeper_is_started_in_a_group_of_its_own_reading_nothing_and_writing_to_a_pipe(monkeypatch):
    seen = []

    class Process:
        pid = 7

        def __init__(self, *args, **kwargs):
            seen.append((args, kwargs))
            read, self._write = os.pipe()
            self.stdout = os.fdopen(read, "rb", buffering=0)
    monkeypatch.setattr(init.subprocess, "Popen", Process)
    monkeypatch.setattr(init, "STATUS_PORT", 9191)
    with Within(5):
        keeper = init.start_web_keeper()
        assert keeper.read() == []                                                 # does not wait
    (args, kwargs), = seen
    assert args[0] == ["python3", init.CORE, "web-keep", "9191"]
    assert kwargs["start_new_session"] is True and kwargs["stdin"] == init.subprocess.DEVNULL and kwargs["stdout"] == init.subprocess.PIPE
    os.close(keeper.process._write)
    keeper.close()


@pytest.mark.parametrize("web, wait", [(None, 1), ("preparing", 1), ("preparing: building the web server's image", 1),
                                       ("up", 10), ("off", 10), ("down: the web server ended: boom", 10)])
def test_the_loop_hurries_while_the_web_server_is_prepared(monkeypatch, web, wait):
    monkeypatch.setattr(init, "WATCH_S", 10.0)
    assert init.loop_wait(None, True, "up", 0, 100, web) == wait
    assert init.loop_wait(None, False, "up", 0, 100, web) == 10                    # refused: nothing is hurried


def test_after_a_refusal_the_keeper_is_forgotten_and_its_state_dropped(monkeypatch, tmp_path, keeper_process):
    """Ready, the keeper says the web server is up, then the watch refuses: "web" and
    "sandbox_image" leave the state with "sandboxes" and "broker", the keeper is not started
    again, and the stop does not signal its group (refuse() ended it; the number may be
    someone else's by then). The code never reaches the state file."""
    import json

    class Running:
        pid = 4100
        returncode = None

        def poll(self):
            return None

    class StopAfterTurns:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits > 4

        def wait(self, seconds):
            self.waits += 1
            return self.is_set()

    calls, codes = [], []
    net = tmp_path / "net"
    net.mkdir()
    process = keeper_process()
    process.say(b'{"web": "up"}\n{"sandbox_image": "ready"}\n')

    def watch(daemon, dns):
        calls.append(("watch", init.state.get("web"), init.state.get("sandbox_image")))
        raise init.Refused("the rules were lost")
    monkeypatch.setattr(init, "Stop", StopAfterTurns)
    monkeypatch.setattr(init, "serve_status", lambda: None)
    monkeypatch.setattr(init, "own_process_namespace", lambda parent, pid1: True)
    monkeypatch.setattr(init, "bring_up", lambda stop: (Running(), ()))
    monkeypatch.setattr(init, "start_wanted_sandboxes", lambda: None)
    monkeypatch.setattr(init, "clear_broker_folders", lambda: None)
    monkeypatch.setattr(init.broker_auth, "PASSWD_FILE", tmp_path / "no-passwd.json")
    monkeypatch.setattr(init, "start_broker", lambda code=None: codes.append(code) or Running())
    monkeypatch.setattr(init, "start_web_keeper", lambda: calls.append("start the web keeper") or init.WebKeeper(process))
    monkeypatch.setattr(init, "broker_answers", lambda: True)
    monkeypatch.setattr(init, "group_is_gone", lambda pgid: False)
    monkeypatch.setattr(init, "end_group", lambda leader, wait_s=5: calls.append(f"end group {leader.pid}"))
    monkeypatch.setattr(init, "end_child", lambda c, wait_s=5: None)
    monkeypatch.setattr(init, "stop_inner_containers", lambda timeout=0: None)
    monkeypatch.setattr(init, "stop_daemon", lambda d, wait_s=0: None)
    monkeypatch.setattr(init, "end_everything_else", lambda: True)
    monkeypatch.setattr(init, "watch", watch)
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(init, "NET_DIR", str(net))
    init.main()
    assert calls == ["start the web keeper", ("watch", "up", "ready")]
    assert init.state["state"] == "refused"
    assert not {"sandboxes", "broker", "web", "sandbox_image"} & set(init.state)
    assert not {"sandboxes", "broker", "web", "sandbox_image"} & set(json.loads((tmp_path / "state.json").read_text()))
    assert len(codes) == 1 and codes[0] not in (tmp_path / "state.json").read_text()
