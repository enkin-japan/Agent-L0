import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import type { TaskListResponse } from "@/types/agent-api"

const empty: TaskListResponse = { status: true, running: [], queued: 0, suspended: 0, tasks: [] }
let next: () => Promise<Response>
let stop: (() => void) | undefined

beforeEach(() => {
  vi.resetModules()
  vi.useFakeTimers()
  stop = undefined
  next = async () => Response.json(empty)
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === "macagent-lang" ? "zh" : null,
    setItem: () => {} })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/tasks")) return next()
    if (url.endsWith("/sessions")) return Response.json({ status: true, sessions: [] })
    if (url.includes("/events?")) return new Response(new ReadableStream({ start(controller) {
      init?.signal?.addEventListener("abort", () => controller.close(), { once: true })
    } }))
    throw new Error(`Unexpected synthetic request ${url}`)
  }))
})
afterEach(() => { stop?.(); vi.useRealTimers(); vi.unstubAllGlobals() })

it("loads preferences safely when browser storage is unavailable", async () => {
  vi.stubGlobal("localStorage", { getItem: () => { throw new DOMException("synthetic blocked", "SecurityError") }, setItem: () => {} })
  const { useUiStore: ui } = await import("./useUiStore")
  expect(["zh", "en", "ja"]).toContain(ui.getState().lang)
  expect(ui.getState().theme).toBe("system")
})

async function setup() {
  await import("./useChatStore")
  const { useUiStore: ui } = await import("./useUiStore")
  ui.getState().startSchedulerPolling()
  stop = () => ui.getState().stopSchedulerPolling()
  await vi.advanceTimersByTimeAsync(1)
  return ui
}

it("shows unknown after initial failure instead of claiming idle", async () => {
  next = async () => new Response("synthetic unavailable", { status: 503 })
  const ui = await setup()
  expect(ui.getState().tasksState).toBe("unknown")
  const { SchedulerStrip } = await import("@/components/dashboard/scheduler-strip")
  const html = renderToStaticMarkup(createElement(SchedulerStrip, {
    tasks: ui.getState().tasks, tasksState: ui.getState().tasksState,
    tasksError: ui.getState().tasksError, conversations: [], activeId: "", onSelect: () => {},
  }))
  expect(html).toContain("任务状态未知")
  expect(html).not.toContain("Worker 空闲")
  expect(html).not.toContain("无进行中的任务")
})

it("retains last successful tasks on failure and labels the snapshot stale", async () => {
  const ui = await setup()
  expect(ui.getState().tasksState).toBe("fresh")
  const good = ui.getState().tasks
  next = async () => new Response("synthetic unauthorized", { status: 401 })
  await vi.advanceTimersByTimeAsync(30000)
  expect(ui.getState().tasks).toBe(good)
  expect(ui.getState().tasksState).toBe("stale")
  expect(ui.getState().tasksError).toBe("unauthorized")
  const { SchedulerStrip } = await import("@/components/dashboard/scheduler-strip")
  const html = renderToStaticMarkup(createElement(SchedulerStrip, {
    tasks: good, tasksState: "stale", tasksError: "unauthorized", conversations: [], activeId: "", onSelect: () => {},
  }))
  expect(html).toContain("最近已知状态")
  expect(html).toContain("鉴权")
  expect(html).not.toContain("Worker 空闲")
})

it("ignores a late older failure after a successful newer snapshot", async () => {
  let release!: (r: Response) => void
  next = () => new Promise<Response>((resolve) => { release = resolve })
  const ui = await setup()
  next = async () => Response.json(empty)
  await vi.advanceTimersByTimeAsync(30000)
  expect(ui.getState().tasksState).toBe("fresh")
  release(new Response("old failure", { status: 503 }))
  await vi.advanceTimersByTimeAsync(1)
  expect(ui.getState().tasksState).toBe("fresh")
  expect(ui.getState().tasksError).toBeNull()
})

it("does not accept an in-flight result after polling stopped", async () => {
  let release!: (r: Response) => void
  next = () => new Promise<Response>((resolve) => { release = resolve })
  const ui = await setup()
  stop?.()
  release(Response.json(empty))
  await vi.advanceTimersByTimeAsync(1)
  expect(ui.getState().tasks).toBeNull()
  expect(ui.getState().tasksState).toBe("unknown")
})

it("does not erase suspended tasks from a failed application response", async () => {
  next = async () => Response.json({ ...empty, suspended: 1, tasks: [
    { session_id: "paused", status: "suspended", kind: "chat", waiting_seconds: 0, running_seconds: 1 },
  ] })
  const ui = await setup()
  const { useChatStore: chat } = await import("./useChatStore")
  await vi.waitFor(() => expect(chat.getState().suspendedSessions.paused).toBe(true))
  next = async () => Response.json({ ...empty, status: false })
  await vi.advanceTimersByTimeAsync(30000)
  expect(ui.getState().tasks?.suspended).toBe(1)
  expect(ui.getState().tasksState).toBe("stale")
  expect(chat.getState().suspendedSessions.paused).toBe(true)
})
