import SwiftUI
import UniformTypeIdentifiers

/// 会话 → Markdown 转写（对齐 web 端 export.ts 的干净口径）：
/// 用户/助手正文全量保留；工具调用/返回折叠为一行引用；系统条折叠为引用；
/// 思维链不进导出。全保真需求走后端 history.jsonl。
enum ConversationExport {
    static func markdown(title: String, messages: [ChatMessage]) -> String {
        var lines: [String] = ["# \(title)", ""]
        let stamp = Date().formatted(date: .abbreviated, time: .shortened)
        lines.append("> 导出自 MacAgent · \(stamp) · \(messages.count) 条消息")
        lines.append("")
        for message in messages {
            switch message.kind {
            case .user:
                lines.append("## 🧑 用户")
                lines.append("")
                lines.append(message.text)
            case .assistant:
                lines.append("## 🤖 MacAgent")
                lines.append("")
                lines.append(message.text)
            case .tool:
                lines.append("> 🔧 \(message.toolLabel ?? "工具")")
            case .toolGroup:
                lines.append(contentsOf: message.toolSteps.map { step in
                    step.isNote ? "> 💬 中途说明：\(step.detail.replacingOccurrences(of: "\n", with: " "))" : "> 🔧 \(step.label)"
                })
            case .note:
                lines.append("> 💬 中途说明：\(message.text.replacingOccurrences(of: "\n", with: " "))")
            case .system:
                lines.append("> ⚠️ \(message.text.replacingOccurrences(of: "\n", with: " "))")
            }
            lines.append("")
        }
        return lines.joined(separator: "\n")
    }

    static let contentType: UTType =
        UTType(filenameExtension: "md") ?? .plainText
}

/// fileExporter 用的纯文本文档包壳。
struct MarkdownFileDocument: FileDocument {
    static let readableContentTypes: [UTType] = [ConversationExport.contentType, .plainText]

    var text: String

    init(text: String) {
        self.text = text
    }

    init(configuration: ReadConfiguration) throws {
        text = String(decoding: configuration.file.regularFileContents ?? Data(), as: UTF8.self)
    }

    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: Data(text.utf8))
    }
}
