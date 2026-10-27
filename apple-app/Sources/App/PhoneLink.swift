import Combine
import CryptoKit
import Foundation
import Security
import SwiftUI
import UserNotifications
#if os(macOS)
import AppKit
import CoreImage.CIFilterBuiltins
#else
import UIKit
#endif

/// iCloud 钥匙串里一台 Mac 的条目：经 tailnet 开放的地址 + 「请求凭据」（ticket）。
/// 每部 iPhone 都要经 Mac 同意才能连上（backend/framework/api/pairing.py）：同一 Apple ID 的
/// iPhone 读到条目后凭 ticket 发起连接请求，Mac 弹框点允许，iPhone 拿到它自己的设备票据；
/// 不同 Apple ID 的扫 Mac 上的二维码。共享密钥（AGENT 密钥）不出 Mac。
/// 一部 iPhone 可以保存多台 Mac（见 MacDirectory）：每台 Mac 只写自己那一条同步条目。
struct PhoneConnection: Codable, Equatable {
    var url: String
    var mac: String
    /// 写条目的那台 Mac 的稳定标识（MacIdentity）。76 起 Mac 写入时必带；
    /// 读到 75 留下的全局旧条目时为 nil。
    var macId: String?
    /// 只有 75 的全局旧条目带共享密钥；76 起不再写进 iCloud。
    var key: String?
    /// 只能用来提交连接请求的凭据（76 起）。
    var ticket: String?
    var updated: Double?

    /// 内容相同即不必重写（updated 只是时间戳）。
    func sameContent(_ other: PhoneConnection) -> Bool {
        url == other.url && mac == other.mac && macId == other.macId
            && key == other.key && ticket == other.ticket
    }
}

/// iPhone 手里对某台 Mac 的凭据是什么。
enum Credential: String, Codable {
    case none          // 只读到了 iCloud 条目，还没经 Mac 同意
    case sharedKey     // 75 / 旧版手填 / 旧连接：共享密钥，启动后自动换成设备票据（不再新建）
    case deviceToken   // Mac 同意（或扫码）后发给这部 iPhone 的票据
}

/// iPhone 端连接信息的来源：iCloud 来源遇 401/离线会自动重读同步条目（见 PhoneLink），
/// 手动填写与扫码配对的值不被静默覆盖。
enum ConnectionSource: String, Codable {
    case manual, icloud, qr
}

/// 可同步的共享钥匙串条目。两端 app 的 bundle id 不同，靠同 Team 前缀的共享访问组互通。
/// macOS 必须走 data protection 钥匙串（iCloud 同步只发生在那里），而那要求签名带
/// keychain-access-groups——只有 bundle-macos.sh 嵌了描述文件的发布构建才有；开发构建
/// 读写得到 errSecMissingEntitlement，界面据此提示改用二维码。
/// 每台 Mac 一条（account = connection.<macId>）；build 75 只有一条全局的 `connection`，
/// 同一 Apple ID 下两台 Mac 会互相覆盖。
enum SharedKeychain {
    static let accessGroup = "9A84P4ZS84.dev.enkin.macagent.shared"
    private static let service = "dev.enkin.macagent.phone-link"
    static let legacyAccount = "connection"
    static func account(for macId: String) -> String { "connection." + macId }

    /// account 为 nil 只用于读（匹配全部条目）；删除与写入一律带具体 account。
    private static func query(account: String?) -> [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccessGroup as String: accessGroup,
            kSecAttrSynchronizable as String: true,
        ]
        if let account { q[kSecAttrAccount as String] = account }
        #if os(macOS)
        q[kSecUseDataProtectionKeychain as String] = true
        #endif
        return q
    }

    /// 读出全部条目（每台 Mac 一条，可能还有 75 的全局旧条目）。没有条目不算错误。
    static func readAll() -> ([PhoneConnection], OSStatus) {
        var q = query(account: nil)
        q[kSecReturnData as String] = true
        q[kSecReturnAttributes as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitAll
        var result: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &result)
        if status == errSecItemNotFound { return ([], errSecSuccess) }
        guard status == errSecSuccess else { return ([], status) }
        guard let rows = result as? [[String: Any]] else { return ([], errSecDecode) }
        let items = rows.compactMap { row -> (account: String, data: Data)? in
            guard let account = row[kSecAttrAccount as String] as? String,
                  let data = row[kSecValueData as String] as? Data else { return nil }
            return (account, data)
        }
        return (decode(items), errSecSuccess)
    }

    /// 条目内容必须与 account 对得上：connection.<macId> 里存的 macId 就是那个 macId。
    /// 对不上的丢弃，免得一条坏条目冒充另一台 Mac。全局旧条目一律当作没有 macId。
    static func decode(_ items: [(account: String, data: Data)]) -> [PhoneConnection] {
        items.compactMap { item in
            guard var connection = try? JSONDecoder().decode(PhoneConnection.self, from: item.data)
            else { return nil }
            if item.account == legacyAccount {
                connection.macId = nil
                return connection
            }
            guard let id = connection.macId, MacDirectory.validMacId(id),
                  item.account == account(for: id) else { return nil }
            return connection
        }
    }

    /// 只写这条连接自己那台 Mac 的条目；没有合法 macId 就拒写（errSecParam）。
    static func write(_ connection: PhoneConnection) -> OSStatus {
        guard let id = connection.macId, MacDirectory.validMacId(id) else { return errSecParam }
        return write(connection, account: account(for: id))
    }

    private static func write(_ connection: PhoneConnection, account: String) -> OSStatus {
        guard let data = try? JSONEncoder().encode(connection) else { return errSecParam }
        let base = query(account: account)
        let status = SecItemUpdate(base as CFDictionary,
                                   [kSecValueData as String: data] as CFDictionary)
        guard status == errSecItemNotFound else { return status }
        var attrs = base
        attrs[kSecValueData as String] = data
        // 可同步条目不能用 ThisDeviceOnly 档
        attrs[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        attrs[kSecAttrLabel as String] = "MacAgent iPhone connection"
        return SecItemAdd(attrs as CFDictionary, nil)
    }

    static func delete(macId: String) -> OSStatus {
        guard MacDirectory.validMacId(macId) else { return errSecParam }
        return delete(account: account(for: macId))
    }

    static func deleteLegacy() -> OSStatus { delete(account: legacyAccount) }

    private static func delete(account: String) -> OSStatus {
        let status = SecItemDelete(query(account: account) as CFDictionary)
        return status == errSecItemNotFound ? errSecSuccess : status
    }

    static func describe(_ status: OSStatus) -> String {
        if status == errSecMissingEntitlement {
            return L("此构建未带 iCloud 钥匙串权限（开发构建），请改用二维码配对。")
        }
        return L("钥匙串错误：") + "\(status)"
    }
}

/// iPhone 保存的一台 Mac。凭据不在这里：票据 / 密钥存本机钥匙串（mac-key.<id>），
/// 请求凭据存 mac-ticket.<id>，都不同步。
struct SavedMac: Codable, Equatable, Identifiable {
    var id: String
    /// Mac 的稳定标识：iCloud 条目与扫码配对都会带；手填的连接没有。
    var macId: String?
    var name: String
    var url: String
    var source: ConnectionSource
    var addedAt: Date
    /// iCloud 来源、但 Mac 那边已经删掉了同步条目（停止开放或关了同步）。不自动删，由用户删。
    var removedFromICloud: Bool
    var credential: Credential
    /// 用户在 iPhone 上改过名：iCloud 同步不再覆盖。
    var customName: Bool

    enum PairingState: Equatable { case paired, unpaired }
    /// 已配对 = 本机手里有这台 Mac 的凭据（设备票据，或待换票据的旧共享密钥）；
    /// 未配对 = 只从 iCloud 知道这台 Mac（或取消配对之后），点按经 Mac 同意重新配对。
    var pairingState: PairingState { credential == .none ? .unpaired : .paired }
}

/// 多 Mac 列表的纯逻辑（两端都编译，主机上可直接测）。
enum MacDirectory {
    static func validMacId(_ id: String) -> Bool {
        (8...64).contains(id.count)
            && id.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-") }
    }

    /// Mac 侧写出的同步条目。macId 必带：没有它，第二台 Mac 会盖掉第一台（75 的缺陷）。
    /// 只带请求凭据，从不带共享密钥。
    static func outgoing(url: String, ticket: String, mac: String, macId: String, now: Date) -> PhoneConnection {
        PhoneConnection(url: url, mac: mac, macId: macId, key: nil, ticket: ticket,
                        updated: now.timeIntervalSince1970)
    }

    /// Mac 侧：75 留下的全局旧条目是不是本机写的。只删自己的；另一台 Mac（可能还停在 75）
    /// 写的那条留着，它的 iPhone 还要靠它连。
    static func ownsLegacy(_ legacy: PhoneConnection, name: String, url: String) -> Bool {
        legacy.macId == nil && (legacy.url == url || (!name.isEmpty && legacy.mac == name))
    }

    /// iPhone 本机钥匙串（不同步）：每台 Mac 的票据（或旧连接的共享密钥），与 DenyApprovalIntent 一致
    static func keyAccount(_ id: String) -> String { "mac-key." + id }
    /// iPhone 本机钥匙串：每台 Mac 的请求凭据（来自 iCloud 条目）
    static func ticketAccount(_ id: String) -> String { "mac-ticket." + id }

    /// Mac 侧：同一服务地址下、macId 不是本机的条目——本机 macId 丢失（重装、清 UserDefaults）
    /// 后留下的旧身份。它还装着旧密钥，iPhone 会看到一台永远 401 的「Mac」，写入时一并清掉。
    static func staleOwnItems(_ items: [PhoneConnection], macId: String, url: String) -> [String] {
        items.compactMap { item in
            guard let id = item.macId, id != macId, item.url == url else { return nil }
            return id
        }
    }

    static func host(_ url: String) -> String? { URL(string: url)?.host()?.lowercased() }

    /// 后端（claim / upgrade / 握手）自报的 instance_id 能不能记到这一项上：格式合法，
    /// 且列表里没有别的项已经用着它（否则推送与审批的路由会被这条冒名的项截走）。
    static func adoptableId(_ claimed: String?, for id: String, macs: [SavedMac]) -> String? {
        guard let claimed, validMacId(claimed),
              !macs.contains(where: { $0.id != id && $0.macId == claimed }) else { return nil }
        return claimed
    }

