import Foundation
import Security
import SwiftUI

/// Keychain 存密钥（R5-6 起多账户）：后端 Bearer key + 拨设备 A 的两把服务密钥。
/// web 端的安全模型是「浏览器永不持 key、由代理注入」；原生 app 是受信客户端，
/// key 落本机 Keychain 是标准做法（网络层另有 Tailscale 设备身份兜底）。
/// app 形态下 `.env.local` 不存在（那是仓库/devctl 的形态），这里就是密钥的家。
enum Keychain {
    private static let service = "dev.enkin.macagent"

    static func load(_ account: String) -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let key = String(data: data, encoding: .utf8) else { return "" }
        return key
    }

    static func save(_ account: String, _ key: String) {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
        guard !key.isEmpty, let data = key.data(using: .utf8) else { return }
        var attrs = base
        attrs[kSecValueData as String] = data
        attrs[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(attrs as CFDictionary, nil)
    }

    // 既有调用面保持原名（`agent-api-key` 账户不变，老用户的钥匙串条目继续有效）
    static func loadAPIKey() -> String { load("agent-api-key") }
    static func saveAPIKey(_ key: String) { save("agent-api-key", key) }
}

/// 连接设置：服务器地址进 UserDefaults，key 进 Keychain。
/// 地址示例——本机：http://127.0.0.1:14159；Tailscale：https://<mac>.<tailnet>.ts.net
/// （推荐 `tailscale serve` 反代 14159：后端保持回环绑定 + 白得合法 HTTPS 证书）。
@MainActor
final class AppSettings: ObservableObject {
    @Published var baseURLString: String {
        didSet { UserDefaults.standard.set(baseURLString, forKey: "agent.baseURL") }
    }
    @Published var apiKey: String {
        didSet { Keychain.saveAPIKey(apiKey) }
    }
    /// 外观："system" / "light" / "dark"
    @Published var appearance: String {
        didSet { UserDefaults.standard.set(appearance, forKey: "app.appearance") }
    }
    /// 语言："system" / "zh" / "ja" / "en"（写入 L10n，RootView 以 .id 触发整树重建）
    @Published var language: String {
        didSet {
            UserDefaults.standard.set(language, forKey: "app.language")
            L10n.current = AppLanguage(rawValue: language) ?? .system
        }
    }

    // ---- 通知偏好（macOS=本地通知过滤；iOS=同步到后端注册表，由后端决定发不发）----
    /// 总开关：关掉即不发任何通知（iOS 同时注销后端设备）。
    @Published var notifEnabled: Bool {
        didSet { UserDefaults.standard.set(notifEnabled, forKey: "notif.enabled") }
    }
    @Published var notifResults: Bool {
        didSet { UserDefaults.standard.set(notifResults, forKey: "notif.results") }
    }
    /// 定时任务"开始了"横幅默认关（周期任务半夜会吵；灵动岛照亮不受此影响）。
    @Published var notifScheduleStart: Bool {
        didSet { UserDefaults.standard.set(notifScheduleStart, forKey: "notif.scheduleStart") }
    }
    /// 鉴权要求默认开（安全关键，评估时与产品决策对齐）。
    @Published var notifApproval: Bool {
        didSet { UserDefaults.standard.set(notifApproval, forKey: "notif.approval") }
    }
    /// 内容分级：full / category / placeholder。默认 category——推类别不推内容
    /// （iOS 推送要过 Apple 服务器，这是隐私缓解的落点；macOS 本地通知同档观感一致）。
    @Published var notifContentLevel: String {
        didSet { UserDefaults.standard.set(notifContentLevel, forKey: "notif.contentLevel") }
    }

