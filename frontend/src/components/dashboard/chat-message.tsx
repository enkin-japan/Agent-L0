import { memo, useMemo, useState } from "react"
import {
  AlertTriangle,
  Cpu,
  FileAudio,
  FileText,
  GitBranch,
  Pencil,
  Trash2,
  User,
  Wrench,
} from "lucide-react"

import { cn } from "@/lib/utils"
import type { ChatMessage as ChatMessageType } from "@/lib/agent-data"
import { turnToMessages } from "@/lib/agent-data"
import type { ChatResponse, RetryGroup, ToolResponse } from "@/types/agent-api"
import { useT } from "@/store/useUiStore"
import { MarkdownContent } from "@/components/dashboard/markdown-content"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { CopyButton } from "@/components/ui/copy-button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/** 工具渲染格投影（PROTOCOL.md §2.3.1）：hint 是账本里的数据，"怎么画"由本端决定
 *  （铁律 4：客户端是薄渲染器）。返回 null = 交回灰框。
 *
 *  **不认识的 hint 必须降级灰框**（§0.5 向前兼容）：后端先于本端上线新 hint 是常态，
 *  那时旧前端得照样把结果显示出来，而不是白屏或空卡。加新 hint 就在这里加一个分支。 */
function renderByHint(tr: ToolResponse) {
  const payload = tr.render?.payload
  if (!payload) return null
  switch (tr.render?.hint) {
    case "citations": {
      const items = Array.isArray(payload.items) ? (payload.items as Citation[]) : null
      if (!items?.length) return null   // 载荷形状不对 → 灰框，不炸组件树
      return <Citations items={items} />
    }
    default:
      return null
  }
}

interface Citation {
  source?: string
  path?: string
  score?: number
  snippet?: string
}

/** citations 渲染格：召回结果排成来源卡片。今天这里是灰框里一大坨
 *  "--- Result N (Match Score: …) ---"，命中分和来源全埋在文字里。 */
