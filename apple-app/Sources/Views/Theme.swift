import SwiftUI

/// 设计令牌单一来源（R5-7 轻改版）。各视图**只引用不自造**——散落的
/// `Color.primary.opacity(0.04)` 这类手调值一律收编到这里，改主题只动本文件。
///
/// 性格：本地、私密、工作台——安静、有工程质感，但不冷。
/// 主色「青瓷」刻意避开系统蓝：自定义状态（选中/运行中）一眼可辨是 app 的
/// 意志而非系统默认；深浅两档分别调过对比度（浅底深青、深底亮青）。
enum Theme {

    // MARK: - 色彩

    /// 主色·青瓷：交互/选中/运行中/链接
    static let accent = dynamic(light: (23, 122, 122), dark: (102, 196, 196))
    /// 思考（推理过程）：紫罗兰——与正文/工具都拉开
    static let thoughts = dynamic(light: (110, 92, 176), dark: (169, 155, 224))
    /// 成功：苔绿（比系统绿沉一档，不抢主色）
    static let success = dynamic(light: (47, 138, 92), dark: (95, 197, 140))
    /// 警示：琥珀
    static let warning = dynamic(light: (176, 116, 32), dark: (232, 169, 79))
    /// 错误：沿用系统红（错误就该像系统级错误）
    static let danger = Color.red

    /// 窗口底：浅色暖纸白 / 深色碳灰
    static let canvas = dynamic(light: (250, 250, 248), dark: (28, 29, 31))
    /// 卡片面：浅色纯白 / 深色浮起一档
    static let card = dynamic(light: (255, 255, 255), dark: (38, 40, 43))
    /// 淡染面（工具灰框/代码底）：主文字的 4~6%
    static let wash = Color.primary.opacity(0.045)
    /// 发丝线（卡片描边）
    static let hairline = Color.primary.opacity(0.08)

    /// 用户气泡：主色 12% 染
    static let userBubble = accent.opacity(0.12)
    /// 思考块底：紫罗兰 8% 染
    static let thoughtsWash = thoughts.opacity(0.08)

    // MARK: - 几何（4pt 网格）

    /// 气泡圆角
    static let radiusBubble: CGFloat = 12
    /// 卡片圆角
    static let radiusCard: CGFloat = 8
    /// 小件圆角（工具行/徽标）
    static let radiusSmall: CGFloat = 6

    static let s1: CGFloat = 4
    static let s2: CGFloat = 8
    static let s3: CGFloat = 12
    static let s4: CGFloat = 16

    // MARK: - 私有

    private static func dynamic(light: (Int, Int, Int), dark: (Int, Int, Int)) -> Color {
        #if os(macOS)
        return Color(NSColor(name: nil) { appearance in
            let c = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat(c.0)/255, green: CGFloat(c.1)/255,
                           blue: CGFloat(c.2)/255, alpha: 1)
        })
        #else
        return Color(UIColor { traits in
            let c = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: CGFloat(c.0)/255, green: CGFloat(c.1)/255,
                           blue: CGFloat(c.2)/255, alpha: 1)
        })
        #endif
    }
}
