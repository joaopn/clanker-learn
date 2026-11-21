"""A project's terminal, its web apps and its ports named by hand, through the real web server
in a real outer container in privileged mode.

One outer container for every test here, with the web server on, both images preloaded, a
password set and one login; the project `apps` runs a small web server on port 8000 of its
sandbox. Requests come from the neighbour container (`peer`), as in test_web.py; the terminal
is spoken to by ws_client.py, from a container of the web server's image on the same network.
"""
import json
import pathlib
import subprocess
import time
import uuid

import pytest
from conftest import NET, PEER, Outer, core, run
from test_broker import set_password
from test_sandboxes import reach
from test_web import PASSWORD, PORT, Page, act, networks_of, proof, said, web_on

pytestmark = pytest.mark.integration

WS_CLIENT = (pathlib.Path(__file__).parent / "ws_client.py").read_text()
CONNECT = {"type": "connect", "rows": 24, "cols": 80}
APP_PORT = 8000
# A plain TCP client: sends an HTTP/1.0 request and prints what came back and how it ended.
RAW = r'''
import json, socket, sys
got, end = b"", "closed"
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)
    s.sendall(b"GET / HTTP/1.0\r\n\r\n")
    while chunk := s.recv(65536):
        got += chunk
except TimeoutError:
    end = "timeout"
except ConnectionResetError:
    end = "reset"
except ConnectionRefusedError:
    end = "refused"
print(json.dumps({"got": got.decode(errors="replace"), "end": end}))
'''
# An app that answers every request with the Cookie header it was sent.
COOKIE_ECHO = r'''
import http.server, sys
class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (self.headers.get("Cookie") or "").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(("0.0.0.0", int(sys.argv[1])), Echo).serve_forever()
'''
# An SSH password login from cl-web, which sits on every project's network: "in", "denied",
# or what went wrong. The password comes on standard input, never on a command line.
SSH_LOGIN = r'''
import asyncio, json, sys
import asyncssh
asked = json.load(sys.stdin)
async def main():
    try:
        conn = await asyncssh.connect(asked["host"], username=asked["user"], password=asked["password"], known_hosts=None,
                                      client_keys=None, agent_path=None, config=[], connect_timeout=10)
    except asyncssh.PermissionDenied:
        return "denied"
    except Exception as exc:
        return "error:" + type(exc).__name__
    conn.close()
    return "in"
print(asyncio.run(main()))
'''


def fed(command, text, timeout=120):
    """A command given `text` on its standard input."""
    done = subprocess.run(command, input=text, capture_output=True, text=True, timeout=timeout, check=False)
    assert done.returncode == 0, done.stderr[-800:]
    return done.stdout


def jar_of(page):
    return json.loads(run("docker", "exec", PEER, "cat", page.jar).stdout)


def terminal(outer, project, cookies, **more):
    """What a terminal of the project gets, as ws_client.py answers it."""
    name = f"cl-test-ws-{uuid.uuid4().hex[:8]}"
    asked = {"url": f"wss://{outer.address()}:{PORT}/broker/project/{project}/terminal", "cookies": cookies, "first": CONNECT, **more}
    try:
        return json.loads(fed(["docker", "run", "--rm", "-i", "--name", name, "--network", NET, "--entrypoint", "python3",
                               outer.web_image, "-c", WS_CLIENT], json.dumps(asked)))
    finally:
        run("docker", "container", "rm", "-f", name, check=False)


def serve_in_sandbox(outer, project, *command):
    """A server started in the sandbox as the sandbox user, in the workspace, left running."""
    outer.inner("exec", "-d", "-u", "learner", "-w", "/workspace", f"cl-sb-{project}", *command)


def services(page, project):
    answer = page.get(f"/broker/project/{project}/services")
    assert answer["status"] == 200 and said(answer)["ok"], answer
    return said(answer)["services"]


def opened(page, project, port, timeout=30):
    """The origin port of the project's app on `port`, once it is detected and opened."""
    deadline = time.monotonic() + timeout
    while not services(page, project).get(f"port-{port}", {}).get("detected"):
        assert time.monotonic() < deadline, f"port {port} of {project} was not detected: {services(page, project)}"
        time.sleep(0.5)
    answer = page.post(f"/broker/project/{project}/origin", {"port": port})
    assert answer["status"] == 200 and said(answer)["ok"], answer
    return said(answer)["origin_port"]