    static func displayName(_ url: String) -> String {
        guard let host = URL(string: url)?.host(), !host.isEmpty else { return url }
        return String(host.split(separator: ".").first ?? Substring(host))
    }

    struct Merged: Equatable {
        var macs: [SavedMac]
        /// 75 旧条目里的共享密钥（SavedMac.id → key），只给没有设备票据的项：点按时拿它换本机的设备票据
        /// （POST /pair/upgrade）。从不写成本机凭据——iCloud 只让 Mac 被发现，配对要你点。
        var legacyKeys: [String: String]
        /// 需要写进本机钥匙串的请求凭据（SavedMac.id → ticket）
        var tickets: [String: String]
        /// 移出列表的未配对项（iCloud 里已经没有它了）：调用方清掉它名下残留的请求凭据
        var dropped: [String]
    }

    /// 把 iCloud 里读到的全部条目并进本机列表。规则：
    /// - 按 macId 认 Mac；本机旧项（75 迁移来的、没有 macId）按地址认领 macId；
    /// - 只有 iCloud 来源的项会被更新地址、名字与请求凭据，扫码与手填的值不被静默覆盖；
    ///   旧条目里的共享密钥从不变成本机凭据（未配对的项点按时才拿它换票据）；用户改过的名字不被覆盖；
    /// - 新 Mac 追加为 iCloud 来源（还没有凭据，要先经 Mac 同意）；取消配对过的照常列出（未配对），
    ///   不记「删过」——77 及以前把它藏起来，用户就再也找不回那台 Mac；
    /// - iCloud 里已经没有的 iCloud 项：已配对的标成 removedFromICloud，不自动删（仍可使用）；
    ///   未配对的移出列表（手里没有凭据，也没有条目可凭着重新配对）。
    /// 只在读取成功时调用（读失败不能据此把 Mac 标成已移除）。读到零条不移出任何项（退出 iCloud 等
    /// 「没有信息」的情形）；pinned（当前那台、正在配对的那台）也从不移出。
    static func merge(_ macs: [SavedMac], synced: [PhoneConnection], pinned: Set<String>,
                      now: Date, newId: () -> String) -> Merged {
        var out = macs
        var legacyKeys: [String: String] = [:]
        var tickets: [String: String] = [:]
        var seen = Set<String>()
        // 75 全局条目与某台 Mac 自己的条目同地址时（那台 Mac 刚升级），以带 macId 的为准
        let tagged = Set(synced.filter { $0.macId != nil }.map(\.url))
        for connection in synced {
            if connection.macId == nil, tagged.contains(connection.url) { continue }
            var index: Int?
            if let macId = connection.macId {
                // iCloud 条目来自同一 Apple ID 的钥匙串，比后端自报的 instance_id 可信：
                // 一条扫码 / 手填的项若自称同一个 macId、却在另一台主机上，是冒名，撤掉它的 macId
                for j in out.indices where out[j].macId == macId && out[j].source != .icloud
                    && host(out[j].url) != host(connection.url) {
                    out[j].macId = nil
                }
                index = out.firstIndex { $0.macId == macId }
                // 75 迁移来的项没有 macId：先按地址认，地址变过（http→https）再按 tailnet 名认
                let adopt = out.firstIndex { $0.macId == nil && $0.url == connection.url }
                    ?? out.firstIndex { $0.macId == nil && $0.source == .icloud && !connection.mac.isEmpty
                                        && displayName($0.url) == connection.mac }
                if index == nil, let adopt {
                    out[adopt].macId = macId
                    index = adopt
                }
            } else {
                index = out.firstIndex { $0.url == connection.url }
            }
            let legacyKey = connection.macId == nil ? connection.key.flatMap { $0.isEmpty ? nil : $0 } : nil
            guard let i = index else {
                let mac = SavedMac(id: newId(), macId: connection.macId,
                                   name: connection.mac.isEmpty ? displayName(connection.url) : connection.mac,
                                   url: connection.url, source: .icloud, addedAt: now,
                                   removedFromICloud: false,
                                   credential: .none, customName: false)
                out.append(mac)
                seen.insert(mac.id)
                if let legacyKey { legacyKeys[mac.id] = legacyKey }
                if let ticket = connection.ticket, !ticket.isEmpty { tickets[mac.id] = ticket }
                continue
            }
            seen.insert(out[i].id)
            guard out[i].source == .icloud else { continue }
            out[i].url = connection.url
            if !connection.mac.isEmpty, !out[i].customName { out[i].name = connection.mac }
            out[i].removedFromICloud = false
            if let ticket = connection.ticket, !ticket.isEmpty { tickets[out[i].id] = ticket }
            if let legacyKey { legacyKeys[out[i].id] = legacyKey }
        }
        let gone = out.filter { $0.source == .icloud && !seen.contains($0.id) }
        // 只有没有任何凭据的项会被移出（按构造：credential == .none）
        let dropped = synced.isEmpty ? [] : gone.filter { $0.credential == .none && !pinned.contains($0.id) }.map(\.id)
        out.removeAll { dropped.contains($0.id) }
        for i in out.indices where out[i].source == .icloud && !seen.contains(out[i].id) {
            out[i].removedFromICloud = true
        }
        // 设备票据从不被共享密钥顶替（也不拿它绕过 Mac 的吊销）：只给没有设备票据的项
        let eligible = Set(out.filter { $0.credential != .deviceToken }.map(\.id))
        legacyKeys = legacyKeys.filter { eligible.contains($0.key) }
        return Merged(macs: out, legacyKeys: legacyKeys, tickets: tickets, dropped: dropped)
    }

    struct Unpaired: Equatable {
        var macs: [SavedMac]
        /// 手里是设备票据：请那台 Mac 一并吊销它（PairingAPI.forgetSelf），设备列表里不留一行
        var forgetSelf: Bool
        /// 本机钥匙串里要清空的账户（票据 / 共享密钥与请求凭据）
        var clearedAccounts: [String]
    }

    /// 取消配对（iPhone 左滑「取消配对」）：本机凭据一律清掉。iCloud 里还列着的那台留在列表里、
    /// 变成未配对（同一个 id，点按即经 Mac 同意重新配对）；扫码 / 手填的、或 Mac 已停止同步的
    /// 移出列表——它若还在 iCloud 里，下一次合并会把它作为未配对的 Mac 重新列出。
    static func unpair(_ macs: [SavedMac], id: String) -> Unpaired? {
        guard let i = macs.firstIndex(where: { $0.id == id }) else { return nil }
        var out = macs
        let forget = out[i].credential == .deviceToken
        if out[i].source == .icloud, !out[i].removedFromICloud {
            out[i].credential = .none
        } else {
            out.remove(at: i)
        }
        return Unpaired(macs: out, forgetSelf: forget, clearedAccounts: [keyAccount(id), ticketAccount(id)])
    }

    /// 扫码配对 / 手填：同一台 Mac（macId 相同，或地址相同）就地更新，否则追加。返回该项 id。
    static func upsert(_ macs: [SavedMac], url: String, macId: String?, source: ConnectionSource,
                       credential: Credential, now: Date,
                       newId: () -> String) -> (macs: [SavedMac], id: String) {
        var out = macs
        var macId = macId
        // claim 的 instance_id 来自免鉴权接口：主机名也对得上才认作已保存的那台（同一台换
        // http/https 或端口照常就地更新；tailnet 内主机名唯一），否则一台自称同 id 的后端
        // 就能把已保存的 Mac 悄悄改指到自己身上。
        if let id = macId, let known = out.first(where: { $0.macId == id }),
           URL(string: known.url)?.host()?.lowercased() != URL(string: url)?.host()?.lowercased() {
            macId = nil
        }
        let byId = macId.flatMap { id in out.firstIndex { $0.macId == id } }
        if let i = byId ?? out.firstIndex(where: { $0.url == url }) {
            out[i].url = url
            out[i].source = source
            out[i].credential = credential
            if let macId { out[i].macId = macId }
            out[i].removedFromICloud = false
            return (out, out[i].id)
        }
        let mac = SavedMac(id: newId(), macId: macId, name: displayName(url), url: url,
                           source: source, addedAt: now, removedFromICloud: false,
                           credential: credential, customName: false)
        out.append(mac)
        return (out, mac.id)
    }

    /// 75 的单连接迁成列表第一项（只在列表为空、已完成设置时）。
    static func migrate(url: String, source: ConnectionSource, now: Date, id: String) -> SavedMac? {
        let url = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty else { return nil }
        return SavedMac(id: id, macId: nil, name: displayName(url), url: url, source: source,
                        addedAt: now, removedFromICloud: false, credential: .sharedKey, customName: false)
    }

    /// 点选一台时要不要先经 Mac 同意（握手）：还没凭据、读不出凭据、或当前这台已拒绝本机的
    /// 票据。freshCredential = 刚换到了这台的新凭据（扫码 / 手填）：之前的拒绝已不算数，
    /// 直接采用，绝不再为它发 /pair/request。
    enum PairingRoute: Equatable { case legacyUpgrade(key: String), request }
    /// 要配对的那一项走哪条路：iCloud 里有 75 旧条目的共享密钥、且本机没有可用凭据（未配对，或
    /// 共享密钥那一项的本机密钥丢了）→ 拿它换设备票据（POST /pair/upgrade）；其余凭请求凭据握手。
    /// 设备票据被拒的项从不走共享密钥：那是 Mac 的吊销，要重新经 Mac 同意。
    static func pairingRoute(credential: Credential, hasKey: Bool, legacyKey: String?) -> PairingRoute {
        if let legacyKey, !legacyKey.isEmpty,
           credential == .none || (credential == .sharedKey && !hasKey) {
            return .legacyUpgrade(key: legacyKey)
        }
        return .request
    }

    static func needsHandshake(credential: Credential, hasKey: Bool, isCurrent: Bool,
                               currentRejected: Bool, freshCredential: Bool) -> Bool {
        if credential == .none || !hasKey { return true }
        if freshCredential { return false }
        return isCurrent && currentRejected
    }

