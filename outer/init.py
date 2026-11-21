#!/usr/bin/env python3
"""The init of clanker-learn's container.

Order of a start, and nothing is skipped:

  1. the container must be its own: its own process namespace, with tini as pid 1; then the
     mode the run command declared (CL_MODE=sysbox or privileged) is read and checked;
  2. the guard rules are applied and checked, before any daemon exists;
  3. the inner Docker daemon is started and waited for, and the rules are checked again
     (Docker's own jump to them must be in place);
  4. anything the daemon is running is stopped (no inner container is ever given a restart
     policy, so this finds nothing unless something went wrong);
  5. one container is run from the self-test image.

Only then is the container "ready". If any step fails the start is refused: every other
process in the container is ended, the daemon and anything it ran included, so nothing runs
unfenced, and the interface's port answers 503 with the reason until the container is
stopped. A fault nobody foresaw is refused the same way, not left as a traceback. The one
exception is the first check: where the process namespace is not the container's own, or
tini is not pid 1, nothing was started and nothing is ended. While ready, the rules are checked again every few seconds and restored when they
are gone. If they cannot be restored, or the daemon has died, the container goes to
"refused" in the same way: everything else in it is ended at once.

The state is also in /run/cl/state.json, with a "boot" value that is new at every start.
"""
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import guard

STATE_FILE = "/run/cl/state.json"
DAEMON_LOG = "/var/log/dockerd.log"
DAEMON_LOG_MAX = 5 * 1024 * 1024
DAEMON_JSON = "/etc/docker/daemon.json"
SELFTEST_ROOT = "/opt/cl/selftest"
SELFTEST_IMAGE = "cl-selftest"
UI_PORT = int(os.environ.get("CL_UI_PORT", "8080"))
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


class Refused(Exception):
    """The start cannot go on; the message is what the learner is shown."""


# "boot" changes at every start of the container: the file outlives a stop and start, so a
# reader tells a fresh state from the last run's by it.
state = {"state": "starting", "mode": None, "reason": "", "boot": uuid.uuid4().hex}
state_lock = threading.Lock()


def log(message):
    print(f"clanker-learn: {message}", file=sys.stderr, flush=True)


def set_state(name, reason="", **extra):
    with state_lock:
        state.update(state=name, reason=reason, **extra)
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    log(f"{name}{': ' + reason if reason else ''}")


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
    server = ThreadingHTTPServer(("0.0.0.0", UI_PORT), Status)
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
    cannot be ended, the container itself exits, which ends it. `mine` is False only when the
    process namespace is not this container's own: then nothing was started and nothing is
    ended."""
    if mine:
        set_state("refusing", reason)
        if not end_everything_else():
            log("processes could not be ended; exiting")
            os._exit(1)
    set_state("refused", reason)


# --- the whole of it -----------------------------------------------------------------------

def bring_up(stop):
    """Steps 1 to 5. Returns (daemon, resolvers the rules except); raises Refused."""
    with open("/proc/self/uid_map") as fh:
        uid_map_text = fh.read()
    with open("/proc/self/status") as fh:
        status_text = fh.read()
    mode = declared_mode(os.environ, uid_map_text, status_text)
    set_state("starting", mode=mode)
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
    serve_status()
    try:
        with open("/proc/1/comm") as fh:
            mine = own_process_namespace(os.getppid(), fh.read())
    except OSError:
        mine = False
    daemon = dns = None
    ready = False
    if not mine:
        refuse("This container does not have its own process namespace and init. Start it without --pid=host, without --init and without another entrypoint. " + HOW_TO_RUN, mine=False)
    else:
        try:
            daemon, dns = bring_up(stop)
            ready = not stop.is_set()
        except Refused as exc:
            refuse(str(exc))
        except Exception as exc:  # noqa: BLE001 - anything unforeseen is a refusal, not a crash
            refuse(unforeseen(exc))
    while not stop.wait(WATCH_S):
        if not ready:
            continue
        try:
            watch(daemon, dns)
        except Exception as exc:  # noqa: BLE001 - as at the start
            ready = False
            daemon = None  # ended, and reaped, by refuse()
            refuse(str(exc) if isinstance(exc, Refused) else unforeseen(exc))
    if mine:
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
