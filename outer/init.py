#!/usr/bin/env python3
"""The init of clanker-learn's container.

Order of a start, and nothing is skipped:

  1. the container must be its own: its own process namespace, with tini as pid 1; a network
     that holds no other Docker daemon's bridges (--network=host is told by them); then the
     mode the run command declared (CL_MODE=sysbox or privileged) is read and checked, and
     its control groups must be its own (not --cgroupns=host);
  2. the guard rules are applied and checked, before any daemon exists;
  3. the inner Docker daemon is started and waited for, and the rules are checked again
     (Docker's own jump to them must be in place);
  4. anything the daemon is running is stopped (no inner container is ever given a restart
     policy, so this finds nothing unless something went wrong);
  5. one container is run from the self-test image.

Only then is the container "ready". Once it is, the projects that should be running are
started by the core, in a child process, while this init goes on watching: a project that
cannot be started is that project's trouble and never a refusal. The state says "sandboxes:
starting" until that child is done, then "started" or "some failed".

The broker (broker.py), the door the web server uses, is started then too, in a process
group of its own in which its operations stay. If it ends it is started again, a few seconds
apart at most; the state says "broker: up" once its socket accepts a connection. A broker that
cannot run is never a refusal either. When no password is set yet, the init makes a set-up code
once per start, says it in the log, and hands it to every broker it starts on its standard
input; it is kept nowhere else.

The web server's keeper (`core.py web-keep`) is started then too, in a group of its own. It
brings the web server's container up (or removes it, with CL_WEB=off) and keeps it up, and
says how it stands, a line of JSON at a time, which the init puts in the state ("web",
"sandbox_image"). A wrong web setting is "web: down" with its reason, never a refusal. At a
stop every broker group and the keeper's are ended, operations included, before the inner
containers are stopped.

If any step fails the start is refused: every other process in the container is ended, the
daemon and anything it ran included, so nothing runs unfenced, and the interface's port answers
503 with the reason until the container is stopped. A fault nobody foresaw is refused the same
way, not left as a traceback. Two refusals differ, both made before anything is started. Where
the process namespace is not the container's own, or tini is not pid 1, nothing is ended. Where
the network is not the container's own, nothing is ended and the port is not opened either: it
would be opened on someone else's network, so the reason is only in the log and the state file.
While ready, the rules are checked again every few seconds and restored when they are gone. If
they cannot be restored, or the daemon has died, the container goes to "refused" in the same
way: everything else in it is ended at once.

The state is also in /run/cl/state.json, with a "boot" value that is new at every start.
"""
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import broker_auth  # its own standard library only: where the password is kept
import guard

STATE_FILE = "/run/cl/state.json"
NET_DIR = "/sys/class/net"
DAEMON_LOG = "/var/log/dockerd.log"
DAEMON_LOG_MAX = 5 * 1024 * 1024
DAEMON_JSON = "/etc/docker/daemon.json"
CORE = "/opt/cl/core.py"
BROKER = "/opt/cl/broker.py"
BROKER_SOCKET = "/run/cl/broker/broker.sock"
BROKER_FOLDERS = ("/run/cl/broker", "/run/cl/broker-ops")  # the socket and progress logs; the markers of operations
BROKER_RESTART_S = 5
BROKER_HURRY_S = 30
SETUP_CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
SETUP_CODE_LENGTH = 20
KEEPER_RESTART_S = 5
KEEPER_KEYS = ("web", "sandbox_image")
KEEPER_LINE_MAX = 4096       # a longer line from the keeper is not one of its reports
KEEPER_VALUE_MAX = 600
SELFTEST_ROOT = "/opt/cl/selftest"
SELFTEST_IMAGE = "cl-selftest"
DEFAULT_STATUS_PORT = 8080
DAEMON_WAIT_S = float(os.environ.get("CL_DAEMON_WAIT_S", "90"))
WATCH_S = float(os.environ.get("CL_WATCH_S", "10"))
STOP_GRACE_S = 10
SHUTDOWN_COMMAND_S = 15
CAP_SYS_ADMIN = 21
# Inner networks draw from one range of their own. The daemon skips a subnet that overlaps a
# route this container already has, so an inner network never shadows the way out.
ADDRESS_POOL = {"base": "10.210.0.0/16", "size": 24}
HOW_TO_RUN = (
    "Start clanker-learn in one of two ways: on sysbox "
    "(docker run --runtime=sysbox-runc -e CL_MODE=sysbox ...) or privileged "
    "(docker run --privileged -e CL_MODE=privileged ...)."
)
SEE_LOG = "The container's log (docker logs) says why."
CGROUP_STARTS = ("/", "/init.scope")
NOT_ITS_OWN_NETWORK = (
    "This container's network already holds a Docker daemon's networks ({found}), as it does when it is started with "
    "--network=host, or shares the network of a container in which a Docker daemon runs or ran. Its guard rules and its own "
    "daemon would act on that network, so nothing was started and its port was not opened. Start it without --network=host, "
    "on a network of its own. "
) + HOW_TO_RUN
NOT_ITS_OWN_CGROUPS = (
    "This container does not have its own control groups, as with --cgroupns=host (which is also Docker's default on a "
    "machine with cgroup v1). Its daemon would manage the machine's control groups, so nothing was started. Start it with "
    "--cgroupns=private. "
) + HOW_TO_RUN


