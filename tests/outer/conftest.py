"""Fixtures for the outer container's tests.

The integration tests need a Docker daemon that can run a privileged container (the dev
container's is one). They publish no port on the machine: everything is asked through
`docker exec`, or from a peer container on a test network. Every container, network and
named volume they create starts with `cl-test-` and is removed afterwards, each container
with the unnamed volumes the image gives it; the images they build, `cl-outer:test` and the
sandbox image `cl-sandbox:<hash>`, are kept as a build cache.
"""
import json
import pathlib
import subprocess
import sys
import time
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "outer"))
sys.path.insert(0, str(REPO / "sandbox"))

import core  # found through the path just added

IMAGE = "cl-outer:test"
NET = "cl-test-net"
PEER = "cl-test-peer"
PEER_PORT = 8099
SANDBOX_NET = "sandbox-net"

# One probe for every question the tests ask of the network: it prints exactly one word, so a
# probe that crashed is never read as a refusal.
PROBE = r'''
import socket, sys
kind, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    if kind == "tcp":
        s = socket.socket(); s.settimeout(4); s.connect((host, port)); print("connected")
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
        s.sendto(b"ping", (host, port)); s.recvfrom(64); print("answered")
except TimeoutError:
    print("timeout")
except ConnectionRefusedError:
    print("refused")
except OSError as exc:
    print("error:" + type(exc).__name__)
'''
ANSWERS = ("connected", "answered", "timeout", "refused")

UDP_RESPONDER = r'''
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", int(sys.argv[1])))
while True:
    data, sender = s.recvfrom(64); s.sendto(b"pong", sender)
'''


def run(*args, timeout=300, check=True):
    done = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)
    if check and done.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed ({done.returncode}): {done.stdout[-600:]} {done.stderr[-600:]}")
    return done


def have_docker():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config, items):
    if have_docker():
        return
    skip = pytest.mark.skip(reason="no Docker daemon to run the outer container on")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def probe_from(container, kind, host, port):
    """What a container of the test daemon that has python3 gets from host:port."""
    out = run("docker", "exec", container, "python3", "-c", PROBE, kind, host, str(port)).stdout.strip()
    assert out in ANSWERS, f"the probe failed: {out}"
    return out


@pytest.fixture(scope="session")
def image():
    run("docker", "build", "-q", "-t", IMAGE, "-f", str(REPO / "outer" / "Dockerfile"), str(REPO), timeout=1200)
    return IMAGE


@pytest.fixture(scope="session")
def sandbox_image(image):
    """The sandbox image, built once on the test daemon under the tag the core computes, to be
    loaded into an outer container so that the core finds it and builds nothing."""
    tag = core.image_tag(str(REPO / "sandbox"))
    run("docker", "build", "-q", "-t", tag, str(REPO / "sandbox"), timeout=2400)
    return tag


@pytest.fixture(scope="session")
def peer(image):
    """A network of the test daemon and a neighbour on it at a private address, which a sandbox
    must not reach. It listens on PEER_PORT and on 53, the port of a resolver, over TCP and
    UDP each."""
    run("docker", "rm", "-f", "-v", PEER, check=False)
    run("docker", "network", "rm", NET, check=False)
    run("docker", "network", "create", NET)
    run("docker", "run", "-d", "--name", PEER, "--network", NET, "--entrypoint", "python3", image,
        "-m", "http.server", str(PEER_PORT), "--directory", "/opt/cl/selftest")
    run("docker", "exec", "-d", PEER, "python3", "-m", "http.server", "53", "--directory", "/opt/cl/selftest")
    for port in (PEER_PORT, 53):
        run("docker", "exec", "-d", PEER, "python3", "-c", UDP_RESPONDER, str(port))
    address = run("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", PEER).stdout.strip()
    deadline = time.monotonic() + 30
    wanted = [("tcp", PEER_PORT, "connected"), ("tcp", 53, "connected"), ("udp", PEER_PORT, "answered"), ("udp", 53, "answered")]
    while [w for w in wanted if probe_from(PEER, w[0], address, w[1]) != w[2]]:
        assert time.monotonic() < deadline, "the peer's listeners did not come up"
        time.sleep(0.3)
    yield {"name": PEER, "address": address, "port": PEER_PORT}
    run("docker", "rm", "-f", "-v", PEER, check=False)
    run("docker", "network", "rm", NET, check=False)


