import SwiftUI

/// Deck 查看器：原生复刻 web 端 slide-renderer 的几何——1280×720 设计坐标、
/// 12×12 网格（padding 56 / gap 8）、role 字号表、主题色 tokens。渲染的是同一份
/// deck.json，保证与 web 端视觉一致（字体族除外，用系统字体近似）。
struct DeckViewer: View {
    let deck: DeckSpec
    let sessionId: String
    @Environment(\.dismiss) private var dismiss
    @State private var index = 0

    var body: some View {
        NavigationStack {
            VStack(spacing: 8) {
                GeometryReader { geo in
                    let scale = min(geo.size.width / SlideGeometry.designW,
                                    geo.size.height / SlideGeometry.designH)
                    SlideCanvasView(slide: deck.slides[index], meta: deck.meta, sessionId: sessionId)
                        .frame(width: SlideGeometry.designW, height: SlideGeometry.designH)
                        .scaleEffect(scale)
                        .frame(width: geo.size.width, height: geo.size.height)
                        .clipped()
                }

                if let notes = deck.slides[index].notes, !notes.isEmpty {
                    Text(notes)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                        .padding(.horizontal)
                }

                HStack(spacing: 24) {
                    Button {
                        if index > 0 { index -= 1 }
                    } label: {
                        Image(systemName: "chevron.left")
                    }
                    .disabled(index == 0)
                    Text("\(index + 1) / \(deck.slides.count)")
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                    Button {
                        if index < deck.slides.count - 1 { index += 1 }
                    } label: {
                        Image(systemName: "chevron.right")
                    }
                    .disabled(index == deck.slides.count - 1)
                }
                .padding(.bottom, 8)
            }
            .navigationTitle(deck.meta.title)
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(L("关闭")) { dismiss() }
                }
            }
            .gesture(
                DragGesture(minimumDistance: 40).onEnded { value in
                    if value.translation.width < -40, index < deck.slides.count - 1 { index += 1 }
                    if value.translation.width > 40, index > 0 { index -= 1 }
                }
            )
        }
        #if os(macOS)
        .frame(minWidth: 760, minHeight: 520)
        #endif
    }
}

/// 设计坐标系常量与网格换算（与 web 端 CSS grid 逐像素对齐）。
enum SlideGeometry {
    static let designW: CGFloat = 1280
    static let designH: CGFloat = 720
    static let padding: CGFloat = 56
    static let gap: CGFloat = 8
    static let cols = 12
    static let rows = 12

    static var cellW: CGFloat {
        (designW - 2 * padding - CGFloat(cols - 1) * gap) / CGFloat(cols)
    }
    static var cellH: CGFloat {
        (designH - 2 * padding - CGFloat(rows - 1) * gap) / CGFloat(rows)
    }

    static func frame(for grid: DeckGrid) -> CGRect {
        let x = padding + CGFloat(grid.col - 1) * (cellW + gap)
        let y = padding + CGFloat(grid.row - 1) * (cellH + gap)
        let w = CGFloat(grid.colSpan) * cellW + CGFloat(grid.colSpan - 1) * gap
        let h = CGFloat(grid.rowSpan) * cellH + CGFloat(grid.rowSpan - 1) * gap
        return CGRect(x: x, y: y, width: w, height: h)
    }
}

/// 单页幻灯片（固定 1280×720，外层负责缩放）。
struct SlideCanvasView: View {
    let slide: DeckSlide
    let meta: DeckMeta
    let sessionId: String

    private var palette: DeckPalette { meta.tokens.palette }
    private var isSection: Bool { slide.layout == "section" || slide.layout == "title" }
    private var centered: Bool { ["title", "section", "quote"].contains(slide.layout) }

    var body: some View {
        ZStack(alignment: .topLeading) {
            if isSection {
                LinearGradient(colors: [Color(deckHex: palette.bg), Color(deckHex: palette.surface)],
                               startPoint: .top, endPoint: .bottomTrailing)
            } else {
                Color(deckHex: palette.bg)
            }

            ForEach(slide.blocks) { block in
                let rect = SlideGeometry.frame(for: block.grid)
                DeckBlockView(block: block, palette: palette, centered: centered, sessionId: sessionId)
                    .frame(width: rect.width, height: rect.height)
                    .clipped()
                    .offset(x: rect.minX, y: rect.minY)
            }

            if isSection {
                LinearGradient(colors: [Color(deckHex: palette.primary), Color(deckHex: palette.accent)],
                               startPoint: .leading, endPoint: .trailing)
                    .frame(width: SlideGeometry.designW, height: 10)
                    .offset(y: SlideGeometry.designH - 10)
            }
        }
        .frame(width: SlideGeometry.designW, height: SlideGeometry.designH)
    }
}

/// 单个内容块：text / chart / image / icon，role 字号表与 web 端一致。
struct DeckBlockView: View {
    let block: DeckBlock
    let palette: DeckPalette
    let centered: Bool
    let sessionId: String

