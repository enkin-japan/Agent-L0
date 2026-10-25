import SwiftUI

/// 青瓷工作台设计令牌。各视图**只引用不自造**——散落的
/// `Color.primary.opacity(0.04)` 这类手调值一律收编到这里，改主题只动本文件。
///
/// 性格：本地、私密、工作台——安静、有工程质感，但不冷。
/// 主色「青瓷」刻意避开系统蓝：自定义状态（选中/运行中）一眼可辨是 app 的
/// 意志而非系统默认；深浅两档分别调过对比度（浅底深青、深底亮青）。
enum Theme {

    // MARK: - 色彩

    /// 主色·青瓷：交互/选中/运行中/链接
    static let accent = dynamic(light: (23, 122, 122), dark: (102, 196, 196))
    /// 主色实心按钮上的文字，深色模式使用深墨色保证对比度。
    static let accentInk = dynamic(light: (255, 255, 255), dark: (19, 41, 41))
    /// 思考（推理过程）：紫罗兰——与正文/工具都拉开
    static let thoughts = dynamic(light: (110, 92, 176), dark: (169, 155, 224))
    /// 成功：苔绿（比系统绿沉一档，不抢主色）
    static let success = dynamic(light: (47, 138, 92), dark: (95, 197, 140))
    /// 警示：琥珀
    static let warning = dynamic(light: (133, 91, 25), dark: (232, 189, 119))
    /// 错误：沿用系统红（错误就该像系统级错误）
    static let danger = Color.red

    /// 窗口底：浅色暖纸白 / 深色碳灰
    static let canvas = dynamic(light: (250, 250, 248), dark: (28, 31, 32))
    /// 卡片面：浅色纯白 / 深色浮起一档
    static let card = dynamic(light: (255, 255, 255), dark: (42, 48, 49))
    static let sidebar = dynamic(light: (241, 243, 240), dark: (36, 40, 41))
    static let inputSurface = card
    static let text = dynamic(light: (37, 45, 44), dark: (232, 236, 234))
    static let secondaryText = dynamic(light: (96, 108, 105), dark: (165, 178, 173))
    static let cardShadow = Color.black.opacity(0.045)
    static let readingWidth: CGFloat = 820
    #if os(macOS)
    static let readingFont = Font.system(size: 15)
    static let contentPadding: CGFloat = 28
    #else
    static let readingFont = Font.body
    static let contentPadding: CGFloat = 16
    #endif
    static let selection = dynamic(light: (225, 238, 234), dark: (42, 66, 66))
    static let selectionText = dynamic(light: (18, 108, 108), dark: (102, 196, 196))
    /// 悬停比选中更轻，按下再加深；不随窗口布局改变尺寸。
    static let controlHover = dynamic(light: (216, 233, 227), dark: (48, 73, 72))
    static let controlPressed = dynamic(light: (195, 220, 213), dark: (57, 88, 85))
    static let accentRule = accent.opacity(0.35)
    static let quotation = dynamic(light: (237, 244, 240), dark: (35, 49, 47))
    static let codeSurface = dynamic(light: (246, 248, 246), dark: (31, 37, 38))
    static let separator = dynamic(light: (220, 226, 222), dark: (59, 69, 66),
                                   highLight: (128, 140, 134), highDark: (142, 158, 150))
    /// 淡染面（工具灰框/代码底）：主文字的 4~6%
    static let wash = Color.primary.opacity(0.045)
    /// 发丝线（卡片描边）
    static let hairline = separator

    /// 用户气泡：主色 12% 染
    static let userBubble = selection
    /// 思考块底：紫罗兰 8% 染
    static let thoughtsWash = thoughts.opacity(0.08)

    // MARK: - 几何（4pt 网格）

    /// 气泡圆角
    static let radiusBubble: CGFloat = 16
    /// 卡片圆角
    static let radiusCard: CGFloat = 12
    /// 小件圆角（工具行/徽标）
    static let radiusSmall: CGFloat = 6
    static let radiusComposer: CGFloat = 16

    #if os(macOS)
    static let controlHitSize: CGFloat = 28
    #else
    static let controlHitSize: CGFloat = 44
    #endif

    static let s1: CGFloat = 4
    static let s2: CGFloat = 8
    static let s3: CGFloat = 12
    static let s4: CGFloat = 16
    static let s5: CGFloat = 24