    /// 扫码换到票据、等完旧票据的注销之后：在**最新**的列表上落位（新项沿用计划时的 id）。
    /// 返回票据该存在哪个 id 下；计划时的 id 若已不在列表里（等待期间被删 / 被别的项取代），
    /// 一并返回，调用方清掉它名下可能残留的凭据。
    static func finalizeClaim(_ macs: [SavedMac], plannedId: String, url: String, macId: String?,
                              now: Date) -> (macs: [SavedMac], id: String, orphan: String?) {
        let result = upsert(macs, url: url, macId: macId, source: .qr, credential: .deviceToken,
                            now: now, newId: { plannedId })
        let orphan = result.id != plannedId && !result.macs.contains(where: { $0.id == plannedId }) ? plannedId : nil
        return (result.macs, result.id, orphan)
    }

    /// 推送 / 灵动岛事件来自哪台：src 认得出就是那台，认不出（旧后端、手填的 Mac）退回当前那台。
    static func route(src: String?, macs: [SavedMac], current: String?) -> String? {
        // 未配对的那台不会发来推送；就算对得上也不切过去（切过去就得配对）
        if let src, let mac = macs.first(where: { $0.macId == src }), mac.pairingState == .paired { return mac.id }
        return current
    }

    /// 审批（允许 / 拒绝）回给哪台：规则见 ApprovalRouting.target（与灵动岛共用一份）。
    static func approvalTarget(src: String?, macs: [SavedMac], current: String?) -> String? {
        ApprovalRouting.target(src: src, macs: macs.map { ApprovalRouting.Mac(id: $0.id, macId: $0.macId) },
                               current: current)
    }
}

/// 二维码里的配对链接：macagent://pair?u=<Mac 的 tailnet 地址>&c=<一次性配对码>。
/// iPhone 系统相机扫到即可唤起 app。链接可被任何网页/app 触发，所以构造时严格校验
/// （地址只收 MagicDNS 的 *.ts.net——Mac 侧只会从 tailscale serve 拿到这种地址），
/// 且 app 内必须经用户确认完整地址后才去换密钥。
struct PairingLink: Equatable, Identifiable {
    let base: URL
    let code: String
    var id: String { code }
    var host: String { base.host() ?? base.absoluteString }

    init?(base: URL, code: String) {
        guard let scheme = base.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = base.host()?.lowercased(), host.hasSuffix(".ts.net"), host.count > 7,
              base.user == nil, base.password == nil, base.query == nil, base.fragment == nil,
              base.path.isEmpty || base.path == "/",
              (16...64).contains(code.count),
              code.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") })
        else { return nil }
        self.base = base
        self.code = code
    }

    init?(url: URL) {
        guard url.scheme == "macagent", url.host() == "pair",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let u = items.first(where: { $0.name == "u" })?.value, let base = URL(string: u),
              let c = items.first(where: { $0.name == "c" })?.value
        else { return nil }
        self.init(base: base, code: c)
    }

    var url: URL {
        var c = URLComponents()
        c.scheme = "macagent"
        c.host = "pair"
        c.queryItems = [URLQueryItem(name: "u", value: base.absoluteString),
                        URLQueryItem(name: "c", value: code)]
        return c.url!
    }
}

/// 连接与设备管理的 HTTP 面（backend/framework/api/pairing.py）。独立于 AgentClient：
/// 扫码与握手都发生在 iPhone 还没有任何凭据的时候。
enum PairingAPI {
    struct Started: Decodable {
        let code: String
        let expires_in: Int
    }

    enum Failure: LocalizedError {
        case refused(String)
        case http(Int)
        /// 5xx：服务端（或 serve 反代）一时出错，不论响应体带没带 detail，都不是 Mac 的回答
        case server(Int, String?)
        case badResponse

        var errorDescription: String? {
            switch self {
            case .refused(let detail): return detail
            case .http(let code): return "HTTP \(code)"
            case .server(let code, let detail): return detail ?? "HTTP \(code)"
            case .badResponse: return L("后端返回异常")
            }
        }
    }

    /// 扫码、握手同意、旧连接升级拿到的设备票据。
    struct Issued: Decodable {
        let token: String
        let device_id: String
        /// 后端所在 Mac 的 macId（Mac app 注入时才有）：iPhone 的 Mac 列表据此认出同一台
        let instance_id: String?
    }

    struct Request: Decodable {
        let request_id: String
        let poll_secret: String
        let code: String
        let expires_in: Int
    }

    struct Poll: Decodable {
        let status: String           // pending | approved | denied
        let token: String?
        let device_id: String?
        let instance_id: String?
    }

    struct Pending: Decodable, Identifiable, Equatable {
        let id: String
        let code: String
        let name: String
        let platform: String
    }

    struct Device: Decodable, Identifiable, Equatable {
        let id: String
        let name: String
        let via: String
        let platform: String
        let created: Double
        let last_used: Double
        let expires: Double?
    }

    struct Devices: Decodable {
        let devices: [Device]
        let default_ttl_days: Int?
        let notice: String?
    }

    private struct Ticket: Decodable { let ticket: String }
    private struct Requests: Decodable { let requests: [Pending] }
    private struct Ignored: Decodable {}
    private static let session = URLSession(configuration: .ephemeral)

    /// iPhone 自报的设备信息（名字留空 = Mac 端默认名「iPhone（10-25 连接）」，可在 Mac 上改）。
    static func identity(pushDeviceId: String?) -> [String: Any] {
        var body: [String: Any] = ["name": ""]
        #if os(iOS)
        body["platform"] = UIDevice.current.userInterfaceIdiom == .pad ? "ipados" : "ios"
        #else
        body["platform"] = "macos"
        #endif
        if let pushDeviceId { body["push_device_id"] = pushDeviceId }
        return body
    }

    // ---- Mac（共享密钥，经回环） ----
    static func start(base: URL, key: String) async throws -> Started {
        try await call(base, "POST", "/api/v1/pair/start", body: [:], key: key)
    }

    static func ticket(base: URL, key: String) async throws -> String {
        let t: Ticket = try await call(base, "GET", "/api/v1/pair/ticket", body: nil, key: key)
        return t.ticket
    }

    static func rotateTicket(base: URL, key: String) async throws -> String {
        let t: Ticket = try await call(base, "POST", "/api/v1/pair/ticket/rotate", body: [:], key: key)
        return t.ticket
    }

    static func pending(base: URL, key: String) async throws -> [Pending] {
        let r: Requests = try await call(base, "GET", "/api/v1/pair/requests", body: nil, key: key)
        return r.requests
    }

    static func decide(base: URL, key: String, id: String, approve: Bool) async throws {
        let _: Ignored = try await call(base, "POST", "/api/v1/pair/requests/\(id)/\(approve ? "approve" : "deny")",
                                        body: [:], key: key)
    }

    static func devices(base: URL, key: String) async throws -> Devices {
        try await call(base, "GET", "/api/v1/pair/devices", body: nil, key: key)
    }

    static func updateDevice(base: URL, key: String, id: String, body: [String: Any]) async throws {
        let _: Ignored = try await call(base, "PATCH", "/api/v1/pair/devices/\(id)", body: body, key: key)
    }

    static func revoke(base: URL, key: String, id: String) async throws {
        let _: Ignored = try await call(base, "DELETE", "/api/v1/pair/devices/\(id)", body: nil, key: key)
    }

    static func setDefaultTTL(base: URL, key: String, days: Int?) async throws {
        let _: Ignored = try await call(base, "PUT", "/api/v1/pair/settings",
                                        body: ["default_ttl_days": days as Any? ?? NSNull()], key: key)
    }

    // ---- iPhone ----
    static func claim(_ link: PairingLink, pushDeviceId: String?) async throws -> Issued {
        var body = identity(pushDeviceId: pushDeviceId)
        body["code"] = link.code
        return try await issued(call(link.base, "POST", "/api/v1/pair/claim", body: body, key: ""))
    }

    static func request(base: URL, ticket: String, pushDeviceId: String?) async throws -> Request {
        var body = identity(pushDeviceId: pushDeviceId)
        body["ticket"] = ticket
        return try await call(base, "POST", "/api/v1/pair/request", body: body, key: "")
    }

    static func poll(base: URL, request: Request) async throws -> Poll {
        try await call(base, "POST", "/api/v1/pair/request/\(request.request_id)/poll",
                       body: ["poll_secret": request.poll_secret], key: "")
    }

    /// 手里还是共享密钥的旧连接：换一张这部 iPhone 自己的票据。
    static func upgrade(base: URL, key: String, pushDeviceId: String?) async throws -> Issued {
        try await issued(call(base, "POST", "/api/v1/pair/upgrade",
                              body: identity(pushDeviceId: pushDeviceId), key: key))
    }

    static func forgetSelf(base: URL, token: String, timeout: TimeInterval = 10) async throws {
        let _: Ignored = try await call(base, "DELETE", "/api/v1/pair/self", body: nil, key: token, timeout: timeout)
    }

    /// 不带 Authorization 打一个受鉴权的接口：401 = 后端开了鉴权；200（或别的）= 没开，
    /// 谁都能进；nil = 没有响应（执行层没起）。
    static func unauthenticatedStatus(base: URL) async -> Int? {
        guard let url = URL(string: "/api/v1/pair/devices", relativeTo: base) else { return nil }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5)
        req.httpMethod = "GET"
        guard let (_, response) = try? await session.data(for: req) else { return nil }
        return (response as? HTTPURLResponse)?.statusCode
    }

    private static func issued(_ value: Issued) throws -> Issued {
        guard !value.token.isEmpty else { throw Failure.badResponse }
        return value
    }

    private static func call<T: Decodable>(_ base: URL, _ method: String, _ path: String,
                                           body: [String: Any]?, key: String,
                                           timeout: TimeInterval = 10) async throws -> T {
        guard let url = URL(string: path, relativeTo: base) else { throw Failure.badResponse }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        req.httpMethod = method
        if !key.isEmpty { req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: req)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            if code >= 500 { throw Failure.server(code, detail) }
            throw detail.map(Failure.refused) ?? Failure.http(code)
        }
        guard let decoded = try? JSONDecoder().decode(T.self, from: data) else { throw Failure.badResponse }
        return decoded
    }
}

