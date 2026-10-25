import AppKit
import SwiftUI

private struct PreviewOptions {
    var output = ""
    var appearance = "light"
    var scenario = "conversation"
    var width = 1120
    var height = 900

    init() throws {
        var args = Array(CommandLine.arguments.dropFirst())
        guard !args.isEmpty else { throw PreviewFailure.message("Missing output directory") }
        output = args.removeFirst()
        while !args.isEmpty {
            let flag = args.removeFirst()
            guard !args.isEmpty else { throw PreviewFailure.message("Missing value for \(flag)") }
            let value = args.removeFirst()
            switch flag {
            case "--appearance": appearance = value
            case "--scenario": scenario = value
            case "--width": width = Int(value) ?? 0
            case "--height": height = Int(value) ?? 0
            default: throw PreviewFailure.message("Unknown option \(flag)")
            }
        }
        guard ["light", "dark"].contains(appearance),
              ["conversation", "approval", "streaming", "welcome"].contains(scenario),
              (760...2560).contains(width), (520...1800).contains(height) else {
            throw PreviewFailure.message("Expected light|dark, conversation|approval|streaming|welcome, width 760...2560, height 520...1800")
        }
    }
}

private enum PreviewFailure: Error { case message(String) }

/// A renderer-only process must not deliver user input to production event monitors.
private final class PreviewApplication: NSApplication {
    private(set) var discardedInputEvents = 0

    override func sendEvent(_ event: NSEvent) {
        switch event.type {
        case .keyDown, .keyUp, .flagsChanged, .leftMouseDown, .leftMouseUp,
             .rightMouseDown, .rightMouseUp, .otherMouseDown, .otherMouseUp,
             .mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged,
             .scrollWheel, .gesture, .magnify, .swipe, .rotate, .pressure:
            discardedInputEvents += 1
        default:
            super.sendEvent(event)
        }
    }

    func verifyInputBoundary(window: NSWindow) throws -> Int {
        // F20 has no product binding and cannot invoke paste, even in the positive control.
        guard let probe = NSEvent.keyEvent(with: .keyDown, location: .zero,
            modifierFlags: [], timestamp: 0, windowNumber: window.windowNumber,
            context: nil, characters: "\u{F717}", charactersIgnoringModifiers: "\u{F717}",
            isARepeat: false, keyCode: 90) else { throw PreviewFailure.message("Cannot create synthetic input probe") }
        var delivered = 0
        let monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { _ in
            delivered += 1
            return nil
        }
        guard let monitor else { throw PreviewFailure.message("Cannot install synthetic input probe") }
        defer { NSEvent.removeMonitor(monitor) }
        // Positive control: ordinary AppKit dispatch really reaches a local monitor.
        super.sendEvent(probe)
        guard delivered == 1 else { throw PreviewFailure.message("Input probe positive control was not delivered") }
        let before = discardedInputEvents
        sendEvent(probe)
        guard delivered == 1, discardedInputEvents == before + 1, !window.isKeyWindow else {
            throw PreviewFailure.message("Preview input escaped the application boundary")
        }
        return delivered
    }
}

