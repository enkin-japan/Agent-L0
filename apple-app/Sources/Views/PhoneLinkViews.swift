import SwiftUI

#if os(macOS)
/// 设置 → 连接 → 连接 iPhone：对 tailnet 开放执行层、同步到 iCloud 钥匙串、备用二维码。
struct MacPhoneLinkSection: View {
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var phoneLink: PhoneLink

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
            if let error = phoneLink.actionError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(Theme.warning)
                    .textSelection(.enabled)
            }
        } header: {
            Text(L("连接 iPhone"))
        } footer: {
            Text(L("iCloud 同步要求两台设备登录同一 Apple ID 并开启 iCloud 钥匙串。不满足时用二维码：它只含一次性配对码，5 分钟内有效，用一次即作废。"))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .task { await phoneLink.refresh() }
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
                    .disabled(phoneLink.busy)
            }
        case .served(let url):
            LabeledContent(L("iPhone 地址")) {
                Text(url.absoluteString).textSelection(.enabled)
            }
            Button(L("停止对 iPhone 开放"), role: .destructive) { Task { await phoneLink.unexpose() } }
                .disabled(phoneLink.busy)
        }
    }

    private var recheckButton: some View {
        Button(L("重新检查")) { Task { await phoneLink.refresh() } }
    }

    @ViewBuilder
    private var syncStatus: some View {
        if phoneLink.syncOK {
            Label(L("已写入 iCloud 钥匙串：同一 Apple ID 的 iPhone 打开 app 即可使用。"),
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
/// iPhone 的引导页与设置页共用：读 Mac 写进 iCloud 钥匙串的连接信息，一键使用。
struct PhoneConnectionSection: View {
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var phoneLink: PhoneLink
    /// 设置页用它刷新自己的输入框（否则随后点「保存」会把旧值写回去）。
    var onApplied: () -> Void = {}

    var body: some View {
        Section {
            if let synced = phoneLink.synced {
                LabeledContent(L("来自 Mac"), value: synced.mac.isEmpty ? synced.url : synced.mac)
                Text(synced.url)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if synced.url == settings.baseURLString, synced.key == settings.apiKey {
                    Label(L("正在使用此连接"), systemImage: "checkmark.circle")
                        .foregroundStyle(Theme.success)
                } else {
                    Button(L("使用 iCloud 同步的连接")) {
                        phoneLink.useSynced()
                        onApplied()
                    }
                }
            } else {
                Text(phoneLink.syncedError
                     ?? L("iCloud 钥匙串里还没有 Mac 的连接信息。请确认两台设备登录同一 Apple ID、开启了 iCloud 钥匙串，并在 Mac 的「设置 → 连接 → 连接 iPhone」中开启同步；刚开启时可能要等一两分钟。"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Button(L("重新检查")) { phoneLink.readSynced() }
        } header: {
            Text(L("从 Mac 获取"))
        } footer: {
            Text(L("也可以在 Mac 的「设置 → 连接 → 连接 iPhone」中显示配对二维码，用 iPhone 相机扫描。"))
        }
        .onAppear { phoneLink.readSynced() }
    }
}
#endif
