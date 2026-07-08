import { create } from "zustand"

import { getTasks, listSchedules } from "@/api/agent"
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
    schedulerTimer = setInterval(tick, 3000)
  },

  stopSchedulerPolling: () => {
    if (schedulerTimer) {
      clearInterval(schedulerTimer)
      schedulerTimer = null
    }
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
