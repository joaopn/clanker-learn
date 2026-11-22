#!/usr/bin/env python3
"""The init of a sandbox, run by tini as root at every start of the container.

  1. An empty home folder is filled from the system's skeleton (the home is a folder of
     clanker-learn's data volume, empty when the project is new).
  2. What the provisioning folder holds is installed: the tutor's public key as the sandbox
     user's authorized_keys, the sshd settings, and the Shell password (its hash) as the
     sandbox user's password, which the web server's terminal logs in with. The core rewrites
     that folder before every start and mounts it read-only, so a project made by an earlier
     version gains what a later one adds without being recreated.
  3. sshd is started: the tutor's way in, and the Shell's. Its host key is the one in the provisioning folder.
  4. The files server is started as the sandbox user.
  5. The Editor's server, code-server, is started as the sandbox user, with a login's
     environment, on port 8443 of every address, as the files server listens, so that the web
     server, on the project's network, reaches it. It asks for no password of its own (the
     gate is the web server's project session), sends no telemetry, looks for no update, asks no question about trusting the workspace, opens
     /workspace, and keeps its user data in the sandbox user's home, which persists.
  6. The setup recipe, /workspace/.learn/setup.sh, is run as the sandbox user if this
     container has not run it: once per container, so never after a plain stop and start
     and once after a recreate, which is how what the tutor installed comes back. Its state
     (none, running, done, failed) is in /var/lib/cl/setup, a file of the container itself,
     and its output in /var/log/cl/setup.log. Cut short by a stop, it runs again from the top
     at the next start, so a recipe must be safe to run twice.
  7. What /workspace/.learn/services lists is started as the sandbox user, one line each,
     `name: command`, with its output in /workspace/.learn/logs/<name>.log. They are started,
     not watched: one that ends stays down until the next start.
  8. Each agent the provisioning folder names (software.json) is deployed into the home by
     /opt/cl/deploy, run as the sandbox user with a time limit, one after the other: copied
     from the distributions (/opt/cl/dist, read-only) only while its launcher is not in the
     home, so a copy that updated itself is kept; its settings installed once, with the
     project's model and effort. None of this holds up sshd, the files server, code-server
     or a stop; what the deploy says is in the container's log.

Then it waits, and starts sshd, the files server or code-server again if one of them ends. On a stop it
asks every other process to end, gives them a few seconds, and exits; what is left is ended
with the container.
"""
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time

USER = "learner"
PROVISION = "/etc/cl-provision"
SSHD_CONFIG = "/etc/ssh/sshd_config.d/cl.conf"
FILES_SERVER = "/opt/cl/files_server.py"
EDITOR_PORT = 8443
STOP_GRACE_S = 5
RESTART_PAUSE_S = 2
SETUP_RECIPE = "/workspace/.learn/setup.sh"
SERVICES = "/workspace/.learn/services"
SERVICE_LOGS = "/workspace/.learn/logs"
SETUP_STATE = "/var/lib/cl/setup"
SETUP_LOG = "/var/log/cl/setup.log"
SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MAX_SERVICES = 64
SERVICES_BYTES = 64 * 1024
CRYPT_HASH = re.compile(r"\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}")   # as the core writes it
SOFTWARE_FILE = os.path.join(PROVISION, "software.json")
AGENTS = ("claude",)            # the core's closed set (outer/core.py, AGENTS)
DEPLOY = "/opt/cl/deploy"
DEPLOY_TIMEOUT_S = 900


def log(message):
    print(f"sandbox: {message}", file=sys.stderr, flush=True)


def fill_home(home, skeleton, uid, gid):
    """Copy the skeleton into a home that has nothing in it yet. A home with anything in it
    is the learner's and is left alone."""
    if os.listdir(home):
        return False
    shutil.copytree(skeleton, home, symlinks=True, dirs_exist_ok=True)
    for folder, names, files in os.walk(home):
        for name in names + files:
            os.lchown(os.path.join(folder, name), uid, gid)
    os.chown(home, uid, gid)
    return True


def install_provisioning(provision, home, uid, gid, sshd_config=SSHD_CONFIG):
    """Install the tutor's key and the sshd settings. What the folder does not hold is not
    installed, and what was installed before stays."""
    key = os.path.join(provision, "authorized_keys")
    if os.path.exists(key):
        ssh_dir = os.path.join(home, ".ssh")
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        os.chown(ssh_dir, uid, gid)
        os.chmod(ssh_dir, 0o700)
        target = os.path.join(ssh_dir, "authorized_keys")
        if os.path.islink(target):
            os.remove(target)  # never write through a link the sandbox user made
        shutil.copyfile(key, target)
        os.chown(target, uid, gid)
        os.chmod(target, 0o600)
    settings = os.path.join(provision, "sshd_config")
    if os.path.exists(settings):
        os.makedirs(os.path.dirname(sshd_config), exist_ok=True)
        shutil.copyfile(settings, sshd_config)
        os.chmod(sshd_config, 0o644)


