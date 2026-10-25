import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

/// 输入区：多行文本 + 附件（文件 / iOS 相册 / macOS 剪贴板图片）+ 语音（WAV 录音）
/// + 思考开关 + 发送/停止。主模型是 VLM，发图是一等公民：附件 chips 对图片显示缩略图。
struct ComposerView: View {
    @EnvironmentObject var store: AgentStore
    @StateObject private var recorder = WavRecorder()

    @State private var draft = ""
    @State private var uploads: [Upload] = []
    @State private var thinking = false
    @State private var showImporter = false
    @State private var micDenied = false
    /// 文件正拖悬在输入区上方（虚线主色框反馈）
    @State private var dropTargeted = false
    /// 斜杠候选列表的当前选中行（↑↓ 移动，Tab/回车补全）
    @State private var slashSelection = 0
    /// 发送前拦截的两个暂停态：后端 accept_turn 会先作废旧的暂停轮（语义正确），
    /// 但作废是不可逆的——发送前必须让用户知情确认，别机械丢掉待审批/挂起的进度。
    @State private var confirmAbandonApproval = false
    @State private var confirmAbandonSuspended = false
    #if os(iOS)
    @State private var showPhotos = false
    @State private var photoItems: [PhotosPickerItem] = []
    /// 剪贴板有图时输入栏旁出现系统粘贴按钮（PasteButton 免授权弹窗）
    @State private var clipboardHasImage = false
    @Environment(\.scenePhase) private var scenePhase
    #endif
    #if os(macOS)
    /// ⌘V 直接粘贴：keyDown 监听器句柄（onAppear 装、onDisappear 拆）
    @State private var pasteMonitor: Any?
    #endif