class Refused(Exception):
    """The start cannot go on; the message is what the learner is shown."""


def status_port_setting(env):
    """(port, problem): CL_STATUS_PORT, or the default; a value that is not a port is a
    problem, which the start refuses with, and meanwhile the default port says so."""
    text = env.get("CL_STATUS_PORT", str(DEFAULT_STATUS_PORT))
    if re.fullmatch(r"[0-9]{1,5}", text.strip()) and 1 <= int(text) <= 65535:
        return int(text), ""
    return DEFAULT_STATUS_PORT, (f"CL_STATUS_PORT is {text!r}: it must be a whole number from 1 to 65535. "
                                 f"Until it is, this page is served on {DEFAULT_STATUS_PORT}.")


# Read here, never crashing: the port is needed before anything else is done.
STATUS_PORT, STATUS_PORT_PROBLEM = status_port_setting(os.environ)


# "boot" changes at every start of the container: the file outlives a stop and start, so a
# reader tells a fresh state from the last run's by it.
state = {"state": "starting", "mode": None, "reason": "", "boot": uuid.uuid4().hex}
state_lock = threading.Lock()


def log(message):
    print(f"clanker-learn: {message}", file=sys.stderr, flush=True)


def set_state(name, reason="", said=None, **extra):
    """Write the state, and say it in the log: `said` when given, else the state's name and
    reason."""
    with state_lock:
        state.update(state=name, reason=reason, **extra)
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    log(said if said is not None else f"{name}{': ' + reason if reason else ''}")


class Stop:
    """Set by SIGTERM or SIGINT. The handler only records the fact; the signal also writes a
    byte to a pipe, which is what wakes a wait. No lock is taken in the handler."""

    def __init__(self):
        self._set = False
        self._read, write = os.pipe()
        os.set_blocking(write, False)
        os.set_blocking(self._read, False)
        signal.set_wakeup_fd(write)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, *_):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, seconds):
        """True as soon as a stop was asked for, False after `seconds` without one."""
        deadline = time.monotonic() + seconds
        while not self._set:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            if select.select([self._read], [], [], left)[0]:
                try:
                    os.read(self._read, 512)
                except BlockingIOError:
                    pass
        return True


# --- the status answer on the interface's port ---------------------------------------------

class Status(BaseHTTPRequestHandler):
    """Until the web server exists (step 1c) this is all the interface's port says: the state
    and, when refused, the reason. Nothing else of the container is readable through it."""

    def _answer(self, body_wanted):
        with state_lock:
            snapshot = dict(state)
        if self.path == "/state.json":
            body, kind = json.dumps(snapshot), "application/json"
        else:
            body = f"clanker-learn: {snapshot['state']}\n"
            if snapshot["reason"]:
                body += snapshot["reason"] + "\n"
            kind = "text/plain; charset=utf-8"
        data = body.encode()
        self.send_response(200 if snapshot["state"] == "ready" else 503)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body_wanted:
            self.wfile.write(data)

    def do_GET(self):
        self._answer(True)

    def do_HEAD(self):
        self._answer(False)

    def log_message(self, *args):
        pass


