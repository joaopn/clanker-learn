#!/usr/bin/env python3
"""The core: learning projects and their sandboxes.

A project is its record, a small file under /data/core/projects. Everything else a project
has is made from the record by `ensure`, which makes what is missing and touches nothing that
is there: the workspace and home folders, two key pairs, the provisioning folder, a network of
the inner daemon, and the sandbox, one plain container on that network. So a project whose
creation was cut short is finished by its next start.

    /data/core/projects/<name>.json   the record: limits, whether it should be running
    /data/projects/<name>             the workspace        -> /workspace in the sandbox
    /data/homes/<name>                the home folder      -> /home/learner
    /data/core/provision/<name>       the tutor's public key, the sandbox's SSH host key,
                                      sshd's settings      -> /etc/cl-provision, read-only
    /data/core/keys/<name>            the sandbox's host key pair (what provisioning copies)
                                      and the Shell password (provisioning gets its hash)
    /data/core/ports/<name>.json      the project's ports named by hand
    /data/tutor/keys/<name>           the key the tutor logs in with, and known_hosts
    cl-sb-<name>, cl-net-<name>       the container and its network

The software projects get (Claude Code, the editor) is fetched once into a distribution:

    /data/dist/agents/claude          what Claude Code's installer left as ~/.local
    /data/dist/agents/claude.json     its version and when it was pulled (also in .cl-dist inside)
    /data/dist/editor, editor.json    the same for the editor (code-server)
    /data/dist-staging                where a distribution is built before it replaces the old
    /data/versions.local.env          versions raised by Refresh, over /opt/cl/versions.env

A sandbox is created without privilege, without a published port, without a restart policy
(the init starts what should run, after the guard rules are in place) and without raw
sockets: the services that will sit on its network also sit on another, and with hand-built
packets a sandbox could address them there. `ping` keeps working through a kernel setting.

Every operation on a project holds that project's lock, an flock on an open file: the kernel
drops it when the holder dies, so nothing stays locked behind a killed caller.

A destroy marks the record first ("destroying") and removes it last. A destroy that was cut
short is therefore never taken for a creation that was: nothing starts such a project, and
running destroy again finishes it.

This module is also the `cl` command: `cl create <name>`, `cl list`, and so on.
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
IMAGES = {"sandbox": (SANDBOX_DIR, IMAGE_REPO, "the sandbox image"), "web": (WEB_DIR, WEB_REPO, "the web server's image")}
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
EDITOR_PORT = 8443            # code-server, started by sandbox/sandbox_init.py
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

def new_record(name, limits=None):
    return {"name": valid_name(name), "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "wanted": "running", "limits": checked_limits(limits)}


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
# The closed table of what can be pulled. `dist` is the place under /data/dist; `launcher`
# the program, relative to the distribution; `in_tree` where the launcher must lead, with the
# version in it; `installer` the vendor's script and the shell and arguments it is run with.
SOFTWARE = {
    "claude": {
        "kind": "agent", "label": "Claude Code", "key": "CLAUDE_CODE_VERSION", "report": "agents",
        "dist": "agents/claude", "launcher": "bin/claude", "in_tree": re.compile(r"share/claude/versions/([^/]+)"),
        "installer": "https://claude.ai/install.sh", "shell": "bash", "args": ("{version}",),
        "upstream": "https://downloads.claude.ai/claude-code-releases/latest",   # a bare version
    },
    "code-server": {
        "kind": "editor", "label": "the editor (code-server)", "key": "CODE_SERVER_VERSION", "report": "editor",
        "dist": "editor", "launcher": "bin/code-server", "in_tree": re.compile(r"lib/code-server-([^/]+)/bin/code-server"),
        "installer": "https://code-server.dev/install.sh", "shell": "sh", "args": ("--method", "standalone", "--version", "{version}"),
        "upstream": "https://github.com/coder/code-server/releases/latest",      # redirects to .../tag/v<version>
    },
}
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
            "installer": f"{PRELOAD}/installers/{software}.sh", "upstream": f"{PRELOAD}/upstream/{software}"}


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
    if key not in {spec["key"] for spec in SOFTWARE.values()}:
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
    """"Use repo version": take the software's key out of the local override. Nothing to take
    out is no error. Refused while that software is being built."""
    spec = known_software(software)
    with build_lane(software), flocked("versions.lock"):
        removed = spec["key"] in parse_pins(versions_local())
        if removed:
            write_pins(pin_lines_without(spec["key"]))
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
        return {"version": checked_version(mark["version"], "The pulled version"), "pulled_at": str(mark.get("pulled_at", ""))}
    except (OSError, ValueError, KeyError, TypeError, CoreError):
        return None


def relativize_launcher(root, launcher):
    """The installer links the launcher to an absolute path under the build's home, which
    leads nowhere in a project's home: it is made relative, as RS does. Any other absolute
    link is left for the check to refuse."""
    path = os.path.join(root, launcher)
    if not os.path.islink(path):
        return
    target = os.readlink(path)
    prefix = BUILD_HOME + "/.local/"
    if target.startswith(prefix):
        relative = os.path.relpath(os.path.join(root, target[len(prefix):]), os.path.dirname(path))
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


def install_dist(software, tree, version, say):
    """Mark a checked tree and put it in the distribution's place by renames: the old one is
    moved aside only once the new one is whole, and removed only after the new one is in. The
    folders above are never replaced: running sandboxes have /data/dist mounted."""
    p = dist_paths(software)
    if os.path.islink(tree) or not os.path.isdir(tree):   # before anything is written into it: never through a link
        raise CoreError("the installer left no folder of its own (a link, or nothing)")
    relativize_launcher(tree, SOFTWARE[software]["launcher"])
    check_tree(software, tree, version)
    mark = {"version": version, "pulled_at": now_utc()}
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


def build_dist(software, version, say=say_nothing):
    """Run the installer of `software` at `version` in a throwaway container of the sandbox
    image, on the fenced build network, with no capability, as the sandbox's user, its home the
    staging folder; check what it left, and install it. Three attempts, as RS."""
    spec, p = known_software(software), dist_paths(software)
    checked_version(version, f"The version of {spec['label']}")
    image = ensure_image(say, "sandbox")
    if not there_is("network", BUILD_NET):
        docker("network", "create", BUILD_NET)
    os.makedirs(f"{DATA}/dist-staging", mode=0o700, exist_ok=True)
    args = [arg.format(version=version) for arg in spec["args"]]
    mounts, script = [], f'set -euo pipefail; curl -fsSL "$0" | {spec["shell"]} -s -- "$@"'
    if os.path.isfile(p["installer"]):   # the tests' way in: the build network reaches no test server
        mounts, script = ["-v", f"{p['installer']}:/opt/cl-installer:ro"], f'set -euo pipefail; {spec["shell"]} /opt/cl-installer "$@"'
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
                return install_dist(software, os.path.join(p["staging"], ".local"), version, say)
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
            version = checked_version(json.loads(fh.read(4096))["version"], f"The version in {p['preload_json']}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CoreError(f"{p['preload_json']} cannot be read ({type(exc).__name__}).") from exc
    say(f"taking {spec['label']} {version} from {p['preload']}")
    os.makedirs(f"{DATA}/dist-staging", mode=0o700, exist_ok=True)
    fresh_folder(p["staging"])
    try:
        tree = os.path.join(p["staging"], ".local")
        shutil.copytree(p["preload"], tree, symlinks=True)
        return install_dist(software, tree, version, say)
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


def refresh_check(software):
    """The version in effect, upstream's newest, and whether upstream's is newer. Changes
    nothing."""
    current, latest = pin(software), upstream_version(software)
    return {"software": software, "version": current, "upstream": latest, "newer": is_newer(latest, current)}


def software_refresh(software, say=say_nothing):
    """Refresh: if upstream has a newer version, raise the pin to it and pull it; otherwise
    there is nothing to do (a pin is never moved down)."""
    spec = known_software(software)
    with build_lane(software):
        say(f"asking upstream for the newest version of {spec['label']}")
        check = refresh_check(software)
        if not check["newer"]:
            say(f"{spec['label']} {check['version']} is in effect and upstream's newest is {check['upstream']}: nothing to do")
            return dict(check, bumped=False)
        say(f"raising {spec['key']} from {check['version']} to {check['upstream']}")
        set_version_pin(spec["key"], check["upstream"])
        return dict(check, previous=check["version"], version=check["upstream"], bumped=True, pulled=build_dist(software, check["upstream"], say))


# Named as RS names them.
def known_agent(agent):
    if agent not in AGENTS:
        raise CoreError(f"There is no agent named {str(agent)[:40]!r} (there is {', '.join(AGENTS)}).")
    return agent


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


def dist_status_word(have, version):
    if have is None:
        return "not pulled"
    return "up to date" if have["version"] == version else STALE


def software_status():
    """One row per software, for Settings -> Software: what is pulled, the version in effect
    and the repository's, whether the local override sets it (so "Use repo version" can be
    offered), a status word, and whether it is being built now. Reads files only."""
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
        try:
            row["version"] = pin(software)
        except CoreError as exc:
            row["problem"] = problem or str(exc)
        row["status"] = dist_status_word(row["pulled"], row["version"])   # a version that cannot be read is never "up to date"
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


def write_provisioning(name):
    """The provisioning folder, written afresh from what the core holds. Files are replaced in
    place, never the folder: a running sandbox has it mounted."""
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
    write_provisioning(name)
    if not there_is("network", p["network"]):
        docker("network", "create", "--label", f"cl.project={name}", p["network"])
    # The network first, then the web server onto it: web_up, the other way round, makes its
    # container first and then lists the networks, so one of the two always connects them.
    connect_web(p["network"], say)
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
                        "state": "unknown", "address": "", "image": "", "container": "", "setup": "", "problem": str(exc)})
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
    than on loopback, the sandbox's own services (sshd, the files server) left out."""
    inodes = {}
    for text, v6 in ((tcp_text, False), (tcp6_text, True)):
        for address, port, inode in listening_sockets(text, v6):
            mapped = getattr(address, "ipv4_mapped", None) or address
            if mapped.is_loopback or port in (SSH_PORT, FILES_SERVER_PORT):
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
    own = {SSH_PORT: "sshd", FILES_SERVER_PORT: "the files server", EDITOR_PORT: "the editor"}
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


