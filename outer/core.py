#!/usr/bin/env python3
"""The core: learning projects and their sandboxes.

A project is its record, a small file under /data/core/projects. Everything else a project
has is made from the record by `ensure`, which makes what is missing and touches nothing that
is there: the workspace and home folders, two key pairs, the provisioning folder, a network of
the inner daemon, and the sandbox, one plain container on that network. So a project whose
creation was cut short is finished by its next start.

    /data/core/projects/<name>.json   the record: limits, whether it should be running, its
                                      agents, whether it has the editor, Claude's model and
                                      effort
    /data/projects/<name>             the workspace        -> /workspace in the sandbox
    /data/homes/<name>                the home folder      -> /home/learner
    /data/core/provision/<name>       the tutor's public key, the sandbox's SSH host key,
                                      sshd's settings, which software the project has
                                                           -> /etc/cl-provision, read-only
    /data/dist                        the distributions (below) -> /opt/cl/dist, read-only
    /data/core/keys/<name>            the sandbox's host key pair (what provisioning copies)
                                      and the Shell password (provisioning gets its hash)
    /data/core/ports/<name>.json      the project's ports named by hand
    /data/tutor/keys/<name>           the key the tutor logs in with, and known_hosts
    /data/tutor                       the tutor service's own (its database, the Claude token),
                                      mounted into cl-tutor
    /data/core/tutor_secret           the secret the web server and the tutor service share
    cl-sb-<name>, cl-net-<name>       the container and its network

The software projects get (Claude Code, the editor) is fetched once into a distribution:

    /data/dist/agents/claude          RS's tree: local/ (what Claude Code's installer left as
                                      ~/.local, its editor extension tucked inside) and
                                      claude/settings.json
    /data/dist/agents/claude.json     its versions and when it was pulled (also in .cl-dist inside)
    /data/dist/editor                 RS's tree: .local/ (code-server's standalone install, RS's
                                      list of built-in extensions removed), tools/ (RS's deploy
                                      script and lazy-start stub), templates/ (RS's User and
                                      Machine settings, its six extensions from Open VSX)
    /data/dist/editor.json            the same for the editor, with the extensions' versions
    /data/dist-staging                where a distribution is built before it replaces the old
    /data/versions.local.env          versions raised by Refresh, over /opt/cl/versions.env
    /data/backups                     the backups (`cl backup`): one archive each, all of /data
                                      but the distributions; `cl restore` puts one back

Every project gets its own copy, in its home folder, as research-sandbox does it: the image's
deploy script (/opt/cl/deploy, run as the sandbox user) copies an agent's distribution into
~/.local. The sandbox's init runs it at every start for each agent the provisioning folder
names, only while that agent's launcher is absent, so a copy that updated itself is kept;
`agents_add` and `agents_update` run it in a running sandbox (Update also forces the agent's
editor extension into the project's editor): the one command the core runs inside a sandbox.
The editor is deployed by RS's own script from its distribution (tools/code-server-deploy.sh),
which the init runs at every start of a project that has the editor, after the agents: it
copies the editor into the home when the home has none or another version, and starts RS's
stub on port 8443, which starts code-server on 127.0.0.1:8444 at the first visit.

A sandbox is created without privilege, without a published port, without a restart policy
(the init starts what should run, after the guard rules are in place) and without raw
sockets: the services that will sit on its network also sit on another, and with hand-built
packets a sandbox could address them there. `ping` keeps working through a kernel setting.

Every operation on a project holds that project's lock, an flock on an open file: the kernel
drops it when the holder dies, so nothing stays locked behind a killed caller.

A destroy marks the record first ("destroying") and removes it last. A destroy that was cut
short is therefore never taken for a creation that was: nothing starts such a project, and
running destroy again finishes it.

This module is also the `cl` command: `cl create <name> [--agent claude] [--editor]
[--claude-model M] [--claude-effort E]`, `cl list`, `cl agents add|update|model <name>`,
`cl backup`, `cl backups`, `cl backup-rm <name>`, `cl restore <file or name>`, and so on.
"""
import argparse
import base64
import contextlib
import datetime
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import select
import shlex
import shutil
import ssl
import stat
import string
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

DATA = "/data"
LOCKS = "/run/cl/locks"
SANDBOX_DIR = "/opt/cl/sandbox"
IMAGE_REPO = "cl-sandbox"
WEB_DIR = "/opt/cl/web"
WEB_REPO = "cl-web"
TUTOR_DIR = "/opt/cl/tutor"
TUTOR_REPO = "cl-tutor"
IMAGES = {"sandbox": (SANDBOX_DIR, IMAGE_REPO, "the sandbox image"), "web": (WEB_DIR, WEB_REPO, "the web server's image"),
          "tutor": (TUTOR_DIR, TUTOR_REPO, "the tutor service's image")}
PRELOAD = "/opt/cl/preload"   # <repository>.tar here is loaded where it holds the image wanted; else the image is built
BUILD_NET = "cl-build"
VERSIONS_FILE = "/opt/cl/versions.env"   # the repository's pins; /data/versions.local.env overrides them per key
# The web server's container and the network its ports are published on. The subnet is
# fixed (the web server listens on its address there and nowhere else) and lies outside
# the range the inner daemon draws project networks from.
WEB_CONTAINER = "cl-web"
FRONT_NET = "cl-front"
FRONT_SUBNET = "10.209.0.0/24"
BROKER_RUN = "/run/cl/broker"
WEB_PIDS = 512
WEB_RESTART_S = 5
WEB_SILENT_S = 30
TUTOR_CONTAINER = "cl-tutor"
TUTOR_PORT = 7801
TUTOR_PIDS = 512
TUTOR_ANSWER_S = 15   # after a start, how long the keeper waits for its first answer before going on
TUTOR_SECRET_MOUNT = "/run/cl-secret/tutor"   # in cl-tutor and cl-web: the secret the two share
WEB_DEFAULTS = {"CL_WEB_PORT": "7788", "CL_ORIGIN_PORTS": "11100-11164", "CL_PASSTHROUGH_PORTS": "11200-11299"}
BUILD_TIMEOUT_S = 3600
SANDBOX_UID = 1000
SANDBOX_USER = "learner"
STOP_GRACE_S = 10
SETUP_STATE = "/var/lib/cl/setup"   # in the sandbox: where its init says how the setup recipe went
SETUP_STATES = re.compile(r"none|running|done|failed \(exit -?[0-9]{1,3}\)")
CPU_PERIOD = 100000
DEFAULT_LIMITS = {"pids": 16384, "memory": None, "cpus": None}
MIN_PIDS = 64
MIN_MEMORY = 64 * 1024 * 1024
MIN_CPUS = 0.05
NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,29}[a-z0-9])?")
WANTED = ("running", "stopped", "destroying")
NOT_THERE = re.compile(r"No such (container|image|network|object)|network \S+ not found", re.IGNORECASE)
UNCHANGED = object()
SHELL_PASSWORD_CHARS = string.ascii_letters + string.digits
SHELL_PASSWORD_LENGTH = 32
CRYPT_HASH = re.compile(r"\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}")   # SHA-512 crypt, as /etc/shadow holds it
SSH_PORT = 22
FILES_SERVER_PORT = 7070      # sandbox/files_server.py
EDITOR_PORT = 8443            # the editor: RS's lazy-start stub (outer/editor/code-server-stub.py), run by the deploy the init starts
EDITOR_UPSTREAM_PORT = 8444   # code-server itself, behind the stub, on the sandbox's loopback
DIST_MOUNT = "/opt/cl/dist"   # in the sandbox: /data/dist, read-only
DEPLOY = "/opt/cl/deploy"     # in the sandbox image: sandbox/deploy.sh
DEPLOY_TIMEOUT_S = 900        # one deploy of a distribution into a home
DEPLOY_OUTPUT_BYTES = 4096    # of what a deploy prints, the end: the sandbox's to write
SOFTWARE_FILE = "software.json"   # in the provisioning folder: the project's agents, editor, Claude's model and effort
MODELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")   # RS's models/catalog.json
CLAUDE_MODEL = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,99}\Z")   # RS's CLAUDE_MODEL_RE, with a length
PROC = "/proc"
PROC_NET_BYTES = 1024 * 1024  # of each socket table: a sandbox can open sockets without end
LABEL_CHARS = 64
SSHD_SETTINGS = """\
# Written by clanker-learn at every start of the sandbox.
HostKey /etc/cl-provision/ssh_host_ed25519_key
PasswordAuthentication yes
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers learner
AcceptEnv CL_*
"""


class CoreError(Exception):
    """An operation could not be done; the message is for the person who asked."""


def say_nothing(_message):
    pass


# --- names and where things are ------------------------------------------------------------

def valid_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise CoreError("A project name is 1 to 31 lower-case letters, digits or dashes, and starts and ends with a letter or a digit.")
    return name


def paths(name):
    valid_name(name)
    return {
        "record": f"{DATA}/core/projects/{name}.json",
        "workspace": f"{DATA}/projects/{name}",
        "home": f"{DATA}/homes/{name}",
        "provision": f"{DATA}/core/provision/{name}",
        "host_keys": f"{DATA}/core/keys/{name}",
        "tutor_keys": f"{DATA}/tutor/keys/{name}",
        "shell_password": f"{DATA}/core/keys/{name}/shell_password",
        "ports": f"{DATA}/core/ports/{name}.json",
        "container": f"cl-sb-{name}",
        "network": f"cl-net-{name}",
    }


FOLDERS = ("tutor_keys", "host_keys", "provision", "home", "workspace")


