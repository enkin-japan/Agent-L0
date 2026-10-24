"""tailnet_dns 金测试（2026-10-24 算力端换 VM 后 app 一直「计算端未就绪」案）。

守的是什么：系统解析器对已配置的算力端 `*.ts.net` 名报「无此主机」时，改问 Tailscale 自身
解析器并按其答案连接；成功的系统解析、未配置的名字（含其他 tailnet 节点）、纯 IPv6 请求、
Tailscale 不在时一律保持原行为。
假 DNS 服务器跑在本机回环 UDP 上，答案走真实报文编解码（含名称压缩指针）。
"""
import asyncio
import re
import socket
import struct
import threading
from pathlib import Path

import pytest

from framework.utils import tailnet_dns

NAME = "agent-compute.example.ts.net"
OTHER_PEER = "laptop.example.ts.net"
REPO = Path(__file__).resolve().parents[2]


class FakeResolver:
    """One-shot-per-query UDP DNS server answering A queries from a table."""

    def __init__(self, table, rcode=0, wrong_id=False):
        self.table, self.rcode, self.wrong_id = table, rcode, wrong_id
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.address = self.sock.getsockname()
        self.queries = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                packet, peer = self.sock.recvfrom(2048)
            except OSError:
                return
            ident = struct.unpack("!H", packet[:2])[0] ^ (1 if self.wrong_id else 0)
            labels, offset = [], 12
            while packet[offset]:
                labels.append(packet[offset + 1:offset + 1 + packet[offset]].decode())
                offset += 1 + packet[offset]
            question = packet[12:offset + 5]
            name = ".".join(labels)
            self.queries.append(name)
            ips = self.table.get(name, [])  # an error rcode still carries them: must be ignored
            rcode = self.rcode or (0 if ips else 3)
            answers = b"".join(b"\xc0\x0c" + struct.pack("!2HIH", 1, 1, 5, 4) + socket.inet_aton(ip)
                               for ip in ips)
            # An unrelated record type first: the parser must skip it by length.
            if ips:
                answers = b"\xc0\x0c" + struct.pack("!2HIH", 16, 1, 5, 3) + b"\x02ok" + answers
            header = struct.pack("!6H", ident, 0x8180 | rcode, 1, len(ips) + bool(ips), 0, 0)
            self.sock.sendto(header + question + answers, peer)

    def close(self):
        self.sock.close()


@pytest.fixture()
def resolver():
    servers = []

    def make(*args, **kwargs):
        servers.append(FakeResolver(*args, **kwargs))
        return servers[-1]
    yield make
    for server in servers:
        server.close()


@pytest.fixture(autouse=True)
def _fresh_names(monkeypatch):
    monkeypatch.setattr(tailnet_dns, "_names", set())


@pytest.fixture()
def stale_system(monkeypatch):
    """System resolver with a cached miss for NAME; IP literals resolve normally."""
    real = socket.getaddrinfo
    calls = []

    def system(host, port, family=0, type=0, proto=0, flags=0):
        calls.append((host, flags))
        if host in (NAME, NAME.encode(), OTHER_PEER):
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
        return real(host, port, family, type, proto, flags)
    monkeypatch.setattr(socket, "getaddrinfo", system)
    return calls


def test_query_parses_real_packets_and_fails_soft(resolver):
    server = resolver({NAME: ["100.64.0.7", "100.64.0.8"]})
    assert tailnet_dns.query_a(NAME, server.address) == ["100.64.0.7", "100.64.0.8"]
    assert tailnet_dns.query_a("absent.example.ts.net", server.address) == []  # NXDOMAIN
    assert tailnet_dns.query_a(NAME, resolver({NAME: ["100.64.0.7"]}, rcode=2).address) == []  # SERVFAIL
    assert tailnet_dns.query_a(NAME, resolver({NAME: ["100.64.0.7"]}, wrong_id=True).address) == []
    closed = resolver({})
    closed.close()
    assert tailnet_dns.query_a(NAME, closed.address, timeout=0.2) == []         # nobody listening


