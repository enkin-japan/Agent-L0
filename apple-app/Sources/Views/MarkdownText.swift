import MarkdownUI
import SwiftUI

/// 散文使用 MarkdownUI 的 GFM 渲染；图片始终经过会话资产白名单。
/// 直播代码只使用可换行 Text，避免向固定高度直播窗引入 AppKit 滚动宿主。
struct MarkdownText: View {
    let text: String
    @EnvironmentObject private var store: AgentStore
    #if os(iOS)
    @ScaledMetric(relativeTo: .body) private var bodySize: CGFloat = 17
    #else
    @ScaledMetric(relativeTo: .body) private var bodySize: CGFloat = 15
    #endif
    var streaming = false

    var body: some View {
        // Keep one GFM parse: cmark owns fences, indentation and list context.
        Markdown(MarkdownImagePolicy.linksForExternalImages(text))
            .markdownTheme(readingTheme)
            .markdownImageProvider(SessionAssetImageProvider())
            .markdownInlineImageProvider(SessionAssetInlineImageProvider { path in
                guard let client = store.client else { throw URLError(.notConnectedToInternet) }
                return try await client.fetchAsset(path: path)
            })
            .markdownTextStyle { FontSize(bodySize) }
            .selectableOnIOS()
            .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
            .tint(Theme.accent)
    }

    private var readingTheme: MarkdownUI.Theme {
        MarkdownUI.Theme()
            .text { ForegroundColor(Theme.text) }
            .strong { FontWeight(.semibold) }
            .link { ForegroundColor(Theme.accent); UnderlineStyle(.single) }
            .code {
                FontFamilyVariant(.monospaced)
                FontSize(.em(0.92))
                ForegroundColor(Theme.selectionText)
                BackgroundColor(Theme.quotation)
            }
            .heading1 { configuration in
                VStack(alignment: .leading, spacing: Theme.s3) {
                    configuration.label
                        .markdownTextStyle { FontSize(.em(1.7)); FontWeight(.semibold) }
                        .relativeLineSpacing(.em(0.12))
                    HStack(spacing: 0) {
                        Capsule().fill(Theme.accent).frame(width: 32, height: 3)
                        Rectangle().fill(Theme.separator).frame(height: 1)
                    }
                    .accessibilityHidden(true)
                }
                .markdownMargin(top: .em(1.3), bottom: .em(0.8))
            }
            .heading2 { configuration in
                configuration.label
                    .markdownTextStyle { FontSize(.em(1.35)); FontWeight(.semibold) }
                    .relativeLineSpacing(.em(0.15))
                    .padding(.leading, Theme.s3)
                    .overlay(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 2)
                            .fill(Theme.accent).frame(width: 3)
                            .accessibilityHidden(true)
                    }
                    .markdownMargin(top: .em(1.25), bottom: .em(0.55))
            }
            .heading3 { configuration in
                configuration.label
                    .markdownTextStyle {
                        FontSize(.em(1.12)); FontWeight(.semibold)
                        ForegroundColor(Theme.selectionText)
                    }
                    .markdownMargin(top: .em(1), bottom: .em(0.45))
            }
            .heading4 { configuration in
                configuration.label
                    .markdownTextStyle { FontWeight(.semibold) }
                    .markdownMargin(top: .em(0.9), bottom: .em(0.4))
            }
            .heading5 { configuration in
                configuration.label
                    .markdownTextStyle { FontWeight(.semibold) }
                    .markdownMargin(top: .em(0.9), bottom: .em(0.4))
            }
            .heading6 { configuration in
                configuration.label
                    .markdownTextStyle { FontWeight(.semibold); ForegroundColor(.secondary) }
                    .markdownMargin(top: .em(0.9), bottom: .em(0.4))
            }
            .paragraph { configuration in
                configuration.label
                    .fixedSize(horizontal: false, vertical: true)
                    .relativeLineSpacing(.em(0.32))
                    .markdownMargin(top: .em(0), bottom: .em(0.9))
            }
            .list { configuration in
                configuration.label.markdownMargin(top: .em(0.2), bottom: .em(0.9))
            }
            .listItem { configuration in
                configuration.label.markdownMargin(top: .em(0.3), bottom: .em(0.3))
            }
            .bulletedListMarker { configuration in
                Text(configuration.listLevel == 1 ? "•" : configuration.listLevel == 2 ? "◦" : "▪")
                    .foregroundStyle(Theme.accent)
                    .relativeFrame(minWidth: .em(1.5), alignment: .trailing)
            }
            .numberedListMarker { configuration in
                Text("\(configuration.itemNumber)")
                    .monospacedDigit()
                    .markdownTextStyle { FontSize(.em(0.85)); FontWeight(.semibold) }
                    .foregroundStyle(Theme.selectionText)
                    .relativeFrame(minWidth: .em(1.5))
                    .relativePadding(.vertical, length: .em(0.15))
                    .background(Theme.selection, in: RoundedRectangle(cornerRadius: 5))
            }
            .taskListMarker { configuration in
                Image(systemName: configuration.isCompleted ? "checkmark.square.fill" : "square")
                    .foregroundStyle(Theme.accent)
                    .imageScale(.small)
                    .relativeFrame(minWidth: .em(1.5), alignment: .trailing)
            }
            .blockquote { configuration in
                configuration.label
                    .markdownTextStyle { ForegroundColor(Theme.secondaryText) }
                    .padding(.horizontal, Theme.s4)
                    .padding(.vertical, Theme.s3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.quotation, in: RoundedRectangle(cornerRadius: Theme.radiusSmall))
                    .overlay(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 2).fill(Theme.accent).frame(width: 3)
                    }
                    .markdownMargin(top: .em(0.3), bottom: .em(0.9))
            }
            .codeBlock { configuration in
                // 嵌套列表/引用中的代码也沿用同一张卡和直播保护。
                MarkdownCodeCard(code: configuration.content, language: configuration.language,
                                 streaming: streaming)
                    .markdownMargin(top: .em(0.3), bottom: .em(0.9))
            }
            .table { configuration in
                configuration.label
                    .fixedSize(horizontal: false, vertical: true)
                    .markdownTableBorderStyle(.init(color: Theme.separator))
                    .markdownTableBackgroundStyle(.alternatingRows(Theme.card, Theme.codeSurface,
                                                                   header: Theme.selection))
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
                    .overlay(RoundedRectangle(cornerRadius: Theme.radiusSmall)
                        .strokeBorder(Theme.separator, lineWidth: 1))
                    .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                    .markdownMargin(top: .em(0.3), bottom: .em(1))
            }
            .tableCell { configuration in
                configuration.label
                    .markdownTextStyle {
                        if configuration.row == 0 {
                            FontWeight(.semibold)
                            ForegroundColor(Theme.selectionText)
                        }
                        BackgroundColor(nil)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                    .relativeLineSpacing(.em(0.2))
                    .padding(Theme.s2)
            }
            .thematicBreak {
                HStack(spacing: Theme.s2) {
                    Capsule().fill(Theme.accentRule).frame(width: 24, height: 3)
                    Rectangle().fill(Theme.separator).frame(height: 1)
                }
                .accessibilityHidden(true)
                .markdownMargin(top: .em(1.4), bottom: .em(1.4))
            }
    }

}

