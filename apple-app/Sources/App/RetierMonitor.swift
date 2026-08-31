import Foundation
import SwiftUI

#if os(macOS)
import AppKit
import UserNotifications

/// 嵌入档位换档检测与重建驱动（分离部署缺陷一的执行端半场）。
///
/// **为什么要有这一层**：模型的事归计算端，执行端只有 app（分离部署铁则）。计算端换了
/// 嵌入模型档位后，本机知识库里的向量还是旧档嵌出来的——维度/语义都不再可比，检索结果
/// 从此是噪声，而且**没有任何报错**。用户不该靠自己发现这件事，也不该被要求跑仓库脚本
/// （执行端连仓库都没有）：换档的发现与重建入口必须长在 app 里。
///
/// **判定不在这里做**：库戳 vs 计算面活体身份的比对由数据面 `rag/retier.py` 一处判定，
/// backend 只是薄代理（`/api/v1/rag/retier`）。本类只做 app 独有的三件事——按节奏问、
/// 拿用户的确认、把进度与结局呈现出来。在 Swift 里重算 mismatch = 第二份判据。
///
/// **重建是破坏性的**：先清库再逐篇重灌，期间检索不可用，所以绝不自动触发——一律
/// 弹窗等用户点头（原文资料不动，最坏情况是重来一次）。
@MainActor
final class RetierMonitor: ObservableObject {

    /// 重建进行中的一行状态（菜单栏面板显示）；空闲 = nil。
    @Published private(set) var rebuildProgress: String?

    private weak var settings: AppSettings?
    private var loop: Task<Void, Never>?
    /// 用户按过「稍后」：本次运行期内不再打扰。下次启动重新检测——换档是低频事件，
    /// 持久化「永久不提醒」只会让库静默烂在旧档上。
    private var snoozed = false
    /// 正在跟进某次重建（5 秒轮询循环在跑）：挡住重入，也挡住这期间的换档弹窗。
    private var following = false

    /// 首检延迟 60 秒：app 刚起时 sidecar/backend 还在拉起，这时候问必然是连接失败。
    private static let firstDelay: TimeInterval = 60
    /// 常规巡检 30 分钟：换档是「用户去计算端换了模型」这种小时级事件，问密了没意义。
    private static let interval: TimeInterval = 30 * 60
    /// 重建期间的进度轮询节奏。
    private static let pollInterval: TimeInterval = 5
    /// 跟进期间连续拿不到状态的容忍次数（5s × 60 = 5 分钟）：backend 中途死了也要能
    /// 退出循环，否则 30 分钟巡检循环被永久卡在这里。
    private static let pollFailureBudget = 60
    /// 停滞预算（首审 S）：状态一直 running 但 docs_done 纹丝不动超过此时长 = 数据面
    /// 卡死（如嵌入调用挂起）。项目长任务纪律是「停滞+超时双报警」——不可达有预算、
    /// 停滞也必须有，否则 30 分钟巡检循环被一场僵住的重建饿死。单篇巨文档的合法
    /// 嵌入最长 15 分钟（计算面 embed 超时 900s），30 分钟足够宽。
    private static let stallBudget: TimeInterval = 30 * 60

    func configure(settings: AppSettings) { self.settings = settings }

    // MARK: - 巡检循环

