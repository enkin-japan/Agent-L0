import { useEffect, useState } from "react"
import { CalendarClock, Loader2, Play, Square, Trash2, MessageSquareText } from "lucide-react"

import {
  cancelScheduleRun,
  deleteSchedule,
  getScheduleRuns,
  runScheduleNow,
  toggleSchedule,
} from "@/api/agent"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { useT, useUiStore } from "@/store/useUiStore"
import type { ScheduleRun, ScheduleSpec, ScheduledTask } from "@/types/agent-api"

/**
 * 定时任务条（侧栏，置于 Worker 状态条下方）：落盘任务实体的常显列表。
 * 创建/编辑走对话（schedule_* 工具 + 审批卡片）；这里是不经过模型的直接控制面——
 * 启停 / 立即运行 / 中止本次运行 / 删除 / 运行历史（点击跳转专属会话查看全文）。
 * 数据来自 useUiStore 的 10s 轮询（服务端落盘状态是唯一真相，天然免疫模型谎报）。
 */
export function ScheduleStrip({
  onOpenSession,
}: {
  onOpenSession: (sessionId: string, title: string) => void
}) {
  const t = useT()
  const schedules = useUiStore((s) => s.schedules)
  const [detailId, setDetailId] = useState<string | null>(null)

  const items = schedules?.schedules ?? []
  if (items.length === 0) return null // 无任务时不占侧栏空间（入口是对话，不需要空态引导常驻）

  const detail = items.find((x) => x.id === detailId) ?? null

  return (
    <div className="rounded-2xl border border-border bg-card/60 p-3">
      <div className="flex items-center gap-2 px-0.5">
        <CalendarClock className="size-3.5 text-muted-foreground" />
        <span className="text-xs font-semibold tracking-wide">{t.schedule.title}</span>
        <span className="ml-auto text-[11px] text-muted-foreground">{items.length}</span>
      </div>
      <ul className="mt-2 flex flex-col gap-1">
        {items.map((task) => (
          <li key={task.id}>
            <button
              type="button"
              onClick={() => setDetailId(task.id)}
              className="flex w-full items-center gap-1.5 rounded-lg px-1.5 py-1 text-left transition-colors hover:bg-accent/60"
            >
              {task.running ? (
                <Loader2 className="size-3 shrink-0 animate-spin text-chart-1" />
              ) : (
                <span
                  className={cn(
                    "size-1.5 shrink-0 rounded-full",
                    task.enabled ? "bg-chart-2" : "bg-muted-foreground/40",
                  )}
                />
              )}
              <span className="min-w-0 flex-1 truncate text-[11px] font-medium">{task.name}</span>
              <span className="shrink-0 text-[10px] text-muted-foreground/70">
                {task.running
                  ? t.schedule.runningNow
                  : task.enabled
                    ? t.schedule.nextRun(formatTs(task.next_run))
                    : t.schedule.paused}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {detail && (
        <ScheduleDetailDialog
          task={detail}
          onClose={() => setDetailId(null)}
          onOpenSession={onOpenSession}
        />
      )}
    </div>
  )
}

/** epoch 秒 → 本地简洁时刻（今天只显示 HH:MM，其余带月日）。 */
function formatTs(ts: number | null): string {
  if (!ts) return "—"
  const d = new Date(ts * 1000)
  const now = new Date()
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`
  if (d.toDateString() === now.toDateString()) return hm
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`
}

function specLabel(spec: ScheduleSpec, t: ReturnType<typeof useT>): string {
  if (spec.type === "interval") return t.schedule.specInterval(spec.minutes ?? 0)
  if (spec.type === "daily") return t.schedule.specDaily(spec.at ?? "")
  if (spec.type === "weekly")
    return t.schedule.specWeekly(t.schedule.weekdays[spec.weekday ?? 0] ?? "", spec.at ?? "")
  return t.schedule.specOnce(spec.at ?? "")
}

function runStatusLabel(status: string, t: ReturnType<typeof useT>): string {
  if (status === "success") return t.schedule.statusSuccess
  if (status === "fail") return t.schedule.statusFail
  if (status === "cancelled") return t.schedule.statusCancelled
  if (status === "denied_approval") return t.schedule.statusDenied
  return t.schedule.statusWarning
}

function runStatusTone(status: string): string {
  if (status === "success") return "bg-chart-2/15 text-chart-2"
  if (status === "fail") return "bg-destructive/15 text-destructive"
  if (status === "cancelled") return "bg-muted text-muted-foreground"
  return "bg-chart-3/15 text-chart-3"
}

function ScheduleDetailDialog({
  task,
  onClose,
  onOpenSession,
}: {
  task: ScheduledTask
  onClose: () => void
  onOpenSession: (sessionId: string, title: string) => void
}) {
  const t = useT()
  const refreshSchedules = useUiStore((s) => s.refreshSchedules)
  const [runs, setRuns] = useState<ScheduleRun[] | null>(null)

  // 打开即拉取运行历史（任务在运行中时随轮询周期性刷新）
  useEffect(() => {
    let stale = false
    getScheduleRuns(task.id)
      .then((r) => { if (!stale) setRuns(r.runs) })
      .catch(() => { if (!stale) setRuns([]) })
    return () => { stale = true }
  }, [task.id, task.running, task.last_run?.run_id])

  function act(fn: () => Promise<unknown>) {
    void fn().finally(refreshSchedules)
  }

  const sessionId = `schedule_${task.id}`

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-6">
            <CalendarClock className="size-4 shrink-0 text-muted-foreground" />
            <span className="min-w-0 truncate">{task.name}</span>
          </DialogTitle>
          <DialogDescription>{specLabel(task.spec, t)} · {t.schedule.nextRun(formatTs(task.next_run))}</DialogDescription>
        </DialogHeader>

        {/* 任务指令（模型每次运行执行的原文） */}
        <div>
          <p className="mb-1 text-xs font-semibold text-muted-foreground">{t.schedule.promptLabel}</p>
          <p className="max-h-24 overflow-y-auto whitespace-pre-wrap rounded-lg bg-muted/50 p-2 text-xs leading-relaxed">
            {task.prompt}
          </p>
          <p className="mt-1 text-[11px] text-muted-foreground/70">{t.schedule.hint}</p>
        </div>

        {/* 直接控制：启停 / 立即运行 / 中止 / 删除 / 打开专属会话 */}
        <div className="flex flex-wrap items-center gap-2">
          <label className="mr-auto flex items-center gap-2 text-xs">
            <Switch
              checked={task.enabled}
              onCheckedChange={(v) => act(() => toggleSchedule(task.id, v))}
            />
            {t.schedule.enableLabel}
          </label>
          {task.running ? (
            <Button
              variant="outline"
              size="xs"
              onClick={() => act(() => cancelScheduleRun(task.id))}
            >
              <Square className="size-3" /> {t.schedule.cancelRun}
            </Button>
          ) : (
            <Button
              variant="outline"
              size="xs"
              onClick={() => act(() => runScheduleNow(task.id))}
            >
              <Play className="size-3" /> {t.schedule.runNow}
            </Button>
          )}
          <Button
            variant="destructive"
            size="xs"
            onClick={() => {
              if (!window.confirm(t.confirm.deleteSchedule)) return
              act(() => deleteSchedule(task.id))
              onClose()
            }}
          >
            <Trash2 className="size-3" /> {t.schedule.delete}
          </Button>
        </div>

        <Button
          variant="secondary"
          size="sm"
          className="w-full justify-center gap-2"
          onClick={() => {
            onOpenSession(sessionId, task.name)
            onClose()
          }}
        >
          <MessageSquareText className="size-3.5" /> {t.schedule.openSession}
        </Button>

        {/* 运行历史（新在前）：状态徽章 + 时刻 + 耗时 + 摘要 */}
        <div>
          <p className="mb-1 text-xs font-semibold text-muted-foreground">{t.schedule.history}</p>
          {runs === null ? (
            <Loader2 className="mx-auto my-2 size-4 animate-spin text-muted-foreground" />
          ) : runs.length === 0 ? (
            <p className="text-xs text-muted-foreground/70">{t.schedule.historyEmpty}</p>
          ) : (
            <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto">
              {runs.map((run) => (
                <li key={run.run_id} className="rounded-lg bg-muted/40 px-2 py-1.5">
                  <div className="flex items-center gap-2">
                    <span className={cn("rounded-full px-1.5 py-0.5 text-[10px] font-medium", runStatusTone(run.status))}>
                      {runStatusLabel(run.status, t)}
                    </span>
                    <span className="text-[11px] text-muted-foreground">{formatTs(run.started_at)}</span>
                    {run.finished_at != null && (
                      <span className="ml-auto text-[10px] text-muted-foreground/70">
                        {Math.max(1, Math.round(run.finished_at - run.started_at))}s
                      </span>
                    )}
                  </div>
                  {run.summary && (
                    <p className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-muted-foreground">
                      {run.summary}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