def test_cached_miss_of_tailnet_name_is_answered_by_magicdns(resolver, stale_system):
    server = resolver({NAME: ["100.64.0.7"]})
    tailnet_dns.install([NAME + ".", None, ""], server.address)
    infos = socket.getaddrinfo(NAME, 14161, 0, socket.SOCK_STREAM)
    assert {info[4][:2] for info in infos} == {("100.64.0.7", 14161)}
    assert server.queries == [NAME]
    # The answer is connected to as a literal, never looked up again by name.
    assert stale_system[-1] == ("100.64.0.7", socket.AI_NUMERICHOST)
    wrapper = socket.getaddrinfo
    tailnet_dns.install([NAME], server.address)  # idempotent: no second wrapper layer
    assert socket.getaddrinfo is wrapper


def test_bytes_host_from_anyio_takes_the_same_path(resolver, stale_system):
    # anyio hands loop.getaddrinfo IDNA-encoded bytes; that is the production path.
    tailnet_dns.install([NAME], resolver({NAME: ["100.64.0.7"]}).address)
    assert socket.getaddrinfo(NAME.encode(), 80)[0][4][0] == "100.64.0.7"
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(b"\xff\xfe.example.ts.net", 80)


def test_unconfigured_tailnet_peer_gains_no_route(resolver, stale_system):
    # web fetch shares this process: other tailnet names must not become reachable.
    server = resolver({OTHER_PEER: ["100.64.0.9"]})
    tailnet_dns.install([NAME, "compute.example.com"], server.address)
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(OTHER_PEER, 80)
    assert server.queries == []


def test_everything_else_keeps_system_behaviour(resolver, stale_system):
    server = resolver({"other.example.com": ["100.64.0.9"]})
    tailnet_dns.install([NAME], server.address)
    assert socket.getaddrinfo("127.0.0.1", 80)[0][4][0] == "127.0.0.1"   # system success untouched
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(NAME, 80, socket.AF_INET6)                     # IPv6-only request
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(NAME, 80)                                      # MagicDNS has no answer
    assert server.queries == [NAME]


def test_non_tailnet_name_never_reaches_magicdns(resolver, monkeypatch):
    server = resolver({"other.example.com": ["100.64.0.9"]})

    def system(host, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "no")
    monkeypatch.setattr(socket, "getaddrinfo", system)
    tailnet_dns.install(["other.example.com"], server.address)   # not a MagicDNS name
    assert not getattr(socket.getaddrinfo, "tailnet_fallback", False)
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("other.example.com", 80)
    assert server.queries == []


def test_asyncio_loop_resolves_through_the_fallback(resolver, stale_system):
    # The compute probe is an httpx AsyncClient; it reaches socket.getaddrinfo only on
    # the stdlib loop, which is why backend/app.py runs uvicorn with loop="asyncio".
    tailnet_dns.install([NAME], resolver({NAME: ["100.64.0.7"]}).address)
    infos = asyncio.run(_loop_getaddrinfo(NAME))
    assert {info[4][0] for info in infos} == {"100.64.0.7"}


async def _loop_getaddrinfo(host):
    return await asyncio.get_running_loop().getaddrinfo(host, 14161, type=socket.SOCK_STREAM)


def test_both_sidecars_install_it_where_their_clients_resolve():
    # Code lines only: the comments around these calls name the same words.
    app = (REPO / "backend/app.py").read_text(encoding="utf-8")
    assert re.search(r'^tailnet_dns\.install\(\[.*"remote".*\n.*"compute"', app, re.M)
    assert re.search(r'^    uvicorn\.run\(app,[^#\n]*\bloop="asyncio"', app, re.M)
    rag = (REPO / "rag/rag_server.py").read_text(encoding="utf-8")
    assert re.search(r'^            "tailnet_dns", Path\(__file__\).*"backend/framework/utils/tailnet_dns\.py"\)$',
                     rag, re.M)
    # Advisory only: a missing file must not stop the RAG data plane from starting.
    assert re.search(r"^    try:\n(?:        .*\n)+?        _tailnet_dns\.install\(\[COMPUTE_HOST\]\)\n"
                     r"    except \(OSError, AttributeError\)", rag, re.M)
    # rag_server loads the file by path relative to itself (repo and app bundle alike).
    assert (REPO / "rag").resolve().parent.joinpath("backend/framework/utils/tailnet_dns.py").exists()
