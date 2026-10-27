"""iPhone 连接（apple-app/Sources/App/PhoneLink.swift）的纯逻辑：扫码链接校验、tailnet 地址解析。

链接 macagent://pair 谁都能触发，所以 PairingLink 的拒收面是守卫；地址解析决定 iPhone
拿到的是不是本机后端的入口。真钥匙串与 tailscale CLI 都不碰。
"""
from pathlib import Path
import re
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
    _assert_unpair_wiring(sources, phone_section, app)
    _assert_probe_tick_wiring(sources)
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


def _code(text):
    """Comments out, whitespace collapsed: the pins below match code tokens, not layout or wording."""
    return re.sub(r"\s+", " ", re.sub(r"//[^\n]*", "", text))


def _body(code, signature):
    """Brace-matched body of the first declaration matching `signature` (a regex on collapsed code)."""
    match = re.search(signature, code)
    assert match, signature
    start = code.index("{", match.end() - 1)
    depth = 0
    for i in range(start, len(code)):
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        if depth == 0:
            return code[start + 1:i]
    raise AssertionError(signature)


def _has(code, *tokens):
    """The tokens appear in order, any whitespace between them."""
    return re.search(r"\s*".join(re.escape(token) for token in tokens), code) is not None


def _assert_unpair_wiring(sources, phone_section, app):
    """build 78: 取消配对 instead of hiding a Mac; iCloud makes a Mac discoverable, pairing is a tap."""
    phone = _code((sources / "App/PhoneLink.swift").read_text())
    # Nothing hides a Mac any more: the old identifiers survive only in the line deleting the old key.
    cleanup = 'defaults.removeObject(forKey: "phone.hiddenMacs")'
    assert phone.count(cleanup) == 1
    assert not re.search(r"hiddenMacs|hideKey|hiddenKey", phone.replace(cleanup, ""))
    assert _has(phone, "static func merge(_ macs: [SavedMac], synced: [PhoneConnection], pinned: Set<String>,",
                "now: Date, newId: () -> String) -> Merged")
    # remove() delegates every decision to MacDirectory.unpair and never drops or hides the row itself.
    remove = _body(phone, r"func remove\(_ id: String\)")
    assert _has(remove, "let plan = MacDirectory.unpair(macs, id: id)") and _has(remove, "if plan.forgetSelf")
    assert _has(remove, "for account in plan.clearedAccounts {", 'Keychain.save(account, "")', "}")
    assert _has(remove, "macs = plan.macs") and _has(remove, "readSynced()")
    assert not re.search(r"removeAll|\.insert\(|Keychain\.save\(Self\.", remove)
    # readSynced: pinned rows (current, being paired) are never dropped; dropped rows lose ticket and key;
    # the 75 shared key stays in memory and is never written as a local credential.
    read = _body(phone, r"func readSynced\(\)")
    pinned = re.search(r"let pinned = Set\(\[([^\]]*)\]", read)
    assert pinned and {part.strip() for part in pinned.group(1).split(",")} == {
        "currentId", "handshake?.macId", "legacyUpgradeId", "requesting?.id"}
    assert _has(read, "MacDirectory.merge(macs, synced: items, pinned: pinned,")
    dropped = _body(read, r"for id in merged\.dropped")
    assert _has(dropped, "Keychain.save(Self.ticketAccount(id),", '"")')
    assert _has(dropped, "Keychain.save(Self.keyAccount(id),", '"")')
    assert "keyAccount" not in read.replace(dropped, "")
    assert _has(read, "legacyKeys = merged.legacyKeys")
    # Only an explicit tap in the Mac list may pair; a notification only switches to a usable Mac.
    assert _has(_body(phone, r"func select\(_ id: String"), "requestOrUpgrade(id)")
    assert _has(_body(phone, r"func retryHandshake\(_ id: String\)"), "requestOrUpgrade(id)")
    switch = _body(phone, r"func switchWithoutPairing\(_ id: String\)")
    assert _has(switch, "!MacDirectory.needsHandshake(") and _has(switch, "else { return }")
    assert not re.search(r"requestOrUpgrade|requestConnection|upgradeLegacy|select\(", switch)
    push = _code((sources / "App/PushManager.swift").read_text())
    open_session = _body(push, r"func openSession\(")
    assert _has(open_session, "phoneLink.switchWithoutPairing(id)") and "phoneLink.select(" not in open_session
    route = _body(phone, r"private func requestOrUpgrade\(_ id: String\)")
    assert _has(route, "guard legacyUpgradeId == nil")
    assert _has(route, "MacDirectory.pairingRoute(credential: mac.credential, hasKey: !key(for: id).isEmpty,",
                "legacyKey: legacyKeys[id])")
    assert _has(route, "case .legacyUpgrade(let key): upgradeLegacy(id, key: key)")
    assert _has(route, "case .request: requestConnection(id)")
    # The upgrade shows the waiting state, runs once, is never cancelled, and stores only the device token.
    legacy = _body(phone, r"private func upgradeLegacy\(_ id: String, key: String\)")
    assert _has(legacy, "guard legacyUpgradeId == nil") and _has(legacy, "legacyUpgradeId = id")
    assert _has(legacy, 'handshake = Handshake(macId: id, macName: mac.name, code: "", state: .waiting)')
    assert _has(legacy, "PairingAPI.upgrade(base: base, key: key") and _has(legacy, "self?.adopt(id, token: issued.token")
    assert "handshakeTask =" not in legacy and "Keychain" not in legacy
    assert _has(_code(app), "case .waiting where handshake.code.isEmpty:")
    # The iOS list: paired rows swipe to 取消配对, unpaired rows have no swipe; both badges shown.
    view = _code(phone_section)
    assert ".onDelete" not in view and 'L("删除")' not in view and view.count('L("取消配对")') == 1
    assert _has(view, ".swipeActions(edge: .trailing) {", "if mac.pairingState == .paired {",
                'Button(L("取消配对"), role: .destructive) {', "phoneLink.remove(mac.id)", "}", "}", "}")
    assert re.search(r'switch mac\.pairingState \{ case \.paired: Text\(L\("已配对"\)\)[^}]*?'
                     r'\.foregroundStyle\(\.tint\)[^}]*?case \.unpaired: Text\(L\("未配对"\)\)[^}]*?'
                     r'\.foregroundStyle\(\.secondary\)', view)
    assert _has(_body(view, r"private func row\(_ mac: SavedMac\)"), "pairingBadge(mac)")
    assert "取消配对后 Mac 仍会列出，可随时重新配对。" in view
    assert _has(view, 'Button(L("重新检查")) {', "phoneLink.readSynced()", "}")
    l10n = (sources / "App/L10n.swift").read_text()
    for added in ("已配对", "未配对", "取消配对", "正在与「", "」配对…"):
        assert f'"{added}": (' in l10n, added
    assert "点选切换，左滑删除。" not in l10n


