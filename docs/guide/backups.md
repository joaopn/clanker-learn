---
title: Backups and restore
parent: User guide
nav_order: 9
---
# Backups and restore

A backup is one archive of everything clanker-learn keeps: your projects, the tutor's conversations and what it knows about you, the password and the settings. You make, download and remove backups in **Settings → Backup**; restoring one is a command on the machine, never a button, because it replaces everything. There are no scheduled backups.

## Making a backup

In **Settings → Backup**, press **Make a backup**. It shows its log while it runs. You can close it: the backup goes on, and the section says one is being made, also after a reload.

Each backup is a row with its name, its size, when it was made and the projects in it, and two buttons:

| Press | What it does |
|---|---|
| **Download** | Asks for your password again, then saves the file in your browser's downloads. |
| **Remove** | Asks for your password again, then removes it for good. |

Or from the machine:

| To | Run |
|---|---|
| Make a backup | `docker exec clanker-learn cl backup` |
| List them, newest first | `docker exec clanker-learn cl backups` |
| Copy one off the machine | `docker cp clanker-learn:/data/backups/cl-backup-<date>T<time>Z.tar.gz .` |
| Remove one | `docker exec clanker-learn cl backup-rm cl-backup-<date>T<time>Z.tar.gz` |

`cl backup` writes `/data/backups/cl-backup-<date>T<time>Z.tar.gz` and says its size and how long it took. Most of a backup is the copies of Claude Code and the editor in each project's home folder: on a test machine, two new projects made a backup of 1.1 GB in under half a minute.

**Stop your projects first for a clean copy.** A backup can be made while projects run, but files being written at that moment may be torn: the command, and the backup's row in Settings, say which projects were running.

A backup needs room on the data volume: up to the size of what it holds, before compression. Without that much free it is refused, saying how much is free and how much it needs, before anything is written.

## What is in a backup

| Inside | Not inside |
|---|---|
| Every project's workspace, home folder, record, keys and ports. | The fetched Claude Code, pi and editor: a new installation fetches Claude Code and the editor at its first start, and pi when you pull it. |
| The tutor's database (copied so that a running tutor does not tear it), the Claude token and the tutor's keys. | What was installed in a sandbox outside its workspace and home folder: each project's [setup recipe](projects.md#what-a-sandbox-brings-back-by-itself) brings it back. |
| The password's hash. | The inner Docker's images: they are built again. |
| The page's certificate, and the versions you raised. | The other backups. |

**Keep a backup as you would keep the keys to everything.** It holds the password's hash, the Claude token if you set one, each project's Claude Code and pi logins, and the keys into every sandbox. It is not encrypted.

## Restoring a backup

Stop every project first (in its Config box, or with `docker exec clanker-learn cl stop <name>`), then:

```
docker exec clanker-learn cl restore cl-backup-<date>T<time>Z.tar.gz
```

A restore **replaces everything** clanker-learn keeps with what the backup holds: projects made since the backup are gone, and the password is the backup's. Before it changes anything it makes a backup of what is there, named in its answer, so a restore can be undone by restoring that one.

It refuses:

- while a project runs, while the tutor is writing an answer, and while a backup, a fetch of the software or an operation on a project is under way;
- a backup made by a newer clanker-learn;
- without room on the data volume for the archive unpacked and for the backup of what is there, saying how much is free and how much it needs, before anything is changed (it reads the archive through once to know its unpacked size).

Afterwards, log in again with the backup's password and start your projects: each one's container is made again, and its setup recipe runs once.

## Restoring onto a new installation

Start the new installation and wait for its [first start](getting-started.md#the-first-start) to finish, then copy the backup in and restore it:

```
docker cp cl-backup-<date>T<time>Z.tar.gz clanker-learn:/data/backups/
docker exec clanker-learn cl restore cl-backup-<date>T<time>Z.tar.gz
```

How it works: [Backups](../architecture/backups.md)
