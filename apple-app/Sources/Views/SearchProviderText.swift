import Foundation

// 设置页「搜索」（build 85 批 C）的界面文案映射。与解码类型（API/Models.swift）分开放：
// Models.swift 被不带 L10n.swift 的单编测试编译（test_native_api_tool_draft 等），不能引用 L()。
// 单编测试：backend/tests/test_native_search_providers.py。

extension SearchProvider {
    /// 最近状态 → 界面文案（未知值按「出错」显示，旧 / 新后端多出的值不至于空白）。
    static func statusText(_ status: String) -> String {
        switch status {
        case "ok":         return L("正常")
        case "rate_limit": return L("限流中")
        case "auth_error": return L("密钥无效")
        case "quota":      return L("额度用尽")
        case "unused":     return L("未使用")
        default:           return L("出错")
        }
    }

    var statusText: String { Self.statusText(status) }
    var secretText: String { hasSecret ? L("密钥已保存") : L("密钥未保存") }
}

extension SearchProviderTestResult {
    var text: String {
        if ok { return String(format: L("可用（%ld 条结果）"), results) }
        switch kind {
        case "auth":       return L("失败：密钥无效")
        case "rate_limit": return L("失败：限流中")
        case "quota":      return L("失败：额度用尽")
        case "network":    return L("失败：网络错误")
        default:           return L("失败：服务返回异常")
        }
    }
}
