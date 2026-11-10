import Foundation

// build 85 批 C：设置页「搜索」的解码与状态文案。
// 编译：backend/tests/test_native_search_providers.py（真 Models.swift + SearchProviderText.swift + L10n.swift）。
// 夹具是后端 GET /api/v1/search-providers 的真实形状（has_secret / status_at 蛇形键）。

var failures = 0
func check(_ ok: Bool, _ message: String) {
    if !ok { failures += 1; print("FAIL: \(message)") }
}

func withLanguage(_ lang: AppLanguage, _ body: () -> Void) {
    let saved = L10n.current
    L10n.current = lang
    body()
    L10n.current = saved
}

let listing = """
{"types": [{"type": "brave", "label": "Brave Search"}, {"type": "tavily", "label": "Tavily"}],
 "providers": [
  {"id": "brave-a1b2c3", "type": "brave", "label": "home", "enabled": true,
   "has_secret": true, "status": "rate_limit", "status_at": 1762740000.5},
  {"id": "tavily-00ff00", "type": "tavily", "label": "", "enabled": false,
   "has_secret": false, "status": "unused", "status_at": null},
  {"id": "brave-ffffff", "type": "brave", "label": "x", "enabled": true,
   "has_secret": true, "status": "quota"}
 ],
 "fallback": "searxng"}
"""

@main
struct SearchProvidersTest {
    static func main() {
        let res = try! JSONDecoder().decode(SearchProvidersResponse.self, from: Data(listing.utf8))
        check(res.types.map(\.type) == ["brave", "tavily"], "types")
        check(res.fallback == "searxng", "fallback")
        check(res.providers.count == 3, "count")
        let a = res.providers[0], t = res.providers[1], q = res.providers[2]
        check(a.id == "brave-a1b2c3" && a.enabled && a.hasSecret && a.statusAt == 1762740000.5, "brave decoded")
        check(!t.enabled && !t.hasSecret && t.statusAt == nil, "tavily decoded")
        check(q.statusAt == nil, "status_at missing decodes as nil")
        check(a.title(types: res.types) == "Brave Search · home", "title with label: \(a.title(types: res.types))")
        check(t.title(types: res.types) == "Tavily", "title without label")

        withLanguage(.zh) {
            check(a.statusText == "限流中", "zh rate_limit")
            check(t.statusText == "未使用", "zh unused")
            check(q.statusText == "额度用尽", "zh quota")
            check(SearchProvider.statusText("ok") == "正常", "zh ok")
            check(SearchProvider.statusText("auth_error") == "密钥无效", "zh auth_error")
            check(SearchProvider.statusText("error") == "出错", "zh error")
            check(SearchProvider.statusText("something-new") == "出错", "zh unknown → error")
            check(a.secretText == "密钥已保存" && t.secretText == "密钥未保存", "zh secret text")
        }
        withLanguage(.ja) {
            check(a.statusText == "レート制限中", "ja rate_limit")
            check(q.statusText == "利用上限に達しました", "ja quota")
            check(SearchProvider.statusText("auth_error") == "キーが無効", "ja auth_error")
            check(a.secretText == "キー保存済み", "ja secret")
        }
        withLanguage(.en) {
            check(q.statusText == "Quota exhausted", "en quota")
            check(SearchProvider.statusText("ok") == "OK", "en ok")
            check(t.statusText == "Not used yet", "en unused")
        }

        // 「测试」结果
        let ok = try! JSONDecoder().decode(SearchProviderTestResult.self,
                                           from: Data(#"{"ok": true, "kind": "ok", "results": 7}"#.utf8))
        let quota = try! JSONDecoder().decode(SearchProviderTestResult.self,
                                              from: Data(#"{"ok": false, "kind": "quota", "results": 0}"#.utf8))
        withLanguage(.zh) {
            check(ok.text == "可用（7 条结果）", "zh ok text: \(ok.text)")
            check(quota.text == "失败：额度用尽", "zh quota text")
            check(SearchProviderTestResult(ok: false, kind: "auth", results: 0).text == "失败：密钥无效", "auth")
            check(SearchProviderTestResult(ok: false, kind: "network", results: 0).text == "失败：网络错误", "network")
            check(SearchProviderTestResult(ok: false, kind: "http", results: 0).text == "失败：服务返回异常", "http")
        }
        withLanguage(.en) {
            check(ok.text == "Working (7 results)", "en ok text: \(ok.text)")
        }

        // 请求体：nil 不编码；密钥只在 create / patch 体里
        let patch = try! JSONEncoder().encode(SearchProviderPatch(enabled: false))
        check(String(data: patch, encoding: .utf8) == #"{"enabled":false}"#, "patch only carries changed key")
        let create = try! JSONSerialization.jsonObject(with: JSONEncoder().encode(
            SearchProviderCreateBody(type: "tavily", label: nil, secret: "test-key-0001"))) as! [String: Any]
        check(Set(create.keys) == ["type", "secret"], "create body omits nil label")

        if failures > 0 { print("FAILED \(failures)"); exit(1) }
        print("OK search_providers")
    }
}
