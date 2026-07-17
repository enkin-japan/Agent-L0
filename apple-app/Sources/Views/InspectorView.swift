import SwiftUI
import AVKit
import QuickLook
#if os(iOS)
import UIKit
#endif

/// 检查器面板（对齐 web 端右侧栏）：计划 / 幻灯片 / 文件 / 工具集。
/// （系统提示词展示已按用户要求移除；工具只列名字。）
/// macOS 挂在 .inspector 的右侧栏；iOS 自适应为 sheet/列。
struct InspectorView: View {
    @EnvironmentObject var store: AgentStore

    enum Tab: String, CaseIterable {
        case plan, deck, files, tools

        var label: String {
            switch self {
            case .plan: return L("计划")
            case .deck: return L("幻灯片")
            case .files: return L("文件")
            case .tools: return L("工具集")
            }
        }
    }

    @State private var tab: Tab = .plan
    @State private var planSnapshot: TaskSnapshot?
    @State private var toolList: [ToolInfo]?
    @State private var showDeckViewer = false
    // 文件页签态
    @State private var files: [AssetFileItem]?
    @State private var busyFile: String?          // 正在下载中的文件名（行内菊花）
    @State private var mediaPreview: MediaPreviewRequest?
    @State private var imagePreviewPath: String?
    @State private var quickLookURL: URL?
    #if os(iOS)
    @State private var shareRequest: ShareRequest?
    #endif

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $tab) {
                ForEach(Tab.allCases, id: \.self) { item in
                    Text(item.label).tag(item)
                }
            }
            .pickerStyle(.segmented)
            // 四页签 + 日文词表（計画/スライド/ファイル/ツール）在检查器 min 260pt
            // 内默认字号会被裁——小号控件才放得下（实机 v0.16.0(45) 截图实证）
            .controlSize(.small)
            .padding(10)
            Divider()
            ScrollView {
                content
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
            }
        }
        .task(id: tab) { await load() }
        .task(id: store.selectedSessionId) {
            planSnapshot = nil
            files = nil
            await load()
        }
        // 消息里点了产物链接：切到文件页签（列表刷新由 task(id: tab) 顺带完成；
        // 已在文件页签时手动刷一次——链接指向的可能是刚生成的文件）
        .onChange(of: store.revealedAssetName) {
            guard store.revealedAssetName != nil else { return }
            if tab == .files {
                Task { await load() }
            } else {
                tab = .files
            }
        }
        .sheet(item: $mediaPreview) { req in
            MediaPlayerSheet(request: req)
        }
        .sheet(isPresented: Binding(get: { imagePreviewPath != nil },
                                    set: { if !$0 { imagePreviewPath = nil } })) {
            if let path = imagePreviewPath {
                VStack(spacing: Theme.s2) {
                    AssetImage(path: path, maxWidth: nil)
                    Button(L("关闭")) { imagePreviewPath = nil }
                        .keyboardShortcut(.cancelAction)
                }
                .padding(Theme.s3)
                #if os(macOS)
                .frame(minWidth: 480, minHeight: 320)
                #endif
            }
        }
        .quickLookPreview($quickLookURL)
        #if os(iOS)
        // 分享面板（存到文件 / 存入相册 / 转发到他 app）——macOS 的 打开/保存 在
        // iOS 上的对位物就是这一张系统 share sheet
        .sheet(item: $shareRequest) { req in
            ActivityShareView(url: req.url)
                .presentationDetents([.medium, .large])
        }
        #endif
    }

    @ViewBuilder
    private var content: some View {
        switch tab {
        case .plan:
            planContent
        case .deck:
            deckContent
        case .files:
            filesContent
        case .tools:
            toolsContent
        }
    }

    // MARK: - 计划（当前会话的多步任务；生成中优先显示 SSE 实时快照）

    @ViewBuilder
    private var planContent: some View {
        let snapshot = store.taskSnapshot ?? planSnapshot
        if let task = snapshot, let plan = task.plan, !plan.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                if let goal = task.goal, !goal.isEmpty {
                    Text(goal).font(.callout).bold()
                }
                ForEach(plan) { step in
                    HStack(alignment: .top, spacing: 8) {
                        Text(Self.stepIcon(step.status))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.title).font(.callout)
                            if let note = step.note, !note.isEmpty {
                                Text(note).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if let activity = store.activity, let current = activity.current,
                   !current.isEmpty, store.isGenerating {
                    Divider()
                    Label(current, systemImage: "bolt")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            emptyHint(L("当前会话没有进行中的多步任务计划"))
        }
    }

    static func stepIcon(_ status: String) -> String {
        switch status {
        case "done": return "✅"
        case "doing": return "▶️"
        case "failed": return "❌"
        case "skipped": return "⏭"
        default: return "○"
        }
    }

    // MARK: - 幻灯片（deck 缩略图纵列，点开放映）

    @ViewBuilder
    private var deckContent: some View {
        if let deck = store.deck, let sid = store.selectedSessionId {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text(deck.meta.title).font(.callout).bold()
                    Spacer()
                    Button {
                        showDeckViewer = true
                    } label: {
                        Label(L("放映"), systemImage: "play.rectangle")
                    }
                    .font(.caption)
                }
                ForEach(Array(deck.slides.enumerated()), id: \.element.id) { index, slide in
                    VStack(alignment: .leading, spacing: 2) {
                        GeometryReader { geo in
                            let scale = geo.size.width / SlideGeometry.designW
                            SlideCanvasView(slide: slide, meta: deck.meta, sessionId: sid)
                                .frame(width: SlideGeometry.designW, height: SlideGeometry.designH)
                                .scaleEffect(scale, anchor: .topLeading)
                        }
                        .aspectRatio(16 / 9, contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6)
                            .stroke(Color.primary.opacity(0.1)))
                        Text("\(index + 1)").font(.caption2).foregroundStyle(.secondary)
                    }
                    .onTapGesture { showDeckViewer = true }
                }
            }
            .sheet(isPresented: $showDeckViewer) {
                DeckViewer(deck: deck, sessionId: sid)
            }
        } else {
            emptyHint(L("当前会话没有演示文稿。让 Agent「做一份 PPT」即可生成。"))
        }
    }

    // MARK: - 文件（会话产物：assets/ 的规范出口，与后端 listing 同一口径）

    @ViewBuilder
    private var filesContent: some View {
        if let files, !files.isEmpty {
            VStack(alignment: .leading, spacing: Theme.s2) {
                ForEach(files) { item in
                    fileRow(item)
                }
            }
        } else if files != nil {
            emptyHint(L("当前会话还没有产物文件。图表、导出的 PDF、下载的音视频会出现在这里。"))
        } else {
            ProgressView().frame(maxWidth: .infinity)
        }
    }

    private func fileRow(_ item: AssetFileItem) -> some View {
        HStack(spacing: Theme.s2) {
            Image(systemName: FileKind.of(item.name).icon)
                .foregroundStyle(Theme.accent)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.name)
                    .font(.callout)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text("\(Self.sizeLabel(item.size)) · \(Timestamps.listLabel(Date(timeIntervalSince1970: item.mtime)))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: Theme.s1)
            if busyFile == item.name {
                ProgressView().controlSize(.small)
            } else {
                #if os(macOS)
                MessageActionButton(icon: "arrow.up.forward.app", help: L("打开")) {
                    open(item)
                }
                MessageActionButton(icon: "square.and.arrow.down", help: L("保存")) {
                    save(item)
                }
                #else
                MessageActionButton(icon: "square.and.arrow.up", help: L("分享")) {
                    share(item)
                }
                #endif
            }
        }
        .padding(Theme.s2)
        .background(store.revealedAssetName == item.name
                    ? Theme.accent.opacity(0.12) : Theme.wash)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
        .contentShape(Rectangle())
        .onTapGesture { preview(item) }
    }

    /// 行点击 = 预览：音视频走流式播放（Range 断点 + 鉴权头，秒开秒拖），
    /// 图片复用 AssetImage，其余下载后 Quick Look（PDF 等；iOS 的 QL 自带分享）。
    private func preview(_ item: AssetFileItem) {
        guard let sid = store.selectedSessionId, let client = store.client else { return }
        let path = item.assetPath(sessionId: sid)
        switch FileKind.of(item.name) {
        case .media:
            if let src = client.assetPlaybackSource(path: path) {
                mediaPreview = MediaPreviewRequest(name: item.name, url: src.url,
                                                   headers: src.headers)
            }
        case .image:
            imagePreviewPath = path
        case .other:
            download(item) { quickLookURL = $0 }
        }
    }

    private func open(_ item: AssetFileItem) {
        #if os(macOS)
        download(item) { NSWorkspace.shared.open($0) }
        #endif
    }

    #if os(iOS)
    /// iOS 的行内操作：下载完弹系统分享面板（存到文件 / 存入相册 / AirDrop / 转发）。
    private func share(_ item: AssetFileItem) {
        download(item) { shareRequest = ShareRequest(url: $0) }
    }
    #endif

    private func save(_ item: AssetFileItem) {
        #if os(macOS)
        let panel = NSSavePanel()
        panel.nameFieldStringValue = item.name
        panel.begin { resp in
            guard resp == .OK, let dest = panel.url else { return }
            download(item) { tmp in
                do {
                    try? FileManager.default.removeItem(at: dest)
                    try FileManager.default.copyItem(at: tmp, to: dest)
                } catch {
                    store.errorMessage = "\(L("保存失败："))\(error.localizedDescription)"
                }
            }
        }
        #endif
    }

    /// 经鉴权 API 流式下载到临时目录（本地/远程后端同一条路，铁律 4）。
    private func download(_ item: AssetFileItem, then action: @escaping (URL) -> Void) {
        guard let sid = store.selectedSessionId, let client = store.client,
              busyFile == nil else { return }
        busyFile = item.name
        Task {
            defer { busyFile = nil }
            do {
                let url = try await client.downloadAsset(
                    path: item.assetPath(sessionId: sid), suggestedName: item.name)
                action(url)
            } catch {
                store.errorMessage = "\(L("下载失败："))\(error.localizedDescription)"
            }
        }
    }

    private static func sizeLabel(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    // MARK: - 工具集（只列名字；需审批的带锁标）

    @ViewBuilder
    private var toolsContent: some View {
        if let tools = toolList {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(tools) { tool in
                    HStack(spacing: 6) {
                        Text(tool.name)
                            .font(.system(.callout, design: .monospaced))
                        if tool.requiresApproval == true {
                            Image(systemName: "lock")
                                .font(.caption2)
                                .foregroundStyle(.orange)
                                .help(L("需审批"))
                        }
                    }
                }
            }
        } else {
            ProgressView().frame(maxWidth: .infinity)
        }
    }

    private func emptyHint(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.top, 24)
    }

    // MARK: - 按页懒加载

    private func load() async {
        guard let client = store.client else { return }
        switch tab {
        case .plan:
            if !store.isGenerating, let sid = store.selectedSessionId {
                planSnapshot = (try? await client.taskStatus(sessionId: sid))?.task
            }
        case .tools:
            if toolList == nil {
                toolList = (try? await client.tools()) ?? []
            }
        case .files:
            if let sid = store.selectedSessionId {
                files = (try? await client.sessionFiles(sessionId: sid)) ?? files ?? []
            } else {
                files = []
            }
        case .deck:
            break
        }
    }
}