/// 旧连接升级（POST /pair/upgrade）失败后的退避：同一台 Mac 失败后等 1、2、4……分钟，
/// 最长 1 小时；期间连接状态再怎么变也不重发。换到票据、或这台的地址 / 密钥变了就从头算。
/// 按 fingerprint（地址 + 密钥的哈希，密钥本身不落盘）持久到 UserDefaults：app 重启不重置。
/// 纯值类型（两端都编译，主机上可直接测）。
struct UpgradeBackoff {
    static let base: TimeInterval = 60
    static let cap: TimeInterval = 3600
    static let defaultsKey = "phone.upgradeBackoff"

    private struct Entry: Codable {
        var failures: Int
        var next: Date
    }
    private let defaults: UserDefaults?
    private var entries: [String: Entry] = [:]

    /// defaults 为 nil 只在内存里记（测试用）。
    init(defaults: UserDefaults? = .standard) {
        self.defaults = defaults
        if let data = defaults?.data(forKey: Self.defaultsKey),
           let stored = try? JSONDecoder().decode([String: Entry].self, from: data) {
            entries = stored
        }
    }

    /// 这台 Mac 此刻的配置：地址 + 凭据。变了说明配置换过，旧的失败不再算数。
    static func fingerprint(url: String, key: String) -> String {
        SHA256.hash(data: Data((url + "\n" + key).utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// 第 n 次连续失败之后要等多久。
    static func delay(afterFailures n: Int) -> TimeInterval {
        guard n > 0 else { return 0 }
        return min(cap, base * pow(2, Double(min(n - 1, 16))))
    }

    func allows(_ fingerprint: String, now: Date) -> Bool {
        guard let entry = entries[fingerprint] else { return true }
        return now >= entry.next
    }

    mutating func failed(_ fingerprint: String, now: Date) {
        let failures = (entries[fingerprint]?.failures ?? 0) + 1
        entries[fingerprint] = Entry(failures: failures,
                                     next: now.addingTimeInterval(Self.delay(afterFailures: failures)))
        persist(now: now)
    }

    mutating func succeeded(_ fingerprint: String) {
        guard entries.removeValue(forKey: fingerprint) != nil else { return }
        persist(now: Date())
    }

    /// 早已过了等待期的旧配置（换过密钥 / 地址）不再留着。
    private mutating func persist(now: Date) {
        entries = entries.filter { $0.value.next > now.addingTimeInterval(-Self.cap) }
        guard let defaults else { return }
        if entries.isEmpty { defaults.removeObject(forKey: Self.defaultsKey) }
        else if let data = try? JSONEncoder().encode(entries) { defaults.set(data, forKey: Self.defaultsKey) }
    }
}

/// 同一台 Mac 上换设备票据（扫码、握手同意）：手里还有这台的旧设备票据时，先用它向那台
/// Mac 注销自己（DELETE /api/v1/pair/self），再保存新票据——后端只在旧票据到期后才自动
/// 取代同一推送设备的旧记录。best effort：最多等 timeout，失败只记日志，绝不挡住换新。
/// 共享密钥不是设备票据（后端回 400），不走这里。
enum TicketHandover {
    static let timeout: TimeInterval = 5

    @MainActor
    static func replace(previous: String?, at base: URL?, timeout: TimeInterval = timeout,
                        forget: @escaping @Sendable (URL, String) async throws -> Void,
                        save: () -> Void, log: (String) -> Void = { print($0) }) async {
        if let previous, !previous.isEmpty, let base {
            let outcome: Bool? = await withTaskGroup(of: Bool?.self) { group in
                group.addTask {
                    do { try await forget(base, previous); return true } catch { return false }
                }
                group.addTask {
                    try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                    return nil
                }
                let first = await group.next() ?? nil
                group.cancelAll()
                return first
            }
            if outcome != true {
                log("[Pair] 注销旧设备票据" + (outcome == nil ? "超时" : "失败") + "（\(base.host() ?? "")），继续换新")
            }
        }
        save()
    }
}

/// 握手轮询：瞬时的网络错误（tailnet 抖动、Mac 睡眠唤醒、后端重启时 serve 回 502/503）
/// 不立即结束等待；连续 maxConsecutiveErrors 次才放弃。Mac 明确回答的错误（拒绝、
/// 请求已不存在、响应不对）一次就结束。
enum HandshakePolling {
    static let maxConsecutiveErrors = 5

    static func isTransient(_ error: Error) -> Bool {
        if let error = error as? URLError { return error.code != .cancelled }
        if case PairingAPI.Failure.server = error { return true }
        if case PairingAPI.Failure.http(let code) = error { return code >= 500 || code == 0 }
        return false
    }

    /// consecutive = 算上这一次、连续失败了几次。
    static func keepsWaiting(after error: Error, consecutive: Int) -> Bool {
        isTransient(error) && consecutive < maxConsecutiveErrors
    }
}

#if os(macOS)
/// 本机的稳定标识：iCloud 同步条目的 account 后缀，也经 AGENT_INSTANCE_ID 注入内建执行层
/// （推送与配对据此告诉 iPhone「来自哪台 Mac」）。首次生成后存 UserDefaults。
enum MacIdentity {
    static let id: String = {
        let defaults = UserDefaults.standard
        if let id = defaults.string(forKey: "phone.macId"), MacDirectory.validMacId(id) { return id }
        let id = UUID().uuidString.lowercased()
        defaults.set(id, forKey: "phone.macId")
        return id
    }()
}

/// Mac 本机的 Tailscale 状态解析（纯函数，便于测试）。
enum TailnetParse {
    struct SelfNode: Equatable {
        let name: String        // tailnet 短名（iPhone 上展示用）
        let running: Bool
        let canHTTPS: Bool      // tailnet 开了 HTTPS 证书
    }

    static func selfNode(_ data: Data) -> SelfNode? {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let me = json["Self"] as? [String: Any],
              let dns = me["DNSName"] as? String else { return nil }
        let certs = json["CertDomains"] as? [String] ?? []
        return SelfNode(name: String(dns.split(separator: ".").first ?? ""),
                        running: json["BackendState"] as? String == "Running",
                        canHTTPS: !certs.isEmpty)
    }

    private static let backendTargets: Set<String> = {
        let local = AppSettings.localBackend                      // http://127.0.0.1:14159
        let bare = local.replacingOccurrences(of: "http://", with: "")
        return [local, bare, local.replacingOccurrences(of: "127.0.0.1", with: "localhost"),
                bare.replacingOccurrences(of: "127.0.0.1", with: "localhost")]
    }()

    /// serve 配置里反代到本机 14159 的入口 → iPhone 该用的地址；nil = 没开放。HTTPS 优先。
    static func servedURL(_ data: Data) -> URL? {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let web = json["Web"] as? [String: Any] else { return nil }
        let tcp = json["TCP"] as? [String: Any] ?? [:]
        var found: [URL] = []
        for (hostPort, value) in web {
            guard let handlers = (value as? [String: Any])?["Handlers"] as? [String: Any],
                  var proxy = (handlers["/"] as? [String: Any])?["Proxy"] as? String else { continue }
            while proxy.hasSuffix("/") { proxy.removeLast() }
            guard backendTargets.contains(proxy),
                  let colon = hostPort.lastIndex(of: ":") else { continue }
            let host = String(hostPort[..<colon])
            let port = String(hostPort[hostPort.index(after: colon)...])
            let https = (tcp[port] as? [String: Any])?["HTTPS"] as? Bool == true
            let defaultPort = https ? "443" : "80"
            let text = "\(https ? "https" : "http")://\(host)" + (port == defaultPort ? "" : ":\(port)")
            if let url = URL(string: text) { found.append(url) }
        }
        return found.sorted { ($0.scheme == "https" ? 0 : 1, $0.absoluteString)
                              < ($1.scheme == "https" ? 0 : 1, $1.absoluteString) }.first
    }

    static func portInUse(_ data: Data, _ port: String) -> Bool {
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        return (json?["TCP"] as? [String: Any])?[port] != nil
    }
}

/// 调本机 tailscale CLI（Homebrew / 独立安装 / App Store 版三处位置）。
enum TailscaleCLI {
    static let candidates = ["/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale",
                             "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
    static var path: String? { candidates.first { FileManager.default.isExecutableFile(atPath: $0) } }

    struct Output {
        let code: Int32
        let out: Data
        let err: String
    }

    /// 10 秒超时：tailscaled 卡住时不能把设置页吊死。nil = 没装 CLI。
    static func run(_ args: [String]) async -> Output? {
        guard let path else { return nil }
        return await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: path)
                process.arguments = args
                let out = Pipe(), err = Pipe()
                process.standardOutput = out
                process.standardError = err
                do { try process.run() } catch {
                    cont.resume(returning: Output(code: -1, out: Data(), err: error.localizedDescription))
                    return
                }
                let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + 10, execute: timeout)
                // 两路并发排空：顺序读时 stderr 写满管道缓冲会把子进程卡到超时
                var stderr = Data()
                let drained = DispatchGroup()
                drained.enter()
                DispatchQueue.global().async {
                    stderr = err.fileHandleForReading.readDataToEndOfFile()
                    drained.leave()
                }
                let stdout = out.fileHandleForReading.readDataToEndOfFile()
                drained.wait()
                process.waitUntilExit()
                timeout.cancel()
                cont.resume(returning: Output(code: process.terminationStatus, out: stdout,
                                              err: String(decoding: stderr, as: UTF8.self)))
            }
        }
    }
}

enum QRCode {
    static func image(_ text: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)),
              let cg = CIContext().createCGImage(output, from: output.extent) else { return nil }
        return NSImage(cgImage: cg, size: NSSize(width: output.extent.width / 2,
                                                 height: output.extent.height / 2))
    }
}
#endif

/// 两端各一份：Mac 侧管「对 iPhone 开放 + 写同步条目 + 出配对码」，
/// iPhone 侧管「多 Mac 列表 + 读同步条目 + 401 自愈（只认当前那台）+ 扫码换密钥」。
@MainActor
final class PhoneLink: ObservableObject {
    private weak var settings: AppSettings?
    private var subscriptions: Set<AnyCancellable> = []

