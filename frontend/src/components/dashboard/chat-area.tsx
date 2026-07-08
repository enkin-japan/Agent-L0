"use client"

import { useEffect, useRef, useState } from "react"
import {
  BrainCircuit,
  Check,
  CheckCircle2,
  Cpu,
  Download,
  ListTodo,
  Loader2,
  PanelLeft,
  PanelLeftOpen,
  PanelRight,
  PanelRightOpen,
  Pencil,
  Play,
  ShieldAlert,
  PauseCircle,
  X,
} from "lucide-react"

import type { Conversation } from "@/lib/agent-data"
import { convTitle } from "@/lib/agent-data"
import { conversationToMarkdown, exportFilename } from "@/lib/export"
import { downloadText } from "@/lib/utils"
import type { LiveActivity, PendingTool, TaskSnapshot } from "@/types/agent-api"
import type { LastMetrics } from "@/store/useChatStore"
import { useT } from "@/store/useUiStore"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ChatInput } from "@/components/dashboard/chat-input"
import { ChatMessage } from "@/components/dashboard/chat-message"
import { ChatNavigator } from "@/components/dashboard/chat-navigator"

const _STEP_MARK: Record<string, string> = { tool: "🛠", plan: "📋", note: "⚠" }

/** 实时步骤流卡片：SSE task_progress 推送，展示"当前在做什么"+ 最近完成动作。
 *  内容（current/label/detail）由后端产出，按原样展示；卡片壳本地化。 */
