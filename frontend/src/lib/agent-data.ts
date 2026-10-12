import { getDict } from "@/lib/i18n"
import type { ExecutionReceipts, ChatResponse, HistoryResponse, RetryGroup, ResponseMessage, ToolCall, ToolResponse } from "@/types/agent-api"

export type ChatRole = "user" | "assistant" | "tool" | "system"

export type MessageAttachment = {
  name: string
  /** file = 非图非音的文档/代码等（后端摄入层解析或懒登记） */
  kind: "image" | "audio" | "file"
  /** 本地 blob 预览地址；历史记录里的附件没有（后端无静态资源服务，见 API.md §5.2）。 */
  previewUrl?: string
}

export type ChatMessage = {
  id: string
  role: ChatRole
  content?: string
  /** enable_thinking=true 时模型输出的思维链。 */
  thoughts?: string
  toolCalls?: ToolCall[]
  toolResponses?: ToolResponse[]
  attachments?: MessageAttachment[]
  variant?: "error" | "warning"
  /** 该消息所属轮次在服务端历史里的下标（分支接口的 node_index）。 */
  turnIndex?: number
  /** 真流式进行中的临时预览气泡：token 逐段填充，turn_end 到达后被权威消息替换。 */
  streaming?: boolean
  /** Rollback withdraws previews, not external effects; null means unknown facts. */
  executionFacts?: ExecutionReceipts | null
}

export type Conversation = {
  /** 同时作为后端 session_id，只用 [a-zA-Z0-9_-]（crypto.randomUUID 满足）。 */
  id: string
  title: string
  /** Epoch ms of last edit — used for chronological (newest-first) ordering. */
  updatedAt: number
  messages: ChatMessage[]
  /** 是否已从 GET /history 拉取过服务端历史。 */
  historyLoaded: boolean
  /** 服务端已落盘的轮次数（分支下标的上界）。 */
  turnCount: number
  /** Raw canonical coverage is independent of the displayed winner and session index. */
  canonicalTurns?: Array<ChatResponse | undefined>
  displayTurnIndices?: number[]
  retryGroups?: RetryGroup[]
  lastCanonicalTurn?: { index: number; prompt: string; turnRef?: string }
  retrySyncError?: boolean
  /** 是否已 pin（落盘 meta.json）：pin 的会话不被后台自动清扫回收。 */
  pinned?: boolean
}

/** 语言无关的「新对话」标题哨兵（与 store 一致）：展示时翻译成当前语言。 */
export const NEW_CHAT_SENTINEL = "__NEW_CHAT__"

/** 会话展示标题：新对话哨兵按当前语言翻译，其余原样返回。 */
export function convTitle(title: string, newChatLabel: string): string {
  return title === NEW_CHAT_SENTINEL ? newChatLabel : title
}

/** Compact relative time label for a chat's last edit, e.g. "5m", "3h", "4d". */
export function formatRelativeTime(updatedAt: number): string {
  const diff = Date.now() - updatedAt
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour
  const week = 7 * day

  if (diff < minute) return "now"
  if (diff < hour) return `${Math.floor(diff / minute)}m`
  if (diff < day) return `${Math.floor(diff / hour)}h`
  if (diff < week) return `${Math.floor(diff / day)}d`
  return `${Math.floor(diff / week)}w`
}

let messageSeq = 0

export function nextMessageId(): string {
  return `${Date.now()}-${messageSeq++}`
}

/**
 * 生成会话 ID（UUID v4）。crypto.randomUUID 仅在安全上下文（https/localhost）可用，
 * 通过内网 IP 的 http 访问时不存在，需用 getRandomValues 降级实现。
 */
export function newSessionId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function baseName(path: string): string {
  return path.split(/[\\/]/).pop() ?? path
}

/** 把历史轮次里的服务器文件路径转成只有文件名的附件（无法回显内容，见 API.md §5.2）。 */
function attachmentsFromTurn(turn: ChatResponse): MessageAttachment[] {
  return [
    ...(turn.image ?? []).map((p) => ({ name: baseName(p), kind: "image" as const })),
    ...(turn.audio ?? []).map((p) => ({ name: baseName(p), kind: "audio" as const })),
    // 摄入层登记的原始附件（PDF/文档/代码等）；audio 类已单独在 turn.audio 或转写标记里体现
    ...(turn.attachments ?? [])
      .filter((a) => a && typeof a.name === "string")
      .map((a) => ({
        name: String(a.name),
        kind: (a.type === "audio" ? "audio" : "file") as MessageAttachment["kind"],
      })),
  ]
}

