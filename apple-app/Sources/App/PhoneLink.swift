import Combine
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
    case sharedKey     // 75 / 手填 / 旧连接：共享密钥，启动后自动换成设备票据
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

    /// 用户删掉的 Mac 记在 hidden 里：有 macId 记 macId，没有（75 旧条目）记地址。
    static func hideKey(macId: String?, url: String) -> String { macId ?? "url:" + url }

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
        /// 需要写进本机钥匙串的共享密钥（只来自 75 旧条目；SavedMac.id → key）
        var keys: [String: String]
        /// 需要写进本机钥匙串的请求凭据（SavedMac.id → ticket）
        var tickets: [String: String]
    }

    /// 把 iCloud 里读到的全部条目并进本机列表。规则：
    /// - 按 macId 认 Mac；本机旧项（75 迁移来的、没有 macId）按地址认领 macId；
    /// - 只有 iCloud 来源的项会被更新地址、名字与请求凭据，扫码与手填的值不被静默覆盖；
    ///   已经拿到设备票据的项，旧条目里的共享密钥不会把它顶掉；用户改过的名字不被覆盖；
    /// - 新 Mac 追加为 iCloud 来源（还没有凭据，要先经 Mac 同意）；用户删过的不再加回来；
    /// - iCloud 里已经没有的 iCloud 项标成 removedFromICloud，不自动删。
    /// 只在读取成功时调用（读失败不能据此把 Mac 标成已移除）。
    static func merge(_ macs: [SavedMac], synced: [PhoneConnection], hidden: Set<String>,
                      now: Date, newId: () -> String) -> Merged {
        var out = macs
        var keys: [String: String] = [:]
        var tickets: [String: String] = [:]
        var seen = Set<String>()
        // 75 全局条目与某台 Mac 自己的条目同地址时（那台 Mac 刚升级），以带 macId 的为准
        let tagged = Set(synced.filter { $0.macId != nil }.map(\.url))
        for connection in synced {
            if connection.macId == nil, tagged.contains(connection.url) { continue }
            if hidden.contains(hideKey(macId: connection.macId, url: connection.url))
                || hidden.contains(hideKey(macId: nil, url: connection.url)) { continue }
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
                                   credential: legacyKey == nil ? .none : .sharedKey, customName: false)
                out.append(mac)
                seen.insert(mac.id)
                if let legacyKey { keys[mac.id] = legacyKey }
                if let ticket = connection.ticket, !ticket.isEmpty { tickets[mac.id] = ticket }
                continue
            }
            seen.insert(out[i].id)
            guard out[i].source == .icloud else { continue }
            out[i].url = connection.url
            if !connection.mac.isEmpty, !out[i].customName { out[i].name = connection.mac }
            out[i].removedFromICloud = false
            if let ticket = connection.ticket, !ticket.isEmpty { tickets[out[i].id] = ticket }
            if let legacyKey, out[i].credential != .deviceToken {
                keys[out[i].id] = legacyKey
                out[i].credential = .sharedKey
            }
        }
        for i in out.indices where out[i].source == .icloud && !seen.contains(out[i].id) {
            out[i].removedFromICloud = true
        }
        return Merged(macs: out, keys: keys, tickets: tickets)
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

    /// 推送 / 灵动岛事件来自哪台：src 认得出就是那台，认不出（旧后端、手填的 Mac）退回当前那台。
    static func route(src: String?, macs: [SavedMac], current: String?) -> String? {
        if let src, let mac = macs.first(where: { $0.macId == src }) { return mac.id }
        return current
    }

    /// 审批（允许 / 拒绝）回给哪台：比 route 严。带了 src 却认不出时，只有当前那台本身
    /// 没有 macId（手填、75 迁移来的，可能正是它）才回给它；当前那台有别的 macId 就拒发——
    /// 审批绝不改投到另一台 Mac。
    static func approvalTarget(src: String?, macs: [SavedMac], current: String?) -> String? {
        guard let src else { return current }
        if let mac = macs.first(where: { $0.macId == src }) { return mac.id }
        guard let current, let mac = macs.first(where: { $0.id == current }), mac.macId == nil else { return nil }
        return current
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
        case badResponse

        var errorDescription: String? {
            switch self {
            case .refused(let detail): return detail
            case .http(let code): return "HTTP \(code)"
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

    static func forgetSelf(base: URL, token: String) async throws {
        let _: Ignored = try await call(base, "DELETE", "/api/v1/pair/self", body: nil, key: token)
    }

    private static func issued(_ value: Issued) throws -> Issued {
        guard !value.token.isEmpty else { throw Failure.badResponse }
        return value
    }

    private static func call<T: Decodable>(_ base: URL, _ method: String, _ path: String,
                                           body: [String: Any]?, key: String) async throws -> T {
        guard let url = URL(string: path, relativeTo: base) else { throw Failure.badResponse }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10)
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
            throw detail.map(Failure.refused) ?? Failure.http(code)
        }
        guard let decoded = try? JSONDecoder().decode(T.self, from: data) else { throw Failure.badResponse }
        return decoded
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

    static let target = AppSettings.localBackend
    @Published private(set) var exposure: Exposure = .checking
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

    func configure(settings: AppSettings) {
        self.settings = settings
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
                await self.pollRequests()
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
              let pending = try? await PairingAPI.pending(base: base, key: key) else { return }
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
        guard let (base, key) = local, let listing = try? await PairingAPI.devices(base: base, key: key)
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
        guard let (base, key) = local, let fresh = try? await PairingAPI.rotateTicket(base: base, key: key)
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
        let status = await TailscaleCLI.run(["status", "--json"])
        let me = status.flatMap { $0.code == 0 ? TailnetParse.selfNode($0.out) : nil }
        let serve = me?.running == true ? await TailscaleCLI.run(["serve", "status", "--json"]) : nil
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
        }
        await sync()
        await refreshDevices()
    }

    /// 由用户在设置页亲手点：serve 配置是 tailscaled 的持久状态，重启后仍在。
    func expose() async {
        guard case .notServed(let https, let taken) = exposure, !taken else { return }
        let args = ["serve", "--bg", https ? "--https=443" : "--http=14159", Self.target]
        await runAction(args, failure: L("开放失败："))
    }

    func unexpose() async {
        guard case .served(let url) = exposure else { return }
        let https = url.scheme == "https"
        let port = url.port ?? (https ? 443 : 80)
        _ = SharedKeychain.delete(macId: MacIdentity.id)
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
        let result = await TailscaleCLI.run(args)
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
            let status = SharedKeychain.delete(macId: MacIdentity.id)
            if case .served(let url) = exposure { removeOwnLegacy(url: url.absoluteString) }
            syncOK = false
            syncError = status == errSecSuccess ? nil : SharedKeychain.describe(status)
            Task { await voidTicket() }
        }
    }

    /// 写本机的 iCloud 条目：地址 + 请求凭据（向本机后端领，经回环）。共享密钥不进 iCloud。
    private func sync() async {
        guard let settings, settings.phoneSyncEnabled else { return }
        guard case .served(let url) = exposure, let (base, key) = local else {
            syncOK = false
            return
        }
        if ticket.isEmpty {
            guard let fresh = try? await PairingAPI.ticket(base: base, key: key) else {
                syncOK = false
                syncError = L("执行层尚未就绪，就绪后自动同步。")
                return
            }
            ticket = fresh
        }
        let connection = MacDirectory.outgoing(url: url.absoluteString, ticket: ticket, mac: macName,
                                               macId: MacIdentity.id, now: Date())
        if let lastWritten, lastWritten.sameContent(connection) { return }
        let status = SharedKeychain.write(connection)
        syncOK = status == errSecSuccess
        syncError = syncOK ? nil : SharedKeychain.describe(status)
        lastWritten = syncOK ? connection : nil
        guard syncOK else { return }
        let (items, readStatus) = SharedKeychain.readAll()
        guard readStatus == errSecSuccess else { return }
        // 本机丢过 macId（重装等）留下的旧身份条目：同地址、别的 macId，删掉
        for stale in MacDirectory.staleOwnItems(items, macId: MacIdentity.id, url: connection.url) {
            _ = SharedKeychain.delete(macId: stale)
        }
        // 本机升级前（75）写的全局条目里有共享密钥：是本机的就删掉，另一台 Mac 的不碰
        removeOwnLegacy(url: connection.url, items: items)
    }

    private func removeOwnLegacy(url: String, items: [PhoneConnection]? = nil) {
        let list: [PhoneConnection]
        if let items {
            list = items
        } else {
            let (read, status) = SharedKeychain.readAll()
            guard status == errSecSuccess else { return }
            list = read
        }
        guard list.contains(where: { MacDirectory.ownsLegacy($0, name: macName, url: url) }) else { return }
        _ = SharedKeychain.deleteLegacy()
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
    private var hidden: Set<String> = []
    private var lastHeal = Date.distantPast
    private var handshakeTask: Task<Void, Never>?
    private var upgrading = Set<String>()

    private static let listKey = "phone.macs"
    private static let currentKey = "phone.currentMac"
    private static let hiddenKey = "phone.hiddenMacs"
    /// 每台 Mac 的票据（或旧连接的共享密钥）：本机钥匙串、不同步（与 DenyApprovalIntent 一致）
    static func keyAccount(_ id: String) -> String { "mac-key." + id }
    /// 每台 Mac 的请求凭据（来自 iCloud 条目）
    static func ticketAccount(_ id: String) -> String { "mac-ticket." + id }

    var current: SavedMac? { macs.first { $0.id == currentId } }

    func configure(settings: AppSettings, store: AgentStore) {
        self.settings = settings
        self.store = store
        let defaults = UserDefaults.standard
        macs = (defaults.data(forKey: Self.listKey))
            .flatMap { try? JSONDecoder().decode([SavedMac].self, from: $0) } ?? []
        currentId = defaults.string(forKey: Self.currentKey)
        hidden = Set(defaults.stringArray(forKey: Self.hiddenKey) ?? [])
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
        defaults.set(Array(hidden).sorted(), forKey: Self.hiddenKey)
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
        let merged = MacDirectory.merge(macs, synced: items, hidden: hidden, now: Date(), newId: Self.newId)
        for (id, key) in merged.keys where self.key(for: id) != key {
            Keychain.save(Self.keyAccount(id), key)
        }
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
    func select(_ id: String) {
        guard let mac = macs.first(where: { $0.id == id }) else { return }
        // 还没经 Mac 同意，或当前这台已不再接受本机的票据（被移除 / 到期）：重新握手
        if mac.credential == .none || key(for: id).isEmpty || (id == currentId && currentRejected) {
            requestConnection(id)
            return
        }
        currentId = id
        currentRejected = false
        persist()
        apply(mac)
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
        handshakeTask = Task { [weak self] in
            do {
                let request = try await PairingAPI.request(base: base, ticket: ticket, pushDeviceId: pushId)
                await MainActor.run {
                    self?.handshake = Handshake(macId: id, macName: mac.name, code: request.code, state: .waiting)
                }
                let deadline = Date().addingTimeInterval(TimeInterval(request.expires_in))
                while Date() < deadline {
                    try await Task.sleep(nanoseconds: 2_000_000_000)
                    let poll = try await PairingAPI.poll(base: base, request: request)
                    switch poll.status {
                    case "approved":
                        guard let token = poll.token, !token.isEmpty else { throw PairingAPI.Failure.badResponse }
                        await MainActor.run { self?.adopt(id, token: token, instanceId: poll.instance_id) }
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
                await MainActor.run {
                    if self?.handshake == nil {
                        self?.handshake = Handshake(macId: id, macName: mac.name, code: "", state: .waiting)
                    }
                    self?.handshake?.state = .failed(error.localizedDescription)
                }
            }
        }
    }

    func cancelHandshake() {
        handshakeTask?.cancel()
        handshake = nil
    }

    /// 握手成功：存票据、设为当前。
    private func adopt(_ id: String, token: String, instanceId: String?) {
        guard let i = macs.firstIndex(where: { $0.id == id }) else { return }
        Keychain.save(Self.keyAccount(id), token)
        macs[i].credential = .deviceToken
        if macs[i].macId == nil, let claimed = MacDirectory.adoptableId(instanceId, for: id, macs: macs) {
            macs[i].macId = claimed
        }
        handshake = nil
        currentId = id
        currentRejected = false
        persist()
        apply(macs[i])
        push?.registerEverywhere()
    }

    /// 手里还是共享密钥的 Mac（75 迁移来的、手填的）：向那台 Mac 换一张本机自己的票据，
    /// 换到后删掉共享密钥。Mac 还没升级（404）或连不上时下次再试。
    private func upgradeSharedKeys() {
        for mac in macs where mac.credential == .sharedKey && !upgrading.contains(mac.id) {
            let key = key(for: mac.id)
            guard !key.isEmpty, let base = URL(string: mac.url) else { continue }
            upgrading.insert(mac.id)
            let pushId = push?.deviceId
            Task { [weak self] in
                let issued = try? await PairingAPI.upgrade(base: base, key: key, pushDeviceId: pushId)
                await MainActor.run {
                    guard let self else { return }
                    self.upgrading.remove(mac.id)
                    guard let issued, let i = self.macs.firstIndex(where: { $0.id == mac.id }),
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

    /// 删掉一台：先向它注销推送，再删凭据。iCloud 里还有的不会再被自动加回来（扫码可以）。
    func remove(_ id: String) {
        guard let mac = macs.first(where: { $0.id == id }) else { return }
        if let client = client(for: id) { push?.unregister(using: client) }
        // 设备票据：顺带让那台 Mac 吊销它，设备列表里不留一行用不着的连接
        if mac.credential == .deviceToken, let base = URL(string: mac.url) {
            let token = key(for: id)
            Task { try? await PairingAPI.forgetSelf(base: base, token: token) }
        }
        Keychain.save(Self.keyAccount(id), "")
        Keychain.save(Self.ticketAccount(id), "")
        hidden.insert(MacDirectory.hideKey(macId: nil, url: mac.url))
        if mac.macId != nil { hidden.insert(MacDirectory.hideKey(macId: mac.macId, url: mac.url)) }
        macs.removeAll { $0.id == id }
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
    }

    /// 设置页 / 引导页手填后保存（地址 + 共享密钥）：同地址的就地更新为手动来源，否则新增
    /// 一台；设为当前。之后自动换成本机自己的票据。
    func saveManual(url: String, key: String) {
        var url = url
        while url.hasSuffix("/") { url.removeLast() }   // 同一台 Mac 不因结尾斜杠分成两行
        guard !url.isEmpty else { return }
        let result = MacDirectory.upsert(macs, url: url, macId: nil, source: .manual,
                                         credential: .sharedKey, now: Date(), newId: Self.newId)
        hidden.remove(MacDirectory.hideKey(macId: nil, url: url))
        macs = result.macs
        Keychain.save(Self.keyAccount(result.id), key)
        select(result.id)
        upgradeSharedKeys()
    }

    /// 设置页清空了地址：不再有当前 Mac（否则下一次 readSynced 又把它换回来）。
    func disconnect() {
        currentId = nil
        persist()
    }

    /// 用户在确认框里点了「连接」之后才调：拿一次性配对码换本机的票据，加进列表并设为当前。
    func claim(_ link: PairingLink) async {
        guard !claiming else { return }
        claiming = true
        defer { claiming = false }
        do {
            let issued = try await PairingAPI.claim(link, pushDeviceId: push?.deviceId)
            let macId = issued.instance_id.flatMap { MacDirectory.validMacId($0) ? $0 : nil }
            let result = MacDirectory.upsert(macs, url: link.base.absoluteString, macId: macId,
                                             source: .qr, credential: .deviceToken, now: Date(),
                                             newId: Self.newId)
            hidden.remove(MacDirectory.hideKey(macId: macId, url: link.base.absoluteString))
            hidden.remove(MacDirectory.hideKey(macId: nil, url: link.base.absoluteString))
            macs = result.macs
            Keychain.save(Self.keyAccount(result.id), issued.token)
            select(result.id)
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
    }
    #endif
}
