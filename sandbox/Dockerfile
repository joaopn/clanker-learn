# The sandbox image: what every project's container starts from. Built by the core inside
# clanker-learn's container the first time a project needs it (outer/core.py), on a network of
# the inner daemon that the guard rules fence like a sandbox's.
#
# The base is pinned by digest; the packages come from that Debian release, the editor's
# server (code-server) from its release on GitHub, pinned by version and checksum. The agents
# are not here: each project gets its own copy in its home folder, which /opt/cl/deploy makes
# from the distributions the core mounts read-only at /opt/cl/dist.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

# What a learning environment starts from: a shell user's tools, Python and Node with their
# package managers, a compiler, sshd for the tutor's way in and the Shell's, tmux, which
# keeps the Shell's session when its page is closed, and jq, with which the deploy script writes
# Claude Code's model and effort into its settings. Recommended packages are left
# out. The host keys the package makes at install are removed: every sandbox gets its own from
# its provisioning folder.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates tini sudo openssh-server openssh-client \
        python3 python3-venv python3-pip nodejs npm build-essential \
        git curl jq less nano procps iproute2 iputils-ping tmux; \
    rm -rf /var/lib/apt/lists/*; \
    rm -f /etc/ssh/ssh_host_*

# The Editor: code-server, the browser VS Code, which the init starts in every sandbox. Its
# release's Debian package for this image's architecture (it depends on nothing), checked
# against the SHA-256 pinned here before it is installed: a download that does not match ends
# the build.
RUN set -eux; \
    version=4.126.0; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) sum=2dd4c57443931123282e829ed4b514c50dcabb097704be22e600d1a591664da8 ;; \
        arm64) sum=9bee520af84a2fbb98a3337110a782b343d3e822c105b4e6d723d8915f7725fb ;; \
        *) echo "no code-server package is pinned for $arch" >&2; exit 1 ;; \
    esac; \
    deb="/tmp/code-server_${version}_${arch}.deb"; \
    curl -fsSL -o "$deb" "https://github.com/coder/code-server/releases/download/v${version}/code-server_${version}_${arch}.deb"; \
    echo "$sum  $deb" | sha256sum -c -; \
    dpkg -i "$deb"; \
    rm -f "$deb"

# The sandbox user. It can become root: the tutor installs software, and the learner should be
# able to do what it did. Root here is root in a plain container.
RUN set -eux; \
    useradd --create-home --shell /bin/bash --uid 1000 learner; \
    echo 'learner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/learner; \
    chmod 0440 /etc/sudoers.d/learner; \
    mkdir -p /workspace /etc/cl-provision; \
    chown learner:learner /workspace

COPY sandbox_init.py files_server.py loopback_fwd.py /opt/cl/

# The deploy script (copies a distribution into the sandbox user's home), and `claude`, which
# runs the home's copy, so that a command over SSH that reads no login files finds it too.
# The distributions are mounted at /opt/cl/dist; the two links give RS's scripts their paths.
COPY deploy.sh /opt/cl/deploy
COPY claude.sh /usr/local/bin/claude
RUN chmod 0755 /opt/cl/deploy /usr/local/bin/claude; \
    ln -s /opt/cl/dist/agents /opt/agent-dist; \
    ln -s /opt/cl/dist/editor /opt/editor-dist

# `cl-expose <port>`: give a service that listens only on loopback a place on the sandbox's
# own address, so that the interface can show it.
RUN printf '#!/bin/sh\nexec python3 /opt/cl/loopback_fwd.py "$@"\n' > /usr/local/bin/cl-expose && chmod 0755 /usr/local/bin/cl-expose

WORKDIR /workspace
STOPSIGNAL SIGTERM
ENTRYPOINT ["tini", "--", "python3", "/opt/cl/sandbox_init.py"]
