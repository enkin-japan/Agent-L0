"""Reach tailnet MagicDNS names even while the system resolver caches a miss.

The Colab compute VM keeps one fixed name (agent-compute.<tailnet>.ts.net) across
rebuilds, and the name does not exist between two VMs. On macOS a lookup during
that gap can leave mDNSResponder serving "no such host" long after the new VM has
registered (observed 2026-10-24: over 20 minutes, while 100.100.100.100 already
answered with the new address). Flushing that cache needs sudo, so instead a
failed system lookup of a configured compute name is retried once against
Tailscale's own resolver. Only the names passed to install() qualify (and only
`*.ts.net` ones): other tailnet peers stay exactly as reachable as the OS resolver
makes them, so tools such as web fetch gain no new destinations. Successful system
lookups and IPv6-only requests are untouched; when Tailscale is not running the
original error is raised as before.

Standard library only: rag/rag_server.py loads this file by path as well.
"""
import random
import socket
import struct

MAGICDNS = ("100.100.100.100", 53)
SUFFIX = ".ts.net"


def query_a(host, server=MAGICDNS, timeout=0.5):
    """IPv4 addresses `server` returns for `host`; [] on any failure."""
    ident = random.getrandbits(16)
    try:
        labels = host.rstrip(".").encode("idna").split(b".")
        question = b"".join(bytes([len(label)]) + label for label in labels) + b"\0"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect(server)
            sock.send(struct.pack("!6H", ident, 0x0100, 1, 0, 0, 0) + question
                      + struct.pack("!2H", 1, 1))
            reply = sock.recv(2048)
        return _answers(reply, ident)
    except (OSError, UnicodeError, ValueError, struct.error, IndexError):
        return []


def _answers(reply, ident):
    rid, flags, qdcount, ancount = struct.unpack("!4H", reply[:8])
    if rid != ident or not flags & 0x8000 or flags & 0x000F:  # not our reply / rcode != 0
        return []
    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(reply, offset) + 4
    addresses = []
    for _ in range(ancount):
        offset = _skip_name(reply, offset)
        rtype, rclass, _ttl, length = struct.unpack("!2HIH", reply[offset:offset + 10])
        offset += 10
        if rtype == 1 and rclass == 1 and length == 4:
            addresses.append(socket.inet_ntoa(reply[offset:offset + 4]))
        offset += length
    return addresses


def _skip_name(reply, offset):
    while True:
        length = reply[offset]
        if length & 0xC0 == 0xC0:  # compression pointer ends the name
            return offset + 2
        if length == 0:
            return offset + 1
        offset += 1 + length


_names = set()


def install(hosts, server=MAGICDNS):
    """Give `hosts` the MagicDNS fallback; wraps socket.getaddrinfo once per process.

    Async clients only pass through it on the stdlib asyncio loop: uvloop resolves
    in libuv, so a server using this must run uvicorn with loop="asyncio".
    """
    _names.update(str(h).strip().rstrip(".").lower() for h in hosts
                  if str(h or "").strip().rstrip(".").lower().endswith(SUFFIX))
    system = socket.getaddrinfo
    if not _names or getattr(system, "tailnet_fallback", False):
        return

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        try:
            return system(host, port, family, type, proto, flags)
        except socket.gaierror:
            if isinstance(host, (bytes, bytearray)):
                host = bytes(host).decode("ascii", "replace")  # anyio passes IDNA bytes
            if (family == socket.AF_INET6 or not isinstance(host, str)
                    or host.rstrip(".").lower() not in _names):
                raise
            addresses = query_a(host, server)
            if not addresses:
                raise
            flags |= socket.AI_NUMERICHOST
            return [info for ip in addresses
                    for info in system(ip, port, family, type, proto, flags)]

    getaddrinfo.tailnet_fallback = True
    socket.getaddrinfo = getaddrinfo
