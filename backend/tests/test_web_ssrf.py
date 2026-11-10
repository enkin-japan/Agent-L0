"""抓取护栏 `_ssrf_ok` 金测试（build 85 B2）。

**病根**：100.64.0.0/10（RFC 6598 CGNAT；Tailscale 的 tailnet 地址段）在 Python 3.13 的
ipaddress 里 is_private / is_reserved 都是 False，原判定把它当公网放行。本机后端与算力端
正走 tailnet，web_read / web_research 的免审批抓取因此够得到内网机器。

桩点只在网络出口 `socket.getaddrinfo`（返回指定地址）——`_ssrf_ok` 本体真实执行，
上下边界逐个钉死，既有拦截类别各留一条回归。
"""
import socket

import pytest

from framework.agents import web_tools


def _resolve_to(monkeypatch, *ips):
    """getaddrinfo 替身：主机名一律解析为给定地址（可多条，模拟多 A/AAAA 记录）。"""
    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        out = []
        for ip in ips:
            if ":" in ip:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return calls


# ==========================================================================
# 1. 100.64.0.0/10：段内拦、段外放（上下边界各一条）
# ==========================================================================

@pytest.mark.parametrize("ip", ["100.64.0.0", "100.64.0.1", "100.100.100.100",
                                "100.101.102.103", "100.127.255.255"])
def test_cgnat_tailnet_range_is_blocked(monkeypatch, ip):
    calls = _resolve_to(monkeypatch, ip)
    ok, reason = web_tools._ssrf_ok("http://some-host.example/x")
    assert calls == ["some-host.example"]      # 真的走了解析出口
    assert ok is False
    assert ip in reason and "SSRF blocked" in reason


@pytest.mark.parametrize("ip", ["100.63.255.255", "100.128.0.0"])
def test_addresses_just_outside_cgnat_pass(monkeypatch, ip):
    _resolve_to(monkeypatch, ip)
    assert web_tools._ssrf_ok("https://some-host.example/") == (True, "")


def test_cgnat_literal_ip_url_is_blocked(monkeypatch):
    """直接写 IP 的 URL（模型最可能的形态：http://100.x.x.x:14161/...）同样经解析判定。"""
    _resolve_to(monkeypatch, "100.101.102.103")
    ok, _ = web_tools._ssrf_ok("http://100.101.102.103:14161/v1/models")
    assert ok is False


def test_ipv4_mapped_cgnat_is_blocked(monkeypatch):
    """IPv4 映射形态（::ffff:100.64.0.1）是同一目标，不得换个写法绕过。"""
    _resolve_to(monkeypatch, "::ffff:100.64.0.1")
    assert web_tools._ssrf_ok("http://some-host.example/")[0] is False


def test_tailscale_ipv6_range_is_blocked(monkeypatch):
    """Tailscale IPv6 段 fd7a:115c:a1e0::/48 属 ULA fc00::/7，既有 is_private 已覆盖——钉住它。"""
    _resolve_to(monkeypatch, "fd7a:115c:a1e0::1")
    assert web_tools._ssrf_ok("http://some-host.example/")[0] is False


def test_mixed_public_and_cgnat_records_are_blocked(monkeypatch):
    """多条解析记录里只要有一条落在 100.64/10 就拒（连接可能落到任一条上）。"""
    _resolve_to(monkeypatch, "93.184.216.34", "100.100.100.100")
    ok, reason = web_tools._ssrf_ok("https://dual.example/")
    assert ok is False and "100.100.100.100" in reason


# ==========================================================================
# 2. 既有类别回归：环回 / 私网 / 云元数据拦，公网放
# ==========================================================================

@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254"])
def test_existing_non_public_categories_still_blocked(monkeypatch, ip):
    _resolve_to(monkeypatch, ip)
    ok, reason = web_tools._ssrf_ok("http://some-host.example/")
    assert ok is False and ip in reason


def test_public_address_passes(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    assert web_tools._ssrf_ok("https://example.com/page") == (True, "")


def test_non_http_scheme_rejected_before_resolution(monkeypatch):
    calls = _resolve_to(monkeypatch, "93.184.216.34")
    ok, _ = web_tools._ssrf_ok("file:///etc/passwd")
    assert ok is False and calls == []


# ==========================================================================
# 3. fail-closed：解析出 ipaddress 判不了的地址串 → 拦（G9）
# ==========================================================================

def test_unparseable_resolved_address_is_blocked(monkeypatch):
    """此前 ValueError → continue（fail-open）：判不了的地址被跳过，只剩它时整条放行。"""
    _resolve_to(monkeypatch, "not-an-ip")
    ok, reason = web_tools._ssrf_ok("http://weird.example/")
    assert ok is False and "not-an-ip" in reason


def test_unparseable_address_among_public_ones_is_blocked(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34", "1.2.3.4%x")
    assert web_tools._ssrf_ok("http://weird.example/")[0] is False


def test_scoped_ipv6_link_local_is_blocked(monkeypatch):
    """getaddrinfo 可能回带 zone 的链路本地地址串；3.13 的 ipaddress 能解析它（scope_id=en0），
    按 is_link_local 拦——不依赖 fail-closed 分支。"""
    _resolve_to(monkeypatch, "fe80::1%en0")
    ok, reason = web_tools._ssrf_ok("http://link-local.example/")
    assert ok is False and "non-public" in reason
