#!/usr/bin/env python3
"""cl-expose — put a service that listens only on the sandbox's loopback on its own address.

    cl-expose <port> [<port> ...]

Many development and notebook servers listen on 127.0.0.1 by default, which nothing outside
the sandbox can reach, so the interface shows no tab for them. This relays the sandbox's own
address at the same port to the loopback service (127.0.0.1, else ::1), and the tab then
appears like any other. It runs entirely inside the sandbox, as whoever started it, and stays
until the sandbox stops; a line in /workspace/.learn/services brings it back at the next
start.

Taken from RS's loopback forwarder (cli/rs_loopback_fwd.py), which works the same way.

It binds the sandbox's specific address, never 0.0.0.0: that would collide with the app's own
127.0.0.1 listener. An app that already listens on all addresses holds the port there, so the
bind fails and the port is left alone: it is reachable already. SO_REUSEADDR is off on
purpose, so that the two cases never coexist by accident.
"""
import asyncio
import errno
import os
import select
import signal
import socket
import subprocess
import sys
import time

CHUNK = 65536
LOOPBACK_HOSTS = ("127.0.0.1", "::1")
REPORT_WAIT_S = 10


def own_address():
    """The sandbox's address on its network: Docker writes the hostname with it in /etc/hosts."""
    return socket.gethostbyname(socket.gethostname())


def ports_of(args):
    """The port numbers named, or None when one is not a port."""
    if not args or not all(a.isascii() and a.isdigit() and 0 < int(a) < 65536 for a in args):
        return None
    return [int(a) for a in args]


async def pump(reader, writer):
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def handle(client_reader, client_writer, port):
    for host in LOOPBACK_HOSTS:
        try:
            backend_reader, backend_writer = await asyncio.open_connection(host, port)
            break
        except OSError:
            continue  # nothing on this address; try the next
    else:
        client_writer.close()  # the app is not up (yet): drop this client, keep serving
        return
    await asyncio.gather(pump(client_reader, backend_writer), pump(backend_reader, client_writer))


async def relay(ports, report):
    """Bind each port on the sandbox's address, say what happened to each (relaying, held,
    failed), then serve."""
    try:
        address = own_address()
    except OSError as exc:
        for port in ports:
            report(f"failed {port} the sandbox's own address could not be found ({exc})")
        report("done")
        return
    servers = []
    for port in ports:
        def callback(p):
            async def on_client(reader, writer):
                await handle(reader, writer, p)
            return on_client
        try:
            servers.append(await asyncio.start_server(callback(port), host=address, port=port, reuse_address=False))
            report(f"relaying {port}")
        except OSError as exc:
            report(f"held {port}" if exc.errno == errno.EADDRINUSE else f"failed {port} {exc.strerror or exc}")
    report("done")
    if servers:
        await asyncio.gather(*(server.serve_forever() for server in servers))


def serve(ports):
    def report(line):
        try:
            print(line, flush=True)
        except OSError:
            pass  # whoever started this has gone
    try:
        asyncio.run(relay(ports, report))
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    foreground = args[:1] == ["--serve"]
    ports = ports_of(args[1:] if foreground else args)
    if ports is None:
        print("usage: cl-expose <port> [<port> ...]", file=sys.stderr)
        return 2
    if foreground:
        return serve(ports)
    # Detached, so that it outlives the shell or the agent's command that asked for it.
    child = subprocess.Popen([sys.executable, __file__, "--serve", *map(str, ports)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    status, done = told(child.stdout.fileno(), REPORT_WAIT_S)
    if not done:
        try:
            os.killpg(child.pid, signal.SIGKILL)  # it said nothing in time: it does not relay anything later either
        except ProcessLookupError:
            pass
    return status


def told(reports, wait_s, out=None, err=None):
    """Pass on what the detached relay reports for each port, read from the file descriptor
    `reports`. Returns the exit status (0 when every port is relayed or was held already; 1
    when one could not be, or the relay did not say it was done within `wait_s` seconds) and
    whether the relay said it was done."""
    out, err = out or sys.stdout, err or sys.stderr
    pending, failed, done, deadline = b"", False, False, time.monotonic() + wait_s
    while not done:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([reports], [], [], left)[0]:
            break
        chunk = os.read(reports, 4096)
        if not chunk:
            break  # the relay ended without saying it was done
        pending += chunk
        while b"\n" in pending and not done:
            line, pending = pending.split(b"\n", 1)
            kind, _, rest = line.decode(errors="replace").strip().partition(" ")
            port, _, reason = rest.partition(" ")
            if kind == "done":
                done = True
            elif kind == "relaying":
                print(f"port {port}: now reachable from outside the sandbox", file=out)
            elif kind == "held":
                print(f"port {port}: already taken on the sandbox's own address, so it is reachable already "
                      "(an app that listens on all addresses, or an earlier cl-expose)", file=out)
            else:
                failed = True
                print(f"port {port}: could not be relayed: {reason}", file=err)
    if not done:
        print("cl-expose: the relay did not start", file=err)
    return (1 if failed or not done else 0), done


if __name__ == "__main__":
    sys.exit(main())
