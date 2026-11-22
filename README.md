# clanker-learn

A tutor you run on your own machine. You learn a technical subject by doing it, in a real Linux environment, with a tutor that works in that environment beside you and remembers what you told it about yourself.

It is for one person who wants to learn something hands-on (a database, a language, a tool, a piece of infrastructure) and would rather keep the whole thing on their own computer. The tutor is Claude Code, driven by your own Claude subscription, or pi, with a provider of your choice; a lesson can switch from one to the other at any point.

## What it looks like

- You make a **project** for each subject. A project is a sandbox: a Linux container of its own with a workspace folder, a terminal, a code editor, and room to install and run whatever the subject needs.
- A project opens on **Goals**: a conversation in which the tutor asks what you want to be able to do, what you know already and how much time you have. It then proposes a **study plan** as a card: modules of small lessons, each with an objective. You edit the card and approve it, and the lessons appear in the list.
- A **lesson** is a conversation with the tutor. It explains, then builds small working examples in the workspace, runs commands, installs software, starts servers, and shows diagrams, plots, pictures and pages it made. Each thing it does in the sandbox is a line you can open. You work in the same sandbox in the Shell and Editor tabs beside the conversation, and each web app started there gets a tab of its own.
- The tutor **keeps a brief about you**: your goals and how you want to be taught, in your own typed words; the lessons you finished, in any project; and notes on how you learn, each resting on something it saw. Every tutor reads the brief when a lesson starts.

## What it needs

- A Linux machine with Docker. It runs as one container, which holds a Docker daemon of its own for the sandboxes. Only x86-64 has been run so far.
- A Claude subscription, for Claude Code. You log in once per project, or set one token for every project in Settings. For pi instead, or as well: an account with a provider pi supports, logged in once per project, in its Shell.
- The internet: at the first start clanker-learn builds its images and fetches Claude Code and the editor, and the sandboxes reach the internet while you work.

## The quickest start

From a copy of this repository:

```
docker build -t clanker-learn -f outer/Dockerfile .
docker run -d --name clanker-learn --stop-timeout 60 --privileged -e CL_MODE=privileged \
    -p 127.0.0.1:7788:7788 -p 127.0.0.1:8080:8080 \
    -p 127.0.0.1:11100-11164:11100-11164 -p 127.0.0.1:11200-11299:11200-11299 \
    -v clanker-learn-docker:/var/lib/docker -v clanker-learn-data:/data \
    clanker-learn
docker logs clanker-learn
```

Open `https://127.0.0.1:7788` in a browser on the same machine, accept its self-made certificate, and type the set-up code that the log shows to choose your password. Then press `+ New project`.

This runs the container privileged, which makes it root-equivalent on your machine; Settings and a line under the project list say so. [INSTALL.md](INSTALL.md) says what that gives up, and how to run it on sysbox instead.

## How far it is built

It is a first version. What exists: projects and their sandboxes; the page with the Shell, the Editor, web-app tabs and ports; Claude Code, pi and the editor fetched once and copied into each project, and updated from Settings; Goals and the study plan; lessons as conversations with the tutor, with diagrams, plots, pictures, pages, exercises and quick questions; tabs side by side; the Knowledge page, where you read and change what the tutor keeps about you and approve the insights it proposes for every project; backups, made, downloaded and removed in Settings, and restored with a command.

pi is built as a second agent, interchangeable with Claude Code: a project can have either or both, a lesson is taught by the one it chose or by the default set in Settings, and it can switch in the middle, the other agent taking the lesson up from its files. **No real model has answered through pi yet**: it was run against a stand-in that speaks the same API, so the first real pi lesson may surface faults.

## Documents

- [INSTALL.md](INSTALL.md): running it, the first start, settings, where your data is, backups, updating, uninstalling, and what to do when it refuses to start.
- [USING.md](USING.md): the page, projects, lessons, exercises and questions, the Shell and the Editor, Settings with its backups, the Knowledge page, and the `cl` command.
- [SECURITY.md](SECURITY.md): what each part holds, what a sandbox can and cannot reach, and what is not protected.

## Tests

For developers:

```
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/pytest tests
.venv/bin/ruff check .
```

The tests marked `integration` start real containers and need a Docker daemon that can run a privileged one. They publish no port on the machine. Every container, network and named volume they create starts with `cl-test-` and is removed afterwards, each container together with the unnamed volumes the image gives it; the images they build, `cl-outer:test` and `cl-sandbox:<hash>`, are kept. One test lets clanker-learn build the sandbox image itself and takes a few minutes.

## Licence

clanker-learn is free software under the GNU Affero General Public License, version 3: see [LICENSE](LICENSE).
