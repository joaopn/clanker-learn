# clanker-learn's one container: a Docker daemon of its own (the "inner" daemon), the guard
# rules that fence what that daemon runs, and, from later steps, the broker and the inner
# containers. It runs on sysbox, or privileged; see init.py.
# Built from the repository's root: docker build -f outer/Dockerfile .
# Debian 13: its crun (1.21) runs the containers of this Docker version. Debian 12's (1.8) is
# refused by it ("unknown version specified"), and crun is the runtime of the sysbox mode.
FROM debian:trixie-slim

# Docker and containerd are pinned; an upgrade is explicit: change the version, rebuild, run
# the tests. The rest (crun among it) comes from the Debian release named above.
ARG DOCKER_VERSION=29.5.3
ARG CONTAINERD_VERSION=2.4.2-4

# Docker's package signing key, by its checksum: a different key fails the build.
ADD --chmod=0644 --checksum=sha256:1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570 \
    https://download.docker.com/linux/debian/gpg /etc/apt/keyrings/docker.asc

# Recommended packages are left out; procps is named because `docker top` needs it, and the
# OpenSSH client because the core makes each project's keys with ssh-keygen.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" \
        > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        iptables iproute2 python3 tini crun busybox-static procps openssh-client \
        "docker-ce=5:${DOCKER_VERSION}-1~debian.13~trixie" \
        "docker-ce-cli=5:${DOCKER_VERSION}-1~debian.13~trixie" \
        "containerd.io=${CONTAINERD_VERSION}~debian.13~trixie"; \
    rm -rf /var/lib/apt/lists/*

# No ZFS tool may be on the image: containerd probes for it at every start, the probe takes
# ten seconds to fail in a container, and dockerd gives up waiting in about half the starts.
RUN if command -v zfs; then echo "a ZFS tool is on the image" >&2; exit 1; fi

# The root filesystem of the self-test image: one static binary. The init imports it into the
# inner daemon and runs it once at every start, so "a container runs" is checked without the
# network.
RUN mkdir -p /opt/cl/selftest/bin && cp /bin/busybox /opt/cl/selftest/bin/busybox

COPY outer/guard.py outer/init.py outer/core.py outer/broker.py outer/broker_auth.py /opt/cl/

# The versions of Claude Code and the editor that projects get, as the repository has them
# (/data/versions.local.env overrides them; see that file's header).
COPY versions.env /opt/cl/versions.env
# Claude's model tiers and the efforts each takes (research-sandbox's models/catalog.json).
COPY outer/models.json /opt/cl/models.json
# The editor's files from research-sandbox (its deploy script, lazy-start stub and settings),
# which the core puts into the editor's distribution when it builds one.
COPY outer/editor/ /opt/cl/editor/

# The definition of the sandbox image. The core builds it in the inner daemon the first time
# a project needs it, and tags it by a hash of these files.
COPY sandbox/ /opt/cl/sandbox/

# The definition of the web server's image, built (or loaded from /opt/cl/preload) the same way
# when the container is first ready.
COPY web/ /opt/cl/web/

# The definition of the tutor service's image, had the same way once the web server is up.
COPY tutor/ /opt/cl/tutor/

# `cl`: the core as a command, for someone with a shell in this container.
RUN printf '#!/bin/sh\nexec python3 /opt/cl/core.py "$@"\n' > /usr/local/bin/cl && chmod 0755 /usr/local/bin/cl

VOLUME ["/var/lib/docker", "/data"]
EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["tini", "--", "python3", "/opt/cl/init.py"]
