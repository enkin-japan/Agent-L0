import { useState } from "react"
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
import { useT } from "@/store/useUiStore"
import { MarkdownContent } from "@/components/dashboard/markdown-content"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { CopyButton } from "@/components/ui/copy-button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

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

export function ChatMessage({
  message,
  onBranch,
  editable = false,
  onEditSubmit,
  onDeleteTurn,
}: {
  message: ChatMessageType
  onBranch?: (nodeIndex: number) => void
  editable?: boolean
  onEditSubmit?: (text: string) => void
  onDeleteTurn?: () => void
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
        <span className="whitespace-pre-wrap leading-relaxed">{message.content}</span>
      </div>
    )
  }

  // 工具执行结果：独立的信息卡片（工具名为内容，前缀"· result"本地化）
  if (message.role === "tool") {
    return (
      <div data-msg-id={message.id} className="flex w-full items-start gap-3 pl-11">
        <div className="flex max-w-[78%] flex-col gap-2">
          {message.toolResponses?.map((tr, i) => (
            <div key={i} className="group/tool rounded-xl border border-border bg-muted/40 px-3 py-2">
              <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <Wrench className="size-3.5" />
                {t.message.toolResult(tr.name)}
                <CopyButton
                  text={tr.response}
                  className="ml-auto opacity-0 transition-opacity focus-visible:opacity-100 group-hover/tool:opacity-100"
                />
              </p>
              <pre className="mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-foreground/80">
                {tr.response}
              </pre>
            </div>
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
                      onClick={() => { setDraft(message.content ?? ""); setEditing(true) }}
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
}