    var body: some View {
        switch block.type {
        case "text":
            textView
        case "chart", "image":
            if let asset = block.content.asset {
                AssetImage(path: "/api/v1/files/\(sessionId)/assets/\(asset)", maxWidth: nil)
            } else if block.type == "chart" {
                placeholder(text: "chart pending…", dashed: false)
            } else {
                placeholder(text: block.content.alt ?? "image",
                            detail: block.visualRequest?.prompt, dashed: true)
            }
        case "icon":
            HStack {
                Image(systemName: Self.sfSymbol(forLucide: block.content.name ?? ""))
                    .font(.system(size: 40))
                    .foregroundStyle(Color(deckHex: palette.accent))
                Spacer(minLength: 0)
            }
        default:
            EmptyView()
        }
    }

    // role → (字号, 字重, 斜体)：镜像 web 端 ROLE_STYLE
    private var roleStyle: (size: CGFloat, weight: Font.Weight, italic: Bool) {
        switch block.role {
        case "title": return (44, .bold, false)
        case "subtitle": return (26, .regular, false)
        case "caption": return (16, .regular, false)
        case "emphasis": return (34, .medium, true)
        default: return (22, .regular, false)  // body
        }
    }

    private var textColor: Color {
        switch block.role {
        case "title": return Color(deckHex: palette.primary)
        case "caption", "subtitle": return Color(deckHex: palette.muted)
        default: return Color(deckHex: palette.fg)
        }
    }

    @ViewBuilder
    private var textView: some View {
        let style = roleStyle
        let font = Font.system(size: style.size, weight: style.weight)
        Group {
            if let bullets = block.content.bullets, !bullets.isEmpty {
                VStack(alignment: .leading, spacing: style.size * 0.55) {
                    ForEach(Array(bullets.enumerated()), id: \.offset) { _, bullet in
                        HStack(alignment: .top, spacing: 10) {
                            Text("•")
                            Text(bullet)
                        }
                    }
                }
                .font(style.italic ? font.italic() : font)
                .foregroundStyle(textColor)
                .frame(maxWidth: .infinity, maxHeight: .infinity,
                       alignment: centered ? .center : .topLeading)
            } else {
                Text(block.content.text ?? "")
                    .font(style.italic ? font.italic() : font)
                    .foregroundStyle(textColor)
                    .multilineTextAlignment(centered ? .center : .leading)
                    .frame(maxWidth: .infinity, maxHeight: .infinity,
                           alignment: centered ? .center : .topLeading)
            }
        }
    }

    private func placeholder(text: String, detail: String? = nil, dashed: Bool) -> some View {
        VStack(spacing: 10) {
            if dashed {
                Image(systemName: "photo")
                    .font(.system(size: 44, weight: .light))
            }
            Text(text).font(.system(size: 17, weight: .medium))
            if let detail {
                Text(detail)
                    .font(.system(size: 13))
                    .opacity(0.75)
                    .frame(maxWidth: 400)
            }
        }
        .foregroundStyle(Color(deckHex: palette.muted))
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(Color(deckHex: palette.surface))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(Color(deckHex: palette.muted).opacity(0.33),
                        style: StrokeStyle(lineWidth: 2, dash: dashed ? [8, 6] : []))
        )
    }

    /// lucide 图标名 → SF Symbol 近似映射；未收录的降级为圆点（web 端同样有降级）。
    static func sfSymbol(forLucide name: String) -> String {
        switch name {
        case "bar-chart", "bar-chart-2", "bar-chart-3": return "chart.bar"
        case "trending-up": return "chart.line.uptrend.xyaxis"
        case "trending-down": return "chart.line.downtrend.xyaxis"
        case "check", "check-circle": return "checkmark.circle"
        case "alert-triangle": return "exclamationmark.triangle"
        case "lightbulb": return "lightbulb"
        case "users", "user": return "person.2"
        case "clock": return "clock"
        case "zap": return "bolt"
        case "shield": return "shield"
        case "globe": return "globe"
        case "star": return "star"
        case "heart": return "heart"
        case "settings": return "gearshape"
        case "book", "book-open": return "book"
        case "rocket": return "paperplane"
        case "target": return "scope"
        case "search": return "magnifyingglass"
        case "database": return "cylinder.split.1x2"
        case "cpu": return "cpu"
        case "brain": return "brain"
        default: return "circle.fill"
        }
    }
}

extension Color {
    /// deck palette 的 hex 颜色（"#RGB" / "#RRGGBB" / "#RRGGBBAA"），解析失败灰色兜底。
    init(deckHex hex: String) {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        if s.count == 3 { s = s.map { "\($0)\($0)" }.joined() }
        guard s.count == 6 || s.count == 8, let value = UInt64(s, radix: 16) else {
            self = .gray
            return
        }
        let hasAlpha = s.count == 8
        let r = Double((value >> (hasAlpha ? 24 : 16)) & 0xFF) / 255
        let g = Double((value >> (hasAlpha ? 16 : 8)) & 0xFF) / 255
        let b = Double((value >> (hasAlpha ? 8 : 0)) & 0xFF) / 255
        let a = hasAlpha ? Double(value & 0xFF) / 255 : 1
        self = Color(red: r, green: g, blue: b, opacity: a)
    }
}