def session_for(page, project):
    answer = page.post(f"/broker/project/{project}/session")
    assert answer["status"] == 200 and said(answer) == {"ok": True}, answer


def on_port(outer, page, port):
    """A client of the origin port `port` that sends the cookies of `page`."""
    app = Page(outer, port)
    app.jar = page.jar
    return app


def raw_tcp(outer, port):
    return json.loads(run("docker", "exec", PEER, "python3", "-c", RAW, outer.address(), str(port)).stdout)


@pytest.fixture(scope="module")
def site(image, peer, preload, web_image):
    """The outer container, a login (`site.page`) and the project `apps`, whose sandbox serves
    its workspace on APP_PORT."""
    one = Outer()
    try:
        web_on(one, preload)
        one.web_image = web_image
        set_password(one, PASSWORD)
        one.page = Page(one)
        assert one.page.post("/broker/login", {"proof": proof()})["status"] == 200
        act(one.page, "/broker/project", {"name": "apps"})            # through the routes: the operation leaves its log
        one.sandbox("apps", "sh", "-c", "echo from the workspace > /workspace/hello.txt", user="learner")
        serve_in_sandbox(one, "apps", "python3", "-m", "http.server", str(APP_PORT))
        yield one
    finally:
        one.remove()


# --- the working path -----------------------------------------------------------------------

def test_a_terminal_runs_a_command_in_the_sandbox(site):
    seen = terminal(site, "apps", jar_of(site.page), send="echo marker-$((6*7))\r", until="marker-42")
    assert seen["status"] == 101 and seen["messages"][:1] == [{"type": "connected"}], seen
    assert "marker-42" in seen["output"], seen


def test_a_web_app_in_a_sandbox_is_fetched_through_its_origin_port(site):
    origin = opened(site.page, "apps", APP_PORT)
    assert services(site.page, "apps")[f"port-{APP_PORT}"]["origin_port"] == origin
    session_for(site.page, "apps")
    answer = on_port(site, site.page, origin).get("/")
    assert answer["status"] == 200 and "hello.txt" in answer["body"], answer


def test_a_port_named_by_hand_is_reached_from_outside_until_it_is_removed(site):
    serve_in_sandbox(site, "apps", "python3", "-m", "http.server", "8001")
    added = site.page.post("/broker/project/apps/ports", {"port": 8001, "label": "raw", "passthrough": True})
    assert added["status"] == 200 and said(added)["ok"], added
    host_port = said(added)["result"]["port"]
    deadline = time.monotonic() + 20
    while not (answer := raw_tcp(site, host_port))["got"].startswith("HTTP/1.0 200"):   # until the server in the sandbox listens
        assert time.monotonic() < deadline, answer
        time.sleep(0.5)
    assert "hello.txt" in answer["got"]
    removed = site.page.post("/broker/project/apps/ports/remove", {"port": host_port, "passthrough": True})
    assert removed["status"] == 200 and said(removed)["ok"], removed
    # Not relayed: closed at once with nothing written (a reset, when the request was there unread).
    after = raw_tcp(site, host_port)
    assert after["got"] == "" and after["end"] in ("closed", "reset"), after


# --- the security properties ------------------------------------------------------------------

def test_without_the_login_no_terminal_and_without_the_projects_session_no_app(site):
    assert terminal(site, "apps", {})["status"] == 401
    origin = opened(site.page, "apps", APP_PORT)
    session_for(site.page, "apps")
    act(site.page, "/broker/project", {"name": "elsewhere"})
    stranger = Page(site)                                             # logged in, with the session of another project only
    assert stranger.post("/broker/login", {"proof": proof()})["status"] == 200
    session_for(stranger, "elsewhere")
    assert on_port(site, Page(site), origin).get("/")["status"] == 401
    assert on_port(site, stranger, origin).get("/")["status"] == 401
    assert on_port(site, site.page, origin).get("/")["status"] == 200


