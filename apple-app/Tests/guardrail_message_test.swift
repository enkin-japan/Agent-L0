import Foundation

// build 85 A3：写给模型看的护栏消息（role=system、以 `[System guardrail]` 开头）不出气泡、不进会话内搜索；
// 其余 system 消息照常显示。编译：backend/tests/test_native_guardrail_message.py（真 Models / ChatMessage / L10n）。

var failures = 0
func check(_ ok: Bool, _ message: String) {
    if !ok { failures += 1; print("FAIL: \(message)") }
}

func turn(_ status: String, _ response: [[String: Any]], prompt: String = "查营收") -> Turn {
    let obj: [String: Any] = ["status": status, "prompt": prompt, "response": response]
    let raw = try! JSONSerialization.data(withJSONObject: obj)
    return try! JSONDecoder().decode(Turn.self, from: raw)
}

let guardrail = "[System guardrail] You have made several tool calls without a plan or a conclusion."
let breaker = "子调研生成已达预算上限（80 次），任务终止。"
let userFacing = "The suspended task expired (session frozen or server shutdown) and was not continued."

@main
struct GuardrailMessageTest {
    static func main() {
        // ① 现场形状（build 84 事故轮 + A1）：工具行 → 护栏提醒 → 熔断说明落成助手消息
        let t = turn("warning", [
            ["role": "assistant", "tool_calls": [["name": "web_research", "arguments": ["query": "q"]]]],
            ["role": "tool", "tool_responses": [["name": "web_research", "response": "0 results"]]],
            ["role": "system", "content": guardrail],
            ["role": "assistant", "content": breaker],
        ])
        let msgs = ChatMessage.from(turn: t)
        check(!msgs.contains { $0.text.hasPrefix(ModelFacingMessage.guardrailPrefix) },
              "guardrail system message must not become a bubble: \(msgs.map { $0.text })")
        check(!msgs.contains { $0.kind == .system }, "no system row expected in a warning turn: \(msgs.map { $0.kind })")
        check(msgs.last?.kind == .assistant && msgs.last?.text == breaker,
              "breaker explanation must be the final assistant bubble: \(String(describing: msgs.last?.text))")

        // ② 其它 system 消息照常出气泡；前缀不在开头 / 非 system 角色都不算护栏
        let t2 = turn("warning", [
            ["role": "system", "content": userFacing],
            ["role": "system", "content": "note: [System guardrail] quoted"],
            ["role": "assistant", "content": "[System guardrail] assistant text stays"],
        ])
        let msgs2 = ChatMessage.from(turn: t2)
        let systems = msgs2.filter { $0.kind == .system }.map { $0.text }
        check(systems == [userFacing, "note: [System guardrail] quoted"],
              "non-guardrail system messages must render: \(systems)")
        check(msgs2.contains { $0.kind == .assistant && $0.text == "[System guardrail] assistant text stays" },
              "assistant role is never filtered")

        // ③ 判据本体
        check(ModelFacingMessage.guardrailPrefix == "[System guardrail]", "prefix literal")
        check(ModelFacingMessage.isHidden(t.response![2]), "guardrail system message is hidden")
        check(!ModelFacingMessage.isHidden(t2.response![0]), "user-facing system message is not hidden")
        check(!ModelFacingMessage.isHidden(t2.response![2]), "assistant with prefix is not hidden")

        // ④ 会话内搜索候选：排除护栏消息，保留用户输入 / 助手正文 / 其它 system 正文
        let texts = ModelFacingMessage.searchableTexts(of: t)
        check(texts == ["查营收", breaker], "searchable texts: \(texts)")
        let texts2 = ModelFacingMessage.searchableTexts(of: t2)
        check(texts2.contains(userFacing) && texts2.count == 4, "searchable texts 2: \(texts2)")
        check(!ModelFacingMessage.searchableTexts(of: t).contains { $0.contains("several tool calls") },
              "guardrail text must not be searchable")

        if failures == 0 { print("OK guardrail_message") } else { exit(1) }
    }
}
