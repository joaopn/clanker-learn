---
title: FAQ
parent: User guide
nav_order: 12
---
# FAQ

### Is my data private?

clanker-learn is self-hosted: everything it keeps is in two Docker volumes on your machine, and there is no account to create. clanker-learn itself sends nothing anywhere; the editor's telemetry is turned off, and so is pi's unless you turn it on; Claude Code keeps its own settings. What leaves the machine: the conversation, to the model provider of the agent that teaches (Anthropic for Claude Code, the provider you chose for pi), the fetches of the software from its makers when you pull or refresh it, and whatever the tutor or you reach from a sandbox, which has the internet.

### Does it cost anything?

clanker-learn is free software under the GNU Affero General Public License, version 3. What costs is the agent's model: Claude Code runs on your Claude subscription, and pi on the provider you log it in to. Every tutor answer is usage of that subscription or account.

### Which subscription do I need?

One that you can log in to Claude Code with: each project's tutor is that project's own Claude Code. Log in once per project in its Shell, or set one token for every project in **Settings → Tutor**. See [Logging in](agents.md#logging-in). For pi instead, any account with a provider pi supports.

### Can I use another model?

Yes, two ways. Claude Code's model and effort are chosen per project, in the New project box or the project's Config box. pi talks to the provider and model you name as `provider/model`. A lesson can switch between the two agents at any point. Note that no real model has answered through pi yet: it was run against a stand-in that speaks the same API. See [Agents and software](agents.md).

### Does it work offline?

No. The tutor needs its model provider, the first start fetches Claude Code and the editor and builds the images, and the sandboxes are meant to reach the internet to install what a subject needs.

### Does it need sysbox?

No. Without sysbox, `./setup` sets privileged mode, which works with Docker alone but makes clanker-learn's container root-equivalent on the machine. With sysbox, root inside the container is not root on the machine. See [Sysbox or privileged](configuration.md#sysbox-or-privileged).

### What if I break a sandbox?

Press **Recreate** in the project's [Config box](projects.md#the-config-box). The project gets a new container; its workspace and home folder stay, with the agents' logins and sessions, and its setup recipe installs again what the tutor recorded there. Nothing in a sandbox can reach another project, so the others are not touched.

### Can lessons run in parallel?

Yes. Lessons are independent conversations: two can have their tutors working at the same time, in different projects or in the same one, where they share the sandbox. Each lesson takes one message at a time.

### What does the tutor see of my machine?

Only the project's sandbox: its workspace, its home folder and what runs in it. It cannot reach your machine, your network or another project. What it learns about you is on the [Knowledge page](knowledge.md), where you can change or delete any of it. See [Security](security.md).

### Can several people use one installation?

Not as separate users. There is one password and no accounts: whoever logs in sees everything. [Brief sets](knowledge.md#brief-sets) keep separate briefs, for different subjects or different learners, but not separate access.

### Can I reach it from another machine?

By default the page is published on the machine's loopback address only. If the machine is in a Tailscale tailnet, `./setup cert-tailscale` publishes it on the tailnet address with a trusted certificate. See [Configuration](configuration.md#a-trusted-certificate).

### Can a sandbox run Docker?

Not yet. A sandbox is a plain container without its own Docker daemon, so subjects that need containers of their own (learning Docker itself, say) are not covered by this version.