    /// 生命周期随 app 进程（MacAppDelegate 里启动，sidecar autostart 之后）。幂等。
    func start() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.firstDelay))
            while !Task.isCancelled {
                await self?.checkOnce()
                try? await Task.sleep(for: .seconds(Self.interval))
            }
        }
    }

    private func checkOnce() async {
        guard !following else { return }
        // 首启引导没完成前不打扰（首审 S）：push.bootstrap 同款后置纪律——
        // OnboardingView 还开着就弹一个知识库重建的模态框，是对新用户的伏击。
        guard settings?.setupCompleted == true else { return }
        // 网络失败静默跳过：backend 没起、用户没连隧道都是常态，不刷日志更不弹窗。
        guard let status = await fetchStatus() else { return }

        if status.rebuild.state == "running" {
            // 别处（命令行直呼数据面端点等）已经在重建：不弹窗，但把进度接过来
            // 显示到底，免得菜单栏对一场正在破坏性重建的事一无所知。
            await follow(assumeRunning: true)
            return
        }
        // 判据是数据面的 needs_rebuild：真换档（mismatch）与中断残局（interrupted，
        // 上一场重建死在半路——单看档位戳反而是「健康」假象）都要提示补跑。
        guard status.needsRebuild == true, !snoozed else { return }
        await promptAndRebuild(status)
    }

    // MARK: - 提示与触发

    private func promptAndRebuild(_ status: RetierStatus) async {
        let unknown = L("未知")
        let sep = L("：")   // 标点也过 L（首审 S）：英文界面不该出现全角冒号
        NSApp.activate()   // 菜单栏常驻形态下窗口可能全关着
        let alert = NSAlert()
        alert.messageText = L("知识库需要重建")
        // 中断残局与真换档的说明不同：前者两侧档位往往已一致，报「换档」是误导
        let reason = (status.interrupted == true && status.mismatch != true)
            ? L("上次知识库重建中途中断，库内数据不完整，需要重新执行全量重建；原文资料不受影响。")
            : L("计算端已更换嵌入档位，知识库须按新档位全量重嵌入后才能继续使用检索；重建期间知识库暂不可用，原文资料不受影响。")
        alert.informativeText = """
        \(L("计算端档位"))\(sep)\(status.compute?.tier ?? unknown)
        \(L("本机知识库档位"))\(sep)\(status.store?.tier ?? unknown)

        \(reason)
        """
        alert.addButton(withTitle: L("立即重建"))
        let later = alert.addButton(withTitle: L("稍后"))
        later.keyEquivalent = "\u{1b}"   // NSAlert 只给 "Cancel" 字面配 Esc，本地化按钮要自己接
        guard alert.runModal() == .alertFirstButtonReturn else {
            snoozed = true
            return
        }
        await startRebuild(baselineStartedAt: status.rebuild.startedAt)
    }

    /// force 恒 false：force 是「两侧已同档也要重灌」的修库开关，用户从提示进来的
    /// 这条路永远是「档位真的不一致」，不该带上它（数据面同档时会 409 拒绝白重灌）。
    private struct ForceBody: Encodable { let force: Bool }

    private func startRebuild(baselineStartedAt: String? = nil) async {
        guard var req = makeRequest("POST", "/api/v1/rag/retier/rebuild",
                                    body: ForceBody(force: false)) else { return }
        // POST 背后串着「backend 代理（25s）→ 数据面活体探查」；比代理再宽一档，
        // 让「代理先答」成为常态、URLSession 超时成为例外。
        req.timeoutInterval = 35
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            // 409 = 已同档 / 已有重建在跑：都不是错误。转入 follow 求真相——在跑就
            // 接管进度显示到底；没在跑则把 409 的说明摆给用户（修正评审：sawRunning
            // 门修好假成功后，这条路曾退化回「一片寂静」——刚点了按钮必须有下文）。
            if code == 409 {
                if await follow(baselineStartedAt: baselineStartedAt) == false {
                    showError(detail(from: data, code: code))
                }
                return
            }
            // 5xx 同样先求真相（阶段复审 m3）：backend 代理 25s 超时会把「数据面已
            // 开跑但还没答完」翻译成一个 503 的**正常 HTTP 响应**——只有确认没在跑
            // （且 started_at 没变过=真没跑过）才把错误摆给用户。
            if code >= 500 {
                if await follow(baselineStartedAt: baselineStartedAt) == false {
                    showError(detail(from: data, code: code))
                }
                return
            }
            guard (200..<300).contains(code) else {
                showError(detail(from: data, code: code))
                return
            }
        } catch {
            // 首审 Critical：超时/断连 ≠ 没开始——backend 可能已受理并spawn了重建线程。
            // 这里断言「失败」会让用户在一场进行中的破坏性重建旁边被告知“没在重建”。
            // 转入 follow 轮询求真相：真没开跑，follow 首拍即退；开跑了，进度如实呈现。
            _ = await follow(baselineStartedAt: baselineStartedAt)
            return
        }
        // POST 2xx = 数据面亲口说 running：本场重建由我们发起，结局必须通知
        _ = await follow(assumeRunning: true)
    }

    // MARK: - 跟进进度

    /// 返回值 =「这场重建确实发生了」（亲见 running / POST 2xx 保证 / started_at
    /// 相对基线变过）——调用方据此决定要不要把拒绝/错误摆给用户。
    @discardableResult
    private func follow(assumeRunning: Bool = false,
                        baselineStartedAt: String? = nil) async -> Bool {
        guard !following else { return assumeRunning }
        following = true
        defer {
            following = false
            rebuildProgress = nil
        }
        var failures = 0
        var last: RebuildInfo?
        // 结局通知的门（阶段复审 m2）：数据面的 _progress 收尾后**停在终态、永不回
        // idle**，follow 首拍就可能读到上一场的残留 done/failed——没亲眼见过 running
        //（也没有 POST 2xx 的保证）就通知「重建完成」，是把旧结局当新结局报。
        // started_at 基线（修正评审）：小库可能在 POST 超时窗内**跑完**——首拍即是
        // 终态但 started_at 已变，同样算「这场发生了」，不许假失败/假沉默。
        var sawRunning = assumeRunning
        var lastDone = -1
        var lastAdvance = Date()
        while !Task.isCancelled {
            guard let status = await fetchStatus() else {
                // 重建期间数据面很忙，抖一两拍是正常的；连续失败到预算上限才放手。
                failures += 1
                if failures >= Self.pollFailureBudget {
                    // 只有确知一场重建在跑时才值得报警（修正评审：POST 根本没到达
                    // backend 的场景里，这条通知会对一场从未开始的重建喊停滞）
                    if sawRunning {
                        notifyStalled(L("已联系不上执行层，进度未知——请检查数据面日志。"))
                    }
                    return sawRunning
                }
                try? await Task.sleep(for: .seconds(Self.pollInterval))
                continue
            }
            failures = 0
            last = status.rebuild
            if let sa = status.rebuild.startedAt, let base = baselineStartedAt, sa != base {
                sawRunning = true   // 基线变过 = 这场确实开跑过（可能已在 POST 窗内跑完）
            }
            guard status.rebuild.state == "running" else { break }
            sawRunning = true
            // 停滞预算（首审 S）：一直 running 但计数不动 = 数据面卡死，不能永远跟着
            let done = status.rebuild.docsDone ?? -1
            if done != lastDone {
                lastDone = done
                lastAdvance = Date()
            } else if Date().timeIntervalSince(lastAdvance) > Self.stallBudget {
                notifyStalled(L("进度长时间停滞——请检查数据面日志。"))
                return true
            }
            rebuildProgress = progressLine(status.rebuild)
            try? await Task.sleep(for: .seconds(Self.pollInterval))
        }
        if let last, sawRunning, last.state != "running" { notifyFinished(last) }
        return sawRunning
    }

    private func progressLine(_ r: RebuildInfo) -> String {
        // docs_total 在原文清点完成前是 null（dump_categories 阶段），这时只说在跑。
        guard let total = r.docsTotal else { return L("知识库重建中…") }
        return "\(L("知识库重建中…")) \(r.docsDone ?? 0)/\(total)"
    }

    // MARK: - 结局通知

    /// 重建是分钟级到小时级的活，用户早走开了——结局必须走系统通知，不是窗口。
    /// 授权在 PushManager.bootstrap 里统一申请过，这里只用不申请：没授权就静默作罢
    /// （为了一条运维通知再弹一次系统授权框是打扰）。
    private func notifyFinished(_ r: RebuildInfo) {
        let body: String
        switch r.state {
        case "done":
            body = "\(L("知识库重建完成"))\(L("（"))\(r.docsDone ?? 0) \(L("篇"))\(L("）"))"
        case "done_with_errors":
            // 总数用 docs_failed（failed_docs 只展示前 20 条，按它计数会低报损失）；
            // 过时标记的丢失同样要让唯一能补救的人看见（修正评审 nit）
            let n = r.docsFailed ?? r.failedDocs?.count ?? 0
            var parts: [String] = []
            if n > 0 { parts.append("\(n) \(L("篇失败（详见日志）"))") }
            let staleLost = (r.staleTotal ?? 0) - (r.staleRestored ?? 0)
            if staleLost > 0 { parts.append("\(staleLost) \(L("条过时标记未能重挂"))") }
            if parts.isEmpty { parts.append(L("部分收尾项有误（详见日志）")) }
            body = "\(L("知识库重建完成"))\(L("，"))" + parts.joined(separator: L("，"))
        case "failed":
            // 原始异常串可能很长（内嵌 repr）：截断 + 不许多行糊进通知中心
            let err = String((r.error ?? L("未知")).prefix(200))
            body = "\(L("知识库重建失败："))\(err)"
        default:
            return
        }
        postNotification(body)
    }

    private func notifyStalled(_ why: String) {
        postNotification("\(L("知识库重建中…"))\(L("，"))\(why)")
    }

    private func postNotification(_ body: String) {
        // app 自己的通知总开关也要认（首审 S）：其余本地通知路径全都先看它
        guard settings?.notifEnabled ?? false else { return }
        // 闭包内重新取 current()（而不是捕获外面那个）：UNUserNotificationCenter 非
        // Sendable，捕获它会在 @Sendable 回调里报并发告警。
        UNUserNotificationCenter.current().getNotificationSettings { s in
            guard s.authorizationStatus == .authorized
                    || s.authorizationStatus == .provisional else { return }
            let content = UNMutableNotificationContent()
            content.title = "MacAgent"
            content.body = body
            content.sound = .default
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: UUID().uuidString,
                                      content: content, trigger: nil))
        }
    }

    private func showError(_ detail: String) {
        NSApp.activate()
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = L("无法开始知识库重建")
        alert.informativeText = detail
        alert.addButton(withTitle: L("好"))
        alert.runModal()
    }

    // MARK: - 网络（与 app 连 backend 同一套鉴权：Bearer settings.apiKey）
    // 刻意用 URLSession.shared 而非 AgentClient 的自配置会话：这里全是低频短请求，
    // 逐请求 timeoutInterval 已覆盖差异，不值得为运维轮询挂进聊天客户端的会话栈。

    private func makeRequest(_ method: String, _ path: String,
                             body: Encodable? = nil) -> URLRequest? {
        guard let settings, let base = settings.baseURL,
              let url = URL(string: path, relativeTo: base) else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 10
        let key = settings.apiKey
        if !key.isEmpty {
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONEncoder().encode(body)
        }
        return req
    }

    private func fetchStatus() async -> RetierStatus? {
        guard let req = makeRequest("GET", "/api/v1/rag/retier") else { return nil }
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(RetierStatus.self, from: data)
    }

    /// FastAPI 的错误体是 {"detail": ...}；解不出就退回状态码，不把整段 HTML/空串糊给用户。
    private func detail(from data: Data, code: Int) -> String {
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let d = obj["detail"] as? String, !d.isEmpty {
            return d
        }
        return "HTTP \(code)"
    }

    // MARK: - 状态体（/api/v1/rag/retier 的解码面；判据在数据面，这里只读）

    private struct TierInfo: Decodable {
        let tier: String?
    }

    private struct RebuildInfo: Decodable {
        /// idle | running | done | done_with_errors | failed
        let state: String
        let docsTotal: Int?      // 原文清点完成前为 null
        let docsDone: Int?       // idle 态整个字段缺席
        let docsFailed: Int?     // 总失败数（failed_docs 只展示前 20 条）
        let failedDocs: [String]?
        let staleTotal: Int?     // 过时标记：快照总数 / 重挂成数（丢失 = 差值）
        let staleRestored: Int?
        let startedAt: String?   // 本场开跑时刻：follow 的基线比对用
        let error: String?

        enum CodingKeys: String, CodingKey {
            case state, error
            case docsTotal = "docs_total"
            case docsDone = "docs_done"
            case docsFailed = "docs_failed"
            case failedDocs = "failed_docs"
            case staleTotal = "stale_total"
            case staleRestored = "stale_restored"
            case startedAt = "started_at"
        }
    }

    private struct RetierStatus: Decodable {
        let store: TierInfo?
        let compute: TierInfo?
        /// null = 无戳（新库）或计算面身份不全：不是「换档」，不提示。
        let mismatch: Bool?
        /// 上一场重建中途死掉（耐久标记在盘上）：档位戳看着健康，库其实是残的。
        let interrupted: Bool?
        /// app 的唯一提示判据：mismatch==true 或 interrupted（数据面统一判定）。
        let needsRebuild: Bool?
        let rebuild: RebuildInfo

        enum CodingKeys: String, CodingKey {
            case store, compute, mismatch, interrupted, rebuild
            case needsRebuild = "needs_rebuild"
        }
    }
}

#endif
