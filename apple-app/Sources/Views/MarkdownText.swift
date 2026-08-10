import MarkdownUI
import SwiftUI

/// Markdown 渲染：围栏代码块 → 等宽横滚块（fence 感知切分，避免资产正则误伤
/// 代码内容）；会话资产图片 → 带鉴权的 AssetImage（白名单与 web 端同口径）；
/// 其余散文段落 → MarkdownUI（GFM 块级全支持：标题/列表/表格/引用）。
struct MarkdownText: View {
    let text: String
    /// 流式直播态：代码块降级为纯 Text 不进横滚 ScrollView。macOS 上 ScrollView 桥接
    /// NSScrollView（AppKitPlatformViewHost），流式打字打进代码围栏时其内容尺寸逐次
    /// 增长，尺寸约束失效撞上 macOS 27 beta AppKit 约束阶段的重入执法 → NSException
    /// → SIGABRT（2026-08-10 崩溃报告实案）。轮末权威消息（streaming=false）仍走
    /// 完整渲染，横滚能力不损失。
    var streaming = false

    private enum Segment: Identifiable {
        case prose(String)
        case code(String)
        case asset(path: String)

        var id: String {
            switch self {
            case .prose(let s): return "p:\(s.hashValue)"
            case .code(let s): return "c:\(s.hashValue)"
            case .asset(let p): return "a:\(p)"
            }
        }
    }

    /// 会话资产白名单（与 web 端 markdown-content.tsx 同一正则口径）：
    /// 只有前缀锚定的 /api/v1/files/{sid}/assets/{file} 相对路径才渲染成图片，
    /// 其余图片语法降级为普通文本/链接。
    private static let assetPattern = /!\[[^\]]*\]\((\/api\/v1\/files\/[^\/]+\/assets\/[^)\/?#]+)\)/

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(segments) { segment in
                switch segment {
                case .prose(let prose):
                    Markdown(prose)
                        .markdownTextStyle {
                            FontSize(15)
                        }
                        .selectableOnIOS()
                case .code(let code):
                    if streaming {
                        // 直播态：不进 NSScrollView（见 streaming 注释），长行软换行
                        Text(code)
                            .font(.system(.callout, design: .monospaced))
                            .padding(10)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.primary.opacity(0.06))
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    } else {
                        ScrollView(.horizontal, showsIndicators: false) {
                            Text(code)
                                .font(.system(.callout, design: .monospaced))
                                .selectableOnIOS()
                                .padding(10)
                        }
                        .background(Color.primary.opacity(0.06))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                case .asset(let path):
                    AssetImage(path: path)
                }
            }
        }
    }

    /// 把一段文字里的白名单资产图片拆成独立段（图前文字 / 图 / 图后文字…）。
    private static func splitAssets(_ prose: String) -> [Segment] {
        var out: [Segment] = []
        var rest = Substring(prose)
        while let match = rest.firstMatch(of: assetPattern) {
            let before = rest[rest.startIndex..<match.range.lowerBound]
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !before.isEmpty { out.append(.prose(String(before))) }
            out.append(.asset(path: String(match.1)))
            rest = rest[match.range.upperBound...]
        }
        let tail = rest.trimmingCharacters(in: .whitespacesAndNewlines)
        if !tail.isEmpty { out.append(.prose(String(tail))) }
        return out
    }

    private var segments: [Segment] {
        var out: [Segment] = []
        var prose: [String] = []
        var code: [String] = []
        var inCode = false
        for line in text.components(separatedBy: "\n") {
            if line.trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                if inCode {
                    out.append(.code(code.joined(separator: "\n")))
                    code = []
                } else if !prose.isEmpty {
                    out.append(contentsOf: Self.splitAssets(prose.joined(separator: "\n")))
                    prose = []
                }
                inCode.toggle()
                continue
            }
            if inCode { code.append(line) } else { prose.append(line) }
        }
        if !code.isEmpty { out.append(.code(code.joined(separator: "\n"))) }
        if !prose.isEmpty { out.append(contentsOf: Self.splitAssets(prose.joined(separator: "\n"))) }
        return out
    }

}
