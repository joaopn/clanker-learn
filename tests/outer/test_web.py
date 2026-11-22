"""The web server, `cl-web`, in a real outer container in privileged mode.

Both images are built on the test daemon and put in the outer container's preload folder
before it starts, so that the core loads them and builds nothing. Every request comes from
the neighbour container (`peer`) over HTTPS to the outer container's address on the page's
port, through web_client.py, as a browser on the machine would reach it. Most tests share one
outer container (`site`) on ports other than the defaults; those that stop, restart or mis-set
something take their own.
"""
import functools
import ipaddress
import itertools
import json
import pathlib
import re
import subprocess
import time
import uuid

import broker_auth
import pytest
from conftest import PEER, Outer, fetch_from, run
from cryptography import x509
from test_broker import set_password
from test_outer_container import PATH_WITH_FAKES, fake_tools, in_a_user_namespace, processes
from test_sandboxes import left_of, reach

pytestmark = pytest.mark.integration

PORT = 7790
ENV = {"CL_MODE": "privileged", "CL_WATCH_S": "1", "CL_WEB": "on", "CL_WEB_PORT": str(PORT),
       "CL_ORIGIN_PORTS": "11300-11310", "CL_PASSTHROUGH_PORTS": "11400-11410"}
PASSWORD = "a dummy test password"
FOREIGN = "https://elsewhere.example:7790"
CLIENT = (pathlib.Path(__file__).parent / "web_client.py").read_text()
SETUP_CODE = re.compile(r"The set-up code is ([a-z0-9]{20})\.")
WEB_RESTART_S = 5
# `docker create` of a sandbox fails while /tmp/fail-create exists in the outer container, after
# noting which networks exist then and which the web server is on.
FAILING_CREATE = (
    "#!/bin/sh\n"
    "if [ \"$1\" = create ] && [ -e /tmp/fail-create ]; then\n"
    "    case \" $* \" in *\" --name cl-sb-\"*)\n"
    "        /usr/bin/docker network ls --format '{{.Name}}' > /tmp/networks-at-create\n"
    "        /usr/bin/docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' cl-web > /tmp/web-at-create\n"
    "        echo 'fake docker: create fails' >&2\n"
    "        exit 1;;\n"
    "    esac\n"
    "fi\n"
    "exec /usr/bin/docker \"$@\"\n"
)
# A web server's image definition whose server ends at once, saying why on standard error, and
# one that cannot be built. Neither needs the network: the self-test image is in every inner daemon.
ENDS_AT_ONCE = 'FROM cl-selftest\nENTRYPOINT ["/bin/busybox", "sh", "-c", "echo the test server cannot start >&2; exit 1"]\n'
CANNOT_BE_BUILT = "FROM cl-selftest\nCOPY not-there /opt/\n"


@functools.cache
def proof(password=PASSWORD):
    return broker_auth.derive_login_proof(password)


class Page:
    """The page's routes, asked from the neighbour container. Each Page keeps its own cookies."""

    def __init__(self, outer, port=PORT):
        self.base = f"https://{outer.address()}:{port}"
        self.jar = f"/tmp/jar-{uuid.uuid4().hex[:12]}"

    def ask(self, method, path, **more):
        request = {"url": self.base + path, "method": method, "jar": self.jar, **more}
        done = subprocess.run(["docker", "exec", "-i", PEER, "python3", "-c", CLIENT], input=json.dumps(request),
                              capture_output=True, text=True, timeout=60, check=False)
        assert done.returncode == 0, done.stderr[-800:]
        return json.loads(done.stdout)

    def get(self, path, **more):
        return self.ask("GET", path, **more)

    def post(self, path, body=None, **more):
        return self.ask("POST", path, json=body or {}, **more)

    def cert(self):
        return x509.load_pem_x509_certificate(self.ask("cert", "/")["cert"].encode())


def said(answer):
    return json.loads(answer["body"])


def setup_code(outer):
    codes = SETUP_CODE.findall(run("docker", "logs", outer.name).stderr)
    assert codes, "no set-up code in the log"
    return codes[-1]


