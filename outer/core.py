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
    /data/tutor/keys/<name>           the key the tutor logs in with, and known_hosts
    cl-sb-<name>, cl-net-<name>       the container and its network

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
import contextlib
import datetime
import fcntl
import hashlib
import io
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tarfile
import time

DATA = "/data"
LOCKS = "/run/cl/locks"
SANDBOX_DIR = "/opt/cl/sandbox"
IMAGE_REPO = "cl-sandbox"
BUILD_NET = "cl-build"
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
SSHD_SETTINGS = """\
# Written by clanker-learn at every start of the sandbox.
HostKey /etc/cl-provision/ssh_host_ed25519_key
PasswordAuthentication no
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
    path = paths(record["name"])["record"]
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(record, fh, indent=2)
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


def image_tag(folder=None):
    """The tag is a hash of the definition: a changed definition is another image, and a
    sandbox keeps the one it was created from until it is recreated."""
    folder = folder or SANDBOX_DIR
    digest = hashlib.sha256()
    for name in image_files(folder):
        with open(os.path.join(folder, name), "rb") as fh:
            digest.update(name.encode() + b"\0" + fh.read() + b"\0")
    return f"{IMAGE_REPO}:{digest.hexdigest()[:12]}"


def ensure_image(say=say_nothing):
    """The sandbox image, built if it is not there: once on a new installation, and again when
    its definition changed. The build runs on a network of its own, which the guard rules
    fence like a sandbox's; one build at a time."""
    tag = image_tag()
    if there_is("image", tag):
        return tag
    with flocked(".build.lock"):
        if there_is("image", tag):
            return tag
        say("building the sandbox image (once; this takes a few minutes and needs the internet)")
        if not there_is("network", BUILD_NET):
            docker("network", "create", BUILD_NET)
        done = docker("build", "--network", BUILD_NET, "-t", tag, SANDBOX_DIR, timeout=BUILD_TIMEOUT_S, check=False)
        if done.returncode != 0:
            raise CoreError("The sandbox image could not be built: " + (done.stderr.strip() or done.stdout.strip())[-600:])
        say("the sandbox image is built")
    return tag


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


def write_provisioning(name):
    """The provisioning folder, written afresh from what the core holds. Files are replaced in
    place, never the folder: a running sandbox has it mounted."""
    p = paths(name)
    tutor_key, host_key = f"{p['tutor_keys']}/id_ed25519", f"{p['host_keys']}/ssh_host_ed25519_key"
    keygen(tutor_key, f"cl-tutor-{name}")
    keygen(host_key, f"cl-sb-{name}")
    host_public = read_text(host_key + ".pub")
    write_file(f"{p['tutor_keys']}/known_hosts", f"{p['container']} {' '.join(host_public.split()[:2])}\n", 0o644)
    os.makedirs(p["provision"], mode=0o755, exist_ok=True)
    wanted = {
        "authorized_keys": (read_text(tutor_key + ".pub"), 0o644),
        "sshd_config": (SSHD_SETTINGS, 0o644),
        "ssh_host_ed25519_key": (read_text(host_key), 0o600),
        "ssh_host_ed25519_key.pub": (host_public, 0o644),
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
    found = [key for key in FOLDERS if os.path.lexists(p[key])]
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
        attached = docker("network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", p["network"], check=False)
        for other in attached.stdout.split():
            docker("network", "disconnect", "-f", p["network"], other, check=False)
        done = docker("network", "rm", p["network"], check=False)
        if done.returncode != 0:
            problems.append(f"the network ({done.stderr.strip()[-200:]})")
    for key in FOLDERS:
        try:
            if os.path.isdir(p[key]) and not os.path.islink(p[key]):
                shutil.rmtree(p[key])  # does not follow a link found inside
            elif os.path.lexists(p[key]):
                os.remove(p[key])
        except OSError as exc:
            problems.append(f"{p[key]} ({exc})")
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
    verbs.add_parser("passwd", help="set the interface's password: read from standard input, or asked for at a terminal")
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
        if args.verb == "passwd":
            out = set_password(read_new_password())
        elif args.verb == "list":
            out = list_projects()
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