def serve_status():
    server = ThreadingHTTPServer(("0.0.0.0", STATUS_PORT), Status)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# --- the mode ------------------------------------------------------------------------------

def uid_map_is_identity(uid_map_text):
    """True when the container is not in a user namespace of its own (sysbox always gives one)."""
    return uid_map_text.split()[:3] == ["0", "0", "4294967295"]


def has_cap(status_text, cap):
    for line in status_text.splitlines():
        if line.startswith("CapEff:"):
            return bool(int(line.split()[1], 16) >> cap & 1)
    return False


def own_process_namespace(my_parent, pid1_command):
    """True when this container has its own process namespace and the init it ships with:
    tini is pid 1 and this process is its child. With --pid=host, or another entrypoint, it
    is not, and then "every other process" would be someone else's."""
    return my_parent == 1 and pid1_command.strip() == "tini"


def foreign_bridges(interfaces):
    """The interfaces the guard rules would take for the inner daemon's bridges. At the start
    of a container with a network of its own there are none: Docker gives it a new network
    namespace at every start. With --network=host the machine's own docker0 and br-* are
    there, and the rules and the daemon would act on them."""
    return sorted(name for name in interfaces if guard.is_inner_bridge(name))


def network_refusal(net_dir):
    """Why the network is not this container's own, or "" when it is. A listing that cannot be
    read is a refusal too: nothing may be opened on a network that cannot be told."""
    try:
        found = foreign_bridges(os.listdir(net_dir))
    except OSError as exc:
        return f"This container's network interfaces could not be listed ({exc}), so nothing was started and its port was not opened. " + HOW_TO_RUN
    return NOT_ITS_OWN_NETWORK.format(found=", ".join(found)) if found else ""


def own_cgroup_namespace(cgroup_text):
    """True when this process sits where a container's processes start in a cgroup namespace
    of its own (/proc/self/cgroup: one line on cgroup v2, one per hierarchy on v1): at the
    root, "/", or in "/init.scope", where sysbox puts them on cgroup v2 so that the root can
    hand controllers down. With --cgroupns=host the path is the container's place in the
    machine's tree, which ends in a name Docker made from the container's id and so is
    neither."""
    return all(line.split(":", 2)[2] in CGROUP_STARTS for line in cgroup_text.splitlines() if line.count(":") >= 2)


def declared_mode(env, uid_map_text, status_text):
    """The mode the run command declared, or Refused. The mode is not guessed from the inside:
    a privileged container nested in a sysbox one looks like sysbox. What can be told is
    refused: a mode that was not declared, a container with neither sysbox nor privilege, and
    sysbox declared where there is no user namespace."""
    mode = env.get("CL_MODE", "")
    if mode not in ("sysbox", "privileged"):
        said = f"CL_MODE is {mode!r}" if mode else "CL_MODE is not set"
        raise Refused(f"{said}. {HOW_TO_RUN}")
    if not has_cap(status_text, CAP_SYS_ADMIN):
        raise Refused(f"This container runs neither on sysbox nor privileged, so it cannot run its own Docker daemon. {HOW_TO_RUN}")
    if mode == "sysbox" and uid_map_is_identity(uid_map_text):
        raise Refused(f"CL_MODE=sysbox was declared, but this container is not in a user namespace, which sysbox always gives. {HOW_TO_RUN}")
    return mode


# --- the guard rules -----------------------------------------------------------------------

def guard_up():
    """Apply and check the rules, before the daemon exists. Returns the resolvers they except."""
    try:
        dns = guard.resolvers()
        guard.apply(dns)
    except guard.GuardError as exc:
        raise Refused(f"The guard rules that fence the sandboxes could not be applied, so nothing was started. ({exc})") from exc
    guard_checked(dns, daemon_up=False)
    return dns


def guard_checked(dns, daemon_up):
    problems = guard.check(dns, daemon_up=daemon_up)
    if problems:
        outcome = "everything was stopped" if daemon_up else "nothing was started"
        raise Refused(f"The guard rules that fence the sandboxes are not in place after applying them, so {outcome}. (" + "; ".join(problems) + ")")


# --- the inner daemon ----------------------------------------------------------------------

