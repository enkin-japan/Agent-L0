import Foundation

/// 审批（允许 / 拒绝）回给哪台 Mac——规则只有这一份。app 里的 MacDirectory.approvalTarget
/// 与灵动岛的 DenyApprovalIntent 都委托到这里。本文件也编进 widget target（那里只有
/// Sources/LiveActivity 与 L10n.swift），所以只用基础类型，不引用 PhoneLink 里的类型。
enum ApprovalRouting {
    /// 一台已保存的 Mac：id = 本机列表里的 SavedMac.id，macId = Mac 自报的稳定标识。
    struct Mac: Equatable {
        let id: String
        let macId: String?
    }

    /// 比推送路由严：带了 src 却认不出时，只有当前那台本身没有 macId（手填、75 迁移来的，
    /// 可能正是它）才回给它；当前那台有别的 macId 就拒发——审批绝不改投到另一台 Mac。
    /// 不带 src（旧后端）回给当前那台。nil = 不发。
    static func target(src: String?, macs: [Mac], current: String?) -> String? {
        guard let src else { return current }
        if let mac = macs.first(where: { $0.macId == src }) { return mac.id }
        guard let current, let mac = macs.first(where: { $0.id == current }), mac.macId == nil else { return nil }
        return current
    }

    enum IntentTarget: Equatable {
        case legacy                     // 没有 Mac 列表：当前连接（agent.baseURL + agent-api-key）
        case mac(String, URL)           // 列表里的那一台（mac-key.<id>）
        case refuse                     // 认不出来源，或目标不在列表 / 地址无效：不发
    }

    /// 灵动岛「拒绝」的解析：list = UserDefaults phone.macs（PhoneLink 存的 [SavedMac] JSON），
    /// currentId = phone.currentMac。挑哪台由 target 决定。
    static func intentTarget(list: Data?, currentId: String?, src: String?) -> IntentTarget {
        struct Saved: Decodable { let id: String; let macId: String?; let url: String }
        guard let list, let saved = try? JSONDecoder().decode([Saved].self, from: list), !saved.isEmpty
        else { return .legacy }
        let macs = saved.map { Mac(id: $0.id, macId: $0.macId) }
        guard let id = target(src: src, macs: macs, current: currentId),
              let chosen = saved.first(where: { $0.id == id }),
              let url = URL(string: chosen.url), url.scheme != nil else { return .refuse }
        return .mac(chosen.id, url)
    }
}
