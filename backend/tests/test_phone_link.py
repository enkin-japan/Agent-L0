"""iPhone 连接（apple-app/Sources/App/PhoneLink.swift）的纯逻辑：扫码链接校验、tailnet 地址解析。

链接 macagent://pair 谁都能触发，所以 PairingLink 的拒收面是守卫；地址解析决定 iPhone
拿到的是不是本机后端的入口。真钥匙串与 tailscale CLI 都不碰。
"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def test_phone_link_logic(tmp_path):
    binary = tmp_path / "phone_link"
    compilation = subprocess.run([
        "xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
        str(ROOT / "apple-app/Sources/App/PhoneLink.swift"),
        str(ROOT / "apple-app/Sources/LiveActivity/ApprovalRouting.swift"),
        str(ROOT / "apple-app/Tests/phone_link_test.swift"), "-o", str(binary),
    ], capture_output=True, text=True, timeout=120)
    assert compilation.returncode == 0, compilation.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr


def test_wiring():
    sources = ROOT / "apple-app/Sources"
    app = (sources / "App/MacAgentApp.swift").read_text()
    settings = (sources / "App/AppSettings.swift").read_text()
    view = (sources / "Views/SettingsView.swift").read_text()
    # Deep link only stages the link; the claim happens from the confirmation button.
    assert "phoneLink.pendingLink = link" in app
    assert 'Button(L("确认连接")) { Task { await phoneLink.claim(link) } }' in app
    assert "Text(link.base.absoluteString + " in app   # full URL, not just the host
    # macOS always talks to the local backend and offers no address field.
    init_block = settings[settings.index("    init() {"):]
    mac = init_block[init_block.index("#if os(macOS)"):init_block.index("#else")]
    assert "baseURLString = Self.localBackend" in mac
    assert "UserDefaults" not in mac.split("baseURLString")[1].split("\n")[0]
    section = view[view.index("private var connectionSections"):view.index("Section(\"API Key\")")]
    assert section.index("#if os(iOS)") < section.index('Section(L("后端地址"))') < section.index("#endif")
    # iOS keeps the default keychain group first so existing items stay where they are.
    project = (ROOT / "apple-app/project.yml").read_text()
    assert ("- $(AppIdentifierPrefix)dev.enkin.macagent\n"
            "          - $(AppIdentifierPrefix)dev.enkin.macagent.shared") in project
