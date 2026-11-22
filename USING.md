# Using clanker-learn

This is the page as you meet it, once clanker-learn runs and you have a password ([INSTALL.md](INSTALL.md) says how). Open `https://127.0.0.1:7788` and log in. The password never leaves the browser: the page sends a proof made from it. A login lasts a working day (8 hours).

The page has your projects down the left side, and the open project's tabs on the right. Under the list are `+ New project`, **Knowledge** (what every tutor knows about you) and **Settings**.

## Projects

A project is one subject, and one sandbox: a Linux container of its own with a workspace folder (`/workspace`), a home folder, and a user, `learner`, who can become root with `sudo`. You and the tutor work in the same sandbox. It reaches the internet, and nothing private: not clanker-learn itself, not another project, not your network. It has no raw sockets: packet capture does not work in it; `ping` still does.

**Making one.** `+ New project` asks for a name (lower-case letters, digits and dashes, up to 31) and what the project gets:

- **Claude Code**, ticked by default, with the model and the effort it starts on (empty means Claude's own defaults). A project needs Claude Code for the tutor.
- **The editor**, ticked by default.
- **Limits**, optional: how many processes, how much memory (as `512m` or `2g`), how many processors (as `0.5` or `2`). With none set, a project may use up to 16384 processes and all the machine's memory and processors.

The box shows the progress. A project cannot be given Claude Code or the editor before the first start has fetched them (Settings, Software, shows it). The first project on a new installation waits for the sandbox image if the first start has not finished building it. Each project gets its own copy of Claude Code and of the editor in its home folder, about 400 MB and 350 MB.

**The gear beside a project** opens its box:

- **Start** and **Stop.** What you installed is still there after a stop and a start, and after clanker-learn itself is stopped and started. The projects that were running when clanker-learn stopped are started again when it starts.
- **Recreate** gives the project a new container from the current sandbox image. The workspace and the home folder stay, with Claude Code's login and sessions; what was installed elsewhere is gone, and the setup recipe (below) brings it back. A stopped project is running afterwards.
- **Agents**: add Claude Code to a project that has none, **Update** it to the version fetched in Settings, change its model and effort. These work while the project runs, without a restart. A project made before projects had agents is recreated first, then given Claude Code here.
- **Limits**: change them. They apply at once, with two exceptions the box tells you about: a memory limit is removed only when the project is next recreated, and on sysbox a processor limit is removed only at its next start.
- **Destroy** asks for your password again, then removes the sandbox, its workspace, its home folder and its ports, for good, and its conversations with them: a new project of the same name starts empty. It also asks whether to **forget what you learned there**. Left unticked, the lessons' summaries and the goals and preferences you stated there stay for your other projects' tutors, and the project's insights stay on the Knowledge page for you, told to no tutor; all marked as from a removed project; ticked, they go too. The tutor service is told at the end of the destroy; if it could not be told, the box says so. If a destroy is cut short, run it again: a project being destroyed is never started.

## Goals and the study plan

A project opens on **Goals**, the first entry in its list. It is a conversation in which the tutor asks what you want to be able to do, what you know already and how much time you have: a few questions. Then it proposes a study plan, shown as a card in the conversation: modules, each a list of lessons with a title and an objective.

Change anything on the card: titles, objectives, lessons added, removed or moved. Then **approve** it, or **dismiss** it. Nothing changes until you approve. On approval the modules and lessons appear in the list under Goals. The tutor learns what you decided with your next message: you do not need to tell it.

Later, a lesson's tutor can propose a **change to the plan** the same way (a lesson added to go over something again, another order, a lesson dropped), as a card you edit and approve or dismiss.

`+ New lesson` at the end of the list adds a lesson by hand, outside the plan, with a title and an optional objective. The `⋯` beside a lesson renames it, changes its objective, or deletes it with its conversation (the files the tutor made in the sandbox stay).

## Lessons

Click a lesson to open it. Lessons can be taken in any order. Each shows its state in the list: not started, in progress, done.

**The conversation.** You write; the tutor answers as it goes, its text appearing while it is written. When a lesson starts, its tutor is told the lesson's objective, its place in the plan, one line on each lesson already done in the project, and your brief (below). When the objective is met it closes the lesson with a short summary and says what comes next. You can still write in a lesson that is done.

**The tool lines.** Each thing the tutor does in the sandbox (a command it runs, a file it reads or writes) and each of clanker-learn's own tools it uses (reading the plan, recording something about you) is a line in its answer. Open one to see what was asked and what came back. What it installs and starts stays in the sandbox.

**Stop** ends the answer being written, and whatever the tutor was running for it. Closing the browser does not: the answer goes on and is there when you come back. If clanker-learn's tutor service restarts while an answer is being written, that answer is shown as interrupted, and the lesson takes your next message.

**What a conversation can show**, besides text, tables and code:

- a diagram (Mermaid);
- a plot (Vega-Lite), with hover;
- a picture the tutor made in the workspace;
- a page the tutor made in the workspace (an HTML file, a notebook's output), framed in the conversation, with a link to open it in a browser tab of its own;
- a link to a file of the workspace, which opens the file in the Editor.

Nothing of it is fetched from outside the project: a picture named by a web address is not shown, and raw HTML is shown as text.

**The context line** under the conversation says how full the tutor's context is, as `Context 55k of 200k tokens (92% cached)`.

**Two at once.** Lessons are independent. Two can have their tutors working at the same time, in different projects or in one; in one project both work in the same sandbox. A lesson takes one message at a time.

**A lesson's files** are in the workspace, under `/workspace/.learn/lessons/<number>-<title>/`: `transcript.md`, the whole conversation, written by clanker-learn after every answer, and `notes.md`, the tutor's own notes. If the tutor's session of a lesson is lost, or its instructions changed, it starts a fresh one, takes the lesson up from these two files and says so.

**Signing in to Claude.** The tutor is the project's own Claude Code, with the project's model and effort, and it needs a login. Either:

- open the project's Shell and run `claude` once, and log in there: that project's tutor then uses that login; or
- set a token in Settings, Tutor: the tutor in every project then uses it, and a new project's first lesson works without a login. Make the token with `claude setup-token` on a machine where you are logged in to Claude.

A lesson continues through Claude Code's own session in the project's home folder; `claude --resume` in the Shell lists it.

**Exercises.** The tutor can set an exercise as a card in its answer: a title, the steps, and the check, a command shown on the card. Do the steps in the Shell or the Editor, then press **Check**: the command runs in your sandbox (up to half a minute) and the card says passed or failed, with the command's output under "Last check". **Check again** runs it again as often as you like. The tutor can run the check too; the card says who ran it last. What a check gives back is kept, and is the kind of evidence an insight about you can rest on.

**Quick questions.** The tutor can ask a multiple-choice question as a card. Pick an option and press **Answer**: the card says right or not, shows the right one and the tutor's explanation, and keeps your answer, right or wrong.

**What the tutor is told, and when.** When no answer is being written, a Check or an Answer goes to the tutor at once as a message of its own, which clanker-learn writes for you ("You ran the check of …", "You answered …"), and the tutor answers it. While an answer is being written, the card changes at once and the tutor is told with your next message; the card says so. A message clanker-learn wrote for you never counts as your own words: no goal or preference is recorded from it. Under each of your messages, "what the tutor was told" opens what went to the tutor before your words: the lesson's opening, how you decided on a card.

## The Shell

A terminal in the sandbox, as `learner`. It runs in tmux, so its session stays when the tab or the browser is closed, and comes back when you open it again. `sudo` works without a password.

## The Editor

In a project that has it: the code editor (code-server) on the workspace, where you browse and edit the files. It starts at the first visit. It comes with Python and Jupyter extensions and, in a project with Claude Code, Claude Code's panel.

## Web apps and Ports

A server that listens on the sandbox's address gets **a tab of its own** a few seconds after it starts listening, and the tab is marked when it is gone. Each such tab is served on a port of its own, behind your login. Some browsers want the certificate accepted once for each port: if a tab stays empty, use its "open in a new tab" link, accept the certificate there, and reload the tab.

A server that listens only on the sandbox's loopback (`127.0.0.1`, the default of many development and notebook servers) cannot be reached from outside the sandbox, so it gets no tab. Inside the sandbox, `cl-expose <port>` puts it on the sandbox's own address too, and the tab appears. Use it only for such a server: for one that listens on all addresses, `cl-expose` takes the port first and the server then fails to start.

The **Ports** tab names a port by hand:

- as a **web app**, for one that was not found by itself: it gets a tab, behind your login;
- as **raw TCP**, to reach a port of the sandbox from your machine with any program (a database client, say): it gets an address on your machine, `127.0.0.1:<port>`, from the raw TCP range. **There is no login in front of it**: anything on your machine that can reach that address reaches the sandbox's port.

## Tabs side by side

Drag a tab to the side of the tab area to open a second column, and again for a third (three at most). Drag the line between two columns to change their widths. Drag a tab from one column to another to move it. The browser remembers the layout, and which project, lesson and tab were last shown; nothing else is kept in it.

## Settings

`⚙ Settings`, at the bottom of the project list.

**Software** has a row for Claude Code and one for the editor: the version fetched, the version in effect, and whether the two agree.

- **Pull** fetches that software again at the version in effect, and shows the log while it runs.
- **Refresh** asks its maker for the newest version, and offers to raise the version and fetch it if there is a newer one. A version is never moved down.
- **Use repo version** goes back to the version clanker-learn came with.

Each is fetched by its maker's own installer, in a container that is thrown away afterwards; one is fetched at a time. Running projects keep the Claude Code they have until you press Update in their gear's Agents section; new projects get the fetched one. A newer editor reaches a project at its next start.

**Tutor** holds two things:

- **The Claude token**, which every project's tutor uses when it is set (see "Signing in to Claude"). It is not shown again once set; you can replace or remove it.
- **The tutor's instructions**: the text every tutor is given with each of your messages, in every project. Read it, change it and save it; a lesson under way follows the new text from its next message, starting again from its files. **Back to the shipped text** puts clanker-learn's own text back.

**Backup** makes a backup of everything clanker-learn keeps, and lists the ones there are.

- **Make a backup** starts one and shows its log while it runs. It can be closed: the backup goes on, and the section says one is being made, also after a reload.
- Each backup is a row: its name, its size, when it was made, and the projects in it. One made while projects were running says which: files being written at that moment may be torn. Stop the projects first for a clean copy.
- **Download** saves the file in your browser's downloads.
- **Remove** asks for your password again, then removes it for good.

A backup holds your projects' workspaces and home folders, the tutor's conversations and what it knows about you, and the settings. It does not hold the fetched Claude Code and editor, which are fetched again, nor what was installed in a sandbox outside its workspace and home folder, which the setup recipe brings back. It holds the password's hash, the Claude token and every project's keys: keep it as safely as those. Restoring one is a command, `cl restore`, and not a button, because it replaces everything ([INSTALL.md](INSTALL.md) says how).

**How it runs** says which way clanker-learn was started: privileged or sysbox ([INSTALL.md](INSTALL.md)). In privileged mode, it says what that means, and a quiet line, "privileged mode", at the bottom of the project list leads there.

## What the tutor keeps about you: the Knowledge page

clanker-learn keeps a small knowledge base about you, shared by every project. **Knowledge**, under the project list, shows all of it, and you can change or delete any of it. Its dot is lit when a tutor proposes something that waits for you.

**Your brief** is what every tutor is told about you when a lesson starts, in any project. It has these parts:

- **Your background**: what you know already and what you work with, in a box you write. Only you write it; every tutor reads it as you wrote it from its next lesson.
- **Your goals** and **how you want to be taught**. A tutor records one only with a sentence you typed in a lesson, quoted exactly (12 characters at least), and never one it inferred. Each line says who recorded it, and "what it rests on" shows your sentence. You can add your own, in your own words, and edit or delete any.
- **How you learn**: the global insights you approved (below).
- **Recently**: the lessons done lately, in any project, one line each; open one for its summary.

"As a tutor reads it" shows the exact text a tutor is given, put together from the records by the program, never written by a model.

**Global insights** are the few things every tutor, in every project, is told about how you learn. A tutor can only propose one: a new one, a replacement, or the retirement of one. A proposal waits under "Waiting for you", with the project and lesson it came from, why, what it rests on, and the like insights of your other projects. Edit its text as you want every tutor to read it, then **Approve** (every tutor is told it from its next message) or **Dismiss** (nothing changes; the tutor that proposed it hears of it with your next message there). You can also add one yourself. There is no limit on how many.

**A project's insights**, under "Per project" on the Knowledge page and in the project's **Insights** tab: what its tutors learned about how you learn there (a misconception, a strength, what worked, what did not). Each rests on something clanker-learn itself saw: a check that ran, an answer you gave, a lesson's summary, or a sentence you typed; "what it rests on" shows it. Only that project's tutors are told them. A tutor changes one only by replacing or retiring it; "show retired" shows those too. A destroyed project's insights and lessons stay here, marked as removed, unless you chose to forget them; no tutor is told them.

**Lessons done**: every lesson a tutor closed, in every project, newest first, with a search over the titles and summaries. The line in bold of a summary is what later lessons' tutors are given.

**History**: every change to what the tutors know about you, by you or by a tutor, newest first, with what it was before and after. **Undo** takes a change back, unless what it changed was changed again since.

## What a sandbox brings back by itself

Two files in the workspace, which the tutor keeps current and you can edit, say what a sandbox needs beyond its files:

- **`/workspace/.learn/setup.sh`, the setup recipe**: what was installed. It runs once in every new container, as `learner` (who can `sudo`), so after a recreate, or a restore, the software is back. It does not run at a plain stop and start, where nothing was lost. Its output is in `/var/log/cl/setup.log` in the sandbox. If the sandbox is stopped while it runs, it runs again from the top at the next start, so it must be safe to run twice. A recipe that failed is not tried again until the next recreate, and one that never ends holds back the services: it has no time limit.
- **`/workspace/.learn/services`**: what should be running, one line each, `name: command`. A name is lower-case letters, digits and dashes, starts with a letter or a digit, and has at most 32 characters; other lines are skipped. At most 64 services and 64 KiB are read. They are started at every start of the sandbox, after the recipe, as `learner`, with their output in `/workspace/.learn/logs/<name>.log`. They are started, not watched: one that ends stays down until the next start. At a stop they are asked to end before they are killed.

For example, a notebook server that listens only on loopback, with its tab:

```
notebook: jupyter lab --port 8888
notebook-tab: cl-expose 8888
```

## The `cl` command

Someone with a shell on the machine can drive clanker-learn from the command line, inside its container. It asks for no password: whoever can run it is root in the container already. Each command answers one line of JSON.

```
docker exec clanker-learn cl list
docker exec clanker-learn cl status kafka
docker exec clanker-learn cl create kafka --agent claude --editor --memory 4g --cpus 2
docker exec clanker-learn cl stop kafka
docker exec clanker-learn cl start kafka
docker exec clanker-learn cl limits kafka --memory 8g --pids none
docker exec clanker-learn cl recreate kafka
docker exec clanker-learn cl destroy kafka
```

- `cl create` gives a project neither Claude Code nor the editor unless asked: `--agent claude`, `--editor`, and `--claude-model` and `--claude-effort` for the model and effort.
- `cl status` also says how the setup recipe went (`setup`: none, running, done, failed).
- `cl agents add <name>`, `cl agents update <name>` and `cl agents model <name> --model … --effort …` are the gear's Agents section.
- `cl software`, `cl agent pull`, `cl agent refresh`, `cl editor pull` and `cl editor refresh` are Settings, Software (`refresh --check` only asks).
- `cl passwd` sets the password ([INSTALL.md](INSTALL.md)).
- `cl backup`, `cl backups`, `cl backup-rm` and `cl restore` make, list, remove and restore backups ([INSTALL.md](INSTALL.md)).

The page and the command never work on one project at the same time: while something works on a project, the page's request is refused as busy, and a `cl` command waits for it to end.