def test_a_sandbox_reaches_neither_the_origin_ports_nor_the_pass_through_ports(site):
    origin = opened(site.page, "apps", APP_PORT)
    added = site.page.post("/broker/project/apps/ports", {"port": 8002, "label": "fenced", "passthrough": True})
    assert added["status"] == 200 and said(added)["ok"], added
    host_port = said(added)["result"]["port"]
    try:
        web = networks_of(site, "cl-web")
        gateway = site.inner("network", "inspect", "cl-net-apps", "-f", "{{(index .IPAM.Config 0).Gateway}}").stdout.strip()
        for port in (origin, host_port):
            assert reach(site, "cl-sb-apps", web["cl-net-apps"], port) == "refused", port   # nothing listens on its project-side address
            for address in (web["cl-front"], gateway, site.address()):
                assert reach(site, "cl-sb-apps", address, port) == "timeout", (address, port)
    finally:
        site.page.post("/broker/project/apps/ports/remove", {"port": host_port, "passthrough": True})


def test_the_terminal_goes_only_to_a_sandbox_whose_host_key_is_the_projects(site):
    site.cl("create", "keyed")
    # Another host key where the sandbox's sshd reads its own, and sshd started again with it
    # (the sandbox's init starts it again when it ends); the core keeps the project's.
    provision = core.paths("keyed")["provision"]
    site.sh("ssh-keygen -q -t ed25519 -N '' -f /tmp/swapped-key && cat /tmp/swapped-key > "
            f"{provision}/ssh_host_ed25519_key && cat /tmp/swapped-key.pub > {provision}/ssh_host_ed25519_key.pub")
    site.sandbox("keyed", "pkill", "-x", "sshd")                       # the listener; a session's process is sshd-session
    deadline = time.monotonic() + 30
    while (seen := terminal(site, "keyed", jar_of(site.page), send="echo marker-$((6*7))\r", seconds=8))["messages"] == [
            {"type": "error", "msg": "connect failed"}]:                # until sshd is back
        assert time.monotonic() < deadline, seen
        time.sleep(1)
    assert seen["messages"] == [{"type": "fingerprint_mismatch"}] and seen["output"] == "", seen
    assert site.sandbox("keyed", "tmux", "has-session", "-t", "main", user="learner", check=False).returncode != 0


def test_the_shell_password_is_in_no_log_no_file_and_opens_no_root_login(site):
    password = site.sh(f"cat {core.paths('apps')['shell_password']}").stdout.strip()
    assert len(password) == 32
    assert "marker-42" in terminal(site, "apps", jar_of(site.page), send="echo marker-$((6*7))\r", until="marker-42")["output"]
    outer_log = run("docker", "logs", site.name)
    web_log = site.inner("logs", "cl-web")
    for text in (outer_log.stdout, outer_log.stderr, web_log.stdout, web_log.stderr):
        assert password not in text
    assert site.sh("ls /run/cl/broker/oplogs").stdout.split(), "no operation log to search"
    searched = ("/run/cl/state.json", "/data/broker/audit.log", "/run/cl/broker/oplogs", core.paths("apps")["workspace"],
                core.paths("apps")["home"])
    found = subprocess.run(["docker", "exec", "-i", site.name, "grep", "-rlF", "-f", "-", *searched], input=password + "\n",
                           capture_output=True, text=True, timeout=120, check=False)
    assert (found.returncode, found.stdout) == (1, ""), found                   # 1: read everything, found nothing
    sandbox = networks_of(site, "cl-sb-apps")["cl-net-apps"]

    def login_as(user):
        return fed(["docker", "exec", "-i", site.name, "docker", "exec", "-i", "cl-web", "python3", "-c", SSH_LOGIN],
                   json.dumps({"host": sandbox, "user": user, "password": password})).strip()

    assert login_as("root") == "denied"
    assert login_as("learner") == "in"                                          # the same login as the sandbox user works


def test_clanker_learns_cookies_do_not_reach_an_app(site):
    fed(["docker", "exec", "-i", site.name, "docker", "exec", "-i", "-u", "learner", "cl-sb-apps", "tee", "/workspace/echo.py"], COOKIE_ECHO)
    serve_in_sandbox(site, "apps", "python3", "/workspace/echo.py", "8003")
    origin = opened(site.page, "apps", 8003)
    session_for(site.page, "apps")
    jar = jar_of(site.page)
    assert {f"cl_broker_{PORT}", f"cl_session_{PORT}_apps"} <= set(jar)
    fed(["docker", "exec", "-i", PEER, "tee", site.page.jar], json.dumps(dict(jar, theme="dark")))
    answer = on_port(site, site.page, origin).get("/broker/x")
    assert answer["status"] == 200 and "theme=dark" in answer["body"], answer
    assert "cl_broker_" not in answer["body"] and "cl_session_" not in answer["body"], answer