    // ---- Sidecar（R5-4，仅 macOS 生效）：app 内建执行层的开关与开发形态的仓库路径 ----
    /// 默认关：bundle 形态要到 R5-9 验收才齐备；dev 形态由用户显式指仓库再开。
    @Published var sidecarEnabled: Bool {
        didSet { UserDefaults.standard.set(sidecarEnabled, forKey: "sidecar.enabled") }
    }
    /// repo 布局的仓库检出路径（bundle 布局自动探测 Resources/backend，不用此项）。
    @Published var sidecarRepoRoot: String {
        didSet { UserDefaults.standard.set(sidecarRepoRoot, forKey: "sidecar.repoRoot") }
    }
    /// 拨设备 A 的两把服务密钥（R5-6）：bundle 布局下注入 backend 子进程环境，
    /// 对应 inference-server / rag 服务端的同名校验。与设备 A 的 `.env.local` 填同值。
    /// repo 布局不用这两条——dev 桥接直接读仓库的 `.env(.local)`（R5-4）。
    @Published var inferApiKey: String {
        didSet { Keychain.save("infer-api-key", inferApiKey) }
    }
    @Published var ragApiKey: String {
        didSet { Keychain.save("rag-api-key", ragApiKey) }
    }
    /// 数据面与计算面分开保存；旧 rag-api-key 账户仍供数据面/过渡拓扑使用。
    @Published var ragComputeApiKey: String {
        didSet {
            Keychain.save("rag-compute-api-key", ragComputeApiKey)
            UserDefaults.standard.set(true, forKey: "sidecar.ragComputeKeyConfigured")
        }
    }
    /// 算力端地址（非密钥，UserDefaults）：填算力机的 Tailscale IP，注入 backend 子进程
    /// 环境覆盖包内 config.yaml 里的计算面 host——换算力机零重打包。空 = 按内置配置。
    /// 一键两职：非空时同时覆盖 INFER_REMOTE_HOST 与 RAG_COMPUTE_HOST（两个服务在同一台
    /// 算力机，一个字段覆盖两个变量——用户无需理解两个变量名，照抄密钥项的哲学）。
    @Published var computeHost: String {
        didSet { UserDefaults.standard.set(computeHost, forKey: "sidecar.computeHost") }
    }
    /// 会话 KV 落盘（kv_persist）：冻结会话时把 KV 缓存写到算力端磁盘，解冻可省 3-13s。
    /// 默认 **关**——KV 实质就是会话内容，缺省不在算力机上留痕（隐私优先）；开与不开的
    /// 决策权在执行端，经 env 注入 backend 子进程（INFER_KV_PERSIST），非密钥走 UserDefaults。
    @Published var kvPersistEnabled: Bool {
        didSet { UserDefaults.standard.set(kvPersistEnabled, forKey: "agent.kvPersist") }
    }
    /// 落盘留存天数（到期由算力端自行清扫）。钳在 1...365：0 天等于「存了立刻删」，
    /// 负数与天文数字都是误输入，不该原样注入子进程 env。
    @Published var kvTTLDays: Int {
        didSet {
            let clamped = min(max(kvTTLDays, 1), 365)
            // @Published 的 setter 会再进一次 didSet；能收敛是因为钳制幂等
            // （第二轮 clamped == kvTTLDays 不再赋值），不是「didSet 不触发观察者」
            if clamped != kvTTLDays { kvTTLDays = clamped }
            UserDefaults.standard.set(clamped, forKey: "agent.kvTTLDays")
        }
    }

    /// 首启强制引导是否完成：false = 根视图只显示 OnboardingView，执行层与连接一律后置
    /// （新装 app 在填完密钥/地址前不空转尝试，省去「填完手动重启 1-2 次」）。
    /// 完成后既有设置页维持原状，作为重置入口。
    @Published var setupCompleted: Bool {
        didSet { UserDefaults.standard.set(setupCompleted, forKey: "setup.completed") }
    }

