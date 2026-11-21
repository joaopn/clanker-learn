"""The guard rules between clanker-learn's inner containers and everything around them.

Inner containers (the sandboxes, later the web server and the tutor) leave by NAT through
this container. Without rules they reach whatever this container reaches: the host at the
bridge gateway, other machines on a private network, and this container's own listeners.
The rule set follows RS's inner guard (container/supervisor/inner-guard.sh), with two
differences: it is keyed on the inner bridges (docker0 and Docker's br-* networks), not on
the uplink, so it holds whatever interfaces this container has or gains; and it has no
exceptions but one.

  CL-GUARD        jumped from DOCKER-USER, the chain Docker keeps for operator rules, jumps
                  to first, and never flushes. What comes from an inner bridge goes to
                  CL-GUARD-INNER; everything else (a published port being reached from
                  outside) returns untouched.
  CL-GUARD-INNER  what an inner container sends. Replies return. A new connection that was
                  DNAT'd by a published port is dropped (one sandbox reaching another, or
                  the interface, through this container's address). What goes to an inner
                  bridge returns, for Docker's own isolation rules to judge. What goes
                  anywhere else toward a private range is dropped, with one exception:
                  port 53 of a resolver this container itself is configured with, when
                  that resolver has a private address (a home router, a tailnet's
                  resolver). Docker's resolver inside a sandbox asks such a resolver from
                  the sandbox's own network namespace, so without the exception no sandbox
                  on such a machine could resolve a name.
  CL-GUARD-IN     jumped from INPUT. A new connection to this container itself from an
                  inner bridge is dropped.

The rules need no daemon: the init installs them, DOCKER-USER included, before it starts the
inner daemon, so there is no moment at which a container runs without them. Unlike RS's
script, a failure is never a warning: the init starts nothing unless they are in place.

IPv6 is not fenced, it is cut: CL-GUARD6, jumped from DOCKER-USER and from INPUT in
ip6tables, drops everything that comes from an inner bridge. The inner daemon's networks
carry no IPv6 (Docker switches it off on a container's interface unless its network asks for
it, and nothing in clanker-learn creates one that does), but the bridges themselves have a
link-local address in this container and Docker's port proxy listens on it, so a sandbox
that built its own IPv6 frames would otherwise reach a published port there. Where the kernel
has no IPv6 at all there is nothing to cut and no rule is made.
"""
import ipaddress
import os
import subprocess

FWD_CHAIN = "CL-GUARD"
INNER_CHAIN = "CL-GUARD-INNER"
IN_CHAIN = "CL-GUARD-IN"
V6_CHAIN = "CL-GUARD6"
V4 = "iptables"
V6 = "ip6tables"
FWD_HOOK = "DOCKER-USER"
IN_HOOK = "INPUT"
# docker0 is the default bridge; every other network of the inner daemon is br-<id>. The outer
# container's own interfaces are never named so, and clanker-learn names no bridge itself.
INNER_BRIDGES = ("docker0", "br-+")
# RS's four, and the carrier-grade NAT range that tailnets use.
PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")
LOCK_WAIT_S = "5"


def is_inner_bridge(name):
    """True when an interface name is one the rules take for a bridge of the inner daemon. A
    trailing + is iptables' wildcard."""
    return any(name.startswith(b[:-1]) if b.endswith("+") else name == b for b in INNER_BRIDGES)


class GuardError(Exception):
    """The rules could not be applied."""


def private_resolvers(resolv_conf_text):
    """The IPv4 nameservers of a resolv.conf that lie in a private range, in order, once each.
    Loopback ones are left out (Docker asks those from this container's own namespace) and
    so are public ones (nothing drops them)."""
    found = []
    for line in resolv_conf_text.splitlines():
        words = line.split()
        if len(words) < 2 or words[0] != "nameserver":
            continue
        try:
            address = ipaddress.IPv4Address(words[1])
        except ValueError:
            continue
        if any(address in ipaddress.ip_network(r) for r in PRIVATE_RANGES) and str(address) not in found:
            found.append(str(address))
    return tuple(found)


def chains(resolvers=()):
    """The rules of each chain, in the form `iptables -S` prints them."""
    fwd = [f"-A {FWD_CHAIN} -i {bridge} -j {INNER_CHAIN}" for bridge in INNER_BRIDGES]
    inner = [
        f"-A {INNER_CHAIN} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        f"-A {INNER_CHAIN} -m conntrack --ctstate DNAT -j DROP",
    ]
    inner += [f"-A {INNER_CHAIN} -o {bridge} -j RETURN" for bridge in INNER_BRIDGES]
    for address in resolvers:
        inner += [f"-A {INNER_CHAIN} -d {address}/32 -p {proto} -m {proto} --dport 53 -j RETURN" for proto in ("udp", "tcp")]
    inner += [f"-A {INNER_CHAIN} -d {r} -j DROP" for r in PRIVATE_RANGES]
    inp = [f"-A {IN_CHAIN} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"]
    inp += [f"-A {IN_CHAIN} -i {bridge} -j DROP" for bridge in INNER_BRIDGES]
    return {FWD_CHAIN: fwd, INNER_CHAIN: inner, IN_CHAIN: inp}