    #if os(macOS)
    enum Exposure: Equatable {
        case checking
        case noTailscale
        case offline
        case notServed(https: Bool, portTaken: Bool)
        case served(URL)
    }

    struct ActivePairing {
        let link: PairingLink
        let expires: Date
        let image: NSImage?
    }

    /// tailscale CLI、iCloud 钥匙串与本机后端的入口。生产用真的；phone_link_test 换成
    /// 假实现，开放 / 停止开放 / 同步的顺序才测得到，又不碰 CLI、钥匙串与网络。
    struct Effects {
        var tailscale: ([String]) async -> TailscaleCLI.Output? = { await TailscaleCLI.run($0) }
        var ticket: (URL, String) async throws -> String = { try await PairingAPI.ticket(base: $0, key: $1) }
        var rotateTicket: (URL, String) async throws -> String = {
            try await PairingAPI.rotateTicket(base: $0, key: $1)
        }
        var pending: (URL, String) async throws -> [PairingAPI.Pending] = {
            try await PairingAPI.pending(base: $0, key: $1)
        }
        var devices: (URL, String) async throws -> PairingAPI.Devices = {
            try await PairingAPI.devices(base: $0, key: $1)
        }
        var writeEntry: (PhoneConnection) -> OSStatus = { SharedKeychain.write($0) }
        var deleteEntry: (String) -> OSStatus = { SharedKeychain.delete(macId: $0) }
        var deleteLegacy: () -> OSStatus = { SharedKeychain.deleteLegacy() }
        var readEntries: () -> ([PhoneConnection], OSStatus) = { SharedKeychain.readAll() }
        var macId: () -> String = { MacIdentity.id }
        /// 不带密钥打受鉴权接口得到的状态码（PairingAPI.unauthenticatedStatus）。
        var probeAuth: (URL) async -> Int? = { await PairingAPI.unauthenticatedStatus(base: $0) }
        /// bundle 形态（Resources 里带执行层）：密钥在 app 设置里，不在 .env.local。
        /// 停止开放的请求要跨重启记住（见 stopRequested）。
        var defaults: UserDefaults = .standard
        var bundleLayout: () -> Bool = {
            Bundle.main.resourceURL.map {
                FileManager.default.fileExists(atPath: $0.appendingPathComponent("backend/app.py").path)
            } ?? false
        }
    }

    /// 对 tailnet 开放前，后端必须有自己的鉴权：后端的护栏只看绑定地址（回环放行），
    /// 无密钥时所有请求都拿 owner 权限，而 serve 正是把回环反代给整个 tailnet。
    static let minimumKeyLength = 16

    /// 只有 tailnet 上开着入口时才有 iPhone 能来请求连接：没开放时不轮询 /pair/requests。
    static func pollsRequests(_ exposure: Exposure) -> Bool {
        if case .served = exposure { return true }
        return false
    }

    static let target = AppSettings.localBackend
    private var effects = Effects()
    @Published private(set) var exposure: Exposure = .checking
    /// 一次停止开放正在进行（按钮防重入）。
    @Published private(set) var stopping = false
    /// 已经请求停止开放：iCloud 条目已删。直到 exposure 真正变成未开放才清除——
    /// tailscale serve off 失败、serve 仍在时，同步（含 watcher 每 3 秒的重试）也不得把
    /// 条目写回；界面显示停止未完成并可重试。
    @Published private(set) var stopRequested = false {
        didSet {
            guard stopRequested != oldValue else { return }
            effects.defaults.set(stopRequested, forKey: Self.stopRequestedKey(effects.macId()))
        }
    }
    static func stopRequestedKey(_ macId: String) -> String { "phone.stopRequested." + macId }
    /// 已开放时多久重新确认一次后端鉴权（后端修好 / 变成无钥都会反映到 authWarning）。
    static let authRecheckInterval: TimeInterval = 60
    /// 已经开放、但后端不带密钥也放行（未启用鉴权）：持久警告，不自动关 serve。
    @Published private(set) var authWarning: String?
    /// 上一次拿到明确状态码的鉴权探测时间（nil = 这次开放还没确认过）。
    private var lastAuthCheck: Date?
    @Published private(set) var syncOK = false
    @Published private(set) var syncError: String?
    @Published private(set) var busy = false
    @Published private(set) var pairing: ActivePairing?
    @Published var actionError: String?
    /// 等 Mac 同意的连接请求；prompt = 正在弹框问你的那一条
    @Published private(set) var requests: [PairingAPI.Pending] = []
    @Published var prompt: PairingAPI.Pending?
    @Published private(set) var devices: [PairingAPI.Device] = []
    @Published private(set) var defaultTTLDays: Int? = 90
    @Published private(set) var deviceNotice: String?
    private var answered = Set<String>()
    private var notified = Set<String>()
    private var macName = ""
    private var ticket = ""
    private var lastWritten: PhoneConnection?
    /// refresh 会从启动、设置页出现、按钮、开关动作四处并发进来：只认最后一次发起的结果，
    /// 免得慢的旧结果把新状态盖回去。
    private var refreshGeneration = 0
    private var watcher: Task<Void, Never>?