// MARK: - 文件页签的支撑件

/// 按扩展名归类：决定预览走哪条路。
private enum FileKind {
    case media   // 流式播放（视频/音频）
    case image
    case other   // 下载后 Quick Look

    static func of(_ name: String) -> FileKind {
        switch (name as NSString).pathExtension.lowercased() {
        case "mp4", "mov", "m4v", "webm", "m4a", "mp3", "wav", "aac", "flac", "ogg":
            return .media
        case "png", "jpg", "jpeg", "gif", "webp", "heic":
            return .image
        default:
            return .other
        }
    }

    var icon: String {
        switch self {
        case .media: return "play.rectangle"
        case .image: return "photo"
        case .other: return "doc"
        }
    }
}

struct MediaPreviewRequest: Identifiable {
    let name: String
    let url: URL
    let headers: [String: String]
    var id: String { name }
}

#if os(iOS)
struct ShareRequest: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}

/// 系统分享面板的 SwiftUI 包壳（UIActivityViewController 没有原生 SwiftUI 呈现件；
/// ShareLink 是按钮不是面板，接不上「下载完成后再弹」的时序）。
struct ActivityShareView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }

    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}
#endif

/// 流式播放 sheet：AVURLAsset 注入 Authorization 头，Range 断点由后端 206 支持，
/// 拖进度条按字节段取——不用整片下完（个人流媒体的全部基本盘）。
/// ⚠️ 不用 SwiftUI 的 VideoPlayer：它在 macOS 27 beta 上实例化即崩
/// （_AVKit_SwiftUI 泛型元数据初始化 getSuperclassMetadata fatalError，
/// 实机崩溃报告 ×2 同签名）——走老牌 AVPlayerView/AVPlayerViewController
/// 的 representable 包壳，完全绕开 _AVKit_SwiftUI。
struct MediaPlayerSheet: View {
    let request: MediaPreviewRequest
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer?

