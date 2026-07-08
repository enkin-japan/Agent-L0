"use client"

import { useEffect, useState } from "react"
import {
  Boxes,
  Brain,
  CheckCircle2,
  Circle,
  CircleSlash,
  Database,
  ListTodo,
  Loader2,
  Presentation,
  ScrollText,
  ShieldAlert,
  Wrench,
  X,
  XCircle,
} from "lucide-react"

import { DeckPreview } from "@/components/deck/deck-preview"

import { getMemoryStatus, getSystemPrompt, getTools } from "@/api/agent"
import { cn } from "@/lib/utils"
import type { Conversation } from "@/lib/agent-data"
import { useT } from "@/store/useUiStore"
import type {
  MemoryStatusResponse,
  SystemPromptResponse,
  TaskSnapshot,
  TaskStep,
  ToolInfo,
} from "@/types/agent-api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

type Tab = "plan" | "deck" | "memory" | "context"

/**
 * 右侧检视面板：把后端既有机理提升为一等公民。
 *  - Plan（pillar B）：多步任务计划常驻可见（后端 /task 已提供 TaskState 快照）。
 *  - Memory（pillar C）：有界记忆可视化——在用上下文 vs 折叠阈值、压缩摘要、RAG 沉淀。
 *  - Context：会话系统提示词 + 模型可用工具（原 context-panel 内容）。
 */
export function InspectorPanel({
  conversation,
  taskProgress,
  onClose,
}: {
  conversation: Conversation
  taskProgress: TaskSnapshot | null
  onClose: () => void
}) {
  const t = useT()
  const [tab, setTab] = useState<Tab>("plan")

  return (
    <aside className="flex h-full w-80 max-w-[85vw] flex-col border-l border-border bg-background">
      <header className="flex items-center justify-between border-b border-border px-4 py-3.5">
        <h2 className="text-sm font-semibold tracking-tight">{t.inspector.title}</h2>
        <Button
          variant="ghost"
          size="icon"
          className="size-7 rounded-lg text-muted-foreground"
          onClick={onClose}
        >
          <X className="size-4" />
          <span className="sr-only">{t.inspector.close}</span>
        </Button>
      </header>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border px-2 py-2">
        <TabButton icon={ListTodo} label={t.inspector.tabPlan} active={tab === "plan"} onClick={() => setTab("plan")} />
        <TabButton icon={Presentation} label={t.inspector.tabDeck} active={tab === "deck"} onClick={() => setTab("deck")} />
        <TabButton icon={Brain} label={t.inspector.tabMemory} active={tab === "memory"} onClick={() => setTab("memory")} />
        <TabButton icon={Boxes} label={t.inspector.tabContext} active={tab === "context"} onClick={() => setTab("context")} />
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4 py-4">
        {tab === "plan" && <PlanTab task={taskProgress} />}
        {tab === "deck" && <DeckPreview sessionId={conversation.id} />}
        {tab === "memory" && <MemoryTab sessionId={conversation.id} />}
        {tab === "context" && <ContextTab sessionId={conversation.id} />}
      </div>
    </aside>
  )
}

function TabButton({
  icon: Icon,
  label,
  active,
  onClick,
}: {
  icon: typeof ListTodo
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex flex-1 items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium transition-colors",
        active ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-accent/50",
      )}
    >
      <Icon className="size-3.5" />
      {label}
    </button>
  )
}

// ── Plan ─────────────────────────────────────────────────────────────────
function StepIcon({ status }: { status: TaskStep["status"] }) {
  switch (status) {
    case "done":
      return <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-chart-2" />
    case "doing":
      return <Loader2 className="mt-0.5 size-3.5 shrink-0 animate-spin text-chart-1" />
    case "failed":
      return <XCircle className="mt-0.5 size-3.5 shrink-0 text-destructive" />
    case "skipped":
      return <CircleSlash className="mt-0.5 size-3.5 shrink-0 text-muted-foreground/50" />
    default:
      return <Circle className="mt-0.5 size-3.5 shrink-0 text-muted-foreground/40" />
  }
}