    init() {
        baseURLString = UserDefaults.standard.string(forKey: "agent.baseURL") ?? "http://127.0.0.1:14159"
        apiKey = Keychain.loadAPIKey()
        appearance = UserDefaults.standard.string(forKey: "app.appearance") ?? "system"
        language = UserDefaults.standard.string(forKey: "app.language") ?? "system"
        let ud = UserDefaults.standard
        notifEnabled = ud.object(forKey: "notif.enabled") as? Bool ?? true
        notifResults = ud.object(forKey: "notif.results") as? Bool ?? true
        notifScheduleStart = ud.object(forKey: "notif.scheduleStart") as? Bool ?? false
        notifApproval = ud.object(forKey: "notif.approval") as? Bool ?? true
        notifContentLevel = ud.string(forKey: "notif.contentLevel") ?? "category"
        sidecarEnabled = ud.object(forKey: "sidecar.enabled") as? Bool ?? false
        sidecarRepoRoot = ud.string(forKey: "sidecar.repoRoot") ?? ""
        inferApiKey = Keychain.load("infer-api-key")
        let storedRagDataKey = Keychain.load("rag-api-key")
        ragApiKey = storedRagDataKey
        let computeConfigured = ud.bool(forKey: "sidecar.ragComputeKeyConfigured")
        let restoredComputeKey = ServiceCredentials.restoredCompute(
            saved: Keychain.load("rag-compute-api-key"), legacyData: storedRagDataKey,
            configured: computeConfigured)
        ragComputeApiKey = restoredComputeKey
        if !computeConfigured {
            // Complete migration once, including an empty legacy account. A data
            // key generated later by the sidecar is never a legacy compute key.
            if !restoredComputeKey.isEmpty { Keychain.save("rag-compute-api-key", restoredComputeKey) }
            ud.set(true, forKey: "sidecar.ragComputeKeyConfigured")
        }
        computeHost = ud.string(forKey: "sidecar.computeHost") ?? ""
        kvPersistEnabled = ud.object(forKey: "agent.kvPersist") as? Bool ?? false
        // 加载时同样钳制：手工写坏的 defaults / 老版本残值不该绕过范围
        kvTTLDays = min(max(ud.object(forKey: "agent.kvTTLDays") as? Int ?? 14, 1), 365)
        // 老用户迁移（关键：升级用户绝不看到引导）：无 setup.completed 记录但已有配置痕迹
        // → 视为已完成并落盘。init 里对存储属性的首次赋值不触发 didSet，须显式 set。
        if let done = ud.object(forKey: "setup.completed") as? Bool {
            setupCompleted = done
        } else {
            #if os(macOS)
            // macOS 痕迹：开过内建执行层，或钥匙串已有 AGENT key
            let hasTraces = (ud.object(forKey: "sidecar.enabled") as? Bool ?? false)
                || !Keychain.loadAPIKey().isEmpty
            #else
            // iOS 痕迹：钥匙串已有 AGENT key，或后端地址非默认值
            let defaultURL = "http://127.0.0.1:14159"
            let hasTraces = !Keychain.loadAPIKey().isEmpty
                || (ud.string(forKey: "agent.baseURL") ?? defaultURL) != defaultURL
            #endif
            setupCompleted = hasTraces
            ud.set(hasTraces, forKey: "setup.completed")
        }
        L10n.current = AppLanguage(rawValue: language) ?? .system
    }

    var colorScheme: ColorScheme? {
        switch appearance {
        case "light": return .light
        case "dark": return .dark
        default: return nil
        }
    }

    var baseURL: URL? {
        URL(string: baseURLString.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    func makeClient() -> AgentClient? {
        guard let url = baseURL, url.scheme != nil else { return nil }
        return AgentClient(baseURL: url, apiKey: apiKey)
    }
}

extension Bundle {
    /// 界面可见的版本戳（侧栏底部 + 设置页）："v0.2.0 (2)"。
    /// 用于一眼确认设备上装的是哪个构建，杜绝「装没装上新版」的猜谜。
    var appVersionLabel: String {
        let version = infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let build = infoDictionary?["CFBundleVersion"] as? String ?? "?"
        return "v\(version) (\(build))"
    }
}
