import Foundation
import Security
import SwiftUI

/// Keychain 里只存一条：后端 Bearer key。
/// web 端的安全模型是「浏览器永不持 key、由代理注入」；原生 app 是受信客户端，
/// key 落本机 Keychain 是标准做法（网络层另有 Tailscale 设备身份兜底）。
enum Keychain {
    private static let service = "dev.enkin.macagent"
    private static let account = "agent-api-key"

    static func loadAPIKey() -> String {
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

    static func saveAPIKey(_ key: String) {
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
