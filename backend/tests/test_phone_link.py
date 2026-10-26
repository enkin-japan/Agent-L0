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
    # iPhone connects only through the iCloud Mac list or a scanned pairing QR (2026-10-27):
    # neither the iOS settings page nor the iOS onboarding offers an address / key entry.
    section = view[view.index("private var connectionSections"):]
    section = section[:section.index("\n    }\n")]
    ios = section[section.index("#if os(iOS)"):section.index("#else")]
    assert "PhoneConnectionSection()" in ios
    body = view[view.index("    var body: some View {"):]
    ios_form = body[body.index("#else\n            Form {"):body.index('.navigationTitle(L("连接设置"))')]
    onboarding = (sources / "Views/OnboardingView.swift").read_text()
    ios_onboarding = onboarding[onboarding.index("private var iosContent"):]
    links = (sources / "Views/PhoneLinkViews.swift").read_text()
    phone_section = links[links.index("struct PhoneConnectionSection"):]
    # The rename alert's name field is the one text input the shared Mac list may have.
    rename_field = 'TextField(L("名称"), text: $newName)'
    assert phone_section.count(rename_field) == 1
    phone_section = phone_section.replace(rename_field, "")
    for manual_entry in ("TextField", "SecureField", "后端地址", "API Key", "AGENT_API_KEY", "测试连接", "test()"):
        for name, scanned in (("settings connectionSections", ios), ("settings form", ios_form),
                              ("onboarding", ios_onboarding), ("PhoneConnectionSection", phone_section)):
            assert manual_entry not in scanned, (name, manual_entry)
    # Onboarding keeps an exit for a phone with no reachable Mac yet.
    assert 'Button(L("稍后配对")) { pairLater() }' in ios_onboarding
    assert "settings.setupCompleted = true" in ios_onboarding
    assert "手动填入" not in onboarding
    for source in ("Views/SettingsView.swift", "Views/OnboardingView.swift", "App/PhoneLink.swift"):
        assert "saveManual" not in (sources / source).read_text(), source
    # macOS: the AGENT key field (and its .env.local caption) only where the user must paste it.
    key_section = view[view.index("private var agentKeySection"):view.index("private var notificationSection")]
    predicate = view[view.index("private var agentKeyManagedByApp"):]
    predicate = predicate[:predicate.index("\n    }\n")]
    # A peer that reports repo or "unknown" (no AGENT_LAYOUT) keeps the field visible.
    assert 'sidecar.peerLayout == nil || sidecar.peerLayout == "bundle"' in predicate
    managed, pasted = key_section.split("} else {", 1)
    assert "if agentKeyManagedByApp {" in managed and 'L("后端密钥已自动生成并同步")' in managed
    assert "SecureField" not in managed and "copyKey()" not in managed
    assert 'SecureField(L("AGENT_API_KEY（存入钥匙串）")' in pasted and ".env.local" in pasted
    assert "regenerateKey()" not in pasted
    # iOS keeps the default keychain group first so existing items stay where they are.
    project = (ROOT / "apple-app/project.yml").read_text()
    assert ("- $(AppIdentifierPrefix)dev.enkin.macagent\n"
            "          - $(AppIdentifierPrefix)dev.enkin.macagent.shared") in project
