import Foundation

// build 84：进度代号 / 工具名按界面语言显示（StepText）与回退矩阵。
// 编译：backend/tests/test_native_step_text.py（真 L10n.swift + 真 TurnReducer / ChatMessage）。
// 归约器只存代号不改 detail（夹具与语言无关）；展示层按 L10n.current 渲染，渲染不了回退后端原文。

var failures = 0
func check(_ ok: Bool, _ message: String) {
    if !ok { failures += 1; print("FAIL: \(message)") }
}

func env(_ seq: Int, _ type: String, _ data: [String: Any]) -> EventEnvelope {
    let obj: [String: Any] = ["seq": seq, "turn_id": "t1", "ts": 1.0, "type": type, "data": data]
    let raw = try! JSONSerialization.data(withJSONObject: obj)
    return try! JSONDecoder().decode(EventEnvelope.self, from: raw)
}

/// tool_started(web_research) → tool_progress(data) 后的那一步。
func runningStep(_ progress: [String: Any], tool: String = "web_research") -> NormalizedStep {
    var s = NormalizedState()
    s = TurnReducer.reduce(s, env(1, "turn_queued", ["prompt": "p"]))
    s = TurnReducer.reduce(s, env(2, "tool_started", ["call_id": "c1", "name": tool, "args": [:]]))
    var data = progress
    data["call_id"] = "c1"
    s = TurnReducer.reduce(s, env(3, "tool_progress", data))
    return s.preview.steps[0]
}

func withLanguage(_ lang: AppLanguage, _ body: () -> Void) {
    let saved = L10n.current
    L10n.current = lang
    body()
    L10n.current = saved
}