private final class PreviewWindow: NSWindow {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

@MainActor private final class PreviewDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow?
    var store: AgentStore?
    var watchdog: DispatchWorkItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let timeout = DispatchWorkItem { Self.fail("Preview exceeded 30 seconds") }
        watchdog = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: timeout)
        Task { @MainActor in
            do { try await render(PreviewOptions()) }
            catch { Self.fail(String(describing: error)) }
        }
    }

    private func render(_ options: PreviewOptions) async throws {
        guard Bundle.main.bundleIdentifier == "dev.enkin.macagent.design-preview" else {
            throw PreviewFailure.message("Refusing to run outside the isolated preview bundle")
        }
        // AgentStore intentionally keeps its real persistence code. Give it a clean,
        // test-only defaults domain each invocation; never access the product domain.
        UserDefaults.standard.removePersistentDomain(forName: "dev.enkin.macagent.design-preview")
        DesignPreviewTransport.scenario = options.scenario
        // Also intercept default URLSession transports if a real view creates one.
        guard URLProtocol.registerClass(DesignPreviewTransport.self) else {
            throw PreviewFailure.message("Could not install offline URLProtocol")
        }
        let settings = AppSettings()
        settings.appearance = options.appearance
        let store = AgentStore(settings: settings)
        self.store = store
        // Real support objects, without configure/bootstrap/autostart/start calls.
        let push = PushManager(settings: settings)
        let sidecar = SidecarManager()
        let retier = RetierMonitor(session: DesignPreviewTransport.session())
        await store.refreshConnection()
        await store.loadSessions()
        await store.refreshGlobalSnapshot()
        guard store.connectionOK == true, !store.sessions.isEmpty else {
            throw PreviewFailure.message("Synthetic session index did not load: \(store.errorMessage ?? "unknown")")
        }
        if options.scenario != "welcome" {
            store.select(sessionId: DesignPreviewFixtures.sessionID)
            try await waitForProjection(store, scenario: options.scenario)
        }
        let size = NSSize(width: CGFloat(options.width), height: CGFloat(options.height))
        let content = RootView()
            .environmentObject(store)
            .environmentObject(settings)
            .environmentObject(push)
            .environmentObject(sidecar)
            .environmentObject(retier)
            .preferredColorScheme(settings.colorScheme)
            .environment(\.controlActiveState, .key)
            .transaction { $0.disablesAnimations = true }
            .tint(Theme.accent)
            // Screenshot-only: do not expose execution, paste, microphone or system-link actions.
            .allowsHitTesting(false)
        let host = NSHostingView(rootView: content)
        host.frame = NSRect(origin: .zero, size: size)
        host.autoresizingMask = [.width, .height]
        let window = PreviewWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "MacAgent · Offline design preview"
        window.appearance = NSAppearance(named: options.appearance == "dark" ? .darkAqua : .aqua)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.setContentSize(size)
        window.center()
        window.orderFront(nil)
        self.window = window
        // Let real SwiftUI tasks/layout and the store's bounded stream flush settle.
        try await Task.sleep(for: .milliseconds(900))
        guard let app = NSApp as? PreviewApplication else {
            throw PreviewFailure.message("Missing preview input boundary")
        }
        let inputPositiveControl = try app.verifyInputBoundary(window: window)
        var resizeWidths: [Int] = []
        let streamBefore = store.streamText.count
        if options.scenario == "streaming" {
            // Exercise real AppKit resizing while the actual Store receives tokens.
            for (index, width) in [1120, 760, 960, 760, 1120, options.width].enumerated() {
                window.setContentSize(NSSize(width: CGFloat(width), height: size.height))
                DesignPreviewTransport.appendToken(seq: 31 + index,
                    text: "\n正在补充第 \(index + 1) 处路线说明，窗口宽度为 \(width)。")
                try await Task.sleep(for: .milliseconds(180))
                host.layoutSubtreeIfNeeded()
                guard abs(host.bounds.width - CGFloat(width)) < 1 else {
                    throw PreviewFailure.message("Resize did not reach requested width \(width): \(host.bounds.width)")
                }
                resizeWidths.append(Int(host.bounds.width))
            }
            guard store.streamText.count > streamBefore else {
                throw PreviewFailure.message("Streaming tokens did not reach the real Store")
            }
        }
        host.layoutSubtreeIfNeeded()
        if options.scenario == "approval", let scroll = scrollViews(in: host)
            .filter({ $0.documentView != nil && $0.bounds.width > 300 })
            .max(by: { $0.bounds.width * $0.bounds.height < $1.bounds.width * $1.bounds.height }),
           let document = scroll.documentView {
            let bottom = document.isFlipped ? max(0, document.bounds.height - scroll.contentView.bounds.height) : 0
            scroll.contentView.scroll(to: NSPoint(x: 0, y: bottom))
            scroll.reflectScrolledClipView(scroll.contentView)
            try await Task.sleep(for: .milliseconds(150))
        }
        host.displayIfNeeded()
        window.displayIfNeeded()
        guard store.errorMessage == nil else { throw PreviewFailure.message(store.errorMessage!) }
        guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
            throw PreviewFailure.message("NSHostingView could not allocate screenshot bitmap")
        }
        host.cacheDisplay(in: host.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            throw PreviewFailure.message("Screenshot PNG encoding failed")
        }
        let directory = URL(fileURLWithPath: options.output, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let stem = "\(options.scenario)-\(options.appearance)-\(options.width)x\(options.height)"
        let output = directory.appendingPathComponent(stem + ".png")
        try png.write(to: output)
        let counts = DesignPreviewTransport.counts
        let report: [String: Any] = ["bundle_id": Bundle.main.bundleIdentifier!, "scenario": options.scenario,
            "appearance": options.appearance, "width_points": options.width, "height_points": options.height,
            "width_pixels": bitmap.pixelsWide, "height_pixels": bitmap.pixelsHigh,
            "canonical_turns": store.canonicalTurns.count, "messages": store.messages.count,
            "approval_seq": store.pendingApprovalTarget.map { $0.seq as Any } ?? NSNull(),
            "intercepted_requests": counts.requests, "attached_streams": counts.streams,
            "input_positive_control_deliveries": inputPositiveControl,
            "discarded_input_events": app.discardedInputEvents, "window_is_key": window.isKeyWindow,
            "resize_widths": resizeWidths, "stream_characters_before": streamBefore,
            "stream_characters_after": store.streamText.count,
            "png": output.path, "finished": true]
        let json = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        try json.write(to: directory.appendingPathComponent(stem + ".json"))
        print(String(data: json, encoding: .utf8)!)
        watchdog?.cancel()
        window.orderOut(nil)
        // No app delegate/sidecar shutdown hook is installed. Only this process exits.
        exit(0)
    }

    private func waitForProjection(_ store: AgentStore, scenario: String) async throws {
        for _ in 0..<300 {
            let ready = store.canonicalTurns.count == 1 && !store.messages.isEmpty &&
                (scenario == "conversation" ||
                 (scenario == "streaming" && store.isGenerating && !store.streamText.isEmpty) ||
                 (scenario == "approval" && store.pendingApprovalTarget == PendingApprovalTarget(
                    sessionId: DesignPreviewFixtures.sessionID, turnId: DesignPreviewFixtures.turnID,
                    seq: DesignPreviewFixtures.approvalSeq)))
            if ready && DesignPreviewTransport.counts.streams > 0 { return }
            if let error = store.errorMessage { throw PreviewFailure.message(error) }
            try await Task.sleep(for: .milliseconds(10))
        }
        throw PreviewFailure.message("Timed out restoring real AgentStore projection for \(scenario)")
    }

    private func scrollViews(in view: NSView) -> [NSScrollView] {
        (view as? NSScrollView).map { [$0] } ?? view.subviews.flatMap { scrollViews(in: $0) }
    }

    static func fail(_ message: String) -> Never {
        FileHandle.standardError.write(Data(("FAIL design preview: " + message + "\n").utf8))
        exit(1)
    }
}

@main private enum DesignPreviewMain {
    @MainActor static func main() {
        let app = PreviewApplication.shared
        let delegate = PreviewDelegate()
        app.setActivationPolicy(.accessory)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
