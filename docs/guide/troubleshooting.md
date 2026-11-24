---
title: When it refuses to start
parent: User guide
nav_order: 11
---
# When it refuses to start

At every start clanker-learn checks that it has a container of its own, applies its guard rules, starts its Docker daemon, checks the rules again and runs a self-test container. If any of that fails, it ends everything it started, so that nothing runs without the rules, and stays in the state `refused` with the reason. Two faults stop it even earlier, before the container exists.

## Before the container exists

Compose says these itself, at `docker compose up`:

| Compose says | Means | What to do |
|---|---|---|
| `unknown or invalid runtime name: sysbox-runc` | `.env` says `CL_MODE=sysbox`, but Docker has no sysbox runtime. A container that was already running is left as it was. | Install sysbox, then `docker compose up -d`. Or have `./setup` write a new `.env` for this machine (see below). |
| `open …/modes/<value>.yaml: no such file or directory` | `CL_MODE` is neither `sysbox` nor `privileged`. | Have `./setup` write a new `.env` (see below). If the shell's environment sets `CL_MODE`, it wins over `.env`: unset it. |
| `invalid containerPort: …` | `CL_STATUS_PORT` is not a port number. | Have `./setup` write a new `.env` (see below). |

`./setup` never changes an `.env` that exists; run again, it says which mode the file holds and warns when Docker cannot run it. To have it choose again, move the old file aside and run it: `mv .env .env.old && ./setup`. The new `.env` has the mode Docker supports now and every other setting at its default, so a `CL_BIND` set by `./setup cert-tailscale` has to be set again by running that command again. Changing a single setting of an existing `.env` by a command is not possible yet: see [Configuration](configuration.md#what-cannot-be-changed-by-a-command-yet).

## Refused at the start

The reason is in the log and in the status answer:

```
docker compose logs
curl -s http://127.0.0.1:8080/
```

| The reason says | Means | What to do |
|---|---|---|
| `CL_MODE is not set` (or is something else) | The container was not started with this repository's `compose.yaml`. | Run `./setup` if there is no `.env`, then `docker compose up -d`. |
| `This container runs neither on sysbox nor privileged` | The container cannot run a Docker daemon: it was started without the mode files in `modes/`. | Start it with `docker compose up -d`. |
| `CL_MODE=sysbox was declared, but this container is not in a user namespace` | It said sysbox but did not run on it. | Start it with `docker compose up -d`, which runs it on `sysbox-runc`. |
| `This container does not have its own process namespace and init` | It was started with `pid: host`, `init:` or another entrypoint. Nothing was started. | This repository's `compose.yaml` sets none of them: start it with that file as it is (`git diff compose.yaml` shows a local change, `git checkout compose.yaml` undoes it). |
| `This container's network already holds a Docker daemon's networks` | It was started with `network_mode: host`, or on the network of another container. Nothing was started, and the status port was not opened (it would have been someone else's), so only the log says why. | As above: start it with this repository's `compose.yaml` as it is. |
| `This container does not have its own control groups` | It was started without `cgroup: private`, which `compose.yaml` sets (without it, Docker's default on a machine with cgroup v1 is the machine's). | As above. |
| `CL_STATUS_PORT is …` | Not a port number from 1 to 65535 (Compose itself stops most wrong values first). | See [Before the container exists](#before-the-container-exists). |
| `The guard rules that fence the sandboxes could not be applied` (or `are not in place after applying them`) | The reason in brackets says what failed. Nothing was started. | Read the reason; [open an issue](https://github.com/joaopn/clanker-learn/issues) if it is not yours to fix. |
| `The inner Docker daemon did not start`, `was not ready after 90 seconds`, `could not run a container` | The log holds the daemon's last lines, which say why. | Read them in `docker compose logs`. |

The network check goes by the machine's Docker bridges (`docker0`, `br-…`): on a machine whose Docker has none, a host network goes unnoticed.

## Refused while it runs

**`The guard rules … were lost and could not be restored`** or **`The inner Docker daemon stopped`**: every sandbox was stopped. Restart clanker-learn:

```
docker compose restart
```

## Starting again after a fix

A refused container stays refused until it is stopped. Once the cause is fixed, make it again (the volumes stay):

```
docker compose up -d --force-recreate
```

## When the page does not answer

A wrong web setting is not a refusal: projects and the `cl` command work, and `state.json` says `web: down` with the reason:

```
curl -s http://127.0.0.1:8080/state.json
```

See [The settings in .env](configuration.md#the-settings-in-env) for what each setting takes.

How it works: [System overview](../architecture/system.md)