def web_lines(outer, after_stop=False):
    """What the log said of the web server, in order; with `after_stop`, only since the last stop."""
    lines = run("docker", "logs", outer.name).stderr.splitlines()
    if after_stop:
        lines = lines[len(lines) - lines[::-1].index("clanker-learn: stopped"):]
    return [line.removeprefix("clanker-learn: web: ") for line in lines if line.startswith("clanker-learn: web: ")]


def networks_of(outer, container):
    shown = outer.inner("inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}", container).stdout.split()
    return dict(pair.split("=") for pair in shown)


def project_networks(outer):
    return set(outer.inner("network", "ls", "--filter", "label=cl.project", "--format", "{{.Name}}").stdout.split())


def answers(page, timeout=30):
    """The page's status, once it answers or the time is up."""
    deadline = time.monotonic() + timeout
    while True:
        status = page.get("/")["status"]
        if status or time.monotonic() > deadline:
            return status
        time.sleep(0.5)


def web_state(outer, test, timeout=60):
    """The state's "web" once `test` holds for it."""
    deadline = time.monotonic() + timeout
    while True:
        state = outer.state()
        if test(state.get("web") or ""):
            return state
        assert time.monotonic() < deadline, f"the web server's state is {state.get('web')!r}; state {state}"
        time.sleep(0.3)


def inner_images(outer, repo):
    return outer.inner("images", "--format", "{{.Repository}}:{{.Tag}}", repo).stdout.split()


def nothing_was_built(outer):
    assert "building" not in run("docker", "logs", outer.name).stderr
    assert outer.inner("network", "inspect", "cl-build", check=False).returncode != 0, "a build was started"


def web_on(one, preload, env=None, **start):
    one.start(env=dict(ENV, **(env or {})), preload=preload, **start)
    assert one.wait()["state"] == "ready", one.state()
    one.wait_web("up", timeout=180)
    return one


@pytest.fixture(scope="module")
def site(image, peer, preload, web_image, sandbox_image, tmp_path_factory):
    """One outer container with the web server on and both images preloaded, shared by the
    tests that only look or use projects of their own."""
    tools = fake_tools(tmp_path_factory.mktemp("site"), {"docker": FAILING_CREATE})
    one = Outer()
    try:
        web_on(one, preload, env={"PATH": PATH_WITH_FAKES}, extra=("-v", f"{tools}:/fake:ro"))
        one.web_image, one.sandbox_image = web_image, sandbox_image
        yield one
    finally:
        one.remove()


# --- the shared container: it comes up -------------------------------------------------------

def test_the_web_server_comes_up_from_the_preloaded_images_and_builds_nothing(site):
    assert site.wait_web("up")["state"] == "ready"
    said_of_web = web_lines(site)
    assert said_of_web == ["preparing", "up"], said_of_web           # "preparing: building ..." had the image been built
    deadline = time.monotonic() + 120
    while site.state().get("sandbox_image") != "ready":
        assert time.monotonic() < deadline and not site.state().get("sandbox_image", "").startswith("failed"), site.state()
        time.sleep(0.5)
    nothing_was_built(site)
    assert inner_images(site, "cl-web") == [site.web_image]
    assert inner_images(site, "cl-sandbox") == [site.sandbox_image]


def directives(header):
    """A content-security header as {directive: [its sources]}."""
    return {words[0]: words[1:] for words in (part.split() for part in header.split(";")) if words}


def test_the_page_and_its_files_come_without_a_login_under_a_strict_content_security_header_and_a_loopback_certificate(site):
    page = Page(site)                                                   # no login, no cookie
    answer = page.get("/")
    assert answer["status"] == 200 and '<script src="/static/app.js" defer></script>' in answer["body"], answer
    assert answer["headers"]["x-content-type-options"] == "nosniff"
    policy = directives(answer["headers"]["content-security-policy"])
    assert policy["script-src"] == ["'self'"] and policy["default-src"] == ["'none'"]
    assert policy["frame-src"] == [f"https://{site.address()}:*"]       # the host the browser asked, nothing else
    assert policy["frame-ancestors"] == ["'none'"] and policy["object-src"] == ["'none'"]
    assert not {"'unsafe-inline'", "'unsafe-eval'"} & set(policy["script-src"] + policy["default-src"])
    for path in ("/static/app.js", "/static/vendor/xterm.js"):
        served = page.get(path)
        assert served["status"] == 200 and served["headers"]["x-content-type-options"] == "nosniff", path
        assert served["headers"]["content-type"].startswith("text/javascript") and len(served["body"]) > 1000, path
    names = page.cert().extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert len(list(names)) == 3
    assert names.get_values_for_type(x509.DNSName) == ["localhost"]
    assert set(names.get_values_for_type(x509.IPAddress)) == {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}


