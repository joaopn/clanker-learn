---
title: Projects and sandboxes
parent: User guide
nav_order: 2
---
# Projects and sandboxes

A project is one subject, and one sandbox: a Linux container of its own with a workspace folder (`/workspace`), a home folder, and a user, `learner`, who can become root with `sudo`. You and the tutor work in the same sandbox. It reaches the internet, and nothing private: not clanker-learn itself, not another project, not your network. It has no raw sockets: packet capture does not work in it; `ping` still does. What exactly it can reach is in [Security](security.md#what-a-sandbox-can-reach).

## Making a project

Press **+ New project** at the bottom of the sidebar. The box asks for:

| Field | What it sets |
|---|---|
| **Name** | Lower-case letters, digits and dashes, up to 31 characters. |
| **Agents** | **Claude Code**, ticked by default, with the model and the effort it starts on (empty means Claude's own defaults). **pi**, a second agent, never ticked by default, with the model it starts on, written `provider/model` as pi names it (`openrouter/anthropic/claude-opus-latest`, say), and its thinking level (empty means pi's own pick). A model not written `provider/model` is refused, and the box says why. Tick either, both or neither; a project's tutor can only be an agent the project has. |
| **Editor** | The code editor (code-server), ticked by default. |
| **Brief** | Which [brief set](knowledge.md#brief-sets) the project's tutors are told about you. **Default** unless you pick another. |
| **Limits (optional)** | How many processes, how much memory (as `512m` or `2g`), how many processors (as `0.5` or `2`). With none set, a project may use up to 16384 processes and all the machine's memory and processors. |

An agent or the editor that has not been fetched yet cannot be ticked ("Not pulled yet: pull it under Settings → Software first"): Claude Code and the editor are fetched at the [first start](getting-started.md#the-first-start), pi only when you pull it in **Settings → Software** (see [Agents and software](agents.md)). The first project on a new installation waits for the sandbox image if the first start has not finished building it.

**Create** shows the progress. Each project gets its own copy of each agent and of the editor it is given, in its home folder: about 400 MB for Claude Code, 270 MB for pi, 350 MB for the editor. When it is made, the project opens on **Goals**: see [Goals and the study plan](goals-and-plans.md).

### From a file

**From a file**, at the top of the box, makes the project from a `.clproject` file that a project's **Export** made (see [The Config box](#the-config-box)). Picking the file fills the name, the limits and the editor; change what you like, then **Create**. The new project gets the file's workspace, plan and lessons, and its conversations when the file has them; its setup recipe runs at the first start. Its tutors are told about you from your own brief set, and a lesson that came with its conversation is taken up by your tutor from the lesson's transcript and notes.

## The Config box

The ⚙ beside a project in the sidebar opens its Config box. The box shows whether the sandbox runs and, while something is being done with the project, a **show the log** link; until that is done, its buttons wait.

- **Start** and **Stop.** What you installed is still there after a stop and a start, and after clanker-learn itself is stopped and started. The projects that were running when clanker-learn stopped are started again when it starts.
- **Recreate** gives the project a new container from the current sandbox image. The workspace and the home folder stay, with Claude Code's login and sessions; what was installed elsewhere is gone, and the [setup recipe](#what-a-sandbox-brings-back-by-itself) brings it back. A stopped project is running afterwards.
- **Agents**: what the project has. **Add** Claude Code or pi to a project that does not have it; change Claude's model and effort, or pi's model and thinking level, with **Apply**; **Update** both agents to the versions fetched in Settings. These work while the project runs, without a restart (Add on a stopped project asks you to start it first). A model and level applied are written into the agent's settings in the project's home folder, and a session started from then on uses them. A project whose sandbox was made before something here was possible is recreated first, then given it: the box says so.
- **Brief**: the brief set the project's tutors are told. A lesson takes a new one at its next start.
- **Limits**: change them with **Change the limits**. They apply at once, with two exceptions the box tells you about: a memory limit is removed only when the project is next recreated, and on sysbox a processor limit is removed only at its next start.
- **Export** makes the project one file, `<project>-<date>.clproject`, and downloads it: to give to someone, or to move the project to another clanker-learn. It always holds the workspace (the setup recipe and the lessons' folders included), the study plan and its lessons, the limits and whether the project has the editor. Tick **Include my conversations** to add what was said in each lesson, the lessons' states, transcripts and notes; without it every lesson arrives not started. It never holds the home folder (the agents' logins and sessions), the project's keys and Shell password, or what clanker-learn knows about you. Everything in the workspace goes in as it is, a `.env` with a key in it too.
- **Destroy** asks for your password again, then removes the sandbox, its workspace, its home folder and its ports for good, and its conversations with them: a new project of the same name starts empty. Tick **also forget what I learned here** to remove the rest too. Left unticked, the lessons' summaries and the goals and preferences you stated there stay for your other projects' tutors, and the project's insights stay on the [Knowledge page](knowledge.md) for you, told to no tutor; all marked as from a removed project. If a destroy is cut short, run it again: a project being destroyed is never started.

## What a sandbox brings back by itself

Two files in the workspace, which the tutor keeps current and you can edit, say what a sandbox needs beyond its files:

- **`/workspace/.learn/setup.sh`, the setup recipe**: what was installed. It runs once in every new container, as `learner` (who can `sudo`), so after a recreate, or a [restore](backups.md), the software is back. It does not run at a plain stop and start, where nothing was lost. Its output is in `/var/log/cl/setup.log` in the sandbox. If the sandbox is stopped while it runs, it runs again from the top at the next start, so it must be safe to run twice. A recipe that failed is not tried again until the next recreate, and one that never ends holds back the services: it has no time limit.
- **`/workspace/.learn/services`**: what should be running, one line each, `name: command`. A name is lower-case letters, digits and dashes, starts with a letter or a digit, and has at most 32 characters; other lines are skipped. At most 64 services and 64 KiB are read. They are started at every start of the sandbox, after the recipe, as `learner`, with their output in `/workspace/.learn/logs/<name>.log`. They are started, not watched: one that ends stays down until the next start. At a stop they are asked to end before they are killed.

For example, a notebook server that listens only on loopback, with its tab (`cl-expose` is explained in [Shell, Editor and web apps](workspace.md#web-apps)):

```
notebook: jupyter lab --port 8888
notebook-tab: cl-expose 8888
```

## The `cl` command

Someone with a shell on the machine can drive clanker-learn from the command line, inside its container. It asks for no password: whoever can run it is root in the container already. Each command answers one line of JSON.

| To | Run |
|---|---|
| List the projects | `docker exec clanker-learn cl list` |
| One project's state, and how its setup recipe went | `docker exec clanker-learn cl status kafka` |
| Make a project | `docker exec clanker-learn cl create kafka --agent claude --editor --memory 4g --cpus 2` |
| Stop, start, recreate | `docker exec clanker-learn cl stop kafka` (`start`, `recreate`) |
| Change its limits (`none` removes one) | `docker exec clanker-learn cl limits kafka --memory 8g --pids none` |
| Destroy it | `docker exec clanker-learn cl destroy kafka` |
| Add an agent, update its agents | `cl agents add kafka pi`, `cl agents update kafka` |
| Set the model | `cl agents model kafka --model sonnet --effort high` (or `--pi-model … --pi-thinking …`) |

`cl create` gives a project no agent and no editor unless asked: `--agent claude`, `--agent pi` (repeat it for both), `--editor`, `--claude-model` and `--claude-effort` for Claude's model and effort, `--pi-model` and `--pi-thinking` for pi's. `cl status` says how the setup recipe went (`setup`: none, running, done, failed). The rest of `cl` is in [Agents and software](agents.md#from-the-command-line), [Backups and restore](backups.md) and [Getting started](getting-started.md#the-first-password) (`cl passwd`).

The page and the command never work on one project at the same time: while something works on a project, the page's request is refused as busy, and a `cl` command waits for it to end.

How it works: [Sandboxes](../architecture/sandboxes.md)
