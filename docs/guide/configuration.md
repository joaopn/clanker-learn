---
title: Configuration
parent: User guide
nav_order: 8
---
# Configuration

clanker-learn is configured in two places: the **Settings** page, for everything you change while it runs, and `.env` in your copy of the repository, for how the container itself runs, which `./setup` writes. Nothing needs configuring before the first start beyond running `./setup` once.

## The Settings page

**Settings** is at the bottom of the sidebar. Its sections, top to bottom:

| Section | What it holds |
|---|---|
| **Interface** | How the page looks in this browser (below). |
| **Software** | Claude Code, pi and the editor: the versions fetched and in effect, **Pull**, **Refresh**, **Use repo version**. See [Agents and software](agents.md#the-software-section). |
| **Tutor** | The Claude token, the default agent, and the tutor's instructions (below). |
| **Backup** | Make, list, download and remove backups. See [Backups and restore](backups.md). |
| **How it runs** | Sysbox or privileged, and the page's certificate (below). |

### Interface

| Setting | What it sets |
|---|---|
| **Interface theme** | The page's colours, from a list of light and dark palettes (Nord by default). |
| **Terminal theme** | The Shell's colours: **Same as interface theme**, or a palette of its own. |
| **Editor zoom** | The zoom of the Editor and every web-app tab (100% by default). |
| **CLI width** | The widest the Shell gets (1000 px by default, or full width). |
| **CLI font size** | The Shell's font size (16 px by default). |

They apply at once and are kept by this browser only, like the sidebar's pin and width.

### Tutor

- **Claude token**: the token every project's tutor signs in to Claude with, when it is set. Make it with `claude setup-token` on a machine where you are logged in to Claude. It is not shown again once set; **Replace** or **Remove** it. Without one, each project's tutor uses that project's own Claude Code login. See [Logging in](agents.md#logging-in).
- **The default agent**: Claude Code or pi, for lessons that have not started and have not chosen one. See [The default agent](agents.md#the-default-agent).
- **The tutor's instructions**: the text every tutor is given with each of your messages, in every project. Read it, change it and save it; a lesson under way follows the new text from its next message, starting again from its files. **Back to the shipped text** puts clanker-learn's own text back.

### How it runs

Says which way clanker-learn was started: **Sysbox** or **Privileged mode**, and what that means. In privileged mode, a quiet line, "privileged mode", at the bottom of the sidebar leads here. Under it, a line names the page's certificate (clanker-learn's own, or one from Tailscale) and when it runs out; from 30 days before, it says the certificate is due for renewal.

## The setup command

`./setup`, in the root of your copy of the repository, is the one command for the settings outside the page.

| To | Run |
|---|---|
| Write `.env` (an existing one is kept) | `./setup` |
| Give the page a trusted certificate from Tailscale | `./setup cert-tailscale` |
| The same, from a script (no questions) | `./setup cert-tailscale --yes` |
| Go back to clanker-learn's own self-signed certificate | `./setup cert-self-signed` |
| Show its usage | `./setup --help` |

`./setup` alone writes `.env` from `.env.example` when there is none, choosing the mode by what Docker has (see [Getting started](getting-started.md#set-it-up)); when `.env` exists it says which mode it holds and warns if Docker cannot run that mode. The two certificate commands need clanker-learn running.

## Sysbox or privileged

The container runs a Docker daemon of its own, for the sandboxes. Docker allows that in two ways, and `CL_MODE` in `.env` says which; `./setup` chooses for you.

**Sysbox** (`CL_MODE=sysbox`, the default) needs [sysbox](https://github.com/nestybox/sysbox) installed on the machine; the container then runs on its runtime, `sysbox-runc`. It runs in a user namespace of its own: root inside it is not root on the machine, so something that gets out of a sandbox is still confined. It is the safer way. It has been started on a real machine, but nothing beyond a first start has been run on it. One thing is not known there: whether a restart of clanker-learn keeps what was installed in each sandbox outside its workspace and home folder. If it does not, recreating a project brings that back through its [setup recipe](projects.md#what-a-sandbox-brings-back-by-itself).

**Privileged** (`CL_MODE=privileged`) is the fallback for a machine without sysbox, and the way that has been tested. It works with Docker alone. What it gives up: a privileged container is root-equivalent on the machine. Anything that gets out of a sandbox into clanker-learn's container can take the machine. What stands between a sandbox and the machine is an ordinary container boundary (the sandbox's own) and clanker-learn's guard rules (see [Security](security.md)).

**Settings → How it runs** says which of the two it runs in.

`./setup` writes the mode only when it creates `.env`. There is no command yet to change the mode of an existing install: see [the gap below](#what-cannot-be-changed-by-a-command-yet).

## A trusted certificate

The page's certificate is clanker-learn's own, made at the first start and signed by nobody else. The editor's previews (Markdown, notebooks and other webviews) run a service worker, and browsers refuse service workers under a certificate they do not trust, even after you accepted the warning: the editor says `Could not register service worker: SecurityError`. If the machine is in a [Tailscale](https://tailscale.com) tailnet, Tailscale gives it a trusted certificate (from Let's Encrypt) for its tailnet name, `<host>.<tailnet>.ts.net`.

What it needs:

- Tailscale running on this machine, which is in the tailnet.
- HTTPS turned on for the tailnet, in the admin console's DNS settings (https://login.tailscale.com/admin/dns).
- `tailscale cert` allowed for you: run `./setup cert-tailscale` with sudo, or make yourself tailscaled's operator once with `sudo tailscale up --operator=$USER`.

Then, with clanker-learn running:

```
./setup cert-tailscale
```

It asks Tailscale for the certificate, gives it to the web server (with a marker that keeps the web server from replacing it with a self-signed one), restarts the web server and prints the address to open: `https://<host>.<tailnet>.ts.net:7788/`. Open the page by that name, not by the address: the certificate names the tailnet name only, and the tabs follow the name the page was opened by.

The tailnet name reaches the page only when the page is published on this machine's tailnet address. When it is published on loopback (the default), the command says so and asks `Publish clanker-learn on <tailnet address>? [Y/n]`. On yes it sets `CL_BIND` to that address in `.env` (creating `.env` first, as `./setup` does, when there is none) and makes the container again with `docker compose up -d`: running sandboxes and lessons are stopped and started again, projects and data stay on the volumes. On no, nothing changes, and the name does not reach the page. Run without a terminal (a script, a pipe), it asks nothing and changes nothing unless it is given `--yes`.

The certificate is good for 90 days and is not renewed by itself: run `./setup cert-tailscale` again before it runs out. From 30 days before, the web server's log says it is due at every start, and so does **Settings → How it runs**. To go back to the self-signed certificate, run `./setup cert-self-signed`: it removes the marker and restarts the web server, which makes its own certificate again.

## The settings in .env

`.env` is this machine's settings, in your copy of the repository. Git ignores it, so `git pull` leaves it alone. `./setup` writes it from `.env.example`, which lists every setting with its default; `compose.yaml` holds the defaults, which apply to every setting `.env` does not set.

| Setting | Default | What it does |
|---|---|---|
| `CL_MODE` | `sysbox` | `sysbox` or `privileged`. `./setup` sets it from what Docker has. |
| `CL_BIND` | `127.0.0.1` | The address every port is published on. The default is the machine's loopback, so the page can be reached from this machine and no other. One specific address of this machine (its LAN or tailnet address) makes it reachable from others; `./setup cert-tailscale` sets it to the tailnet address. |
| `CL_WEB` | `on` | `off` means no page: no web server, no tutor service, no set-up code. Projects and the `cl` command work all the same. |
| `CL_WEB_PORT` | `7788` | The page's port (HTTPS). |
| `CL_ORIGIN_PORTS` | `11100-11164` | The web-app tabs' ports, one per tab (65 at once). |
| `CL_PASSTHROUGH_PORTS` | `11200-11299` | The raw TCP ports you name by hand in a project. No login is in front of them. |
| `CL_STATUS_PORT` | `8080` | The status answer (`state.json`): how the start is going. |
| `CL_DATA_VOLUME`, `CL_DOCKER_VOLUME` | `clanker-learn-data`, `clanker-learn-docker` | The names of the two volumes. |

Each port is published on the same number outside the container as inside, from the one setting: the page builds the tabs' addresses from these numbers. The ports are numbers from 1024 to 65535, and none may overlap another. A wrong web setting leaves the rest working: `state.json` says `web: down` with the reason.

Besides these, `compose.yaml` gives the container its two volumes, a minute to stop the sandboxes cleanly, the restart policy `unless-stopped`, and control groups, processes, network and init of its own. Leave those as they are: without them clanker-learn refuses to start (see [When it refuses to start](troubleshooting.md)).

### What cannot be changed by a command yet

Every setting is meant to be changed through `./setup` or the page, never by editing a file. Today that holds only in part: `./setup` writes `.env` once, and afterwards changes only `CL_BIND`, through `./setup cert-tailscale`. There is no command yet, and no place on the page, to change the mode, the address (other than the tailnet one), the ports, `CL_WEB` or the volume names of an existing install. This is a known gap.

What exists today:

- Removing `.env` and running `./setup` again writes a fresh one, with the mode Docker supports now and every other setting at its default. Then `docker compose up -d` makes the container again with it, on the same volumes.
- A value given in the shell's environment wins over `.env` for that one command (`CL_WEB=off docker compose up -d`). It has to be given again at every later `docker compose up`, or the container is made again with `.env`'s value.

How it works: [System overview](../architecture/system.md)