# --- the set-up code is said in the log only ---------------------------------------------------

SEARCH = r'''
import json, os, sys
code = sys.stdin.read().strip().encode()
me, found, read, files = str(os.getpid()), [], [], 0
for pid in os.listdir("/proc"):
    if not pid.isdigit() or pid == me:
        continue
    try:
        environ, cmdline = (open(f"/proc/{pid}/{part}", "rb").read() for part in ("environ", "cmdline"))
    except OSError:
        continue
    read.append(cmdline.replace(b"\0", b" ").decode(errors="replace"))
    found += [f"/proc/{pid}/{part}" for part, data in (("environ", environ), ("cmdline", cmdline)) if code in data]
for top in ("/run/cl", "/data"):
    for base, dirs, names in os.walk(top):
        for name in names:
            path = os.path.join(base, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            files += 1
            with open(path, "rb") as fh:
                if code in fh.read():
                    found.append(path)
print(json.dumps({"found": found, "read": read, "files": files}))
'''


def test_the_setup_code_is_in_the_log_and_nowhere_else(site, peer):
    code = setup_code(site)
    assert code not in json.dumps(site.state())
    status, body = fetch_from(peer["name"], f"http://{site.address()}:8080/state.json")
    assert status == 200 and "boot" in body and code not in body
    searched = json.loads(subprocess.run(["docker", "exec", "-i", site.name, "python3", "-c", SEARCH], input=code,
                                         capture_output=True, text=True, timeout=120, check=True).stdout)
    assert searched["found"] == []
    # The search did read what matters: the init's, the broker's and the web server's processes, and files.
    for process in ("/opt/cl/init.py", "/opt/cl/broker.py serve", "/opt/cl/server.py"):
        assert any(process in line for line in searched["read"]), process
    assert searched["files"] >= 3                                    # the state, the certificate and its key at least
    inspected = site.inner("inspect", "cl-web").stdout
    assert '"CL_WEB_PORT=7790"' in inspected and code not in inspected   # environment, mounts, labels, arguments


# --- cl-web as created ------------------------------------------------------------------------

def capabilities(outer, pid):
    status = outer.sh(f"cat /proc/{pid}/status").stdout
    return {key: value for key, value in (line.split(":\t") for line in status.splitlines() if ":\t" in line) if key in ("CapEff", "CapBnd", "NoNewPrivs")}


SOURCE_ADDRESS = "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('192.0.2.1', 9)); print(s.getsockname()[0])"


def test_cl_web_runs_without_capabilities_or_privileges_and_sees_only_its_two_folders(site):
    web = json.loads(site.inner("inspect", "cl-web").stdout)[0]
    pid = web["State"]["Pid"]
    children = site.sh(f"cat /proc/{pid}/task/{pid}/children").stdout.split()
    assert children, "the server is not running under tini"
    for one in (pid, *children):
        assert capabilities(site, one) == {"CapEff": "0000000000000000", "CapBnd": "0000000000000000", "NoNewPrivs": "1"}, one
    host = web["HostConfig"]
    assert host["SecurityOpt"] == ["no-new-privileges"] and host["Privileged"] is False
    mounts = sorted((m["Source"], m["Destination"], m["RW"]) for m in web["Mounts"])
    # No Docker socket, no project folder or home: the broker's folder read-only, its own data folder,
    # and the secret it shares with the tutor service, read-only.
    assert mounts == [("/data/core/tutor_secret", "/run/cl-secret/tutor", False), ("/data/web", "/data", True),
                      ("/run/cl/broker", "/run/cl-broker", False)]
    write = site.inner("exec", "cl-web", "python3", "-c", "open('/run/cl-broker/written', 'w')", check=False)
    assert write.returncode != 0 and "Read-only file system" in write.stderr
    assert site.sh("test -e /run/cl/broker/written", check=False).returncode != 0
    assert host["PidsLimit"] == 512
    assert host["LogConfig"] == {"Type": "json-file", "Config": {"max-file": "3", "max-size": "10m"}}
    assert host["RestartPolicy"]["Name"] in ("", "no")
    # Its default route leaves by cl-front, even when it is also on a network whose name sorts
    # first (Docker's choice when no network is given a priority).
    front = networks_of(site, "cl-web")["cl-front"]
    site.inner("network", "create", "cl-aaa")
    try:
        site.inner("network", "connect", "cl-aaa", "cl-web")
        assert site.inner("exec", "cl-web", "python3", "-c", SOURCE_ADDRESS).stdout.strip() == front
    finally:
        site.inner("network", "disconnect", "-f", "cl-aaa", "cl-web", check=False)
        site.inner("network", "rm", "cl-aaa", check=False)


