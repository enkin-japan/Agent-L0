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

            HStack(alignment: .bottom, spacing: 8) {
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
                    .controlSize(.small)
                    .tint(.secondary)
                }
                #endif

                Button {
                    toggleRecording()
                } label: {
                    Image(systemName: recorder.isRecording ? "mic.fill" : "mic")
                        .font(.title3)
                        .foregroundStyle(recorder.isRecording ? Color.red : Color.primary)
                        .symbolEffect(.pulse, isActive: recorder.isRecording)
                }
                .buttonStyle(.plain)
                .disabled(store.isGenerating)
                .help(L("语音输入（松开即作为音频附件发送给后端转写）"))

                TextField(L("给 Agent 发消息…"), text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...8)
                    .padding(.horizontal, 10)
                    .padding(.vertical, Theme.s2)
                    .background(Theme.wash)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
                    .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard)
                        .strokeBorder(Theme.hairline, lineWidth: 1))
                    .onSubmit(send)

                Toggle(isOn: $thinking) {
                    Image(systemName: "brain")
                }
                .toggleStyle(.button)
                .tint(Theme.thoughts)
                .help(L("深度思考模式"))

                if store.isGenerating {
                    Button {
                        store.cancelGeneration()
                    } label: {
                        Image(systemName: "stop.circle.fill")
                            .font(.title2)
                            .foregroundStyle(.red)
                    }
                    .buttonStyle(.plain)
                    .help(L("取消本轮（后端整轮回滚）"))
                } else {
                    Button(action: send) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.title2)
                            .foregroundStyle(canSend ? Theme.accent : Color.secondary)
                    }
                    .buttonStyle(.plain)
                    .disabled(!canSend)
                    .keyboardShortcut(.return, modifiers: .command)
                }
            }
        }
        .padding(10)
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
        .onChange(of: store.composerPrefill) {
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
            Image(systemName: "plus.circle")
                .font(.title3)
        }
        .menuIndicator(.hidden)
        .buttonStyle(.plain)
        .disabled(store.isGenerating)
        .help(L("添加附件（图片 / PDF / 文档 / 音频）"))
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
            }
            .buttonStyle(.plain)
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

    // MARK: - 发送

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !uploads.isEmpty
    }

    private func send() {
        guard canSend, !store.isGenerating else { return }
        let prompt = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        store.send(prompt: prompt.isEmpty ? L("（见附件）") : prompt, uploads: uploads, thinking: thinking)
        draft = ""
        uploads = []
    }

    // MARK: - 各路附件导入

    private func importFiles(_ result: Result<[URL], Error>) {
        guard case .success(let urls) = result else { return }
        for url in urls {
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
                for url in urls where url.isFileURL {
                    if let data = try? Data(contentsOf: url) {
                        uploads.append(Upload(filename: url.lastPathComponent,
                                              mime: Multipart.mime(for: url.lastPathComponent),
                                              data: data))
                    }
                }
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