function ActivityCard({ activity, fallback }: { activity: LiveActivity; fallback: string }) {
  const recent = activity.steps.slice(-6)
  const running = activity.phase === "running" || activity.phase === "awaiting_approval"
  return (
    <div className="w-full rounded-2xl border border-border bg-card px-4 py-3 shadow-sm">
      <p className="flex items-center gap-2 text-xs font-medium">
        {running ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-chart-1" />
        ) : (
          <CheckCircle2 className="size-3.5 shrink-0 text-chart-2" />
        )}
        <span className="truncate">{activity.current || fallback}</span>
      </p>
      {recent.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1 border-t border-border/60 pt-2">
          {recent.map((step, i) => (
            <li key={i} className="flex items-start gap-2 text-xs text-muted-foreground">
              <span className="shrink-0">{_STEP_MARK[step.kind] ?? "•"}</span>
              <span className="min-w-0">
                <span className="font-medium text-foreground/80">{step.label}</span>
                {step.detail ? <span className="text-muted-foreground/70"> — {step.detail}</span> : null}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function ChatArea({
  conversation,
  thinkingEnabled,
  generating,
  notice,
  metrics,
  sidebarHidden,
  pendingTools,
  suspended,
  taskProgress,
  activity,
  contextOpen,
  onToggleContext,
  onApproval,
  onSend,
  onStop,
  onSuspend,
  onResume,
  onAbandon,
  onBranch,
  onUpdateLast,
  onDeleteLast,
  onRename,
  onDismissNotice,
  onOpenMobileSidebar,
  onShowSidebar,
}: {
  conversation: Conversation
  thinkingEnabled: boolean
  generating: boolean
  notice: string | null
  metrics: LastMetrics | null
  sidebarHidden: boolean
  pendingTools: PendingTool[] | null
  suspended: boolean
  taskProgress: TaskSnapshot | null
  activity: LiveActivity | null
  contextOpen: boolean
  onToggleContext: () => void
  onApproval: (approve: boolean) => void
  onSend: (value: string, files: File[]) => void
  onStop: () => void
  onSuspend: () => void
  onResume: () => void
  onAbandon: () => void
  onBranch: (nodeIndex: number) => void
  onUpdateLast: (text: string) => void
  onDeleteLast: () => void
  onRename: (id: string, title: string) => void
  onDismissNotice: () => void
  onOpenMobileSidebar: () => void
  onShowSidebar: () => void
}) {
  const t = useT()
  const bottomRef = useRef<HTMLDivElement>(null)
  const titleInputRef = useRef<HTMLInputElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const userMessages = conversation.messages
    .filter((m) => m.role === "user")
    .map((m) => ({ id: m.id, content: m.content ?? "" }))

  const lastUserMessageId = userMessages.at(-1)?.id

  const [editingId, setEditingId] = useState<string | null>(null)
  const [draftTitle, setDraftTitle] = useState("")
  const isEditing = editingId === conversation.id

  const title = convTitle(conversation.title, t.store.newChatTitle)
  const planActive = !!taskProgress && taskProgress.plan.length > 0
  const planDone = taskProgress ? taskProgress.plan.filter((s) => s.status === "done").length : 0

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [conversation.messages.length, conversation.id, generating])

  function startEditing() {
    setDraftTitle(title)
    setEditingId(conversation.id)
  }

  function commitTitle() {
    if (draftTitle.trim()) onRename(conversation.id, draftTitle.trim())
    setEditingId(null)
  }

  return (
    <main className="flex h-full min-w-0 flex-1 flex-col">
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-border bg-background/80 px-5 py-3.5 backdrop-blur">
        <Button variant="ghost" size="icon" className="rounded-lg text-muted-foreground lg:hidden" onClick={onOpenMobileSidebar}>
          <PanelLeft />
          <span className="sr-only">{t.header.openSidebar}</span>
        </Button>
        {sidebarHidden && (
          <Button variant="ghost" size="icon" className="hidden rounded-lg text-muted-foreground lg:inline-flex" onClick={onShowSidebar}>
            <PanelLeftOpen />
            <span className="sr-only">{t.header.showSidebar}</span>
          </Button>
        )}

        <div className="flex min-w-0 flex-1 items-center">
          {isEditing ? (
            <div className="flex w-full max-w-md items-center gap-1.5">
              <Input
                ref={titleInputRef}
                value={draftTitle}
                autoFocus
                onFocus={(e) => e.target.select()}
                onChange={(e) => setDraftTitle(e.target.value)}
                onBlur={commitTitle}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.nativeEvent.isComposing) commitTitle()
                  else if (e.key === "Escape") setEditingId(null)
                }}
                className="h-9 rounded-lg text-base font-semibold"
              />
              <Button variant="ghost" size="icon" className="rounded-lg text-muted-foreground" onMouseDown={(e) => e.preventDefault()} onClick={commitTitle}>
                <Check />
              </Button>
              <Button variant="ghost" size="icon" className="rounded-lg text-muted-foreground" onMouseDown={(e) => e.preventDefault()} onClick={() => setEditingId(null)}>
                <X />
              </Button>
            </div>
          ) : (
            <div className="flex min-w-0 flex-col leading-none">
              <div className="group/title flex min-w-0 items-center gap-1.5">
                <h1 className="truncate text-base font-semibold tracking-tight">{title}</h1>
                <button
                  type="button"
                  onClick={startEditing}
                  className="shrink-0 rounded-md p-0.5 text-muted-foreground/50 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Pencil className="size-4" />
                  <span className="sr-only">{t.header.rename}</span>
                </button>
              </div>
              <span className="pt-1 text-xs text-muted-foreground">
                {t.header.messages(conversation.messages.length)}
                {metrics?.generationTps ? ` · ${t.header.tokPerSec(metrics.generationTps.toFixed(1))}` : ""}
              </span>
            </div>
          )}
        </div>

        {/* 活动中的多步计划：点开检视面板的 Plan 页 */}
        {planActive && (
          <button type="button" onClick={onToggleContext}>
            <Badge variant="secondary" className="gap-1.5 rounded-full font-normal">
              <ListTodo className="size-3.5" />
              {t.activity.progress(planDone, taskProgress!.plan.length)}
            </Badge>
          </button>
        )}

        {thinkingEnabled && (
          <Badge variant="secondary" className="gap-1.5 rounded-full font-normal">
            <BrainCircuit className="size-3.5" />
            {t.header.thinkingOn}
          </Badge>
        )}

        {conversation.messages.length > 0 && (
          <Button
            variant="ghost"
            size="icon"
            className="rounded-lg text-muted-foreground"
            title={t.header.exportChat}
            onClick={() =>
              downloadText(
                exportFilename(title),
                conversationToMarkdown(conversation, title, t.exportDoc),
              )
            }
          >
            <Download />
            <span className="sr-only">{t.header.exportChat}</span>
          </Button>
        )}

        <Button variant="ghost" size="icon" className="rounded-lg text-muted-foreground" onClick={onToggleContext}>
          {contextOpen ? <PanelRightOpen /> : <PanelRight />}
          <span className="sr-only">{t.header.toggleInspector}</span>
        </Button>
      </header>

      {/* Notice banner */}
      {notice && (
        <div className="mx-auto mt-3 flex w-full max-w-3xl items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
          <span className="flex-1">{notice}</span>
          <button type="button" onClick={onDismissNotice} className="shrink-0 rounded-md p-0.5 hover:bg-destructive/10">
            <X className="size-4" />
          </button>
        </div>
      )}

      {/* Messages + Navigator */}
      <div className="relative min-h-0 flex-1">
        <div ref={scrollRef} className="h-full overflow-y-auto">
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-8">
            {conversation.messages.length === 0 && !generating && (
              <div className="flex flex-col items-center gap-3 py-24 text-center">
                <div className="flex size-12 items-center justify-center rounded-2xl bg-primary text-primary-foreground">
                  <Cpu className="size-6" />
                </div>
                <p className="text-sm font-medium">{t.empty.title}</p>
                <p className="max-w-sm text-xs text-muted-foreground">{t.empty.desc}</p>
              </div>
            )}

            {conversation.messages.map((message) => (
              <ChatMessage
                key={message.id}
                message={message}
                onBranch={onBranch}
                editable={message.id === lastUserMessageId && !generating}
                onEditSubmit={onUpdateLast}
                onDeleteTurn={onDeleteLast}
              />
            ))}

            {/* 实时步骤流：生成期间展示"当前动作 + 最近步骤"；无步骤时回退为打字指示 */}
            {generating &&
              (activity && (activity.current || activity.steps.length > 0) ? (
                <ActivityCard activity={activity} fallback={t.activity.processing} />
              ) : (
                <div className="flex w-full items-start gap-3">
                  <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
                    <Cpu className="size-4" />
                  </div>
                  <div className="flex items-center gap-1.5 rounded-2xl rounded-tl-md border border-border bg-card px-4 py-4 shadow-sm">
                    <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:-0.3s]" />
                    <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:-0.15s]" />
                    <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground" />
                  </div>
                </div>
              ))}
            <div ref={bottomRef} />
          </div>
        </div>
        <ChatNavigator
          scrollRef={scrollRef}
          items={userMessages}
          recomputeKey={`${conversation.id}:${conversation.messages.length}`}
        />
      </div>

      {/* 敏感操作审批卡片 */}
      {pendingTools && pendingTools.length > 0 && !generating && (
        <div className="mx-auto w-full max-w-3xl px-4 pb-3">
          <div className="rounded-2xl border border-chart-3/40 bg-chart-3/10 p-4">
            <p className="flex items-center gap-2 text-sm font-semibold text-chart-3">
              <ShieldAlert className="size-4 shrink-0" />
              {t.approval.title}
            </p>
            <p className="mt-1 text-xs leading-relaxed text-chart-3/80">{t.approval.desc}</p>
            <div className="mt-2.5 flex flex-col gap-2">
              {pendingTools.map((tool, i) => (
                <div key={i} className="rounded-xl border border-chart-3/30 bg-background/60 px-3 py-2">
                  <p className="text-xs font-medium">{tool.name}</p>
                  {tool.arguments && Object.keys(tool.arguments).length > 0 && (
                    <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words text-xs text-muted-foreground">
                      {JSON.stringify(tool.arguments, null, 2)}
                    </pre>
                  )}
                </div>
              ))}
            </div>
            <div className="mt-3 flex justify-end gap-2">
              <Button variant="outline" size="sm" className="rounded-lg" onClick={() => onApproval(false)}>
                {t.approval.reject}
              </Button>
              <Button variant="destructive" size="sm" className="rounded-lg" onClick={() => onApproval(true)}>
                {t.approval.allow}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* 挂起任务恢复卡 */}
      {suspended && !generating && !(pendingTools && pendingTools.length > 0) && (
        <div className="mx-auto w-full max-w-3xl px-4 pb-3">
          <div className="rounded-2xl border border-chart-5/40 bg-chart-5/10 p-4">
            <p className="flex items-center gap-2 text-sm font-semibold text-chart-5">
              <PauseCircle className="size-4 shrink-0" />
              {t.suspended.title}
            </p>
            <p className="mt-1 text-xs leading-relaxed text-chart-5/80">{t.suspended.desc}</p>
            <div className="mt-3 flex justify-end gap-2">
              <Button variant="outline" size="sm" className="rounded-lg" onClick={onAbandon}>
                {t.suspended.abandon}
              </Button>
              <Button size="sm" className="gap-1.5 rounded-lg" onClick={onResume}>
                <Play className="size-3.5" />
                {t.suspended.resume}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Input */}
      <ChatInput
        onSend={onSend}
        onStop={onStop}
        onSuspend={onSuspend}
        disabled={generating || !!(pendingTools && pendingTools.length > 0)}
        approvalPending={!generating && !!(pendingTools && pendingTools.length > 0)}
        canSuspend={generating}
        thinkingEnabled={thinkingEnabled}
      />
    </main>
  )
}
