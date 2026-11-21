#!/usr/bin/env python3
"""The init of a sandbox, run by tini as root at every start of the container.

  1. An empty home folder is filled from the system's skeleton (the home is a folder of
     clanker-learn's data volume, empty when the project is new).
  2. What the provisioning folder holds is installed: the tutor's public key as the sandbox
     user's authorized_keys, and the sshd settings. The core rewrites that folder before every
     start and mounts it read-only, so a project made by an earlier version gains what a later
     one adds without being recreated.
  3. sshd is started: the tutor's way in. Its host key is the one in the provisioning folder.
  4. The files server is started as the sandbox user.

Then it waits, and starts sshd or the files server again if one of them ends. On a stop it
asks every other process to end, gives them a few seconds, and exits; what is left is ended
with the container.
"""
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time

USER = "learner"
PROVISION = "/etc/cl-provision"
SSHD_CONFIG = "/etc/ssh/sshd_config.d/cl.conf"
FILES_SERVER = "/opt/cl/files_server.py"
STOP_GRACE_S = 5
RESTART_PAUSE_S = 2


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


def start_sshd():
    os.makedirs("/run/sshd", mode=0o755, exist_ok=True)
    return subprocess.Popen(["/usr/sbin/sshd", "-D", "-e"])


def start_files_server(user):
    env = {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name, "PATH": "/usr/local/bin:/usr/bin:/bin"}
    return subprocess.Popen(["python3", FILES_SERVER], user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                            cwd="/", env=env, stdin=subprocess.DEVNULL)


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
    services = {"sshd": start_sshd, "the files server": lambda: start_files_server(user)}
    running, tried = {}, {}
    log("up")
    while not stopping:
        keep_running(services, running, tried)
        time.sleep(0.5)  # a signal that lands between the check and a pause() would be lost
    log("stopping")
    end_the_others()


if __name__ == "__main__":
    main()
