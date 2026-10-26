import SwiftUI

/// 首启强制引导（双端）：新装 app 在完成初始设置前不进主界面，也不启动执行层 / 不发起连接。
/// 既有设置页维持原状（作为重置入口）；本页只覆盖「第一次」需要的最小字段。
/// - macOS：主路径「内建执行层」（算力端地址 + INFER/RAG 密钥，可留空）+ 次路径「外部后端」。
/// - iOS：只有「从 Mac 获取」——iCloud 钥匙串同步的 Mac 列表，或相机扫 Mac 上的配对
///   二维码；没有手填入口（2026-10-27 用户决定）。点选 / 扫码确认后 PhoneLink.apply()
///   标记引导完成。
struct OnboardingView: View {
    @EnvironmentObject var store: AgentStore
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var phoneLink: PhoneLink
    #if os(macOS)
    @EnvironmentObject var sidecar: SidecarManager

    @State private var computeHost = ""
    @State private var inferKey = ""
    @State private var ragKey = ""
    #endif

    var body: some View {
        NavigationStack {
            Form {
                #if os(macOS)
                macContent
                #else
                iosContent
                #endif
            }
            .formStyle(.grouped)
            .navigationTitle(L("欢迎使用 MacAgent"))
        }
        .onAppear {
            #if os(macOS)
            computeHost = settings.computeHost
            inferKey = settings.inferApiKey
            ragKey = settings.ragComputeApiKey
            #endif
        }
        #if os(macOS)
        .frame(minWidth: 520, minHeight: 500)
        #endif
    }

    #if os(macOS)
    private var fieldsEmpty: Bool {
        computeHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || inferKey.isEmpty || ragKey.isEmpty
    }

    @ViewBuilder
    private var macContent: some View {
        Section {
            Text(L("开始之前，先完成一次初始设置。"))
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        Section {
            TextField(L("算力端地址"), text: $computeHost, prompt: Text("100.x.y.z"))
                .autocorrectionDisabled()
            SecureField("INFER_API_KEY", text: $inferKey)
            SecureField("RAG_COMPUTE_API_KEY", text: $ragKey)
            Text(L("与算力端 .env.local 同值（keygen 会打印）。"))
                .font(.caption)
                .foregroundStyle(.secondary)
        } header: {
            Text(L("内建执行层"))
        } footer: {
            Text(L("由 app 在本机拉起并监护后端；首次启动会自动生成 AGENT 密钥并存入钥匙串。"))
                .font(.caption).foregroundStyle(.secondary)
        }
        Section {
            Button(L("保存设置")) { completeBuiltIn() }
                .buttonStyle(.borderedProminent)
            Text(L("保存后会检查连接与鉴权；尚未配置或离线的服务会单独提示。"))
                .font(.caption).foregroundStyle(.secondary)
            if fieldsEmpty {
                Text(L("分离部署需填写，可稍后在设置中补"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        Section {
            Button(L("使用外部后端（devctl）/ 稍后配置")) { completeExternal() }
                .font(.caption)
        }
    }

    /// 内建执行层：写三字段 → 开执行层开关 + 标记引导完成 → 启连接与执行层。
    /// 三字段允许留空（单机 inprocess/融合形态不需要）。
    private func completeBuiltIn() {
        settings.computeHost = computeHost.trimmingCharacters(in: .whitespacesAndNewlines)
        settings.inferApiKey = SidecarManager.cleanKey(inferKey)
        settings.ragComputeApiKey = SidecarManager.cleanKey(ragKey)
        // AGENT 密钥本会在 autostart 里首次生成（SidecarManager R5-6）。但那是异步、
        // 发生在 applySettings 建好客户端之后——客户端会带空 key 而后端要求生成的 key，
        // 落地就是引导完成即 401（B 要消除的「手动重启 1-2 次」痛点）。这里先同步生成
        // 同一把（autostart 见非空即复用），令 app 客户端与后端一开始就同 key。
        if settings.apiKey.isEmpty { settings.apiKey = SidecarManager.randomKey() }
        settings.sidecarEnabled = true
        settings.setupCompleted = true
        store.applySettings()          // 连接后置：此刻起 setupCompleted，监听正式启动
        Task { await sidecar.autostart() }
    }

    /// 外部后端（devctl）/ 稍后配置：只标记引导完成、启动连接，不开内建执行层。
    private func completeExternal() {
        settings.setupCompleted = true
        store.applySettings()
    }
    #else

    @ViewBuilder
    private var iosContent: some View {
        PhoneConnectionSection()
        // 出口：新装且暂时没有可连的 Mac（无 iCloud 条目、手边也没有 Mac 的二维码）时不能
        // 卡死在引导页。进主界面后照样能配对：系统相机扫码由 MacAgentApp 的 onOpenURL
        // 接住（引导页与主界面同一处），Mac 列表在主界面 ⚙ → 设置里。
        Section {
            Button(L("稍后配对")) { pairLater() }
        }
    }

    /// 与旧版「地址留空保存」同效：地址清空（否则下次启动 build 75 迁移会把默认回环地址
    /// 当成一台 Mac 迁进列表）、标记引导完成、启动连接（无地址 = 不连）。
    private func pairLater() {
        settings.baseURLString = ""
        settings.connectionSource = .manual
        settings.setupCompleted = true
        store.applySettings()
    }
    #endif
}
