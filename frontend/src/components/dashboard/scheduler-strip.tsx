import { Cpu, Loader2, PauseCircle, Clock, Square } from "lucide-react"

import { cancelTask } from "@/api/agent"
import { cn } from "@/lib/utils"
import { convTitle, type Conversation } from "@/lib/agent-data"
import { useT } from "@/store/useUiStore"
import type { TaskHandleInfo, TaskListResponse } from "@/types/agent-api"

/**
 * Worker 状态条（pillar A：单 worker 协调调度的可视化）。
 * 后端 `GET /api/v1/tasks` 已提供跨会话快照——谁占用唯一的 worker、谁排队、谁挂起。
 * 这是本 Agent 的建筑签名（像单核 CPU 那样在任务间协作式轮换），把它摆到侧栏常显。
 */
export function SchedulerStrip({
  tasks,
  conversations,
  activeId,
  onSelect,
}: {
  tasks: TaskListResponse | null
  conversations: Conversation[]
  activeId: string
  onSelect: (id: string) => void
}) {
  const t = useT()
  const busy = (tasks?.running?.length ?? 0) > 0
  const items = tasks?.tasks ?? []

  function label(id: string): string {
    const conv = conversations.find((c) => c.id === id)
    return conv ? convTitle(conv.title, t.store.newChatTitle) : id.slice(0, 8)
  }

  function kindLabel(kind: string): string {
    if (kind === "chat") return t.scheduler.kindChat
    if (kind === "resume") return t.scheduler.kindResume
    if (kind === "summary") return t.scheduler.kindSummary
    return kind
  }

  return (
    <div className="rounded-2xl border border-border bg-card/60 p-3">
      <div className="flex items-center gap-2 px-0.5">
        <Cpu className={cn("size-3.5", busy ? "text-chart-1" : "text-muted-foreground")} />
        <span className="text-xs font-semibold tracking-wide">{t.scheduler.title}</span>
        <span className="ml-auto flex items-center gap-1.5">
          <span
            className={cn(
              "size-1.5 rounded-full",
              busy ? "animate-pulse bg-chart-1" : "bg-muted-foreground/40",
            )}
          />
          <span className="text-[11px] text-muted-foreground">
            {busy ? t.scheduler.busy : t.scheduler.idle}
          </span>
        </span>
      </div>

      {/* 计数摘要（有值才显示，避免噪声） */}
      {tasks && (tasks.queued > 0 || tasks.suspended > 0) && (
        <div className="mt-2 flex flex-wrap gap-1 px-0.5">
          {tasks.queued > 0 && (
            <Chip tone="amber">{tasks.queued} {t.scheduler.queued}</Chip>
          )}
          {tasks.suspended > 0 && (
            <Chip tone="amber">{tasks.suspended} {t.scheduler.suspended}</Chip>
          )}
        </div>
      )}

      {items.length === 0 ? (
        <p className="mt-2 px-0.5 text-[11px] leading-relaxed text-muted-foreground/70">
          {t.scheduler.noTasks}
        </p>
      ) : (
        <ul className="mt-2 flex flex-col gap-1">
          {items.map((task) => (
            <TaskRow
              key={task.session_id}
              task={task}
              title={label(task.session_id)}
              kind={kindLabel(task.kind)}
              isActive={task.session_id === activeId}
              onClick={() => onSelect(task.session_id)}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

function Chip({ tone, children }: { tone: "amber" | "emerald"; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        "rounded-full px-1.5 py-0.5 text-[10px] font-medium",
        tone === "amber"
          ? "bg-chart-3/15 text-chart-3"
          : "bg-chart-2/15 text-chart-2",
      )}
    >
      {children}
    </span>
  )
}

function TaskRow({
  task,
  title,
  kind,
  isActive,
  onClick,
}: {
  task: TaskHandleInfo
  title: string
  kind: string
  isActive: boolean
  onClick: () => void
}) {
  const t = useT()
  const running = task.status === "running"
  const suspended = task.status === "suspended"
  // worker 级中止入口：运行中/排队中都可杀（排队轮已持会话锁，准入后第一个检查点即取消）。
  // 会话窗口不可用时（如输入被中断丢失后的空窗口）这里是唯一的手动中止通道。
  const stoppable = running || task.status === "queued"

  const icon = running ? (
    <Loader2 className="size-3 shrink-0 animate-spin text-chart-1" />
  ) : suspended ? (
    <PauseCircle className="size-3 shrink-0 text-chart-3" />
  ) : (
    <Clock className="size-3 shrink-0 text-muted-foreground/70" />
  )

  const duration = running
    ? t.scheduler.runningFor(`${Math.round(task.running_seconds)}s`)
    : task.status === "queued"
      ? t.scheduler.waitingFor(`${Math.round(task.waiting_seconds)}s`)
      : ""

  function handleStop(e: React.MouseEvent) {
    e.stopPropagation()
    if (!window.confirm(t.confirm.stopWorkerTask)) return
    void cancelTask(task.session_id).catch(() => {})
  }

  return (
    <li>
      {/* 行与中止按钮同处一个单行容器：中止按钮以「行」为垂直定位基准。
          duration 另起一行放在容器外，避免它把 li 撑高、令 absolute top-1/2 的按钮偏下失准。 */}
      <div className="relative flex items-center">
        <button
          type="button"
          onClick={onClick}
          className={cn(
            "flex w-full items-center gap-1.5 rounded-lg px-1.5 py-1 text-left transition-colors",
            isActive ? "bg-accent" : "hover:bg-accent/60",
            stoppable && "pr-8",
          )}
        >
          {icon}
          <span className="min-w-0 flex-1 truncate text-[11px] font-medium">{title}</span>
          <span className="shrink-0 text-[10px] text-muted-foreground/70">{kind}</span>
          {isActive && (
            <span className="size-1.5 shrink-0 rounded-full bg-chart-1" title={t.scheduler.thisSession} />
          )}
        </button>
        {stoppable && (
          <button
            type="button"
            onClick={handleStop}
            title={t.scheduler.stopTask}
            className="absolute right-1.5 top-1/2 flex size-5 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground/50 transition-colors hover:bg-destructive/15 hover:text-destructive"
          >
            {/* 实心方块＝通用「停止」符号；描边空心方框会被误读成半成品/复选框 */}
            <Square className="size-2.5 fill-current" />
            <span className="sr-only">{t.scheduler.stopTask}</span>
          </button>
        )}
      </div>
      {duration && (
        <p className="pl-6 text-[10px] text-muted-foreground/60">{duration}</p>
      )}
    </li>
  )
}
