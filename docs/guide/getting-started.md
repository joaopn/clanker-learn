---
title: Getting started
parent: User guide
nav_order: 1
---
# Getting started

clanker-learn runs as one Docker container on a Linux machine, started with Docker Compose from a copy of the repository. Inside it are a Docker daemon of its own, which runs a sandbox for each of your projects, and the services that make the page: a broker, a web server and the tutor service. Nothing is installed on the machine itself but the image and two volumes.

## What it needs

- **A Linux machine with Docker and Docker Compose.**
- **A Claude subscription**, for Claude Code; or, for pi, an account with a provider pi supports, logged in inside each project that uses it. See [Agents and software](agents.md).
- **The internet**, for the first start and while you work: the sandboxes reach it.
- **A few gigabytes of disk.** Each project has its own copy of Claude Code (about 400 MB), of pi if you give it pi (about 270 MB), and of the editor (about 350 MB), besides what you install in it.
- **[Sysbox](https://github.com/nestybox/sysbox)**, if you can: the container runs its own Docker daemon, and sysbox is the safer way to allow that. Without it clanker-learn runs in privileged mode. See [Sysbox or privileged](configuration.md#sysbox-or-privileged).

## Set it up

```
git clone https://github.com/joaopn/clanker-learn.git
cd clanker-learn
./setup
```

`./setup` writes `.env`, this machine's settings, from `.env.example`. It asks Docker whether it has sysbox's runtime (`sysbox-runc`), sets `CL_MODE=sysbox` if it has and `CL_MODE=privileged` if not, and says which and why. It never overwrites an `.env` that exists: run again, it says the file is kept and which mode it holds, and warns when it holds sysbox while Docker has no sysbox runtime. Git ignores `.env`, so `git pull` leaves it alone. The rest of what `./setup` does is in [Configuration](configuration.md#the-setup-command).

## Start it

In the copy of the repository:

```
docker compose up
```

The first time, this builds the image (a few minutes, with the internet), then starts the container and shows its log, set-up code included. `Ctrl-C` stops it. To run it in the background instead, add `-d`, and read the log with `docker compose logs`.

The container is called `clanker-learn`, so every `docker exec clanker-learn …` in this guide works from anywhere on the machine. The `docker compose …` commands are run in the copy of the repository.

It restarts by itself (`unless-stopped`): after a reboot of the machine or a restart of Docker it starts again, unless you stopped it, and the projects that were running come back.

## The first start

The first start builds and fetches what clanker-learn needs, once:

1. The container applies its guard rules, starts its Docker daemon and runs a self-test. This takes seconds.
2. The web server's image is built, then the page answers. Then the tutor service's image is built.
3. The sandbox image is built.
4. Claude Code and the editor (code-server) are fetched, each in a throwaway container, and kept on the data volume.

With a fast connection it takes about five minutes; the page answers after half a minute. A project cannot be given Claude Code or the editor before they are fetched. pi is not fetched at the first start: you fetch it when you want it, with **Pull** on its row in **Settings → Software**.

To watch the steps, ask the status port:

```
curl -s http://127.0.0.1:8080/state.json
```

| Key | Says |
|---|---|
| `state` | `ready` once the container is up (`refused`, with a reason, if it would not start: see [When it refuses to start](troubleshooting.md)) |
| `broker` | `up` once the broker answers |
| `web`, `tutor` | `preparing: …`, then `up` |
| `sandbox_image` | `preparing: …`, then `ready` |
| `agents` (Claude Code), `editor` | `pulling`, then `ready: <version>` |

A step that failed says `down: <why>` or `failed: <why>`. The log says the same as it happens (`docker compose logs`, or `docker logs clanker-learn` from anywhere; add `-f` to follow it).

## The first password

Open **https://127.0.0.1:7788** in a browser on the same machine. The certificate is clanker-learn's own, made at the first start and signed by nobody else, so the browser warns the first time: accept it for that address. (To get a certificate the browser trusts, see [A trusted certificate](configuration.md#a-trusted-certificate).)

While no password is set, the page asks for a set-up code and a new password of at least 8 characters. The code is in the container's log (`docker compose logs`), on the line `No password is set yet. The set-up code is …`. It is new at every start and good until the container stops or a password is set. Ten wrong codes within a minute shut it for a minute.

Or set the password from the machine, without the code. It asks for the password twice, without showing it:

```
docker exec -it clanker-learn cl passwd
```

To give it on standard input instead (for a script):

```
printf '%s' 'the new password' | docker exec -i clanker-learn cl passwd
```

`cl passwd` also changes a password that is set, and ends every session. There is one password and no user accounts: clanker-learn is for one learner.

## Opening the page

Log in at **https://127.0.0.1:7788**. The password never leaves the browser: the page sends a proof made from it. A login lasts a working day (8 hours).

The page opens with the tab strip across the top. Its first tab, **Projects**, shows the sidebar: your projects, each with its lessons under it, and at the bottom **+ New project**, **Knowledge**, **Settings** and **Log out**. Press **+ New project** to make your first one: see [Projects and sandboxes](projects.md), then [Goals and the study plan](goals-and-plans.md).

The page is published on the machine's loopback address only, so it can be reached from that machine and no other. To reach it from another machine, see [The settings in .env](configuration.md#the-settings-in-env).

## Updating

An update is a new image on the same volumes. Make a [backup](backups.md) first. Then, in your copy of the repository:

```
git pull
docker compose up -d
```

`docker compose up` builds the image again and, when it changed, makes the container again from it. Your projects, conversations, password and settings are on the volumes and in `.env`, and stay. The same goes for a change you make to the code yourself; with nothing changed, the build is cached and the running container is left alone.

At the first start after an update:

- The images of the web server, the tutor service and the sandboxes are built again if their definition changed, and the web server's and the tutor service's containers are made again. The tutor's database is brought up to date by the tutor service.
- The fetched Claude Code, pi and editor stay. If the new version of the repository names newer versions, **Settings → Software** says so, and **Pull** fetches them.
- Existing projects keep their containers until you recreate them: **Recreate** in a project's [Config box](projects.md#the-config-box) gives it a container from the new sandbox image, keeps its workspace and home folder, and runs its setup recipe. Something a newer sandbox image makes possible is refused in a project made before it, with the sentence that the project must be recreated first.
- A setting added to `.env.example` by an update is not added to an `.env` that exists: its default in `compose.yaml` applies.

## Stopping and uninstalling

| To | Run |
|---|---|
| Stop (every sandbox is stopped first) | `docker compose stop` |
| Start again (the projects that were running come back) | `docker compose start` |
| Remove the container, keep the volumes | `docker compose down` |
| Make it again, with everything that was there | `docker compose up -d` |
| Uninstall: remove the container, both volumes and the image | `docker compose down -v --rmi local` |

A stopped clanker-learn stays stopped after a reboot, until you start it.

**`-v` removes the volumes, and with them every project, conversation and backup, for good.** Copy out any backup you want to keep first (see [Backups and restore](backups.md)).

## Where your data is kept

In two named volumes, and nowhere else. They outlive the container.

`clanker-learn-data`, mounted at `/data`, keeps everything that is yours:

| Folder | Holds |
|---|---|
| `projects/<name>` | Each project's workspace (`/workspace` in its sandbox). |
| `homes/<name>` | Each project's home folder (`/home/learner`), with its copies of Claude Code, pi and the editor, their logins and sessions. |
| `core/` | Each project's record, keys and ports; the secret the web server and the tutor service share. |
| `tutor/` | The tutor service's database (lessons, conversations, plans, what the tutor keeps about you), the Claude token set in Settings, and the keys the tutor logs in to each sandbox with. |
| `broker/` | The password's hash and a log of logins and changes. |
| `web/` | The page's certificate and which tab is on which port. |
| `dist/` | The fetched Claude Code, pi and editor. |
| `versions.local.env` | The versions you raised in Settings. |
| `backups/` | The backups. It is made at the first start, empty. |

`clanker-learn-docker`, mounted at `/var/lib/docker`, keeps the inner Docker's images and each project's container, which holds what was installed in the sandbox outside its workspace and home folder.

## Moving an install made with `docker run`

An earlier version of these instructions started clanker-learn with `docker build` and `docker run`. To move such an install to Compose, remove its container (the volumes stay), then set up and start as above:

```
docker rm -f clanker-learn
./setup
docker compose up -d
```

The volumes keep their names, `clanker-learn-data` and `clanker-learn-docker`, so the new container finds every project, conversation and the password. At each `docker compose up`, Compose warns that each volume "already exists but was not created by Docker Compose"; that is expected, and it uses them all the same. If the old install ran privileged on a machine that has sysbox, `./setup` now chooses sysbox.

Such volumes are also left behind by `docker compose down -v` (`Volume … Skipped Volume was not created by Docker Compose`). To uninstall, remove them by name: `docker volume rm clanker-learn-data clanker-learn-docker`.

How it works: [System overview](../architecture/system.md)
