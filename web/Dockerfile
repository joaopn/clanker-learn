# The web server's image: what the browser talks to (web/server.py), as an inner container
# of clanker-learn. Built by the core in the inner daemon, on a network the guard rules
# fence, the first time clanker-learn starts with its web server on.
#
# The same pinned base as the sandbox image; the packages come from that Debian release, so
# nothing is fetched from anywhere but Debian's archive.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tini python3 python3-aiohttp python3-cryptography python3-asyncssh; \
    rm -rf /var/lib/apt/lists/*

COPY server.py services.py /opt/cl/

WORKDIR /opt/cl
STOPSIGNAL SIGTERM
ENTRYPOINT ["tini", "--", "python3", "/opt/cl/server.py"]
