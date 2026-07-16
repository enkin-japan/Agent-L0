import Foundation

/// 一帧 SSE 事件（event + data 原始串）。与 frontend/src/api/sse.ts 的解析语义一致：
/// 帧以空行（\n\n）分隔；心跳注释帧（": ping"）没有 data 行，解析为 nil 静默跳过。
struct SSEFrame {
    let event: String
    let data: String
}

enum SSEError: LocalizedError {
    case badStatus(Int)

    var errorDescription: String? {
        switch self {
        case .badStatus(let code): return "HTTP \(code)"
        }
    }
}

enum SSEClient {
    /// 发起请求并把响应体解析成 SSE 帧流。请求失败/非 200 直接抛错。
    static func frames(for request: URLRequest, session: URLSession) -> AsyncThrowingStream<SSEFrame, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await session.bytes(for: request)
                    if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                        throw SSEError.badStatus(http.statusCode)
                    }
                    var buffer: [UInt8] = []
                    buffer.reserveCapacity(4096)
                    for try await byte in bytes {
                        buffer.append(byte)
                        let n = buffer.count
                        // 帧边界：连续两个换行。data 内容是 JSON 编码串，真实换行都被转义，
                        // 不会出现假边界。
                        if n >= 2, buffer[n - 1] == 0x0A, buffer[n - 2] == 0x0A {
                            if let frame = parseFrame(String(decoding: buffer, as: UTF8.self)) {
                                continuation.yield(frame)
                            }
                            buffer.removeAll(keepingCapacity: true)
                        }
                    }
                    // 冲刷尾部（末帧可能无结尾空行）
                    if !buffer.isEmpty, let frame = parseFrame(String(decoding: buffer, as: UTF8.self)) {
                        continuation.yield(frame)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    static func parseFrame(_ raw: String) -> SSEFrame? {
        var event = "message"
        var dataLines: [String] = []
        for line in raw.split(separator: "\n", omittingEmptySubsequences: true) {
            if line.hasPrefix("event:") {
                event = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("data:") {
                var d = line.dropFirst(5)
                if d.hasPrefix(" ") { d = d.dropFirst() }
                dataLines.append(String(d))
            }
        }
        guard !dataLines.isEmpty else { return nil }  // 心跳注释帧
        return SSEFrame(event: event, data: dataLines.joined(separator: "\n"))
    }
}
