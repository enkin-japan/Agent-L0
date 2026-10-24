import Combine
import Foundation
import Security
import SwiftUI
#if os(macOS)
import AppKit
import CoreImage.CIFilterBuiltins
#endif

/// iPhone 连上执行端（Mac 上的后端）所需的全部信息：Mac 经 tailnet 开放的地址 + AGENT 密钥。
/// 主路径是 iCloud 钥匙串同步：Mac 写、iPhone 读，同一 Apple ID 下装好 app 即可连上；
/// Mac 轮换密钥后 iPhone 遇 401 自动重读。备用出口是扫码配对：二维码只载地址 + 一次性
/// 配对码（backend/framework/api/pairing.py），长期密钥从不上屏。
struct PhoneConnection: Codable, Equatable {
    var url: String
    var key: String
    var mac: String
}

/// 可同步的共享钥匙串条目。两端 app 的 bundle id 不同，靠同 Team 前缀的共享访问组互通。
/// macOS 必须走 data protection 钥匙串（iCloud 同步只发生在那里），而那要求签名带
/// keychain-access-groups——只有 bundle-macos.sh 嵌了描述文件的发布构建才有；开发构建
/// 读写得到 errSecMissingEntitlement，界面据此提示改用二维码。
enum SharedKeychain {
    static let accessGroup = "9A84P4ZS84.dev.enkin.macagent.shared"
    private static let service = "dev.enkin.macagent.phone-link"
    private static let account = "connection"