def daemon_config(mode):
    """runc under sysbox trips over sysbox's /proc, so RS runs inner containers on crun there;
    privileged mode keeps Docker's own default."""
    config = {"default-address-pools": [ADDRESS_POOL]}
    if mode == "sysbox":
        config.update({"default-runtime": "crun", "runtimes": {"crun": {"path": "/usr/bin/crun"}}})
    return config


def delegate_cgroups():
    """cgroup v2: a group that holds processes cannot hand controllers to its children, and
    without that the inner containers run with no limits. Move this container's processes
    into a child group, then enable every controller for the subtree (the docker-in-docker
    boot sequence). True when the controllers are delegated or there is nothing to do."""
    root = "/sys/fs/cgroup"
    if not os.path.exists(f"{root}/cgroup.controllers"):
        return True
    try:
        os.makedirs(f"{root}/init", exist_ok=True)
    except OSError:
        return False
    for _ in range(50):
        try:
            with open(f"{root}/cgroup.procs") as fh:
                pids = fh.read().split()
            for pid in pids:
                try:
                    with open(f"{root}/init/cgroup.procs", "w") as fh:
                        fh.write(pid)
                except OSError:
                    pass  # the process ended meanwhile
            with open(f"{root}/cgroup.controllers") as fh:
                wanted = " ".join("+" + c for c in fh.read().split())
            with open(f"{root}/cgroup.subtree_control", "w") as fh:
                fh.write(wanted)
            return True
        except OSError:
            time.sleep(0.05)  # a process entered the root group meanwhile; move it too
    return False


def docker(*args, timeout=120):
    """A command against the inner daemon. One that hangs or cannot be run is a refusal, not a
    crash: the caller's Refused handling ends everything."""
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"The inner Docker daemon did not answer `docker {args[0]}`. {SEE_LOG}") from exc


def daemon_log_tail():
    try:
        with open(DAEMON_LOG, errors="replace") as fh:
            return "".join(fh.readlines()[-15:])
    except OSError:
        return ""


def start_daemon(mode, stop):
    os.makedirs(os.path.dirname(DAEMON_JSON), exist_ok=True)
    with open(DAEMON_JSON, "w") as fh:
        json.dump(daemon_config(mode), fh, indent=2)
    # /var/run survives a stop and start of this container; a stale pid file makes dockerd refuse.
    for stale in ("/var/run/docker.pid", "/var/run/docker.sock", "/run/docker/containerd/containerd.pid"):
        try:
            os.remove(stale)
        except OSError:
            pass
    try:
        if os.path.getsize(DAEMON_LOG) > DAEMON_LOG_MAX:
            os.replace(DAEMON_LOG, DAEMON_LOG + ".1")
    except OSError:
        pass
    delegated = delegate_cgroups()
    if not delegated:
        log("the cgroup controllers could not be delegated; inner containers will run without limits")
    try:
        with open(DAEMON_LOG, "ab") as daemon_log:
            daemon = subprocess.Popen(["dockerd"], stdout=daemon_log, stderr=subprocess.STDOUT)
    except OSError as exc:
        raise Refused(f"The inner Docker daemon could not be started. ({exc})") from exc
    deadline = time.monotonic() + DAEMON_WAIT_S
    while time.monotonic() < deadline and not stop.is_set():
        if daemon.poll() is not None:
            sys.stderr.write(daemon_log_tail())
            raise Refused(f"The inner Docker daemon did not start. {SEE_LOG}")
        try:
            if docker("info", timeout=20).returncode == 0:
                return daemon, delegated
        except Refused:
            pass  # not answering yet
        stop.wait(0.5)
    if stop.is_set():
        return daemon, delegated
    sys.stderr.write(daemon_log_tail())
    raise Refused(f"The inner Docker daemon was not ready after {DAEMON_WAIT_S:.0f} seconds. {SEE_LOG}")


def stop_daemon(daemon, wait_s=40):
    if daemon is None or daemon.poll() is not None:
        return
    daemon.terminate()
    try:
        daemon.wait(wait_s)
    except subprocess.TimeoutExpired:
        daemon.kill()


def running_containers(timeout=120):
    done = docker("ps", "-q", timeout=timeout)
    if done.returncode != 0:
        raise Refused(f"The inner Docker daemon could not list its containers. {SEE_LOG}")
    return done.stdout.split()


