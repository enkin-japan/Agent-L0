import SwiftUI

#if os(macOS)
/// 设置 → 连接 → 连接 iPhone：对 tailnet 开放执行层、同步到 iCloud 钥匙串、备用二维码。
struct MacPhoneLinkSection: View {
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var phoneLink: PhoneLink
    @State private var renaming: PairingAPI.Device?
    @State private var newName = ""
    @State private var removing: PairingAPI.Device?

    var body: some View {
        Section {
            exposureRows
            if case .served = phoneLink.exposure {
                Toggle(L("通过 iCloud 钥匙串同步到 iPhone"), isOn: Binding(
                    get: { settings.phoneSyncEnabled },
                    set: { phoneLink.setSyncEnabled($0) }))
                if settings.phoneSyncEnabled { syncStatus }
                pairingRows
            }
            devicesRows
            if let error = phoneLink.actionError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(Theme.warning)
                    .textSelection(.enabled)
            }
        } header: {
            Text(L("连接 iPhone"))
        } footer: {
            Text(L("每部 iPhone 都要经你同意才能连上：同一 Apple ID 的 iPhone 读到 iCloud 同步后会在这里请求连接，你点允许即可；不同 Apple ID 的用二维码（只含一次性配对码，5 分钟内有效，用一次即作废）。共享密钥不会离开这台 Mac。"))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .task { await phoneLink.refresh() }
        .alert(L("给设备改名"), isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField(L("名称"), text: $newName)
            Button(L("保存")) {
                if let id = renaming?.id { Task { await phoneLink.renameDevice(id, to: newName) } }
            }
            Button(L("取消"), role: .cancel) {}
        }
        .confirmationDialog(L("移除这台设备？"), isPresented: Binding(get: { removing != nil },
                                                                 set: { if !$0 { removing = nil } }),
                            presenting: removing) { device in
            Button(L("移除") + "「" + device.name + "」", role: .destructive) {
                Task { await phoneLink.revokeDevice(device.id) }
            }
        } message: { _ in
            Text(L("它会立即断开，之后要重新经你同意才能连上。"))
        }
        // 配对码 5 分钟就死：关掉设置窗再打开不该看到一张失效的旧码
        .onDisappear { phoneLink.endPairing() }
    }

    @ViewBuilder
    private var exposureRows: some View {
        switch phoneLink.exposure {
        case .checking:
            HStack(spacing: Theme.s2) {
                ProgressView().controlSize(.small)
                Text(L("正在检查 Tailscale…")).foregroundStyle(.secondary)
            }
        case .noTailscale:
            Text(L("未找到 Tailscale。iPhone 经 Tailscale 连接本机：请在 Mac 与 iPhone 上安装并登录同一个 tailnet。"))
                .font(.callout)
            recheckButton
        case .offline:
            Text(L("Tailscale 未登录或未运行。"))
                .font(.callout)
            recheckButton
        case .notServed(_, let portTaken):
            Text(L("执行层尚未对 tailnet 开放，iPhone 还连不上。"))
                .font(.callout)
            if portTaken {
                Text(L("目标端口已被其他 tailscale serve 配置占用，请在终端自行调整。"))
                    .font(.caption)
                    .foregroundStyle(Theme.warning)
            } else {
                Button(L("对 iPhone 开放（tailscale serve）")) { Task { await phoneLink.expose() } }
                    .disabled(phoneLink.busy || phoneLink.exposeRefusal != nil)
                if let refusal = phoneLink.exposeRefusal {
                    Text(refusal)
                        .font(.caption)
                        .foregroundStyle(Theme.warning)
                }
            }
        case .served(let url):
            LabeledContent(L("iPhone 地址")) {
                Text(url.absoluteString).textSelection(.enabled)
            }
            if let warning = phoneLink.authWarning {
                Text(warning)
                    .font(.caption)
                    .foregroundStyle(Theme.warning)
            }
            if phoneLink.stopRequested && !phoneLink.stopping {
                Text(L("停止开放没有完成：iCloud 条目已删除，但 tailscale serve 仍在运行。"))
                    .font(.caption)
                    .foregroundStyle(Theme.warning)
            }
            Button(phoneLink.stopRequested ? L("重试停止开放") : L("停止对 iPhone 开放"), role: .destructive) {
                Task { await phoneLink.unexpose() }
            }
                .disabled(phoneLink.busy || phoneLink.stopping)
        }
    }

