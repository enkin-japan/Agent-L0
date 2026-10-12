import { create } from "zustand"

import { attachGlobalFeed, getTasks, getSessionFiles, HttpError, listSchedules, getScheduleRuns,
  toggleSchedule, runScheduleNow, cancelScheduleRun, deleteSchedule } from "@/api/agent"
import { feedFence, type FeedFenceState } from "@/lib/feed-fence"
import { detectLang, getDict, type Dict, type Lang } from "@/lib/i18n"
import type { ScheduleListResponse, ScheduleRun, SessionFile, TaskListResponse } from "@/types/agent-api"

export type Theme = "light" | "dark" | "system"
export type SnapshotState = "unknown" | "fresh" | "stale"
export type SnapshotError = "unauthorized" | "unavailable" | null

const LANG_KEY = "macagent-lang"
const THEME_KEY = "macagent-theme"

function loadLang(): Lang {
  try {
    const s = localStorage.getItem(LANG_KEY)
    if (s === "zh" || s === "ja" || s === "en") return s
  } catch { /* 浏览器可能禁止存储；偏好缺席不应阻止页面加载。 */ }
  return detectLang()
}

function loadTheme(): Theme {
  try {
    const s = localStorage.getItem(THEME_KEY)
    if (s === "light" || s === "dark" || s === "system") return s
  } catch { /* 禁止存储时使用系统外观。 */ }
  return "system"
}

/** 应用主题到 <html>：显式 light/dark 覆写；system 两个类都不加，交给 CSS 的 prefers-color-scheme。 */
export function applyTheme(theme: Theme) {
  const el = document.documentElement
  el.classList.remove("light", "dark")
  if (theme === "light") el.classList.add("light")
  else if (theme === "dark") el.classList.add("dark")
}

interface UiState {
  lang: Lang
  theme: Theme
  /** 全局单 worker 调度快照（跨会话），驱动侧栏的 Worker 状态条（低频轮询）。 */
  tasks: TaskListResponse | null
  tasksState: SnapshotState
  tasksError: SnapshotError
  /** 定时任务清单（落盘实体），驱动侧栏 Schedules 条（更低频轮询）。 */
  schedules: ScheduleListResponse | null
  schedulesState: SnapshotState
  schedulesError: SnapshotError
  scheduleRuns: { id: string; data: ScheduleRun[] | null; state: SnapshotState | "loading"; error: SnapshotError } | null
  scheduleAction: { id: string; status: "sending" | "accepted" | "rejected" | "failed"; message: string } | null
  files: { sessionId: string; data: SessionFile[] | null; state: SnapshotState | "loading"; error: SnapshotError } | null
  watchFiles: (sessionId: string) => () => void
  isWatchingFiles: (sessionId: string) => boolean
  observeFilesTerminal: (sessionId: string, seq: number) => void
  refreshFiles: () => Promise<void>
  setLang: (l: Lang) => void
  setTheme: (t: Theme) => void
  startSchedulerPolling: () => void
  stopSchedulerPolling: () => void
  /** 操作后（启停/运行/删除）立刻刷一次清单，不等下个轮询周期。 */
  refreshSchedules: () => Promise<void>
  watchScheduleRuns: (id: string) => () => void
  refreshScheduleRuns: () => Promise<void>
  actSchedule: (id: string, action: "toggle" | "run" | "cancel" | "delete", enabled?: boolean) => Promise<boolean>
}

let schedulerTimer: ReturnType<typeof setInterval> | null = null
let schedulerGeneration = 0
let tasksRequest = 0
let filesRequest = 0
let filesObserver: { sessionId: string; token: object; lastTerminalSeq: number } | null = null
let schedulesRequest = 0
let runsRequest = 0
let actionRequest = 0
let runsObserver: { id: string } | null = null

// ---- 全局任务 feed(Phase 3b):跨会话生命周期事件的长连,断线静默退避重连 ----
let feedCtl: AbortController | null = null