def stop_inner_containers(grace_s=STOP_GRACE_S, timeout=120):
    """Stop whatever the inner daemon runs, all in one `docker stop` (it stops them together,
    each by its own stop signal, and kills what is left after the grace). Raises Refused
    unless nothing is left running."""
    running = running_containers(timeout)
    if not running:
        return
    log(f"stopping {len(running)} container{'s' if len(running) != 1 else ''} of the inner daemon")
    docker("stop", "-t", str(grace_s), *running, timeout=grace_s + timeout)
    if running_containers(timeout):
        raise Refused("Containers of the inner Docker daemon could not be stopped.")


def selftest():
    if docker("image", "inspect", SELFTEST_IMAGE).returncode != 0:
        try:
            with subprocess.Popen(["tar", "-C", SELFTEST_ROOT, "-c", "."], stdout=subprocess.PIPE) as tar:
                done = subprocess.run(["docker", "import", "-", SELFTEST_IMAGE], stdin=tar.stdout, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refused(f"The inner Docker daemon could not import the self-test image. ({exc})") from exc
        if done.returncode != 0:
            raise Refused(f"The inner Docker daemon could not import the self-test image. ({done.stderr.strip()[:300]})")
    done = docker("run", "--rm", "--network", "none", SELFTEST_IMAGE, "/bin/busybox", "true")
    if done.returncode != 0:
        raise Refused(f"The inner Docker daemon could not run a container. ({done.stderr.strip()[:300]})")


def end_everything_else():
    """End every process in this container but pid 1 and this one: the daemon, containerd, its
    shims, and with them every inner container, whose processes outlive a daemon that died.
    It asks nothing of the daemon, so it cannot hang. True when nothing else is left."""
    keep = (1, os.getpid())
    for _ in range(100):
        others = [int(name) for name in os.listdir("/proc") if name.isdigit() and int(name) not in keep]
        if not others:
            return True
        for pid in others:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass  # reap what was ours
        except ChildProcessError:
            pass
        time.sleep(0.1)
    return False


def refuse(reason, mine=True):
    """Go to "refused": nothing else may keep running. The port says so before the ending
    starts ("refusing") and "refused" is written only once nothing else is left. If something
    cannot be ended, the container itself exits, which ends it. `mine` is False when the
    process namespace or the network is not this container's own: then nothing was started
    and nothing is ended."""
    if mine:
        set_state("refusing", reason)
        if not end_everything_else():
            log("processes could not be ended; exiting")
            os._exit(1)
    set_state("refused", reason)


# --- the whole of it -----------------------------------------------------------------------

def bring_up(stop):
    """Steps 1 to 5. Returns (daemon, resolvers the rules except); raises Refused."""
    if STATUS_PORT_PROBLEM:
        raise Refused(STATUS_PORT_PROBLEM)
    with open("/proc/self/uid_map") as fh:
        uid_map_text = fh.read()
    with open("/proc/self/status") as fh:
        status_text = fh.read()
    mode = declared_mode(os.environ, uid_map_text, status_text)
    set_state("starting", mode=mode)
    with open("/proc/self/cgroup") as fh:
        if not own_cgroup_namespace(fh.read()):
            raise Refused(NOT_ITS_OWN_CGROUPS)
    dns = guard_up()
    daemon, delegated = start_daemon(mode, stop)
    if stop.is_set():
        return daemon, dns
    guard_checked(dns, daemon_up=True)
    stop_inner_containers()
    selftest()
    if not stop.is_set():
        set_state("ready", mode=mode, limits=delegated)
    return daemon, dns


def start_wanted_sandboxes():
    """The core, in a child, starting the projects that should be running. None when it
    cannot be run; then nothing is started and the state says so."""
    try:
        return subprocess.Popen(["python3", CORE, "start-wanted"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                start_new_session=True)  # a group of its own: end_child ends its docker commands with it
    except OSError as exc:
        log(f"the projects could not be started: {exc}")
        return None


def end_child(child, wait_s=5):
    """End the child that starts sandboxes, and the command it is waiting on, so that it asks
    the daemon for no more starts while the inner containers are stopped."""
    if child is None or child.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            child.send_signal(sig)  # not the leader of a group of its own: the child alone
        try:
            child.wait(wait_s)
            break
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(child.pid, signal.SIGKILL)  # what of its group did not end at the first signal
    except (ProcessLookupError, PermissionError):
        pass


def clear_broker_folders():
    """Once per start of the container, before any broker runs: /run outlives a stop and
    start, and what an earlier run left there (logs of operations that were cut off, markers)
    describes nothing that still exists. The socket's folder is made again at once, as the
    broker makes it: the web server's container mounts it, and a missing source fails that."""
    for folder in BROKER_FOLDERS:
        shutil.rmtree(folder, ignore_errors=True)
    try:
        os.makedirs(BROKER_FOLDERS[0], mode=0o755, exist_ok=True)
        os.chmod(BROKER_FOLDERS[0], 0o755)
    except OSError as exc:
        log(f"the broker's folder could not be made: {exc}")


def password_is_set():
    return os.path.exists(broker_auth.PASSWD_FILE)


def new_setup_code():
    return "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH))


def setup_code(env=os.environ):
    """A set-up code when no password is set yet, else None. Made once per start, said once
    in the log, and handed to each broker on its standard input: never in the environment, the
    state, a file, or a command line. None too with the web server off (CL_WEB=off): there is
    no page to enter it on, and the broker says how to set the password instead."""
    if password_is_set() or env.get("CL_WEB", "on").strip().lower() == "off":
        return None
    code = new_setup_code()
    log(f"No password is set yet. The set-up code is {code}. The web page asks for it to set the first password; "
        "it is good until this container stops or a password is set. "
        "Or set the password from a shell: docker exec -i clanker-learn cl passwd")
    return code


def start_broker(code=None):
    """The broker, in a process group of its own, with the set-up code on its standard input
    when there is one. None when it cannot be run."""
    try:
        broker = subprocess.Popen(["python3", BROKER, "serve"], stdin=subprocess.PIPE if code else subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log(f"the broker could not be started: {exc}")
        return None
    if code:
        try:
            broker.stdin.write((code + "\n").encode())
            broker.stdin.close()
        except OSError:  # it ended before it read: it is started again, and given the code again
            log("the broker ended before it took the set-up code")
            try:
                broker.stdin.close()
            except OSError:
                pass
    return broker


def keep_broker(brokers, tried, now=time.monotonic, code=None):
    """Start the broker if none runs, not more often than every BROKER_RESTART_S seconds.
    `brokers` holds the running one, and each ended one whose operations still run in its
    group. Returns when it was last tried."""
    # One that ended is kept only while something of its group still runs: once the group is
    # empty its number is free for the kernel to give to others, which a stop must not signal.
    running = bool(brokers) and brokers[-1].poll() is None
    brokers[:] = [b for i, b in enumerate(brokers) if (running and i == len(brokers) - 1) or not group_is_gone(b.pid)]
    if running:
        return tried
    if now() - tried < BROKER_RESTART_S:
        return tried
    if tried != float("-inf"):
        log("the broker is not running; starting it again")
    new = start_broker(code)
    if new is not None:
        brokers.append(new)
    return now()


class WebKeeper:
    """The child that brings the web server up and keeps it up (`core.py web-keep`), and what
    it has said so far. Its output is read without waiting: the init's loop never blocks on it."""

    def __init__(self, process):
        self.process = process
        self.pid = process.pid
        self.pending = b""
        self.skipping = False      # in the middle of a line that was too long
        self.reported = False      # it said something of the web server
        self.ended = False         # its end was seen and dealt with
        os.set_blocking(process.stdout.fileno(), False)

    def poll(self):
        return self.process.poll()

    def read(self):
        """The reports in what it wrote since the last read, in order: [(key, value)]. A last
        line cut short by its end is dropped."""
        reports = []
        while True:
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                self.pending, self.skipping = b"", False
                break
            *lines, self.pending = (self.pending + chunk).split(b"\n")
            for line in lines:
                if self.skipping:
                    self.skipping = False   # the end of the line that was too long
                    continue
                report = keeper_report(line)
                if report is not None:
                    reports.append(report)
            if len(self.pending) > KEEPER_LINE_MAX:
                self.pending, self.skipping = b"", True
        self.reported = self.reported or any(key == "web" for key, _ in reports)
        return reports

    def close(self):
        try:
            self.process.stdout.close()
        except OSError:
            pass


def keeper_report(line):
    """(key, value) when `line` is one of the keeper's reports: a JSON object with exactly one
    of KEEPER_KEYS and a string value, which is cut to KEEPER_VALUE_MAX. Anything else is None."""
    if len(line) > KEEPER_LINE_MAX:
        return None
    try:
        said = json.loads(line.decode())
    except (ValueError, RecursionError):
        return None
    if not isinstance(said, dict) or len(said) != 1:
        return None
    (key, value), = said.items()
    if key not in KEEPER_KEYS or not isinstance(value, str):
        return None
    return key, value[:KEEPER_VALUE_MAX]


def web_is(value):
    """What the init itself has to say of the web server: in the state, and in the log as
    the keeper's own reports are."""
    set_state("ready", web=value, said=f"web: {value}")


def start_web_keeper():
    """The web server's keeper, in a process group of its own; None when it cannot be run.
    It inherits the environment, which holds the web server's settings."""
    try:
        process = subprocess.Popen(["python3", CORE, "web-keep", str(STATUS_PORT)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   start_new_session=True)
    except OSError as exc:
        web_is(f"down: the web server's keeper could not be started ({exc})")
        return None
    return WebKeeper(process)


def keep_web_keeper(keepers, tried, now=time.monotonic):
    """Put what the web keeper said in the state, and start it again if it ended unexpectedly,
    not more often than every KEEPER_RESTART_S seconds. One that ended with 0 (the web server
    is off) or 3 (it said why it is down) is not started again. `keepers` holds the running
    one, and each ended one whose group still has a member; `tried` is when one was last
    started, or None once none is to be started any more. Returns the new `tried`."""
    current = keepers[-1] if keepers else None
    if current is not None and not current.ended:
        code = current.poll()
        for key, value in current.read():   # all of it, when it has ended
            set_state("ready", said=f"{key}: {value}", **{key: value})   # in the log too: why the web server is down
        if code is not None:
            current.ended = True
            current.close()
            if code in (0, 3):
                if code == 3 and not current.reported:
                    web_is("down: the web server's keeper ended without saying why")
                tried = None
            else:
                web_is(f"down: the web server's keeper ended unexpectedly (exit {code})")
    running = current is not None and not current.ended
    keepers[:] = [k for i, k in enumerate(keepers) if (running and i == len(keepers) - 1) or not group_is_gone(k.pid)]
    if tried is None or running or now() - tried < KEEPER_RESTART_S:
        return tried
    new = start_web_keeper()
    if new is not None:
        keepers.append(new)
    return now()


def loop_wait(starting, ready, broker, hurry_until, now, web):
    """How long the init's loop sleeps. A second while the projects are being started, while
    the web server is being prepared (or has not been heard of yet), and while it is in a hurry
    to see the broker answer; WATCH_S otherwise, which is also the pace of the guard check."""
    web_preparing = web is None or web.startswith("preparing")
    hurried = starting or (ready and (web_preparing or (broker != "up" and now < hurry_until)))
    return min(WATCH_S, 1) if hurried else WATCH_S


def hurry_after(was, broker, hurry_until, now):
    """Until when the loop hurries, given what the state said of the broker and what is seen
    now. It hurries anew when a broker that was answering no longer is: the one started in its
    place should be shown soon. A broker that cannot be started, or never answers, renews
    nothing, so it cannot keep the loop, and the guard check with it, at one-second pace."""
    return now + BROKER_HURRY_S if was == "up" and broker == "down" else hurry_until


def broker_answers(path=BROKER_SOCKET):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(2)
            probe.connect(path)
        return True
    except OSError:
        return False


def group_is_gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def end_group(leader, wait_s=5):
    """End a process group this init started (a broker and its operations), whether or not
    its leader still lives: asked first, then killed."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(leader.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            leader.poll()  # collect the leader, so that its group can empty
            if group_is_gone(leader.pid):
                return
            time.sleep(0.05)
    leader.poll()


def watch(daemon, dns):
    """One pass while ready. Raises Refused when the container can no longer be trusted."""
    if daemon.poll() is not None:
        sys.stderr.write(daemon_log_tail())
        raise Refused(f"The inner Docker daemon stopped, so every sandbox was stopped. Restart clanker-learn. {SEE_LOG}")
    problems = guard.check(dns, daemon_up=True)
    if not problems:
        return
    log("the guard rules are not in place (" + "; ".join(problems) + "); applying them again")
    try:
        guard.apply(dns, daemon_up=True)
        problems = guard.check(dns, daemon_up=True)
    except guard.GuardError as exc:
        problems = [str(exc)]
    if problems:
        raise Refused("The guard rules that fence the sandboxes were lost and could not be restored, so every sandbox was stopped. (" + "; ".join(problems) + ")")
    log("the guard rules are in place again")


def unforeseen(exc):
    """The reason shown for a fault that is not a Refused; the traceback goes to the log."""
    traceback.print_exception(exc)
    return f"clanker-learn's init failed in a way it does not know ({type(exc).__name__}). {SEE_LOG}"


def main():
    stop = Stop()
    set_state("starting")
    not_my_network = network_refusal(NET_DIR)
    if not not_my_network:
        serve_status()
    try:
        with open("/proc/1/comm") as fh:
            mine = own_process_namespace(os.getppid(), fh.read())
    except OSError:
        mine = False
    daemon = dns = starting = code = None
    ready = False
    brokers, broker_tried, hurry_until = [], float("-inf"), 0.0
    keepers, keeper_tried = [], float("-inf")
    if not mine:
        refuse("This container does not have its own process namespace and init. Start it without --pid=host, without --init and without another entrypoint. " + HOW_TO_RUN, mine=False)
    elif not_my_network:
        refuse(not_my_network, mine=False)
    else:
        try:
            daemon, dns = bring_up(stop)
            ready = not stop.is_set()
        except Refused as exc:
            refuse(str(exc))
        except Exception as exc:  # noqa: BLE001 - anything unforeseen is a refusal, not a crash
            refuse(unforeseen(exc))
    if ready:
        clear_broker_folders()
        code = setup_code()
        starting = start_wanted_sandboxes()
        broker_tried = keep_broker(brokers, broker_tried, code=code)
        hurry_until = time.monotonic() + BROKER_HURRY_S
        set_state("ready", sandboxes="starting" if starting else "some failed", broker="down")
        keeper_tried = keep_web_keeper(keepers, keeper_tried)
    while not stop.wait(loop_wait(starting, ready, state.get("broker"), hurry_until, time.monotonic(), state.get("web"))):
        if not ready:
            continue
        if starting is not None and starting.poll() is not None:
            set_state("ready", sandboxes="started" if starting.returncode == 0 else "some failed")
            starting = None
        broker_tried = keep_broker(brokers, broker_tried, code=code)
        keeper_tried = keep_web_keeper(keepers, keeper_tried)
        broker = "up" if brokers and brokers[-1].poll() is None and broker_answers() else "down"
        hurry_until = hurry_after(state.get("broker"), broker, hurry_until, time.monotonic())
        if broker != state.get("broker"):
            set_state("ready", broker=broker)
        try:
            watch(daemon, dns)
        except Exception as exc:  # noqa: BLE001 - as at the start
            ready = False
            daemon = starting = None  # ended, and reaped, by refuse()
            brokers.clear()
            for keeper in keepers:
                keeper.close()
            keepers.clear()  # ended by refuse(); its group number may be someone else's by the stop
            with state_lock:
                for key in ("sandboxes", "broker", "web", "sandbox_image"):
                    state.pop(key, None)  # nothing is starting, started or kept any more
            refuse(str(exc) if isinstance(exc, Refused) else unforeseen(exc))
    if mine:
        end_child(starting)
        for leader in (*brokers, *keepers):
            end_group(leader)  # brokers and their operations, the web keeper: they ask the daemon for nothing more
        if daemon is not None and daemon.poll() is None:
            log("stopping the inner containers and the daemon")
            try:
                stop_inner_containers(timeout=SHUTDOWN_COMMAND_S)
            except Refused as exc:
                log(str(exc))
            stop_daemon(daemon, wait_s=SHUTDOWN_COMMAND_S)
        end_everything_else()
    log("stopped")


if __name__ == "__main__":
    main()