@main
struct StepTextTest {
    static func main() {
        // ---- 新后端：代号 + 参数 ----
        let thinking = runningStep(["label": "联网调查：思考中（2/6）", "detail": "",
                                    "code": "research.thinking", "args": ["step": 2, "max": 6]])
        // 归约器 detail 仍是后端原文（夹具 / web 归约器同形，与语言无关）
        check(thinking.detail == "联网调查：思考中（2/6）", "reducer keeps backend text in detail")
        check(thinking.progress?.code == "research.thinking", "reducer records progress code")
        withLanguage(.zh) {
            // zh 模板渲染 == 后端 label 逐字相同
            check(StepText.detail(thinking) == "联网调查：思考中（2/6）", "zh render equals backend text")
            check(StepText.label(thinking) == "联网调查", "zh tool name")
        }
        withLanguage(.ja) {
            check(StepText.detail(thinking) == "ウェブ調査：検討中（2/6）", "ja progress: \(StepText.detail(thinking))")
            check(StepText.label(thinking) == "ウェブ調査", "ja tool name")
        }
        withLanguage(.en) {
            check(StepText.detail(thinking) == "Web research: thinking (2/6)", "en progress: \(StepText.detail(thinking))")
            check(StepText.label(thinking) == "Web research", "en tool name")
        }

        // 字符串参数；参数值里的占位写法不被二次展开
        let search = runningStep(["label": "联网调查：搜索「a{max}b」", "detail": "",
                                  "code": "research.searching", "args": ["query": "a{max}b"]])
        withLanguage(.zh) { check(StepText.detail(search) == "联网调查：搜索「a{max}b」", "zh search equals backend") }
        withLanguage(.ja) { check(StepText.detail(search) == "ウェブ調査：「a{max}b」を検索", "ja search single-pass: \(StepText.detail(search))") }

        // label + 后端 detail（文件名等原样数据）：本地化 label 后拼回 detail，与归约器同形
        let inspect = runningStep(["label": "拆解表格结构", "detail": "report.xlsx",
                                   "code": "excel.inspecting", "args": [:]], tool: "excel_research")
        check(inspect.detail == "拆解表格结构 · report.xlsx", "reducer joins label · detail")
        withLanguage(.zh) { check(StepText.detail(inspect) == inspect.detail, "zh label+detail equals reducer text") }
        withLanguage(.ja) {
            check(StepText.detail(inspect) == "表の構造を解析 · report.xlsx", "ja label+detail: \(StepText.detail(inspect))")
            check(StepText.label(inspect) == "Excel 作業", "ja excel tool name")
        }
        // args 缺省（不带 args 键）且模板无占位 → 照常本地化
        let noArgs = runningStep(["label": "阅读网页：归纳内容", "code": "web_read.summarizing"], tool: "web_read")
        withLanguage(.ja) {
            check(StepText.detail(noArgs) == "ウェブページ閲覧：内容を要約中", "ja without args key")
            check(StepText.label(noArgs) == "ウェブページ閲覧", "ja web_read name")
        }

        // ---- 回退矩阵：一律显示后端原文，不崩 ----
        let fallbacks: [(String, [String: Any])] = [
            ("old backend (no code)", ["label": "联网调查：阅读 3 个页面", "detail": ""]),
            ("unknown code", ["label": "联网调查：阅读 3 个页面", "code": "research.teleport", "args": ["count": 3]]),
            ("empty code", ["label": "联网调查：阅读 3 个页面", "code": "", "args": ["count": 3]]),
            ("missing arg", ["label": "联网调查：阅读 3 个页面", "code": "research.reading", "args": [:]]),
            ("wrong arg type (bool)", ["label": "联网调查：阅读 3 个页面", "code": "research.reading", "args": ["count": true]]),
            ("wrong arg type (fraction)", ["label": "联网调查：阅读 3 个页面", "code": "research.reading", "args": ["count": 2.5]]),
            ("wrong arg type (object)", ["label": "联网调查：阅读 3 个页面", "code": "research.reading", "args": ["count": ["n": 3]]]),
            ("args not an object", ["label": "联网调查：阅读 3 个页面", "code": "research.reading", "args": [3]]),
            ("code not a string", ["label": "联网调查：阅读 3 个页面", "code": 7, "args": ["count": 3]]),
        ]
        for (name, data) in fallbacks {
            let step = runningStep(data)
            for lang in [AppLanguage.zh, .ja, .en] {
                withLanguage(lang) {
                    check(StepText.detail(step) == "联网调查：阅读 3 个页面", "fallback \(name) [\(lang)]: \(StepText.detail(step))")
                }
            }
        }

        // tool_result 覆盖 detail 后不再按旧代号渲染
        var s = NormalizedState()
        s = TurnReducer.reduce(s, env(1, "turn_queued", ["prompt": "p"]))
        s = TurnReducer.reduce(s, env(2, "tool_started", ["call_id": "c1", "name": "web_research", "args": [:]]))
        s = TurnReducer.reduce(s, env(3, "tool_progress", ["call_id": "c1", "label": "联网调查：归纳结论",
                                                           "detail": "", "code": "research.summarizing", "args": [:]]))
        s = TurnReducer.reduce(s, env(4, "tool_result", ["call_id": "c1", "name": "web_research",
                                                         "response": "digest", "status": "ok"]))
        check(s.preview.steps[0].progress == nil, "tool_result clears progress")
        withLanguage(.ja) { check(StepText.detail(s.preview.steps[0]) == "digest", "result detail shown as-is") }

        // ---- 工具名表 ----
        withLanguage(.ja) {
            check(StepText.toolName("web_research") == "ウェブ調査", "ja web_research")
            check(StepText.toolName("web_read") == "ウェブページ閲覧", "ja web_read")
            check(StepText.toolName("render_chart") == "グラフ作成", "ja render_chart")
            check(StepText.toolName("file_extract") == "ファイル読み取り", "ja file_extract")
            check(StepText.toolName("run_shell_command") == "コマンド実行", "ja run_shell_command")
            check(StepText.toolName("my_registered_api") == "my_registered_api", "unknown tool falls back to raw name")
            // 计划步（模型写的标题）不走工具名表，哪怕恰好与工具名同名
            var plan = NormalizedState()
            plan = TurnReducer.reduce(plan, env(1, "turn_queued", ["prompt": "p"]))
            plan = TurnReducer.reduce(plan, env(2, "step_completed", ["title": "web_research", "result": "r"]))
            check(StepText.label(plan.preview.steps[0]) == "web_research", "plan step label untouched")
        }
        withLanguage(.en) { check(StepText.toolName("render_chart") == "Draw chart", "en render_chart") }
        withLanguage(.zh) { check(StepText.toolName("render_chart") == "绘制图表", "zh render_chart") }
        // 每个登记工具在三种语言下都有非空显示名，且 ja/en 与 zh 不同（确有译文）
        for (tool, zh) in StepText.toolNames {
            withLanguage(.ja) { check(!StepText.toolName(tool).isEmpty && StepText.toolName(tool) != zh, "ja name for \(tool)") }
            withLanguage(.en) { check(!StepText.toolName(tool).isEmpty && StepText.toolName(tool) != zh, "en name for \(tool)") }
        }
        // 每个代号在 ja/en 下都能用其占位参数渲染出非 zh 文本
        for (code, zh) in StepText.progressTemplates {
            var args: [String: JSONValue] = [:]
            for key in ["step", "max", "count"] where zh.contains("{\(key)}") { args[key] = .number(3) }
            if zh.contains("{query}") { args["query"] = .string("q") }
            withLanguage(.zh) { check(StepText.progress(code: code, args: args) != nil, "zh renders \(code)") }
            withLanguage(.ja) {
                let text = StepText.progress(code: code, args: args)
                check(text != nil && text != StepText.fill(zh, args) && !(text ?? "").contains("{"), "ja renders \(code)")
            }
            withLanguage(.en) {
                let text = StepText.progress(code: code, args: args)
                check(text != nil && text != StepText.fill(zh, args) && !(text ?? "").contains("{"), "en renders \(code)")
            }
        }

        // ---- 历史轮次：执行过程组的工具行标签同样本地化 ----
        let turnJSON = """
        {"status": "success", "prompt": "p", "response": [
          {"role": "assistant", "tool_calls": [{"type": "function", "name": "web_research", "arguments": {"query": "x"}}]},
          {"role": "tool", "tool_responses": [{"name": "web_research", "response": "digest"}]},
          {"role": "assistant", "content": "done"}]}
        """
        let turn = try! JSONDecoder().decode(Turn.self, from: Data(turnJSON.utf8))
        withLanguage(.ja) {
            let labels = ChatMessage.from(turn: turn).compactMap { $0.toolLabel }
            check(labels == ["\(L("调用")) ウェブ調査", "ウェブ調査 \(L("返回"))"], "ja history labels: \(labels)")
        }

        if failures > 0 { print("\(failures) failure(s)"); exit(1) }
        print("OK step_text")
    }
}