@contextlib.contextmanager
def flocked(filename):
    os.makedirs(LOCKS, exist_ok=True)
    with open(os.path.join(LOCKS, filename), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


# Projects whose lock this process holds already, through a descriptor it was handed (the
# broker takes the lock without waiting and hands it to the child that does the work).
HELD = set()


def locked(name):
    """A project's lock. The name is checked before it becomes part of a path."""
    valid_name(name)
    return contextlib.nullcontext() if name in HELD else flocked(name + ".lock")


def try_lock(name):
    """A project's lock, taken without waiting: an open file that holds it, or None when
    something else does. It is held until that file, and every copy of its descriptor in a
    child, is closed."""
    valid_name(name)
    os.makedirs(LOCKS, exist_ok=True)
    fh = open(os.path.join(LOCKS, name + ".lock"), "w")  # noqa: SIM115 - the caller keeps it, or hands it on
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


# --- limits --------------------------------------------------------------------------------

def parse_size(text):
    """'512m', '2g' or a number of bytes; 'none' is no limit."""
    text = str(text).strip().lower()
    if text == "none":
        return None
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    try:
        return int(float(text[:-1]) * units[text[-1]]) if text[-1:] in units else int(text)
    except (ValueError, OverflowError) as exc:
        raise CoreError(f"{text!r} is not a size: write 512m, 2g, a number of bytes, or none.") from exc


def checked_limits(limits):
    """The limits as stored: every key present, each a sane number or None for no limit."""
    out = dict(DEFAULT_LIMITS)
    out.update(limits or {})
    if set(out) != set(DEFAULT_LIMITS):
        raise CoreError("The limits are pids, memory and cpus.")
    try:
        pids = None if out["pids"] is None else int(out["pids"])
        memory = None if out["memory"] is None else int(out["memory"])
        cpus = None if out["cpus"] is None else float(out["cpus"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise CoreError("A limit is a number, or none.") from exc
    if pids is not None and pids < MIN_PIDS:
        raise CoreError(f"The pids limit is at least {MIN_PIDS}.")
    if memory is not None and memory < MIN_MEMORY:
        raise CoreError("The memory limit is at least 64m.")
    if cpus is not None and not MIN_CPUS <= cpus <= 1024:
        raise CoreError(f"The processor limit is at least {MIN_CPUS}.")
    return {"pids": pids, "memory": memory, "cpus": cpus}


def limit_args(limits, for_update=False):
    """The limits as `docker create` or `docker update` takes them. Memory always comes with an
    equal swap limit (Docker refuses to raise one without the other). Processor time is a
    period and a quota, never --cpus, which a later update cannot change. An update cannot
    remove a memory limit, so none is asked for there."""
    args = ["--pids-limit", str(limits["pids"] if limits["pids"] is not None else -1)]
    if limits["memory"] is not None:
        args += ["--memory", str(limits["memory"]), "--memory-swap", str(limits["memory"])]
    if limits["cpus"] is not None:
        args += ["--cpu-period", str(CPU_PERIOD), "--cpu-quota", str(int(limits["cpus"] * CPU_PERIOD))]
    elif for_update:
        args += ["--cpu-quota", "-1"]
    return args


# --- records -------------------------------------------------------------------------------

def new_record(name, limits=None, agents=(), editor=False, claude_model="", claude_effort=""):
    model, effort = checked_claude_pair(claude_model, claude_effort)
    return {"name": valid_name(name), "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "wanted": "running", "limits": checked_limits(limits), "agents": checked_agents(agents), "editor": checked_editor(editor),
            "claude_model": model, "claude_effort": effort}


def model_catalog():
    """Claude's model tiers and the efforts each takes, as RS has them."""
    try:
        with open(MODELS_FILE) as fh:
            catalog = json.load(fh)
        aliases = {m["alias"]: list(m["efforts"]) for m in catalog["models"]}
        return aliases, list(catalog["efforts"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CoreError(f"The model catalog {MODELS_FILE} cannot be read ({type(exc).__name__}).") from exc


def checked_claude_pair(model, effort):
    """A project's Claude model and effort, as RS checks them (_clean_claude_model,
    _claude_effort_ok): each may be empty (Claude's own default); a model is a tier of the
    catalog or a model id of a safe character set; an effort is one of the catalog's, and for a
    tier only one that tier takes (haiku takes none)."""
    if not isinstance(model, str) or not isinstance(effort, str):
        raise CoreError("The model and the effort are words (or empty).")
    if model and not CLAUDE_MODEL.match(model):
        raise CoreError(f"{model[:40]!r} is not a model: a tier (as opus) or a model id of letters, digits and . _ - [ ].")
    if effort:
        tiers, efforts = model_catalog()
        if effort not in efforts:
            raise CoreError(f"{effort[:40]!r} is not an effort (there is {', '.join(efforts)}).")
        if model in tiers and effort not in tiers[model]:
            raise CoreError(f"{model} takes no effort {effort}" + (f" (it takes {', '.join(tiers[model])})." if tiers[model] else "."))
    return model, effort


def checked_agents(agents):
    """A project's agents: a list of names from the closed set, each once, in the order given."""
    if isinstance(agents, str) or not isinstance(agents, (list, tuple)) or not all(isinstance(one, str) for one in agents):
        raise CoreError("The agents are a list of names.")
    return [known_agent(agent) for agent in dict.fromkeys(agents)]


def checked_editor(editor):
    if not isinstance(editor, bool):
        raise CoreError("editor must be true or false.")
    return editor


def write_record(record):
    write_json(paths(record["name"])["record"], record)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    folder = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(folder)  # the rename itself, so that a power loss does not bring the old record back
    finally:
        os.close(folder)


def mark_destroying(name):
    """From here on nothing starts this project, and a removal that is cut short is finished
    by `destroy`. Where the record cannot be rewritten (a full volume, which removing the
    project is the way out of) it is emptied in place instead: an empty record reads as
    "being destroyed", so nothing starts it either, and destroy still finds it. A record that cannot be read is
    left as it is, for the same reason."""
    path = paths(name)["record"]
    try:
        write_record(dict(read_record(name), wanted="destroying"))
    except CoreError:
        pass
    except OSError:
        with contextlib.suppress(OSError):
            os.truncate(path, 0)


def read_record(name):
    path = paths(name)["record"]
    if emptied(name):
        raise CoreError(f"Project {name} is being destroyed. `destroy {name}` finishes it.")
    try:
        with open(path) as fh:
            record = json.load(fh)
        if record["name"] != name or record["wanted"] not in WANTED or not isinstance(record["created"], str):
            raise ValueError("not this project's record")
        record["limits"] = checked_limits(record["limits"])
        # A record made before projects had agents: none, and the editor, which every
        # sandbox had then.
        record["agents"] = checked_agents(record.get("agents", []))
        record["editor"] = checked_editor(record.get("editor", True))
        record["claude_model"], record["claude_effort"] = checked_claude_pair(record.get("claude_model", ""), record.get("claude_effort", ""))
        return record
    except FileNotFoundError as exc:
        raise CoreError(f"There is no project named {name}.") from exc
    except (OSError, ValueError, KeyError, TypeError, OverflowError, CoreError) as exc:
        raise CoreError(f"The record of project {name} cannot be read ({exc}).") from exc


def emptied(name):
    """True for a record that mark_destroying emptied because it could not rewrite it."""
    try:
        return os.path.getsize(paths(name)["record"]) == 0
    except OSError:
        return False


def recorded_names():
    try:
        files = os.listdir(f"{DATA}/core/projects")
    except FileNotFoundError:
        return []
    return sorted(f[:-5] for f in files if f.endswith(".json") and NAME.fullmatch(f[:-5]))


# --- the inner daemon ----------------------------------------------------------------------

def docker(*args, timeout=120, check=True):
    try:
        done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoreError(f"The inner Docker daemon did not answer `docker {args[0]}`.") from exc
    if check and done.returncode != 0:
        raise CoreError(f"`docker {' '.join(args[:2])}` failed: {done.stderr.strip()[-400:]}")
    return done


def inspected(kind, name):
    """What the daemon says of a container, network or image, or None when it says there is
    none. A daemon that does not answer is an error, never "not there": a destroy must not
    take a silent daemon for a removed container."""
    done = docker(kind, "inspect", name, check=False)
    if done.returncode == 0:
        return json.loads(done.stdout)[0]
    if NOT_THERE.search(done.stderr):
        return None
    raise CoreError(f"The inner Docker daemon could not say whether {name} exists: {done.stderr.strip()[-300:]}")


def there_is(kind, name):
    return inspected(kind, name) is not None


def inspect_container(name):
    return inspected("container", name)


# --- the sandbox image ---------------------------------------------------------------------

def image_files(folder):
    """The files of the image's definition, sorted; compiled Python caches are not part of it."""
    found = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        found += [os.path.relpath(os.path.join(base, f), folder) for f in files if not f.endswith(".pyc")]
    return sorted(found)


def image_tag(folder=None, repo=None):
    """The tag is a hash of the definition: a changed definition is another image, and a
    sandbox keeps the one it was created from until it is recreated."""
    folder = folder or SANDBOX_DIR
    digest = hashlib.sha256()
    for name in image_files(folder):
        with open(os.path.join(folder, name), "rb") as fh:
            digest.update(name.encode() + b"\0" + fh.read() + b"\0")
    return f"{repo or IMAGE_REPO}:{digest.hexdigest()[:12]}"


class ImageError(CoreError):
    """An image could not be had. Not tried again on a timer: at the next start, or when a
    project is created."""


def ensure_image(say=say_nothing, kind="sandbox"):
    """One of clanker-learn's inner images (the sandbox's, the web server's), had if it is not
    there: once on a new installation, and again when its definition changed. It is loaded
    from the preload folder when that holds it, and built otherwise, on a network of its own,
    which the guard rules fence like a sandbox's; one at a time."""
    folder, repo, called = IMAGES[kind]
    tag = image_tag(folder, repo)
    if there_is("image", tag):
        return tag
    with flocked(".build.lock"):
        if there_is("image", tag):
            return tag
        preloaded = f"{PRELOAD}/{repo}.tar"
        if os.path.isfile(preloaded):
            loaded = docker("load", "-i", preloaded, timeout=BUILD_TIMEOUT_S, check=False)
            if loaded.returncode == 0 and there_is("image", tag):
                return tag
            say(f"{preloaded} does not hold {tag}; building it")
        say(f"building {called} (once; this takes a few minutes and needs the internet)")
        if not there_is("network", BUILD_NET):
            docker("network", "create", BUILD_NET)
        done = docker("build", "--network", BUILD_NET, "-t", tag, folder, timeout=BUILD_TIMEOUT_S, check=False)
        if done.returncode != 0:
            raise ImageError(f"{called[0].upper()}{called[1:]} could not be built: " + (done.stderr.strip() or done.stdout.strip())[-600:])
        say(f"{called} is built")
    return tag


# --- the software projects get: pins and distributions -------------------------------------
#
# As research-sandbox does it: the version of each piece of software is pinned (the
# repository's versions.env, which a local file overrides key by key), a throwaway container
# runs the vendor's installer at that version, and what the installer left in its ~/.local
# becomes the distribution, from which every project gets its own copy. A version becomes part
# of a command line and of a path, so nothing that is not digits and dots is ever taken as one.

VERSION = re.compile(r"[0-9]+(\.[0-9]+){1,3}")
DIST_ATTEMPTS = 3
DIST_BUILD_TIMEOUT_S = 1200      # one installer run; the container is ended after it
UPSTREAM_TIMEOUT_S = 15          # curl's --max-time when upstream is asked, as in RS
BUILD_HOME = "/build"            # the build container's home: the staging folder
MARK = ".cl-dist"                # inside a distribution, written last: the tree is whole
# Claude Code's settings, as RS ships them (cli/rscore.py, _AGENT_SETTINGS_JSON), verbatim:
# installed into a project's ~/.claude once, never over the learner's own.
AGENT_SETTINGS_JSON = json.dumps(
    {"permissions": {"defaultMode": "bypassPermissions"}, "theme": "dark",
     "env": {"CLAUDE_CODE_DISABLE_MOUSE_CLICKS": "1"},
     "attribution": {"commit": "", "pr": "", "sessionUrl": False},
     "remoteControlAtStartup": False,
     "feedbackDrafts": "off"},
    indent=2) + "\n"
# Open VSX's name for this machine (RS pins linux-x64; the extension is built for both).
EXT_PLATFORM = {"aarch64": "linux-arm64", "arm64": "linux-arm64"}.get(os.uname().machine, "linux-x64")
# The closed table of what can be pulled. `dist` is the place under /data/dist; `local` where
# the installer's ~/.local goes inside it; `launcher` the
# program, relative to the distribution; `in_tree` where the launcher must lead, with the
# version in it; `installer` the vendor's script and the shell and arguments it is run with.
# An agent's tree is RS's fixed one: local/ (with the agent's editor extension tucked inside
# it, at `ext.file`) and the agent's settings beside it (`files`). The editor's is RS's too:
# .local/, the repository's files of outer/editor/ (`copies`), and the extensions RS bundles
# (`extensions`, each at its own pin, fetched from Open VSX in the build container), with RS's
# list of built-in extensions removed from the install (`strip`).
SOFTWARE = {
    "claude": {
        "kind": "agent", "label": "Claude Code", "key": "CLAUDE_CODE_VERSION", "report": "agents",
        "dist": "agents/claude", "local": "local", "launcher": "local/bin/claude",
        "in_tree": re.compile(r"local/share/claude/versions/([^/]+)"),
        "installer": "https://claude.ai/install.sh", "shell": "bash", "args": ("{version}",),
        "upstream": "https://downloads.claude.ai/claude-code-releases/latest",   # a bare version
        "ext": {"key": "CLAUDE_CODE_EXT_VERSION", "file": "share/rs-agent-ext/anthropic.claude-code.vsix",
                "url": f"https://open-vsx.org/api/Anthropic/claude-code/{EXT_PLATFORM}/{{version}}/file/Anthropic.claude-code-{{version}}@{EXT_PLATFORM}.vsix",
                "upstream": f"https://open-vsx.org/api/Anthropic/claude-code/{EXT_PLATFORM}"},   # JSON, its "version"
        "files": {"claude/settings.json": AGENT_SETTINGS_JSON},
    },
    "code-server": {
        "kind": "editor", "label": "the editor (code-server)", "key": "CODE_SERVER_VERSION", "report": "editor",
        "dist": "editor", "local": ".local", "launcher": ".local/bin/code-server",
        "in_tree": re.compile(r"\.local/lib/code-server-([^/]+)/bin/code-server"),
        "installer": "https://code-server.dev/install.sh", "shell": "sh", "args": ("--method", "standalone", "--version", "{version}"),
        "upstream": "https://github.com/coder/code-server/releases/latest",      # redirects to .../tag/v<version>
        # RS's _CODE_SERVER_STRIP_EXTS (cli/rscore.py), verbatim: "keep grammar/themes/markdown/
        # notebook AND the git stack ...; drop heavy language-servers and JS build/debug tooling".
        "strip": ("typescript-language-features", "html-language-features",
                  "css-language-features", "json-language-features", "php-language-features",
                  "npm", "grunt", "gulp",
                  "jake", "node-debug", "node-debug2", "debug-auto-launch",
                  "debug-server-ready", "references-view", "extension-editing",
                  "simple-browser"),
        # RS's _EDITOR_EXTENSIONS, all universal Open VSX packages, each saved as <ns>.<name>.vsix.
        "extensions": (("ms-python", "python", "PYTHON_EXT_VERSION"),
                       ("ms-toolsai", "jupyter", "JUPYTER_VERSION"),
                       ("ms-toolsai", "vscode-jupyter-cell-tags", "JUPYTER_CELL_TAGS_VERSION"),
                       ("ms-toolsai", "jupyter-keymap", "JUPYTER_KEYMAP_VERSION"),
                       ("ms-toolsai", "jupyter-renderers", "JUPYTER_RENDERERS_VERSION"),
                       ("ms-toolsai", "vscode-jupyter-slideshow", "JUPYTER_SLIDESHOW_VERSION")),
        "copies": {"tools/code-server-deploy.sh": ("code-server-deploy.sh", 0o755),
                   "tools/code-server-stub.py": ("code-server-stub.py", 0o755),
                   "templates/User/settings.json": ("code-server-settings.json", 0o644),
                   "templates/Machine/settings.json": ("code-server-machine-settings.json", 0o644)},
    },
}
EDITOR_FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "editor")   # outer/editor, /opt/cl/editor in the image
OPEN_VSX_VSIX_URL = "https://open-vsx.org/api/{ns}/{name}/{ver}/file/{ns}.{name}-{ver}.vsix"   # RS's _OPEN_VSX_VSIX_URL
EXTENSIONS_DIR = "templates/extensions"   # in the editor's distribution
AGENTS = tuple(one for one, spec in SOFTWARE.items() if spec["kind"] == "agent")
STALE = "stale — re-pull"


def known_software(software):
    if not isinstance(software, str) or software not in SOFTWARE:
        raise CoreError(f"There is no software named {str(software)[:40]!r} to pull (there is {', '.join(SOFTWARE)}).")
    return SOFTWARE[software]


def checked_version(value, called):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise CoreError(f"{called} is not a version (digits and dots, as 2.1.330): {str(value)[:40]!r}.")
    return value


def version_key(value):
    """For comparing versions number by number: 2.1.10 is above 2.1.9, and 2.1 is 2.1.0."""
    numbers = [int(part) for part in checked_version(value, "The version").split(".")]
    return tuple(numbers + [0] * (4 - len(numbers)))


def is_newer(value, than):
    return version_key(value) > version_key(than)


def dist_paths(software):
    spec = known_software(software)
    root = f"{DATA}/dist/{spec['dist']}"
    return {"root": root, "sidecar": root + ".json", "staging": f"{DATA}/dist-staging/{software}",
            "old": f"{DATA}/dist-staging/{software}.old", "container": f"cl-dist-{software}",
            "preload": f"{PRELOAD}/dist/{spec['dist']}", "preload_json": f"{PRELOAD}/dist/{spec['dist']}.json",
            "installer": f"{PRELOAD}/installers/{software}.sh", "upstream": f"{PRELOAD}/upstream/{software}",
            "ext_file": f"{PRELOAD}/installers/{software}.vsix", "ext_upstream": f"{PRELOAD}/upstream/{software}-ext"}


def versions_local():
    return f"{DATA}/versions.local.env"


def parse_pins(path):
    """KEY=VALUE lines of one file, `#` comment lines and blank lines passed over; {} when there
    is no such file. The values are checked where they are used."""
    pins = {}
    try:
        text = read_text(path)
    except FileNotFoundError:
        return pins
    except (OSError, UnicodeDecodeError) as exc:
        raise CoreError(f"{path} cannot be read ({type(exc).__name__}).") from exc
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            if key.strip():
                pins[key.strip()] = value.strip()
    return pins


def load_versions():
    """The pins in effect: the repository's, overlaid key by key with the local override."""
    return {**parse_pins(VERSIONS_FILE), **parse_pins(versions_local())}


def pin(software):
    """The version of `software` in effect, checked."""
    spec = known_software(software)
    value = load_versions().get(spec["key"])
    if value is None:
        raise CoreError(f"No version of {spec['label']} is pinned ({spec['key']} in {VERSIONS_FILE}).")
    return checked_version(value, f"{spec['key']} (in {VERSIONS_FILE} or {versions_local()})")


def ext_pin(software):
    """The version of the agent's editor extension in effect, checked; None for software that
    has none."""
    ext = known_software(software).get("ext")
    if ext is None:
        return None
    value = load_versions().get(ext["key"])
    if value is None:
        raise CoreError(f"No version of the extension of {software} is pinned ({ext['key']} in {VERSIONS_FILE}).")
    return checked_version(value, f"{ext['key']} (in {VERSIONS_FILE} or {versions_local()})")


def extension_pins(software):
    """{"<ns>.<name>": version} of the extensions bundled into `software`'s distribution, at
    the pins in effect, checked; {} for software that bundles none. A pin that is not set is
    refused, as RS's build dies on one: a distribution never ships silently without one."""
    versions, out = load_versions(), {}
    for ns, name, key in known_software(software).get("extensions", ()):
        if versions.get(key) is None:
            raise CoreError(f"No version of the extension {ns}.{name} is pinned ({key} in {VERSIONS_FILE}).")
        out[f"{ns}.{name}"] = checked_version(versions[key], f"{key} (in {VERSIONS_FILE} or {versions_local()})")
    return out


def pin_keys(software):
    spec = known_software(software)
    return [spec["key"], *([spec["ext"]["key"]] if "ext" in spec else []), *(key for _, _, key in spec.get("extensions", ()))]


def write_pins(lines):
    """The override file, replaced whole: a reader never sees half of it."""
    os.makedirs(DATA, exist_ok=True)
    write_file(versions_local(), "".join(line + "\n" for line in lines), 0o644)


def pin_lines_without(key):
    """The override's lines but those that set `key` (all of them: the last one would win)."""
    try:
        lines = read_text(versions_local()).splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise CoreError(f"{versions_local()} cannot be read ({type(exc).__name__}).") from exc
    return [line for line in lines if line.strip().startswith("#") or line.partition("=")[0].strip() != key]


def set_version_pin(key, value):
    """Raise a pin in the local override. Never down, and an unchanged pin is not written (a
    line that only repeats the repository's value would keep the pin from following the
    repository later). True when the file was written."""
    if key not in {key for software in SOFTWARE for key in pin_keys(software)}:
        raise CoreError(f"{key!r} is not a version clanker-learn pins.")
    checked_version(value, f"The new {key}")
    with flocked("versions.lock"):
        current = load_versions().get(key)
        if current is not None and VERSION.fullmatch(current):   # one that is not a version is replaced
            if version_key(value) == version_key(current):
                return False
            if version_key(value) < version_key(current):
                raise CoreError(f"{key} is {current}; {value} would move it down, so nothing was changed.")
        lines = pin_lines_without(key)
        if lines is None:
            lines = ["# Written by clanker-learn: versions raised over /opt/cl/versions.env (see its header)."]
        write_pins([*lines, f"{key}={value}"])
        return True


def pin_reset(software):
    """"Use repo version": take the software's keys (an agent's and its extension's) out of the
    local override. Nothing to take out is no error. Refused while that software is being built."""
    spec = known_software(software)
    with build_lane(software), flocked("versions.lock"):
        removed = False
        for key in pin_keys(software):
            if key in parse_pins(versions_local()):
                write_pins(pin_lines_without(key))
                removed = True
    return {"software": software, "key": spec["key"], "removed": removed, "version": pin(software)}


# The build lane: one build at a time, refused rather than queued. Two locks, flocks on open
# files as a project's lock is, so the kernel drops them when the holder dies: the lane itself,
# and one per software, which says what is being built to anyone who asks without waiting.

LANE_HELD = None     # the software whose lane this process was handed (by a parent that took it)


def lane_file(name):
    os.makedirs(LOCKS, exist_ok=True)
    return open(os.path.join(LOCKS, name), "a")


def building_now():
    """The software being built now, or None."""
    for software in SOFTWARE:
        with lane_file(f"dist-{software}.lock") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return software
    return None


def take_build_lane(software):
    """The lane for building `software`, taken without waiting: two open files that hold it
    until they are closed. Refused, naming what is being built, while another build runs."""
    spec = known_software(software)
    lane = lane_file("dist.lock")
    try:
        fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lane.close()
        now = building_now()
        what = SOFTWARE[now]["label"] if now else "Something"
        raise CoreError(f"{what[0].upper()}{what[1:]} is being built now; try again when that is done.") from None
    own = lane_file(f"dist-{software}.lock")
    try:
        fcntl.flock(own, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        own.close()
        lane.close()
        raise CoreError(f"{spec['label'][0].upper()}{spec['label'][1:]} is in use; try again in a moment.") from None
    return [lane, own]


@contextlib.contextmanager
def build_lane(software):
    if LANE_HELD == software:
        yield
        return
    held = take_build_lane(software)
    try:
        yield
    finally:
        for fh in reversed(held):
            fh.close()


def now_utc():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def pulled(software):
    """What is pulled of `software`: its mark ({"version", "pulled_at"}), or None when there is
    no whole distribution (no mark, a mark that does not read, no launcher)."""
    spec, p = known_software(software), dist_paths(software)
    try:
        with open(os.path.join(p["root"], MARK)) as fh:
            mark = json.loads(fh.read(4096))
        if not os.path.lexists(os.path.join(p["root"], spec["launcher"])):
            return None
        out = {"version": checked_version(mark["version"], "The pulled version"), "pulled_at": str(mark.get("pulled_at", ""))}
        if "ext" in spec:
            out["ext_version"] = checked_version(mark["ext_version"], "The pulled extension's version")
        if "extensions" in spec:
            out["extensions"] = {str(one)[:80]: checked_version(version, "A bundled extension's version") for one, version in mark["extensions"].items()}
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError, CoreError):
        return None


def relativize_launcher(root, launcher, local=""):
    """The installer links the launcher to an absolute path under the build's home, which
    leads nowhere in a project's home: it is made relative, as RS does. Any other absolute
    link is left for the check to refuse."""
    path = os.path.join(root, launcher)
    if not os.path.islink(path):
        return
    target = os.readlink(path)
    prefix = BUILD_HOME + "/.local/"
    if target.startswith(prefix):
        relative = os.path.relpath(os.path.join(root, local, target[len(prefix):]), os.path.dirname(path))
        tmp = path + ".cl-tmp"
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
        os.symlink(relative, tmp)
        os.replace(tmp, path)


def check_tree(software, root, version):
    """A tree is taken only if its launcher is there, leads (through relative links only) to
    a program inside the tree, and that program is the version wanted. Raises with why not."""
    spec = known_software(software)
    path = os.path.join(root, spec["launcher"])
    if not os.path.lexists(path):
        raise CoreError(f"the installer left no {spec['launcher']}")
    if os.path.islink(path) and os.path.isabs(os.readlink(path)):
        raise CoreError(f"{spec['launcher']} is a link to {os.readlink(path)[:200]}, outside the distribution")
    real, top = os.path.realpath(path), os.path.realpath(root)
    if not real.startswith(top + os.sep):
        raise CoreError(f"{spec['launcher']} leads outside the distribution")
    try:
        mode = os.stat(real).st_mode
    except OSError as exc:
        raise CoreError(f"{spec['launcher']} leads nowhere ({type(exc).__name__})") from exc
    if not stat.S_ISREG(mode) or not mode & 0o111:
        raise CoreError(f"{spec['launcher']} does not lead to a program")
    found = spec["in_tree"].fullmatch(os.path.relpath(real, top))
    if not found:
        raise CoreError(f"{spec['launcher']} leads to {os.path.relpath(real, top)[:200]}, not to a version of {spec['label']}")
    if found.group(1) != version:
        raise CoreError(f"the distribution holds version {found.group(1)[:40]}, not {version}")


def remove_path(path):
    """Remove a folder or a file, without following a link."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)


def fresh_folder(path, owner=None):
    """An empty folder at `path`: whatever an earlier build left there is removed first."""
    remove_path(path)
    os.makedirs(path, mode=0o755)
    if owner is not None:
        os.chown(path, owner, owner)


def arrange_tree(software, captured):
    """RS's fixed tree around the ~/.local an installer left: for an agent local/ and the
    agent's settings beside it; for the editor .local/, the repository's tools and settings,
    and the extensions the build fetched into ext/ beside the ~/.local, as RS's
    _editor_build_dist assembles it. What the installer left in state/ (its locks) is not part
    of it."""
    spec = known_software(software)
    if os.path.islink(captured) or not os.path.isdir(captured):   # before anything is written into it: never through a link
        raise CoreError("the installer left no folder of its own (a link, or nothing)")
    remove_path(os.path.join(captured, "state"))
    tree = os.path.join(os.path.dirname(captured), "tree")
    fresh_folder(tree)
    os.rename(captured, os.path.join(tree, spec["local"]))
    for relative, text in spec.get("files", {}).items():
        os.makedirs(os.path.dirname(os.path.join(tree, relative)), mode=0o755, exist_ok=True)
        write_file(os.path.join(tree, relative), text, 0o644)
    for relative, (source, mode) in spec.get("copies", {}).items():
        os.makedirs(os.path.dirname(os.path.join(tree, relative)), mode=0o755, exist_ok=True)
        shutil.copyfile(os.path.join(EDITOR_FILES, source), os.path.join(tree, relative))
        os.chmod(os.path.join(tree, relative), mode)
    if "extensions" in spec:
        fetched = os.path.join(os.path.dirname(captured), "ext")
        os.makedirs(os.path.join(tree, EXTENSIONS_DIR), mode=0o755)
        for ns, name, _ in spec["extensions"]:
            source = os.path.join(fetched, f"{ns}.{name}.vsix")
            if os.path.islink(source) or not os.path.isfile(source):
                raise CoreError(f"the build fetched no {ns}.{name}.vsix")
            os.rename(source, os.path.join(tree, EXTENSIONS_DIR, f"{ns}.{name}.vsix"))
    return tree


def install_dist(software, tree, version, say, ext_version=None, extensions=None):
    """Mark a checked tree and put it in the distribution's place by renames: the old one is
    moved aside only once the new one is whole, and removed only after the new one is in. The
    folders above are never replaced: running sandboxes have /data/dist mounted."""
    p, spec = dist_paths(software), SOFTWARE[software]
    if os.path.islink(tree) or not os.path.isdir(tree):   # before anything is written into it: never through a link
        raise CoreError("the installer left no folder of its own (a link, or nothing)")
    relativize_launcher(tree, spec["launcher"], spec["local"])
    check_tree(software, tree, version)
    mark = {"version": version, "pulled_at": now_utc()}
    if "ext" in spec:
        # The extension rides inside local/ (RS's "tuck"): a tree without it is refused.
        ext_file = os.path.join(tree, spec["local"], spec["ext"]["file"])
        if os.path.islink(ext_file) or not os.path.isfile(ext_file) or not os.path.getsize(ext_file):
            raise CoreError(f"the distribution has no editor extension ({spec['local']}/{spec['ext']['file']})")
        mark["ext_version"] = checked_version(ext_version, "The extension's version")
    wanted = [*spec.get("files", {}), *spec.get("copies", {}), *(f"{EXTENSIONS_DIR}/{ns}.{name}.vsix" for ns, name, _ in spec.get("extensions", ()))]
    for relative in wanted:
        path = os.path.join(tree, relative)
        if os.path.islink(path) or not os.path.isfile(path) or not os.path.getsize(path):
            raise CoreError(f"the distribution has no {relative}")
    if "extensions" in spec:
        mark["extensions"] = {str(one): checked_version(v, f"The version of the extension {str(one)[:80]}") for one, v in dict(extensions or {}).items()}
    write_json(os.path.join(tree, MARK), mark)   # last: a tree without it was cut short, and is not copied
    os.makedirs(os.path.dirname(p["root"]), mode=0o755, exist_ok=True)
    remove_path(p["old"])   # left by a swap that was cut short
    if os.path.lexists(p["root"]):
        os.rename(p["root"], p["old"])
    try:
        os.rename(tree, p["root"])
    except OSError:
        if os.path.lexists(p["old"]) and not os.path.lexists(p["root"]):
            os.rename(p["old"], p["root"])   # the old one back, rather than none
        raise
    write_json(p["sidecar"], mark)
    if os.path.lexists(p["old"]):
        shutil.rmtree(p["old"], ignore_errors=True)
    say(f"{SOFTWARE[software]['label']} {version} is pulled")
    return dict(mark, software=software)


DROPPED = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{SANDBOX_UID}:{SANDBOX_UID}")


def build_dist(software, version, say=say_nothing, ext_version=None):
    """Run the installer of `software` at `version` in a throwaway container of the sandbox
    image, on the fenced build network, with no capability, as the sandbox's user, its home the
    staging folder; for an agent, fetch its editor extension (at `ext_version`, else its pin)
    into the ~/.local it left, as RS does; for the editor, remove RS's list of built-in
    extensions from the install and fetch the bundled extensions at their pins, as RS's
    _editor_build_dist does; check what it left, and install it. Three attempts, as RS."""
    spec, p = known_software(software), dist_paths(software)
    checked_version(version, f"The version of {spec['label']}")
    ext_version = (ext_version or ext_pin(software)) if "ext" in spec else None
    extensions = extension_pins(software)
    image = ensure_image(say, "sandbox")
    if not there_is("network", BUILD_NET):
        docker("network", "create", BUILD_NET)
    os.makedirs(f"{DATA}/dist-staging", mode=0o700, exist_ok=True)
    args = [arg.format(version=version) for arg in spec["args"]]
    mounts, script = [], f'set -euo pipefail; curl -fsSL "$0" | {spec["shell"]} -s -- "$@"'
    if os.path.isfile(p["installer"]):   # the tests' way in: the build network reaches no test server
        mounts, script = ["-v", f"{p['installer']}:/opt/cl-installer:ro"], f'set -euo pipefail; {spec["shell"]} /opt/cl-installer "$@"'
    if ext_version is not None:
        checked_version(ext_version, f"The version of the extension of {spec['label']}")
        place = f'"$HOME/.local/{spec["ext"]["file"]}"'
        script += f'; mkdir -p "$(dirname {place})"; '
        if os.path.isfile(p["ext_file"]):   # the tests' way in, as the installer's
            mounts += ["-v", f"{p['ext_file']}:/opt/cl-ext.vsix:ro"]
            script += f"cp /opt/cl-ext.vsix {place}"
        else:
            mounts += ["-e", "CL_EXT_URL=" + spec["ext"]["url"].format(version=ext_version)]
            script += f'curl -fsSL --compressed -o {place} "$CL_EXT_URL"'
    if "strip" in spec:
        # RS's lines (cli/rscore.py, _editor_build_dist): the names and versions are the closed
        # table's and checked pins, nothing a request brings.
        strip = "; ".join(f'rm -rf "$EXT_DIR/{one}" || true' for one in spec["strip"])
        script += ('; EXT_DIR="$(find ~/.local/lib -maxdepth 6 -type d -name extensions | head -n1)"; '
                   '[ -n "$EXT_DIR" ] || { echo "code-server extensions dir not found" >&2; exit 1; }; ' + strip)
    if extensions:
        script += '; mkdir -p "$HOME/ext"; ' + "; ".join(
            f'curl -fsSL --compressed -o "$HOME/ext/{ns}.{name}.vsix" ' + shlex.quote(OPEN_VSX_VSIX_URL.format(ns=ns, name=name, ver=extensions[f"{ns}.{name}"]))
            for ns, name, _ in spec["extensions"])
    last = ""
    try:
        for attempt in range(1, DIST_ATTEMPTS + 1):
            docker("rm", "-f", "-v", p["container"], check=False, timeout=STOP_GRACE_S + 120)   # a leftover of a build that was cut
            fresh_folder(p["staging"], SANDBOX_UID)
            say(f"installing {spec['label']} {version} (attempt {attempt} of {DIST_ATTEMPTS})")
            try:
                done = docker("run", "--rm", "--name", p["container"], "--network", BUILD_NET, *DROPPED,
                              "-e", f"HOME={BUILD_HOME}", "-v", f"{p['staging']}:{BUILD_HOME}", *mounts,
                              "--entrypoint", "timeout", image, str(DIST_BUILD_TIMEOUT_S), "bash", "-c", script, spec["installer"], *args,
                              timeout=DIST_BUILD_TIMEOUT_S + 120, check=False)
            except CoreError as exc:
                last = str(exc)
                continue
            if done.returncode != 0:
                last = f"the installer failed (exit {done.returncode}): " + (done.stderr.strip() or done.stdout.strip())[-500:]
                continue
            try:
                return install_dist(software, arrange_tree(software, os.path.join(p["staging"], ".local")), version, say, ext_version, extensions)
            except CoreError as exc:
                last = str(exc)
    finally:
        docker("rm", "-f", "-v", p["container"], check=False, timeout=STOP_GRACE_S + 120)
        if os.path.lexists(p["staging"]):
            shutil.rmtree(p["staging"], ignore_errors=True)
    raise CoreError(f"{spec['label'][0].upper()}{spec['label'][1:]} {version} could not be pulled in {DIST_ATTEMPTS} attempts: {last}")


def take_preloaded(software, say=say_nothing):
    """A ready distribution from the preload folder, copied, checked, marked and installed as
    a built one is. None when there is none."""
    spec, p = known_software(software), dist_paths(software)
    if not os.path.isdir(p["preload"]) or not os.path.isfile(p["preload_json"]):
        return None
    try:
        with open(p["preload_json"]) as fh:
            given = json.loads(fh.read(4096))
        version = checked_version(given["version"], f"The version in {p['preload_json']}")
        ext_version = checked_version(given["ext_version"], f"The extension's version in {p['preload_json']}") if "ext" in spec else None
        extensions = given.get("extensions", extension_pins(software)) if "extensions" in spec else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CoreError(f"{p['preload_json']} cannot be read ({type(exc).__name__}).") from exc
    say(f"taking {spec['label']} {version} from {p['preload']}")
    os.makedirs(f"{DATA}/dist-staging", mode=0o700, exist_ok=True)
    fresh_folder(p["staging"])
    try:
        tree = os.path.join(p["staging"], "tree")
        shutil.copytree(p["preload"], tree, symlinks=True)
        return install_dist(software, tree, version, say, ext_version, extensions)
    finally:
        shutil.rmtree(p["staging"], ignore_errors=True)


def software_pull(software, say=say_nothing):
    """Pull `software` again at the version in effect (Settings -> Software, Pull)."""
    known_software(software)
    with build_lane(software):
        return build_dist(software, pin(software), say)


def first_pull(software, say=say_nothing, build=True):
    """What the keeper does at a first start: a distribution handed in through the preload
    folder is taken; otherwise it is built, if `build` (the sandbox image is there)."""
    spec = known_software(software)
    with build_lane(software):
        there = pulled(software)
        if there is not None:
            return dict(there, software=software)
        taken = take_preloaded(software, say)
        if taken is not None:
            return taken
        if not build:
            raise CoreError(f"{spec['label'][0].upper()}{spec['label'][1:]} is built in a container of the sandbox image, which could not be had.")
        return build_dist(software, pin(software), say)


def upstream_version(software):
    """The newest version upstream, asked from a throwaway container on the build network, as
    RS does (or read from the preload folder, the tests' way in). Checked as a version."""
    spec, p = known_software(software), dist_paths(software)
    if os.path.isfile(p["upstream"]):
        answer = read_bounded(p["upstream"], 256).strip()
    else:
        image = ensure_image(kind="sandbox")
        if not there_is("network", BUILD_NET):
            docker("network", "create", BUILD_NET)
        how = ["--max-filesize", "1024", spec["upstream"]] if software == "claude" else ["-o", "/dev/null", "-w", "%{url_effective}", spec["upstream"]]
        try:
            done = docker("run", "--rm", "--network", BUILD_NET, *DROPPED, "--entrypoint", "curl", image,
                          "-fsSL", "--max-time", str(UPSTREAM_TIMEOUT_S), *how, timeout=UPSTREAM_TIMEOUT_S + 60, check=False)
        except CoreError as exc:
            raise CoreError(f"Upstream could not be asked for the newest version of {spec['label']}: {exc}") from exc
        if done.returncode != 0:
            raise CoreError(f"Upstream could not be asked for the newest version of {spec['label']}: {(done.stderr.strip() or 'curl failed')[-300:]}")
        answer = done.stdout.strip()
        if software == "code-server":
            # The version rides the redirect to .../releases/tag/v<version>; without one the
            # address still ends in "latest", which must never become a pin.
            if "/releases/tag/" not in answer:
                raise CoreError(f"Upstream did not name a release of {spec['label']} (it answered {answer[:120]!r}).")
            answer = answer.rsplit("/", 1)[-1].removeprefix("v")
    return checked_version(answer, f"Upstream's newest version of {spec['label']}")


def upstream_ext_version(software):
    """The newest version of an agent's editor extension on Open VSX (its metadata's
    "version"), asked as upstream_version asks, or read from the preload folder."""
    spec, p = known_software(software), dist_paths(software)
    if os.path.isfile(p["ext_upstream"]):
        answer = read_bounded(p["ext_upstream"], 256).strip()
    else:
        image = ensure_image(kind="sandbox")
        if not there_is("network", BUILD_NET):
            docker("network", "create", BUILD_NET)
        try:
            done = docker("run", "--rm", "--network", BUILD_NET, *DROPPED, "--entrypoint", "curl", image, "-fsSL", "--max-time",
                          str(UPSTREAM_TIMEOUT_S), "--max-filesize", "4000000", spec["ext"]["upstream"], timeout=UPSTREAM_TIMEOUT_S + 60, check=False)
        except CoreError as exc:
            raise CoreError(f"Open VSX could not be asked for the newest extension of {spec['label']}: {exc}") from exc
        if done.returncode != 0:
            raise CoreError(f"Open VSX could not be asked for the newest extension of {spec['label']}: {(done.stderr.strip() or 'curl failed')[-300:]}")
        try:
            answer = json.loads(done.stdout).get("version")
        except (ValueError, AttributeError, RecursionError):
            answer = None
    return checked_version(answer, f"Open VSX's newest version of the extension of {spec['label']}")


def refresh_check(software):
    """The version in effect, upstream's newest, and whether upstream's is newer; for an agent
    also its editor extension's two, resolved independently as RS does (Open VSX can trail
    the release by hours), and "newer" when either is. Changes nothing."""
    current, latest = pin(software), upstream_version(software)
    out = {"software": software, "version": current, "upstream": latest, "newer": is_newer(latest, current)}
    if "ext" in SOFTWARE[software]:
        ext_current, ext_latest = ext_pin(software), upstream_ext_version(software)
        out.update(ext_version=ext_current, ext_upstream=ext_latest, newer=out["newer"] or is_newer(ext_latest, ext_current))
    return out


def software_refresh(software, say=say_nothing):
    """Refresh: if upstream has a newer version (of an agent or of its extension), raise the
    pins that are behind and pull; otherwise there is nothing to do (a pin is never moved
    down)."""
    spec = known_software(software)
    with build_lane(software):
        say(f"asking upstream for the newest version of {spec['label']}")
        check = refresh_check(software)
        if not check["newer"]:
            say(f"{spec['label']} {check['version']} is in effect and upstream's newest is {check['upstream']}: nothing to do")
            return dict(check, bumped=False)
        version = check["upstream"] if is_newer(check["upstream"], check["version"]) else check["version"]
        ext_version = None
        if "ext" in spec:
            ext_version = check["ext_upstream"] if is_newer(check["ext_upstream"], check["ext_version"]) else check["ext_version"]
            if ext_version != check["ext_version"]:
                say(f"raising {spec['ext']['key']} from {check['ext_version']} to {ext_version}")
                set_version_pin(spec["ext"]["key"], ext_version)
        if version != check["version"]:
            say(f"raising {spec['key']} from {check['version']} to {version}")
            set_version_pin(spec["key"], version)
        out = dict(check, previous=check["version"], version=version, bumped=True, pulled=build_dist(software, version, say, ext_version))
        if "ext" in spec:
            out.update(previous_ext=check["ext_version"], ext_version=ext_version)
        return out


# Named as RS names them.
def known_agent(agent):
    if agent not in AGENTS:
        raise CoreError(f"There is no agent named {str(agent)[:40]!r} (there is {', '.join(AGENTS)}).")
    return agent


def refuse_unpulled(agents, editor):
    """What a project is to be given must be pulled; refused in RS's words where it is not."""
    for agent in agents:
        if pulled(agent) is None:
            raise CoreError(f"agent {agent!r}: no cached dist — pull it under Settings → Software first")
    if editor and pulled("code-server") is None:
        raise CoreError("editor: no cached dist — pull it under Settings → Software first")


def agent_pull(agent="claude", say=say_nothing):
    return software_pull(known_agent(agent), say)


def agent_refresh_check(agent="claude"):
    return refresh_check(known_agent(agent))


def agent_refresh(agent="claude", say=say_nothing):
    return software_refresh(known_agent(agent), say)


def editor_pull(say=say_nothing):
    return software_pull("code-server", say)


def editor_refresh_check():
    return refresh_check("code-server")


def editor_refresh(say=say_nothing):
    return software_refresh("code-server", say)


def dist_status_word(have, version, ext_version=None, extensions=None):
    if have is None:
        return "not pulled"
    same = have["version"] == version and have.get("ext_version") == ext_version and have.get("extensions") == extensions
    return "up to date" if same else STALE


def software_status():
    """One row per software, for Settings -> Software: what is pulled, the version in effect
    and the repository's, whether the local override sets it (so "Use repo version" can be
    offered), a status word, and whether it is being built now. The editor's row also lists
    its bundled extensions (RS's sub-rows): each one's pin key, the version in effect, the
    repository's and the one pulled. Reads files only."""
    rows, now = [], building_now()
    try:
        base, problem = parse_pins(VERSIONS_FILE), ""
        override = parse_pins(versions_local())
    except CoreError as exc:
        base, override, problem = {}, {}, str(exc)
    for software, spec in SOFTWARE.items():
        row = {"id": software, "kind": spec["kind"], "row": f"{spec['kind']}: {software}", "label": spec["label"],
               "key": spec["key"], "pulled": pulled(software), "version": None, "repo_version": base.get(spec["key"]),
               "overridden": spec["key"] in override, "building": now == software}
        ext_version = extensions = None
        if "ext" in spec:
            row.update(ext_key=spec["ext"]["key"], ext_version=None, ext_repo_version=base.get(spec["ext"]["key"]))
            row["overridden"] = row["overridden"] or spec["ext"]["key"] in override
        if "extensions" in spec:
            had = (row["pulled"] or {}).get("extensions", {})
            row["extensions"] = [{"id": f"{ns}.{name}", "key": key, "version": None, "repo_version": base.get(key),
                                  "pulled": had.get(f"{ns}.{name}")} for ns, name, key in spec["extensions"]]
            row["overridden"] = row["overridden"] or any(key in override for _, _, key in spec["extensions"])
        try:
            row["version"] = pin(software)
            if "ext" in spec:
                row["ext_version"] = ext_version = ext_pin(software)
            if "extensions" in spec:
                extensions = extension_pins(software)
                for one in row["extensions"]:
                    one["version"] = extensions[one["id"]]
        except CoreError as exc:
            row["problem"] = problem or str(exc)
        # A version that cannot be read is never "up to date"; an agent is stale when either
        # its pin or its extension's moved since the pull, the editor when any bundled pin did.
        row["status"] = dist_status_word(row["pulled"], row["version"], ext_version, extensions)
        rows.append(row)
    return rows


def keep_software(report, image_ready):
    """The keeper's first pulls, once the sandbox image is had: for the agent and then the
    editor, a distribution that is there is said ready; one that is not is pulled, once."""
    for software, spec in SOFTWARE.items():
        key = spec["report"]
        try:
            there = pulled(software)
            if there is None:
                report(key, "pulling")
                there = first_pull(software, lambda message, key=key: report(key, f"pulling: {message}"), build=image_ready)
            report(key, f"ready: {there['version']}")
        except (CoreError, OSError) as exc:
            report(key, f"failed: {exc}")


# --- making what a project needs -----------------------------------------------------------

def container_args(record, image):
    """What `docker create` is given for a project's sandbox."""
    p = paths(record["name"])
    return [
        "--name", p["container"], "--hostname", record["name"], "--network", p["network"],
        "--label", f"cl.project={record['name']}",
        "--cap-drop", "NET_RAW", "--sysctl", "net.ipv4.ping_group_range=0 2147483647",
        "-v", f"{p['workspace']}:/workspace",
        "-v", f"{p['home']}:/home/{SANDBOX_USER}",
        "-v", f"{p['provision']}:/etc/cl-provision:ro",
        # Every distribution, read-only: what the sandbox's deploy script copies from. The
        # folder above them all, because a Pull swaps a distribution by renames inside it,
        # which a running sandbox sees (a mount of the distribution itself would stay on the
        # folder that was renamed away).
        "-v", f"{DATA}/dist:{DIST_MOUNT}:ro",
        *limit_args(record["limits"]),
        image,
    ]


def keygen(path, comment):
    """A key pair at `path` and `path`.pub, unless both are there. One half alone is what a
    killed ssh-keygen leaves: it is removed and the pair made again."""
    if all(os.path.exists(half) and os.path.getsize(half) for half in (path, path + ".pub")):
        return
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    for half in (path, path + ".pub"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(half)
    try:
        done = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", path],
                              capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoreError(f"A key could not be made ({exc}).") from exc
    if done.returncode != 0:
        raise CoreError(f"A key could not be made: {done.stderr.strip()[-300:]}")


def write_file(path, text, mode):
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_text(path):
    with open(path) as fh:
        return fh.read()


def shell_password(name, make=False):
    """The project's Shell password: the sandbox user's, which the web server's terminal logs
    in with. Made once, with the core's keys for the project (not the tutor's), and kept;
    "" when there is none and `make` is false."""
    path = paths(name)["shell_password"]
    with contextlib.suppress(FileNotFoundError):
        password = read_text(path).strip()
        if password or not make:
            return password
    if not make:
        return ""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    password = "".join(secrets.choice(SHELL_PASSWORD_CHARS) for _ in range(SHELL_PASSWORD_LENGTH))
    write_file(path, password + "\n", 0o600)
    return password


def crypt_hash(password):
    """The password's SHA-512 crypt hash, as /etc/shadow holds it. The password reaches openssl
    on its standard input: a command line can be read by every process of this container. No
    error text here repeats what openssl said or was given."""
    try:
        done = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n",
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CoreError("The Shell password could not be hashed: openssl did not run.") from None
    hashed = done.stdout.strip()
    if done.returncode != 0 or not CRYPT_HASH.fullmatch(hashed):
        raise CoreError("The Shell password could not be hashed: openssl did not give a SHA-512 crypt hash.")
    return hashed


def host_fingerprint(name):
    """The SHA-256 fingerprint of the sandbox's host key, in the form asyncssh gives it
    (`SHA256:` and the unpadded base64 of the key blob's hash), from the public key file."""
    try:
        blob = base64.b64decode(read_text(f"{paths(name)['host_keys']}/ssh_host_ed25519_key.pub").split()[1], validate=True)
    except (OSError, IndexError, ValueError) as exc:
        raise CoreError(f"The host key of {name} cannot be read ({type(exc).__name__}); starting the project makes it again.") from exc
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def write_provisioning(record):
    """The provisioning folder, written afresh from what the core holds and the project's
    record. Files are replaced in place, never the folder: a running sandbox has it mounted."""
    name = record["name"]
    p = paths(name)
    tutor_key, host_key = f"{p['tutor_keys']}/id_ed25519", f"{p['host_keys']}/ssh_host_ed25519_key"
    keygen(tutor_key, f"cl-tutor-{name}")
    keygen(host_key, f"cl-sb-{name}")
    host_public = read_text(host_key + ".pub")
    password = shell_password(name, make=True)
    write_file(f"{p['tutor_keys']}/known_hosts", f"{p['container']} {' '.join(host_public.split()[:2])}\n", 0o644)
    os.makedirs(p["provision"], mode=0o755, exist_ok=True)
    wanted = {
        "authorized_keys": (read_text(tutor_key + ".pub"), 0o644),
        "sshd_config": (SSHD_SETTINGS, 0o644),
        "ssh_host_ed25519_key": (read_text(host_key), 0o600),
        "ssh_host_ed25519_key.pub": (host_public, 0o644),
        "shell_password_hash": (crypt_hash(password) + "\n", 0o600),
        # What the sandbox's init deploys at start (the agents, and the editor if true), and the model
        # and effort the deploy script writes into Claude Code's settings.
        SOFTWARE_FILE: (json.dumps({"agents": record["agents"], "editor": record["editor"],
                                    "claude": {"model": record["claude_model"], "effort": record["claude_effort"]}}) + "\n", 0o644),
    }
    for filename, (text, mode) in wanted.items():
        write_file(os.path.join(p["provision"], filename), text, mode)
    for extra in set(os.listdir(p["provision"])) - set(wanted):
        target = os.path.join(p["provision"], extra)
        shutil.rmtree(target) if os.path.isdir(target) and not os.path.islink(target) else os.remove(target)


def ensure(record, say=say_nothing):
    """Make whatever of the project is missing. What is there is left as it is, but for the
    provisioning folder and known_hosts, which are written afresh every time."""
    name = record["name"]
    p = paths(name)
    for key in ("workspace", "home"):
        os.makedirs(p[key], mode=0o755, exist_ok=True)
        if os.stat(p[key]).st_uid != SANDBOX_UID:  # new, or made by a call that was killed before this line
            os.chown(p[key], SANDBOX_UID, SANDBOX_UID)
    write_provisioning(record)
    # The source of the distributions' mount, made here with the modes a sandbox user can
    # read, and not left to the daemon, which makes a missing source folder its own way.
    os.makedirs(f"{DATA}/dist/agents", mode=0o755, exist_ok=True)
    if not there_is("network", p["network"]):
        docker("network", "create", "--label", f"cl.project={name}", p["network"])
    # The network first, then the web server and the tutor service onto it: web_up and
    # tutor_up, the other way round, make their container first and then list the networks, so
    # one of the two always connects them.
    connect_web(p["network"], say)
    connect_web(p["network"], say, TUTOR_CONTAINER, "the tutor service")
    if not there_is("container", p["container"]):
        docker("create", *container_args(record, ensure_image(say)))


def apply_limits(record):
    """Put the record's limits on the existing container. Returns what could not be applied."""
    p = paths(record["name"])
    notes = []
    container = inspect_container(p["container"])
    if container is None:
        return notes
    if record["limits"]["memory"] is None and container["HostConfig"].get("Memory"):
        notes.append("The memory limit is removed when the sandbox is next recreated; Docker cannot remove it from an existing container.")
    done = docker("update", *limit_args(record["limits"], for_update=True), p["container"], check=False)
    if done.returncode != 0:
        notes.append("The limits could not be applied: " + done.stderr.strip()[-300:])
    elif record["limits"]["cpus"] is None and container["State"].get("Running"):
        # runc lifts a processor limit at once; crun (the sysbox mode's runtime) only records
        # it, and the running sandbox keeps the old one. Read what is in force, from this
        # container's own view of the sandbox's control group: nothing is run in the sandbox.
        in_force = cpu_limit_in_force(container["Id"])
        if in_force is not None and not in_force.startswith("max"):
            notes.append("The processor limit is removed when the sandbox next starts; its runtime does not lift it from a running container.")
    return notes


def cpu_limit_in_force(container_id, root="/sys/fs/cgroup"):
    """The sandbox's cpu.max as the kernel has it, or None where it cannot be read (the group
    is named by the daemon's cgroup driver)."""
    for group in (f"docker/{container_id}", f"system.slice/docker-{container_id}.scope"):
        with contextlib.suppress(OSError):
            return read_text(f"{root}/{group}/cpu.max")
    return None


# --- the operations ------------------------------------------------------------------------

def leftovers(name):
    """What exists under a project's name, the record aside."""
    p = paths(name)
    found = [key for key in (*FOLDERS, "ports") if os.path.lexists(p[key])]
    found += [kind for kind, key in (("container", "container"), ("network", "network")) if there_is(kind, p[key])]
    return found


def setup_state(container):
    """How the setup recipe went in this container, as its init wrote it: none, running, done,
    failed (exit N); "" when it has not said, or said something else. The file is copied out
    by the daemon (nothing is run in the sandbox) and at most a few kilobytes of it are read:
    it is the sandbox's to write."""
    try:
        archive = first_bytes(["docker", "cp", f"{container}:{SETUP_STATE}", "-"], 8192, 20)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:  # a plain archive: no decompressor sees these bytes
            member = tar.next()
        if member is None:
            return ""
        # A link or a folder has no content, and a long file fails the match below.
        text = archive[member.offset_data:member.offset_data + member.size].decode(errors="replace").strip()
    except (OSError, EOFError, tarfile.TarError):
        return ""
    return text if SETUP_STATES.fullmatch(text) else ""


def first_bytes(command, limit, timeout):
    """At most `limit` bytes of a command's output, read for at most `timeout` seconds; the
    command is ended whatever it still had to say."""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL) as process:
        try:
            got, deadline = b"", time.monotonic() + timeout
            while len(got) < limit and select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                chunk = os.read(process.stdout.fileno(), limit - len(got))
                if not chunk:
                    break
                got += chunk
            return got
        finally:
            process.kill()


def status(name):
    record = read_record(name)
    p = paths(name)
    container = inspect_container(p["container"])
    out = {"name": name, "created": record["created"], "wanted": record["wanted"], "limits": record["limits"],
           "agents": record["agents"], "editor": record["editor"], "claude_model": record["claude_model"], "claude_effort": record["claude_effort"],
           "state": "missing", "address": "", "image": "", "container": "", "setup": ""}
    if container:
        out["state"] = "running" if container["State"].get("Running") else "stopped"
        out["address"] = container["NetworkSettings"]["Networks"].get(p["network"], {}).get("IPAddress", "")
        out["image"] = container["Config"].get("Image", "")
        out["container"] = container["Id"][:12]
        out["setup"] = setup_state(p["container"])
    return out


def list_projects():
    out = []
    for name in recorded_names():
        try:
            record = read_record(name)
        except CoreError as exc:
            out.append({"name": name, "state": "unreadable", "problem": str(exc)})
            continue
        try:
            out.append(status(name))
        except CoreError as exc:  # the record is fine; the daemon could not say how the sandbox is
            out.append({"name": name, "created": record["created"], "wanted": record["wanted"], "limits": record["limits"],
                        "agents": record["agents"], "editor": record["editor"], "claude_model": record["claude_model"],
                        "claude_effort": record["claude_effort"], "state": "unknown", "address": "", "image": "", "container": "", "setup": "", "problem": str(exc)})
    return out


def running_sandbox(name):
    """What the daemon says of a project's sandbox, which must be running."""
    not_being_destroyed(read_record(name))
    container = inspect_container(paths(name)["container"])
    if container is None or not container["State"].get("Running"):
        raise CoreError(f"The sandbox of {name} is not running; start the project first.")
    return container


def attach(name):
    """Where and how the web server's terminal logs in to a running sandbox: its address on
    the project's network, sshd's port, the sandbox user, the Shell password, and the host
    key's fingerprint to hold the connection to. The password is in the result only."""
    p = paths(name)
    container = running_sandbox(name)
    host = (container["NetworkSettings"]["Networks"].get(p["network"]) or {}).get("IPAddress", "")
    if not host:
        raise CoreError(f"The sandbox of {name} has no address on {p['network']}; stop and start the project.")
    password = shell_password(name)
    if not password:
        raise CoreError(f"Project {name} has no Shell password yet; stop and start the project to make one.")
    return {"name": name, "host": host, "port": SSH_PORT, "username": SANDBOX_USER, "password": password,
            "fingerprint": host_fingerprint(name)}


def listening_sockets(text, v6):
    """(address, port, inode) of each listening socket in a /proc/net/tcp or tcp6 table. The
    kernel prints each 32-bit word of the address in the host's order (little-endian here).
    A line that does not parse is passed over: the sandbox's sockets are its own to make."""
    found, width = [], 32 if v6 else 8
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":   # 0A: LISTEN
            continue
        address, _, port = fields[1].partition(":")
        try:
            raw = bytes.fromhex(address)
            port, inode = int(port, 16), int(fields[9])
        except ValueError:
            continue
        if len(address) != width or not 1 <= port <= 65535:
            continue
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
        found.append((ipaddress.IPv6Address(raw) if v6 else ipaddress.IPv4Address(raw), port, inode))
    return found


def detected_ports(tcp_text, tcp6_text):
    """The rows of `port_detect`, from the two tables: one per port something listens on other
    than on loopback, the sandbox's own services (sshd, the files server, and code-server behind
    the editor's stub, should it ever listen elsewhere than on loopback) left out."""
    inodes = {}
    for text, v6 in ((tcp_text, False), (tcp6_text, True)):
        for address, port, inode in listening_sockets(text, v6):
            mapped = getattr(address, "ipv4_mapped", None) or address
            if mapped.is_loopback or port in (SSH_PORT, FILES_SERVER_PORT, EDITOR_UPSTREAM_PORT):
                continue
            inodes.setdefault(port, set()).add(inode)
    return [{"port": port, "inodes": sorted(inodes[port]), "label": str(port)} for port in sorted(inodes)]


def read_bounded(path, limit=PROC_NET_BYTES):
    """At most `limit` bytes of a file, without a last line the limit cut."""
    with open(path, errors="replace") as fh:
        text = fh.read(limit)
    return text if len(text) < limit else text[:text.rfind("\n") + 1]


def port_detect(name):
    """What listens in a running sandbox, read from this container's side: the socket tables
    of the network namespace of the sandbox's first process. Nothing is run in the sandbox."""
    pid = running_sandbox(name)["State"].get("Pid") or 0
    try:
        tcp = read_bounded(f"{PROC}/{pid}/net/tcp") if pid > 0 else None
        tcp6 = ""
        with contextlib.suppress(FileNotFoundError):  # a kernel without IPv6
            tcp6 = read_bounded(f"{PROC}/{pid}/net/tcp6")
    except OSError:
        tcp = None
    if tcp is None:
        raise CoreError(f"The sandbox of {name} is not running; start the project first.")
    return {"project": name, "running": True, "ports": detected_ports(tcp, tcp6)}


# --- ports named by hand -------------------------------------------------------------------
#
# A project's registry holds the ports of its sandbox that were named by hand, of two kinds,
# in RS's entry shape: a web app the detection does not see, {port, label}, to which the web
# server gives an origin port; and raw TCP, {port, label, passthrough: true, host_port}, which
# the web server relays from host_port, a port of CL_PASSTHROUGH_PORTS that no other entry
# of any project holds.

def port_number(value, called):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise CoreError(f"{called} must be a port number from 1 to 65535.")
    return value


def entry_is_sound(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("label"), str):
        return False
    try:
        port_number(entry.get("port"), "port")
        if entry.get("passthrough") is True:
            return set(entry) == {"port", "label", "passthrough", "host_port"} and bool(port_number(entry["host_port"], "host_port"))
    except CoreError:
        return False
    return set(entry) == {"port", "label"}


def read_ports(name):
    """A project's registry: its entries, [] when it has none."""
    path = paths(name)["ports"]
    try:
        with open(path) as fh:
            entries = json.load(fh).get("ports")
    except FileNotFoundError:
        return []
    except (OSError, ValueError, AttributeError) as exc:
        raise CoreError(f"The port registry of {name} cannot be read ({type(exc).__name__}).") from exc
    if not isinstance(entries, list) or not all(entry_is_sound(entry) for entry in entries):
        raise CoreError(f"The port registry of {name} cannot be read (an entry is not one).")
    return entries


def claimed_host_ports():
    """{host port: project} over every project's registry. A registry that cannot be read is
    an error: a port it may hold must not be given out again."""
    try:
        files = os.listdir(f"{DATA}/core/ports")
    except FileNotFoundError:
        return {}
    claimed = {}
    for filename in sorted(files):
        name = filename.removesuffix(".json")
        if filename.endswith(".json") and NAME.fullmatch(name):
            claimed.update({entry["host_port"]: name for entry in read_ports(name) if entry.get("passthrough")})
    return claimed


def passthrough_range(env=None):
    env = os.environ if env is None else env
    return range_setting(env.get("CL_PASSTHROUGH_PORTS", WEB_DEFAULTS["CL_PASSTHROUGH_PORTS"]), "CL_PASSTHROUGH_PORTS")


def port_add(name, port, label, passthrough=False, host_port=None, env=None):
    """Name a port of the sandbox by hand: a web app (passthrough false) or raw TCP on a host
    port of the pass-through range, the lowest free one unless one is asked for. The sandbox
    need not run. The result is RS's: the entry's key (host_port for raw TCP, else port), its
    label, and the project's entries after the change."""
    valid_name(name)
    port = port_number(port, "port")
    own = {SSH_PORT: "sshd", FILES_SERVER_PORT: "the files server", EDITOR_PORT: "the editor", EDITOR_UPSTREAM_PORT: "the editor"}
    if port in own:
        raise CoreError(f"Port {port} is the sandbox's own ({own[port]}).")
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > LABEL_CHARS or not label.strip().isprintable():
        raise CoreError(f"label must be a name of 1 to {LABEL_CHARS} printable characters.")
    label = label.strip()
    if not isinstance(passthrough, bool):
        raise CoreError("passthrough must be true or false.")
    if host_port is not None and not passthrough:
        raise CoreError("A host port is given only for raw TCP (passthrough).")
    with locked(name), flocked("ports.lock"):   # the second: a host port is claimed across every project
        not_being_destroyed(read_record(name))
        entries = read_ports(name)
        if any(entry["port"] == port and entry.get("passthrough", False) == passthrough for entry in entries):
            raise CoreError(f"Port {port} of {name} is named already{' for raw TCP' if passthrough else ''}.")
        entry = {"port": port, "label": label}
        if passthrough:
            low, high = passthrough_range(env)
            claimed = claimed_host_ports()
            if host_port is None:
                host_port = next((candidate for candidate in range(low, high + 1) if candidate not in claimed), None)
                if host_port is None:
                    raise CoreError(f"Every port of the pass-through range {low}-{high} is taken.")
            else:
                host_port = port_number(host_port, "host_port")
                if not low <= host_port <= high:
                    raise CoreError(f"Host port {host_port} is not in the pass-through range {low}-{high}.")
                if host_port in claimed:
                    raise CoreError(f"Host port {host_port} is taken by project {claimed[host_port]}.")
            entry.update(passthrough=True, host_port=host_port)
        entries = sorted([*entries, entry], key=lambda one: (one["port"], one.get("host_port", 0)))
        write_json(paths(name)["ports"], {"ports": entries})
    return {"project": name, "port": host_port if passthrough else port, "label": label, "ports": entries}


def port_remove(name, port, passthrough=False):
    """Take an entry out: for raw TCP `port` is its host port, else the sandbox's port. An
    entry that is not there is gone already."""
    valid_name(name)
    port = port_number(port, "port")
    if not isinstance(passthrough, bool):
        raise CoreError("passthrough must be true or false.")

    def matches(entry):
        return entry.get("host_port") == port if passthrough else entry["port"] == port and not entry.get("passthrough")
    with locked(name), flocked("ports.lock"):
        not_being_destroyed(read_record(name))
        entries = read_ports(name)
        kept = [entry for entry in entries if not matches(entry)]
        if kept != entries:
            write_json(paths(name)["ports"], {"ports": kept})
    return {"project": name, "port": port, "ports": kept}


def port_list(name):
    read_record(name)
    return {"project": name, "ports": read_ports(name)}


def create(name, limits=None, say=say_nothing, agents=(), editor=False, claude_model="", claude_effort=""):
    """A new project with the agents and the editor asked for (none by default), each of which
    must be pulled, and Claude's model and effort (Claude's own default when empty): what is
    asked for is checked before anything is made, as RS does."""
    valid_name(name)
    agents, editor = checked_agents(agents), checked_editor(editor)
    checked_claude_pair(claude_model, claude_effort)
    refuse_unpulled(agents, editor)
    with locked(name):
        if os.path.exists(paths(name)["record"]):
            raise CoreError(f"A project named {name} exists. (If it is being destroyed, `destroy {name}` finishes that.)")
        left = leftovers(name)
        if left:
            raise CoreError(f"Something of an earlier project named {name} is left ({', '.join(left)}). `destroy {name}` clears it.")
        record = new_record(name, limits, agents, editor, claude_model, claude_effort)
        try:
            write_record(record)
            ensure(record, say)
            docker("start", paths(name)["container"])
        except Exception:
            mark_destroying(name)  # a clean-up that is cut short is finished by destroy, not by the next start
            try:
                remove_everything(name)
            except CoreError as exc:
                say(f"cleaning up after the failed creation: {exc}")
            raise
        return status(name)


def not_being_destroyed(record):
    if record["wanted"] == "destroying":
        raise CoreError(f"Project {record['name']} is being destroyed. `destroy {record['name']}` finishes it.")
    return record


def start(name, say=say_nothing):
    with locked(name):
        record = not_being_destroyed(read_record(name))
        ensure(record, say)
        notes = apply_limits(record)
        docker("start", paths(name)["container"])
        record["wanted"] = "running"
        write_record(record)
        return dict(status(name), notes=notes)


def stop(name):
    with locked(name):
        record = not_being_destroyed(read_record(name))
        container = paths(name)["container"]
        if there_is("container", container):
            docker("stop", "-t", str(STOP_GRACE_S), container, timeout=STOP_GRACE_S + 120)
        record["wanted"] = "stopped"
        write_record(record)
        return status(name)


def recreate(name, say=say_nothing):
    """A new container for the project, from the current sandbox image, with the same
    workspace, home folder and network. Its own filesystem is new, so its init runs the setup
    recipe again, which is how what was installed comes back. The image is made sure of before
    anything is touched: a recreate that cannot get its image removes nothing."""
    with locked(name):
        record = not_being_destroyed(read_record(name))
        ensure_image(say)
        container = paths(name)["container"]
        if there_is("container", container):
            docker("stop", "-t", str(STOP_GRACE_S), container, timeout=STOP_GRACE_S + 120, check=False)
            docker("rm", "-f", "-v", container, timeout=STOP_GRACE_S + 120)
        # From here to the start, a call that is cut short leaves a record without a
        # container, which the next start (or this command again) completes.
        ensure(record, say)
        docker("start", container)
        record["wanted"] = "running"
        write_record(record)
        return status(name)


def set_limits(name, pids=UNCHANGED, memory=UNCHANGED, cpus=UNCHANGED):
    with locked(name):
        record = not_being_destroyed(read_record(name))
        changed = {k: v for k, v in (("pids", pids), ("memory", memory), ("cpus", cpus)) if v is not UNCHANGED}
        record["limits"] = checked_limits(dict(record["limits"], **changed))
        write_record(record)
        return dict(status(name), notes=apply_limits(record))


# --- agents in a running project -----------------------------------------------------------
#
# As RS's Add and Update in a project's Config box: live, nothing restarted. The core runs the
# image's deploy script in the sandbox, as the sandbox user (the home is the learner's, and
# links in it must not be followed with root's rights): the one command the core runs inside a
# sandbox. Its argument list is fixed but for one id of the closed table, checked.

def has_dist_mount(container):
    """Whether a sandbox was created with the distributions mounted (since projects have
    agents). One created before cannot be deployed into until it is recreated."""
    return any(mount.get("Destination") == DIST_MOUNT for mount in container.get("Mounts") or [])


def deployable_sandbox(name):
    container = inspect_container(paths(name)["container"])
    if container is None or not container["State"].get("Running"):
        raise CoreError("Start the project to update its agents")
    if not has_dist_mount(container):
        raise CoreError(f"The sandbox of {name} was made before projects had agents: recreate the project first "
                        "(its workspace and home folder are kept).")
    return container


def tail_of(command, limit, timeout):
    """Run a command: its exit status (None when it was ended for taking too long) and the last
    `limit` bytes of what it printed, standard error included. What a sandbox prints is the
    sandbox's to choose: it is read to the end but never kept whole."""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL) as process:
        tail, deadline = b"", time.monotonic() + timeout
        try:
            while select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                tail = (tail + chunk)[-limit:]
            else:
                return None, tail.decode(errors="replace")
            return process.wait(timeout=30), tail.decode(errors="replace")
        except subprocess.TimeoutExpired:
            return None, tail.decode(errors="replace")
        finally:
            if process.poll() is None:
                process.kill()


def deploy_agents(name, agents):
    """Deploy each of `agents` into the running sandbox of `name`, in the deploy script's update
    mode: the program is overwritten (and its editor extension forced into the project's
    editor, where it has one), settings and logins are left alone. Refused while one of
    them is being built, or is not pulled. Returns {agent: version deployed}."""
    p, deployed = paths(name), {}
    for agent in agents:
        spec = known_software(known_agent(agent))
        if building_now() == agent:
            raise CoreError(f"{spec['label']} is being built now; try again when that is done.")
        refuse_unpulled([agent], False)
        version = pulled(agent)["version"]
        run_deploy(p["container"], "update", agent, f"{spec['label']} could not be deployed into {name}")
        deployed[agent] = version
    return deployed


def run_deploy(container, mode, agent, failed):
    """The image's deploy script in a running sandbox, as the sandbox user, with a time limit:
    the one command the core runs in a sandbox. `mode` and `agent` are words of closed sets,
    never a request's text. Returns the last line it printed; raises `failed` and why."""
    if mode not in ("update", "settings") or agent not in AGENTS:
        raise CoreError(f"There is no deploy {str(mode)[:20]} {str(agent)[:20]}.")
    command = ["docker", "exec", "--user", SANDBOX_USER, container, "timeout", str(DEPLOY_TIMEOUT_S), DEPLOY, mode, agent]
    try:
        code, said = tail_of(command, DEPLOY_OUTPUT_BYTES, DEPLOY_TIMEOUT_S + 60)
    except OSError as exc:
        raise CoreError("The inner Docker daemon did not answer `docker exec`.") from exc
    last = "".join(c if c.isprintable() else "?" for c in (said.strip().splitlines() or [""])[-1])[-300:]
    if code != 0:
        raise CoreError(f"{failed}: " + ("it took too long" if code in (None, 124) else last or f"exit {code}"))
    return last


def agents_add(name, agent="claude"):
    """Add an agent to a running project, live: deployed into its home (over a copy that is
    there, as Update does), then written into its record and its provisioning folder, which
    its init reads at the next start. Adding one the project has is no error."""
    valid_name(name)
    known_agent(agent)
    with locked(name):
        record = not_being_destroyed(read_record(name))
        deployable_sandbox(name)
        deployed = deploy_agents(name, [agent])
        if agent not in record["agents"]:
            record["agents"] = [*record["agents"], agent]
            write_record(record)
            write_provisioning(record)
        return {"project": name, "agents": record["agents"], "deployed": deployed}


def agents_update(name):
    """Update in a project's Config box: deploy the pulled version of every agent the running
    project has; the deploy script also forces each agent's editor extension into the
    project's editor, as RS's Update does. The editor itself is not replaced: a newer one
    reaches a project at its next start."""
    valid_name(name)
    with locked(name):
        record = not_being_destroyed(read_record(name))
        deployable_sandbox(name)
        if not record["agents"]:
            raise CoreError(f"Project {name} has no agents to update.")
        return {"project": name, "agents": record["agents"], "deployed": deploy_agents(name, record["agents"])}


def agents_model(name, model=None, effort=None):
    """Set or clear Claude's model and effort of a running project (RS's Config box, Agents):
    None keeps what the project has, "" clears it (Claude's own default). Written into the
    record and the provisioning folder, then into ~/.claude/settings.json in the sandbox by
    the deploy script, as RS's _write_agent_settings does. "applied" says whether there was a
    settings file to write into: one that is installed later gets the pair then."""
    valid_name(name)
    with locked(name):
        record = not_being_destroyed(read_record(name))
        model = record["claude_model"] if model is None else model
        effort = record["claude_effort"] if effort is None else effort
        model, effort = checked_claude_pair(model, effort)
        deployable_sandbox(name)
        record["claude_model"], record["claude_effort"] = model, effort
        write_record(record)
        write_provisioning(record)
        said = run_deploy(paths(name)["container"], "settings", "claude", f"The model and effort could not be written into the settings of {name}")
        return {"project": name, "claude_model": model, "claude_effort": effort, "applied": not said.endswith("settings absent")}


def remove_everything(name):
    """Remove a project: the container first, and nothing else unless it is gone; the record
    last, and only when everything else is. Run again, it carries on where it stopped."""
    p = paths(name)
    docker("rm", "-f", "-v", p["container"], check=False, timeout=STOP_GRACE_S + 120)
    if there_is("container", p["container"]):
        raise CoreError(f"The sandbox of {name} could not be removed, so nothing else was.")
    problems = []
    if there_is("network", p["network"]):
        # What else sits on the project's network (the services that join every project's) is
        # taken off it, not removed.
        for _attempt in (1, 2):  # the web server may be connected again between the two steps: once more, then
            attached = docker("network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", p["network"], check=False)
            for other in attached.stdout.split():
                docker("network", "disconnect", "-f", p["network"], other, check=False)
            done = docker("network", "rm", p["network"], check=False)
            if done.returncode == 0:
                break
        if done.returncode != 0:
            problems.append(f"the network ({done.stderr.strip()[-200:]})")
    for key in (*FOLDERS, "ports"):
        try:
            if os.path.isdir(p[key]) and not os.path.islink(p[key]):
                shutil.rmtree(p[key])  # does not follow a link found inside
            elif os.path.lexists(p[key]):
                os.remove(p[key])
        except OSError as exc:
            problems.append(f"{p[key]} ({exc})")
    with contextlib.suppress(OSError):
        os.remove(p["ports"] + ".tmp")
    if problems:
        raise CoreError(f"Not everything of {name} could be removed: " + "; ".join(problems) + f". Run `destroy {name}` again.")
    for record in (p["record"] + ".tmp", p["record"]):
        with contextlib.suppress(FileNotFoundError):
            os.remove(record)


def destroy(name):
    valid_name(name)
    with locked(name):
        if os.path.exists(paths(name)["record"]):
            mark_destroying(name)  # before anything is removed
        elif not leftovers(name):
            raise CoreError(f"There is no project named {name}.")
        remove_everything(name)
        return {"name": name, "state": "destroyed"}


def start_wanted(say=say_nothing):
    """Start every project that should be running. One that fails does not stop the others."""
    started, failed = [], {}
    for name in recorded_names():
        try:
            if emptied(name) or read_record(name)["wanted"] != "running":
                continue
            start(name, say)
            started.append(name)
        except Exception as exc:  # noqa: BLE001 - whatever went wrong with one project, the others are still started
            failed[name] = str(exc) or type(exc).__name__
            say(f"{name} could not be started: {failed[name]}")
    return {"started": started, "failed": failed}


# --- the web server's container ------------------------------------------------------------

def port_setting(text, called):
    try:
        port = int(str(text).strip())
    except ValueError:
        port = 0
    if not 1024 <= port <= 65535 or str(port) != str(text).strip():
        raise CoreError(f"{called} is {text!r}: it must be a port number from 1024 to 65535.")
    return port


def range_setting(text, called):
    low, dash, high = str(text).strip().partition("-")
    if not dash:
        raise CoreError(f"{called} is {text!r}: it must be a range of ports, as 11100-11164.")
    low, high = port_setting(low, called), port_setting(high, called)
    if low > high:
        raise CoreError(f"{called} is {text!r}: its first port is above its last.")
    return low, high


def web_wanted(env):
    """Whether clanker-learn runs its web server (CL_WEB: on, the default, or off)."""
    said = env.get("CL_WEB", "on").strip().lower()
    if said not in ("on", "off"):
        raise CoreError(f"CL_WEB is {env.get('CL_WEB')!r}: it must be on or off.")
    return said == "on"


def web_settings(env, status_port):
    """The page's port and the two ranges, as the run command names them, checked: each a
    port or a range above 1023, none overlapping another or the status page's port."""
    said = {key: env.get(key, default) for key, default in WEB_DEFAULTS.items()}
    out = {"port": port_setting(said["CL_WEB_PORT"], "CL_WEB_PORT"),
           "origin": range_setting(said["CL_ORIGIN_PORTS"], "CL_ORIGIN_PORTS"),
           "passthrough": range_setting(said["CL_PASSTHROUGH_PORTS"], "CL_PASSTHROUGH_PORTS")}
    spans = [("the status page's port", (status_port, status_port)), ("CL_WEB_PORT", (out["port"], out["port"])),
             ("CL_ORIGIN_PORTS", out["origin"]), ("CL_PASSTHROUGH_PORTS", out["passthrough"])]
    for i, (first, (a_low, a_high)) in enumerate(spans):
        for second, (b_low, b_high) in spans[i + 1:]:
            if a_low <= b_high and b_low <= a_high:
                raise CoreError(f"{first} and {second} overlap ({a_low}-{a_high} and {b_low}-{b_high}).")
    return out


def web_args(settings, image):
    """What `docker create` is given for the web server's container: no capability and no
    way to gain one, the front network pinned as the one its published ports live on (the
    page's, and the two ranges: web apps' origin ports and raw TCP's pass-through ports), the
    broker's socket folder read-only, its own folder of the data volume, the secret it shares
    with the tutor service read-only, and nothing of a project. The label is a hash of all of it: a container made from another image or with
    other settings is told by it and made again."""
    port = settings["port"]
    origin, passthrough = ("{}-{}".format(*settings[key]) for key in ("origin", "passthrough"))
    args = [
        "--name", WEB_CONTAINER, "--hostname", WEB_CONTAINER, "--network", f"name={FRONT_NET},gw-priority=1",
        "--label", "cl.role=web",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(WEB_PIDS),
        "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "-p", f"{port}:{port}", "-p", f"{origin}:{origin}", "-p", f"{passthrough}:{passthrough}",
        "--mount", f"type=bind,source={BROKER_RUN},target=/run/cl-broker,readonly",
        "--mount", f"type=bind,source={DATA}/web,target=/data",
        "--mount", f"type=bind,source={tutor_secret_path()},target={TUTOR_SECRET_MOUNT},readonly",
        "-e", f"CL_WEB_PORT={port}", "-e", f"CL_LISTEN_SUBNET={FRONT_SUBNET}",
        "-e", f"CL_ORIGIN_PORTS={origin}", "-e", f"CL_PASSTHROUGH_PORTS={passthrough}",
        "-e", f"CL_TUTOR_URL=http://{TUTOR_CONTAINER}:{TUTOR_PORT}",
    ]
    made_from = hashlib.sha256(json.dumps([args, image]).encode()).hexdigest()[:16]
    return [*args, "--label", f"cl.made-from={made_from}", image]


def overlapping_route(subnet, routes_text, own_device=""):
    """The first route of this container that overlaps `subnet`, or "". A network made with a
    subnet of our choosing gets no such check from the daemon, and replies to the browser
    would go into it. The routes of `own_device` (the front network's own bridge, once it
    exists) are left out: they are that network."""
    wanted = ipaddress.ip_network(subnet)
    for line in routes_text.splitlines():
        words = line.split()  # "default" is no network: ip_network refuses it below
        if not words:
            continue
        if own_device and "dev" in words[:-1] and words[words.index("dev") + 1] == own_device:
            continue
        with contextlib.suppress(ValueError):
            if ipaddress.ip_network(words[0], strict=False).overlaps(wanted):
                return line.strip()
    return ""


def read_routes():
    """This container's IPv4 routes, as `ip` lists them. Routes that cannot be read are an
    error: an overlap that was not seen would send the browser's replies astray."""
    try:
        listed = subprocess.run(["ip", "-4", "route", "show"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoreError(f"This container's routes could not be read ({exc}), so the web server's network was not checked.") from exc
    if listed.returncode != 0:
        raise CoreError(f"This container's routes could not be read ({listed.stderr.strip()[-200:]}), so the web server's network was not checked.")
    return listed.stdout


def connect_web(network, say=say_nothing, container=WEB_CONTAINER, called="the web server"):
    """Put the web server (or the tutor service) on a project's network, if it is there. Not
    being able to is a line in the log, never a failed start of a project."""
    try:
        if not there_is("container", container):
            return
        done = docker("network", "connect", network, container, check=False)
        if done.returncode != 0 and "already exists" not in done.stderr:
            say(f"{called} could not be connected to {network}: {done.stderr.strip()[-200:]}")
    except CoreError as exc:
        say(f"{called} could not be connected to {network}: {exc}")


def web_up(settings, say=say_nothing):
    """The web server's container, made if it is missing or was made from another image or
    other settings, connected to every project's network, and started."""
    image = ensure_image(say, "web")
    # Checked at every start, not only when the network is made: a route added since (a VPN
    # of the machine's, another network) would take the replies of the browser.
    front = inspected("network", FRONT_NET)
    own_device = f"br-{front['Id'][:12]}" if front is not None else ""
    clash = overlapping_route(FRONT_SUBNET, read_routes(), own_device)
    if clash:
        raise CoreError(f"The web server's network {FRONT_SUBNET} overlaps a network this container is on ({clash}).")
    if front is None:
        docker("network", "create", "--subnet", FRONT_SUBNET, "--label", "cl.role=front", FRONT_NET)
    os.makedirs(f"{DATA}/web", mode=0o700, exist_ok=True)
    # The backups' folder, there from the first start: an archive is copied into it for a
    # restore onto a new installation (write_backup makes it too, with the same mode).
    os.makedirs(f"{DATA}/backups", mode=0o700, exist_ok=True)
    tutor_secret()   # mounted into the container: there before it is made
    args = web_args(settings, image)
    existing = inspect_container(WEB_CONTAINER)
    if existing is not None and existing["Config"]["Labels"].get("cl.made-from") != args[-2].partition("=")[2]:
        say("the web server's container was made from another image or other settings; making it again")
        docker("rm", "-f", "-v", WEB_CONTAINER, timeout=STOP_GRACE_S + 120)
        existing = None
    if existing is None:
        docker("create", *args)
    for name in recorded_names():  # after the container exists: see ensure()
        network = paths(name)["network"]
        if there_is("network", network):
            connect_web(network, say)
    docker("start", WEB_CONTAINER)


def web_health(settings, timeout=3):
    """How the web server stands, as (kind, text): "up"; "absent" (no container); "ended"
    (its container is not running; the text holds the last line of its log); "silent" (it
    runs and does not answer on its front address); "unknown" (the daemon could not be
    asked). Asked from this container, without running anything in the web server's."""
    try:
        container = inspect_container(WEB_CONTAINER)
        if container is None:
            return "absent", "there is no web server container"
        if not container["State"].get("Running"):
            last = docker("logs", "--tail", "1", WEB_CONTAINER, check=False)
            return "ended", ("the web server ended: " + (last.stderr.strip() or last.stdout.strip())[-300:]).rstrip(": ")
    except CoreError as exc:
        return "unknown", str(exc)
    address = (container["NetworkSettings"]["Networks"].get(FRONT_NET) or {}).get("IPAddress", "")
    if not address:
        return "silent", f"the web server does not answer yet (it has no address on {FRONT_NET})"
    try:
        # The certificate is the web server's own, self-signed, and names no address of the
        # front network: this asks whether it answers, not who it is.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(f"https://{address}:{settings['port']}/", timeout=timeout, context=context) as answer:
            if answer.status == 200:
                return "up", ""
            return "silent", f"the web server does not answer yet (it answered {answer.status})"
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return "silent", f"the web server does not answer yet ({getattr(exc, 'reason', exc)})"


# --- the tutor service's container ----------------------------------------------------------

def tutor_secret_path():
    return f"{DATA}/core/tutor_secret"


def tutor_secret():
    """The secret the web server and the tutor service share: 64 hex characters, made once,
    mode 0600, mounted read-only into both containers. Returns its path."""
    path = tutor_secret_path()
    with contextlib.suppress(FileNotFoundError):
        if re.fullmatch(r"[0-9a-f]{64}", read_text(path).strip()):
            return path
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    write_file(path, secrets.token_hex(32), 0o600)
    return path


def tutor_args(image):
    """What `docker create` is given for the tutor service's container: no capability and no
    way to gain one, the front network without any published port (the web server reaches it
    there; it listens on that address only), its folder of the data volume (its database, the
    Claude token, the projects' keys for the tutor), the shared secret read-only. It is put on
    every project's network to reach the sandboxes' sshd by name. Labelled with a hash of all
    of it, as the web server's container is."""
    args = [
        "--name", TUTOR_CONTAINER, "--hostname", TUTOR_CONTAINER, "--network", f"name={FRONT_NET},gw-priority=1",
        "--label", "cl.role=tutor",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(TUTOR_PIDS),
        "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--mount", f"type=bind,source={DATA}/tutor,target=/data/tutor",
        "--mount", f"type=bind,source={tutor_secret_path()},target={TUTOR_SECRET_MOUNT},readonly",
        "-e", f"CL_LISTEN_SUBNET={FRONT_SUBNET}", "-e", f"CL_TUTOR_PORT={TUTOR_PORT}",
    ]
    made_from = hashlib.sha256(json.dumps([args, image]).encode()).hexdigest()[:16]
    return [*args, "--label", f"cl.made-from={made_from}", image]


def tutor_up(say=say_nothing):
    """The tutor service's container, made if it is missing or was made from another image or
    other settings, connected to every project's network, and started. The front network is
    the web server's, made by web_up before this runs."""
    image = ensure_image(say, "tutor")
    if not there_is("network", FRONT_NET):
        raise CoreError(f"The front network {FRONT_NET} is not there yet; the web server makes it.")
    os.makedirs(f"{DATA}/tutor", mode=0o700, exist_ok=True)
    tutor_secret()
    args = tutor_args(image)
    existing = inspect_container(TUTOR_CONTAINER)
    if existing is not None and existing["Config"]["Labels"].get("cl.made-from") != args[-2].partition("=")[2]:
        say("the tutor service's container was made from another image or other settings; making it again")
        docker("rm", "-f", "-v", TUTOR_CONTAINER, timeout=STOP_GRACE_S + 120)
        existing = None
    if existing is None:
        docker("create", *args)
    for name in recorded_names():
        network = paths(name)["network"]
        if there_is("network", network):
            connect_web(network, say, TUTOR_CONTAINER, "the tutor service")
    docker("start", TUTOR_CONTAINER)


def tutor_health(timeout=3):
    """How the tutor service stands, as web_health says it of the web server: "up", "absent",
    "ended", "silent" or "unknown", with a text. Asked at GET /health on its front address."""
    try:
        container = inspect_container(TUTOR_CONTAINER)
        if container is None:
            return "absent", "there is no tutor service container"
        if not container["State"].get("Running"):
            last = docker("logs", "--tail", "1", TUTOR_CONTAINER, check=False)
            return "ended", ("the tutor service ended: " + (last.stderr.strip() or last.stdout.strip())[-300:]).rstrip(": ")
    except CoreError as exc:
        return "unknown", str(exc)
    address = (container["NetworkSettings"]["Networks"].get(FRONT_NET) or {}).get("IPAddress", "")
    if not address:
        return "silent", f"the tutor service does not answer yet (it has no address on {FRONT_NET})"
    try:
        with urllib.request.urlopen(f"http://{address}:{TUTOR_PORT}/health", timeout=timeout) as answer:
            if answer.status == 200:
                return "up", ""
            return "silent", f"the tutor service does not answer yet (it answered {answer.status})"
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return "silent", f"the tutor service does not answer yet ({getattr(exc, 'reason', exc)})"


def keep_tutor(kept, report, now=time.monotonic):
    """One turn of keeping the tutor service, as keep_web keeps the web server, saying how it
    stands through `report("tutor", ...)`: "preparing", "up", "down: <why>". `kept` holds what
    it knows between turns. An image that cannot be had is down for this run; anything else
    that fails is tried again at the next turn. The first turn builds its image, once."""
    def tell(state):
        if state != kept.get("said"):
            report("tutor", state)
            kept["said"] = state

    def progress(message):
        report("tutor", f"preparing: {message}")
        kept["said"] = f"preparing: {message}"
    if kept.get("gave_up"):
        return
    first = "said" not in kept
    if first:
        tell("preparing")
    kind, text = tutor_health()
    if kind != "silent":
        kept["silent_since"] = None
    if kind == "up":
        tell("up")
    elif kind == "silent":
        kept["silent_since"] = now() if kept.get("silent_since") is None else kept["silent_since"]
        if now() - kept["silent_since"] > WEB_SILENT_S:
            tell(f"down: {text}")
    else:
        if not (first and kind in ("absent", "ended")) and not kept.get("up_failed"):
            tell(f"down: {text}")
        try:
            tutor_up(progress)
            kept["up_failed"] = False
            # Asked again for a few seconds: the keeper's next turn can be minutes away (it
            # pulls the agent and the editor first), and "preparing" would stand until then.
            for _ in range(TUTOR_ANSWER_S):
                time.sleep(1)
                if tutor_health()[0] == "up":
                    tell("up")
                    break
        except ImageError as exc:
            tell(f"down: {exc}")
            kept["gave_up"] = True
        except (CoreError, OSError) as exc:
            tell(f"down: {exc}")
            kept["up_failed"] = True


def keep_web(env, status_port, report, sleep=time.sleep, turns=None, now=time.monotonic):
    """Bring the web server up and keep it up, saying how it stands through `report(key,
    value)`: "preparing", "up", "off", or "down: <why>". Once it is up, the tutor service is
    brought up and kept up the same way ("tutor", from then on at every turn: keep_tutor), the
    sandbox image is had ("sandbox_image"), then the agent's and the editor's distributions ("agents", "editor":
    "pulling", then "ready: <version>" or "failed: <why>"). Returns 0 when it is off, 3 when it
    is down for good this run (a wrong setting, an image that cannot be had: the next start of
    clanker-learn tries again). A web server that is missing, ended, or cannot be asked about is
    started again, a turn apart; one that runs and does not answer is left alone, and called
    down after WEB_SILENT_S. `turns` bounds the loop, for tests."""
    try:
        wanted = web_wanted(env)
    except CoreError as exc:
        report("web", f"down: {exc}")
        return 3
    if not wanted:
        try:
            for container in (WEB_CONTAINER, TUTOR_CONTAINER):   # left by an earlier start with CL_WEB=on
                if there_is("container", container):
                    docker("rm", "-f", "-v", container, timeout=STOP_GRACE_S + 120)
        except CoreError as exc:
            report("web", f"down: {exc}")
            return 3
        report("web", "off")
        return 0
    try:
        settings = web_settings(env, status_port)
    except CoreError as exc:
        report("web", f"down: {exc}")
        return 3
    said = [None]

    def tell(state):
        if state != said[0]:
            report("web", state)
            said[0] = state

    def progress(message):
        # Said as it comes, and remembered as said, so that the state after it is said again.
        report("web", f"preparing: {message}")
        said[0] = f"preparing: {message}"
    tell("preparing")
    first, silent_since, sandbox_image_tried, up_failed = True, None, False, False
    tutor_kept = None   # what keep_tutor knows, once the web server has been up
    while turns is None or turns > 0:
        turns = None if turns is None else turns - 1
        kind, text = web_health(settings)
        if kind != "silent":
            silent_since = None
        if kind == "up":
            tell("up")
        elif kind == "silent":
            # Starting, or stuck: no restart either way, but not "preparing" for ever.
            silent_since = now() if silent_since is None else silent_since
            if now() - silent_since > WEB_SILENT_S:
                tell(f"down: {text}")
        else:
            # No container yet, one that ended, or a daemon that did not answer: made, or
            # started again. Only the first two, and only at the first turn, are a start.
            # After a web_up that failed the state holds its reason, which this would hide
            # (and the two would take turns in the log while it lasts).
            if not (first and kind in ("absent", "ended")) and not up_failed:
                tell(f"down: {text}")
            try:
                web_up(settings, progress)
                up_failed = False
            except ImageError as exc:
                tell(f"down: {exc}")
                return 3
            except (CoreError, OSError) as exc:  # OSError: its folder on the data volume, say
                tell(f"down: {exc}")
                up_failed = True
        first = False
        if said[0] == "up" and tutor_kept is None:
            tutor_kept = {}
        if tutor_kept is not None:
            # After the web server, every turn; its image build is the one long wait it adds.
            keep_tutor(tutor_kept, report, now)
        if said[0] == "up" and not sandbox_image_tried:
            # The page is up first; the sandbox's image is its own line, never "web: down".
            sandbox_image_tried = True
            try:
                ensure_image(lambda message: report("sandbox_image", f"preparing: {message}"), "sandbox")
                report("sandbox_image", "ready")
                image_ready = True
            except CoreError as exc:
                report("sandbox_image", f"failed: {exc}")
                image_ready = False
            # Then the agent and the editor, each pulled here once if it is not there (a
            # distribution is built in a container of the sandbox image): never at every start.
            keep_software(report, image_ready)
        sleep(1 if said[0].startswith("preparing") else WEB_RESTART_S)
    return 0


# --- backups -------------------------------------------------------------------------------
#
# A backup is one gzipped tar under /data/backups, written under a temporary name and renamed
# when whole (gaggiclanker's write_backup). Its first member is manifest.json; then what a
# restore needs, at the paths it has under /data. The distributions are not in it (they are
# fetched again), nor is a sandbox's own filesystem (its setup recipe brings that back when its
# container is made again), nor the backups. The tutor's database is copied through SQLite's
# own backup call, so that a running tutor service does not tear it; everything else is copied
# as it stands, running projects included, and the result says so.

BACKUPS = f"{DATA}/backups"
BACKUP_NAME = re.compile(r"cl-backup-[0-9]{8}T[0-9]{6}Z\.tar\.gz")
BACKUP_FORMAT, BACKUP_FORMAT_VERSION = "clanker-learn backup", 1
# What of /data a backup keeps: "broker" is the broker's folder (broker_auth.BROKER_DIR: the
# password's hash, the audit log).
BACKUP_TOPS = ("core", "projects", "homes", "tutor", "web", "broker", "versions.local.env")
BACKUP_LEAVES_OUT = {
    "dist": "the fetched software (Claude Code, the editor): fetched again at the first start, or with Pull in Settings",
    "dist-staging": "what a build of the software left half made",
    "backups": "the backups themselves",
    "the sandboxes' own filesystems": "what was installed outside /workspace and the home folder: each project's setup recipe "
                                      "(/workspace/.learn/setup.sh) brings it back when its container is made again",
}
TUTOR_DB = f"{DATA}/tutor/tutor.db"
WEB_DOWNLOADS = f"{DATA}/web/downloads"   # cl-web's /data/downloads: a backup linked there for one download
DOWNLOAD_LINK = re.compile(r"[0-9a-f]{16}-(cl-backup-[0-9]{8}T[0-9]{6}Z\.tar\.gz)")
DOWNLOAD_LINK_S = 600          # a link the web server did not take within this is cleared
BACKUP_GZIP_LEVEL = 1          # most of a home is programs, which compress little: speed over a few per cent
RESTORE_STAGING = f"{DATA}/.restore-staging"
RESTORE_OLD = f"{DATA}/.restore-old"
BACKUP_HELD = False            # this process was handed the backup lane by a parent that took it


def take_backup_lane():
    """The lane of backups and restores, taken without waiting: an open file that holds it."""
    lane = lane_file("backup.lock")
    try:
        fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lane.close()
        raise CoreError("A backup or a restore is under way; try again when it is done.") from None
    return [lane]


@contextlib.contextmanager
def backup_lane():
    if BACKUP_HELD:
        yield
        return
    held = take_backup_lane()
    try:
        yield
    finally:
        for fh in held:
            fh.close()


def fsync_folder(path):
    folder = os.open(path, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def human_size(size):
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def tutor_db_version_supported():
    """The newest version of the tutor's database the tutor service of this image opens: read
    from its code, which is in this image as the build context of its image (an older file it
    brings up to date itself)."""
    try:
        found = re.search(r"^SCHEMA_VERSION = ([0-9]+)$", read_text(f"{TUTOR_DIR}/store.py"), re.MULTILINE)
    except OSError:
        found = None
    if not found:
        raise CoreError(f"Which version of the tutor's database this clanker-learn knows could not be read from {TUTOR_DIR}/store.py.")
    return int(found.group(1))


def snapshot_tutor_db(dest):
    """A consistent copy of the tutor's database at `dest`, through SQLite's backup call (a
    reader: the tutor service goes on writing), as one file. Its version, or None when there
    is no database yet."""
    import sqlite3
    if not os.path.isfile(TUTOR_DB):
        return None
    source = sqlite3.connect(TUTOR_DB, timeout=30)
    try:
        copy = sqlite3.connect(dest)
        try:
            source.backup(copy)
            copy.execute("PRAGMA journal_mode = DELETE")   # one file, whatever the source's journal is
            version = copy.execute("PRAGMA user_version").fetchone()[0]
        finally:
            copy.close()
    except sqlite3.Error as exc:
        raise CoreError(f"The tutor's database could not be copied ({exc}).") from exc
    finally:
        source.close()
    return version


class Padded:
    """A file read for exactly `size` bytes: what tar's header announced. A file that shrank
    while it was read (a running project writes) is filled with zeros, one that grew is cut;
    either way the archive stays whole and the file is counted as torn."""

    def __init__(self, fh, size):
        self.fh, self.left, self.short = fh, size, False

    def read(self, count=-1):
        count = self.left if count is None or count < 0 else min(count, self.left)
        data = self.fh.read(count)
        if len(data) < count:
            data += b"\0" * (count - len(data))
            self.short = True
        self.left -= count
        return data


def add_tree(tar, arc, tally, only_folder=(), leave_out=()):
    """Add /data/<arc> to the archive, a folder with everything in it, without following a
    link. Something that vanishes while it is walked (a running project) is counted, not an
    error. A folder in `only_folder` goes in without its content; `leave_out` is not added."""
    if arc in leave_out:
        return
    path = os.path.join(DATA, arc)
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISREG(mode):
            # Opened without blocking, so that a file replaced by a pipe meanwhile does not hang
            # the backup; described from the open file, so that the header and the data agree.
            with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as fh:
                info = tar.gettarinfo(arcname=arc, fileobj=fh)
                if info is None:
                    return
                if info.isreg():
                    data = Padded(fh, info.size)
                    tar.addfile(info, data)
                    if data.short or os.fstat(fh.fileno()).st_size != info.size:
                        tally["torn"] += 1
                else:
                    tar.addfile(info)   # a hard link to a file already in, or what the file became
            tally["files"] += 1
            return
        info = tar.gettarinfo(path, arcname=arc)
        if info is None or info.ischr() or info.isblk():   # a socket, a device: nothing to keep
            return
        tar.addfile(info)
        if not info.isdir() or arc in only_folder:
            return
        names = sorted(os.listdir(path))
    except (FileNotFoundError, NotADirectoryError):
        tally["vanished"] += 1
        return
    for name in names:
        add_tree(tar, f"{arc}/{name}", tally, only_folder, leave_out)


def add_bytes(tar, arc, data):
    info = tarfile.TarInfo(arc)
    info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
    tar.addfile(info, io.BytesIO(data))


def project_parts(name):
    """A project's files and folders, as paths under /data."""
    p = paths(name)
    return [os.path.relpath(p[key], DATA) for key in ("record", "ports", "host_keys", "provision", "tutor_keys", "workspace", "home")]


def sandbox_running(name):
    """Whether a project's sandbox runs; True when the daemon cannot say (the backup then says
    its files may be torn rather than nothing)."""
    try:
        container = inspect_container(paths(name)["container"])
    except CoreError:
        return True
    return bool(container and container["State"].get("Running"))


def image_identity():
    """What this clanker-learn is, as far as it knows: the tags of the three inner images (a
    hash of their definitions), the mode it runs in, the newest tutor database it opens."""
    out = {kind: image_tag(folder, repo) for kind, (folder, repo, _label) in IMAGES.items() if os.path.isdir(folder)}
    with contextlib.suppress(OSError, ValueError):
        out["mode"] = json.loads(read_text("/run/cl/state.json")).get("mode")
    with contextlib.suppress(CoreError):
        out["tutor_db_version"] = tutor_db_version_supported()
    return out


def running_mode():
    """The mode this clanker-learn runs in, "sysbox" or "privileged", as the init declared it
    (its state, else CL_MODE, which the broker inherits from it); None when neither says."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        mode = json.loads(read_text("/run/cl/state.json")).get("mode")
        if mode in ("sysbox", "privileged"):
            return mode
    mode = os.environ.get("CL_MODE", "")
    return mode if mode in ("sysbox", "privileged") else None


def backup(say=say_nothing):
    """`cl backup`: one archive of everything clanker-learn keeps but the distributions."""
    with backup_lane():
        return write_backup(say)


def write_backup(say=say_nothing, kind="backup"):
    """The backup itself, with the backup lane in hand. Each project is copied under its lock
    (waited for: a create, a destroy or a start is not cut through); its sandbox may run and
    write meanwhile, which the result says."""
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    for left in os.listdir(BACKUPS):   # what a backup that was killed left
        if left.startswith(".cl-backup-"):
            with contextlib.suppress(OSError):
                remove_path(os.path.join(BACKUPS, left))
    began, made_at = time.monotonic(), datetime.datetime.now(datetime.UTC)
    name = f"cl-backup-{made_at.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    final, temporary, db_copy = f"{BACKUPS}/{name}", f"{BACKUPS}/.{name}.part", f"{BACKUPS}/.{name}.tutor.db"
    if os.path.exists(final):
        raise CoreError(f"A backup named {name} is there already; try again in a second.")
    projects = []
    for one in recorded_names():
        with contextlib.suppress(CoreError):
            record = read_record(one)
            if record["wanted"] != "destroying":
                projects.append({"name": one, "agents": record["agents"], "editor": record["editor"], "created": record["created"]})
    running = [one["name"] for one in projects if sandbox_running(one["name"])]
    tally = {"files": 0, "torn": 0, "vanished": 0}
    try:
        say("copying the tutor's database")
        db_version = snapshot_tutor_db(db_copy)
        manifest = {"format": BACKUP_FORMAT, "format_version": BACKUP_FORMAT_VERSION, "kind": kind,
                    "made_at": made_at.isoformat(timespec="seconds"), "made_by": image_identity(),
                    "tutor_db_version": db_version, "projects": projects, "running": running,
                    "inside": [top for top in BACKUP_TOPS if os.path.lexists(f"{DATA}/{top}")], "not_inside": BACKUP_LEAVES_OUT}
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
            with tarfile.open(fileobj=fh, mode="w:gz", compresslevel=BACKUP_GZIP_LEVEL, format=tarfile.PAX_FORMAT) as tar:
                add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
                say("copying the settings, the password, the keys and the web server's certificate")
                # The projects' own parts of core/ and tutor/ are added with each project, under its lock.
                per_project = ("core/projects", "core/provision", "core/keys", "core/ports", "tutor/keys")
                for top in BACKUP_TOPS:
                    if top not in ("projects", "homes") and os.path.lexists(f"{DATA}/{top}"):
                        add_tree(tar, top, tally, only_folder=per_project,
                                 leave_out=("tutor/tutor.db", "tutor/tutor.db-wal", "tutor/tutor.db-shm", "web/downloads"))
                if db_version is not None:
                    tar.add(db_copy, arcname="tutor/tutor.db")
                for top in ("projects", "homes"):
                    if os.path.isdir(f"{DATA}/{top}"):
                        add_tree(tar, top, tally, only_folder=(top,))
                for one in projects:
                    say(f"copying {one['name']}" + (" (it runs: what it writes meanwhile may be torn)" if one["name"] in running else ""))
                    with locked(one["name"]):
                        if not os.path.exists(paths(one["name"])["record"]):
                            say(f"{one['name']} was destroyed meanwhile; it is not in the backup")
                            continue
                        for part in project_parts(one["name"]):
                            if os.path.lexists(f"{DATA}/{part}"):   # a project without ports named by hand has no ports file
                                add_tree(tar, part, tally)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, final)
        fsync_folder(BACKUPS)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        if isinstance(exc, OSError):
            raise CoreError(f"The backup could not be written ({exc.strerror or exc}); nothing was kept.") from exc
        raise
    finally:
        with contextlib.suppress(OSError):
            os.remove(db_copy)
    size, seconds = os.path.getsize(final), round(time.monotonic() - began, 1)
    said = f"{name}: {human_size(size)}, made in {seconds} s, with {len(projects)} project{'s' * (len(projects) != 1)}."
    if running:
        said += (f" Made while {len(running)} project{'s were' if len(running) != 1 else ' was'} running ({', '.join(running)}):"
                 " files being written at that moment may be torn.")
    if tally["torn"] or tally["vanished"]:
        said += f" {tally['torn']} file(s) changed while they were copied and {tally['vanished']} vanished."
    said += " The fetched software is not in it: it is fetched again."
    return {"backup": name, "path": final, "size": size, "seconds": seconds, "projects": [one["name"] for one in projects],
            "running": running, "files": tally["files"], "torn": tally["torn"], "vanished": tally["vanished"], "said": said,
            "manifest": manifest}


def checked_backup_name(name):
    if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
        raise CoreError("A backup is named as clanker-learn names it: cl-backup-<date>T<time>Z.tar.gz.")
    return name


def read_manifest(path):
    """The manifest of an archive, checked to be a clanker-learn backup's; it is its first
    member, so this reads the start of the file only."""
    try:
        with tarfile.open(path, "r:*") as tar:
            first = tar.next()
            if first is None or first.name != "manifest.json" or not first.isreg() or first.size > 1024 * 1024:
                raise ValueError("it does not begin with clanker-learn's manifest")
            manifest = json.loads(tar.extractfile(first).read())
    except tarfile.ReadError as exc:
        raise CoreError(f"{os.path.basename(path)} is not a clanker-learn backup (it is not a tar archive).") from exc
    except (OSError, EOFError, tarfile.TarError, ValueError, UnicodeDecodeError) as exc:
        raise CoreError(f"{os.path.basename(path)} is not a clanker-learn backup ({exc}).") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise CoreError(f"{os.path.basename(path)} is not a clanker-learn backup (its manifest is another's).")
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise CoreError(f"{os.path.basename(path)} is a backup of format {manifest.get('format_version')!r}; this clanker-learn reads format "
                        f"{BACKUP_FORMAT_VERSION} only.")
    return manifest


def backups_list():
    """`cl backups`: every backup, newest first: its name, when it was made, its size, its
    projects; one whose manifest cannot be read says why."""
    try:
        names = sorted((one for one in os.listdir(BACKUPS) if BACKUP_NAME.fullmatch(one)), reverse=True)
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        path = f"{BACKUPS}/{name}"
        try:
            row = {"backup": name, "size": os.path.getsize(path)}
        except OSError:
            continue   # removed meanwhile
        try:
            manifest = read_manifest(path)
            projects = [one["name"] for one in manifest.get("projects", []) if isinstance(one, dict) and isinstance(one.get("name"), str)]
            row.update(made_at=str(manifest.get("made_at", "")), kind=str(manifest.get("kind", "")), projects=projects,
                       running=list(manifest.get("running", [])))
        except CoreError as exc:
            row["problem"] = str(exc)
        out.append(row)
    return out


def backup_rm(name):
    """`cl backup-rm <name>`."""
    path = f"{BACKUPS}/{checked_backup_name(name)}"
    try:
        os.remove(path)
    except FileNotFoundError:
        raise CoreError(f"There is no backup named {name}.") from None
    return {"backup": name, "removed": True}


def backup_fetch(name):
    """One backup made readable to the web server for one download: a hard link to it in the
    web server's own folder (the same volume: nothing is copied), under a name nobody can
    guess. The web server opens it and removes the link at once. The backups' folder itself is
    never mounted into the web server: it would read every backup, each of which holds the
    password's hash, the tutor's token and every project's keys (research-sandbox keeps its
    exports out of its web server for the same reason)."""
    path = f"{BACKUPS}/{checked_backup_name(name)}"
    if not os.path.isfile(path):
        raise CoreError(f"There is no backup named {name}.")
    os.makedirs(WEB_DOWNLOADS, mode=0o700, exist_ok=True)
    for left in os.listdir(WEB_DOWNLOADS):   # links the web server never took (it died, say): each holds its backup's space
        with contextlib.suppress(OSError):
            if time.time() - os.lstat(f"{WEB_DOWNLOADS}/{left}").st_mtime > DOWNLOAD_LINK_S:
                os.remove(f"{WEB_DOWNLOADS}/{left}")
    link = f"{secrets.token_hex(8)}-{name}"
    os.link(path, f"{WEB_DOWNLOADS}/{link}")
    os.utime(f"{WEB_DOWNLOADS}/{link}", follow_symlinks=False)   # a link shares the file's times: the clearing above goes by when it was made
    return {"backup": name, "size": os.path.getsize(path), "file": link}


def tutor_activity(timeout=5):
    """The answers being written now, as the tutor service lists them at /activity, or None
    and why when it cannot be asked (asked as the keeper asks its health, with the secret the
    web server and the tutor service share)."""
    try:
        container = inspect_container(TUTOR_CONTAINER)
        if container is None or not container["State"].get("Running"):
            return None, "the tutor service is not running"
        address = (container["NetworkSettings"]["Networks"].get(FRONT_NET) or {}).get("IPAddress", "")
        if not address:
            return None, f"the tutor service has no address on {FRONT_NET}"
        request = urllib.request.Request(f"http://{address}:{TUTOR_PORT}/activity", headers={"X-CL-Secret": read_text(tutor_secret_path()).strip()})
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            running = json.loads(answer.read(1024 * 1024))["result"]["running"]
        return list(running), ""
    except (CoreError, OSError, urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        return None, f"the tutor service could not be asked ({getattr(exc, 'reason', exc)})"


def restore_filter(member, dest):
    """tar's own filter for where a member lands (inside the staging folder, no absolute
    path), with the archive's owners and modes kept as they are (a home is the sandbox user's);
    a hard link must point inside too; a device is not made."""
    tarfile.tar_filter(member, dest)   # raises for a member that would land outside; its changed copy is not used
    if member.ischr() or member.isblk():
        return None
    if member.islnk():
        target = os.path.realpath(os.path.join(dest, member.linkname))
        if os.path.commonpath([target, os.path.realpath(dest)]) != os.path.realpath(dest):
            raise tarfile.OutsideDestinationError(member, target)
    return member


def restored_members(tar):
    for member in tar:
        if member.name == "manifest.json":
            continue
        if member.name.split("/", 1)[0] not in BACKUP_TOPS:
            raise CoreError(f"The backup holds {member.name[:200]!r}, which is not something clanker-learn keeps; nothing was replaced.")
        yield member


def labelled(kind, running_only=False):
    """The names of the inner daemon's containers (or networks) that belong to a project: what
    the core labels with cl.project, recorded or not."""
    if kind == "network":
        listed = docker("network", "ls", "--filter", "label=cl.project", "--format", "{{.Name}}")
    else:
        listed = docker("ps", *(() if running_only else ("-a",)), "--filter", "label=cl.project", "--format", "{{.Names}}")
    return listed.stdout.split()


def restore(source, say=say_nothing):
    """`cl restore <file or name>`: everything a backup holds put back in place of what is
    there. Refused while a project runs, an answer is being written, or a backup, a build or
    an operation is under way. What is there is first kept as a backup of its own. Every
    project's container is removed (its next start makes it again from its record, and its
    setup recipe runs), and the web server and the tutor service are removed too: the keeper
    makes them again within seconds, and they read the restored state."""
    global BACKUP_HELD
    path = f"{BACKUPS}/{source}" if BACKUP_NAME.fullmatch(source) else os.path.abspath(source)
    if not os.path.isfile(path):
        raise CoreError(f"There is no backup at {path}.")
    manifest = read_manifest(path)
    have = tutor_db_version_supported()
    if isinstance(manifest.get("tutor_db_version"), int) and manifest["tutor_db_version"] > have:
        raise CoreError(f"This backup was made by a newer clanker-learn: its tutor's database is at version {manifest['tutor_db_version']}, "
                        f"and this one knows versions up to {have}. Restore it with that version or a newer one; nothing was changed.")
    held, names, kept = take_backup_lane(), set(), set()
    try:
        build = lane_file("dist.lock")
        held.append(build)
        try:
            fcntl.flock(build, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CoreError("The software is being built now; restore when that is done.") from None
        names |= set(recorded_names()) | {one["name"] for one in manifest.get("projects", []) if isinstance(one, dict) and NAME.fullmatch(str(one.get("name")))}
        for name in sorted(names):
            lock = try_lock(name)
            if lock is None:
                raise CoreError(f"Something is being done with {name}; restore when it is over. Nothing was changed.")
            held.append(lock)
            HELD.add(name)
        running = sorted(container.removeprefix("cl-sb-") for container in labelled("container", running_only=True))
        if running:
            raise CoreError(f"Stop every project first: {', '.join(running)} {'is' if len(running) == 1 else 'are'} running, and a restore "
                            "replaces the projects' folders. Nothing was changed.")
        answers, why_not = tutor_activity()
        if answers:
            raise CoreError(f"The tutor is writing {len(answers)} answer(s) now; restore when they are done. Nothing was changed.")
        notes = [] if answers is not None else [f"Whether an answer was being written could not be asked ({why_not}); with every project stopped none can be."]
        say("keeping what is there as a backup first")
        BACKUP_HELD = True
        try:
            safety = write_backup(say, kind="what was there before a restore")
        finally:
            BACKUP_HELD = False
        say(f"kept as {safety['backup']}; unpacking {os.path.basename(path)}")
        remove_path(RESTORE_STAGING)
        os.makedirs(RESTORE_STAGING, mode=0o700)
        try:
            with tarfile.open(path, "r:*") as tar:
                tar.extractall(RESTORE_STAGING, members=restored_members(tar), numeric_owner=True, filter=restore_filter)
            if not os.path.isdir(f"{RESTORE_STAGING}/core"):
                raise CoreError("The backup holds no core folder; nothing was replaced.")
        except BaseException as exc:
            remove_path(RESTORE_STAGING)
            if isinstance(exc, (OSError, EOFError, tarfile.TarError)):
                raise CoreError(f"The backup could not be unpacked ({exc}); nothing was replaced.") from exc
            raise
        say("removing the projects' containers")
        for container in labelled("container"):
            docker("rm", "-f", "-v", container, timeout=STOP_GRACE_S + 120)
        say("putting the backup's folders in place")
        remove_path(RESTORE_OLD)
        os.makedirs(RESTORE_OLD, mode=0o700)
        try:
            # Renames on one volume: each folder is replaced at once. The web server and the
            # tutor service keep the folders they have mounted (the old ones, renamed away)
            # until they are made again, below.
            for top in BACKUP_TOPS:
                if os.path.lexists(f"{DATA}/{top}"):
                    os.rename(f"{DATA}/{top}", f"{RESTORE_OLD}/{top}")
                if os.path.lexists(f"{RESTORE_STAGING}/{top}"):
                    os.rename(f"{RESTORE_STAGING}/{top}", f"{DATA}/{top}")
            fsync_folder(DATA)
        except OSError as exc:
            raise CoreError(f"The folders could not all be put in place ({exc}). What was there is in {RESTORE_OLD} and in the backup "
                            f"{safety['backup']}.") from exc
        for container in (WEB_CONTAINER, TUTOR_CONTAINER):
            if there_is("container", container):
                docker("rm", "-f", "-v", container, timeout=STOP_GRACE_S + 120)
        kept = {one["name"] for one in manifest.get("projects", []) if isinstance(one, dict) and isinstance(one.get("name"), str)}
        for network in labelled("network"):
            if network.removeprefix("cl-net-") not in kept:
                docker("network", "rm", network, check=False)
        remove_path(RESTORE_OLD)
        remove_path(RESTORE_STAGING)
    finally:
        HELD.difference_update(names)
        for fh in held:
            fh.close()
    projects = sorted(kept)
    said = (f"Restored {os.path.basename(path)} (made {manifest.get('made_at', '?')}), with {len(projects)} project(s): {', '.join(projects) or 'none'}. "
            f"What was there before is kept as {safety['backup']}. The web server and the tutor service are being made again and read the "
            "restored state in a few seconds; log in again with the backup's password. Start your projects: each one's container is made "
            "again, and its setup recipe runs once.")
    return {"restored": os.path.basename(path), "made_at": manifest.get("made_at"), "projects": projects, "safety_backup": safety["backup"],
            "notes": notes, "said": said}


# --- the `cl` command ----------------------------------------------------------------------

def read_new_password(stdin=None):
    """The new password: typed twice without echo at a terminal, else all of standard input,
    less one trailing line end (which `echo` adds and nobody means)."""
    stdin = stdin or sys.stdin
    if stdin.isatty():
        import getpass
        first = getpass.getpass("New password: ")
        if getpass.getpass("Again: ") != first:
            raise CoreError("The two did not match; nothing was changed.")
        return first
    text = stdin.read()
    return text[:-2] if text.endswith("\r\n") else text.removesuffix("\n")


def set_password(password):
    """Set the password the interface logs in with. What is stored is a hash of the proof the
    browser derives from it, never the password; setting it ends every session."""
    import broker_auth
    if len(password) < broker_auth.MIN_PASSWORD_LENGTH:
        raise CoreError(f"The password must have at least {broker_auth.MIN_PASSWORD_LENGTH} characters; nothing was changed.")
    broker_auth.set_password(broker_auth.derive_login_proof(password), path=broker_auth.PASSWD_FILE)
    return {"password": "set"}


def limit_option(text, kind):
    if text is None:
        return UNCHANGED
    if text.strip().lower() == "none":
        return None
    if kind == "memory":
        return parse_size(text)
    try:
        return int(text) if kind == "pids" else float(text)
    except ValueError as exc:
        raise CoreError(f"{text!r} is not a number.") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cl", description="clanker-learn's projects, from inside its container.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("list")
    verbs.add_parser("start-wanted")
    keeper = verbs.add_parser("web-keep", help="(run by the init) bring the web server up and keep it up")
    keeper.add_argument("status_port", type=int)
    verbs.add_parser("passwd", help="set the interface's password: read from standard input, or asked for at a terminal")
    verbs.add_parser("software", help="what is pulled of the agent and the editor, and at which version")
    for verb in ("agent", "editor"):
        actions = verbs.add_parser(verb, help=f"pull or refresh the {verb}'s distribution").add_subparsers(dest="action", required=True)
        for action in ("pull", "refresh"):
            one = actions.add_parser(action, help="pull again at the version in effect" if action == "pull"
                                     else "raise the version to upstream's newest and pull it, if it is newer")
            if verb == "agent":
                one.add_argument("agent", nargs="?", default="claude", choices=AGENTS)
            if action == "refresh":
                one.add_argument("--check", action="store_true", help="only say whether upstream has a newer version")
    deploys = verbs.add_parser("agents", help="add an agent to a running project, or update its agents").add_subparsers(dest="action", required=True)
    one = deploys.add_parser("add", help="deploy an agent into a running project and add it to the project")
    one.add_argument("name")
    one.add_argument("agent", nargs="?", default="claude")
    deploys.add_parser("update", help="deploy the pulled version of every agent of a running project").add_argument("name")
    one = deploys.add_parser("model", help="set Claude's model and effort of a running project (an empty value clears it)")
    one.add_argument("name")
    one.add_argument("--model", help="a tier (opus, sonnet, haiku, fable) or a model id")
    one.add_argument("--effort", help="low, medium, high, xhigh or max")
    verbs.add_parser("backup", help="one archive of everything clanker-learn keeps but the fetched software, under /data/backups")
    verbs.add_parser("backups", help="the backups there are")
    verbs.add_parser("backup-rm", help="remove a backup").add_argument("backup")
    verbs.add_parser("restore", help="put a backup back in place of everything there is (every project stopped first)").add_argument(
        "backup", help="a backup's name (cl backups lists them) or the path of an archive")
    for verb in ("status", "create", "start", "stop", "recreate", "destroy", "limits"):
        one = verbs.add_parser(verb)
        one.add_argument("name")
        if verb in ("create", "limits"):
            for option in ("pids", "memory", "cpus"):
                one.add_argument(f"--{option}", help="a number (memory: 512m, 2g), or none")
        if verb == "create":
            one.add_argument("--agent", action="append", default=[], dest="agents", help="an agent to add (claude); again for another")
            one.add_argument("--editor", action="store_true", help="give the project the editor")
            one.add_argument("--claude-model", default="", help="Claude's model: a tier (opus, sonnet, haiku, fable) or a model id")
            one.add_argument("--claude-effort", default="", help="Claude's effort: low, medium, high, xhigh or max")
    args = parser.parse_args(argv)

    def say(message):
        print(f"clanker-learn: {message}", file=sys.stderr, flush=True)

    try:
        if args.verb == "web-keep":
            def report(key, value):
                print(json.dumps({key: value}), flush=True)
            return keep_web(os.environ, args.status_port, report)
        if args.verb == "passwd":
            out = set_password(read_new_password())
        elif args.verb == "list":
            out = list_projects()
        elif args.verb == "software":
            out = software_status()
        elif args.verb in ("agent", "editor"):
            software = args.agent if args.verb == "agent" else "code-server"
            if args.action == "pull":
                out = software_pull(software, say)
            else:
                out = refresh_check(software) if args.check else software_refresh(software, say)
        elif args.verb == "agents":
            if args.action == "add":
                out = agents_add(args.name, args.agent)
            elif args.action == "update":
                out = agents_update(args.name)
            else:
                out = agents_model(args.name, args.model, args.effort)
        elif args.verb == "start-wanted":
            out = start_wanted(say)
        elif args.verb == "backup":
            out = backup(say)
        elif args.verb == "backups":
            out = backups_list()
        elif args.verb == "backup-rm":
            out = backup_rm(args.backup)
        elif args.verb == "restore":
            out = restore(args.backup, say)
        elif args.verb == "status":
            out = status(args.name)
        elif args.verb in ("create", "limits"):
            given = {k: limit_option(getattr(args, k), k) for k in ("pids", "memory", "cpus")}
            if args.verb == "create":
                out = create(args.name, {k: v for k, v in given.items() if v is not UNCHANGED}, say, args.agents, args.editor,
                             args.claude_model, args.claude_effort)
            else:
                out = set_limits(args.name, **given)
        elif args.verb == "start":
            out = start(args.name, say)
        elif args.verb == "stop":
            out = stop(args.name)
        elif args.verb == "recreate":
            out = recreate(args.name, say)
        else:
            out = destroy(args.name)
    except (CoreError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 1 if args.verb == "start-wanted" and out["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
