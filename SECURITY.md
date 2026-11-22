# Security

What clanker-learn is made of, what each part holds, what is enforced and by what, and what is not protected. It is written for the person who runs it: read "What is not protected" before you put anything you care about into a sandbox.

## The stance

clanker-learn runs a language model (Claude Code) with full rights in a sandbox, on content that can be hostile: pages it reads, packages it installs, code it runs, files you or it put there. So:

- **A sandbox is treated as untrusted.** Whatever runs in it may try to reach your machine, your network, the other projects and clanker-learn's own services. Those are what is defended.
- **Within a project, nothing is defended from the sandbox.** The tutor of a project works in that project's sandbox and believes what it finds there. What is in a sandbox can steer its tutor.
- **Your machine is defended only as far as the mode allows.** In privileged mode, clanker-learn's container is root-equivalent on the machine. Settings, under "How it runs", says which mode it was started in, and in privileged mode a line at the bottom of the project list says "privileged mode".

## The parts and what each holds

- **The outer container** (`clanker-learn`): one container on your machine's Docker. It holds a Docker daemon of its own (the inner daemon), which runs everything below; the guard rules; the init, which checks all of it at every start; and both volumes: `/data` with everything that is yours, and the inner daemon's `/var/lib/docker`. Whoever is root in it holds everything.
- **The broker**: a process of the outer container, the one door the web server has to projects. It listens on a unix socket (mode 0600, its own user only) that no sandbox and no inner container has, apart from the web server, which gets the socket's folder read-only. It knows a closed list of verbs, never the inner daemon itself, and checks the login for every verb but four reads (the project list, a project's status, how an operation stands, whether a first password can be set). It holds the password's hash (`/data/broker/`), the login sessions in memory, and through the core every project's keys.
- **The web server** (`cl-web`): an inner container with no capability, no way to gain one, no Docker socket and no project folder. It has the broker's socket folder read-only, its own folder (the page's certificate and key, which tab is on which port), and the secret it shares with the tutor service, read-only. It holds the login sessions' cookies and, while a terminal is open, that project's Shell password, in memory. It listens on its address on its own network (`cl-front`) only, where the page's ports are published.
- **The tutor service** (`cl-tutor`): an inner container with no capability, no way to gain one and no published port. It has its own folder of `/data`: its database (lessons, conversations, plans, what it keeps about you), the Claude token set in Settings (a file of mode 0600, never returned to the page and never logged), and **the SSH key into every project's sandbox**. It is on every project's network, to reach each sandbox's SSH server. It listens twice: its API on its `cl-front` address only (port 7801), answering only requests that carry the secret it shares with the web server; and the tool server, port 7802 on every address, for Claude Code in the sandboxes (below).
- **A sandbox per project** (`cl-sb-<name>`): a plain container, not privileged, with Docker's default capabilities less `NET_RAW` (no raw sockets; `ping` still works), no published port and no restart policy, on a network of its own. Its user `learner` can become root in it with `sudo`. It has its workspace and its home folder read-write, its provisioning folder (its SSH host key, the tutor's public key, the Shell password's hash) read-only, and the fetched Claude Code and editor read-only. Its home holds that project's Claude Code login, if you logged in there.

## What a sandbox can reach, and what enforces it

| A sandbox | Reaches | Enforced by |
|---|---|---|
| The internet (public addresses) | yes | nothing stops it |
| Your machine, your network, any private address (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10) | no, but port 53 of a resolver the outer container is configured with that has a private address (a home router, say), so that names resolve | the guard rules: what leaves an inner bridge for a private address is dropped |
| The outer container itself: the broker, the status port, the inner daemon | no | the guard rules drop every new connection from an inner bridge to the outer container; the broker's socket is not in any sandbox |
| A port published by clanker-learn, through the outer container's address | no | the guard rules drop a connection that was redirected to a published port |
| Another project's sandbox | no | each project has its own network; Docker's isolation between networks; the guard rules send traffic between inner networks to Docker's rules |
| The web server | no | it is on the project's network but listens only on its `cl-front` address, which is on another network |
| The tutor service's API | no | it listens only on its `cl-front` address |
| The tutor's tool server (port 7802) | yes, by the name `cl-tutor` | it answers only with the token of an answer being written in that project (below) |
| IPv6 anywhere | no | the guard rules drop all IPv6 from an inner bridge |
| Raw packets, to address a service's other network | no | `NET_RAW` is dropped |

The guard rules are iptables chains in the outer container, keyed on the inner daemon's bridges. They are put in place before the inner daemon starts, so no inner container ever runs without them; they are checked at every start and every few seconds while it runs. Rules that cannot be put back, or an inner daemon that died, make clanker-learn end everything it runs and refuse to go on. RS's version of these rules warns and carries on; this one does not.

The services a sandbox shares a network with (the web server, the tutor service) have no capability, and listen on another network's address, apart from the tool server.

## The login

- **The password never leaves the browser.** The page derives a proof from it (PBKDF2-SHA256, 600,000 rounds, with a fixed salt) and sends that, over the page's HTTPS. The broker stores a scrypt hash of the proof, with a salt of its own, on the data volume.
- **A session** is a token made by the broker and held in its memory for at most 8 hours; a restart of the broker or a new password ends every session. The web server keeps the token on its side, under a cookie that is `HttpOnly`, `Secure`, `SameSite=Strict` and sent only to the login's own paths.
- **Wrong passwords**: after 10 within a minute, logins are refused for a minute. The set-up code for the first password has the same limit, works only while no password is set, never replaces one, and is printed only in the container's log.
- **Step-up**: destroying a project, and removing a backup in Settings, need the password again in the request, beside the session.
- **Origin checks**: every request that changes something, and every terminal connection, is refused unless the browser says it comes from the page's own address.
- **Web-app tabs** each have a port of their own, behind a session for that project: a cookie made from the login, `HttpOnly`, `Secure`, `SameSite=Strict`, that lasts no longer than the login. The web server takes its own cookies out of a request before passing it to a web app, and out of the app's answer.
- **The page's content-security policy**: scripts from the page's own files only, none inline; fetches and the terminal's connection to the page's own address only; frames and pictures only from the page's host over HTTPS (the web-app tabs, and the project's files server for a lesson's pictures and pages); no plugins, no form sent by the browser itself, and the page cannot be framed by another.
- **The terminal** connects to the sandbox's SSH server with the address and host key the broker gives, never with what the browser names, and refuses a host key it was not told.