class Outer:
    def __init__(self):
        tag = uuid.uuid4().hex[:8]
        self.name = f"cl-test-outer-{tag}"
        self.volume = f"cl-test-docker-{tag}"
        self.data = f"cl-test-data-{tag}"
        self.sandbox_gateway = None
        self.sandbox_image = None

    def start(self, env=None, privileged=True, extra=(), command=()):
        """`docker run` the outer container. env=None declares the privileged mode."""
        env = {"CL_MODE": "privileged", "CL_WATCH_S": "1"} if env is None else env
        args = ["docker", "run", "-d", "--name", self.name, "--network", NET, "--stop-timeout", "60",
                "-v", f"{self.volume}:/var/lib/docker", "-v", f"{self.data}:/data"]
        if privileged:
            args.append("--privileged")
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        run(*args, *extra, IMAGE, *command)
        return self

    def state(self):
        done = run("docker", "exec", self.name, "cat", "/run/cl/state.json", check=False)
        try:
            return json.loads(done.stdout)
        except ValueError:
            return {"state": "unknown", "reason": done.stderr}

    def wait(self, wanted=("ready", "refused"), timeout=120, after_boot=None):
        """The state once it is one of `wanted`. After a stop and start, pass the earlier
        state's "boot" as `after_boot`: the state file outlives the restart."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state["state"] in wanted and state.get("boot") != after_boot:
                return state
            time.sleep(0.5)
        logs = run("docker", "logs", "--tail", "40", self.name, check=False)
        raise AssertionError(f"{self.name} did not reach {wanted} in {timeout}s; state {self.state()}; log:\n{logs.stderr[-2000:]}")

    def restart(self):
        """Stop and start the container; the state of the new start."""
        boot = self.state().get("boot")
        run("docker", "stop", "-t", "60", self.name)
        code = run("docker", "inspect", "-f", "{{.State.ExitCode}}", self.name).stdout.strip()
        assert code == "0", f"the container did not stop cleanly (exit {code})"
        run("docker", "start", self.name)
        return self.wait(after_boot=boot)

    def sh(self, command, check=True, timeout=120):
        """A shell line inside the outer container."""
        return run("docker", "exec", self.name, "sh", "-c", command, check=check, timeout=timeout)

    def inner(self, *args, check=True, timeout=120):
        """`docker ...` against the inner daemon."""
        return run("docker", "exec", self.name, "docker", *args, check=check, timeout=timeout)

    def load_probe(self):
        """Give the inner daemon an image with python3 (this same image) and a network of its
        own, as a sandbox will have. Records the address this container has on that network."""
        with subprocess.Popen(["docker", "save", IMAGE], stdout=subprocess.PIPE) as save:
            load = subprocess.run(["docker", "exec", "-i", self.name, "docker", "load"], stdin=save.stdout,
                                  capture_output=True, text=True, timeout=600, check=False)
        assert load.returncode == 0, load.stderr
        self.inner("network", "create", SANDBOX_NET)
        self.sandbox_gateway = self.inner("network", "inspect", SANDBOX_NET, "-f", "{{(index .IPAM.Config 0).Gateway}}").stdout.strip()
        assert self.sandbox_gateway
        return self

    def run_in_sandbox(self, *command, network=SANDBOX_NET, check=True):
        """Run a command in a new container of the inner daemon, on the sandbox network or on
        another network of the inner daemon ("bridge" is its default one)."""
        return self.inner("run", "--rm", "--network", network, "--entrypoint", command[0], IMAGE, *command[1:], check=check)

    def reach(self, host, port, kind="tcp", network=SANDBOX_NET):
        """What a container of the inner daemon gets from host:port: "connected" (tcp),
        "answered" (udp), "timeout" (dropped on the way) or "refused" (nothing listens)."""
        done = self.run_in_sandbox("python3", "-c", PROBE, kind, host, str(port), network=network, check=False)
        out = done.stdout.strip()
        assert done.returncode == 0 and out in ANSWERS, f"the probe failed: {done.stdout} {done.stderr}"
        return out

    def load_image(self, tag):
        """Give the inner daemon an image of the test daemon."""
        with subprocess.Popen(["docker", "save", tag], stdout=subprocess.PIPE) as save:
            load = subprocess.run(["docker", "exec", "-i", self.name, "docker", "load"], stdin=save.stdout,
                                  capture_output=True, text=True, timeout=900, check=False)
        assert load.returncode == 0, load.stderr
        return self

    def wait_sandboxes(self, after_boot=None, timeout=180):
        """The state once the init is done starting the projects that should be running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            done = state["state"] == "refused" or (state["state"] == "ready" and state.get("sandboxes") in ("started", "some failed"))
            if done and state.get("boot") != after_boot:
                return state
            time.sleep(0.5)
        raise AssertionError(f"{self.name} did not finish starting its sandboxes in {timeout}s; state {self.state()}")

    def wait_broker(self, wanted="up", timeout=60):
        """The state once it says the broker is `wanted` (up or down)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state.get("broker") == wanted:
                return state
            time.sleep(0.3)
        raise AssertionError(f"the broker of {self.name} did not become {wanted} in {timeout}s; state {self.state()}")

    def cl(self, *args, check=True, timeout=300):
        """The core's command in the outer container: the JSON it prints, or, when it fails
        and `check` is off, {"error": ...}."""
        done = run("docker", "exec", self.name, "cl", *args, check=False, timeout=timeout)
        if done.returncode != 0:
            assert not check, f"cl {' '.join(args)} failed: {done.stderr[-800:]}"
            return json.loads(done.stderr.strip().splitlines()[-1])
        return json.loads(done.stdout)

    def sandbox(self, project, *command, user="root", check=True):
        """A command in a project's sandbox, straight through the inner daemon."""
        return self.inner("exec", "-u", user, f"cl-sb-{project}", *command, check=check)

    def address(self):
        return run("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", self.name).stdout.strip()

    def remove(self, volume=True):
        run("docker", "rm", "-f", "-v", self.name, check=False)
        if volume:
            run("docker", "volume", "rm", self.volume, self.data, check=False)