def chains6():
    """The one IPv6 chain: nothing from an inner bridge goes anywhere."""
    return {V6_CHAIN: [f"-A {V6_CHAIN} -i {bridge} -j DROP" for bridge in INNER_BRIDGES]}


def ipv6_present():
    return os.path.isdir("/proc/sys/net/ipv6")


def jumps(daemon_up, ipv6):
    """(tool, hook, chain): each jump that must be the first rule of its hook. With the daemon
    up, Docker's own jump to DOCKER-USER must be first in FORWARD as well: a rule above it
    would decide before ours."""
    wanted = [(V4, FWD_HOOK, FWD_CHAIN), (V4, IN_HOOK, IN_CHAIN)]
    if ipv6:
        wanted += [(V6, FWD_HOOK, V6_CHAIN), (V6, IN_HOOK, V6_CHAIN)]
    if daemon_up:
        wanted += [(tool, "FORWARD", FWD_HOOK) for tool in ((V4, V6) if ipv6 else (V4,))]
    return wanted


def restore_text(built):
    """One iptables-restore transaction: every chain declared (so flushed) and refilled."""
    lines = ["*filter"] + [f":{chain} - [0:0]" for chain in built]
    for rules in built.values():
        lines += rules
    return "\n".join(lines + ["COMMIT"]) + "\n"


def _run(args, stdin=None):
    try:
        return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError(f"{args[0]} could not be run: {exc}") from exc


def _tables(tool, *args):
    return _run([tool, "-w", LOCK_WAIT_S, *args])


def resolvers():
    """The private resolvers this container is configured with."""
    try:
        with open("/etc/resolv.conf") as fh:
            return private_resolvers(fh.read())
    except OSError as exc:
        raise GuardError(f"/etc/resolv.conf could not be read: {exc}") from exc


def _appended(chain, tool=V4):
    """The `-A` lines of a chain, or None when the chain cannot be read."""
    done = _tables(tool, "-S", chain)
    if done.returncode != 0:
        return None
    return [line for line in done.stdout.splitlines() if line.startswith("-A ")]


def _jump_first(tool, hook, chain):
    """Make the jump the first rule of its hook without ever taking a jump that is in place
    away: insert at the top when it is not there, then drop any other copy, one at a time,
    by a number read just before."""
    jump = f"-A {hook} -j {chain}"
    rules = _appended(hook, tool)
    if rules is None:
        raise GuardError(f"the {tool} chain {hook} cannot be read")
    if not rules or rules[0] != jump:
        done = _tables(tool, "-I", hook, "1", "-j", chain)
        if done.returncode != 0:
            raise GuardError(f"the {tool} jump from {hook} to {chain} could not be inserted: {done.stderr.strip()[:300]}")
    for _ in range(20):
        rules = _appended(hook, tool) or []
        copies = [number for number, rule in enumerate(rules, start=1) if rule == jump and number > 1]
        if not copies:
            return
        _tables(tool, "-D", hook, str(copies[-1]))


def _restore(tool, built):
    done = _run([f"{tool}-restore", "-w", LOCK_WAIT_S, "--noflush"], stdin=restore_text(built))
    if done.returncode != 0:
        raise GuardError(f"{tool}-restore failed: {done.stderr.strip()[:300]}")
    if _appended(FWD_HOOK, tool) is None:
        # Before the daemon's first start the chain does not exist. Docker adopts one that does.
        done = _tables(tool, "-N", FWD_HOOK)
        if done.returncode != 0:
            raise GuardError(f"the {tool} chain {FWD_HOOK} could not be created: {done.stderr.strip()[:300]}")


def apply(dns=(), daemon_up=False):
    """Install the chains and their jumps. Raises GuardError; never half-reports success.
    With the daemon up, Docker's own jump to DOCKER-USER is put back first in FORWARD too."""
    ipv6 = ipv6_present()
    _restore(V4, chains(dns))
    if ipv6:
        _restore(V6, chains6())
    for tool, hook, chain in jumps(daemon_up, ipv6):
        _jump_first(tool, hook, chain)


def check(dns=(), daemon_up=False):
    """What is wrong with the installed rules; an empty list when they are in place."""
    problems = []
    try:
        ipv6 = ipv6_present()
        for tool, built in ((V4, chains(dns)), (V6, chains6() if ipv6 else {})):
            for chain, want in built.items():
                if _appended(chain, tool) != want:
                    problems.append(f"the {tool} chain {chain} does not hold the expected rules")
        for tool, hook, chain in jumps(daemon_up, ipv6):
            got = _appended(hook, tool)
            if not got or got[0] != f"-A {hook} -j {chain}":
                problems.append(f"the first {tool} rule of {hook} is not the jump to {chain}")
    except GuardError as exc:
        problems.append(str(exc))
    return problems
