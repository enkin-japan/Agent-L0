import type { Conversation } from "@/lib/agent-data"

/** 导出文案（随当前 UI 语言，由 i18n 的 exportDoc 段传入）。 */
export type ExportLabels = {
  user: string
  assistant: string
  system: string
  meta: (date: string, n: number) => string
  attachment: (name: string) => string
  toolCall: (name: string, args: string) => string
}

/** 工具参数压缩成单行短串，进导出转写的引用行。 */
function compactArgs(args: unknown, max = 200): string {
  if (!args || (typeof args === "object" && Object.keys(args).length === 0)) return ""
  const s = JSON.stringify(args)
  return s.length > max ? s.slice(0, max) + "…" : s
}

/**
 * 会话 → Markdown 转写（干净口径）：
 *  - 用户/助手正文全量保留（助手本就是 markdown，原样嵌入）；
 *  - 工具调用折叠为一行引用（> Tool call: …），工具返回体与思维链不进导出——
 *    归档以对话为主，全保真需求走后端 history.jsonl；
 *  - 系统警示条折叠为一行引用。
 */
export function conversationToMarkdown(
  conv: Conversation,
  title: string,
  labels: ExportLabels,
): string {
  const date = new Date().toISOString().slice(0, 10)
  const visible = conv.messages.filter((m) => m.role !== "tool")
  const lines: string[] = [
    `# ${title}`,
    "",
    `> ${labels.meta(date, visible.length)}`,
    "",
    "---",
    "",
  ]

  for (const m of visible) {
    if (m.role === "system") {
      if (m.content) lines.push(`> ⚠️ ${m.content.replace(/\n/g, " ")}`, "")
      continue
    }
    lines.push(`## ${m.role === "user" ? labels.user : labels.assistant}`, "")
    m.attachments?.forEach((a) => lines.push(labels.attachment(a.name), ""))
    m.toolCalls?.forEach((tc) => {
      if (tc.type !== "error") // 与后端语义对齐：旧历史里成功调用可能缺 type 字段
        lines.push(`> 🔧 ${labels.toolCall(tc.name ?? "?", compactArgs(tc.arguments))}`, "")
    })
    if (m.content) lines.push(m.content.trim(), "")
  }

  return lines.join("\n")
}

/** 导出文件名：标题去掉文件系统保留字符，附日期。 */
export function exportFilename(title: string): string {
  const safe = title.replace(/[\\/:*?"<>|\n]+/g, " ").trim().slice(0, 60) || "chat"
  return `${safe}-${new Date().toISOString().slice(0, 10)}.md`
}