    var body: some View {
        VStack(spacing: Theme.s2) {
            HStack {
                Text(request.name)
                    .font(.callout).bold()
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                Button(L("关闭")) { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            Group {
                if let player {
                    ClassicPlayerView(player: player)
                } else {
                    Color.black
                }
            }
            .aspectRatio(16 / 9, contentMode: .fit)
        }
        .padding(Theme.s3)
        #if os(macOS)
        .frame(minWidth: 560, minHeight: 380)
        #endif
        .onAppear {
            let asset = AVURLAsset(url: request.url,
                                   options: ["AVURLAssetHTTPHeaderFieldsKey": request.headers])
            let p = AVPlayer(playerItem: AVPlayerItem(asset: asset))
            player = p
            p.play()
        }
        .onDisappear { player?.pause() }
    }
}

#if os(macOS)
/// AVKit 经典播放视图的 SwiftUI 包壳（见 MediaPlayerSheet 头注的崩溃规避）。
struct ClassicPlayerView: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        view.controlsStyle = .inline
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        view.player = player
    }
}
#else
struct ClassicPlayerView: UIViewControllerRepresentable {
    let player: AVPlayer

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let vc = AVPlayerViewController()
        vc.player = player
        return vc
    }

    func updateUIViewController(_ vc: AVPlayerViewController, context: Context) {
        vc.player = player
    }
}
#endif
