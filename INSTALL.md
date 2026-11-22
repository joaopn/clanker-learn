# Installing and running clanker-learn

clanker-learn runs as one Docker container on a Linux machine, started with Docker Compose from a copy of this repository. Inside it are a Docker daemon of its own, which runs a sandbox for each of your projects, and the services that make the page: a broker, a web server and the tutor service. Nothing is installed on the machine itself but the image and two volumes.

What you need:

- A Linux machine with Docker and Docker Compose. It has been run with Docker 29.5.3 and Compose 5.7.0. Only x86-64 has been run so far; arm64 has not.
- A Claude subscription, for Claude Code; or, for pi, an account with a provider pi supports, logged in inside each project that uses it.
- The internet, for the first start and while you work.
- A few gigabytes of disk: each project has its own copy of Claude Code (about 400 MB), of pi if you give it pi (about 270 MB), and of the editor (about 350 MB), besides what you install in it.

## Two ways to run it

The container needs to run a Docker daemon of its own. Docker allows that in two ways, and `CL_MODE` in `.env` says which one. `./setup` (below) chooses for you.

**Sysbox** (`CL_MODE=sysbox`, the default) needs [sysbox](https://github.com/nestybox/sysbox) installed on the machine; the container then runs on its runtime, `sysbox-runc`. It runs in a user namespace of its own: root inside it is not root on the machine, so something that gets out of a sandbox is still confined. It is the safer way, but it has not yet been run on a real machine. One thing is not known there: whether a restart of clanker-learn keeps what was installed in each sandbox outside its workspace and home folder. If it does not, recreating a project brings that back through its setup recipe (see [USING.md](USING.md)).

**Privileged** (`CL_MODE=privileged`) is the fallback for a machine without sysbox, and the way that has been tested. It works with Docker alone. What it gives up: a privileged container is root-equivalent on the machine. Anything that gets out of a sandbox into clanker-learn's container can take the machine. What stands between a sandbox and the machine is an ordinary container boundary (the sandbox's own) and clanker-learn's guard rules (see [SECURITY.md](SECURITY.md)).

The page says which of the two it runs in: Settings, under "How it runs". In privileged mode it also says what that means, and a quiet line, "privileged mode", at the bottom of the project list leads there.

To change the mode later, set `CL_MODE` in `.env` and run `docker compose up -d`: the container is made again, on the same volumes.

## Set it up

From a copy of this repository:

```
git clone <the repository's address> clanker-learn
cd clanker-learn
./setup
```

`./setup` writes `.env`, this machine's settings, from `.env.example`. It asks Docker whether it has sysbox's runtime (`sysbox-runc`) and sets `CL_MODE=sysbox` if it has, `CL_MODE=privileged` if not, and says which and why. It never overwrites an `.env` that exists: run again, it says the file is kept and which mode it holds, and warns when it holds sysbox while Docker has no sysbox runtime. `.env` is not part of the repository (git ignores it), so `git pull` leaves it alone.

## Start it

In the copy of the repository:

```
docker compose up
```

The first time, this builds the image (a few minutes, with the internet), then starts the container and shows its log, set-up code included. `Ctrl-C` stops it. To run it in the background instead:

```
docker compose up -d
```

What `compose.yaml` gives the container:

- Two named volumes: `clanker-learn-data`, mounted at `/data`, keeps everything that is yours: projects, conversations, the password, settings. `clanker-learn-docker`, mounted at `/var/lib/docker`, keeps the inner Docker's images and the sandboxes' containers. They outlive the container.
- The ports: `7788` is the page. `8080` is a small status answer (`state.json`) that says how the start is going. `11100-11164` are the ports the web-app tabs are served on, one port per tab. `11200-11299` are for raw TCP ports you name by hand in a project; these have no login in front of them.
- Every port is published on the machine's loopback address only (`127.0.0.1`), so the page can be reached from that machine and no other.
- A minute to stop (`stop_grace_period`), so that clanker-learn can stop the sandboxes cleanly.
- The restart policy `unless-stopped`: after a reboot of the machine, or a restart of Docker, it starts again by itself, unless you stopped it. The projects that were running come back by themselves.
- Control groups of its own (`cgroup: private`), and its own processes, network and init.