    // MARK: - 私有

    private static func dynamic(light: (Int, Int, Int), dark: (Int, Int, Int),
                                highLight: (Int, Int, Int)? = nil,
                                highDark: (Int, Int, Int)? = nil) -> Color {
        #if os(macOS)
        return Color(NSColor(name: nil) { appearance in
            let match = appearance.bestMatch(from: [.aqua, .darkAqua,
                .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua])
            let c: (Int, Int, Int)
            switch match {
            case .accessibilityHighContrastAqua: c = highLight ?? light
            case .accessibilityHighContrastDarkAqua: c = highDark ?? dark
            case .darkAqua: c = dark
            default: c = light
            }
            return NSColor(srgbRed: CGFloat(c.0)/255, green: CGFloat(c.1)/255,
                           blue: CGFloat(c.2)/255, alpha: 1)
        })
        #else
        return Color(UIColor { traits in
            let isDark = traits.userInterfaceStyle == .dark
            let c = traits.accessibilityContrast == .high
                ? (isDark ? highDark ?? dark : highLight ?? light)
                : (isDark ? dark : light)
            return UIColor(red: CGFloat(c.0)/255, green: CGFloat(c.1)/255,
                           blue: CGFloat(c.2)/255, alpha: 1)
        })
        #endif
    }
}

/// Shared feedback for explicit buttons. iOS uses the same pressed state without
/// requiring hover; disabled controls never look active. Only color animates.
struct CeladonControlStyle: ButtonStyle {
    var selected = false
    var prominent = false
    var filled = false
    var cornerRadius: CGFloat = Theme.radiusSmall

    func makeBody(configuration: Configuration) -> some View {
        Control(configuration: configuration, selected: selected,
                prominent: prominent, filled: filled, cornerRadius: cornerRadius)
    }

    private struct Control: View {
        let configuration: ButtonStyle.Configuration
        let selected: Bool
        let prominent: Bool
        let filled: Bool
        let cornerRadius: CGFloat
        @Environment(\.isEnabled) private var enabled
        @Environment(\.accessibilityReduceMotion) private var reduceMotion
        @State private var hovering = false

        var body: some View {
            configuration.label
                .background(surface, in: RoundedRectangle(cornerRadius: cornerRadius))
                .overlay {
                    if filled && enabled && (hovering || configuration.isPressed) {
                        RoundedRectangle(cornerRadius: cornerRadius)
                            .fill(Theme.accentInk.opacity(configuration.isPressed ? 0.22 : 0.12))
                            .allowsHitTesting(false)
                    }
                    RoundedRectangle(cornerRadius: cornerRadius)
                        .strokeBorder(enabled && (hovering || configuration.isPressed)
                                      ? Theme.accentRule : (prominent ? Theme.separator : Color.clear), lineWidth: 1)
                        .allowsHitTesting(false)
                }
                .opacity(enabled ? 1 : 0.45)
                .contentShape(RoundedRectangle(cornerRadius: cornerRadius))
                .onHover { hovering = $0 }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.12), value: hovering)
                .animation(reduceMotion ? nil : .easeOut(duration: 0.08), value: configuration.isPressed)
        }

        private var surface: Color {
            guard enabled else { return prominent ? Theme.card : .clear }
            if configuration.isPressed { return Theme.controlPressed }
            if hovering { return Theme.controlHover }
            if selected { return Theme.selection }
            return prominent ? Theme.card : .clear
        }
    }
}

/// A visual-only surface for native List rows and Menu labels. No gesture takes
/// selection, keyboard navigation or context-menu ownership away from the host.
struct CeladonHoverSurface: ViewModifier {
    var selected = false
    var cornerRadius: CGFloat = 9
    @Environment(\.isEnabled) private var enabled
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false

    func body(content: Content) -> some View {
        content
            .background(selected ? Theme.selection : (hovering && enabled ? Theme.controlHover : .clear),
                        in: RoundedRectangle(cornerRadius: cornerRadius))
            .overlay {
                RoundedRectangle(cornerRadius: cornerRadius)
                    .strokeBorder(hovering && enabled ? Theme.accentRule : .clear, lineWidth: 1)
                    .allowsHitTesting(false)
            }
            .onHover { hovering = $0 }
            .animation(reduceMotion ? nil : .easeOut(duration: 0.12), value: hovering)
    }
}
