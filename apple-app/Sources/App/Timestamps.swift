import Foundation

/// 时间戳显示规则（用户定，2026-07-17）：
/// **当日内 → 时分秒；当日以外 7 天以内 → 周几；更早 → 年月日。**
/// 会话列表用 `listLabel`（纯档位）；消息气泡用 `messageLabel`（非当日档位后
/// 追加时分——会话内定位比只有「周三」有用）。
/// 周几随 app 语言（星期三 / 水曜日 / Wednesday），跟 L10n 同一分辨逻辑。
enum Timestamps {

    static func listLabel(_ date: Date, now: Date = Date()) -> String {
        switch tier(of: date, now: now) {
        case .today: return format(date, "HH:mm:ss")
        case .thisWeek: return format(date, "EEEE")
        case .older: return format(date, "yyyy-MM-dd")
        }
    }

    static func messageLabel(_ date: Date, now: Date = Date()) -> String {
        switch tier(of: date, now: now) {
        case .today: return format(date, "HH:mm:ss")
        case .thisWeek: return format(date, "EEEE HH:mm")
        case .older: return format(date, "yyyy-MM-dd HH:mm")
        }
    }

    // MARK: - 档位

    private enum Tier { case today, thisWeek, older }

    private static func tier(of date: Date, now: Date) -> Tier {
        let cal = Calendar.current
        if cal.isDate(date, inSameDayAs: now) { return .today }
        let days = cal.dateComponents([.day], from: cal.startOfDay(for: date),
                                      to: cal.startOfDay(for: now)).day ?? Int.max
        // 1..6 个自然日：周几不与今天同名，无歧义；≥7 天（或时钟偏差的未来日期）
        // 一律落到年月日
        return (1...6).contains(days) ? .thisWeek : .older
    }

    // MARK: - 格式化（formatter 造价高，按 格式+语言 缓存）

    private static var cache: [String: DateFormatter] = [:]

    private static func format(_ date: Date, _ fmt: String) -> String {
        let localeId: String
        switch L10n.resolved {
        case "ja": localeId = "ja_JP"
        case "en": localeId = "en_US"
        default: localeId = "zh_CN"
        }
        let key = "\(localeId)|\(fmt)"
        let formatter: DateFormatter
        if let hit = cache[key] {
            formatter = hit
        } else {
            let f = DateFormatter()
            f.locale = Locale(identifier: localeId)
            f.dateFormat = fmt
            cache[key] = f
            formatter = f
        }
        return formatter.string(from: date)
    }
}
