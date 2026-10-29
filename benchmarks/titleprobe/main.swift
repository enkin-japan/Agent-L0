import SwiftUI
import AppKit
let hideToolbar = CommandLine.arguments.contains("--hide-toolbar")
struct Root: View {
    var body: some View {
        GeometryReader { geo in
            VStack(alignment: .leading) {
                Text("safeTop=\(Int(geo.safeAreaInsets.top)) hideToolbar=\(hideToolbar ? "yes" : "no")").padding(.top, 40)
                Spacer()
            }.frame(maxWidth: .infinity, maxHeight: .infinity).background(Color(red: 0.12, green: 0.14, blue: 0.2))
        }
        .modifier(MaybeHide())
        .frame(minWidth: 420, minHeight: 240)
    }
}
struct MaybeHide: ViewModifier {
    func body(content: Content) -> some View {
        if hideToolbar { content.toolbar(.hidden, for: .windowToolbar) } else { content }
    }
}
final class Delegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.regular); NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            guard let w = NSApp.windows.first else { print("NOWINDOW"); exit(2) }
            let btns = [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton].map { t -> String in
                guard let b = w.standardWindowButton(t) else { return "nil" }
                return "hidden=\(b.isHidden) alpha=\(b.alphaValue) inWindow=\(b.window != nil) superHidden=\(b.superview?.isHidden ?? true) frame=\(b.frame)"
            }
            print("WINDOWID \(w.windowNumber) styleMask=\(w.styleMask.rawValue) titleVisibility=\(w.titleVisibility.rawValue) titlebarTransparent=\(w.titlebarAppearsTransparent) toolbar=\(w.toolbar != nil)")
            for (i, b) in btns.enumerated() { print("BTN\(i) \(b)") }
            fflush(stdout)
        }
    }
}
@main struct ProbeApp: App {
    @NSApplicationDelegateAdaptor(Delegate.self) var d
    var body: some Scene { WindowGroup("Probe") { Root() }.windowStyle(.hiddenTitleBar) }
}
