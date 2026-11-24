---
title: Lessons
parent: User guide
nav_order: 4
---
# Lessons

A lesson is a conversation with the tutor, in the project's sandbox. The tutor explains, builds small working examples in the workspace, runs commands, installs software and starts servers, and you work in the same sandbox beside it. Click a lesson in the sidebar to open it in a tab; closing the tab leaves the lesson in the list.

## The conversation

You write in the box at the bottom and press **Send**; the tutor's answer appears while it is written. When a lesson starts, its tutor is told the lesson's objective, its place in the plan, one line on each lesson already done in the project, and your [brief](knowledge.md). When the objective is met it closes the lesson with a short summary and says what comes next. You can still write in a lesson that is done.

The lesson's header shows its title, objective and state, the [agent](#switching-agent) that teaches it, **Files** (the lesson's folder, in the Editor) and, once it is done, **Summary**.

**The tool lines.** Each thing the tutor does in the sandbox (a command it runs, a file it reads or writes) and each of clanker-learn's own tools it uses (reading the plan, recording something about you) is a line in its answer. Open one to see what was asked and what came back. What it installs and starts stays in the sandbox.

**Stop** ends the answer being written, and whatever the tutor was running for it. Closing the browser does not: the answer goes on and is there when you come back. If clanker-learn's tutor service restarts while an answer is being written, that answer is shown as interrupted, and the lesson takes your next message.

## What the tutor can show

Besides text, tables and code, an answer can hold:

- a diagram (Mermaid);
- a plot (Vega-Lite), with hover;
- a picture the tutor made in the workspace;
- a page the tutor made in the workspace (an HTML file, a notebook's output), framed in the conversation, with a link to open it in a browser tab of its own;
- a link to a file of the workspace, which opens the file in the Editor.

Nothing of it is fetched from outside the project: a picture named by a web address is not shown, and raw HTML is shown as text.

## Exercises

The tutor can set an exercise as a card in its answer: a title, the steps, and the check, a command shown on the card. Do the steps in the [Shell or the Editor](workspace.md), then press **Check**: the command runs in your sandbox (up to half a minute) and the card says passed or failed, with the command's output under "Last check". **Check again** runs it as often as you like. The tutor can run the check too; the card says who ran it last. What a check gives back is kept, and is the kind of evidence an [insight](knowledge.md#project-insights) about you can rest on.

## Quick questions

The tutor can ask a multiple-choice question as a card. Pick an option and press **Answer**: the card says right or not, shows the right one and the tutor's explanation, and keeps your answer, right or wrong.

## What the tutor is told, and when

When no answer is being written, a **Check** or an **Answer** goes to the tutor at once as a message of its own, which clanker-learn writes for you ("You ran the check of …", "You answered …"), and the tutor answers it. While an answer is being written, the card changes at once and the tutor is told with your next message; the card says so.

A message clanker-learn wrote for you never counts as your own words: no goal or preference is recorded from it. Under each of your messages, "what the tutor was told" opens what went to the tutor before your words: the lesson's opening, how you decided on a card.

## The context line

The line under the conversation says how full the tutor's context is, as `Context 55k of 200k tokens (92% cached)`. A long brief takes more of it.

## Several lessons at once

Lessons are independent. Two can have their tutors working at the same time, in different projects or in one; in one project both work in the same sandbox. A lesson takes one message at a time.

## A lesson's files

Each lesson has a folder in the workspace, `/workspace/.learn/lessons/<number>-<title>/`, which **Files** opens in the Editor:

- `transcript.md`: the whole conversation, written by clanker-learn after every answer;
- `notes.md`: the tutor's own notes.

If the tutor's session of a lesson is lost, or its instructions changed, it starts a fresh one, takes the lesson up from these two files and says so. A lesson otherwise continues through the agent's own session in the project's home folder; with Claude Code, `claude --resume` in the Shell lists it.

## Switching agent

The choice in a lesson's header says which agent teaches it:

- "the default (…)", which follows **Settings → Tutor** until the lesson's first answer, then "… which began this lesson", which stays when the default changes;
- or one of the agents the project has.

It can be changed at any time, also in the middle of a lesson, except while an answer is being written (then the lesson says so: stop it or wait). The other agent takes the lesson up from its files (the transcript and the tutor's notes) and is told which answers it did not see; changing back, the first agent goes on with its own session and is told what it missed. When more than one agent has answered in a lesson, each answer says whose it is ("Tutor · pi", "Tutor · Claude Code"). The tools, the cards, the plan, what the tutor knows about you and the context line are the same with either.

Choosing pi in a project that has no pi is refused in the conversation: add it from the project's Config box (its ⚙), under **Agents** (see [The Config box](projects.md#the-config-box)). Each agent needs a login: see [Agents and software](agents.md#logging-in).

How it works: [The tutor](../architecture/tutor.md)