/** 把一轮响应里的 response 数组映射为前端消息列表。 */
export function mapResponseMessages(
  response: ResponseMessage[],
  turnIndex?: number,
  systemVariant: "error" | "warning" = "error",
): ChatMessage[] {
  const out: ChatMessage[] = []
  for (const m of response) {
    if (m.role === "assistant") {
      if ("tool_calls" in m) {
        out.push({
          id: nextMessageId(),
          role: "assistant",
          toolCalls: m.tool_calls,
          thoughts: m.thoughts,
          turnIndex,
        })
      } else {
        out.push({
          id: nextMessageId(),
          role: "assistant",
          content: m.content,
          thoughts: m.thoughts,
          turnIndex,
        })
      }
    } else if (m.role === "tool") {
      out.push({
        id: nextMessageId(),
        role: "tool",
        toolResponses: m.tool_responses,
        turnIndex,
      })
    } else {
      // role === "system"：引擎异常（含 traceback，error 红色）/ 取消或护栏提示（warning 黄色）
      out.push({
        id: nextMessageId(),
        role: "system",
        content: m.content,
        variant: systemVariant,
        turnIndex,
      })
    }
  }
  return out
}

/** Bound the display projection even when an old/malformed peer sends oversized facts. */
function executionFacts(value: ChatResponse["execution_receipts"]): ExecutionReceipts | null {
  if (!value || !Array.isArray(value.items)) return null
  let truncated = value.truncated === true || value.items.length > 64
  const items = value.items.slice(-64).flatMap((item) => {
    if (!item || !["tool_started", "tool_result"].includes(item.type)) { truncated = true; return [] }
    const detail = typeof item.detail === "string" ? item.detail : ""
    const name = typeof item.name === "string" ? item.name : ""
    const cut = item.truncated === true || detail.length > 2000 || name.length > 128
      || (item.call_id?.length ?? 0) > 128 || (item.status?.length ?? 0) > 32
    if (cut) truncated = true
    return [{ seq: item.seq, ts: item.ts, type: item.type, name: name.slice(0, 128), detail: detail.slice(0, 2000),
      call_id: item.call_id?.slice(0, 128), status: item.status?.slice(0, 32), truncated: cut }]
  })
  return { items, truncated }
}

/** One canonical mapping for history and live terminal reconciliation. */
export function turnToMessages(turn: ChatResponse, turnIndex: number, labels = getDict("en").store): ChatMessage[] {
  const attachments = attachmentsFromTurn(turn)
  const userMessage: ChatMessage = {
    id: nextMessageId(), role: "user", content: turn.prompt,
    attachments: attachments.length > 0 ? attachments : undefined, turnIndex,
  }
  if (turn.status === "cancelled" || turn.status === "fail") {
    return [userMessage, { id: nextMessageId(), role: "system", turnIndex,
      content: turn.status === "cancelled" ? labels.cancelled : labels.engineFail,
      variant: turn.status === "cancelled" ? "warning" : "error",
      executionFacts: executionFacts(turn.execution_receipts) }]
  }
  const messages = [userMessage, ...mapResponseMessages(turn.response ?? [], turnIndex,
    turn.status === "warning" ? "warning" : "error")]
  if (turn.status === "warning" && !messages.some((m) => m.role === "assistant" && m.content)) {
    messages.push({ id: nextMessageId(), role: "assistant", content: labels.warningNoConclusion, variant: "warning", turnIndex })
  }
  return messages
}

/** Consume server projection only: validate bounds/duplicates, never rebuild retry families. */
export function historyProjection(history: HistoryResponse, labels = getDict("en").store) {
  const turns = history.turns ?? []
  const validIndex = (index: number) => Number.isInteger(index) && index >= 0 && index < turns.length
  const candidate = history.display_turn_indices
  const validDisplay = Array.isArray(candidate) && candidate.every(validIndex)
    && new Set(candidate).size === candidate.length && (turns.length === 0 || candidate.length > 0)
  let displayTurnIndices = validDisplay ? candidate : turns.map((_, index) => index)
  const used = new Set<number>()
  let retryGroups = (validDisplay && Array.isArray(history.retry_groups) ? history.retry_groups : []).filter((group) => {
    if (!group || !validIndex(group.root_index) || !displayTurnIndices.includes(group.display_index)
        || !Array.isArray(group.turn_indices) || group.turn_indices.length < 2
        || !group.turn_indices.every(validIndex) || new Set(group.turn_indices).size !== group.turn_indices.length
        || group.turn_indices.some((index, position) => position > 0 && index <= group.turn_indices[position - 1])
        || !group.turn_indices.includes(group.root_index) || !group.turn_indices.includes(group.display_index)
        || group.turn_indices.some((index) => used.has(index))) return false
    group.turn_indices.forEach((index) => used.add(index))
    return true
  })
  // Optional/partial peer metadata must never hide an attempt without an audit entry.
  // This checks visibility coverage only; family membership remains server-owned.
  const visibleCoverage = new Set([...displayTurnIndices, ...used])
  if (visibleCoverage.size !== turns.length) {
    displayTurnIndices = turns.map((_, index) => index)
    retryGroups = []
  }
  const tail = turns.at(-1)
  return { canonicalTurns: turns, displayTurnIndices, retryGroups,
    lastCanonicalTurn: tail ? { index: turns.length - 1, prompt: tail.prompt, turnRef: history.last_turn_ref ?? tail.turn_ref } : undefined,
    messages: displayTurnIndices.flatMap((index) => turnToMessages(turns[index], index, labels)) }
}
