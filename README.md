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

Give it its own namespaces and its own init: no `--pid=host`, `--network=host`, `--cgroupns=host`, `--init` or other entrypoint. It refuses the ones it can tell (the process namespace and the init); the other two it cannot tell, and with them its rules and its daemon would act on the machine's own network and control groups.

On sysbox the container is confined from the host: a process that gets out of a sandbox into it is still not root on the machine. Privileged, the container is root-equivalent on the host: what then stands between a sandbox and the host is an ordinary container boundary and the guard rules. Only the privileged way is exercised by the tests so far; the sysbox way has not been run.

At every start the container applies the guard rules, starts its daemon, checks the rules, and runs one self-test container. If any of that fails it ends everything it started and says why: in `docker logs`, and as a 503 answer on port 8080. It does the same if, while running, the rules are lost and cannot be put back, or the daemon dies.

## Tests

```
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/pytest tests
.venv/bin/ruff check .
```

The tests marked `integration` start real containers and need a Docker daemon that can run a privileged one. They publish no port on the machine. Every container, network and named volume they create starts with `cl-test-` and is removed afterwards, each container together with the unnamed volumes the image gives it; the image they build, `cl-outer:test`, is kept.