## What the tutor writes is shown as data

The tutor's text is decided by what is in its sandbox, so the page treats it as data, never as code on the page's own address:

- Its Markdown is parsed into elements by the page, with raw HTML off: a `<script>` stays text. Links only to `http`, `https` and `mailto` addresses, opened in a new tab without an opener. A picture named by a web address is not fetched.
- A diagram is drawn by Mermaid at its strict level, without HTML labels, and copied into the page element by element from a list of allowed ones: no script, no handler, no link.
- A plot is drawn by Vega with its expression interpreter (no code made at run time) and a loader that refuses every address: only the data inside the plot is used.
- A picture and a page come only from the project's own files server, a read-only server of the workspace in the sandbox, served on the project's own web-app port, never from the page's address. A page made by the tutor runs its own scripts there, framed, on that port: it can do what that project's web apps can do, and no more.

## The tutor's tools

Claude Code in a sandbox reaches one thing of clanker-learn's own: the tool server, through which a tutor reads the plan, closes a lesson, proposes a plan or a change, records what it learned about you, sets exercises and questions.

- **A token per answer**: each answer being written has a token of its own, made when it starts and gone when it ends, handed to Claude Code in the sandbox's environment. A request without a token of an answer that is running now is refused before anything else is looked at.
- **Whose data a call touches comes from the token only**: the project, the lesson and the answer. No argument of a call names them.
- **Another project's insights reach no tool**: a tutor reads the brief (shared by every project) and its own project's records; evidence or insights of another project are refused when cited.
- A call is checked against its schema, limited in time, and recorded; one answer may make at most 60 calls, and a result is at most 64 KiB.

## What the tutor may record about you

- A **goal** or a **preference** is recorded only with a sentence you typed in that project, quoted exactly (at least 12 characters), and found by clanker-learn in your own messages. A message the page composed for you (an answer on a card) does not count.
- An **insight** rests on evidence clanker-learn itself saw (a check that ran, an answer it graded with your typed words, a lesson's summary) or on a sentence you typed. It is changed only by being replaced or retired, and every change is logged with what it was before.
- A **global insight**, told to every project's tutors, exists only after you approve it on the Knowledge page. A tutor can only propose one.
- **Everything is yours to change**: the Knowledge page edits or deletes any record, and every change, by you or by a tutor, is in its History and can be undone.
- **Destroying a project** tells the tutor service, which keeps the project's lesson summaries, insights and your sentences there marked as from a removed project, or forgets them if you ticked that. Its conversations go either way.

## What is not protected

Each of these is true today. They are stated once, here.

- **In privileged mode, the outer container is root-equivalent on your machine.** What stands between a sandbox and the machine is the sandbox's own container boundary and the guard rules. A way out of a sandbox (a kernel or container-runtime flaw) is a way to the machine.
- **Code the tutor runs in a sandbox can read the Claude token** set in Settings, which reaches each answer through its environment, **and the project's own Claude Code login** in its home folder. Treat either as exposed to anything that runs in a sandbox; if one may have leaked, revoke it with Claude and set a new one.
- **What is in a sandbox can steer its tutor**: a file, a package, a web page the tutor read. And so it can steer what the tutor writes to you, the plans and changes it proposes, the commands it runs, and what it records about you, within the rules above.
- **While an answer is being written, anything in the sandbox can use its token.** The token is in the environment of Claude Code and of what it runs, so a program there can call the tutor's tools as the tutor would, within the same rules.
- **A check shows a state of the sandbox, not who made it.** An exercise's check that passes says the files were right when it ran, not that you wrote them: the tutor, or anything else in the sandbox, could have.
- **A quick question's answer is in the sandbox.** The tutor gives the right answer to the tool when it asks the question, and Claude Code keeps its session files in the project's home folder, which you can read from the Shell.
- **A backup holds the keys.** It holds the password's hash, the Claude token, each project's Claude Code login and the keys into every sandbox, unencrypted. Keep it as such. Anyone logged in to the page can download one: the web server is handed one backup at a time, for one download, and never the folder of backups.
- **Raw TCP ports you name by hand have no login in front of them.** Anything that can reach that port on your machine reaches the sandbox's port.
- **The tool server is the one thing of clanker-learn's own that a sandbox reaches**, and it runs in the tutor service, the process that holds the keys into every sandbox and the Claude token. A flaw in it would be reachable from any sandbox.
- **The proof the browser sends is the same at every login.** To clanker-learn it is as good as the password; it travels only inside the page's HTTPS.
- **The certificate is clanker-learn's own**, accepted by you at the first visit. The browser cannot tell it from another self-made one later.
- **Sysbox has not been run.** The sysbox mode is written and tested only as far as its configuration goes; nothing here has been checked on a real sysbox machine.
- **arm64 has not been run.**
- **One learner per installation.** There are no user accounts: whoever has the password has everything, and whoever can run `docker exec` on the machine has everything without it.

## Reporting a problem

Open an issue on the repository you got clanker-learn from. There is no other address for reports.
