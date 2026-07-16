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

    @Published private(set) var state: State = .disabled
    @Published private(set) var restartCount = 0

    private struct Layout {
        let libEdge: URL          // scripts/lib_edge.sh
        let env: [String: String] // EDGE_* + AGENT_DATA_DIR (+ dev 桥接的密钥)
        let logDir: URL
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

    func autostart() async {
        guard settings?.sidecarEnabled == true else { state = .disabled; return }
        guard let layout = resolveLayout() else {
            state = .failed("找不到执行层布局：bundle 无 Resources/backend，设置里也未填仓库路径")
            return
        }
        self.layout = layout
        // 先探活：已有人在跑（devctl 起的 dev 后端）就只用不管。收摊时也不碰它——
        // 「app 退出顺手杀掉用户的开发后端」是最恶劣的一种越权。
        if await healthOK() { state = .external; return }
        await up()
    }

    /// 用户在设置里翻开关后手动触发。
    func startStop() async {
        switch state {
        case .running, .starting:
            await down()
            state = .disabled
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
            await down()
            await autostart()
        case .failed:
            await autostart()
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

    private func down() async {
        guard let layout else { return }
        cancelWatchers()
        if let run = try? runLib(layout, command: "down") {
            _ = await run.waitAsync()
        }
    }

    /// kqueue 盯 pid 文件里的进程（lib 拉起的是脱管守护进程，不是本进程的子进程——
    /// DispatchSourceProcess 底层是 EVFILT_PROC，同用户即可盯非子进程）。
    private func watchPids() {
        cancelWatchers()
        guard let layout else { return }
        for name in ["backend", "searx"] {
            let pidFile = layout.logDir.appendingPathComponent("\(name).pid")
            guard let text = try? String(contentsOf: pidFile, encoding: .utf8),
                  let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)),
                  kill(pid, 0) == 0 else { continue }   // searx 可选：没起就不盯
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
        // bundle 布局：Resources/ 下带完整执行层（R5-5 之后成立）
        if let res = Bundle.main.resourceURL,
           fm.fileExists(atPath: res.appendingPathComponent("backend/app.py").path) {
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
            if let key = settings?.apiKey, !key.isEmpty { env["AGENT_API_KEY"] = key }
            // 拨设备 A 的两把必须留（inference-server / rag 服务端各自校验）；
            // 用户在设置里填（与设备 A 的 .env.local 同值），空 = 未启用鉴权的 dev 对端。
            // 注入端也清洗一次：钥匙串里可能躺着清洗逻辑上线前存入的带引号旧值。
            let infer = Self.cleanKey(settings?.inferApiKey ?? "")
            if !infer.isEmpty { env["INFER_API_KEY"] = infer }
            let rag = Self.cleanKey(settings?.ragApiKey ?? "")
            if !rag.isEmpty { env["RAG_API_KEY"] = rag }
            return Layout(libEdge: res.appendingPathComponent("scripts/lib_edge.sh"),
                          env: env, logDir: logDir)
        }
        // repo 布局：设置里填的仓库检出（开发形态，与 devctl 共用 lib/日志/pid）
        guard let root = settings?.sidecarRepoRoot, !root.isEmpty else { return nil }
        let rootURL = URL(fileURLWithPath: (root as NSString).expandingTildeInPath)
        guard fm.fileExists(atPath: rootURL.appendingPathComponent("scripts/lib_edge.sh").path)
        else { return nil }
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
        return Layout(libEdge: rootURL.appendingPathComponent("scripts/lib_edge.sh"),
                      env: env, logDir: logDir)
    }

    /// 首启生成的 AGENT_API_KEY：32 字节系统熵，base64url（无需人抄写，只走 Keychain）。
    private static func randomKey() -> String {
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
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let m = s.range(of: #"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*="#,
                           options: .regularExpression) {
            s = String(s[m.upperBound...])
        }
        if s.count >= 2, let f = s.first, f == s.last, f == "\"" || f == "'" {
            s = String(s.dropFirst().dropLast())
        }
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
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

    private func healthOK() async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:14159/health") else { return false }
        var req = URLRequest(url: url); req.timeoutInterval = 3
        guard let (_, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse else { return false }
        return http.statusCode == 200
    }
}

#endif
