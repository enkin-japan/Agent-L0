import CryptoKit
import Foundation
import Security
import SwiftUI

#if os(macOS)

/// Sidecar 进程监护（R5-4 / D.13）。
///
/// **职责边界（D.13，越界即架构退化）**：「启什么 / 什么顺序 / 怎么算就绪」的唯一
/// 真相在 `scripts/lib_edge.sh`——本类**只**做 app 独有的进程监护：
///   - 经 `lib_edge.sh up|down` 拉起与收摊（编队、searx 先于 backend、容错都在 lib 里）；
///   - 崩溃重启（kqueue 盯 pid，带退避，防 crash-loop 空转）；
///   - 端口冲突呈现（lib 已判定并报告占用者，这里只把它的输出交给用户）;
///   - 随 app 收摊（quit → down；**外部启动的服务不收**——那是用户 devctl 起的）。
/// 在 Swift 里重写启动顺序 = 把 §S.3 那次事故的教训复制成第二份注释。
///
/// **布局**（差异只在 7 个 EDGE_* 环境变量，见 lib_edge.sh 头部）：
///   - bundle：Resources/ 里带 backend + python(PBS, R5-3) + searxng（R5-5 起齐备）；
///     可写物全部落 `AGENT_DATA_DIR`（D.16）= ~/Library/Application Support/dev.enkin.macagent
///   - repo：开发形态，指向仓库检出（设置里填路径），与 devctl 完全同一套 lib/日志/pid。
@MainActor
final class SidecarManager: ObservableObject {

    enum State: Equatable {
        case disabled            // 设置未开启
        case external            // backend 已由别人拉起（devctl）——只用，不管，不收
        case starting
        case running
        case failed(String)      // lib 退出非 0（端口冲突/依赖缺失/超时），附日志尾
    }

    /// 探活四态（#85 完整版 1）：「14159 上有 200」不足以认领一个后端——端口可能被别的
    /// 程序占着（.foreign），也可能是钥匙已漂移的旧/dev 后端（.keyMismatch，收养即构造性
    /// 401，2026-08-10 全天事故的实症）。只有身份与钥匙都对得上才收养为 external。
    enum HealthProbe: Equatable {
        case down          // 不可达 / 非 200：14159 上没人
        case foreign       // 有 HTTP 服务但不自称 macagent-backend：端口被外来程序占用
        case keyMismatch   // 是本系统后端，但它认的钥匙不是 app 手里这把（指纹不等）
        case healthy       // 身份 + 钥匙都对得上
    }

    @Published private(set) var state: State = .disabled
    @Published private(set) var restartCount = 0
    /// 本 app 解析出的布局形态（"bundle" / "repo"，nil = 尚未解析或解析失败）。
    /// 事故根因之一是 bundle 判据失败**静默退 repo 布局**（密钥换源）——形态必须可见。
    @Published private(set) var layoutKind: String?
    /// 裸包：包内有 Resources 但没有执行层（install-macos.sh 的纯客户端快装覆盖了全栈包）。
    @Published private(set) var isBareBundle = false
    /// 14159 上那个后端自报的布局形态（探活得来；nil = 探不到或对方没报）。
    @Published private(set) var peerLayout: String?
    /// Configuration only; process and authenticated connection states remain separate.
    @Published private(set) var configuration: EdgeConfiguration?

    private struct Layout {
        let libEdge: URL          // scripts/lib_edge.sh
        let env: [String: String] // EDGE_* + AGENT_DATA_DIR (+ dev 桥接的密钥)
        let logDir: URL
        // 孤儿回收只在 bundle 布局做：app 专属数据根里的 pid 文件必然是上代 app 写的；
        // repo 布局与 devctl 共用 logs/，pid 无法区分归属，reap 会误杀用户的开发后端
        let isBundle: Bool
    }

    private var layout: Layout?
    private var watchers: [DispatchSourceProcess] = []
    private var restartWindowStart = Date.distantPast
    private weak var settings: AppSettings?

    /// 崩溃重启退避：窗口内超过上限就停手亮红——无限重启只会把同一个错误刷成日志海。
    private static let maxRestarts = 3
    private static let restartWindow: TimeInterval = 300

    func configure(settings: AppSettings) { self.settings = settings }