    /// 已经连上这台 Mac 的设备：改名、有效期（默认 90 天，可改永久）、移除。
    @ViewBuilder
    private var devicesRows: some View {
        if let notice = phoneLink.deviceNotice {
            Text(L(notice))
                .font(.caption)
                .foregroundStyle(Theme.warning)
        }
        Toggle(L("新设备的连接 90 天后到期"), isOn: Binding(
            get: { phoneLink.defaultTTLDays != nil },
            set: { on in Task { await phoneLink.setDefaultExpiry(on) } }))
        if phoneLink.devices.isEmpty {
            Text(L("还没有设备连上这台 Mac。"))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        ForEach(phoneLink.devices) { device in
            HStack(spacing: Theme.s2) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(device.name)
                    Text(detail(device))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Menu {
                    Button(L("改名…")) {
                        newName = device.name
                        renaming = device
                    }
                    if device.expires == nil {
                        Button(L("恢复 90 天期限")) { Task { await phoneLink.setPermanent(device.id, false) } }
                    } else {
                        Button(L("改为永久")) { Task { await phoneLink.setPermanent(device.id, true) } }
                    }
                    Divider()
                    Button(L("移除…"), role: .destructive) { removing = device }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }
        }
        Button(L("重新生成 iCloud 连接凭据")) { Task { await phoneLink.rotateTicket() } }
            .help(L("旧凭据与所有等待同意的请求随即作废；已连上的设备不受影响。"))
    }

    private func detail(_ device: PairingAPI.Device) -> String {
        let via: String
        switch device.via {
        case "icloud": via = L("iCloud 同步")
        case "qr": via = L("扫码配对")
        default: via = L("旧连接升级")
        }
        let used = Date(timeIntervalSince1970: device.last_used)
            .formatted(.relative(presentation: .named))
        let expiry: String
        if let expires = device.expires {
            let days = max(0, Int(ceil((expires - Date().timeIntervalSince1970) / 86400)))
            expiry = days == 0 ? L("已到期") : "\(days) " + L("天后到期")
        } else {
            expiry = L("永久")
        }
        return "\(via) · \(L("最近使用")) \(used) · \(expiry)"
    }

    private var recheckButton: some View {
        Button(L("重新检查")) { Task { await phoneLink.refresh() } }
    }

    @ViewBuilder
    private var syncStatus: some View {
        if phoneLink.syncOK {
            Label(L("已写入 iCloud 钥匙串：同一 Apple ID 的 iPhone 打开 app 后会在这里请求连接，你点允许即可。"),
                  systemImage: "checkmark.icloud")
                .font(.caption)
                .foregroundStyle(Theme.success)
        } else if let error = phoneLink.syncError {
            Text(error)
                .font(.caption)
                .foregroundStyle(Theme.warning)
        } else if settings.apiKey.isEmpty {
            Text(L("执行层尚未生成 AGENT 密钥，生成后自动同步。"))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var pairingRows: some View {
        if let pairing = phoneLink.pairing {
            VStack(spacing: Theme.s2) {
                if let image = pairing.image {
                    Image(nsImage: image)
                        .interpolation(.none)
                        .resizable()
                        .frame(width: 200, height: 200)
                }
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    let remaining = Int(pairing.expires.timeIntervalSince(context.date))
                    Text(remaining > 0
                         ? "\(L("用 iPhone 相机扫描")) · \(remaining) \(L("秒后失效"))"
                         : L("配对码已失效"))
                        .font(.caption)
                        .foregroundStyle(remaining > 0 ? Color.secondary : Theme.warning)
                }
                HStack(spacing: Theme.s2) {
                    Button(L("重新生成")) { Task { await phoneLink.startPairing() } }
                        .disabled(phoneLink.busy)
                    Button(L("收起")) { phoneLink.endPairing() }
                }
            }
            .frame(maxWidth: .infinity)
        } else {
            Button(L("显示配对二维码（备用）")) { Task { await phoneLink.startPairing() } }
                .disabled(phoneLink.busy)
        }
    }
}
#endif

#if os(iOS)
/// iPhone 的引导页与设置页共用：保存过的 Mac（iCloud 同步 / 扫码 / 手填），点选切换、左滑删除。
struct PhoneConnectionSection: View {
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var phoneLink: PhoneLink
    @State private var renaming: SavedMac?
    @State private var newName = ""
    /// 设置页用它刷新自己的输入框（否则随后点「保存」会把旧值写回去）。
    var onApplied: () -> Void = {}

    var body: some View {
        Section {
            if phoneLink.macs.isEmpty {
                Text(phoneLink.syncedError
                     ?? L("iCloud 钥匙串里还没有 Mac 的连接信息。请确认两台设备登录同一 Apple ID、开启了 iCloud 钥匙串，并在 Mac 的「设置 → 连接 → 连接 iPhone」中开启同步；刚开启时可能要等一两分钟。"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(phoneLink.macs) { mac in
                    Button {
                        phoneLink.select(mac.id)
                        onApplied()
                    } label: {
                        row(mac)
                    }
                    .foregroundStyle(.primary)
                    .swipeActions(edge: .leading) {
                        Button(L("改名")) {
                            newName = mac.name
                            renaming = mac
                        }
                        .tint(Theme.accent)
                    }
                }
                .onDelete { offsets in
                    // 先取 id 再删：删除会改变下标
                    let ids = offsets.map { phoneLink.macs[$0].id }
                    ids.forEach(phoneLink.remove)
                    onApplied()
                }
                if let error = phoneLink.syncedError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(Theme.warning)
                }
            }
            Button(L("重新检查")) { phoneLink.readSynced() }
        } header: {
            Text(L("Mac 列表"))
        } footer: {
            Text(L("点选切换，左滑删除，右滑改名。同一 Apple ID 的 Mac 会自动出现在这里，第一次连接要在 Mac 上点允许；不同 Apple ID 的，在 Mac 的「设置 → 连接 → 连接 iPhone」中显示配对二维码，用 iPhone 相机扫描。"))
        }
        .onAppear { phoneLink.readSynced() }
        .alert(L("给 Mac 改名"), isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField(L("名称"), text: $newName)
            Button(L("保存")) { if let id = renaming?.id { phoneLink.rename(id, to: newName) } }
            Button(L("取消"), role: .cancel) {}
        }
    }

    private func row(_ mac: SavedMac) -> some View {
        HStack(spacing: Theme.s2) {
            VStack(alignment: .leading, spacing: 2) {
                Text(mac.name)
                Text(mac.url)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(sourceLabel(mac))
                    .font(.caption2)
                    .foregroundStyle(mac.removedFromICloud ? Theme.warning : Color.secondary)
            }
            Spacer()
            if mac.id == phoneLink.currentId, settings.setupCompleted {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(Theme.success)
                    .accessibilityLabel(L("正在使用此连接"))
            }
        }
        .contentShape(Rectangle())
    }

    private func sourceLabel(_ mac: SavedMac) -> String {
        if mac.id == phoneLink.currentId, phoneLink.currentRejected {
            return L("这台 Mac 已不再接受本机（被移除或已到期），点按重新请求连接")
        }
        if mac.removedFromICloud { return L("Mac 已停止同步（仍可使用，不需要时左滑删除）") }
        if mac.credential == .none { return L("点按向这台 Mac 请求连接") }
        switch mac.source {
        case .icloud: return L("iCloud 同步")
        case .qr: return L("扫码配对")
        case .manual: return L("手动填写")
        }
    }
}
#endif