function startFeed(onLifecycle: (invalidateSessionPolicy?: boolean) => void) {
  if (feedCtl) return
  const ctl = new AbortController()
  feedCtl = ctl
  let refreshTimer: ReturnType<typeof setTimeout> | null = null
  const refresh = () => {
    // 去抖 300ms:一轮生命周期常带多个事件(queued+started 相邻),合并成一次快照刷新
    if (refreshTimer) clearTimeout(refreshTimer)
    refreshTimer = setTimeout(onLifecycle, 300)
  }
  void (async () => {
    let backoff = 1000
    let cursor = 0
    const fence: FeedFenceState = { generation: null }
    while (!ctl.signal.aborted) {
      try {
        let reanchor = false
        for await (const item of attachGlobalFeed(cursor, ctl.signal)) {
          backoff = 1000
          if (item.type === "stream_attached") {
            // 代际围栏（B2）：后端重启 → feed_seq 归零 → 旧游标作废，清零重连
            if (feedFence(fence, item.data?.generation) === "reanchor") {
              void import("@/store/useChatStore").then(({ useChatStore }) => {
                if (!ctl.signal.aborted) useChatStore.getState().invalidateTaskControls()
              })
              onLifecycle(true)
              cursor = 0
              reanchor = true
              break
            }
            continue
          }
          if (typeof item.feed_seq === "number" && item.feed_seq > 0) {
            if (item.feed_seq <= cursor) continue
            cursor = item.feed_seq
          }
          if (item.session_id && typeof item.seq === "number") {
            const id = item.session_id, seq = item.seq
            void import("@/store/useChatStore").then(({ useChatStore }) => {
              if (!ctl.signal.aborted) useChatStore.getState().observeTaskEvent(id,
                { type: item.type, seq, turn_id: item.turn_id ?? null })
            })
          }
          refresh()
        }
        if (reanchor) {
          // 重锚限速(首审 M8):代理轮询打到两个活后端/后端崩溃循环时,每次连接首帧
          // 都触发重锚——不加档就是无上限的热重连环。1s 足以把它压到无害。
          await new Promise((r) => setTimeout(r, 1000))
          continue // 带新游标(0)重连,不是正常收流
        }
        break // 服务端正常收流
      } catch {
        if (ctl.signal.aborted) break
        await new Promise((r) => setTimeout(r, backoff))
        backoff = Math.min(backoff * 2, 15000)
      }
    }
    if (feedCtl === ctl) feedCtl = null
  })()
}

function stopFeed() {
  feedCtl?.abort()
  feedCtl = null
}

/**
 * 用 /tasks 快照对账挂起会话集合（后端调度器是唯一权威）：
 * 刷新页面后本地 suspendedSessions 丢失，而后端任务仍挂着——不对账的话恢复入口就此消失。
 * 动态 import 避免 useUiStore ↔ useChatStore 模块级循环依赖。
 */
function syncSuspendedSessions(t: TaskListResponse, isCurrent: () => boolean) {
  void import("@/store/useChatStore").then(({ useChatStore, locallyAbandonedSuspended }) => {
    if (!isCurrent()) return
    const backendSuspended = new Set<string>()
    for (const h of t.tasks ?? []) {
      if (h.status === "suspended" && typeof h.session_id === "string") {
        backendSuspended.add(h.session_id)
      }
    }
    // 本地已放弃的：后端句柄回收前跳过；后端不再挂起的清出放弃集合（对账权威回归）
    for (const id of [...locallyAbandonedSuspended]) {
      if (!backendSuspended.has(id)) locallyAbandonedSuspended.delete(id)
    }
    const authoritative: Record<string, boolean> = {}
    for (const id of backendSuspended) {
      if (!locallyAbandonedSuspended.has(id)) authoritative[id] = true
    }
    const current = useChatStore.getState().suspendedSessions
    const same =
      Object.keys(current).length === Object.keys(authoritative).length &&
      Object.keys(authoritative).every((k) => current[k])
    if (!same) useChatStore.setState({ suspendedSessions: authoritative })
  })
}