    // MARK: - 生命周期

    /// 生命周期随 **app 进程**，与窗口无关（R5-4 修）：启动时触发一次即可，窗口重开
    /// 不得重入——重入会让 reap 把本代健在的服务当孤儿杀掉（探活失败→重启），
    /// kqueue 又看到孩子死亡补一次崩溃重启，凭空两次重启。
    /// 幂等守卫：已在管（running/starting）或外部在跑（external）直接返回，reap 与探活
    /// 只在真正冷启动（disabled）/失败重试（failed）路径跑。
    /// `force`：restart() 在 down() 之后要强穿守卫重拉（down 不改 state，守卫会误挡）。
    func autostart(force: Bool = false) async {
        guard settings?.sidecarEnabled == true else { state = .disabled; return }
        if !force {
            switch state {
            case .running, .starting, .external: return
            case .disabled, .failed: break
            }
        }
        configuration = nil
        guard let layout = resolveLayout() else {
            state = .failed("找不到执行层布局：bundle 无 Resources/backend，设置里也未填仓库路径")
            return
        }
        self.layout = layout
        configuration = (try? await EdgeConfiguration.read(lib: layout.libEdge, environment: layout.env))
            ?? EdgeConfiguration(status: "unknown", missing: [])
        // 孤儿回收（bundle 布局限定）：app 被 SIGKILL 后守护进程活了下来，若不回收，
        // 下面的探活会见「健康」而当 external 永远只用不管——孤儿从此跑着已删除的
        // 旧 bundle 代码（R5-9 实证）。判据在 lib 的 reap：数据根 pid 文件仍存活 =
        // 上代 app 所拉。repo 布局绝不 reap（与 devctl 共用 pid，无法区分归属）。
        if layout.isBundle, let run = try? runLib(layout, command: "reap") {
            _ = await run.waitAsync()
        }
        // 先探活：已有人在跑（devctl 起的 dev 后端）就只用不管。收摊时也不碰它——
        // 「app 退出顺手杀掉用户的开发后端」是最恶劣的一种越权。
        // 但**认身份、认钥匙，不认端口**（#85）：钥匙对不上的后端收养了也只会全端点 401，
        // 且退出重开/重新生成/开关关开每一条路都会原样复现（事故实证）——不如当场亮红说清。
        switch await probeHealth() {
        case .healthy:
            state = .external
            return
        case .keyMismatch:
            state = .failed("14159 上有本系统后端在跑，但其密钥与 app 不一致（指纹不匹配）"
                          + "——若是 devctl 起的 dev 后端，请同步 .env.local 与 app 设置的密钥后重试")
            return
        case .foreign:
            state = .failed("端口 14159 被其他程序占用（非本系统后端；也可能是未自报身份的旧版后端，"
                          + "重启它即可自证）")
            return
        case .down:
            break
        }
        await up()
    }

    /// 用户在设置里翻开关后手动触发。
    func startStop() async {
        switch state {
        case .running, .starting:
            if await down() { state = .disabled }
        default:
            await autostart()
        }
    }

    /// 设置里改了服务密钥（INFER/RAG_API_KEY）后手动触发：密钥是 spawn 时注入 env 的
    /// （R5-6），正在跑的编队拿不到新值——收掉自己拉起的再按新布局拉起（autostart
    /// 重新走 resolveLayout，从 Keychain 取最新值）。external（devctl 的后端）不碰。
    func restart() async {
        switch state {
        case .running, .starting:
            guard await down() else { return }
            // down() 不改 state（仍是 running/starting），非 force 会被新守卫误挡在门外，
            // 导致「重启」变成空动作——force 强穿守卫，reap 后探活见 down 已真杀→重拉。
            await autostart(force: true)
        case .failed:
            await autostart()   // failed 本就穿守卫，无需 force
        case .disabled, .external:
            break
        }
    }

    /// app 退出路径（applicationShouldTerminate 里同步等待）。
    /// 只收自己拉起的：external 状态下一个进程都不碰。
    func shutdownSync() {
        guard let layout, state == .running || state == .starting else { return }
        cancelWatchers()
        _ = try? runLib(layout, command: "down").wait()
    }

    // MARK: - up / down / 监护

