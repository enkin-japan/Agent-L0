import AppKit
import Foundation
import MarkdownUI
import ObjectiveC
import SwiftUI

/// 全部 URL 都被进程内 protocol 截获，包含默认 loader 的 positive control。
final class MarkdownImageProbe: URLProtocol {
    static let lock = NSLock()
    private static var seen: [URL] = []
    static var requests: [URL] {
        lock.lock(); defer { lock.unlock() }
        return seen
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); Self.seen.append(request.url!); Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Self.png)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
    static let png = Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=")!
}

extension URLSessionConfiguration {
    @objc class func markdownImageProbeConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MarkdownImageProbe.self]
        return configuration
    }
}

// 仅替换块级资产 View 的产品依赖；实际 policy/provider/MarkdownUI 都参与编译。
struct AssetImage: View {
    let path: String
    var body: some View {
        Image(nsImage: NSImage(data: MarkdownImageProbe.png)!)
            .task {
                let session = URLSession(configuration: .default)
                defer { session.invalidateAndCancel() }
                _ = try? await session.data(from: URL(string: path, relativeTo: URL(string: "https://asset.invalid")!)!)
            }
    }
}

final class AgentStore: ObservableObject {
    let client: MarkdownAssetTestClient? = MarkdownAssetTestClient()
}

struct MarkdownAssetTestClient {
    func fetchAsset(path: String) async throws -> Data {
        let session = URLSession(configuration: .default)
        defer { session.invalidateAndCancel() }
        return try await session.data(from: URL(string: path, relativeTo: URL(string: "https://asset.invalid")!)!).0
    }
}

extension View {
    func selectableOnIOS() -> some View { self }
}

