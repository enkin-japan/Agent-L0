// 第二批功能协议层 E2E：记忆状态 / 分支 / 删除最后一轮 / 定时任务列表 / 资产拉取。
import Foundation
func L(_ value: String) -> String { value }

var passed = 0

func check(_ name: String, _ cond: Bool, _ detail: String = "") {
    if cond { passed += 1; print("  ✅ \(name)") }
    else { print("  ❌ \(name)  \(detail)"); exit(1) }
}

@main
enum E2E2 {
    static func main() async {
        let env = ProcessInfo.processInfo.environment
        let base = URL(string: env["AGENT_E2E_BASE_URL"] ?? "http://127.0.0.1:14159")!
        let cache = env["AGENT_E2E_CACHE_ROOT"].map { URL(fileURLWithPath: $0, isDirectory: true) }
        let client = AgentClient(baseURL: base, apiKey: env["AGENT_E2E_API_KEY"] ?? "", cacheRoot: cache)
        do {
            // 0. 造一个 1 轮会话（账本协议:202 受理 + 附着流等 turn_ended）
            let sid = "e2e-b2-\(Int(Date().timeIntervalSince1970))"
            var params = ChatParams(prompt: "只回复两个字:好的。不要多说。")
            params.maxTokens = 512
            _ = try await client.submitTurn(sessionId: sid, params: params, files: [])
            var final: Turn?
            for try await env in try client.attachEvents(sessionId: sid, after: 0) {
                if env.type == "turn_ended" { final = env.decode(Turn.self); break }
            }
            check("前置：1 轮会话建立", final?.status == "success")

            // 1. 记忆状态
            // 注意后端怪癖：热会话 total_turns = len(session.history)（消息条数），
            // 冷会话才是落盘轮数。这里只验证可读与解码。
            let mem = try await client.memoryStatus(sessionId: sid)
            check("记忆状态可读（\(mem.lastPromptTokens ?? 0) tokens / total_turns=\(mem.totalTurns ?? 0)）",
                  mem.status && (mem.totalTurns ?? 0) >= 1 && (mem.highThreshold ?? 0) > 0)

            // 2. 从节点 0 分支
            let branch = try await client.branchSession(sessionId: sid, nodeIndex: 0)
            check("分支成功（\(branch.sessionId ?? "?")）", branch.status && branch.sessionId != nil)
            let bid = branch.sessionId!
            let bHistory = try await client.history(sessionId: bid)
            check("分支历史 = 1 轮且内容一致",
                  bHistory.turns.count == 1 && bHistory.turns.first?.prompt == params.prompt)

            // 3. 分支上删除最后一轮
            let del = try await client.deleteLastTurn(sessionId: bid)
            check("删除最后一轮成功", del.status)
            let after = try await client.history(sessionId: bid)
            check("回退后历史为空", after.turns.isEmpty)

            // 4. 定时任务列表（可空，验证解码与鉴权路径）
            let schedules = try await client.listSchedules()
            check("定时任务列表可读（\(schedules.count) 个）", true)
            if let first = schedules.first {
                print("     示例：\(first.name ?? first.id) — \(first.spec?.summary ?? "")")
            }

            // 5. 资产拉取（自带夹具 PNG，run_e2e.sh 跑前种进数据根——写死历史会话
            //    的路径在新环境/bundle 数据根必 404，R5-9 实证过）+ URLCache 复用
            let assetPath = "/api/v1/files/e2efixture-chart/assets/chart-fixture.png"
            let png = try await client.fetchAsset(path: assetPath)
            check("资产可拉取（\(png.count) bytes）且为 PNG",
                  png.count > 1000 && png.prefix(4) == Data([0x89, 0x50, 0x4E, 0x47]))
            let t0 = Date()
            _ = try await client.fetchAsset(path: assetPath)
            let ms = Date().timeIntervalSince(t0) * 1000
            print("     二次拉取 \(String(format: "%.1f", ms))ms（immutable 缓存生效则近零）")

            // 6. 清理
            _ = try await client.deleteSession(sessionId: bid)
            _ = try await client.deleteSession(sessionId: sid)
            check("测试会话已清理", true)

            print("== batch2 E2E: \(passed) checks passed ==")
        } catch {
            print("  ❌ 异常：\(error)")
            exit(1)
        }
    }
}