function PlanTab({ task }: { task: TaskSnapshot | null }) {
  const t = useT()
  if (!task || task.plan.length === 0) {
    return <EmptyHint>{t.inspector.planNone}</EmptyHint>
  }
  const done = task.plan.filter((s) => s.status === "done").length
  return (
    <div className="flex flex-col gap-3">
      {task.goal && (
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t.inspector.planGoal}
          </p>
          <p className="mt-1 text-sm leading-relaxed">{task.goal}</p>
        </div>
      )}

      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <span className="font-medium text-foreground/80">{done}/{task.plan.length}</span>
        <span>·</span>
        <span>{t.inspector.planReplan(task.replan_count)}</span>
        <span>·</span>
        <span>{t.inspector.planIter(task.step_iterations)}</span>
      </div>

      <ul className="flex flex-col gap-2">
        {task.plan.map((step) => (
          <li key={step.id} className="flex items-start gap-2 text-xs leading-relaxed">
            <StepIcon status={step.status} />
            <span
              className={cn(
                "min-w-0",
                (step.status === "done" || step.status === "skipped") &&
                  "text-muted-foreground/60 line-through decoration-muted-foreground/30",
                step.status === "failed" && "text-destructive/80",
              )}
            >
              {step.title}
              {step.note ? <span className="text-muted-foreground/60"> — {step.note}</span> : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ── Memory ───────────────────────────────────────────────────────────────
function MemoryTab({ sessionId }: { sessionId: string }) {
  const t = useT()
  const [state, setState] = useState<{ id: string; data: MemoryStatusResponse | null; error: boolean }>({
    id: sessionId,
    data: null,
    error: false,
  })
  const data = state.id === sessionId ? state.data : null
  const error = state.id === sessionId && state.error

  useEffect(() => {
    let cancelled = false
    getMemoryStatus(sessionId)
      .then((r) => { if (!cancelled) setState({ id: sessionId, data: r, error: false }) })
      .catch(() => { if (!cancelled) setState({ id: sessionId, data: null, error: true }) })
    return () => { cancelled = true }
  }, [sessionId])

  if (error) return <EmptyHint tone="error">{t.inspector.memError}</EmptyHint>
  if (!data) return <EmptyHint>{t.inspector.memLoading}</EmptyHint>

  const high = Math.max(1, data.high_threshold)
  const ratio = Math.min(1, data.last_prompt_tokens / high)
  const barColor = ratio >= 1 ? "bg-chart-1" : ratio >= 0.7 ? "bg-chart-3" : "bg-chart-2"

  return (
    <div className="flex flex-col gap-4">
      {!data.loaded && <EmptyHint tone="muted">{t.inspector.memCold}</EmptyHint>}

      {/* 在用上下文 vs 折叠阈值 */}
      <section>
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium">{t.inspector.memLiveContext}</span>
          <span className="font-mono text-[11px] text-muted-foreground">
            {t.inspector.memTokens(data.last_prompt_tokens)}
          </span>
        </div>
        <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-muted">
          <div className={cn("h-full rounded-full transition-all", barColor)} style={{ width: `${ratio * 100}%` }} />
        </div>
        <div className="mt-1 flex items-center justify-between text-[10px] text-muted-foreground/70">
          <span>{t.inspector.memFoldThreshold}</span>
          <span className="font-mono">{t.inspector.memTokens(data.high_threshold)}</span>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground/80">
          {t.inspector.memContextHint}
        </p>
      </section>

      {/* 压缩记忆 */}
      <section className="rounded-xl border border-border bg-muted/30 px-3 py-2.5">
        <p className="flex items-center gap-1.5 text-xs font-medium">
          <Database className="size-3.5 text-muted-foreground" />
          {t.inspector.memSummary}
        </p>
        <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground/85">
          {data.has_summary
            ? t.inspector.memHasSummary(data.summarized_turns, data.summary_chars)
            : t.inspector.memNoSummary}
        </p>
        <p className="mt-1.5 text-[10px] text-muted-foreground/60">{t.inspector.memTotalTurns(data.total_turns)}</p>
      </section>

      {/* RAG 沉淀 */}
      <p className="text-[11px] leading-relaxed text-muted-foreground/75">
        {data.sediment_to_rag ? t.inspector.memSediment : t.inspector.memNoSediment}
      </p>
    </div>
  )
}

// ── Context（系统提示词 + 工具） ────────────────────────────────────────────
function ContextTab({ sessionId }: { sessionId: string }) {
  const t = useT()
  const [tools, setTools] = useState<ToolInfo[] | null>(null)
  const [toolsError, setToolsError] = useState(false)
  const [promptState, setPromptState] = useState<{ id: string; data: SystemPromptResponse | null; error: boolean } | null>(null)
  const prompt = promptState?.id === sessionId ? promptState.data : null
  const promptError = promptState?.id === sessionId && promptState.error

  useEffect(() => {
    let cancelled = false
    getTools()
      .then((r) => { if (!cancelled) setTools(r.tools) })
      .catch(() => { if (!cancelled) setToolsError(true) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    let cancelled = false
    getSystemPrompt(sessionId)
      .then((r) => { if (!cancelled) setPromptState({ id: sessionId, data: r, error: false }) })
      .catch(() => { if (!cancelled) setPromptState({ id: sessionId, data: null, error: true }) })
    return () => { cancelled = true }
  }, [sessionId])

  return (
    <div className="flex flex-col gap-6">
      <section>
        <div className="flex items-center gap-1.5">
          <ScrollText className="size-3.5 text-muted-foreground" />
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t.inspector.ctxSystemPrompt}
          </h3>
          {prompt && (
            <Badge variant="secondary" className="rounded-full text-[10px] font-normal">
              {prompt.source === "default" ? t.inspector.ctxSourceDefault : t.inspector.ctxSourceSession}
            </Badge>
          )}
        </div>
        <div className="mt-2 rounded-xl border border-border bg-muted/30 px-3 py-2.5">
          {promptError ? (
            <p className="text-xs text-destructive">{t.inspector.ctxPromptError}</p>
          ) : prompt ? (
            <p className="whitespace-pre-wrap text-xs leading-relaxed text-foreground/80">
              {prompt.system_prompt.trim()}
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">{t.inspector.loading}</p>
          )}
        </div>
      </section>

      <section>
        <div className="flex items-center gap-1.5">
          <Wrench className="size-3.5 text-muted-foreground" />
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {tools ? t.inspector.ctxToolsCount(tools.length) : t.inspector.ctxTools}
          </h3>
        </div>
        <div className="mt-2 flex flex-col gap-2">
          {toolsError ? (
            <p className="text-xs text-destructive">{t.inspector.ctxToolsError}</p>
          ) : tools === null ? (
            <p className="text-xs text-muted-foreground">{t.inspector.loading}</p>
          ) : tools.length === 0 ? (
            <p className="text-xs text-muted-foreground">{t.inspector.ctxNoTools}</p>
          ) : (
            tools.map((tool) => {
              const paramEntries = Object.entries(tool.parameters.properties ?? {})
              return (
                <div key={tool.name} className="rounded-xl border border-border bg-muted/30 px-3 py-2.5">
                  <div className="flex items-center gap-1.5">
                    <span className="font-mono text-xs font-medium">{tool.name}</span>
                    {tool.requires_approval && (
                      <Badge
                        variant="secondary"
                        className="gap-1 rounded-full border-chart-3/40 bg-chart-3/10 text-[10px] font-normal text-chart-3"
                      >
                        <ShieldAlert className="size-3" />
                        {t.inspector.ctxNeedsApproval}
                      </Badge>
                    )}
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{tool.description}</p>
                  {paramEntries.length > 0 && (
                    <details className="mt-1.5">
                      <summary className="cursor-pointer select-none text-xs text-muted-foreground/70 hover:text-foreground">
                        {t.inspector.ctxParams(paramEntries.length)}
                      </summary>
                      <ul className="mt-1.5 flex flex-col gap-1.5">
                        {paramEntries.map(([key, p]) => (
                          <li key={key} className="text-xs leading-relaxed">
                            <span className="font-mono">{key}</span>
                            {tool.parameters.required?.includes(key) && (
                              <span className="text-destructive">*</span>
                            )}
                            {p.description && <span className="text-muted-foreground"> — {p.description}</span>}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              )
            })
          )}
        </div>
      </section>
    </div>
  )
}

function EmptyHint({ children, tone = "default" }: { children: React.ReactNode; tone?: "default" | "error" | "muted" }) {
  return (
    <p
      className={cn(
        "rounded-xl border px-3 py-2.5 text-xs leading-relaxed",
        tone === "error"
          ? "border-destructive/30 bg-destructive/10 text-destructive"
          : tone === "muted"
            ? "border-chart-3/30 bg-chart-3/10 text-chart-3"
            : "border-border bg-muted/30 text-muted-foreground",
      )}
    >
      {children}
    </p>
  )
}