    func configure(settings: AppSettings, effects: Effects = Effects()) {
        self.settings = settings
        self.effects = effects
        // 上次 serve off 失败后退出：重启后仍算「已请求停止」，直到真的未开放
        stopRequested = effects.defaults.bool(forKey: Self.stopRequestedKey(effects.macId()))
        // 密钥轮换（设置页重新生成 / 执行层首启生成）后重新取请求凭据并同步
        settings.$apiKey.dropFirst().removeDuplicates()
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.ticket = ""
                    self?.lastWritten = nil
                    await self?.sync()
                }
            }
            .store(in: &subscriptions)
        Task { await refresh() }
        watch()
    }

    /// 每 3 秒问一次本机后端有没有等同意的连接请求（回环、轻量）；同步没成功时顺带重试
    /// （执行层比 app 晚起，开始时取不到请求凭据）。
    private func watch() {
        watcher?.cancel()
        watcher = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                guard let self else { return }
                if Self.pollsRequests(self.exposure) { await self.pollRequests() }
                // 启动时已经开放着：执行层可能还没起，确认到鉴权状态为止
                if case .served = self.exposure, self.authCheckDue { await self.checkServedAuth() }
                if !self.syncOK, case .served = self.exposure { await self.sync() }
                if tick % 10 == 0 { await self.refreshDevices() }
                tick += 1
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    private var local: (URL, String)? {
        guard let settings, let base = settings.baseURL, !settings.apiKey.isEmpty else { return nil }
        return (base, settings.apiKey)
    }

    private func pollRequests() async {
        guard let (base, key) = local,
              let pending = try? await effects.pending(base, key) else { return }
        requests = pending
        if let prompt, !pending.contains(prompt) { self.prompt = nil }
        if prompt == nil { prompt = pending.first { !answered.contains($0.id) } }
        for request in pending where !notified.contains(request.id) {
            notified.insert(request.id)
            if !NSApp.isActive { Self.notify(request) }
        }
    }

    /// app 不在前台时：本机通知提醒你回来点允许（点通知即激活 app，弹框在主窗口）。
    private static func notify(_ request: PairingAPI.Pending) {
        let content = UNMutableNotificationContent()
        content.title = L("iPhone 请求连接")
        content.body = "「\(request.name)」" + L("请求连接这台 Mac。核对码 ") + request.code
        content.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "pair-" + request.id, content: content, trigger: nil))
    }

    func decide(_ request: PairingAPI.Pending, approve: Bool) async {
        answered.insert(request.id)
        prompt = nil
        guard let (base, key) = local else { return }
        do {
            try await PairingAPI.decide(base: base, key: key, id: request.id, approve: approve)
        } catch {
            actionError = L("处理连接请求失败：") + error.localizedDescription
        }
        await pollRequests()
        if approve {
            // 设备记录在 iPhone 领走票据时才建：稍等再刷新列表
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            await refreshDevices()
        }
    }

    func refreshDevices() async {
        guard let (base, key) = local, let listing = try? await effects.devices(base, key)
        else { return }
        devices = listing.devices
        defaultTTLDays = listing.default_ttl_days
        deviceNotice = listing.notice
    }

    func renameDevice(_ id: String, to name: String) async {
        await manage { base, key in try await PairingAPI.updateDevice(base: base, key: key, id: id, body: ["name": name]) }
    }

    func setPermanent(_ id: String, _ permanent: Bool) async {
        await manage { base, key in
            try await PairingAPI.updateDevice(base: base, key: key, id: id, body: ["permanent": permanent])
        }
    }

    func revokeDevice(_ id: String) async {
        await manage { base, key in try await PairingAPI.revoke(base: base, key: key, id: id) }
    }

    /// 新设备默认有效期：开 = 90 天，关 = 永久。
    func setDefaultExpiry(_ on: Bool) async {
        await manage { base, key in try await PairingAPI.setDefaultTTL(base: base, key: key, days: on ? 90 : nil) }
    }

    /// 重新生成请求凭据：旧凭据与所有等同意的请求随即作废，iCloud 条目随之更新。
    func rotateTicket() async {
        await manage { [weak self] base, key in
            let fresh = try await PairingAPI.rotateTicket(base: base, key: key)
            await MainActor.run {
                self?.ticket = fresh
                self?.lastWritten = nil
            }
        }
        await sync()
    }

    /// 停止同步 / 停止开放：iPhone 本机缓存的旧凭据随之作废，不能再来弹请求。
    private func voidTicket() async {
        guard let (base, key) = local, let fresh = try? await effects.rotateTicket(base, key)
        else { return }
        ticket = fresh
    }

    private func manage(_ action: @escaping (URL, String) async throws -> Void) async {
        guard let (base, key) = local else { return }
        actionError = nil
        do {
            try await action(base, key)
        } catch {
            actionError = L("操作失败：") + error.localizedDescription
        }
        await refreshDevices()
    }

    func refresh() async {
        refreshGeneration += 1
        let generation = refreshGeneration
        let status = await effects.tailscale(["status", "--json"])
        let me = status.flatMap { $0.code == 0 ? TailnetParse.selfNode($0.out) : nil }
        let serve = me?.running == true ? await effects.tailscale(["serve", "status", "--json"]) : nil
        guard generation == refreshGeneration else { return }
        guard status != nil else {
            exposure = .noTailscale
            return
        }
        guard let me, me.running else {
            exposure = .offline
            return
        }
        macName = me.name
        let config = serve?.code == 0 ? serve!.out : Data("{}".utf8)
        if let url = TailnetParse.servedURL(config) {
            exposure = .served(url)
        } else {
            exposure = .notServed(https: me.canHTTPS,
                                  portTaken: TailnetParse.portInUse(config, me.canHTTPS ? "443" : "14159"))
            stopRequested = false
            lastAuthCheck = nil
            authWarning = nil
        }
        // 每次 refresh 都重新确认（watcher 另外每 60 秒一次）
        if case .served = exposure { await checkServedAuth() }
        await sync()
        await refreshDevices()
    }

    /// 已开放时确认后端真的要密钥；不要就挂上持久警告（serve 不自动关，由你决定）。
    /// 没响应（执行层还没起）不下结论，watcher 下一轮再查。
    private var authCheckDue: Bool {
        guard let lastAuthCheck else { return true }
        return Date().timeIntervalSince(lastAuthCheck) >= Self.authRecheckInterval
    }

    private func checkServedAuth() async {
        guard let base = settings?.baseURL, let status = await effects.probeAuth(base) else { return }
        guard case .served = exposure else { return }
        lastAuthCheck = Date()
        switch status {
        case 401:
            authWarning = nil
        case 404:
            authWarning = L("后端版本过旧或地址不对，无法确认鉴权。")
        default:
            authWarning = effects.bundleLayout()
                ? L("警告：已对 tailnet 开放，但后端未启用鉴权，tailnet 上的任何设备都能免密钥访问。请先停止开放，再在设置里重新生成密钥。")
                : L("警告：已对 tailnet 开放，但后端未启用鉴权，tailnet 上的任何设备都能免密钥访问。请先停止开放，再在 .env.local 设置 AGENT_API_KEY 并重启后端。")
        }
    }

    /// nil = 可以开放；否则是拒绝开放的原因（设置页据此禁用按钮并显示同一句话）。
    var exposeRefusal: String? {
        let key = settings?.apiKey.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard key.count < Self.minimumKeyLength else { return nil }
        return effects.bundleLayout()
            ? L("密钥未配置或过短，不能对外开放。请在设置里重新生成密钥。")
            : L("密钥未配置或过短，不能对外开放。先在 .env.local 设置 AGENT_API_KEY 或运行 ./devctl.sh setup。")
    }

    /// 由用户在设置页亲手点：serve 配置是 tailscaled 的持久状态，重启后仍在。
    func expose() async {
        guard case .notServed(let https, let taken) = exposure, !taken else { return }
        if let refusal = exposeRefusal {
            actionError = refusal
            return
        }
        // app 里有钥不等于后端开了鉴权（repo 布局：钥匙串留着旧钥、后端却没配）：
        // 不带密钥问一次受鉴权接口，必须是 401 才开放。
        guard !busy, let base = settings?.baseURL else { return }
        busy = true
        actionError = nil
        let status = await effects.probeAuth(base)
        busy = false
        guard case .notServed = exposure else { return }
        guard status == 401 else {
            switch status {
            case nil:
                actionError = L("无法确认后端已启用鉴权：执行层没有响应，请稍后再试。")
            case 404:
                actionError = L("后端版本过旧或地址不对，无法确认鉴权。")
            default:
                actionError = effects.bundleLayout()
                    ? L("后端未启用鉴权（不带密钥的请求也被放行），不能对外开放。请在设置里重新生成密钥。")
                    : L("后端未启用鉴权（不带密钥的请求也被放行），不能对外开放。先在 .env.local 设置 AGENT_API_KEY 并重启后端。")
            }
            return
        }
        let args = ["serve", "--bg", https ? "--https=443" : "--http=14159", Self.target]
        await runAction(args, failure: L("开放失败："))
    }

    /// 先进入「已请求停止」（sync 不再写回），再删条目、作废请求凭据、停 serve。
    /// serve off 失败时 stopRequested 留着，再点一次即重试。
    func unexpose() async {
        guard case .served(let url) = exposure, !stopping else { return }
        stopping = true
        stopRequested = true
        defer { stopping = false }
        let https = url.scheme == "https"
        let port = url.port ?? (https ? 443 : 80)
        _ = effects.deleteEntry(effects.macId())
        removeOwnLegacy(url: url.absoluteString)
        lastWritten = nil
        syncOK = false
        await voidTicket()
        await runAction(["serve", https ? "--https=\(port)" : "--http=\(port)", "off"],
                        failure: L("关闭失败："))
    }

    private func runAction(_ args: [String], failure: String) async {
        busy = true
        defer { busy = false }
        actionError = nil
        let result = await effects.tailscale(args)
        if result?.code != 0 {
            let reason = String((result?.err ?? "").trimmingCharacters(in: .whitespacesAndNewlines).prefix(300))
            let command = "tailscale " + args.joined(separator: " ")
            actionError = "\(failure)\(reason)\n\(L("也可以在终端运行："))\(command)"
        }
        await refresh()
    }

    func setSyncEnabled(_ on: Bool) {
        settings?.phoneSyncEnabled = on
        lastWritten = nil
        if on {
            Task { await sync() }
        } else {
            let status = effects.deleteEntry(effects.macId())
            if case .served(let url) = exposure { removeOwnLegacy(url: url.absoluteString) }
            syncOK = false
            syncError = status == errSecSuccess ? nil : SharedKeychain.describe(status)
            Task { await voidTicket() }
        }
    }

    /// 写本机的 iCloud 条目：地址 + 请求凭据（向本机后端领，经回环）。共享密钥不进 iCloud。
    private func sync() async {
        guard let settings, settings.phoneSyncEnabled, !stopRequested else { return }
        guard case .served(let url) = exposure, let (base, key) = local else {
            syncOK = false
            return
        }
        if ticket.isEmpty {
            guard let fresh = try? await effects.ticket(base, key) else {
                syncOK = false
                syncError = L("执行层尚未就绪，就绪后自动同步。")
                return
            }
            ticket = fresh
        }
        // 领凭据期间可能开始了停止开放（或开放方式变了）：那就不写
        guard !stopRequested, settings.phoneSyncEnabled, case .served(url) = exposure else { return }
        let macId = effects.macId()
        let connection = MacDirectory.outgoing(url: url.absoluteString, ticket: ticket, mac: macName,
                                               macId: macId, now: Date())
        if let lastWritten, lastWritten.sameContent(connection) { return }
        let status = effects.writeEntry(connection)
        syncOK = status == errSecSuccess
        syncError = syncOK ? nil : SharedKeychain.describe(status)
        lastWritten = syncOK ? connection : nil
        guard syncOK else { return }
        let (items, readStatus) = effects.readEntries()
        guard readStatus == errSecSuccess else { return }
        // 本机丢过 macId（重装等）留下的旧身份条目：同地址、别的 macId，删掉
        for stale in MacDirectory.staleOwnItems(items, macId: macId, url: connection.url) {
            _ = effects.deleteEntry(stale)
        }
        // 本机升级前（75）写的全局条目里有共享密钥：是本机的就删掉，另一台 Mac 的不碰
        removeOwnLegacy(url: connection.url, items: items)
    }

    private func removeOwnLegacy(url: String, items: [PhoneConnection]? = nil) {
        let list: [PhoneConnection]
        if let items {
            list = items
        } else {
            let (read, status) = effects.readEntries()
            guard status == errSecSuccess else { return }
            list = read
        }
        guard list.contains(where: { MacDirectory.ownsLegacy($0, name: macName, url: url) }) else { return }
        _ = effects.deleteLegacy()
    }

    func startPairing() async {
        guard case .served(let url) = exposure, let settings, let base = settings.baseURL else { return }
        busy = true
        defer { busy = false }
        actionError = nil
        do {
            let started = try await PairingAPI.start(base: base, key: settings.apiKey)
            guard let link = PairingLink(base: url, code: started.code) else {
                throw PairingAPI.Failure.badResponse
            }
            pairing = ActivePairing(link: link,
                                    expires: Date().addingTimeInterval(TimeInterval(started.expires_in)),
                                    image: QRCode.image(link.url.absoluteString))
        } catch {
            actionError = L("生成配对码失败：") + error.localizedDescription
        }
    }

    func endPairing() { pairing = nil }
    #else
    /// 一次握手：向某台 Mac 提交了连接请求，等你在 Mac 上点允许。
    struct Handshake: Equatable {
        enum State: Equatable { case waiting, denied, expired, failed(String) }
        let macId: String           // SavedMac.id
        let macName: String
        let code: String
        var state: State
    }

    private weak var store: AgentStore?
    /// 推送登记要覆盖列表里每一台 Mac（PushManager 反过来向这里要各台的客户端）。
    weak var push: PushManager?
    @Published private(set) var macs: [SavedMac] = []
    @Published private(set) var currentId: String?
    @Published private(set) var syncedError: String?
    @Published var pendingLink: PairingLink?
    @Published private(set) var claiming = false
    @Published var claimError: String?
    @Published var handshake: Handshake?
    /// 当前那台拒绝了本机的票据（被 Mac 移除或已到期）：界面提示重新请求连接
    @Published private(set) var currentRejected = false
    private var lastHeal = Date.distantPast
    /// 最近一次读 iCloud 得到的 75 旧条目共享密钥（只在内存里，见 MacDirectory.Merged.legacyKeys）
    private var legacyKeys: [String: String] = [:]
    /// 正在用共享密钥换票据的那台：请求在途时不再发第二次（第一次可能已经发出票据）
    private var legacyUpgradeId: String?
    /// 连接请求在途（还没拿到核对码）的那台；attempt 防止被取消的旧请求清掉新请求的标记
    private var requesting: (id: String, attempt: UUID)?
    private var handshakeTask: Task<Void, Never>?
    /// 「重试」刚发起了新请求：弹窗随之关闭时（binding setter）不能把它取消。
    private var retryPending = false
    private var upgrading = Set<String>()
    private var upgradeBackoff = UpgradeBackoff()

    private static let listKey = "phone.macs"
    private static let currentKey = "phone.currentMac"
    /// 每台 Mac 的票据（或旧连接的共享密钥）：本机钥匙串、不同步（与 DenyApprovalIntent 一致）
    static func keyAccount(_ id: String) -> String { MacDirectory.keyAccount(id) }
    /// 每台 Mac 的请求凭据（来自 iCloud 条目）
    static func ticketAccount(_ id: String) -> String { MacDirectory.ticketAccount(id) }

    var current: SavedMac? { macs.first { $0.id == currentId } }

    func configure(settings: AppSettings, store: AgentStore) {
        self.settings = settings
        self.store = store
        let defaults = UserDefaults.standard
        macs = (defaults.data(forKey: Self.listKey))
            .flatMap { try? JSONDecoder().decode([SavedMac].self, from: $0) } ?? []
        currentId = defaults.string(forKey: Self.currentKey)
        defaults.removeObject(forKey: "phone.hiddenMacs")   // 77 及以前藏起「删过的 Mac」的集合，78 起不再隐藏
        // build 75 只有一条连接：迁成列表第一项（共享密钥），并设为当前；随后自动换票据
        if macs.isEmpty, settings.setupCompleted,
           let first = MacDirectory.migrate(url: settings.baseURLString, source: settings.connectionSource,
                                            now: Date(), id: Self.newId()) {
            Keychain.save(Self.keyAccount(first.id), settings.apiKey)
            macs = [first]
            currentId = first.id
            persist()
        }
        readSynced()
        upgradeSharedKeys()
        pruneOfflineCaches()
        // 不去重：iCloud 钥匙串传播要几十秒到几分钟，离线期间每轮探活都给一次重读机会，
        // 由 heal 的 30 秒节流限频
        store.$connState
            .sink { [weak self] state in
                Task { @MainActor in self?.connectionChanged(state) }
            }
            .store(in: &subscriptions)
    }

    private static func newId() -> String { UUID().uuidString.lowercased() }

    private func persist() {
        let defaults = UserDefaults.standard
        if let data = try? JSONEncoder().encode(macs) { defaults.set(data, forKey: Self.listKey) }
        defaults.set(currentId, forKey: Self.currentKey)
    }

    func key(for id: String) -> String { Keychain.load(Self.keyAccount(id)) }

    private func connectionChanged(_ state: AgentStore.ConnState) {
        switch state {
        case .unauthorized:
            // 设备票据被拒：被 Mac 移除或已到期，要重新经 Mac 同意（不自动发请求，由你点）
            currentRejected = current?.credential == .deviceToken
            heal()
        case .offline:
            heal()
        default:
            currentRejected = false
            upgradeSharedKeys()
        }
    }

    /// 读 iCloud 里全部 Mac 的条目并入列表。当前那台若随之更新了地址就换上——
    /// 但绝不因此换成另一台 Mac：currentId 只由用户的选择改变。
    func readSynced() {
        let (items, status) = SharedKeychain.readAll()
        syncedError = status == errSecSuccess ? nil : SharedKeychain.describe(status)
        guard status == errSecSuccess else { return }
        let pinned = Set([currentId, handshake?.macId, legacyUpgradeId, requesting?.id].compactMap { $0 })
        let merged = MacDirectory.merge(macs, synced: items, pinned: pinned, now: Date(), newId: Self.newId)
        for id in merged.dropped {
            Keychain.save(Self.ticketAccount(id), "")
            Keychain.save(Self.keyAccount(id), "")
        }
        legacyKeys = merged.legacyKeys
        for (id, ticket) in merged.tickets where Keychain.load(Self.ticketAccount(id)) != ticket {
            Keychain.save(Self.ticketAccount(id), ticket)
        }
        if merged.macs != macs {
            macs = merged.macs
            persist()
        }
        if settings?.setupCompleted == true, let current { applyIfChanged(current) }
        upgradeSharedKeys()
    }

    /// iCloud 来源的当前连接离线：Mac 可能换了开放方式（地址）——重读。30 秒节流。
    private func heal() {
        guard let settings, settings.setupCompleted, current?.source == .icloud,
              Date().timeIntervalSince(lastHeal) > 30 else { return }
        lastHeal = Date()
        readSynced()
    }

    /// 点选一台：已有凭据就切过去；只读到了 iCloud 条目（还没经 Mac 同意）就发起握手。
    func select(_ id: String, freshCredential: Bool = false) {
        guard let mac = macs.first(where: { $0.id == id }) else { return }
        // 还没经 Mac 同意，或当前这台已不再接受本机的票据（被移除 / 到期）：重新握手
        if MacDirectory.needsHandshake(credential: mac.credential, hasKey: !key(for: id).isEmpty,
                                       isCurrent: id == currentId, currentRejected: currentRejected,
                                       freshCredential: freshCredential) {
            requestOrUpgrade(id)
            return
        }
        currentId = id
        currentRejected = false
        persist()
        apply(mac)
    }

    /// 点开通知：只切到手里有可用凭据的那台，绝不发起配对（配对只能在 Mac 列表里点）。
    func switchWithoutPairing(_ id: String) {
        guard let mac = macs.first(where: { $0.id == id }),
              !MacDirectory.needsHandshake(credential: mac.credential, hasKey: !key(for: id).isEmpty,
                                           isCurrent: id == currentId, currentRejected: currentRejected,
                                           freshCredential: false) else { return }
        currentId = id
        currentRejected = false
        persist()
        apply(mac)
    }

    /// 在 Mac 列表里点了要配对的一台（MacDirectory.pairingRoute 决定走哪条路）。
    /// 共享密钥换票据在途时一律忽略：不取消、也不另发一个可能已经发出票据的请求。
    private func requestOrUpgrade(_ id: String) {
        guard legacyUpgradeId == nil, let mac = macs.first(where: { $0.id == id }) else { return }
        switch MacDirectory.pairingRoute(credential: mac.credential, hasKey: !key(for: id).isEmpty,
                                         legacyKey: legacyKeys[id]) {
        case .legacyUpgrade(let key): upgradeLegacy(id, key: key)
        case .request: requestConnection(id)
        }
    }

    /// 75 旧条目的 Mac：用条目里的共享密钥向它换本机自己的设备票据（POST /pair/upgrade），
    /// 换到后才存票据（adopt）。共享密钥本身从不存成本机凭据。
    private func upgradeLegacy(_ id: String, key: String) {
        guard legacyUpgradeId == nil, let mac = macs.first(where: { $0.id == id }),
              let base = URL(string: mac.url) else { return }
        handshakeTask?.cancel()
        legacyUpgradeId = id
        // 核对码为空的 .waiting：界面显示「正在配对」（不用在 Mac 上点允许）
        handshake = Handshake(macId: id, macName: mac.name, code: "", state: .waiting)
        let pushId = push?.deviceId
        // 不放进 handshakeTask：弹窗的「取消」或点别的 Mac 都不取消它——票据可能已经发出，
        // 拿到了就存下，不在 Mac 上留一张孤儿票据
        Task { [weak self] in
            do {
                let issued = try await PairingAPI.upgrade(base: base, key: key, pushDeviceId: pushId)
                await self?.adopt(id, token: issued.token, instanceId: issued.instance_id)
                await MainActor.run { self?.legacyUpgradeId = nil }
            } catch {
                await MainActor.run {
                    self?.legacyUpgradeId = nil
                    self?.retryPending = false
                    self?.handshake = Handshake(macId: id, macName: mac.name, code: "",
                                                state: .failed(error.localizedDescription))
                }
            }
        }
    }

    /// 同一 Apple ID：凭 iCloud 里的请求凭据向 Mac 提交连接请求，等你在 Mac 上点允许。
    func requestConnection(_ id: String) {
        guard let mac = macs.first(where: { $0.id == id }), let base = URL(string: mac.url) else { return }
        let ticket = Keychain.load(Self.ticketAccount(id))
        guard !ticket.isEmpty else {
            handshake = Handshake(macId: id, macName: mac.name, code: "",
                                  state: .failed(L("这台 Mac 还没有开启 iCloud 同步，请改用二维码。")))
            return
        }
        handshakeTask?.cancel()
        let pushId = push?.deviceId
        let attempt = UUID()
        requesting = (id, attempt)
        handshakeTask = Task { [weak self] in
            defer { Task { @MainActor in if self?.requesting?.attempt == attempt { self?.requesting = nil } } }
            do {
                let request = try await PairingAPI.request(base: base, ticket: ticket, pushDeviceId: pushId)
                await MainActor.run {
                    // 新请求已送达：此后的关闭不再是「重试」引起的
                    self?.retryPending = false
                    self?.handshake = Handshake(macId: id, macName: mac.name, code: request.code, state: .waiting)
                }
                let deadline = Date().addingTimeInterval(TimeInterval(request.expires_in))
                var errors = 0
                while Date() < deadline {
                    try await Task.sleep(nanoseconds: 2_000_000_000)
                    let poll: PairingAPI.Poll
                    do {
                        poll = try await PairingAPI.poll(base: base, request: request)
                        errors = 0
                    } catch {
                        try Task.checkCancellation()
                        errors += 1
                        if HandshakePolling.keepsWaiting(after: error, consecutive: errors) { continue }
                        throw error
                    }
                    switch poll.status {
                    case "approved":
                        guard let token = poll.token, !token.isEmpty else { throw PairingAPI.Failure.badResponse }
                        await self?.adopt(id, token: token, instanceId: poll.instance_id)
                        return
                    case "denied":
                        await MainActor.run { self?.handshake?.state = .denied }
                        return
                    default:
                        continue
                    }
                }
                await MainActor.run { self?.handshake?.state = .expired }
            } catch is CancellationError {
            } catch {
                // 取消时在途请求抛的是 URLError.cancelled：已取消的握手不再弹失败
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    self?.retryPending = false
                    if self?.handshake == nil {
                        self?.handshake = Handshake(macId: id, macName: mac.name, code: "", state: .waiting)
                    }
                    self?.handshake?.state = .failed(error.localizedDescription)
                }
            }
        }
    }

    func cancelHandshake() {
        retryPending = false
        handshakeTask?.cancel()
        handshake = nil
    }

    /// 弹窗里的「重试」：新请求接替旧的那一次。
    func retryHandshake(_ id: String) {
        retryPending = true
        requestOrUpgrade(id)
    }

    /// 弹窗关闭（SwiftUI 在任何按钮之后都会走 binding 的 setter）。「重试」之后的这次关闭
    /// 只收起旧状态，不取消刚发出的请求——它送达后会带着新核对码重新弹出。其余情况
    /// 等同取消。按钮动作与 setter 谁先谁后都成立：setter 先到时 retryPending 还没置位，
    /// 取消的是旧请求，随后的「重试」照常发起新的。
    func handshakeDismissed() {
        guard retryPending else {
            cancelHandshake()
            return
        }
        retryPending = false
        handshake = nil
    }

    /// 旧设备票据的注销（TicketHandover）：最多等 5 秒。
    private static let forgetPrevious: @Sendable (URL, String) async throws -> Void = { base, token in
        try await PairingAPI.forgetSelf(base: base, token: token, timeout: TicketHandover.timeout)
    }

    /// 握手成功：（旧票据还在就先注销它）存票据、设为当前。
    private func adopt(_ id: String, token: String, instanceId: String?) async {
        guard let mac = macs.first(where: { $0.id == id }) else { return }
        let previous = mac.credential == .deviceToken ? key(for: id) : nil
        await TicketHandover.replace(previous: previous, at: URL(string: mac.url), forget: Self.forgetPrevious,
                                     save: { [self] in
            // 等注销期间被删掉了：不留一把孤儿票据
            if macs.contains(where: { $0.id == id }) { Keychain.save(Self.keyAccount(id), token) }
        })
        guard let i = macs.firstIndex(where: { $0.id == id }) else { return }
        macs[i].credential = .deviceToken
        if macs[i].macId == nil, let claimed = MacDirectory.adoptableId(instanceId, for: id, macs: macs) {
            macs[i].macId = claimed
        }
        retryPending = false
        handshake = nil
        currentId = id
        currentRejected = false
        persist()
        apply(macs[i])
        push?.registerEverywhere()
    }

    /// 手里还是共享密钥的 Mac（75 迁移来的、手填的）：向那台 Mac 换一张本机自己的票据，
    /// 换到后删掉共享密钥。Mac 还没升级（404）或连不上时下次再试。
    /// 每次连接状态变化都会进来：失败过的 Mac 按 UpgradeBackoff 退避，不对一台回 404 / 401
    /// 的 Mac 反复 POST。
    private func upgradeSharedKeys() {
        let now = Date()
        for mac in macs where mac.credential == .sharedKey && !upgrading.contains(mac.id) {
            let key = key(for: mac.id)
            guard !key.isEmpty, let base = URL(string: mac.url) else { continue }
            let fingerprint = UpgradeBackoff.fingerprint(url: mac.url, key: key)
            guard upgradeBackoff.allows(fingerprint, now: now) else { continue }
            upgrading.insert(mac.id)
            let pushId = push?.deviceId
            Task { [weak self] in
                let issued = try? await PairingAPI.upgrade(base: base, key: key, pushDeviceId: pushId)
                await MainActor.run {
                    guard let self else { return }
                    self.upgrading.remove(mac.id)
                    guard let issued else {
                        self.upgradeBackoff.failed(fingerprint, now: Date())
                        return
                    }
                    self.upgradeBackoff.succeeded(fingerprint)
                    guard let i = self.macs.firstIndex(where: { $0.id == mac.id }),
                          self.key(for: mac.id) == key else { return }
                    Keychain.save(Self.keyAccount(mac.id), issued.token)
                    self.macs[i].credential = .deviceToken
                    if self.macs[i].macId == nil,
                       let claimed = MacDirectory.adoptableId(issued.instance_id, for: mac.id, macs: self.macs) {
                        self.macs[i].macId = claimed
                    }
                    self.persist()
                    if self.currentId == mac.id { self.apply(self.macs[i]) }
                }
            }
        }
    }

    /// 给 Mac 改个名（只改本机显示，iCloud 同步不再覆盖它）。
    func rename(_ id: String, to name: String) {
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, let i = macs.firstIndex(where: { $0.id == id }) else { return }
        macs[i].name = String(name.prefix(64))
        macs[i].customName = true
        persist()
    }

    /// 取消配对：先向它注销推送，再删本机凭据（MacDirectory.unpair）。iCloud 里还有的照常列出、
    /// 成为未配对，点按即可重新配对；扫码的那台重新扫码。
    func remove(_ id: String) {
        guard let mac = macs.first(where: { $0.id == id }),
              let plan = MacDirectory.unpair(macs, id: id) else { return }
        if let client = client(for: id) { push?.unregister(using: client) }
        // 设备票据：顺带让那台 Mac 吊销它，设备列表里不留一行用不着的连接
        if plan.forgetSelf, let base = URL(string: mac.url) {
            let token = key(for: id)
            Task { try? await PairingAPI.forgetSelf(base: base, token: token) }
        }
        for account in plan.clearedAccounts { Keychain.save(account, "") }
        macs = plan.macs
        if currentId == id {
            if let next = macs.first(where: { $0.credential != .none }) {
                currentId = next.id
                apply(next)
            } else {
                currentId = nil
                settings?.baseURLString = ""
                settings?.apiKey = ""
                settings?.setupCompleted = false
                store?.applySettings()
            }
        }
        persist()
        pruneOfflineCaches()
        // 移出列表的那台若还在 iCloud 里，立即作为未配对列回来；留下的那台取回请求凭据
        readSynced()
    }

    /// 用户在确认框里点了「连接」之后才调：拿一次性配对码换本机的票据，加进列表并设为当前。
    func claim(_ link: PairingLink) async {
        guard !claiming else { return }
        claiming = true
        defer { claiming = false }
        do {
            let issued = try await PairingAPI.claim(link, pushDeviceId: push?.deviceId)
            let macId = issued.instance_id.flatMap { MacDirectory.validMacId($0) ? $0 : nil }
            // 落到哪一项（同一台 Mac 就地更新）：那一项手里还有旧设备票据时先注销它
            let planned = MacDirectory.upsert(macs, url: link.base.absoluteString, macId: macId,
                                              source: .qr, credential: .deviceToken, now: Date(),
                                              newId: Self.newId)
            let existing = macs.first { $0.id == planned.id }
            let previous = existing?.credential == .deviceToken ? key(for: planned.id) : nil
            // 等注销期间列表可能变了：等完再在最新的列表上落位，票据只存到最终那一项名下
            var placement: (macs: [SavedMac], id: String, orphan: String?)?
            await TicketHandover.replace(previous: previous, at: existing.flatMap { URL(string: $0.url) },
                                         forget: Self.forgetPrevious, save: { [self] in
                let placed = MacDirectory.finalizeClaim(macs, plannedId: planned.id, url: link.base.absoluteString,
                                                        macId: macId, now: Date())
                Keychain.save(Self.keyAccount(placed.id), issued.token)
                if let orphan = placed.orphan {
                    Keychain.save(Self.keyAccount(orphan), "")
                    Keychain.save(Self.ticketAccount(orphan), "")
                }
                placement = placed
            })
            guard let placement else { return }
            macs = placement.macs
            // 新票据刚存：之前被拒绝（吊销 / 到期）不再算数，直接采用，不再弹握手
            select(placement.id, freshCredential: true)
            push?.registerEverywhere()
        } catch {
            claimError = error.localizedDescription
        }
    }

    func client(for id: String) -> AgentClient? {
        guard let mac = macs.first(where: { $0.id == id }), mac.credential != .none,
              let url = URL(string: mac.url), url.scheme != nil else { return nil }
        return AgentClient(baseURL: url, apiKey: key(for: id))
    }

    var allClients: [AgentClient] { macs.compactMap { client(for: $0.id) } }

    /// 推送 / 灵动岛事件该交给哪台（src = 后端 instance_id，即 Mac 的 macId）。
    func route(src: String?) -> String? { MacDirectory.route(src: src, macs: macs, current: currentId) }

    func approvalTarget(src: String?) -> String? {
        MacDirectory.approvalTarget(src: src, macs: macs, current: currentId)
    }

    private func applyIfChanged(_ mac: SavedMac) {
        guard let settings else { return }
        let key = key(for: mac.id)
        // 读不到这台的凭据时不动现有连接：宁可保持原样，也不拿空串顶掉能用的那一把
        guard !key.isEmpty, mac.url != settings.baseURLString || key != settings.apiKey
                || mac.source != settings.connectionSource else { return }
        apply(mac)
    }

    private func apply(_ mac: SavedMac) {
        guard let settings, let store else { return }
        settings.baseURLString = mac.url
        settings.apiKey = key(for: mac.id)
        settings.connectionSource = mac.source
        settings.setupCompleted = true
        store.applySettings()
        pruneOfflineCaches()
    }

    /// 离线缓存按「地址 + 凭据」分区：换票据（升级、到期重新同意、扫码）、地址变化、删掉
    /// 一台之后，旧分区再也不会被读写，也不计入当前分区的预算。清理排进 OfflineCachePruner
    /// 串行执行，保留集合在执行那一刻按最新的列表与钥匙串重算（OfflineCacheRoot.keepSet）：
    /// 任何一台的凭据读不出来、或什么都不剩时这一轮不删。
    private func pruneOfflineCaches() {
        OfflineCachePruner.shared.schedule { [weak self] in
            await MainActor.run { self?.currentKeepSet() } ?? nil
        }
    }

    private func currentKeepSet() -> Set<String>? {
        let held = macs.filter { $0.credential != .none }
        var keys: [String: String] = [:]
        for mac in held { keys[mac.id] = key(for: mac.id) }
        return OfflineCacheRoot.keepSet(macs: held.map { OfflineCacheRoot.Connection(id: $0.id, url: $0.url) },
                                        keys: keys, fallbackBaseURL: settings?.baseURL,
                                        fallbackKey: settings?.apiKey ?? "")
    }
    #endif
}