def create(name, limits=None, say=say_nothing):
    valid_name(name)
    with locked(name):
        if os.path.exists(paths(name)["record"]):
            raise CoreError(f"A project named {name} exists. (If it is being destroyed, `destroy {name}` finishes that.)")
        left = leftovers(name)
        if left:
            raise CoreError(f"Something of an earlier project named {name} is left ({', '.join(left)}). `destroy {name}` clears it.")
        record = new_record(name, limits)
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
    broker's socket folder read-only, its own folder of the data volume, and nothing of a
    project. The label is a hash of all of it: a container made from another image or with
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
        "-e", f"CL_WEB_PORT={port}", "-e", f"CL_LISTEN_SUBNET={FRONT_SUBNET}",
        "-e", f"CL_ORIGIN_PORTS={origin}", "-e", f"CL_PASSTHROUGH_PORTS={passthrough}",
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


def connect_web(network, say=say_nothing):
    """Put the web server on a project's network, if there is a web server. Not being able to
    is a line in the log, never a failed start of a project."""
    try:
        if not there_is("container", WEB_CONTAINER):
            return
        done = docker("network", "connect", network, WEB_CONTAINER, check=False)
        if done.returncode != 0 and "already exists" not in done.stderr:
            say(f"the web server could not be connected to {network}: {done.stderr.strip()[-200:]}")
    except CoreError as exc:
        say(f"the web server could not be connected to {network}: {exc}")


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


