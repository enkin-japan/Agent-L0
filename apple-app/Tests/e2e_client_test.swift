// Apple 客户端协议层 E2E：用与 app 完全相同的 API 源码直连本机后端(账本协议 v1)。
// 覆盖：health / 会话列表 / 202 受理 → /turn 状态 → 事件附着(重放+续播) /
//      历史对账 / 生命周期事件序 / 会话删除。
import Foundation

var passed = 0

func check(_ name: String, _ cond: Bool, _ detail: String = "") {
    if cond { passed += 1; print("  ✅ \(name)") }
    else { print("  ❌ \(name)  \(detail)"); exit(1) }
}

@main
enum E2E {
    static func main() async {
        let base = URL(string: "http://127.0.0.1:14159")!
        let client = AgentClient(baseURL: base, apiKey: "")
        do {
            // 1. health
            let ok = try await client.health()
            check("health 可达", ok)

            // 2. 会话列表（同时验证鉴权路径）
            let before = try await client.listSessions()
            check("会话列表可读（\(before.count) 个）", true)

            // 3. 202 受理即返
            let sid = "e2e-swift-\(Int(Date().timeIntervalSince1970))"
            var params = ChatParams(prompt: "只回复两个字:好的。不要多说。")
            params.maxTokens = 512
            let accepted = try await client.submitTurn(sessionId: sid, params: params, files: [])
            check("202 受理即返（turn_id=\(accepted.turnId ?? "?")）", accepted.status && accepted.turnId != nil)

            // 4. /turn 可查进行中状态
            let ts = try await client.turnState(sessionId: sid)
            check("/turn 显示活跃轮（\(ts.turn?.state ?? "none")）", ts.turn != nil)

            // 5. 附着事件流:重放 + 续播,至 turn_ended
            var types: [String] = []
            var tokenChars = 0
            var endedTurn: Turn?
            for try await env in try client.attachEvents(sessionId: sid, after: 0) {
                if types.last != env.type { types.append(env.type) }
                if env.type == "token_delta" { tokenChars += env.str("text").count }
                if env.type == "turn_ended" {
                    endedTurn = env.decode(Turn.self)
                    break
                }
            }
            check("生命周期起点正确", types.first == "stream_attached" && types.contains("turn_queued"))
            check("token_delta 有内容（\(tokenChars) 字符）", tokenChars > 0)
            check("收到 turn_ended 且终态 success", endedTurn?.status == "success",
                  endedTurn?.status ?? "nil")
            let assistantText = (endedTurn?.response ?? [])
                .filter { $0.role == "assistant" }
                .compactMap(\.content).joined()
            check("权威 response 有助手正文", !assistantText.isEmpty)
            print("     助手回复：\(assistantText.prefix(60))")

            // 6. 历史对账 + /turn 归零
            let history = try await client.history(sessionId: sid)
            check("历史含 1 轮且 prompt 一致", history.turns.count == 1
                  && history.turns.first?.prompt == params.prompt)
            let tsAfter = try await client.turnState(sessionId: sid)
            check("/turn 无活跃轮（已完结）", tsAfter.turn == nil)

            // 7. 清理：删除测试会话
            let del = try await client.deleteSession(sessionId: sid)
            check("测试会话已删除", del.status)

            print("== E2E: \(passed) checks passed ==")
        } catch {
            print("  ❌ 异常：\(error)")
            exit(1)
        }
    }
}
