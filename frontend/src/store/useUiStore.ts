import { create } from "zustand"

import { attachGlobalFeed, getTasks, listSchedules } from "@/api/agent"
import { detectLang, getDict, type Dict, type Lang } from "@/lib/i18n"
import type { ScheduleListResponse, TaskListResponse } from "@/types/agent-api"

export type Theme = "light" | "dark" | "system"

const LANG_KEY = "macagent-lang"
const THEME_KEY = "macagent-theme"

function loadLang(): Lang {
  const s = localStorage.getItem(LANG_KEY)
  return s === "zh" || s === "ja" || s === "en" ? s : detectLang()
}

function loadTheme(): Theme {
  const s = localStorage.getItem(THEME_KEY)
  return s === "light" || s === "dark" || s === "system" ? s : "system"
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
  /** 定时任务清单（落盘实体），驱动侧栏 Schedules 条（更低频轮询）。 */
  schedules: ScheduleListResponse | null
  setLang: (l: Lang) => void
  setTheme: (t: Theme) => void
  startSchedulerPolling: () => void
  stopSchedulerPolling: () => void
  startSchedulePolling: () => void
  stopSchedulePolling: () => void
  /** 操作后（启停/运行/删除）立刻刷一次清单，不等下个轮询周期。 */
  refreshSchedules: () => void
}

let schedulerTimer: ReturnType<typeof setInterval> | null = null
let scheduleTimer: ReturnType<typeof setInterval> | null = null

// ---- 全局任务 feed(Phase 3b):跨会话生命周期事件的长连,断线静默退避重连 ----
let feedCtl: AbortController | null = null

function startFeed(onLifecycle: () => void) {
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
    while (!ctl.signal.aborted) {
      try {
        for await (const item of attachGlobalFeed(cursor, ctl.signal)) {
          backoff = 1000
          if (typeof item.feed_seq === "number" && item.feed_seq > 0) {
            if (item.feed_seq <= cursor) continue
            cursor = item.feed_seq
          }
          if (item.type !== "stream_attached") refresh()
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
function syncSuspendedSessions(t: TaskListResponse) {
  void import("@/store/useChatStore").then(({ useChatStore, locallyAbandonedSuspended }) => {
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
  schedules: null,

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
    const tick = () => {
      getTasks()
        .then((t) => {
          set({ tasks: t })
          syncSuspendedSessions(t)
        })
        .catch(() => set({ tasks: null }))
    }
    tick()
    // Phase 3b:全局 feed 驱动——生命周期事件到达即刷新快照(去抖),
    // 固定轮询降级为 30s 慢对账兜底(断线/漏事件自愈)。/tasks 快照仍是
    // 数据权威(waiting/running 秒数等),feed 只当"该刷新了"的门铃。
    schedulerTimer = setInterval(tick, 30000)
    startFeed(tick)
  },

  stopSchedulerPolling: () => {
    if (schedulerTimer) {
      clearInterval(schedulerTimer)
      schedulerTimer = null
    }
    stopFeed()
  },

  startSchedulePolling: () => {
    if (scheduleTimer) return
    const tick = () => {
      listSchedules()
        .then((s) => set({ schedules: s }))
        .catch(() => set({ schedules: null }))
    }
    tick()
    scheduleTimer = setInterval(tick, 10000)
  },

  stopSchedulePolling: () => {
    if (scheduleTimer) {
      clearInterval(scheduleTimer)
      scheduleTimer = null
    }
  },

  refreshSchedules: () => {
    listSchedules()
      .then((s) => set({ schedules: s }))
      .catch(() => { /* 下个轮询周期自愈 */ })
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