def keep_web(env, status_port, report, sleep=time.sleep, turns=None, now=time.monotonic):
    """Bring the web server up and keep it up, saying how it stands through `report(key,
    value)`: "preparing", "up", "off", or "down: <why>". Once it is up, the sandbox image is had
    ("sandbox_image"), then the agent's and the editor's distributions ("agents", "editor":
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
            if there_is("container", WEB_CONTAINER):  # left by an earlier start with CL_WEB=on
                docker("rm", "-f", "-v", WEB_CONTAINER, timeout=STOP_GRACE_S + 120)
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
    for verb in ("status", "create", "start", "stop", "recreate", "destroy", "limits"):
        one = verbs.add_parser(verb)
        one.add_argument("name")
        if verb in ("create", "limits"):
            for option in ("pids", "memory", "cpus"):
                one.add_argument(f"--{option}", help="a number (memory: 512m, 2g), or none")
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
        elif args.verb == "start-wanted":
            out = start_wanted(say)
        elif args.verb == "status":
            out = status(args.name)
        elif args.verb in ("create", "limits"):
            given = {k: limit_option(getattr(args, k), k) for k in ("pids", "memory", "cpus")}
            if args.verb == "create":
                out = create(args.name, {k: v for k, v in given.items() if v is not UNCHANGED}, say)
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