function Citations({ items }: { items: Citation[] }) {
  return (
    <div className="flex flex-col gap-1.5">
      {items.map((c, i) => (
        <div key={i} className="rounded-xl border border-border bg-muted/40 px-3 py-2">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <FileText className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate">{c.source || "Unknown"}</span>
            {typeof c.score === "number" && (
              <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
                {c.score.toFixed(2)}
              </span>
            )}
          </p>
          {c.snippet && (
            <p className="mt-1 line-clamp-3 text-xs leading-relaxed text-foreground/80">
              {c.snippet}
            </p>
          )}
          {c.path && (
            <p className="mt-1 truncate text-[11px] text-muted-foreground/70" title={c.path}>
              {c.path}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}

/** 思维链折叠块（enable_thinking=true 时出现）。内容由模型产出，按原样展示。 */
function Thoughts({ text, label }: { text: string; label: string }) {
  return (
    <details className="mb-2 rounded-xl border border-border/70 bg-muted/40 px-3 py-2">
      <summary className="cursor-pointer select-none text-xs font-medium text-muted-foreground">
        {label}
      </summary>
      <p className="whitespace-pre-wrap pt-2 text-xs leading-relaxed text-muted-foreground">
        {text}
      </p>
    </details>
  )
}

/** memo：流式期间 store 每次增量都会重建 conversations 数组导致父级整体重渲染；
 *  非流式消息的 message 引用是稳定的（updateMessage 只重建目标消息），memo 让
 *  它们整棵跳过（含内部 MarkdownContent 的 remark 重解析）。回调 props 由
 *  ChatArea 用 useStableCallback 稳定，浅比较才有效——新增回调 prop 时同样处理。 */
/** Shared tool observation renderer for ordinary transcripts and archived facts. */
function ToolObservation({ tr, label }: { tr: ToolResponse; label: string }) {
  const rendered = renderByHint(tr)
  if (rendered) return <div>{rendered}</div>
  return (
    <div className="group/tool rounded-xl border border-border bg-muted/40 px-3 py-2">
      <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Wrench className="size-3.5" />{label}
        <CopyButton text={tr.response} className="ml-auto opacity-0 transition-opacity focus-visible:opacity-100 group-hover/tool:opacity-100" />
      </p>
      <pre className="mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-foreground/80">{tr.response}</pre>
    </div>
  )
}

function ExecutionFacts({ facts }: { facts: NonNullable<ChatMessageType["executionFacts"]> | null }) {
  const t = useT()
  return (
    <details className="mt-3 w-full rounded-xl border border-border bg-background/70 p-3 text-foreground">
      <summary className="cursor-pointer text-xs font-medium">{t.execution.title}</summary>
      <div className="mt-2 flex flex-col gap-2 text-xs">
        <p className="text-muted-foreground">{t.execution.limits}</p>
        <p className="text-muted-foreground">{t.execution.semantics}</p>
        {!facts ? <p>{t.execution.unknown}</p> : facts.items.length === 0 ? <p>{t.execution.empty}</p> : null}
        {facts?.truncated && <p className="text-chart-3">{t.execution.truncated}</p>}
        {facts?.items.map((item, index) => (
          <div key={`${item.seq}-${index}`}>
            <ToolObservation tr={{ name: item.name, response: item.detail }}
              label={item.type === "tool_started" ? t.execution.started(item.name) : t.execution.returned(item.name)} />
            {item.status && <p className="mt-1 text-muted-foreground">{t.execution.sourceStatus(item.status)}</p>}
            {item.truncated && <p className="mt-1 text-muted-foreground">{t.execution.truncated}</p>}
          </div>
        ))}
      </div>
    </details>
  )
}

/** Audit reuses the canonical mapper and message renderer; no editing/execution controls. */
export function RetryAttempts({ group, turns, onBranch }: {
  group: RetryGroup; turns: Array<ChatResponse | undefined>; onBranch?: (index: number) => void
}) {
  const t = useT()
  const attempts = useMemo(() => group.turn_indices.flatMap((index) => {
    const turn = turns[index]
    return turn ? [{ index, turn, messages: turnToMessages(turn, index, t.store) }] : []
  }), [group, turns, t.store])
  const latest = attempts.at(-1)?.turn
  const status = (value: ChatResponse["status"]) => t.retry.statuses[value] ?? value
  return (
    <details className="rounded-xl border border-border bg-muted/20 px-3 py-2" data-retry-display-index={group.display_index}>
      <summary className="cursor-pointer text-xs font-medium">{t.retry.title} · {latest ? t.retry.latest(status(latest.status)) : t.retry.timeUnknown}</summary>
      <div className="mt-3 flex flex-col gap-5">
        {attempts.map(({ index, turn, messages }) => (
          <section key={index} data-canonical-index={index} className="flex flex-col gap-3">
            <p className="text-xs text-muted-foreground">{t.retry.turn(index + 1)} · {status(turn.status)} · {typeof turn.timestamp === "number" && Number.isFinite(turn.timestamp)
              ? new Date(turn.timestamp * 1000).toLocaleString() : t.retry.timeUnknown}
              {index === group.display_index ? ` · ${t.retry.current}` : ""}</p>
            {messages.map((message) => <ChatMessage key={message.id} message={message} onBranch={onBranch} editable={false} />)}
          </section>
        ))}
      </div>
    </details>
  )
}

export const ChatMessage = memo(function ChatMessage({
  message,
  onBranch,
  editable = false,
  onEditSubmit,
  onDeleteTurn,
  editPrompt,
  editNote,
}: {
  message: ChatMessageType
  onBranch?: (nodeIndex: number) => void
  editable?: boolean
  onEditSubmit?: (text: string) => void
  onDeleteTurn?: () => void
  editPrompt?: string
  editNote?: string
}) {
  const t = useT()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState("")

  // 引擎异常 / 中断等系统提示：居中的警示条
  if (message.role === "system") {
    return (
      <div
        data-msg-id={message.id}
        className={cn(
          "mx-auto flex max-w-[90%] items-start gap-2 rounded-xl border px-4 py-3 text-sm",
          message.variant === "warning"
            ? "border-chart-3/40 bg-chart-3/10 text-chart-3"
            : "border-destructive/30 bg-destructive/10 text-destructive",
        )}
      >
        <AlertTriangle className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0 flex-1">
          <span className="whitespace-pre-wrap leading-relaxed">{message.content}</span>
          {message.executionFacts !== undefined && <ExecutionFacts facts={message.executionFacts} />}
        </div>
      </div>
    )
  }

  // 工具执行结果：有渲染格（PROTOCOL.md §2.3.1）的按格投影，其余照旧灰框
  if (message.role === "tool") {
    return (
      <div data-msg-id={message.id} className="flex w-full items-start gap-3 pl-11">
        <div className="flex max-w-[78%] flex-col gap-2">
          {message.toolResponses?.map((tr, i) => (
            <ToolObservation key={i} tr={tr} label={t.message.toolResult(tr.name)} />
          ))}
        </div>
      </div>
    )
  }

  const isUser = message.role === "user"
  const canBranch = !isUser && onBranch && message.turnIndex !== undefined

  return (
    <div
      data-msg-id={message.id}
      className={cn(
        "group/msg flex w-full items-start gap-3 scroll-mt-6",
        isUser ? "flex-row-reverse" : "flex-row",
      )}
    >
      <Avatar className="size-8 shrink-0">
        <AvatarFallback className={cn(isUser ? "bg-accent text-accent-foreground" : "bg-primary text-primary-foreground")}>
          {isUser ? <User className="size-4" /> : <Cpu className="size-4" />}
        </AvatarFallback>
      </Avatar>

      <div className={cn("flex max-w-[78%] flex-col", isUser ? "items-end" : "items-start")}>
        <div
          className={cn(
            "w-fit max-w-full rounded-2xl px-4 py-3 text-sm leading-relaxed",
            isUser
              ? "rounded-tr-md bg-primary text-primary-foreground"
              : "rounded-tl-md border border-border bg-card text-card-foreground shadow-sm",
            message.variant === "warning" && "border-chart-3/40 bg-chart-3/10 text-chart-3",
          )}
        >
          {message.thoughts && <Thoughts text={message.thoughts} label={t.message.thoughts} />}

          {/* 用户附件：本地图片直接预览，音频/历史附件显示文件名 */}
          {message.attachments && message.attachments.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {message.attachments.map((a, i) =>
                a.kind === "image" && a.previewUrl ? (
                  <img key={i} src={a.previewUrl} alt={a.name} className="max-h-40 rounded-xl object-cover" />
                ) : (
                  <span
                    key={i}
                    className={cn(
                      "flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs",
                      isUser ? "bg-primary-foreground/15" : "bg-muted",
                    )}
                  >
                    {a.kind === "audio" ? <FileAudio className="size-3.5" /> : <FileText className="size-3.5" />}
                    <span className="max-w-40 truncate">{a.name}</span>
                  </span>
                ),
              )}
            </div>
          )}

          {/* 工具调用卡片 */}
          {message.toolCalls && message.toolCalls.length > 0 && (
            <div className="flex flex-col gap-2">
              {message.toolCalls.map((tc, i) =>
                tc.type !== "error" ? ( // 与后端语义对齐：旧历史里成功调用可能缺 type 字段
                  <div key={i} className="rounded-xl border border-border bg-muted/40 px-3 py-2">
                    <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                      <Wrench className="size-3.5" />
                      {t.message.callingTool(tc.name ?? "")}
                    </p>
                    {tc.arguments && Object.keys(tc.arguments).length > 0 && (
                      <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs text-foreground/80">
                        {JSON.stringify(tc.arguments, null, 2)}
                      </pre>
                    )}
                  </div>
                ) : (
                  <p key={i} className="text-xs italic text-muted-foreground">
                    {t.message.malformed}
                  </p>
                ),
              )}
            </div>
          )}

          {editing ? (
            <div className="flex w-72 max-w-full flex-col gap-2">
              <textarea
                value={draft}
                autoFocus
                rows={3}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Escape") setEditing(false) }}
                className="w-full resize-y rounded-lg border border-primary-foreground/30 bg-primary-foreground/10 px-2.5 py-2 text-sm leading-relaxed text-primary-foreground placeholder:text-primary-foreground/50 focus:outline-none"
              />
              {message.attachments && message.attachments.length > 0 && (
                <p className="text-xs text-primary-foreground/70">{t.message.editAttachNote}</p>
              )}
              <div className="flex justify-end gap-1.5">
                <button
                  type="button"
                  onClick={() => setEditing(false)}
                  className="rounded-md px-2 py-1 text-xs text-primary-foreground/70 hover:bg-primary-foreground/10 hover:text-primary-foreground"
                >
                  {t.message.editCancel}
                </button>
                <button
                  type="button"
                  disabled={!draft.trim()}
                  onClick={() => { setEditing(false); onEditSubmit?.(draft.trim()) }}
                  className="rounded-md bg-primary-foreground/90 px-2 py-1 text-xs font-medium text-primary hover:bg-primary-foreground disabled:opacity-50"
                >
                  {t.message.editSave}
                </button>
              </div>
            </div>
          ) : (
            message.content &&
            (isUser ? (
              // 用户输入保持纯文本：星号/下划线等不是 markdown 意图，渲染反而失真
              <span className="whitespace-pre-wrap">{message.content}</span>
            ) : (
              <MarkdownContent text={message.content} />
            ))
          )}
        </div>

        {/* 消息操作行：复制（所有有正文的消息）· 编辑/删除（最后一轮用户发言）· 分支（assistant） */}
        {isUser && editable && editNote && <p className="mt-1 text-xs text-muted-foreground">{editNote}</p>}
        {!editing && !message.streaming && (message.content || (isUser && editable) || canBranch) && (
          <div className="mt-1 flex gap-0.5 opacity-100 transition-opacity hover-hover:opacity-0 hover-hover:group-hover/msg:opacity-100 focus-within:opacity-100">
            {message.content && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <CopyButton
                    text={message.content}
                    label={t.message.copy}
                    copiedLabel={t.message.copied}
                  />
                </TooltipTrigger>
                <TooltipContent side="bottom">{t.message.copyTooltip}</TooltipContent>
              </Tooltip>
            )}
            {isUser && editable && (
              <>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => { setDraft(editPrompt ?? message.content ?? ""); setEditing(true) }}
                      className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-muted-foreground/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <Pencil className="size-3.5" />
                      {t.message.edit}
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom">{t.message.editTooltip}</TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={onDeleteTurn}
                      className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-muted-foreground/60 hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <Trash2 className="size-3.5" />
                      {t.message.delete}
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom">{t.message.deleteTooltip}</TooltipContent>
                </Tooltip>
              </>
            )}
            {canBranch && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    onClick={() => onBranch(message.turnIndex!)}
                    className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-muted-foreground/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <GitBranch className="size-3.5" />
                    {t.message.branch}
                  </button>
                </TooltipTrigger>
                <TooltipContent side="bottom">{t.message.branchTooltip}</TooltipContent>
              </Tooltip>
            )}
          </div>
        )}
      </div>
    </div>
  )
})