Do not add `pid: host`, `network_mode`, `init:` or another entrypoint to it: with them clanker-learn would act on the machine's own processes or network, so it refuses to start (see "When it refuses to start", below).

The container is called `clanker-learn`, so every `docker exec clanker-learn …` below works from anywhere on the machine. The `docker compose …` commands are run in the copy of the repository.

## The first start

The first start builds and fetches what clanker-learn needs, once:

1. The container applies its guard rules, starts its Docker daemon and runs a self-test. This takes seconds.
2. The web server's image is built, then the page answers. Then the tutor service's image is built.
3. The sandbox image is built.
4. Claude Code and the editor (code-server) are fetched, each in a throwaway container, and kept on the data volume.

On a test machine with a fast connection all of it took about five minutes; the page answered after half a minute. A project cannot be given Claude Code or the editor before they are fetched.

pi is not fetched at the first start: it is fetched when you ask, with Pull on its row in Settings, Software (or `cl agent pull pi`), and until then the New project box does not offer it.

The status answer shows each step:

```
curl -s http://127.0.0.1:8080/state.json
```

`state` is `ready` once the container is up, and `broker` is `up` once the broker answers (if it ends, clanker-learn starts it again). `web` says `preparing: …`, then `up`; `tutor` the same. `sandbox_image` says `preparing: …`, then `ready`. `agents` (Claude Code) and `editor` say `pulling`, then `ready: <version>`. A step that failed says `down: <why>` or `failed: <why>`. The log says the same as it happens:

```
docker compose logs
```

(or `docker logs clanker-learn` from anywhere; add `-f` to either to follow it).

## The first password

Open `https://127.0.0.1:7788` in a browser on the same machine. The certificate is clanker-learn's own, made at the first start and signed by nobody else, so the browser warns the first time: accept it for that address.

While no password is set, the page asks for a set-up code and a new password of at least 8 characters. The code is in the container's log:

```
docker compose logs
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

The settings are in `.env`, in the copy of the repository; `compose.yaml` holds their defaults and `.env.example` lists them, each with its default. Change one in `.env` and run `docker compose up -d`: the container is made again with it, on the same volumes. A setting given in the shell's environment (`CL_WEB=off docker compose up -d`) wins over `.env`.

- `CL_MODE`: `sysbox` (the default) or `privileged`; `./setup` sets it.
- `CL_BIND`: the address every port is published on, `127.0.0.1` by default. To reach the page from another machine, give one specific address of this one (its LAN or tailnet address).
- `CL_WEB`: `on` (the default) or `off`. Off, there is no page: no web server runs, no tutor service, no set-up code. Projects and the `cl` command work all the same.
- `CL_WEB_PORT`: the page's port, `7788` by default.
- `CL_ORIGIN_PORTS`: the web-app tabs' ports, `11100-11164` by default (65 tabs at once).
- `CL_PASSTHROUGH_PORTS`: the raw TCP ports, `11200-11299` by default.
- `CL_STATUS_PORT`: the status answer's port, `8080` by default.
- `CL_DATA_VOLUME`, `CL_DOCKER_VOLUME`: the names of the two volumes, `clanker-learn-data` and `clanker-learn-docker` by default.

Each port is published on the same number outside the container as inside, from the one setting (the page builds the tabs' addresses from these numbers). For example, the page on 8443 is the line `CL_WEB_PORT=8443` in `.env`.

The ports are numbers from 1024 to 65535, and none may overlap another. A wrong web setting leaves the rest working: `state.json` says `web: down` with the reason. A `CL_STATUS_PORT` that is not a port number stops `docker compose up` before the container exists (`invalid containerPort: …`).

## Where everything is kept

In the two volumes, and nowhere else.

`clanker-learn-data`, mounted at `/data`:

- `projects/<name>`: each project's workspace (`/workspace` in its sandbox).
- `homes/<name>`: each project's home folder (`/home/learner`), with its copies of Claude Code, pi and the editor, their logins and sessions.
- `core/`: each project's record, keys and ports; the secret the web server and the tutor service share.
- `tutor/`: the tutor service's database (lessons, conversations, plans, what the tutor keeps about you), the Claude token set in Settings, and the keys the tutor logs in to each sandbox with.
- `broker/`: the password's hash and a log of logins and changes.
- `web/`: the page's certificate and which tab is on which port.
- `dist/`: the fetched Claude Code, pi and editor.
- `versions.local.env`: the versions you raised in Settings.
- `backups/`: the backups. It is made at the first start, empty.

`clanker-learn-docker`, mounted at `/var/lib/docker`: the inner Docker's images and each project's container, which holds what was installed in the sandbox outside its workspace and home folder.

## The versions of Claude Code, pi and the editor

clanker-learn names the versions it fetches in `versions.env` in the repository. At the time of writing: Claude Code 2.1.330, pi 1.5.0 and code-server 4.126.0.

pi is fetched with four more parts, each pinned there too: a node runtime of its own (`NODE_VERSION`, 24.18.0), the pi-subagents package (`PI_SUBAGENTS_VERSION`, 0.84.0), the rules extension that makes pi read the same instruction files as Claude Code (`RS_PI_RULES_VERSION`, 0.8.0, shipped in the repository, from research-sandbox), and a counter of the folder's layout (`RS_PI_DIST_LAYOUT`). Settings, Software, shows each under pi's row; when one moved since the fetch, the row says the fetched copy is stale and Pull fetches it again. Refresh raises pi's own version only.

To move to newer ones, use Settings, Software, in the page: **Refresh** asks for the newest version and offers to raise it and fetch it. **Pull** fetches again at the version in effect. **Use repo version** goes back to the repository's. A version is never moved down by Refresh. The same from the machine:

```
docker exec clanker-learn cl software
docker exec clanker-learn cl agent refresh --check
docker exec clanker-learn cl agent refresh
docker exec clanker-learn cl agent pull pi
docker exec clanker-learn cl agent refresh pi --check
docker exec clanker-learn cl editor refresh
```

A raised version is kept in `/data/versions.local.env`, over the repository's file. Running projects keep the agent versions they have until Update is pressed in their gear's Agents section; new projects get the fetched ones. A newer editor reaches a project at its next start. Claude Code may also update itself inside a project.

## Backing up and restoring

A backup is one archive of everything clanker-learn keeps. Make one in the page, in Settings, Backup: **Make a backup** shows its log while it runs, and each backup is listed with its size, when it was made and its projects, with **Download** and **Remove** (both ask for your password again). Or from the machine:

```
docker exec clanker-learn cl backup
docker exec clanker-learn cl backups
```

`cl backup` writes `/data/backups/cl-backup-<date>T<time>Z.tar.gz` and says its size and how long it took. `cl backups` lists them, newest first. Most of a backup is the copies of Claude Code and the editor in each project's home folder: on a test machine, two new projects made a backup of 1.1 GB in under half a minute. A backup can be made while projects run, but files being written at that moment may be torn: the command, and the backup's row in Settings, say which projects were running. Stop them first for a clean copy. A backup needs room on the data volume: up to the size of what it holds, before compression. Without that much free it is refused, saying how much is free and how much it needs, before anything is written.

What is inside: every project's workspace, home folder, record, keys and ports; the tutor's database (copied so that a running tutor does not tear it), the Claude token and the tutor's keys; the password's hash; the page's certificate; the versions you raised.

What is not inside: the fetched Claude Code, pi and editor (a new installation fetches Claude Code and the editor at its first start, and pi when you pull it); what was installed in a sandbox outside its workspace and home folder (each project's setup recipe brings it back); the inner Docker's images (built again); the other backups.

**Keep a backup as you would keep the keys to everything.** It holds the password's hash, the Claude token if you set one, each project's Claude Code and pi logins, and the keys into every sandbox. It is not encrypted.

To keep one off the machine, press **Download** beside it in Settings, Backup, and type your password again, or copy it out:

```
docker cp clanker-learn:/data/backups/cl-backup-<date>T<time>Z.tar.gz .
```

To remove one, press **Remove** beside it, or:

```
docker exec clanker-learn cl backup-rm cl-backup-<date>T<time>Z.tar.gz
```

To restore one, stop every project first (in the page, or with `cl stop <name>`), then:

```
docker exec clanker-learn cl restore cl-backup-<date>T<time>Z.tar.gz
```

A restore **replaces everything** clanker-learn keeps with what the backup holds: projects made since the backup are gone, and the password is the backup's. Before it changes anything it makes a backup of what is there, named in its answer, so a restore can be undone by restoring that one. It refuses while a project runs, while the tutor is writing an answer, and while a backup, a fetch of the software or an operation on a project is under way. It refuses a backup made by a newer clanker-learn. It needs room on the data volume for the archive unpacked and for the backup of what is there, and without it refuses, saying how much is free and how much it needs, before anything is changed (it reads the archive through once to know its unpacked size). Afterwards, log in again with the backup's password and start your projects: each one's container is made again, and its setup recipe runs once.

To restore onto a new installation, start it and wait for its first start to finish, then copy the backup in and restore it:

```
docker cp cl-backup-<date>T<time>Z.tar.gz clanker-learn:/data/backups/
docker exec clanker-learn cl restore cl-backup-<date>T<time>Z.tar.gz
```

There are no scheduled backups. A restore is made with this command only, never in the page, because it replaces everything.

## Updating clanker-learn

An update is a new image on the same volumes. Make a backup first. Then, in your copy of the repository:

```
git pull
docker compose up -d
```

`docker compose up` builds the image again and, when it changed, makes the container again from it. Your projects, conversations, password and settings are on the volumes and in `.env`, and stay. The same goes for a change you make to the code yourself: `docker compose up -d` rebuilds and makes the container again; with nothing changed, the build is cached and the running container is left alone.

At the first start after an update, the images of the web server, the tutor service and the sandboxes are built again if their definition changed, and the web server's and the tutor service's containers are made again. The tutor's database is brought up to date by the tutor service. The fetched Claude Code, pi and editor stay; if the new version of the repository names newer versions, Settings, Software, says so, and Pull fetches them. Existing projects keep their containers until you recreate them: Recreate gives a project a container from the new sandbox image, keeps its workspace and home folder, and runs its setup recipe. Something a newer sandbox image makes possible (pi, for a project made before it) is refused in a project made before it, with the sentence that the project must be recreated first.

A setting added to `.env.example` by an update is not added to an `.env` that exists: its default in `compose.yaml` applies until you set it.

## Moving an install made with `docker run`

An earlier version of these instructions started clanker-learn with `docker build` and `docker run`. To move such an install to Compose, remove its container (the volumes stay), then set up and start as above:

```
docker rm -f clanker-learn
./setup
docker compose up -d
```

The volumes keep their names, `clanker-learn-data` and `clanker-learn-docker`, so the new container finds every project, conversation and the password. At each `docker compose up`, Compose warns that each volume "already exists but was not created by Docker Compose"; that is expected, and it uses them all the same. If the old install ran privileged on a machine that has sysbox, `./setup` now chooses sysbox; set `CL_MODE=privileged` in `.env` to keep the old mode.

## Stopping and uninstalling

In the copy of the repository:

```
docker compose stop
docker compose start
```

Stopping stops every sandbox first. Starting brings back the projects that were running. A stopped clanker-learn stays stopped after a reboot, until you start it.

```
docker compose down
```

removes the container (and the network Compose made for it) and keeps the volumes; `docker compose up -d` makes it again, with everything that was there.

To uninstall, copy out any backup you want to keep, then remove the container, the two volumes and the image:

```
docker compose down -v --rmi local
```

**`-v` removes the volumes, and with them every project, conversation and backup, for good.**

An install moved from `docker run` (above) has volumes Compose did not make, and `-v` leaves them (`Volume … Skipped Volume was not created by Docker Compose`). Remove them by name: `docker volume rm clanker-learn-data clanker-learn-docker`.

## When it refuses to start

Two faults stop it before the container exists, and Compose says so itself:

- **`unknown or invalid runtime name: sysbox-runc`** (Docker's own words, at `docker compose up`): `.env` says `CL_MODE=sysbox` but Docker has no sysbox runtime. Install sysbox, or set `CL_MODE=privileged` in `.env`. A container that was already running is left as it was.
- **`open …/modes/<value>.yaml: no such file or directory`**: `CL_MODE` is neither `sysbox` nor `privileged`. Correct it in `.env` (or in the shell's environment, which wins over `.env`).

At every start clanker-learn checks that it has a container of its own, applies its guard rules, starts its Docker daemon, checks the rules again and runs a self-test container. If any of that fails, it ends everything it started, so that nothing runs without the rules, and stays in the state `refused` with the reason. The reason is in the log and in the status answer:

```
docker compose logs
curl -s http://127.0.0.1:8080/
```

The refusals and what to do:

- **`CL_MODE is not set`** (or is something else): the container was not started with this repository's `compose.yaml`. Set `CL_MODE=sysbox` or `CL_MODE=privileged` in `.env` (`./setup` writes it) and start it with `docker compose up -d`.
- **`This container runs neither on sysbox nor privileged`**: the container cannot run a Docker daemon; it was started without the mode files in `modes/`. Start it with `docker compose up -d`.
- **`CL_MODE=sysbox was declared, but this container is not in a user namespace`**: it said sysbox but did not run on it. Start it with `docker compose up -d`, which runs it on `sysbox-runc`, or set `CL_MODE=privileged` in `.env`.
- **`This container does not have its own process namespace and init`**: it was started with `pid: host`, `init:` or another entrypoint. Take them out of `compose.yaml`. Nothing was started.
- **`This container's network already holds a Docker daemon's networks`**: it was started with `network_mode: host`, or on the network of another container. Take that out of `compose.yaml`. Nothing was started and the status port was not opened (it would have been someone else's), so only the log says why. This is told by the machine's Docker bridges (`docker0`, `br-…`): on a machine whose Docker has none, a host network goes unnoticed.
- **`This container does not have its own control groups`**: it was started without `cgroup: private`, which `compose.yaml` sets (without it, Docker's default on a machine with cgroup v1 is the machine's). Put it back.
- **`CL_STATUS_PORT is …`**: give a port number from 1 to 65535 (Compose itself stops most wrong values first).
- **`The guard rules that fence the sandboxes could not be applied`** (or `are not in place after applying them`): the reason in brackets says what failed. Nothing was started.
- **`The inner Docker daemon did not start`**, **`was not ready after 90 seconds`**, **`could not run a container`**: the log holds the daemon's last lines, which say why.
- While it runs, **`The guard rules … were lost and could not be restored`** or **`The inner Docker daemon stopped`**: every sandbox was stopped. Restart clanker-learn with `docker compose restart`.

A refused container stays refused until it is stopped. Once the cause is fixed, make it again (the volumes stay):

```
docker compose up -d --force-recreate
```

A wrong web setting is not a refusal: projects and the `cl` command work, and `state.json` says `web: down` with the reason. Fix the setting in `.env` and run `docker compose up -d`.
