# Installing and running clanker-learn

clanker-learn runs as one Docker container on a Linux machine. Inside it are a Docker daemon of its own, which runs a sandbox for each of your projects, and the services that make the page: a broker, a web server and the tutor service. Nothing is installed on the machine itself but the image and two volumes.

What you need:

- A Linux machine with Docker. Only x86-64 has been run so far; arm64 has not.
- A Claude subscription, for Claude Code.
- The internet, for the first start and while you work.
- A few gigabytes of disk: each project has its own copy of Claude Code (about 400 MB) and of the editor (about 350 MB), besides what you install in it.

## Two ways to run it

The container needs to run a Docker daemon of its own. Docker allows that in two ways, and you say which one with `CL_MODE`.

**Privileged** (`--privileged -e CL_MODE=privileged`) is the way that has been tested. It works with Docker alone. What it gives up: a privileged container is root-equivalent on the machine. Anything that gets out of a sandbox into clanker-learn's container can take the machine. What stands between a sandbox and the machine is an ordinary container boundary (the sandbox's own) and clanker-learn's guard rules (see [SECURITY.md](SECURITY.md)).

**Sysbox** (`--runtime=sysbox-runc -e CL_MODE=sysbox`) needs [sysbox](https://github.com/nestybox/sysbox) installed on the machine. The container then runs in a user namespace of its own: root inside it is not root on the machine, so something that gets out of a sandbox is still confined. It is supported by the code, and is the safer way, but it has not yet been run on a real machine. One thing is not known there: whether a restart of clanker-learn keeps what was installed in each sandbox outside its workspace and home folder. If it does not, recreating a project brings that back through its setup recipe (see [USING.md](USING.md)).

## Build the image

From a copy of this repository:

```
git clone <the repository's address> clanker-learn
cd clanker-learn
docker build -t clanker-learn -f outer/Dockerfile .
```

The build takes a few minutes the first time and needs the internet.

## Start it

Privileged:

```
docker run -d --name clanker-learn --stop-timeout 60 --privileged -e CL_MODE=privileged \
    -p 127.0.0.1:7788:7788 -p 127.0.0.1:8080:8080 \
    -p 127.0.0.1:11100-11164:11100-11164 -p 127.0.0.1:11200-11299:11200-11299 \
    -v clanker-learn-docker:/var/lib/docker -v clanker-learn-data:/data \
    clanker-learn
```

On sysbox:

```
docker run -d --name clanker-learn --stop-timeout 60 --runtime=sysbox-runc -e CL_MODE=sysbox \
    -p 127.0.0.1:7788:7788 -p 127.0.0.1:8080:8080 \
    -p 127.0.0.1:11100-11164:11100-11164 -p 127.0.0.1:11200-11299:11200-11299 \
    -v clanker-learn-docker:/var/lib/docker -v clanker-learn-data:/data \
    clanker-learn
```

What the command gives it:

- `-v clanker-learn-data:/data` keeps everything that is yours: projects, conversations, the password, settings. `-v clanker-learn-docker:/var/lib/docker` keeps the inner Docker's images and the sandboxes' containers. Both are named volumes, so they outlive the container.
- `7788` is the page. `8080` is a small status answer (`state.json`) that says how the start is going. `11100-11164` are the ports the web-app tabs are served on, one port per tab. `11200-11299` are for raw TCP ports you name by hand in a project; these have no login in front of them.
- Every port is published on the machine's loopback address only (`127.0.0.1`), so the page can be reached from that machine and no other.
- `--stop-timeout 60` gives clanker-learn time to stop the sandboxes cleanly when the container is stopped.

Leave out `--pid=host`, `--network=host`, `--cgroupns=host`, `--init`, another entrypoint and `--network=container:…`: with them clanker-learn would act on the machine's own processes, network or control groups, so it refuses to start (see "When it refuses to start", below).

The container does not start by itself when the machine boots. Start it again with `docker start clanker-learn`; the projects that were running come back by themselves.

## The first start

The first start builds and fetches what clanker-learn needs, once:

1. The container applies its guard rules, starts its Docker daemon and runs a self-test. This takes seconds.
2. The web server's image is built, then the page answers. Then the tutor service's image is built.
3. The sandbox image is built.
4. Claude Code and the editor (code-server) are fetched, each in a throwaway container, and kept on the data volume.

On a test machine with a fast connection all of it took about five minutes; the page answered after half a minute. A project cannot be given Claude Code or the editor before they are fetched.

The status answer shows each step:

```
curl -s http://127.0.0.1:8080/state.json
```

`state` is `ready` once the container is up, and `broker` is `up` once the broker answers (if it ends, clanker-learn starts it again). `web` says `preparing: …`, then `up`; `tutor` the same. `sandbox_image` says `preparing: …`, then `ready`. `agents` (Claude Code) and `editor` say `pulling`, then `ready: <version>`. A step that failed says `down: <why>` or `failed: <why>`. The log says the same as it happens:

```
docker logs clanker-learn
```

## The first password

Open `https://127.0.0.1:7788` in a browser on the same machine. The certificate is clanker-learn's own, made at the first start and signed by nobody else, so the browser warns the first time: accept it for that address.

While no password is set, the page asks for a set-up code and a new password of at least 8 characters. The code is in the container's log:

```
docker logs clanker-learn
```

Look for the line `No password is set yet. The set-up code is …`. The code is new at every start and is good until the container stops or a password is set. Ten wrong codes within a minute shut it for a minute.

Or set the password from the machine, without the code:

```
docker exec -it clanker-learn cl passwd
```

It asks for the password twice, without showing it. To give it on standard input instead (for a script):

```
printf '%s' 'the new password' | docker exec -i clanker-learn cl passwd
```

`cl passwd` also changes a password that is set, and ends every session: log in again. What is stored is a hash, on the data volume. There is one password and no user accounts: clanker-learn is for one learner.

## Settings

Settings are given to the run command with `-e NAME=value`.

- `CL_MODE`: `privileged` or `sysbox`. It has no default.
- `CL_WEB`: `on` (the default) or `off`. Off, there is no page: no web server runs, no tutor service, no set-up code. Projects and the `cl` command work all the same.
- `CL_WEB_PORT`: the page's port, `7788` by default.
- `CL_ORIGIN_PORTS`: the web-app tabs' ports, `11100-11164` by default (65 tabs at once).
- `CL_PASSTHROUGH_PORTS`: the raw TCP ports, `11200-11299` by default.
- `CL_STATUS_PORT`: the status answer's port, `8080` by default.

Publish each port on the same number outside as inside: the page builds the tabs' addresses from these numbers. For example, the page on 8443:

```
-e CL_WEB_PORT=8443 -p 127.0.0.1:8443:8443
```

The ports are numbers from 1024 to 65535, and none may overlap another. A wrong web setting leaves the rest working: `state.json` says `web: down` with the reason. A wrong `CL_STATUS_PORT` refuses the start; the status answer is then on 8080 and says why.

## Where everything is kept

In the two volumes, and nowhere else.

`clanker-learn-data`, mounted at `/data`:

- `projects/<name>`: each project's workspace (`/workspace` in its sandbox).
- `homes/<name>`: each project's home folder (`/home/learner`), with its copies of Claude Code and the editor, its Claude Code login and sessions.
- `core/`: each project's record, keys and ports; the secret the web server and the tutor service share.
- `tutor/`: the tutor service's database (lessons, conversations, plans, what the tutor keeps about you), the Claude token set in Settings, and the keys the tutor logs in to each sandbox with.
- `broker/`: the password's hash and a log of logins and changes.
- `web/`: the page's certificate and which tab is on which port.
- `dist/`: the fetched Claude Code and editor.
- `versions.local.env`: the versions you raised in Settings.
- `backups/`: the backups.

`clanker-learn-docker`, mounted at `/var/lib/docker`: the inner Docker's images and each project's container, which holds what was installed in the sandbox outside its workspace and home folder.

## The versions of Claude Code and the editor

clanker-learn names the versions it fetches in `versions.env` in the repository. At the time of writing: Claude Code 2.1.330 and code-server 4.126.0.

To move to newer ones, use Settings, Software, in the page: **Refresh** asks for the newest version and offers to raise it and fetch it. **Pull** fetches again at the version in effect. **Use repo version** goes back to the repository's. A version is never moved down by Refresh. The same from the machine:

```
docker exec clanker-learn cl software
docker exec clanker-learn cl agent refresh --check
docker exec clanker-learn cl agent refresh
docker exec clanker-learn cl editor refresh
```

A raised version is kept in `/data/versions.local.env`, over the repository's file. Running projects keep the Claude Code they have until Update is pressed in their gear's Agents section; new projects get the fetched one. A newer editor reaches a project at its next start. Claude Code may also update itself inside a project.

## Backing up and restoring

A backup is one archive of everything clanker-learn keeps, made inside the container:

```
docker exec clanker-learn cl backup
docker exec clanker-learn cl backups
```

`cl backup` writes `/data/backups/cl-backup-<date>T<time>Z.tar.gz` and says its size and how long it took. `cl backups` lists them, newest first. Most of a backup is the copies of Claude Code and the editor in each project's home folder: on a test machine, two new projects made a backup of 1.1 GB in under half a minute. A backup can be made while projects run, but files being written at that moment may be torn: the command says which projects were running. Stop them first for a clean copy.

What is inside: every project's workspace, home folder, record, keys and ports; the tutor's database (copied so that a running tutor does not tear it), the Claude token and the tutor's keys; the password's hash; the page's certificate; the versions you raised.

What is not inside: the fetched Claude Code and editor (a new installation fetches them at its first start); what was installed in a sandbox outside its workspace and home folder (each project's setup recipe brings it back); the inner Docker's images (built again); the other backups.

**Keep a backup as you would keep the keys to everything.** It holds the password's hash, the Claude token if you set one, each project's Claude Code login, and the keys into every sandbox. It is not encrypted.

To keep one off the machine, copy it out:

```
docker cp clanker-learn:/data/backups/cl-backup-<date>T<time>Z.tar.gz .
```

To remove one:

```
docker exec clanker-learn cl backup-rm cl-backup-<date>T<time>Z.tar.gz
```

To restore one, stop every project first (in the page, or with `cl stop <name>`), then:

```
docker exec clanker-learn cl restore cl-backup-<date>T<time>Z.tar.gz
```

A restore **replaces everything** clanker-learn keeps with what the backup holds: projects made since the backup are gone, and the password is the backup's. Before it changes anything it makes a backup of what is there, named in its answer, so a restore can be undone by restoring that one. It refuses while a project runs, while the tutor is writing an answer, and while a backup, a fetch of the software or an operation on a project is under way. It refuses a backup made by a newer clanker-learn. Afterwards, log in again with the backup's password and start your projects: each one's container is made again, and its setup recipe runs once.

To restore onto a new installation, start it and wait for its first start to finish, then copy the backup in and restore it. A new installation has no folder of backups yet, so make it first:

```
docker exec clanker-learn mkdir -p -m 700 /data/backups
docker cp cl-backup-<date>T<time>Z.tar.gz clanker-learn:/data/backups/
docker exec clanker-learn cl restore cl-backup-<date>T<time>Z.tar.gz
```

There are no scheduled backups. Restores are made with this command only; the page cannot make or restore one yet.

## Updating clanker-learn

An update is a new image on the same volumes. Make a backup first. Then, in your copy of the repository:

```
git pull
docker build -t clanker-learn -f outer/Dockerfile .
docker stop clanker-learn
docker rm clanker-learn
```

and run the same `docker run` command as before. Your projects, conversations, password and settings are on the volumes and stay.

At the first start after an update, the images of the web server, the tutor service and the sandboxes are built again if their definition changed, and the web server's and the tutor service's containers are made again. The tutor's database is brought up to date by the tutor service. The fetched Claude Code and editor stay; if the new version of the repository names newer versions, Settings, Software, says so, and Pull fetches them. Existing projects keep their containers until you recreate them: Recreate gives a project a container from the new sandbox image, keeps its workspace and home folder, and runs its setup recipe.

## Stopping and uninstalling

```
docker stop clanker-learn
docker start clanker-learn
```

Stopping stops every sandbox first. Starting brings back the projects that were running.

To uninstall, copy out any backup you want to keep, then remove the container, the two volumes and the image:

```
docker rm -f clanker-learn
docker volume rm clanker-learn-docker clanker-learn-data
docker image rm clanker-learn
```

Removing the volumes removes every project, conversation and backup for good.

## When it refuses to start

At every start clanker-learn checks that it has a container of its own, applies its guard rules, starts its Docker daemon, checks the rules again and runs a self-test container. If any of that fails, it ends everything it started, so that nothing runs without the rules, and stays in the state `refused` with the reason. The reason is in the log and in the status answer:

```
docker logs clanker-learn
curl -s http://127.0.0.1:8080/
```

The refusals and what to do:

- **`CL_MODE is not set`** (or is something else): add `-e CL_MODE=privileged` with `--privileged`, or `-e CL_MODE=sysbox` with `--runtime=sysbox-runc`.
- **`This container runs neither on sysbox nor privileged`**: the container cannot run a Docker daemon. Add `--privileged`, or `--runtime=sysbox-runc`.
- **`CL_MODE=sysbox was declared, but this container is not in a user namespace`**: the run command said sysbox but did not use it. Add `--runtime=sysbox-runc`, or run privileged.
- **`This container does not have its own process namespace and init`**: it was started with `--pid=host`, `--init` or another entrypoint. Leave them out. Nothing was started.
- **`This container's network already holds a Docker daemon's networks`**: it was started with `--network=host`, or on the network of another container. Leave that out. Nothing was started and the status port was not opened (it would have been someone else's), so only the log says why. This is told by the machine's Docker bridges (`docker0`, `br-…`): on a machine whose Docker has none, `--network=host` goes unnoticed.
- **`This container does not have its own control groups`**: it was started with `--cgroupns=host`, which is also Docker's default on a machine with cgroup v1. Add `--cgroupns=private`.
- **`CL_STATUS_PORT is …`**: give a port number from 1 to 65535.
- **`The guard rules that fence the sandboxes could not be applied`** (or `are not in place after applying them`): the reason in brackets says what failed. Nothing was started.
- **`The inner Docker daemon did not start`**, **`was not ready after 90 seconds`**, **`could not run a container`**: the log holds the daemon's last lines, which say why.
- While it runs, **`The guard rules … were lost and could not be restored`** or **`The inner Docker daemon stopped`**: every sandbox was stopped. Restart clanker-learn.

A refused container stays refused until it is stopped. Once the cause is fixed, remove it and run the command again (the volumes stay):

```
docker rm -f clanker-learn
```

A wrong web setting is not a refusal: projects and the `cl` command work, and `state.json` says `web: down` with the reason. Fix the setting, remove the container and run it again.