# --- the routes, against the real server ------------------------------------------------------

GATED_GETS = ("/broker/projects", "/broker/project/some", "/broker/lifecycle", "/broker/op/some-op", "/broker/op/some-op/log",
              "/broker/op/some-op/alive")
GATED_POSTS = ("/broker/project", "/broker/project/some/limits", "/broker/project/some/start", "/broker/project/some/stop",
               "/broker/project/some/recreate", "/broker/project/some/destroy")
OPEN_POSTS = ("/broker/login", "/broker/logout", "/broker/setup")


def test_every_gated_route_wants_a_session_and_every_post_its_own_origin(site):
    page = Page(site)
    for path in GATED_GETS:
        assert page.get(path)["status"] == 401, path
    for path in GATED_POSTS:
        assert page.post(path, {"name": "some", "pids": 300})["status"] == 401, path
    for path in GATED_POSTS + OPEN_POSTS:
        body = {"name": "some", "code": "x" * 20, "proof": proof()}
        assert page.post(path, body, origin=FOREIGN)["status"] == 403, path
        assert page.post(path, body, origin=None)["status"] == 403, path


def test_a_body_over_one_mebibyte_is_refused_with_413(site):
    page = Page(site)
    assert page.ask("POST", "/broker/login", filler=1024 * 1024)["status"] == 413
    assert page.get("/")["status"] == 200


def follow(page, op_id, timeout=180):
    """An operation followed to its end through the page: (its end, its log as read piece by piece)."""
    deadline, read_from, pieces = time.monotonic() + timeout, 0, []
    while True:
        log = said(page.get(f"/broker/op/{op_id}/log?from={read_from}"))
        assert log["ok"] and log["from"] == read_from
        if log["data"]:
            pieces.append(log["data"])
            read_from = log["next"]
        stands = said(page.get(f"/broker/op/{op_id}"))["result"]
        if stands["state"] != "running":
            break
        assert time.monotonic() < deadline, f"{op_id} is still running"
        time.sleep(0.3)
    rest = said(page.get(f"/broker/op/{op_id}/log?from={read_from}"))
    pieces.append(rest["data"])
    whole = said(page.get(f"/broker/op/{op_id}/log?from=0"))
    assert "".join(pieces) == whole["data"] and whole["next"] == rest["next"] == len(whole["data"].encode())
    assert said(page.get(f"/broker/op/{op_id}/alive")) == {"ok": True, "alive": False}
    return stands, [json.loads(line) for line in whole["data"].splitlines()]


def act(page, path, body=None):
    answer = page.post(path, body)
    assert answer["status"] == 200 and said(answer)["ok"], answer
    stands, records = follow(page, said(answer)["op_id"])
    assert stands["state"] == "done", stands
    assert records[0]["step"] == "accepted" and records[-1]["status"] == "done"
    return stands


def status_of(page, name):
    answer = page.get(f"/broker/project/{name}")
    assert answer["status"] == 200, answer
    return said(answer)["result"]