def _assert_probe_tick_wiring(sources):
    """build 78 M2: the iCloud heal / shared-key upgrade retry runs on every probe tick.
    AgentStore assigns connState only on change, so `$connState` falls silent across repeated
    offline / unauthorized ticks; PhoneLink must listen to the per-tick `probeTick` instead,
    and the store must send it unconditionally at the end of recordBackendAccess."""
    phone = _code((sources / "App/PhoneLink.swift").read_text())
    store = _code((sources / "Store/AgentStore.swift").read_text())
    configure = _body(phone, r"func configure\(settings: AppSettings, store: AgentStore\)")
    assert "$connState" not in configure
    assert _has(configure, "store.probeTick", ".sink { [weak self] state in",
                "Task { @MainActor in self?.connectionChanged(state) }")
    assert _has(store, "let probeTick = PassthroughSubject<ConnState, Never>()")
    record = _body(store, r"private func recordBackendAccess\(")
    # Last statement, outside the switch and with no change-only guard around it.
    assert record.rstrip().endswith("probeTick.send(connState)"), record[-200:]
    # No client at all is a probe outcome too (offline); its early return must still tick.
    refresh = _body(store, r"func refreshConnection\(\) async")
    no_client = _body(refresh, r"guard let client else")
    assert no_client.rstrip().endswith("probeTick.send(connState) return"), no_client
    # Throttles stay where they were: heal 30 s, upgrade retries through upgradeBackoff.
    assert _has(_body(phone, r"private func heal\(\)"), "Date().timeIntervalSince(lastHeal) > 30")
    assert _has(_body(phone, r"private func upgradeSharedKeys\(\)"),
                "guard upgradeBackoff.allows(fingerprint, now: now) else { continue }")
