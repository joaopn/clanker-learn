"""The parts of the outer container that are decided without a daemon."""
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


def test_a_stop_in_someone_elses_namespace_ends_nothing(monkeypatch, tmp_path):
    """The whole of main() where the process namespace is not the container's own: refused at
    the start, then stopped. Nothing may be ended on either path."""
    class StopAlreadyAsked:
        def is_set(self):
            return True

        def wait(self, seconds):
            return True

    ended = []
    monkeypatch.setattr(init, "Stop", StopAlreadyAsked)
    monkeypatch.setattr(init, "serve_status", lambda: None)
    monkeypatch.setattr(init, "own_process_namespace", lambda parent, pid1: False)
    monkeypatch.setattr(init, "end_everything_else", lambda: ended.append(True) or True)
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    init.main()
    assert ended == []
    assert init.state["state"] == "refused" and "its own process namespace" in init.state["reason"]


def test_a_refusal_in_someone_elses_namespace_ends_nothing(monkeypatch, tmp_path):
    ended = []
    monkeypatch.setattr(init, "end_everything_else", lambda: ended.append(True) or True)
    monkeypatch.setattr(init, "STATE_FILE", str(tmp_path / "state.json"))
    init.refuse("not mine", mine=False)
    assert ended == [] and (init.state["state"], init.state["reason"]) == ("refused", "not mine")
    init.refuse("mine")
    assert ended == [True] and (init.state["state"], init.state["reason"]) == ("refused", "mine")