def test_the_first_password_is_set_with_the_code_and_a_project_lives_and_dies_through_the_page(site):
    page = Page(site)
    assert said(page.get("/broker/setup")) == {"ok": True, "needed": True}
    assert page.post("/broker/setup", {"code": "0" * 20, "proof": proof()})["status"] == 401
    code = setup_code(site)
    assert page.post("/broker/setup", {"code": code, "proof": proof()})["status"] == 200
    assert said(page.get("/broker/setup")) == {"ok": True, "needed": False}
    assert page.post("/broker/setup", {"code": code, "proof": proof("another password")})["status"] == 401

    assert page.post("/broker/login", {"proof": proof("not the password")})["status"] == 401
    login = page.post("/broker/login", {"proof": proof()})
    assert login["status"] == 200 and len(login["set_cookie"]) == 1
    cookie = login["set_cookie"][0]
    assert cookie.startswith(f"cl_broker_{PORT}=")
    assert {"secure", "httponly", "path=/broker"} <= {part.strip().lower() for part in cookie.split(";")}
    assert said(page.get("/broker/projects")) == {"ok": True, "result": []}

    made = act(page, "/broker/project", {"name": "paged", "memory": "512m"})
    assert made["msg"] == "paged is running."
    assert [p["name"] for p in said(page.get("/broker/projects"))["result"]] == ["paged"]
    assert status_of(page, "paged")["state"] == "running"
    assert act(page, "/broker/project/paged/stop")["msg"] == "paged is stopped."
    assert status_of(page, "paged")["state"] == "stopped"
    act(page, "/broker/project/paged/start")
    assert status_of(page, "paged")["state"] == "running"
    before = status_of(page, "paged")["container"]
    act(page, "/broker/project/paged/recreate")
    assert status_of(page, "paged")["state"] == "running" and status_of(page, "paged")["container"] != before
    limited = page.post("/broker/project/paged/limits", {"pids": 300, "memory": "1g"})
    assert limited["status"] == 200 and said(limited)["ok"], limited
    assert status_of(page, "paged")["limits"] == {"pids": 300, "memory": 1024 ** 3, "cpus": None}
    assert said(page.get("/broker/lifecycle")) == {"ok": True, "result": {"ops": []}}

    for body in ({}, {"proof": proof("not the password")}):
        refused = page.post("/broker/project/paged/destroy", body)
        assert refused["status"] == 200 and said(refused)["error"]["kind"] == "step_up_required", refused
    assert status_of(page, "paged")["state"] == "running"
    assert act(page, "/broker/project/paged/destroy", {"proof": proof()})["msg"] == "paged is destroyed."
    assert said(page.get("/broker/projects")) == {"ok": True, "result": []}

    run("docker", "exec", PEER, "cp", page.jar, page.jar + ".old")
    logout = page.post("/broker/logout")
    assert logout["status"] == 200 and said(logout) == {"ok": True}
    assert page.get("/broker/projects")["status"] == 401
    page.jar += ".old"                                                # the cookie of before is no good either
    assert page.get("/broker/projects")["status"] == 401


# --- projects and the web server ---------------------------------------------------------------

def test_from_a_sandbox_the_pages_port_is_not_reachable(site):
    site.cl("create", "fenced")
    web = networks_of(site, "cl-web")
    gateway = site.inner("network", "inspect", "cl-net-fenced", "-f", "{{(index .IPAM.Config 0).Gateway}}").stdout.strip()
    assert reach(site, "cl-sb-fenced", web["cl-net-fenced"], PORT) == "refused"   # nothing listens on its project-side address
    assert reach(site, "cl-sb-fenced", web["cl-front"], PORT) == "timeout"
    assert reach(site, "cl-sb-fenced", gateway, PORT) == "timeout"
    assert reach(site, "cl-sb-fenced", site.address(), PORT) == "timeout"


def test_a_creation_that_fails_while_the_web_server_runs_leaves_no_network(site):
    site.sh("touch /tmp/fail-create")
    try:
        failed = site.cl("create", "failing", check=False)["error"]
    finally:
        site.sh("rm -f /tmp/fail-create")
    assert "fake docker: create fails" in failed
    assert "cl-net-failing" in site.sh("cat /tmp/networks-at-create").stdout.split()
    assert "cl-net-failing" in site.sh("cat /tmp/web-at-create").stdout.split()     # the web server had been put on it
    assert left_of(site, "failing") == []
    assert "cl-net-failing" not in networks_of(site, "cl-web")
    assert site.state()["web"] == "up"