def set_password(provision=PROVISION, user=USER, run=subprocess.run):
    """Set the sandbox user's password from the hash in the provisioning folder, on chpasswd's
    standard input. Without the file the account is left as it is. True when it was set."""
    try:
        with open(os.path.join(provision, "shell_password_hash")) as fh:
            hashed = fh.read(4096).strip()
    except FileNotFoundError:
        return False
    if not CRYPT_HASH.fullmatch(hashed):
        log("the Shell password's hash is not one; the account's password is left as it is")
        return False
    done = run(["chpasswd", "-e"], input=f"{user}:{hashed}\n".encode(), capture_output=True, timeout=30, check=False)
    if done.returncode != 0:
        log(f"the Shell password could not be set: {done.stderr.decode(errors='replace').strip()[-200:]}")
        return False
    return True


def start_sshd():
    os.makedirs("/run/sshd", mode=0o755, exist_ok=True)
    return subprocess.Popen(["/usr/sbin/sshd", "-D", "-e"])


def start_files_server(user):
    env = {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name, "PATH": "/usr/local/bin:/usr/bin:/bin"}
    return subprocess.Popen(["python3", FILES_SERVER], user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                            cwd="/", env=env, stdin=subprocess.DEVNULL)


def start_editor(user):
    return as_user(user, ["code-server", "--bind-addr", f"0.0.0.0:{EDITOR_PORT}", "--auth", "none", "--disable-telemetry",
                          "--disable-update-check", "--disable-workspace-trust", "--user-data-dir", os.path.join(user.pw_dir, ".local/share/code-server"),
                          "/workspace"])


def keep_running(services, running, tried, pause_s=RESTART_PAUSE_S, now=time.monotonic):
    """Start each service that is not running: at first, and again when it has ended, but not
    more often than every `pause_s` seconds."""
    for name, start in services.items():
        process = running.get(name)
        if process is not None and process.poll() is None:
            continue
        if name in tried and now() - tried[name] < pause_s:
            continue
        if process is not None:
            log(f"{name} ended; starting it again")
        tried[name] = now()
        try:
            running[name] = start()
        except OSError as exc:
            running[name] = None
            log(f"{name} could not be started: {exc}")


def user_env(user):
    return {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name, "SHELL": "/bin/bash",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def as_user(user, command, **more):
    """A process of the sandbox user's, in a session of its own, in the workspace."""
    return subprocess.Popen(command, user=user.pw_uid, group=user.pw_gid, extra_groups=os.getgrouplist(user.pw_name, user.pw_gid),
                            cwd="/workspace", env=user_env(user), stdin=subprocess.DEVNULL, start_new_session=True, **more)


def setup_state(path=None):
    try:
        with open(path or SETUP_STATE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def write_setup_state(text, path=None):
    path = path or SETUP_STATE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text + "\n")
    os.replace(tmp, path)


def setup_is_owed(state):
    """The recipe has not run in this container, or was cut short while running."""
    return state in ("", "running")


def start_setup(user, recipe=SETUP_RECIPE, log_path=SETUP_LOG):
    """Run the recipe if there is one. Returns the process, or None where there is nothing to
    run; the state is written either way."""
    if not os.path.isfile(recipe):
        write_setup_state("none")
        return None
    write_setup_state("running")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "ab") as out:
        log("running the setup recipe")
        return as_user(user, ["bash", "-l", recipe], stdout=out, stderr=subprocess.STDOUT)  # a login shell, as the services get


def setup_ended(setup):
    """Record how the recipe went, once it has ended. Returns the process while it still runs,
    else None. A state that cannot be written (a full disk, which a recipe that installs
    software is the likeliest to cause) is a line in the log, never the end of this init."""
    if setup is None or setup.poll() is None:
        return setup
    try:
        write_setup_state("done" if setup.returncode == 0 else f"failed (exit {setup.returncode})")
        log(f"the setup recipe ended with {setup.returncode}")
    except OSError as exc:
        log(f"the setup recipe ended with {setup.returncode}, which could not be recorded: {exc}")
    return None


def parse_services(text):
    """The services a file lists: (name, command) for each line `name: command`, at most
    MAX_SERVICES of them. Blank lines and lines that start with # are skipped; a line that is
    not of that shape, repeats a name, holds a NUL (no command can), or is one too many is
    returned in the second list."""
    services, refused, seen = [], [], set()
    for line in text.split("\n"):  # a newline and nothing else ends a line: splitlines() also cuts at a form feed, U+2028...
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, colon, command = line.partition(":")
        name, command = name.strip(), command.strip()
        if colon and command and "\0" not in command and SERVICE_NAME.fullmatch(name) and name not in seen and len(services) < MAX_SERVICES:
            seen.add(name)
            services.append((name, command))
        else:
            refused.append(line)
    return services, refused


