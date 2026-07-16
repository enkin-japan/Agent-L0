#if os(iOS)
import AppIntents
import Foundation
import Security

/// 灵动岛/锁屏上的「拒绝」——LiveActivityIntent 由系统在 **app 进程**里后台执行。
/// 安全设计（非对称解锁）：锁屏按钮不强制解锁，所以岛上只放"拒绝"（安全方向）；
/// "允许"只存在于通知动作（authenticationRequired 强制 Face ID/解锁）或 App 内。
///
/// 本文件同时编进 app 与 widget 两个 target（widget 引用类型、app 执行），
/// 因此自足实现连接读取，不依赖 AppSettings/AgentClient（那些不在 widget target）。
/// 常量与 AppSettings.swift / AgentClient.swift 保持一致——改那边记得改这边。
struct DenyApprovalIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "拒绝敏感操作"
    static let description = IntentDescription("拒绝 MacAgent 当前等待授权的敏感工具调用。")
    static let openAppWhenRun = false

    @Parameter(title: "会话")
    var sessionId: String
    @Parameter(title: "轮次")
    var turnId: String

    init() {}

    init(sessionId: String, turnId: String) {
        self.sessionId = sessionId
        self.turnId = turnId
    }

    func perform() async throws -> some IntentResult {
        guard let base = Self.baseURL(),
              let url = URL(string: "/api/v1/chat/\(sessionId)/turns/approval",
                            relativeTo: base) else { return .result() }
        var req = URLRequest(url: url, timeoutInterval: 10)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let key = Self.apiKey()
        if !key.isEmpty {
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        // expected_turn_id 防错批：目标轮已换时后端 409，绝不错递给后来的审批
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "approve": false, "expected_turn_id": turnId,
        ])
        // 202=已受理，岛面随后端 Live Activity 推送更新；409=已被别处处理，同样无需 UI。
        // 网络失败静默——轮次仍安全地停在等待授权态，用户随时可进 App 处理。
        _ = try? await URLSession.shared.data(for: req)
        return .result()
    }

    private static func baseURL() -> URL? {
        guard let s = UserDefaults.standard.string(forKey: "agent.baseURL"),
              let u = URL(string: s.trimmingCharacters(in: .whitespacesAndNewlines)),
              u.scheme != nil else { return nil }
        return u
    }

    private static func apiKey() -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "dev.enkin.macagent",
            kSecAttrAccount as String: "agent-api-key",
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let key = String(data: data, encoding: .utf8) else { return "" }
        return key
    }
}
#endif