def test_the_web_server_is_on_every_projects_network_and_comes_back_after_a_kill(site):
    site.cl("create", "joined")
    assert "cl-net-joined" in networks_of(site, "cl-web")                 # at once, by the creation itself
    wanted = project_networks(site)
    assert "cl-net-joined" in wanted and wanted <= set(networks_of(site, "cl-web"))
    site.inner("kill", "cl-web")
    down = web_state(site, lambda web: web.startswith("down: "), timeout=WEB_RESTART_S * 3)
    assert down["web"].startswith("down: the web server ended"), down
    web_state(site, lambda web: web == "up", timeout=WEB_RESTART_S * 4)
    assert wanted <= set(networks_of(site, "cl-web"))
    assert answers(Page(site)) == 200


# --- containers of their own ---------------------------------------------------------------

def web_folder(tmp_path, dockerfile):
    folder = tmp_path / "web"
    folder.mkdir()
    folder.chmod(0o755)
    (folder / "Dockerfile").write_text(dockerfile)
    (folder / "Dockerfile").chmod(0o644)
    return folder


def starts_of_cl_web(outer, since, until):
    """When the inner daemon started cl-web between two of its clock's times, in seconds."""
    shown = outer.inner("events", "--since", str(since), "--until", str(until), "--filter", "container=cl-web",
                        "--filter", "event=start", "--format", "{{.TimeNano}}").stdout.split()
    return [int(t) / 1e9 for t in shown]


def test_a_web_server_that_ends_at_once_is_down_with_its_words_and_tried_again_apart(outer, tmp_path):
    folder = web_folder(tmp_path, ENDS_AT_ONCE)
    one = outer().start(env=ENV, extra=("-v", f"{folder}:/opt/cl/web:ro"))
    assert one.wait()["state"] == "ready", one.state()
    web_state(one, lambda web: web.startswith("down"), timeout=180)
    since = float(one.sh("date +%s.%N").stdout)
    for _ in range(16):
        state = one.state()
        assert state["web"] == "down: the web server ended: the test server cannot start", state
        assert state["state"] == "ready" and state["broker"] == "up"
        time.sleep(1)
    starts = starts_of_cl_web(one, since, float(one.sh("date +%s.%N").stdout))
    assert 2 <= len(starts) <= 5, starts
    assert all(later - earlier > 3 for earlier, later in itertools.pairwise(starts)), starts
    assert one.cl("list") == []


def test_a_web_image_that_cannot_be_built_is_down_and_not_tried_again(outer, tmp_path):
    folder = web_folder(tmp_path, CANNOT_BE_BUILT)
    one = outer().start(env=ENV, extra=("-v", f"{folder}:/opt/cl/web:ro"))
    assert one.wait()["state"] == "ready", one.state()
    down = web_state(one, lambda web: web.startswith("down"), timeout=180)["web"]
    assert down.startswith("down: The web server's image could not be built: ") and "not-there" in down, down

    def builds():
        return run("docker", "logs", one.name).stderr.count("building the web server's image")

    assert builds() == 1
    time.sleep(15)
    state = one.state()
    assert (state["state"], state["web"], state["broker"]) == ("ready", down, "up")
    assert builds() == 1
    assert not any("web-keep" in line for line in processes(one))
    assert one.inner("container", "inspect", "cl-web", check=False).returncode != 0


def test_a_new_port_makes_cl_web_again_with_the_same_certificate_and_password(outer, preload):
    one = web_on(outer(), preload, env={"CL_WEB_PORT": "7791"})
    set_password(one, PASSWORD)
    old = Page(one, 7791)
    cert = one.sh("cat /data/web/tls/cert.pem").stdout
    assert old.cert().serial_number == x509.load_pem_x509_certificate(cert.encode()).serial_number
    before = one.inner("inspect", "-f", "{{.Id}}", "cl-web").stdout.strip()

    one.remove(volume=False)
    web_on(one, preload, env={"CL_WEB_PORT": "7792"})
    assert one.inner("inspect", "-f", "{{.Id}}", "cl-web").stdout.strip() != before
    new = Page(one, 7792)
    assert answers(new) == 200
    assert Page(one, 7791).get("/")["status"] == 0                        # nothing answers on the old port
    assert one.sh("cat /data/web/tls/cert.pem").stdout == cert
    assert new.cert().serial_number == x509.load_pem_x509_certificate(cert.encode()).serial_number
    login = new.post("/broker/login", {"proof": proof()})
    assert login["status"] == 200 and login["set_cookie"][0].startswith("cl_broker_7792=")
    assert said(new.get("/broker/projects"))["ok"]