    var body: some View {
        VStack(spacing: 6) {
            if !uploads.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(uploads) { upload in
                            chip(upload)
                        }
                    }
                }
            }

            // 斜杠候选（Claude Code 式）：/ 起头即弹，前缀过滤，↑↓ 选择，Tab/回车补全。
            // 缺依赖的剧本灰显 + 注明缺什么（点名它仍会发出：后端会前置自动重扫）。
            if !slashCandidates.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(slashCandidates.enumerated()), id: \.element.id) { index, skill in
                        Button {
                            completeSlash(skill)
                        } label: {
                            HStack(spacing: 8) {
                                Text("/" + skill.name)
                                    .font(.callout.monospaced())
                                    .foregroundStyle(skill.available == false
                                                     ? AnyShapeStyle(.secondary)
                                                     : AnyShapeStyle(Theme.accent))
                                Text(skill.description)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                Spacer(minLength: 0)
                                if skill.available == false {
                                    Text(L("缺依赖：") + (skill.missing ?? []).joined(separator: ", "))
                                        .font(.caption2)
                                        .foregroundStyle(Theme.warning)
                                        .lineLimit(1)
                                }
                            }
                            .padding(.horizontal, Theme.s2)
                            .padding(.vertical, 5)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(index == clampedSlashSelection
                                        ? Theme.accent.opacity(0.12) : Color.clear)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(CeladonControlStyle())
                    }
                }
                .background(Theme.card)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard)
                    .strokeBorder(Theme.hairline, lineWidth: 1))
            }

            // 命中徽标：首令牌 = 已知剧本 → 确认本轮将按剧本确定性分发
            if let skill = matchedSkill {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.seal.fill")
                        .font(.caption)
                        .foregroundStyle(Theme.success)
                    Text("/" + skill.name)
                        .font(.caption.monospaced().weight(.medium))
                    Text(skill.description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, Theme.s2)
                .padding(.vertical, 4)
                .background(Theme.success.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
            }

            VStack(alignment: .leading, spacing: Theme.s2) {
                TextField(L("给 Agent 发消息…"), text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(Theme.readingFont)
                    .lineLimit(1...8)
                    .padding(.horizontal, Theme.s1)
                    .padding(.top, Theme.s1)
                    .frame(minHeight: Theme.controlHitSize, alignment: .topLeading)
                    .onSubmit(send)
                    // 候选弹出期间键盘接管：↑↓ 移选、Tab/回车补全；候选收起后
                    // 全部 .ignored 走原路（回车 onSubmit 发送）。剧本名是 ASCII，
                    // 弹出窗口期不与中文输入法的回车确认冲突。
                    .onKeyPress(.upArrow) {
                        guard !slashCandidates.isEmpty else { return .ignored }
                        moveSlashSelection(-1)
                        return .handled
                    }
                    .onKeyPress(.downArrow) {
                        guard !slashCandidates.isEmpty else { return .ignored }
                        moveSlashSelection(1)
                        return .handled
                    }
                    .onKeyPress(.tab) {
                        guard !slashCandidates.isEmpty else { return .ignored }
                        completeSlash(slashCandidates[clampedSlashSelection])
                        return .handled
                    }
                    .onKeyPress(keys: [.return], phases: .down) { press in
                        if !slashCandidates.isEmpty {
                            completeSlash(slashCandidates[clampedSlashSelection])
                            return .handled
                        }
                        #if os(macOS)
                        // ⇧↩ = 换行（用户定；此前与 ↩ 同为发送）。光标处插行借
                        // field editor 的标准响应链动作（⌥↩ 同款）——SwiftUI 的
                        // TextField 拿不到光标位置，拼接字符串会插错地方。
                        if press.modifiers.contains(.shift) {
                            NSApp.sendAction(#selector(NSResponder.insertNewlineIgnoringFieldEditor(_:)),
                                             to: nil, from: nil)
                            return .handled
                        }
                        #endif
                        return .ignored   // 素 ↩ 走原路：onSubmit 发送
                    }
                    .onChange(of: draft) { slashSelection = 0 }

                HStack(spacing: Theme.s1) {
                    attachMenu

                    #if os(iOS)
                    if clipboardHasImage {
                        PasteButton(payloadType: PastedImage.self) { items in
                            Task { @MainActor in
                                let stamp = Int(Date().timeIntervalSince1970)
                                for (index, item) in items.enumerated() {
                                    let kind = Multipart.sniffImage(item.data)
                                    uploads.append(Upload(
                                        filename: "clipboard-\(stamp)-\(index).\(kind.ext)",
                                        mime: kind.mime, data: item.data))
                                }
                            }
                        }
                        .labelStyle(.iconOnly)
                        .buttonBorderShape(.capsule)
                        .frame(minWidth: Theme.controlHitSize, minHeight: Theme.controlHitSize)
                        .tint(.secondary)
                    }
                    #endif

                    Button {
                        toggleRecording()
                    } label: {
                        Image(systemName: recorder.isRecording ? "mic.fill" : "mic")
                            .font(.body)
                            .foregroundStyle(recorder.isRecording ? Theme.danger : Color.secondary)
                            .symbolEffect(.pulse, isActive: recorder.isRecording)
                            .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(CeladonControlStyle())
                    .disabled(store.isGenerating || store.selectedSessionIsDeleting)
                    .help(L("语音输入（松开即作为音频附件发送给后端转写）"))
                    .accessibilityLabel(L("语音输入"))

                    Toggle(isOn: $thinking) {
                        Image(systemName: "brain")
                            .font(.body)
                            .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                    }
                    .toggleStyle(.button)
                    .buttonStyle(CeladonControlStyle(selected: thinking))
                    .foregroundStyle(thinking ? Theme.thoughts : Color.secondary)
                    .background(thinking ? Theme.thoughtsWash : Color.clear)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
                    .help(L("深度思考模式"))
                    .accessibilityLabel(L("深度思考模式"))

                    #if os(macOS)
                    Text(L("回车发送 · ⇧回车换行"))
                        .font(.caption2)
                        .foregroundStyle(Theme.secondaryText)
                        .lineLimit(1)
                        .padding(.leading, Theme.s2)
                    #endif
                    Spacer(minLength: Theme.s1)

                    if store.isGenerating {
                        Button {
                            store.cancelGeneration()
                        } label: {
                            Image(systemName: "stop.fill")
                                .font(.body.weight(.semibold))
                                .foregroundStyle(Theme.danger)
                                .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                                .background(Theme.wash, in: Circle())
                        }
                        .buttonStyle(CeladonControlStyle(filled: true, cornerRadius: Theme.controlHitSize / 2))
                        .disabled(store.controlRequest != nil || store.selectedSessionIsDeleting)
                        .help(L("停止后续执行（已完成的副作用不会自动撤销）"))
                        .accessibilityLabel(L("停止"))
                    } else {
                        Button(action: send) {
                            Image(systemName: "arrow.up")
                                .font(.body.weight(.semibold))
                                .foregroundStyle(canSend ? Theme.accentInk : Color.secondary)
                                .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                                .background(canSend ? Theme.accent : Theme.wash, in: Circle())
                        }
                        .buttonStyle(CeladonControlStyle(filled: true, cornerRadius: Theme.controlHitSize / 2))
                        .disabled(!canSend)
                        .keyboardShortcut(.return, modifiers: .command)
                        .help(L("发送消息"))
                        .accessibilityLabel(L("发送消息"))
                    }
                }
            }
            .padding(Theme.s3)
            .background(Theme.inputSurface)
            .clipShape(RoundedRectangle(cornerRadius: Theme.radiusComposer))
            .overlay(RoundedRectangle(cornerRadius: Theme.radiusComposer)
                .strokeBorder(Theme.separator, lineWidth: 1))
            .shadow(color: Theme.cardShadow, radius: 8, y: 3)
        }
        .frame(maxWidth: Theme.readingWidth)
        .padding(.horizontal, Theme.contentPadding)
        .padding(.top, Theme.s2)
        .padding(.bottom, Theme.s4)
        .frame(maxWidth: .infinity)
        // 从 Finder / 文件 App 拖文件进输入区 → 附件化（与文件选择器/⌘V 同一条导入路）。
        // 悬停时描一圈虚线主色框作为可放提示；生成中拒收（与其他附件入口口径一致）。
        .dropDestination(for: URL.self) { urls, _ in
            guard !store.isGenerating else { return false }
            addFileURLs(urls)
            return true
        } isTargeted: { dropTargeted = $0 }
        .overlay {
            if dropTargeted {
                RoundedRectangle(cornerRadius: Theme.radiusComposer)
                    .strokeBorder(Theme.accent, style: StrokeStyle(lineWidth: 2, dash: [6]))
            }
        }
        .fileImporter(isPresented: $showImporter,
                      allowedContentTypes: [.item],
                      allowsMultipleSelection: true) { result in
            importFiles(result)
        }
        #if os(iOS)
        .photosPicker(isPresented: $showPhotos, selection: $photoItems,
                      maxSelectionCount: 6, matching: .images)
        .onChange(of: photoItems) {
            importPhotos()
        }
        .onAppear { refreshClipboardState() }
        .onChange(of: scenePhase) { refreshClipboardState() }
        .onReceive(NotificationCenter.default.publisher(
            for: UIPasteboard.changedNotification)) { _ in
            refreshClipboardState()
        }
        #endif
        #if os(macOS)
        .onAppear { installPasteMonitor() }
        .onDisappear { removePasteMonitor() }
        #endif
        .onChange(of: store.composerPrefill, initial: true) {
            // 「编辑并重发」通道：store 回填上一条用户输入
            if let prefill = store.composerPrefill {
                draft = prefill
                store.composerPrefill = nil
            }
        }
        .alert(L("麦克风权限被拒绝"), isPresented: $micDenied) {
            Button(L("好"), role: .cancel) {}
        } message: {
            Text(L("请在系统设置中允许本 App 使用麦克风。"))
        }
        // 暂停态发送确认（两态各一份：标题随状态切换会在消散动画里闪字）。
        // 取消 = 什么都不发生，草稿与附件原样留在输入区。
        .confirmationDialog(L("有操作在等你审批，发送新消息将放弃它。"),
                            isPresented: $confirmAbandonApproval,
                            titleVisibility: .visible) {
            Button(L("放弃并发送"), role: .destructive) { performSend() }
            Button(L("取消"), role: .cancel) {}
        }
        .confirmationDialog(L("当前有挂起的任务，发送新消息将放弃它。"),
                            isPresented: $confirmAbandonSuspended,
                            titleVisibility: .visible) {
            Button(L("放弃并发送"), role: .destructive) { performSend() }
            Button(L("取消"), role: .cancel) {}
        }
    }

    // MARK: - 附件入口

    private var attachMenu: some View {
        Menu {
            Button {
                showImporter = true
            } label: {
                Label(L("选择文件…"), systemImage: "folder")
            }
            #if os(iOS)
            Button {
                showPhotos = true
            } label: {
                Label(L("照片图库…"), systemImage: "photo.on.rectangle")
            }
            #endif
        } label: {
            Image(systemName: "plus")
                .font(.body)
                .foregroundStyle(.secondary)
                .frame(width: Theme.controlHitSize, height: Theme.controlHitSize)
                .contentShape(Rectangle())
                .modifier(CeladonHoverSurface(cornerRadius: Theme.radiusSmall))
        }
        .menuIndicator(.hidden)
        .buttonStyle(CeladonControlStyle())
        .disabled(store.isGenerating || store.selectedSessionIsDeleting)
        .help(L("添加附件（图片 / PDF / 文档 / 音频）"))
        .accessibilityLabel(L("添加附件（图片 / PDF / 文档 / 音频）"))
    }

    /// 附件 chip：图片显示缩略图，其余显示回形针 + 文件名。
    private func chip(_ upload: Upload) -> some View {
        HStack(spacing: 4) {
            if upload.mime.hasPrefix("image/"), let image = CrossPlatformImage(data: upload.data) {
                thumbnail(image)
            } else {
                Image(systemName: upload.mime.hasPrefix("audio/") ? "waveform" : "paperclip")
            }
            Text(upload.filename).lineLimit(1)
            Button {
                uploads.removeAll { $0.id == upload.id }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .frame(minWidth: Theme.controlHitSize, minHeight: Theme.controlHitSize)
                    .contentShape(Rectangle())
            }
            .buttonStyle(CeladonControlStyle())
            .accessibilityLabel(L("移除附件") + " " + upload.filename)
        }
        .font(.caption)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Color.primary.opacity(0.06))
        .clipShape(Capsule())
    }

    private func thumbnail(_ image: CrossPlatformImage) -> some View {
        #if os(macOS)
        return Image(nsImage: image).resizable()
            .aspectRatio(contentMode: .fill)
            .frame(width: 28, height: 28)
            .clipShape(RoundedRectangle(cornerRadius: 6))
        #else
        return Image(uiImage: image).resizable()
            .aspectRatio(contentMode: .fill)
            .frame(width: 28, height: 28)
            .clipShape(RoundedRectangle(cornerRadius: 6))
        #endif
    }

    // MARK: - 斜杠候选（剧本提示）

    /// 正在敲斜杠名（行首 / 且其后尚无空白）时的过滤词；否则 nil = 不弹候选。
    /// 与后端 _SLASH_RE 同一心智：首令牌到第一个空白为止。
    private var slashQuery: String? {
        guard draft.hasPrefix("/") else { return nil }
        let rest = draft.dropFirst()
        guard !rest.contains(where: \.isWhitespace) else { return nil }
        return String(rest)
    }

    private var slashCandidates: [SkillItem] {
        guard let query = slashQuery else { return [] }
        return store.skills.filter { query.isEmpty || $0.name.hasPrefix(query) }
    }

    private var clampedSlashSelection: Int {
        min(slashSelection, max(slashCandidates.count - 1, 0))
    }

    /// 首令牌恰好命中已知剧本（名字后已敲空白）→ 确认徽标。
    private var matchedSkill: SkillItem? {
        guard slashQuery == nil, draft.hasPrefix("/") else { return nil }
        let token = String(draft.dropFirst().prefix(while: { !$0.isWhitespace }))
        return store.skills.first { $0.name == token }
    }

    private func completeSlash(_ skill: SkillItem) {
        draft = "/\(skill.name) "
        slashSelection = 0
    }

    private func moveSlashSelection(_ delta: Int) {
        let count = slashCandidates.count
        guard count > 0 else { return }
        slashSelection = (clampedSlashSelection + delta + count) % count
    }

    // MARK: - 发送

    private var canSend: Bool {
        !store.historyIsReadOnly && !store.selectedSessionIsDeleting && (!draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !uploads.isEmpty)
    }

    /// 发送入口：暂停态（待审批 / 已挂起）先弹确认，其余直发。
    /// 待审批与挂起互斥（后端同一时刻只有一种暂停态），审批优先判。
    private func send() {
        guard canSend, !store.isGenerating else { return }
        if store.pendingTools?.isEmpty == false {
            confirmAbandonApproval = true
            return
        }
        if store.isSuspended {
            confirmAbandonSuspended = true
            return
        }
        performSend()
    }

    private func performSend() {
        guard canSend, !store.isGenerating else { return }
        let prompt = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        // 纯语音发送时发空 prompt：后端摄入期会把 ASR 转写升格为本轮用户文本
        // （任务 goal/全文搜索/斜杠点名随之全通）。占位符只留给非音频附件——
        // 文档内容不是"用户的话"，不该被升格。
        let audioOnly = !uploads.isEmpty && uploads.allSatisfy { $0.mime.hasPrefix("audio/") }
        let effective = prompt.isEmpty ? (audioOnly ? "" : L("（见附件）")) : prompt
        store.send(prompt: effective, uploads: uploads, thinking: thinking)
        draft = ""
        uploads = []
    }

    // MARK: - 各路附件导入

    private func importFiles(_ result: Result<[URL], Error>) {
        guard case .success(let urls) = result else { return }
        addFileURLs(urls)
    }

    /// 文件 URL → 附件（文件选择器 / ⌘V 粘贴 / 拖放 三路共用）。
    /// security scope 按需取还（选择器/iOS 拖放需要；macOS 拖放/粘贴返回 false 无害）。
    /// 读不出的（目录、无权限）静默跳过——与既有选择器行为一致。
    private func addFileURLs(_ urls: [URL]) {
        for url in urls where url.isFileURL {
            let scoped = url.startAccessingSecurityScopedResource()
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            if let data = try? Data(contentsOf: url) {
                uploads.append(Upload(filename: url.lastPathComponent,
                                      mime: Multipart.mime(for: url.lastPathComponent),
                                      data: data))
            }
        }
    }

    #if os(iOS)
    private func importPhotos() {
        let items = photoItems
        photoItems = []
        guard !items.isEmpty else { return }
        Task {
            var picked: [Upload] = []
            for (index, item) in items.enumerated() {
                guard let data = try? await item.loadTransferable(type: Data.self) else { continue }
                let ext = item.supportedContentTypes.first?.preferredFilenameExtension ?? "jpg"
                let name = "photo-\(Int(Date().timeIntervalSince1970))-\(index).\(ext)"
                picked.append(Upload(filename: name, mime: Multipart.mime(for: name), data: data))
            }
            await MainActor.run { uploads.append(contentsOf: picked) }
        }
    }
    #endif

    #if os(macOS)
    /// ⌘V 直接粘贴进输入区：keyDown 本地监听。分流规则——
    /// Finder 复制的文件 → 附件化（Finder 会同时放文件名字符串，须先于文本判断）；
    /// 纯文本 → 放行给输入框正常粘贴；位图（截图等）→ 转附件。
    private func installPasteMonitor() {
        guard pasteMonitor == nil else { return }
        pasteMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
                  event.charactersIgnoringModifiers == "v",
                  !store.isGenerating,
                  NSApp.keyWindow?.attachedSheet == nil  // 设置等 sheet 打开时不拦截
            else { return event }

            let pasteboard = NSPasteboard.general
            if let urls = pasteboard.readObjects(forClasses: [NSURL.self]) as? [URL],
               urls.contains(where: \.isFileURL) {
                addFileURLs(urls)
                return nil  // 已消费
            }
            if pasteboard.string(forType: .string) != nil {
                return event  // 文本粘贴交回输入框
            }
            let stamp = Int(Date().timeIntervalSince1970)
            if let png = pasteboard.data(forType: .png) {
                uploads.append(Upload(filename: "clipboard-\(stamp).png",
                                      mime: "image/png", data: png))
                return nil
            }
            if let tiff = pasteboard.data(forType: .tiff),
               let rep = NSBitmapImageRep(data: tiff),
               let png = rep.representation(using: .png, properties: [:]) {
                uploads.append(Upload(filename: "clipboard-\(stamp).png",
                                      mime: "image/png", data: png))
                return nil
            }
            return event
        }
    }

    private func removePasteMonitor() {
        if let monitor = pasteMonitor {
            NSEvent.removeMonitor(monitor)
            pasteMonitor = nil
        }
    }
    #endif

    #if os(iOS)
    /// 只读 pasteboard 类型（不读内容，不触发系统粘贴授权提示）。
    private func refreshClipboardState() {
        clipboardHasImage = UIPasteboard.general.hasImages
    }
    #endif

    private func toggleRecording() {
        if recorder.isRecording {
            if let upload = recorder.stop() {
                uploads.append(upload)
            }
        } else {
            Task {
                guard await WavRecorder.requestPermission() else {
                    micDenied = true
                    return
                }
                try? recorder.start()
            }
        }
    }
}

#if os(iOS)
/// PasteButton 的图片载荷：以 .image 类型从剪贴板导入原始字节。
struct PastedImage: Transferable {
    let data: Data

    static var transferRepresentation: some TransferRepresentation {
        DataRepresentation(importedContentType: .image) { PastedImage(data: $0) }
    }
}
#endif