    private static var query: [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: accessGroup,
            kSecAttrSynchronizable as String: true,
        ]
        #if os(macOS)
        q[kSecUseDataProtectionKeychain as String] = true
        #endif
        return q
    }

    /// 没有条目不算错误：返回 (nil, errSecSuccess)。
    static func read() -> (PhoneConnection?, OSStatus) {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return (nil, status == errSecItemNotFound ? errSecSuccess : status)
        }
        guard let connection = try? JSONDecoder().decode(PhoneConnection.self, from: data) else {
            return (nil, errSecDecode)
        }
        return (connection, errSecSuccess)
    }

    static func write(_ connection: PhoneConnection) -> OSStatus {
        guard let data = try? JSONEncoder().encode(connection) else { return errSecParam }
        let status = SecItemUpdate(query as CFDictionary,
                                   [kSecValueData as String: data] as CFDictionary)
        guard status == errSecItemNotFound else { return status }
        var attrs = query
        attrs[kSecValueData as String] = data
        // 可同步条目不能用 ThisDeviceOnly 档
        attrs[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        attrs[kSecAttrLabel as String] = "MacAgent iPhone connection"
        return SecItemAdd(attrs as CFDictionary, nil)
    }

    static func delete() -> OSStatus {
        let status = SecItemDelete(query as CFDictionary)
        return status == errSecItemNotFound ? errSecSuccess : status
    }

    static func describe(_ status: OSStatus) -> String {
        if status == errSecMissingEntitlement {
            return L("此构建未带 iCloud 钥匙串权限（开发构建），请改用二维码配对。")
        }
        return L("钥匙串错误：") + "\(status)"
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

/// 配对两步的 HTTP 面。独立于 AgentClient：claim 发生在还没有任何密钥的时候。
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

    private struct Claimed: Decodable { let api_key: String }
    private static let session = URLSession(configuration: .ephemeral)

    static func start(base: URL, key: String) async throws -> Started {
        try await post(base, "/api/v1/pair/start", body: [:], key: key)
    }

    static func claim(_ link: PairingLink) async throws -> String {
        let claimed: Claimed = try await post(link.base, "/api/v1/pair/claim",
                                              body: ["code": link.code], key: "")
        guard !claimed.api_key.isEmpty else { throw Failure.badResponse }
        return claimed.api_key
    }

    private static func post<T: Decodable>(_ base: URL, _ path: String, body: [String: String],
                                           key: String) async throws -> T {
        guard let url = URL(string: path, relativeTo: base) else { throw Failure.badResponse }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !key.isEmpty { req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization") }
        req.httpBody = try JSONEncoder().encode(body)
        let (data, response) = try await session.data(for: req)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            let detail = (try? JSONDecoder().decode([String: String].self, from: data))?["detail"]
            throw detail.map(Failure.refused) ?? Failure.http(code)
        }
        guard let decoded = try? JSONDecoder().decode(T.self, from: data) else { throw Failure.badResponse }
        return decoded
    }
}

#if os(macOS)
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
/// iPhone 侧管「读同步条目 + 401 自愈 + 扫码换密钥」。
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
    private var macName = ""
    private var lastWritten: PhoneConnection?
    /// refresh 会从启动、设置页出现、按钮、开关动作四处并发进来：只认最后一次发起的结果，
    /// 免得慢的旧结果把新状态盖回去。
    private var refreshGeneration = 0

    func configure(settings: AppSettings) {
        self.settings = settings
        // 密钥轮换（设置页重新生成 / 执行层首启生成）即刻同步；sink 在 willSet 时触发，
        // 用传入的新值而不是回读 settings.apiKey
        settings.$apiKey.dropFirst().removeDuplicates()
            .sink { [weak self] key in self?.sync(key: key) }
            .store(in: &subscriptions)
        Task { await refresh() }
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
        sync(key: settings?.apiKey ?? "")
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
        _ = SharedKeychain.delete()
        lastWritten = nil
        syncOK = false
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
            sync(key: settings?.apiKey ?? "")
        } else {
            let status = SharedKeychain.delete()
            syncOK = false
            syncError = status == errSecSuccess ? nil : SharedKeychain.describe(status)
        }
    }

    private func sync(key: String) {
        guard let settings, settings.phoneSyncEnabled else { return }
        guard case .served(let url) = exposure, !key.isEmpty else {
            syncOK = false
            return
        }
        let connection = PhoneConnection(url: url.absoluteString, key: key, mac: macName)
        guard connection != lastWritten else { return }
        let status = SharedKeychain.write(connection)
        syncOK = status == errSecSuccess
        syncError = syncOK ? nil : SharedKeychain.describe(status)
        lastWritten = syncOK ? connection : nil
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
    private weak var store: AgentStore?
    @Published private(set) var synced: PhoneConnection?
    @Published private(set) var syncedError: String?
    @Published var pendingLink: PairingLink?
    @Published private(set) var claiming = false
    @Published var claimError: String?
    private var lastHeal = Date.distantPast

    func configure(settings: AppSettings, store: AgentStore) {
        self.settings = settings
        self.store = store
        readSynced()
        // 不去重：iCloud 钥匙串传播要几十秒到几分钟，401 期间每轮探活都给一次重读机会，
        // 由 heal 的 30 秒节流限频（去重会让自愈只跑一次、多半还读到旧值）
        store.$connState
            .sink { [weak self] state in
                guard state == .unauthorized || state == .offline else { return }
                Task { @MainActor in self?.heal() }
            }
            .store(in: &subscriptions)
    }

    func readSynced() {
        let (connection, status) = SharedKeychain.read()
        synced = connection
        syncedError = status == errSecSuccess ? nil : SharedKeychain.describe(status)
    }

    func useSynced() {
        guard let synced else { return }
        apply(url: synced.url, key: synced.key, source: .icloud)
    }

    /// iCloud 来源的连接遇 401/离线：Mac 可能刚轮换了密钥或换了开放方式——重读同步条目，
    /// 与当前不同就换上。手动/扫码来源不碰（用户自己给的值不被静默覆盖）。30 秒节流。
    private func heal() {
        guard let settings, settings.setupCompleted, settings.connectionSource == .icloud,
              Date().timeIntervalSince(lastHeal) > 30 else { return }
        lastHeal = Date()
        readSynced()
        guard let synced, synced.url != settings.baseURLString || synced.key != settings.apiKey
        else { return }
        apply(url: synced.url, key: synced.key, source: .icloud)
    }

    /// 用户在确认框里点了「连接」之后才调：拿一次性配对码换密钥。
    func claim(_ link: PairingLink) async {
        guard !claiming else { return }
        claiming = true
        defer { claiming = false }
        do {
            let key = try await PairingAPI.claim(link)
            apply(url: link.base.absoluteString, key: key, source: .qr)
        } catch {
            claimError = error.localizedDescription
        }
    }

    private func apply(url: String, key: String, source: ConnectionSource) {
        guard let settings, let store else { return }
        settings.baseURLString = url
        settings.apiKey = key
        settings.connectionSource = source
        settings.setupCompleted = true
        store.applySettings()
    }
    #endif
}
