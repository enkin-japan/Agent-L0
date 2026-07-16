import Foundation

/// multipart/form-data 请求体拼装（对话端点的 prompt/参数 + files 上传）。
enum Multipart {
    static func body(fields: [(String, String)], files: [Upload], boundary: String) -> Data {
        var data = Data()
        let dash = "--\(boundary)\r\n"
        for (name, value) in fields {
            data.append(dash.data(using: .utf8)!)
            data.append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".data(using: .utf8)!)
            data.append(value.data(using: .utf8)!)
            data.append("\r\n".data(using: .utf8)!)
        }
        for file in files {
            data.append(dash.data(using: .utf8)!)
            let disposition = "Content-Disposition: form-data; name=\"files\"; filename=\"\(sanitize(file.filename))\"\r\n"
            data.append(disposition.data(using: .utf8)!)
            data.append("Content-Type: \(file.mime)\r\n\r\n".data(using: .utf8)!)
            data.append(file.data)
            data.append("\r\n".data(using: .utf8)!)
        }
        data.append("--\(boundary)--\r\n".data(using: .utf8)!)
        return data
    }

    /// 文件名进 header 前去掉引号/控制字符（后端资产名单独有白名单校验，这里只保证 header 合法）。
    private static func sanitize(_ name: String) -> String {
        name.replacingOccurrences(of: "\"", with: "_")
            .replacingOccurrences(of: "\r", with: "")
            .replacingOccurrences(of: "\n", with: "")
    }

    /// 图片字节嗅探（剪贴板来的数据没有文件名，按魔数定扩展名/MIME）。
    static func sniffImage(_ data: Data) -> (ext: String, mime: String) {
        if data.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return ("png", "image/png") }
        if data.starts(with: [0xFF, 0xD8]) { return ("jpg", "image/jpeg") }
        if data.count > 12, Array(data[4..<8]) == Array("ftyp".utf8) {
            return ("heic", "image/heic")
        }
        return ("png", "image/png")
    }

    /// 常见扩展名 → MIME（覆盖摄入层支持的类型，兜底 octet-stream）。
    static func mime(for filename: String) -> String {
        switch (filename as NSString).pathExtension.lowercased() {
        case "jpg", "jpeg": return "image/jpeg"
        case "png": return "image/png"
        case "gif": return "image/gif"
        case "webp": return "image/webp"
        case "heic": return "image/heic"
        case "wav": return "audio/wav"
        case "mp3": return "audio/mpeg"
        case "m4a": return "audio/mp4"
        case "flac": return "audio/flac"
        case "pdf": return "application/pdf"
        case "txt", "md", "py", "js", "ts", "json", "yaml", "yml", "csv", "log", "swift":
            return "text/plain"
        case "docx": return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        default: return "application/octet-stream"
        }
    }
}
