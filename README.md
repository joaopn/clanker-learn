# clanker-learn

A self-hosted tutor: you learn a technical subject by doing it in a real environment, with a tutor that knows what you already know. One container, no tool on the host.

It is being built. What exists so far is the outer container and, inside it, projects with their sandboxes and the broker that the web interface will talk to; there is no web interface yet.

## The outer container

`outer/` builds the one container clanker-learn runs in. It holds a Docker daemon of its own, which runs the sandboxes, and the guard rules that fence them. Build it from the repository's root:

```
docker build -t clanker-learn -f outer/Dockerfile .
```

Start it in one of two ways, and say which:

```
docker run -d --name clanker-learn --stop-timeout 60 --runtime=sysbox-runc -e CL_MODE=sysbox \
    -p 127.0.0.1:8080:8080 -v clanker-learn-docker:/var/lib/docker -v clanker-learn-data:/data clanker-learn

docker run -d --name clanker-learn --stop-timeout 60 --privileged -e CL_MODE=privileged \
    -p 127.0.0.1:8080:8080 -v clanker-learn-docker:/var/lib/docker -v clanker-learn-data:/data clanker-learn
```

Give it its own namespaces and its own init: no `--pid=host`, `--network=host`, `--cgroupns=host`, `--init` or other entrypoint, and no network shared with another container (`--network=container:…`). With them its rules and its daemon would act on the machine's own processes, network or control groups, so it refuses to start and says which one it found. Two limits:

- It tells `--network=host` by the bridges of the machine's Docker (`docker0`, `br-…`). On a machine whose Docker has none, or names its only bridge differently, the flag goes unnoticed. A network shared with another container is refused only when a Docker daemon's bridges are already in it, which usually means not at the first start and at every later one.
- On a machine with cgroup v1, Docker's default is the machine's control groups: add `--cgroupns=private` to the command.

On sysbox the container is confined from the host: a process that gets out of a sandbox into it is still not root on the machine. Privileged, the container is root-equivalent on the host: what then stands between a sandbox and the host is an ordinary container boundary and the guard rules. Only the privileged way is exercised by the tests so far; the sysbox way has not been run.

At every start the container applies the guard rules, starts its daemon, checks the rules, and runs one self-test container. If any of that fails it ends everything it started and says why: in `docker logs`, and as a 503 answer on port 8080. (Refused because its network is not its own, as with `--network=host`, it opens no port, because the port would be someone else's: `docker logs` says why.) It does the same if, while running, the rules are lost and cannot be put back, or the daemon dies.

## Projects and sandboxes

A project is one sandbox: a container of its own inside clanker-learn, on a network of its own, with a workspace (`/workspace`), a home folder, a user (`learner`) who can become root, an SSH server for the tutor to come in by, and a small server that shows the workspace's files on port 7070. A sandbox reaches the internet and nothing private: not clanker-learn's container, not another sandbox, not your network. It has no raw sockets: packet capture does not work in it; `ping` still does.

Until the web interface exists, projects are driven by a command inside the container:

```
docker exec clanker-learn cl create kafka --memory 4g --cpus 2
docker exec clanker-learn cl list
docker exec clanker-learn cl stop kafka
docker exec clanker-learn cl start kafka
docker exec clanker-learn cl limits kafka --memory 8g --pids none
docker exec clanker-learn cl recreate kafka
docker exec clanker-learn cl destroy kafka
```

- The first project on a new installation takes a few minutes: the sandbox image (`sandbox/`) is built then, once, and needs the internet.
- What you install in a sandbox is still there after `stop` and `start`, and after clanker-learn itself is stopped and started; the projects that were running come back by themselves.
- Limits apply at once. Two changes wait: removing a memory limit waits for the sandbox to be recreated, and on sysbox removing a processor limit waits for its next start. The command says so when it happens.
- `recreate` gives the project a new container from the current sandbox image. The workspace and the home folder stay; what was installed elsewhere in the old container is gone, and the setup recipe (below) brings it back. A stopped project is running afterwards.
- `destroy` removes the sandbox, its workspace and its home folder. If it is interrupted, run it again: a project being destroyed is never started.
- Everything of a project but its container is in the `/data` volume: workspaces under `projects/`, home folders under `homes/`.

### What a sandbox brings back by itself

Two files in the workspace, which the tutor will keep current and you can edit, say what a sandbox needs beyond its files:

- `/workspace/.learn/setup.sh`, the setup recipe: what was installed. It runs once in every new container, as the sandbox user (who can `sudo`), so after a `recreate` the software is back. It does not run at a plain stop and start, where nothing was lost. `cl status` says how it went (`setup`: none, running, done, failed), and its output is in `/var/log/cl/setup.log` in the sandbox. If the sandbox is stopped while it runs, it runs again from the top at the next start, so it must be safe to run twice. A recipe that failed is not tried again until the next `recreate`, and one that never ends holds the services back: there is no time limit on it.
- `/workspace/.learn/services`, what should be running: one line each, `name: command`. A name is lower-case letters, digits and dashes, starts with a letter or a digit and has at most 32 characters; other lines are skipped, with a line in the sandbox's log (`docker logs` of its container). At most 64 services and 64 KiB are read. They are started at every start of the sandbox, after the recipe, as the sandbox user, with their output in `/workspace/.learn/logs/<name>.log`. They are started, not watched: one that ends stays down until the next start. At a stop they are asked to end before they are killed.

A server that listens only on the sandbox's loopback address (many development and notebook servers do by default) cannot be reached from outside the sandbox, so the interface will show no tab for it. Inside the sandbox, `cl-expose 8888` puts it on the sandbox's own address; listed in the services file, that comes back too:

```
notebook: jupyter lab --port 8888
notebook-tab: cl-expose 8888
```

Use it only for a server that listens on loopback. Listed for a server that listens on all addresses, it takes the port on the sandbox's address first and the server then fails to start there.

## The broker

The broker is the door through which the web interface will ask for anything that touches projects. It is a process of clanker-learn's container, started once the container is ready, and it listens on a unix socket (`/run/cl/broker/broker.sock`) that no sandbox has. Nothing uses it yet; it is here so that the web server, when it comes, has one narrow place to ask.

- It knows a short, closed list of things to do (list, status, create, start, stop, recreate, limits, destroy, and how an operation stands). There is no way through it to the inner Docker itself.
- Listing the projects, one project's status and how an operation stands need nothing. Everything else needs a session, which a login with the password gives; destroying a project needs the password again.
- Set the password from the machine clanker-learn runs on. It is read from standard input, or asked for twice without echo at a terminal, and must have at least eight characters:

  ```
  docker exec -it clanker-learn cl passwd
  ```

  Setting it ends every session. What is stored is a hash, in the `/data` volume.
- Create, start, stop, recreate and destroy run in the background, one per project at a time; a second request for a project that is being worked on is answered "busy" at once. The `cl` command and the broker never work on one project at the same time.
- If the broker ends, clanker-learn starts it again; operations that were running go on. `http://127.0.0.1:8080/state.json` says `broker: up`.
- The `cl` command inside the container asks for no password: whoever can run it is root in the container already.

It speaks the protocol of RS's broker, so that RS's web server can be brought over with little change.

## Tests

```
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/pytest tests
.venv/bin/ruff check .
```

The tests marked `integration` start real containers and need a Docker daemon that can run a privileged one. They publish no port on the machine. Every container, network and named volume they create starts with `cl-test-` and is removed afterwards, each container together with the unnamed volumes the image gives it; the images they build, `cl-outer:test` and `cl-sandbox:<hash>`, are kept. One test lets clanker-learn build the sandbox image itself and takes a few minutes.