@pytest.fixture
def outer(image, peer):
    """A factory for outer containers, each removed with its volumes afterwards."""
    made = []

    def make():
        made.append(Outer())
        return made[-1]

    yield make
    for one in made:
        one.remove()


@pytest.fixture(scope="module")
def ready(image, peer):
    """One outer container, ready in privileged mode, shared by the tests that only look."""
    one = Outer().start()
    try:
        assert one.wait()["state"] == "ready", one.state()
        one.load_probe()
        yield one
    finally:
        one.remove()


def fetch_code(url, timeout_s=4):
    """A python program that GETs `url` and prints the status, then the body; status 0 when
    nothing answered."""
    return (
        "import sys, urllib.request, urllib.error\n"
        "try:\n"
        f"    r = urllib.request.urlopen({url!r}, timeout={timeout_s}); print(r.status); print(r.read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code); print(e.read().decode())\n"
        "except Exception as e:\n"
        "    print(0); print(repr(e))\n"
    )


def fetched(stdout):
    out = stdout.split("\n", 1)
    return int(out[0]), out[1] if len(out) > 1 else ""


def fetch_from(container, url, timeout_s=4):
    """GET `url` from inside a container of the test daemon that has python3.
    Returns (status, body); status 0 when nothing answered."""
    return fetched(run("docker", "exec", container, "python3", "-c", fetch_code(url, timeout_s)).stdout)