def test_with_the_web_server_off_there_is_none_and_a_later_start_puts_it_on_every_projects_network(outer, preload):
    one = outer().start(env=dict(ENV, CL_WEB="off"), preload=preload)
    assert one.wait()["state"] == "ready", one.state()
    assert one.wait_web("off")["web"] == "off"
    assert one.inner("container", "inspect", "cl-web", check=False).returncode != 0
    assert inner_images(one, "cl-web") == []
    assert SETUP_CODE.search(run("docker", "logs", one.name).stderr) is None
    one.cl("create", "early")
    one.cl("stop", "early")                    # not started again at the next start: only the web server joins its network

    one.remove(volume=False)
    web_on(one, preload)
    assert "cl-net-early" in networks_of(one, "cl-web")

    one.remove(volume=False)
    one.start(env=dict(ENV, CL_WEB="off"), preload=preload)
    assert one.wait()["state"] == "ready", one.state()
    one.wait_web("off")
    assert one.inner("container", "inspect", "cl-web", check=False).returncode != 0   # removed
    assert one.cl("status", "early")["state"] == "stopped"


@pytest.mark.parametrize("setting, value, named", [
    ("CL_WEB", "maybe", "CL_WEB is 'maybe': it must be on or off"),
    ("CL_WEB_PORT", "80", "CL_WEB_PORT is '80': it must be a port number"),
    ("CL_ORIGIN_PORTS", "5-4", "CL_ORIGIN_PORTS is '5': it must be a port number"),
    ("CL_ORIGIN_PORTS", "11300", "CL_ORIGIN_PORTS is '11300': it must be a range of ports"),
    ("CL_ORIGIN_PORTS", "7785-7795", "CL_WEB_PORT and CL_ORIGIN_PORTS overlap"),
    ("CL_WEB_PORT", "8080", "the status page's port and CL_WEB_PORT overlap"),
])
def test_a_wrong_web_setting_leaves_the_container_ready_with_the_web_server_down(outer, preload, setting, value, named):
    one = outer().start(env={**ENV, setting: value}, preload=preload)
    assert one.wait()["state"] == "ready", one.state()
    state = web_state(one, lambda web: web.startswith("down"), timeout=60)
    assert named in state["web"], state["web"]
    assert state["state"] == "ready" and one.cl("list") == []
    assert one.inner("container", "inspect", "cl-web", check=False).returncode != 0


def test_a_status_port_that_is_not_a_port_refuses_and_says_so_on_8080(outer, peer):
    one = outer().start(env=dict(ENV, CL_STATUS_PORT="abc"))
    state = one.wait()
    assert state["state"] == "refused" and "CL_STATUS_PORT is 'abc'" in state["reason"], state
    status, body = fetch_from(peer["name"], f"http://{one.address()}:8080/")
    assert status == 503 and "CL_STATUS_PORT is 'abc'" in body


def test_a_stop_with_the_web_server_up_is_clean_and_the_next_start_builds_nothing(outer, preload):
    one = web_on(outer(), preload)
    boot = one.state()["boot"]
    started = time.monotonic()
    run("docker", "stop", "-t", "60", one.name)
    assert time.monotonic() - started < 40
    assert run("docker", "inspect", "-f", "{{.State.ExitCode}}", one.name).stdout.strip() == "0"
    assert run("docker", "logs", one.name).stderr.splitlines()[-1] == "clanker-learn: stopped"
    run("docker", "start", one.name)
    assert one.wait(after_boot=boot)["state"] == "ready"
    one.wait_web("up", timeout=120)
    assert web_lines(one, after_stop=True) == ["preparing", "up"]    # a web server stopped with the rest is no fault
    nothing_was_built(one)
    assert len(inner_images(one, "cl-web")) == 1
    assert answers(Page(one)) == 200


@pytest.mark.skipif(not in_a_user_namespace(), reason="the tests themselves are not inside a sysbox container")
def test_the_web_server_comes_up_on_the_runtime_of_the_sysbox_mode(outer, preload):
    one = web_on(outer(), preload, env={"CL_MODE": "sysbox"})
    assert one.inner("inspect", "-f", "{{.HostConfig.Runtime}}", "cl-web").stdout.strip() == "crun"
    assert answers(Page(one)) == 200
