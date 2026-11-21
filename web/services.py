"""Pure helpers of the web server: its listen address, its certificate's renewal rules, its
cookie names. From RS's webui/services.py (RS commit 62788a8), which holds much more; these
are the ones this server uses so far, kept as RS has them.
"""
import ipaddress
import socket
from datetime import UTC, datetime, timedelta

# Any port will do: a UDP socket's connect() only asks the kernel for a route.
LISTEN_PROBE_PORT = 9


def listen_address(subnet: str) -> str:
    """The container's own address inside `subnet` (IPv4 CIDR, no host bits),
    found as the source the kernel picks toward the subnet's first host. A
    missing, malformed or IPv6 subnet, no route at all, or a route that leaves
    by another network (the container is not attached to that subnet) raises
    ValueError naming the subnet — the caller stops the web server with its remedy."""
    if not subnet:
        raise ValueError("no listen subnet given (CL_LISTEN_SUBNET is empty)")
    try:
        net = ipaddress.ip_network(subnet, strict=True)
    except ValueError as e:
        raise ValueError(f"listen subnet {subnet!r} is not a network: {e}") from None
    if net.version != 4:
        raise ValueError(f"listen subnet {subnet} is not IPv4")
    first = next(net.hosts(), None) if net.num_addresses > 1 else net.network_address
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((str(first), LISTEN_PROBE_PORT))
            src = s.getsockname()[0]
    except OSError as e:
        raise ValueError(f"no route toward the listen subnet {subnet} ({e})") from None
    if ipaddress.ip_address(src) not in net:
        raise ValueError(f"this container has no address in the listen subnet "
                         f"{subnet} (the route there leaves from {src})")
    return src


# How long before its expiry the certificate counts as due for renewal: someone who
# restarts clanker-learn at least monthly never meets an expired one.
CERT_RENEW_BEFORE = timedelta(days=30)


def cert_expiry_state(not_after: datetime | None, now: datetime) -> str:
    """Classify a certificate's notAfter against `now`: "unknown" (unreadable),
    "expired", "renew-due" (less than CERT_RENEW_BEFORE left) or "ok". A naive
    datetime is read as UTC (cryptography's legacy accessor returns naive UTC)."""
    if not_after is None:
        return "unknown"
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if not_after <= now:
        return "expired"
    if not_after - now < CERT_RENEW_BEFORE:
        return "renew-due"
    return "ok"


def self_signed_regen_reason(files_present: bool, covers_bind: bool,
                             expiry: str) -> str | None:
    """Why the web server must regenerate its self-signed certificate, or None to
    keep it. Missing files first, then the expiry, then the bind address."""
    if not files_present:
        return "no certificate yet"
    if expiry == "expired":
        return "certificate expired"
    if expiry == "renew-due":
        return f"certificate expires within {CERT_RENEW_BEFORE.days} days"
    if expiry == "unknown":
        return "certificate expiry unreadable"
    if not covers_bind:
        return "certificate does not cover the bind address"
    return None


def broker_cookie_name(web_port: int) -> str:
    """The login session's cookie name for the web server published on ``web_port``.
    Cookies are keyed by host and ignore the port, so the port is in the name: a second
    web server on the same host (RS's, say) keeps its own."""
    return f"cl_broker_{int(web_port)}"


def session_cookie_name(web_port: int, project: str) -> str:
    """The cookie that opens ``project``'s web apps (used from 1c-2 on)."""
    return f"cl_session_{int(web_port)}_{project}"
