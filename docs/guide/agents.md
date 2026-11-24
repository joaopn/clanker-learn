---
title: Agents and software
parent: User guide
nav_order: 7
---
# Agents and software

The tutor is an agent running in the project's sandbox: **Claude Code**, on your Claude subscription, or **pi**, with a provider of your choice. A project can have either or both, and a lesson can switch between them at any point (see [Switching agent](lessons.md#switching-agent)). Both agents, and the editor, are fetched once into clanker-learn and copied into each project that is given them; **Settings → Software** fetches and updates them.

## Claude Code and pi

| | Claude Code | pi |
|---|---|---|
| Fetched | At the first start. | When you press **Pull** on its row in Settings → Software. |
| Given to a new project | Ticked by default in the New project box. | Never ticked by default. |
| Its model | Model and effort, in the New project box or the project's Config box. Empty means Claude's own defaults. | Model as `provider/model` and thinking level. Empty means pi's own pick. |
| Its login | The project's own, or one token for every project in Settings → Tutor. | The project's own only. |

The tools, the cards, the plan, what the tutor knows about you and the context line are the same with either agent. To give a project an agent it does not have, or to change its model, use **Agents** in its [Config box](projects.md#the-config-box).

## Logging in

**Claude Code** needs a login. Either:

- open the project's **Shell**, run `claude` once, and log in there: that project's tutor then uses that login; or
- set a token in **Settings → Tutor**: the tutor in every project then uses it, and a new project's first lesson works without a login. Make the token with `claude setup-token` on a machine where you are logged in to Claude. Once set it is not shown again; you can replace or remove it.

**pi** has no token in Settings: its login is the project's own. Open the project's Shell, run `pi`, and log in there to your provider (`/login`), or set its key as pi says. A project whose pi has no login says so in the conversation: "pi is not logged in in this project: run pi in its Shell and log in there."

Each login lives in the project's home folder, where anything running in that sandbox can read it: see [Security](security.md#what-is-not-protected).

## The default agent

**Settings → Tutor → The default agent** is Claude Code or pi. A lesson that has not started and has not chosen an agent follows it; a lesson under way stays with the agent that began it until you choose another in its header.

## The Software section

**Settings → Software** has a row for Claude Code, one for pi and one for the editor: the version fetched (**Pulled**), the version **In effect**, and whether the two agree. pi reads "not pulled" until you press **Pull**.

| Press | What it does |
|---|---|
| **Pull** | Fetches that software again at the version in effect, and shows the log while it runs. |
| **Refresh** | Asks its maker for the newest version and, if there is a newer one, offers to raise the version and fetch it. A version is never moved down. In the first minutes of a new installation it answers that the software cannot be checked yet, while the sandbox image is being made. |
| **Use repo version** | Goes back to the version this copy of clanker-learn came with. |

Each is fetched by its maker's own installer, in a container that is thrown away afterwards; one is fetched at a time. The versions clanker-learn names are in `versions.env` in the repository (at the time of writing Claude Code 2.1.330, pi 1.5.0 and code-server 4.126.0); a version you raised is kept in `/data/versions.local.env`, over the repository's.

pi is fetched with four more parts, each pinned in `versions.env` too: a node runtime of its own, the pi-subagents package, the rules extension that makes pi read the same instruction files as Claude Code, and a counter of its folder's layout. Each has a line under pi's row; when one moved since the fetch, the row says the fetched copy is stale and **Pull** fetches it again. **Refresh** raises pi's own version only.

## Getting a new version into a project

- **Agents**: running projects keep the versions they have until you press **Update** under **Agents** in their Config box; new projects get the fetched ones. Claude Code may also update itself inside a project.
- **The editor**: a newer editor reaches a project at its next start.

## The editor

The editor is code-server, deployed into a project the same way as the agents: fetched once, copied into the project's home folder, given in the New project box. It comes with the Python and Jupyter extensions. In a project with Claude Code, Claude Code's own extension (fetched with Claude Code) is put into the editor too, which gives it Claude Code's panel. Using it is in [Shell, Editor and web apps](workspace.md#the-editor).

## From the command line

| To | Run |
|---|---|
| See what is fetched, at which version | `docker exec clanker-learn cl software` |
| Fetch again (Pull) | `docker exec clanker-learn cl agent pull` (Claude Code), `cl agent pull pi`, `cl editor pull` |
| Ask whether there is a newer version | `docker exec clanker-learn cl agent refresh --check` (add `pi` for pi) |
| Raise the version and fetch it (Refresh) | `docker exec clanker-learn cl agent refresh` (`cl agent refresh pi`, `cl editor refresh`) |
| Add an agent to a running project | `docker exec clanker-learn cl agents add <name> pi` |
| Update a running project's agents | `docker exec clanker-learn cl agents update <name>` |
| Set a project's model | `docker exec clanker-learn cl agents model <name> --model … --effort …` (or `--pi-model … --pi-thinking …`) |

How it works: [Software](../architecture/software.md)