/// MarkdownUI 的统一代码块卡片；直播时不创建滚动宿主。
private struct MarkdownCodeCard: View {
    let code: String
    let language: String?
    let streaming: Bool
    @State private var copied = false
    @State private var resetTask: Task<Void, Never>?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: Theme.s2) {
                Image(systemName: "chevron.left.forwardslash.chevron.right")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(Theme.accent)
                    .accessibilityHidden(true)
                Text(language.flatMap { $0.isEmpty ? nil : $0 } ?? L("代码"))
                    .font(.caption.monospaced().weight(.medium))
                    .lineLimit(1)
                Spacer(minLength: Theme.s2)
                Button {
                    #if os(macOS)
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                    #else
                    UIPasteboard.general.string = code
                    #endif
                    copied = true
                    resetTask?.cancel()
                    resetTask = Task {
                        try? await Task.sleep(nanoseconds: 1_500_000_000)
                        guard !Task.isCancelled else { return }
                        copied = false
                    }
                } label: {
                    Label(copied ? L("已复制") : L("复制代码"),
                          systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.caption)
                        .frame(minHeight: Theme.controlHitSize)
                        .padding(.horizontal, Theme.s2)
                        .contentShape(Rectangle())
                }
                .buttonStyle(CeladonControlStyle())
                .help(L("复制代码"))
                .foregroundStyle(Theme.accent)
            }
            .foregroundStyle(.secondary)
            .padding(.leading, Theme.s3)
            .padding(.trailing, Theme.s1)
            .background(Theme.quotation)
            Rectangle().fill(Theme.separator).frame(height: 1)
            if streaming {
                // 直播态：纯 Text 软换行，不创建滚动宿主。
                Text(code)
                    .font(.system(.callout, design: .monospaced))
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(Theme.s3)
                    .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    Text(code)
                        .font(.system(.callout, design: .monospaced))
                        .selectableOnIOS()
                        .padding(Theme.s3)
                }
            }
        }
        .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
        .background(Theme.codeSurface)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard)
            .strokeBorder(Theme.separator, lineWidth: 1))
        .onDisappear { resetTask?.cancel() }
    }
}