export const useUiStore = create<UiState>((set) => ({
  lang: loadLang(),
  theme: loadTheme(),
  tasks: null,
  tasksState: "unknown",
  tasksError: null,
  schedules: null,
  schedulesState: "unknown",
  schedulesError: null,
  scheduleRuns: null,
  scheduleAction: null,
  files: null,

  watchFiles: (sessionId) => {
    const observer = { sessionId, token: {}, lastTerminalSeq: 0 }
    filesObserver = observer
    void useUiStore.getState().refreshFiles()
    return () => { if (filesObserver === observer) { filesObserver = null; filesRequest++ } }
  },
  isWatchingFiles: (sessionId) => filesObserver?.sessionId === sessionId,
  observeFilesTerminal: (sessionId, seq) => {
    const observer = filesObserver
    // Snapshot cursors describe task projection, not delivery of a file-refresh
    // doorbell. Retain only this observer's actually consumed terminal watermark.
    if (!observer || observer.sessionId !== sessionId || !Number.isFinite(seq) || seq <= observer.lastTerminalSeq) return
    observer.lastTerminalSeq = seq
    void useUiStore.getState().refreshFiles()
  },
  refreshFiles: async () => {
    const observer = filesObserver
    if (!observer) return
    const request = ++filesRequest
    const { useChatStore: chat } = await import("@/store/useChatStore")
    const version = chat.getState().selectionVersion
    const current = () => filesObserver === observer && filesRequest === request
      && chat.getState().activeId === observer.sessionId && chat.getState().selectionVersion === version
    if (!current()) return
    set((s) => ({ files: { sessionId: observer.sessionId, data: s.files?.sessionId === observer.sessionId ? s.files.data : null,
      state: "loading", error: null } }))
    try {
      const response = await getSessionFiles(observer.sessionId)
      if (!current()) return
      if (!response.status || !Array.isArray(response.files)) throw new Error("File snapshot unavailable")
      set({ files: { sessionId: observer.sessionId, data: response.files, state: "fresh", error: null } })
    } catch (error) {
      if (!current()) return
      set((s) => ({ files: { sessionId: observer.sessionId, data: s.files?.data ?? null,
        state: s.files?.data !== null ? "stale" : "unknown", error: error instanceof HttpError && [401, 403].includes(error.status) ? "unauthorized" : "unavailable" } }))
    }
  },

  setLang: (lang) => {
    try { localStorage.setItem(LANG_KEY, lang) } catch { /* 忽略配额错误 */ }
    set({ lang })
  },

  setTheme: (theme) => {
    try { localStorage.setItem(THEME_KEY, theme) } catch { /* 忽略配额错误 */ }
    applyTheme(theme)
    set({ theme })
  },

  startSchedulerPolling: () => {
    if (schedulerTimer) return
    const generation = ++schedulerGeneration
    set((s) => ({ tasksState: s.tasks ? "stale" : "unknown", schedulesState: s.schedules ? "stale" : "unknown" }))
    const tick = (invalidateSessionPolicy = false) => {
      if (generation !== schedulerGeneration) return
      const request = ++tasksRequest
      const isCurrent = () => generation === schedulerGeneration && request === tasksRequest
      if (invalidateSessionPolicy) {
        set((s) => ({ tasksState: s.tasks ? "stale" : "unknown", schedulesState: s.schedules ? "stale" : "unknown" }))
      }
      // The existing feed doorbell + 30s fallback also refresh schedule definitions/runs.
      void useUiStore.getState().refreshSchedules()
      // Reuse the existing feed + slow snapshot loop for session metadata too.
      // A new backend generation must not keep advertising the previous policy.
      void import("@/store/useChatStore").then(({ useChatStore }) => {
        if (generation !== schedulerGeneration) return
        if (invalidateSessionPolicy) useChatStore.setState({ retentionPolicy: null })
        void useChatStore.getState().reconcileSessions()
      })
      getTasks()
        .then((t) => {
          if (!isCurrent()) return
          if (!t.status) throw new Error("Task snapshot unavailable")
          set({ tasks: t, tasksState: "fresh", tasksError: null })
          syncSuspendedSessions(t, isCurrent)
        })
        .catch((error: unknown) => {
          if (!isCurrent()) return
          set((s) => ({ tasksState: s.tasks ? "stale" : "unknown",
            tasksError: error instanceof HttpError && [401, 403].includes(error.status)
              ? "unauthorized" : "unavailable" }))
        })
    }
    tick()
    // Phase 3b:全局 feed 驱动——生命周期事件到达即刷新快照(去抖),
    // 固定轮询降级为 30s 慢对账兜底(断线/漏事件自愈)。/tasks 快照仍是
    // 数据权威(waiting/running 秒数等),feed 只当"该刷新了"的门铃。
    schedulerTimer = setInterval(tick, 30000)
    startFeed(tick)
  },

  stopSchedulerPolling: () => {
    schedulerGeneration += 1
    if (schedulerTimer) {
      clearInterval(schedulerTimer)
      schedulerTimer = null
    }
    stopFeed()
    set((s) => ({ schedulesState: s.schedules ? "stale" : "unknown" }))
  },

  refreshSchedules: async () => {
    const request = ++schedulesRequest, generation = schedulerGeneration
    const current = () => request === schedulesRequest && generation === schedulerGeneration
    try {
      const response = await listSchedules()
      if (!current()) return
      if (!response.status || !Array.isArray(response.schedules)) throw new Error("Schedule snapshot unavailable")
      set({ schedules: response, schedulesState: "fresh", schedulesError: null })
      if (runsObserver) void useUiStore.getState().refreshScheduleRuns()
    } catch (error) {
      if (!current()) return
      set((s) => ({ schedulesState: s.schedules ? "stale" : "unknown",
        schedulesError: error instanceof HttpError && [401, 403].includes(error.status) ? "unauthorized" : "unavailable" }))
    }
  },
  watchScheduleRuns: (id) => {
    const observer = { id }; runsObserver = observer; actionRequest++
    set({ scheduleAction: null })
    void useUiStore.getState().refreshScheduleRuns()
    return () => { if (runsObserver === observer) { runsObserver = null; runsRequest++; actionRequest++ } }
  },
  refreshScheduleRuns: async () => {
    const observer = runsObserver
    if (!observer) return
    const request = ++runsRequest, generation = schedulerGeneration
    const current = () => runsObserver === observer && request === runsRequest && generation === schedulerGeneration
    set((s) => ({ scheduleRuns: { id: observer.id, data: s.scheduleRuns?.id === observer.id ? s.scheduleRuns.data : null,
      state: "loading", error: null } }))
    try {
      const response = await getScheduleRuns(observer.id)
      if (!current()) return
      if (!response.status || !Array.isArray(response.runs)) throw new Error("Run history unavailable")
      set({ scheduleRuns: { id: observer.id, data: response.runs, state: "fresh", error: null } })
    } catch (error) {
      if (!current()) return
      set((s) => ({ scheduleRuns: { id: observer.id, data: s.scheduleRuns?.data ?? null,
        state: s.scheduleRuns?.data !== null ? "stale" : "unknown",
        error: error instanceof HttpError && [401, 403].includes(error.status) ? "unauthorized" : "unavailable" } }))
    }
  },
  actSchedule: async (id, action, enabled) => {
    const observer = runsObserver
    if (!observer || observer.id !== id || useUiStore.getState().scheduleAction?.status === "sending") return false
    const request = ++actionRequest, generation = schedulerGeneration
    const current = () => runsObserver === observer && request === actionRequest && generation === schedulerGeneration
    const feedback = (status: NonNullable<UiState["scheduleAction"]>["status"], message: string) => {
      if (current()) set({ scheduleAction: { id, status, message } })
    }
    if (useUiStore.getState().schedulesState !== "fresh") { feedback("rejected", currentDict().management.snapshotUnknown); return false }
    feedback("sending", currentDict().management.sending)
    try {
      const chat = await import("@/store/useChatStore")
      let response
      try {
        response = await chat.useChatStore.getState().withSessionWrite(`schedule_${id}`, () =>
          action === "toggle" ? toggleSchedule(id, !!enabled) : action === "run" ? runScheduleNow(id)
            : action === "cancel" ? cancelScheduleRun(id) : deleteSchedule(id))
      } catch (error) {
        if (error instanceof chat.PendingDeletionError) { feedback("rejected", error.message); return false }
        throw error
      }
      if (!current()) return false
      if (!response.status) { feedback("rejected", response.response || currentDict().management.rejected); return false }
      feedback("accepted", response.response || currentDict().management.accepted)
      return true
    } catch (error) {
      if (!current()) return false
      feedback("failed", currentDict().management.failed(error instanceof Error ? error.message : "HTTP failed"))
      return false
    } finally {
      // A lifecycle snapshot remains authoritative; acknowledgements do not toggle running.
      if (generation === schedulerGeneration) void useUiStore.getState().refreshSchedules()
    }
  },
}))

/** React 组件用：按当前语言取整份字典（订阅语言变化自动重渲染）。 */
export function useT(): Dict {
  return getDict(useUiStore((s) => s.lang))
}

/** 非 React（如 zustand store 内部）用：读当前语言字典。 */
export function currentDict(): Dict {
  return getDict(useUiStore.getState().lang)
}
