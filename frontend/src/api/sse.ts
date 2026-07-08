// SSE over fetch（见 docs/frontend-refactor-decisions.md §1）。
// 用 fetch + ReadableStream 消费 Server-Sent Events，而非浏览器原生 EventSource：
// 后者不能设自定义 header（带不了 Authorization Bearer）、也不能 POST（对话是 multipart 上传）。

export interface SSEFrame {
  event: string
  /** data 字段的原始字符串（多行 data 以 \n 连接）；调用方自行 JSON.parse。 */
  data: string
}

/** 把一个 SSE 响应体流逐帧解析产出。事件以空行（\n\n）分隔。 */
export async function* parseSSE(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SSEFrame> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = parseFrame(buffer.slice(0, idx))
        buffer = buffer.slice(idx + 2)
        if (frame) yield frame
      }
    }
    // 冲刷尾部（末帧可能无结尾空行）
    const tail = parseFrame(buffer)
    if (tail) yield tail
  } finally {
    reader.releaseLock()
  }
}

function parseFrame(raw: string): SSEFrame | null {
  let event = "message"
  const dataLines: string[] = []
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim()
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""))
  }
  if (dataLines.length === 0) return null
  return { event, data: dataLines.join("\n") }
}
