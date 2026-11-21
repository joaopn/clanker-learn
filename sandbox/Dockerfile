# The sandbox image: what every project's container starts from. Built by the core inside
# clanker-learn's container the first time a project needs it (outer/core.py), on a network of
# the inner daemon that the guard rules fence like a sandbox's.
#
# The base is pinned by digest; the packages come from that Debian release. The agents are not
# here: they are distributed into running sandboxes (a later part). Nor is the editor's server
# yet.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

# What a learning environment starts from: a shell user's tools, Python and Node with their
# package managers, a compiler, and sshd for the tutor's way in. Recommended packages are left
# out. The host keys the package makes at install are removed: every sandbox gets its own from
# its provisioning folder.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates tini sudo openssh-server openssh-client \
        python3 python3-venv python3-pip nodejs npm build-essential \
        git curl less nano procps iproute2 iputils-ping; \
    rm -rf /var/lib/apt/lists/*; \
    rm -f /etc/ssh/ssh_host_*

# The sandbox user. It can become root: the tutor installs software, and the learner should be
# able to do what it did. Root here is root in a plain container.
RUN set -eux; \
    useradd --create-home --shell /bin/bash --uid 1000 learner; \
    echo 'learner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/learner; \
    chmod 0440 /etc/sudoers.d/learner; \
    mkdir -p /workspace /etc/cl-provision; \
    chown learner:learner /workspace

COPY sandbox_init.py files_server.py /opt/cl/

WORKDIR /workspace
STOPSIGNAL SIGTERM
ENTRYPOINT ["tini", "--", "python3", "/opt/cl/sandbox_init.py"]
