import AppIntents
import Foundation

/// 「问 MacAgent」——Siri / 快捷指令 / 聚焦搜索的一次性问答入口。
/// 每次提问开一个独立会话（siri- 前缀），回答同时落进会话列表，App 里可回看全文。
/// 注意：系统对后台运行的 Intent 有时限（约半分钟量级），复杂长任务请在 App 里发起。
struct AskAgentIntent: AppIntent {
    static let title: LocalizedStringResource = "问 MacAgent"
    static let description = IntentDescription("把问题发给本地 Agent，等待并返回回答文本。")
    static let openAppWhenRun = false

    @Parameter(title: "问题", requestValueDialog: "想问什么？")
    var prompt: String

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let client = await MainActor.run { AppSettings().makeClient() }
        guard let client else {
            let msg = L("尚未连接 Mac，请先在 MacAgent 里通过 iCloud 或扫码配对。")
            return .result(value: msg, dialog: IntentDialog(stringLiteral: msg))
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyMMdd-HHmmss"
        let sessionId = "siri-\(formatter.string(from: Date()))"

        var params = ChatParams(prompt: prompt)
        params.maxTokens = 2048  // 时限内求快：不开思考、限制长度

        let answer: String
        do {
            let turn = try await client.chatBlocking(sessionId: sessionId, params: params)
            if turn.status == "pending_approval" {
                answer = "这个问题触发了需要授权的操作，请打开 MacAgent 继续。"
            } else {
                let text = (turn.response ?? [])
                    .filter { $0.role == "assistant" }
                    .compactMap(\.content)
                    .joined(separator: "\n")
                answer = text.isEmpty ? "（没有产生文本回答，请到 App 里查看会话 \(sessionId)）" : text
            }
        } catch {
            answer = "连接 Agent 失败：\(error.localizedDescription)"
        }

        // 语音/横幅场景截短播报；完整文本作为返回值供快捷指令后续步骤使用
        let spoken = answer.count > 300 ? String(answer.prefix(300)) + "…" : answer
        return .result(value: answer, dialog: IntentDialog(stringLiteral: spoken))
    }
}

/// 注册 Siri 唤起短语与快捷指令卡片（App 首次启动后生效）。
struct MacAgentShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskAgentIntent(),
            phrases: [
                "问\(.applicationName)",
                "让\(.applicationName)查",
            ],
            shortTitle: "问 MacAgent",
            systemImageName: "brain"
        )
    }
}
