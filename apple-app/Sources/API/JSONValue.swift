import Foundation

/// 任意 JSON 值（工具参数、activity 等松散字段的容器）。
enum JSONValue: Codable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else {
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "无法识别的 JSON 值")
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }

    /// 单行紧凑表示（审批卡片/工具行展示用），超长截断。
    func compact(max: Int = 200) -> String {
        let s: String
        switch self {
        case .string(let v): s = v
        case .number(let v): s = v == v.rounded() ? String(Int(v)) : String(v)
        case .bool(let v): s = v ? "true" : "false"
        case .null: s = "null"
        case .array, .object:
            if let data = try? JSONEncoder().encode(self), let str = String(data: data, encoding: .utf8) {
                s = str
            } else { s = "…" }
        }
        return s.count > max ? String(s.prefix(max)) + "…" : s
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    func compact(max: Int = 200) -> String {
        JSONValue.object(self).compact(max: max)
    }
}
