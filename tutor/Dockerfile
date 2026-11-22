# The tutor service's image: a conversation per lesson, with Claude Code in the project's
# sandbox (tutor/server.py), as an inner container of clanker-learn. Built by the core in the
# inner daemon, on a network the guard rules fence, the first time clanker-learn starts with
# its web server on.
#
# The same pinned base as the other images; the packages come from that Debian release: aiohttp
# (the web server's framework), and the OpenSSH client, with which a turn reaches the sandbox.
# SQLite is Python's own.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tini python3 python3-aiohttp openssh-client; \
    rm -rf /var/lib/apt/lists/*

COPY server.py store.py sse.py tasks.py turn.py claude_stream.py schema.sql tutor.md /opt/cl/tutor/

WORKDIR /opt/cl/tutor
STOPSIGNAL SIGTERM
ENTRYPOINT ["tini", "--", "python3", "/opt/cl/tutor/server.py"]
