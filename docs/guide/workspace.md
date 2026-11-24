---
title: Shell, Editor and web apps
parent: User guide
nav_order: 5
---
# Shell, Editor and web apps

A project's tabs sit in the strip across the top of the page: the open lesson, **Shell**, **Editor** (in a project that has it), a tab for each web app running in the sandbox, **Insights** (see [The Knowledge page](knowledge.md#project-insights)) and **Ports**. Everything in them works on the same sandbox the tutor works in.

## The Shell

A terminal in the sandbox, as `learner`. It runs in tmux, so its session stays when the tab or the browser is closed, and comes back when you open it again. `sudo` works without a password. The Shell is also where you log in to an agent by hand (see [Agents and software](agents.md#logging-in)).

## The Editor

In a project that has it: the code editor (code-server) on the workspace, where you browse and edit the files the tutor made. It starts at the first visit. It comes with the Python and Jupyter extensions and, in a project with Claude Code, Claude Code's panel. A file the tutor links to in a lesson opens here.

The editor's previews (Markdown, notebooks and other webviews) need a certificate the browser trusts: with clanker-learn's own self-signed one they fail with `Could not register service worker: SecurityError`. See [A trusted certificate](configuration.md#a-trusted-certificate).

## Web apps

A server that listens on the sandbox's address gets **a tab of its own** a few seconds after it starts listening, and the tab is marked when it is gone. Each such tab is served on a port of its own, behind your login. Some browsers want the certificate accepted once for each port: if a tab stays empty, use its "open in a new tab" link, accept the certificate there, and reload the tab.

A server that listens only on the sandbox's loopback (`127.0.0.1`, the default of many development and notebook servers) cannot be reached from outside the sandbox, so it gets no tab. Inside the sandbox, `cl-expose <port>` puts it on the sandbox's own address too, and the tab appears. Use it only for such a server: for one that listens on all addresses, `cl-expose` takes the port first and the server then fails to start.

## Ports

The **Ports** tab names a port by hand:

| As | For | What you get |
|---|---|---|
| **web app** | A web app that was not found by itself. | A tab, behind your login. |
| **raw TCP** | Reaching a port of the sandbox from your machine with any program (a database client, say). | An address on your machine, `127.0.0.1:<port>`, from the raw TCP range. **There is no login in front of it**: anything on your machine that can reach that address reaches the sandbox's port. |

## Tabs side by side

Drag a tab to the side of the tab area to open a second column, and again for a third (three at most). Drag the line between two columns to change their widths. Drag a tab from one column to another to move it. The browser remembers the layout, and which project, lesson and tab were last shown.

## The sidebar

The **Projects** tab, first in the strip, shows and hides the sidebar: your projects with their lessons, and **+ New project**, **▤ All projects** (every project, archived ones too), **Knowledge**, **Settings** and **Log out** at the bottom. Click **Knowledge** or **Settings** again to go back to your project.

- **Floating** (the default): the sidebar opens over the page and closes again once you open something from it (a lesson, a tab, Settings, Knowledge) or click elsewhere.
- **Pinned**: the pin at the top of the sidebar keeps it open beside the page. The **Projects** tab then hides it and unpins it in one click.
- Drag its right edge to change its width.

Like the [Interface settings](configuration.md#interface), the pin and the width are kept by this browser only.

How it works: [The page](../architecture/the-page.md)