    private func up() async {
        guard let layout else { return }
        state = .starting
        do {
            let run = try runLib(layout, command: "up")
            let code = await run.waitAsync()
            if code == 0 {
                state = .running
                watchPids()
            } else {
                // lib 已把原因说清（端口占用者清单 / RAG 未就绪 / 超时 + 日志尾），
                // 这里不二次诊断，把它的话原样带给用户。
                state = .failed(run.outputTail(lines: 12))
            }
        } catch {
            state = .failed("无法执行 lib_edge.sh: \(error.localizedDescription)")
        }
    }

    private func down() async -> Bool {
        guard let layout else {
            state = .failed("找不到执行层布局，无法确认停止")
            return false
        }
        cancelWatchers()
        do {
            let run = try runLib(layout, command: "down")
            guard await run.waitAsync() == 0 else {
                state = .failed(run.outputTail(lines: 12))
                return false
            }
            return true
        } catch {
            state = .failed("无法停止执行层: \(error.localizedDescription)")
            return false
        }
    }

    /// kqueue 盯 pid 文件里的进程（lib 拉起的是脱管守护进程，不是本进程的子进程——
    /// DispatchSourceProcess 底层是 EVFILT_PROC，同用户即可盯非子进程）。
    private func watchPids() {
        cancelWatchers()
        guard let layout else { return }
        for name in ["backend", "searx", "rag"] {
            let pidFile = layout.logDir.appendingPathComponent("\(name).pid")
            guard let text = try? String(contentsOf: pidFile, encoding: .utf8),
                  let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)),
                  kill(pid, 0) == 0 else { continue }   // searx 可选、rag 仅本机数据面时有：没起就不盯
            let src = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit,
                                                       queue: .main)
            src.setEventHandler { [weak self] in
                Task { @MainActor in await self?.handleCrash(of: name) }
            }
            src.activate()
            watchers.append(src)
        }
    }

    private func cancelWatchers() {
        watchers.forEach { $0.cancel() }
        watchers.removeAll()
    }

    private func handleCrash(of name: String) async {
        guard state == .running else { return }   // 主动 down 的收尾事件不算崩溃
        let now = Date()
        if now.timeIntervalSince(restartWindowStart) > Self.restartWindow {
            restartWindowStart = now
            restartCount = 0
        }
        restartCount += 1
        guard restartCount <= Self.maxRestarts else {
            cancelWatchers()
            state = .failed("\(name) 在 \(Int(Self.restartWindow))s 内崩溃超过 \(Self.maxRestarts) 次，"
                          + "已停止自动重启。日志：\(layout?.logDir.path ?? "")/\(name).log")
            return
        }
        // 重启走同一个 up：lib 幂等（活着的「已在运行，跳过」，死了的按正确顺序补起）。
        // 单独重启 searx 或 backend 的排列组合一概不做——那就是在 Swift 里重写编队。
        await up()
    }

    // MARK: - 布局解析

    private func resolveLayout() -> Layout? {
        let fm = FileManager.default
        // 裸包判定（#85 完整版 2）：包内有 Resources 却没有 backend/app.py = 纯客户端快装
        // 覆盖了全栈包。此前这里**静默退 repo 布局**（密钥从钥匙串换成 .env.local）=
        // 构造性 401 且每条重试路径都原样复现（2026-08-10 事故根因）。形态必须亮给用户。
        let hasResources = Bundle.main.resourceURL != nil
        // bundle 布局：Resources/ 下带完整执行层（R5-5 之后成立）
        if let res = Bundle.main.resourceURL,
           fm.fileExists(atPath: res.appendingPathComponent("backend/app.py").path) {
            isBareBundle = false
            layoutKind = "bundle"
            let dataDir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("dev.enkin.macagent")
            let logDir = dataDir.appendingPathComponent("logs")
            try? fm.createDirectory(at: logDir, withIntermediateDirectories: true)
            var env = baseEnv()
            env["EDGE_BACKEND_DIR"] = res.appendingPathComponent("backend").path
            env["EDGE_BACKEND_PY"] = res.appendingPathComponent("python/bin/python3.13").path
            // 一棵树两个服务（R5-5/D.8）：searx 非 editable 装进同一个 PBS 运行时
            // （实测共享依赖仅 +40M；bundle 里没有 searxng/.venv 这回事）
            env["EDGE_SEARX_DIR"] = dataDir.path   // 仅作 cwd；searx 以 -m 模块方式运行
            env["EDGE_SEARX_PY"] = res.appendingPathComponent("python/bin/python3.13").path
            // bundle 只读：首启生成的 settings.yml 必须落数据根（D.16 同一根）
            env["EDGE_SEARX_SETTINGS"] = dataDir.appendingPathComponent("searxng-settings.yml").path
            env["EDGE_LOG_DIR"] = logDir.path
            env["EDGE_CONFIG"] = res.appendingPathComponent("config.yaml").path
            env["AGENT_DATA_DIR"] = dataDir.path
            // 布局自报（#85）：backend 的 /health 原样回显，让探针（本类下次探活、devctl、
            // 用户 curl）分得清「app 全栈包起的」与「devctl 起的 dev 后端」。
            env["AGENT_LAYOUT"] = "bundle"
            // bundle 只读：字节码已在组装期 compileall 进印章，运行时一个 .pyc 都
            // 不许写回包内（写了就破坏 codesign 印章——R5-9 实证）。repo 布局不设。
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            // 密钥形态（R5-6）：bundle 里没有 .env.local，Keychain 是密钥的家。
            // AGENT_API_KEY 首启生成随机值——虽是同进程回环（可退化为空），但生成
            // 一把成本为零，还把「回环上其他本机进程白嫖 backend」也关掉了；
            // 存进 settings.apiKey（Keychain 落盘）使 app 自己的客户端同时拿到它。
            if let settings, settings.apiKey.isEmpty {
                settings.apiKey = Self.randomKey()
            }
            // 本机数据面独立持钥；旧安装保留现值，绝不随计算面设置轮换。
            if let settings, settings.ragApiKey.isEmpty {
                settings.ragApiKey = Self.randomKey()
            }
            let credentials = ServiceCredentials(
                agent: settings?.apiKey ?? "", inference: settings?.inferApiKey ?? "",
                ragData: settings?.ragApiKey ?? "", ragCompute: settings?.ragComputeApiKey ?? "")
            // 明确清掉继承值，空设置不得意外使用启动 app 的 shell 凭据。
            env = credentials.applying(to: env)
            // B2c 拆分：RAG 数据面随包（config 的 rag.host 为回环时 lib_edge 编队自动拉起，
            // 先于 backend；指远端 = 过渡拓扑，不拉）。数据落数据根，代码目录保持只读。
            env["EDGE_RAG_DIR"] = res.appendingPathComponent("rag").path
            env["EDGE_RAG_PY"] = res.appendingPathComponent("python/bin/python3.13").path
            env["EDGE_RAG_DATA_DIR"] = dataDir.appendingPathComponent("rag").path
            // 算力端地址一键两职：inference-server 与 RAG 计算面在同一台算力机，一个
            // 设置项覆盖两个 host 变量（后端已支持这两个 env 覆盖 config）——用户无需
            // 理解两个变量名。空 = 不注入，回落包内 config.yaml 的值（换算力机零重打包）。
            let host = (settings?.computeHost ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !host.isEmpty {
                env["INFER_REMOTE_HOST"] = host
                env["RAG_COMPUTE_HOST"] = host
            }
            injectKVPersist(&env)
            return Layout(libEdge: res.appendingPathComponent("scripts/lib_edge.sh"),
                          env: env, logDir: logDir, isBundle: true)
        }
        // repo 布局：设置里填的仓库检出（开发形态，与 devctl 共用 lib/日志/pid）
        isBareBundle = hasResources
        guard let root = settings?.sidecarRepoRoot, !root.isEmpty else { layoutKind = nil; return nil }
        let rootURL = URL(fileURLWithPath: (root as NSString).expandingTildeInPath)
        guard fm.fileExists(atPath: rootURL.appendingPathComponent("scripts/lib_edge.sh").path)
        else { layoutKind = nil; return nil }
        layoutKind = "repo"
        let logDir = rootURL.appendingPathComponent("logs")
        var env = baseEnv()
        env["EDGE_BACKEND_DIR"] = rootURL.appendingPathComponent("backend").path
        env["EDGE_BACKEND_PY"] = rootURL.appendingPathComponent("backend/.venv/bin/python").path
        env["EDGE_SEARX_DIR"] = rootURL.appendingPathComponent("searxng").path
        env["EDGE_SEARX_PY"] = rootURL.appendingPathComponent("searxng/.venv/bin/python").path
        env["EDGE_SEARX_SETTINGS"] = rootURL.appendingPathComponent("searxng/settings.yml").path
        env["EDGE_LOG_DIR"] = logDir.path
        env["EDGE_CONFIG"] = rootURL.appendingPathComponent("config.yaml").path
        // dev 桥接：devctl 的密钥来自 source .env(.local)，这里读同一份文件注入同名变量。
        // KEY=VALUE 的平面解析足够——那两个文件本就是给 set -a source 的赋值清单。
        // bundle 布局不走这条（密钥形态是 Keychain，R5-6）。
        for envFile in [".env", ".env.local"] {
            let f = rootURL.appendingPathComponent(envFile)
            guard let text = try? String(contentsOf: f, encoding: .utf8) else { continue }
            for line in text.split(separator: "\n") {
                let t = line.trimmingCharacters(in: .whitespaces)
                guard !t.isEmpty, !t.hasPrefix("#"), let eq = t.firstIndex(of: "=") else { continue }
                let k = String(t[..<eq]).trimmingCharacters(in: .whitespaces)
                var v = String(t[t.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
                v = v.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                if !k.isEmpty { env[k] = v }
            }
        }
        // B2c：仓库布局的 RAG 数据面承载（与 devctl 同一套 rag/.venv）；
        // EDGE_RAG_DATA_DIR 不设 = 数据根缺省 rag/ 本目录（devctl 现状）。
        // RAG_COMPUTE_API_KEY 无需单独注入：上面的 .env(.local) 平面解析已带全部键。
        env["EDGE_RAG_DIR"] = rootURL.appendingPathComponent("rag").path
        env["EDGE_RAG_PY"] = rootURL.appendingPathComponent("rag/.venv/bin/python").path
        // 与 devctl 同一句自报（devctl.sh 的 export AGENT_LAYOUT=repo）：同一套仓库检出、
        // 同一份 .env.local 密钥来源，形态就是 repo，不因发起者是 app 而改称。
        env["AGENT_LAYOUT"] = "repo"
        // 放在 .env(.local) 平面解析**之后**：会话 KV 落不落盘是设置页上的用户决定，
        // 仓库里那份开发用赋值清单不该盖掉它。
        injectKVPersist(&env)
        return Layout(libEdge: rootURL.appendingPathComponent("scripts/lib_edge.sh"),
                      env: env, logDir: logDir, isBundle: false)
    }

    /// 会话 KV 落盘的 env 注入（两种布局共用）。
    /// **决策权归执行端（数据主权）**：开不开、留几天由本机 app 的设置说了算，算力端
    /// 只负责存 + 到期清扫。关闭时两个变量一个都不注入——由 config 的缺省值（false）
    /// 生效，算力机上不留任何会话 KV。
    private func injectKVPersist(_ env: inout [String: String]) {
        guard let settings else { return }
        // 关也要**显式注 "0"**（首审 Critical）：repo 布局先平铺解析了 .env(.local)，
        // 里面残留的 INFER_KV_PERSIST=1 会在「不注入」时原样生效——隐私开关拨到关
        // 却继续往算力端落盘。app 管的 sidecar，app 的开关就是最终话语权。
        env["INFER_KV_PERSIST"] = settings.kvPersistEnabled ? "1" : "0"
        if settings.kvPersistEnabled {
            env["INFER_KV_TTL_DAYS"] = String(settings.kvTTLDays)
        }
    }

    /// 首启生成的 AGENT_API_KEY：32 字节系统熵，base64url（无需人抄写，只走 Keychain）。
    /// 设置页的「重新生成」（key 轮换）也用它——同一把熵源、同一形态。
    static func randomKey() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            return UUID().uuidString + UUID().uuidString   // 熵源失败的兜底，仍不可猜
        }
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .trimmingCharacters(in: CharacterSet(charactersIn: "="))
    }

    /// 密钥粘贴清洗：去首尾空白/换行、剥 `KEY=` 前缀（整行拷贝）、剥一层包裹引号。
    /// `.env.local` 的行是 `RAG_API_KEY="xxx"` 形态——shell source 会剥引号，
    /// env 直注不会；带引号/带变量名的值对服务端就是稳定 401（实机二三轮根因域）。
    /// 真钥匙是随机 token，不会长得像 `NAME=...`，前缀剥离不会误伤。
    static func cleanKey(_ raw: String) -> String {
        ServiceCredentials.cleanKey(raw)
    }

    private func baseEnv() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        // lib 依赖 lsof(/usr/sbin) 与 curl；.app 环境的 PATH 可能只有 /usr/bin:/bin
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/sbin:"
                    + (env["PATH"] ?? "/usr/bin:/bin")
        return env
    }

    // MARK: - lib 执行与探活

    private final class LibRun: @unchecked Sendable {
        let process = Process()
        private let pipe = Pipe()
        // readabilityHandler 在后台线程回调，锁住 buffer——不锁就是跨线程写 Data 的竞争
        private let lock = NSLock()
        private var buffer = Data()

        init(lib: URL, command: String, env: [String: String]) throws {
            process.executableURL = URL(fileURLWithPath: "/bin/bash")
            process.arguments = [lib.path, command]
            process.environment = env
            process.standardOutput = pipe
            process.standardError = pipe
            pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
                let data = h.availableData
                guard let self, !data.isEmpty else { return }
                self.lock.lock(); self.buffer.append(data); self.lock.unlock()
            }
            try process.run()
        }

        func wait() -> Int32 { process.waitUntilExit(); return process.terminationStatus }

        func waitAsync() async -> Int32 {
            await withCheckedContinuation { cont in
                process.terminationHandler = { p in cont.resume(returning: p.terminationStatus) }
                if !process.isRunning { cont.resume(returning: process.terminationStatus) }
            }
        }

        func outputTail(lines: Int) -> String {
            lock.lock(); defer { lock.unlock() }
            let text = String(data: buffer, encoding: .utf8) ?? ""
            return text.split(separator: "\n").suffix(lines).joined(separator: "\n")
        }
    }

    private func runLib(_ layout: Layout, command: String) throws -> LibRun {
        try LibRun(lib: layout.libEdge, command: command, env: layout.env)
    }

    /// /health 的实例指纹（backend/app.py 的 `health()`）。旧后端不报这三个字段，
    /// 解不出 service 即判 .foreign——不为「兼容旧后端」留后门：能被收养的必须自证身份。
    private struct HealthBody: Decodable {
        let service: String?
        let layout: String?
        let keyFP: String?
        enum CodingKeys: String, CodingKey {
            case service, layout
            case keyFP = "key_fp"
        }
    }

    /// 收养前的身份 + 密钥核对（免鉴权的 /health 上比对指纹，明文不出网也不入日志）。
    private func probeHealth() async -> HealthProbe {
        peerLayout = nil
        guard let url = URL(string: "http://127.0.0.1:14159/health") else { return .down }
        var req = URLRequest(url: url); req.timeoutInterval = 3
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse, http.statusCode == 200 else { return .down }
        guard let body = try? JSONDecoder().decode(HealthBody.self, from: data),
              body.service == "macagent-backend" else { return .foreign }
        peerLayout = body.layout
        // 对端 key_fp 为空 = 它没启用鉴权（dev 对端），任何 Bearer 都放行 → 不会 401
        let peerFP = body.keyFP ?? ""
        if peerFP.isEmpty { return .healthy }
        return peerFP == Self.keyFingerprint(settings?.apiKey ?? "") ? .healthy : .keyMismatch
    }

    /// 密钥指纹：sha256 十六进制前 8 位，与 backend 的 `key_fingerprint()` 同一算法。
    /// 空钥 → 空串（本地空 + 对端非空 = 不匹配，正是「app 没钥匙却去连有钥后端」那一档）。
    static func keyFingerprint(_ key: String) -> String {
        guard !key.isEmpty else { return "" }
        let hex = SHA256.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined()
        return String(hex.prefix(8))
    }
}

#endif