def whole_lines(data, limit):
    """The first `limit` bytes of the services file, of which one byte more than that was
    read, without a last line that the limit cut: a command cut in the middle is another
    command."""
    if len(data) <= limit:
        return data
    log(f"the services file is longer than {limit} bytes; what is past that is not read")
    return data[:data.rfind(b"\n") + 1]  # a line that ends with the one byte read past the limit is whole, and kept


def start_listed_services(user, path=SERVICES):
    """Start what the services file lists. Returns the processes, for the init to collect
    when they end."""
    # Read as the sandbox user, with a time limit and a size limit: this init, which is root,
    # never opens a path the sandbox user can turn into a link or a pipe.
    try:
        read = subprocess.run(["head", "-c", str(SERVICES_BYTES + 1), path], user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if read.returncode != 0:
        return []
    services, refused = parse_services(whole_lines(read.stdout, SERVICES_BYTES).decode(errors="replace"))
    for line in refused[:20]:
        log(f"not a service line, skipped: {line[:80]}")
    started = []
    for name, command in services:
        # The log is opened by the service's own shell, as the sandbox user: this init, which
        # is root, never opens a path the sandbox user chose. A log that cannot be opened is
        # no reason not to run the service.
        log_file = f"{SERVICE_LOGS}/{name}.log"
        script = f'mkdir -p {SERVICE_LOGS} 2>/dev/null; ( : >> {log_file} ) 2>/dev/null && exec >> {log_file} 2>&1; exec bash -lc "$1"'
        try:
            started.append(as_user(user, ["bash", "-c", script, "service", command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            log(f"started the service {name}")
        except (OSError, ValueError) as exc:
            log(f"the service {name} could not be started: {exc}")
    return started


def provisioned_agents(path=SOFTWARE_FILE):
    """The agents the provisioning folder names, from the closed set; none where the file is
    missing or holds anything else."""
    try:
        with open(path, "rb") as fh:
            named = json.loads(fh.read(4096)).get("agents")
    except (OSError, ValueError, RecursionError, AttributeError):
        return []
    return [agent for agent in AGENTS if isinstance(named, list) and agent in named]


def next_deploy(user, deploying, owed):
    """One deploy at a time: once the one running has ended, the next agent of `owed` is
    started. Returns the deploy running now, or None."""
    if deploying is not None and deploying.poll() is None:
        return deploying
    while owed:
        agent = owed.pop(0)
        try:
            # Its output goes to the container's log, as this init's does.
            return as_user(user, ["timeout", str(DEPLOY_TIMEOUT_S), DEPLOY, "start", agent])
        except OSError as exc:
            log(f"{agent} could not be deployed: {exc}")
    return None


def end_the_others(grace_s=STOP_GRACE_S):
    """Ask every other process of the container to end, and wait for them a little."""
    try:
        os.kill(-1, signal.SIGTERM)  # everything but pid 1 and this process
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    keep = {1, os.getpid()}
    while time.monotonic() < deadline:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        if not [name for name in os.listdir("/proc") if name.isdigit() and int(name) not in keep]:
            return
        time.sleep(0.1)


def main():
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
    user = pwd.getpwnam(USER)
    try:
        if fill_home(user.pw_dir, "/etc/skel", user.pw_uid, user.pw_gid):
            log("filled the new home folder")
        install_provisioning(PROVISION, user.pw_dir, user.pw_uid, user.pw_gid)
    except OSError as exc:
        log(f"the provisioning could not be installed: {exc}")
    try:
        set_password()   # before sshd starts
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"the Shell password could not be set: {exc}")
    services = {"sshd": start_sshd, "the files server": lambda: start_files_server(user), "code-server": lambda: start_editor(user)}
    running, tried = {}, {}
    # The recipe first, then what the services file lists, which may need what it installed.
    # Neither holds up sshd, the files server, code-server or a stop.
    setup, listed = None, None
    try:
        if setup_is_owed(setup_state()):
            setup = start_setup(user)
    except OSError as exc:
        log(f"the setup recipe could not be run: {exc}")
    owed, deploying = provisioned_agents(), None
    log("up")
    while not stopping:
        keep_running(services, running, tried)
        deploying = next_deploy(user, deploying, owed)
        setup = setup_ended(setup)
        if setup is None and listed is None:
            listed = start_listed_services(user)
        if listed:
            listed = [process for process in listed if process.poll() is None]  # collect the ones that ended
        time.sleep(0.5)  # a signal that lands between the check and a pause() would be lost
    log("stopping")
    setup_ended(setup)  # one that ended just before the stop is not run again for nothing
    end_the_others()


if __name__ == "__main__":
    main()
