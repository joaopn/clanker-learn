# clanker-learn

A self-hosted tutor: you learn a technical subject by doing it in a real environment, with a tutor that knows what you already know. One container, no tool on the host.

It is being built; what exists so far is the outer container.

## The outer container

`outer/` builds the one container clanker-learn runs in. It holds a Docker daemon of its own, which will run the sandboxes, and the guard rules that fence them.

```
docker build -t clanker-learn outer
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

## Tests

```
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/pytest tests
.venv/bin/ruff check .
```

The tests marked `integration` start real containers and need a Docker daemon that can run a privileged one. They publish no port on the machine. Every container, network and named volume they create starts with `cl-test-` and is removed afterwards, each container together with the unnamed volumes the image gives it; the image they build, `cl-outer:test`, is kept.