@main struct MarkdownImagePolicyTest {
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        if !condition() {
            print("FAIL: \(message); intercepted_requests=\(MarkdownImageProbe.requests.count)")
            exit(1)
        }
    }

    static func main() async throws {
        L10n.current = .zh
        method_exchangeImplementations(
            class_getClassMethod(URLSessionConfiguration.self, #selector(getter: URLSessionConfiguration.default))!,
            class_getClassMethod(URLSessionConfiguration.self, #selector(URLSessionConfiguration.markdownImageProbeConfiguration))!)
        defer {
            method_exchangeImplementations(
                class_getClassMethod(URLSessionConfiguration.self, #selector(getter: URLSessionConfiguration.default))!,
                class_getClassMethod(URLSessionConfiguration.self, #selector(URLSessionConfiguration.markdownImageProbeConfiguration))!)
        }

        let remote = URL(string: "https://image.invalid/synthetic-context.png")!
        _ = try await DefaultInlineImageProvider.default.image(with: remote, label: "positive control")
        expect(MarkdownImageProbe.requests.count == 1, "fake transport detects default renderer request")
        print("PASS: default provider positive control captured one remote request locally")
        if CommandLine.arguments.contains("--baseline") {
            print("FAIL: old default provider violates zero automatic remote requests")
            exit(1)
        }

        let asset = "/api/v1/files/synthetic-session/assets/chart.png"
        expect(MarkdownImagePolicy.assetPath(for: URL(string: asset)) == asset, "session asset allowed")
        for denied in [remote.absoluteString, "http://127.0.0.1/secret", "//image.invalid" + asset,
                       "https://image.invalid" + asset, "file:///tmp/image.png", "data:image/png;base64,AAAA",
                       asset + "?context=synthetic", asset + "#fragment", "/api/v1/files/s/assets/../secret",
                       "/api/v1/files/s/assets/%2Fsecret", "/api/v1/files/../assets/chart.png",
                       "/api/v1/files/s/assets/%2e%2e", "/api/v1/files/s/assets/%5Csecret", "chart.png"] {
            expect(MarkdownImagePolicy.assetPath(for: URL(string: denied)) == nil, "denied URL: \(denied)")
        }
        expect(MarkdownImagePolicy.assetPath(for: URL(string: asset, relativeTo: remote)) == nil, "base URL rejected")
        print("PASS: asset URL scheme/host/path/query/fragment boundaries")

        let destination = "https://destination.invalid/"
        for markdown in ["[![logo](\(remote))](\(destination))",
                         "![![logo](\(remote))](\(destination))",
                         "[**![logo](\(remote))**](\(destination))",
                         "[![logo][image]][target]\n\n[image]: \(remote)\n[target]: \(destination)",
                         "[![](\(remote))](\(destination))"] {
            let html = MarkdownContent(MarkdownImagePolicy.linksForExternalImages(markdown)).renderHTML()
            expect(html.contains("href=\"\(destination)\""), "linked image keeps exact outer destination: \(html)")
            expect(!html.contains("href=\"\(remote)\""), "linked image must not replace outer destination: \(html)")
            expect(html.components(separatedBy: "<a href=").count == 2, "linked image yields one link")
            expect(!html.contains("<img"), "linked external image yields text")
            expect(html.contains(markdown.contains("![]") ? "查看图片" : "logo"), "linked image retains visible alt")
        }
        let ordinaryHTML = MarkdownContent(MarkdownImagePolicy.linksForExternalImages("![logo](\(remote))")).renderHTML()
        expect(ordinaryHTML.contains("href=\"\(remote)\""), "ordinary external image keeps exact image URL link")
        let linkedAssetHTML = MarkdownContent(MarkdownImagePolicy.linksForExternalImages("[![chart](\(asset))](\(destination))")).renderHTML()
        expect(linkedAssetHTML.contains("href=\"\(destination)\"") && linkedAssetHTML.contains("<img"),
               "linked legitimate asset retains outer link and image")
        print("PASS: exact outer link destinations preserved through cmark serialize and MarkdownUI reparse")

        for markdown in ["![remote](\(remote))", "before ![remote](\(remote)) after",
                         "![remote][tracking]\n\n[tracking]: \(remote)",
                         "before ![remote][] after\n\n[remote]: \(remote)",
                         "![remote]\n\n[remote]: \(remote)",
                         "> ![remote](\(remote))", "- ![remote](\(remote))",
                         "[![remote](\(remote))](https://link.invalid/)",
                         "![](\(remote))", "![remote](<\(remote)> \"title\")"] {
            let sanitized = MarkdownImagePolicy.linksForExternalImages(markdown)
            let html = MarkdownContent(sanitized).renderHTML()
            expect(!html.contains("<img"), "external image downgraded: \(markdown)")
            expect(html.contains("<a href="), "external image retains link: \(markdown)")
        }
        for markdown in ["![chart](\(asset))", "before ![chart](\(asset)) after",
                         "![chart][asset]\n\n[asset]: \(asset)"] {
            expect(MarkdownContent(MarkdownImagePolicy.linksForExternalImages(markdown)).renderHTML().contains("<img"),
                   "legitimate asset remains image")
        }
        let code = "`![remote](\(remote))`\n\n```swift\n![remote](\(remote))\n```"
        let codeHTML = MarkdownContent(MarkdownImagePolicy.linksForExternalImages(code)).renderHTML()
        expect(codeHTML.contains("<code>![remote]"), "inline code untouched")
        expect(!codeHTML.contains("<a href="), "code block untouched")
        let table = "| a | b |\n|---|---|\n| ~~one~~ | ![remote](\(remote)) |"
        let tableHTML = MarkdownContent(MarkdownImagePolicy.linksForExternalImages(table)).renderHTML()
        expect(tableHTML.contains("<table>") && tableHTML.contains("<del>"), "GFM extensions preserved")
        print("PASS: block/inline/reference/nested/empty-alt image links; assets/code/GFM preserved")

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MarkdownImageProbe.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let provider = SessionAssetInlineImageProvider { path in
            // 使用真实的 URLSession 假传输：移除 provider 的 guard 后会观察到请求。
            try await session.data(from: URL(string: path, relativeTo: URL(string: "https://asset.invalid")!)!).0
        }
        let before = MarkdownImageProbe.requests.count
        for url in [remote, URL(string: "https://image.invalid" + asset)!, URL(string: "file:///tmp/image.png")!] {
            do {
                _ = try await provider.image(with: url, label: "denied")
                expect(false, "denied provider URL unexpectedly loaded")
            } catch {}
        }
        expect(MarkdownImageProbe.requests.count == before, "denied images produce zero transport requests")
        _ = try await provider.image(with: URL(string: asset)!, label: "chart")
        expect(MarkdownImageProbe.requests.count == before + 1, "allowed asset loaded through injected transport")
        expect(MarkdownImageProbe.requests.last?.path == asset, "asset request keeps whitelisted path")
        print("PASS: inline provider blocks remote requests and decodes allowed asset via fake transport")

        // Block provider has no async/network entry; constructing both branches covers URL policy dispatch.
        _ = SessionAssetImageProvider().makeImage(url: remote)
        _ = SessionAssetImageProvider().makeImage(url: URL(string: asset))
        expect(MarkdownImageProbe.requests.count == before + 1, "block provider construction makes no network request")
        print("PASS: block provider dispatch makes no automatic request")
        try await hostedProviderChecks(remote: remote, asset: asset)
    }

    @MainActor static func hostedProviderChecks(remote: URL, asset: String) async throws {
        // 隐藏测试窗口仅用于让 SwiftUI 启动真实的 renderer .task；不展示 UI 或访问真实后端。
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 300),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        defer { window.contentView = nil }
        let initial = MarkdownImageProbe.requests.count
        let defaultView = NSHostingView(rootView: Markdown("![remote](\(remote.absoluteString)?block-control)"))
        window.contentView = defaultView
        defaultView.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 300_000_000)
        expect(MarkdownImageProbe.requests.count == initial + 1, "hosted default block provider positive control")
        print("PASS: hosted default block renderer requested one image via local protocol")

        let protectedView = NSHostingView(rootView: Markdown("![remote](\(remote.absoluteString)?protected-block)")
            .markdownImageProvider(SessionAssetImageProvider()))
        window.contentView = protectedView
        protectedView.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 300_000_000)
        expect(MarkdownImageProbe.requests.count == initial + 1, "hosted protected block provider blocks automatic request")

        let assetView = NSHostingView(rootView: Markdown("![chart](\(asset))")
            .markdownImageProvider(SessionAssetImageProvider()))
        window.contentView = assetView
        assetView.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 300_000_000)
        expect(MarkdownImageProbe.requests.count == initial + 2, "hosted asset image branch still loads")
        print("PASS: hosted protected block renderer denies remote and dispatches asset view")

        let store = AgentStore()
        let rendererStart = MarkdownImageProbe.requests.count
        for streaming in [false, true] {
            for markdown in ["![remote](\(remote))", "before ![remote](\(remote)) after",
                             "![remote][ref]\n\n[ref]: \(remote)",
                             "before ![remote][ref] after\n\n[ref]: \(remote)",
                             "[**![logo](\(remote))**](https://destination.invalid/)"] {
                let hosted = NSHostingView(rootView: MarkdownText(text: markdown, streaming: streaming)
                    .environmentObject(store))
                window.contentView = hosted
                hosted.layoutSubtreeIfNeeded()
                try await Task.sleep(nanoseconds: 100_000_000)
                expect(MarkdownImageProbe.requests.count == rendererStart, "MarkdownText blocks external image while streaming=\(streaming)")
            }
        }
        for markdown in ["![chart](\(asset))", "before ![chart](\(asset)) after",
                         "![chart][ref]\n\n[ref]: \(asset)"] {
            let before = MarkdownImageProbe.requests.count
            let hosted = NSHostingView(rootView: MarkdownText(text: markdown).environmentObject(store))
            window.contentView = hosted
            hosted.layoutSubtreeIfNeeded()
            try await Task.sleep(nanoseconds: 100_000_000)
            expect(MarkdownImageProbe.requests.count == before + 1, "MarkdownText loads valid asset")
        }
        print("PASS: actual MarkdownText hosted block/inline/reference/streaming deny all remote requests; assets load")
    }
}
