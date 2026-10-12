import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
let list: () => Promise<Response>, runs: () => Promise<Response>, action: () => Promise<Response>
let feed: ReadableStreamDefaultController<Uint8Array> | undefined, stop: (() => void) | undefined, stopRuns: (() => void) | undefined
let scheduleCalls: number
const task = { id: "s1", name: "daily", prompt: "report", enabled: true, running: true, spec: { type: "daily", at: "08:00" }, next_run: 100, last_run: null }
const known = { status: true, count: 1, schedules: [task] }
beforeEach(() => {
  vi.resetModules(); vi.useFakeTimers(); stop = undefined; stopRuns = undefined; scheduleCalls = 0; feed = undefined
  list = async () => Response.json(known); runs = async () => Response.json({ status: true, runs: [{ run_id: "r1", status: "success", summary: "known", started_at: 1, finished_at: 2 }], count: 1 })
  action = async () => Response.json({ status: true, schedule_id: "s1", response: "signal accepted" })
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === "macagent-lang" ? "zh" : null, setItem: () => {} })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/schedules")) { scheduleCalls++; return list() }
    if (url.endsWith("/runs?limit=20")) return runs()
    if (url.includes("/schedules/") && init?.method) return action()
    if (url.endsWith("/tasks")) return Response.json({ status: true, tasks: [], running: [], suspended: 0, queued: 0 })
    if (url.endsWith("/sessions")) return Response.json({ status: true, sessions: [] })
    if (url.includes("/api/v1/events?")) return new Response(new ReadableStream<Uint8Array>({ start(c) { feed = c; init?.signal?.addEventListener("abort", () => { try { c.close() } catch { /* closed */ } }, { once: true }) } }))
    throw Error(`Unexpected synthetic request ${url}`)
  }))
})
afterEach(() => { stopRuns?.(); stop?.(); vi.useRealTimers(); vi.unstubAllGlobals() })
async function setup() {
  await import("./useChatStore"); const { useUiStore: ui } = await import("./useUiStore")
  ui.getState().startSchedulerPolling(); stop = () => ui.getState().stopSchedulerPolling(); await vi.advanceTimersByTimeAsync(1)
  return ui
}
it("schedule list uses existing feed plus 30s fallback, with no 10s schedule timer", async () => {
  const ui = await setup(); expect(ui.getState().schedulesState).toBe("fresh"); expect(scheduleCalls).toBe(1)
  await vi.advanceTimersByTimeAsync(10000); expect(scheduleCalls).toBe(1)
  list = async () => Response.json({ ...known, schedules: [{ ...task, running: false, last_run: { run_id: "r2", status: "cancelled" } }] })
  feed!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ type: "turn_ended", feed_seq: 1, seq: 2, session_id: "schedule_s1", turn_id: "t", data: {} })}\n\n`))
  await vi.advanceTimersByTimeAsync(350); expect(ui.getState().schedules?.schedules[0].running).toBe(false)
  await vi.advanceTimersByTimeAsync(19650); expect(scheduleCalls).toBe(3)
})
for (const business of [false, true]) it(`failed list retains last success as stale (business=${business})`, async () => {
  const ui = await setup(); const old = ui.getState().schedules
  list = async () => business ? Response.json({ status: false, schedules: [] }) : new Response("offline", { status: 503 })
  await ui.getState().refreshSchedules(); expect(ui.getState().schedules).toBe(old); expect(ui.getState().schedulesState).toBe("stale")
  expect(ui.getState().schedulesError).toBeTruthy()
})
it("initial offline state is unknown and actual list renderer does not claim no schedules", async () => {
  list = async () => new Response("offline", { status: 503 }); const ui = await setup()
  expect(ui.getState().schedulesState).toBe("unknown")
  const { ScheduleListView } = await import("@/components/dashboard/schedule-strip")
  const html = renderToStaticMarkup(createElement(ScheduleListView, { items: [], state: ui.getState().schedulesState, error: ui.getState().schedulesError, onRefresh: () => {}, onSelect: () => {} }))
  expect(html).toContain("定时任务状态未知"); expect(html).toContain("重试")
})
it("old list result cannot replace a newer snapshot or return after monitor stopped", async () => {
  const ui = await setup(); let resolve!: (r: Response) => void; list = () => new Promise((r) => { resolve = r })
  const old = ui.getState().refreshSchedules(); list = async () => Response.json({ ...known, schedules: [{ ...task, name: "new" }] }); await ui.getState().refreshSchedules()
  resolve(Response.json(known)); await old; expect(ui.getState().schedules?.schedules[0].name).toBe("new")
  list = () => new Promise((r) => { resolve = r }); const late = ui.getState().refreshSchedules(); stop!(); resolve(Response.json(known)); await late
  expect(ui.getState().schedules?.schedules[0].name).toBe("new")
})
for (const business of [false, true]) it(`run history keeps last successful records on failure (business=${business})`, async () => {
  const ui = await setup(); stopRuns = ui.getState().watchScheduleRuns("s1"); await vi.advanceTimersByTimeAsync(1)
  const old = ui.getState().scheduleRuns?.data
  runs = async () => business ? Response.json({ status: false, runs: [] }) : new Response("offline", { status: 503 })
  await ui.getState().refreshScheduleRuns(); expect(ui.getState().scheduleRuns?.data).toBe(old); expect(ui.getState().scheduleRuns?.state).toBe("stale")
})
it("late runs for old detail cannot overwrite new detail or be released by its old cleanup", async () => {
  const ui = await setup(); let resolve!: (r: Response) => void; runs = () => new Promise((r) => { resolve = r })
  const oldRelease = ui.getState().watchScheduleRuns("s1"); await vi.advanceTimersByTimeAsync(1)
  runs = async () => Response.json({ status: true, runs: [], count: 0 }); stopRuns = ui.getState().watchScheduleRuns("s2"); oldRelease(); await vi.advanceTimersByTimeAsync(1)
  resolve(Response.json({ status: true, runs: [{ run_id: "old" }] })); await vi.advanceTimersByTimeAsync(1)
  expect(ui.getState().scheduleRuns?.id).toBe("s2"); expect(ui.getState().scheduleRuns?.data).toEqual([])
})
for (const kind of ["run", "cancel", "delete"] as const) it(`${kind} business false is explicit refusal, and deletion does not close detail as success`, async () => {
  const ui = await setup(); stopRuns = ui.getState().watchScheduleRuns("s1"); await vi.advanceTimersByTimeAsync(1)
  action = async () => Response.json({ status: false, response: "synthetic refusal" })
  expect(await ui.getState().actSchedule("s1", kind)).toBe(false)
  expect(ui.getState().scheduleAction?.status).toBe("rejected"); expect(ui.getState().scheduleAction?.message).toContain("synthetic refusal")
})
it("late schedule action ACK cannot update another detail, and HTTP failure remains explicit", async () => {
  const ui = await setup(); stopRuns = ui.getState().watchScheduleRuns("s1"); await vi.advanceTimersByTimeAsync(1)
  let resolve!: (r: Response) => void; action = () => new Promise((r) => { resolve = r }); const old = ui.getState().actSchedule("s1", "delete")
  await vi.advanceTimersByTimeAsync(1); expect(resolve).toBeDefined()
  stopRuns(); stopRuns = ui.getState().watchScheduleRuns("s2"); await vi.advanceTimersByTimeAsync(1)
  resolve(Response.json({ status: true, response: "old deleted" })); expect(await old).toBe(false)
  expect(ui.getState().scheduleAction?.message).not.toBe("old deleted")
  stopRuns(); stopRuns = ui.getState().watchScheduleRuns("s1"); await vi.advanceTimersByTimeAsync(1)
  action = async () => new Response("offline", { status: 503 }); expect(await ui.getState().actSchedule("s1", "run")).toBe(false)
  expect(ui.getState().scheduleAction?.status).toBe("failed")
})
it("old action ACK after detail ABA cannot claim the new same-id detail", async () => {
  const ui = await setup(); stopRuns = ui.getState().watchScheduleRuns("s1"); await vi.advanceTimersByTimeAsync(1)
  let resolve!: (r: Response) => void; action = () => new Promise((r) => { resolve = r }); const old = ui.getState().actSchedule("s1", "delete")
  await vi.advanceTimersByTimeAsync(1); expect(resolve).toBeDefined()
  stopRuns(); stopRuns = ui.getState().watchScheduleRuns("s2"); stopRuns(); stopRuns = ui.getState().watchScheduleRuns("s1")
  await vi.advanceTimersByTimeAsync(1); resolve(Response.json({ status: true, response: "old same-id deletion" }))
  expect(await old).toBe(false); expect(ui.getState().scheduleAction).toBeNull()
})
it("an old action response cannot restart fresh reads after monitor generation stopped", async () => {
  const ui = await setup(); stopRuns = ui.getState().watchScheduleRuns("s1"); await vi.advanceTimersByTimeAsync(1)
  let resolve!: (r: Response) => void; action = () => new Promise((r) => { resolve = r }); const old = ui.getState().actSchedule("s1", "delete")
  await vi.advanceTimersByTimeAsync(1); expect(resolve).toBeDefined()
  const calls = scheduleCalls; stop!(); resolve(Response.json({ status: true, response: "old" })); expect(await old).toBe(false)
  await vi.advanceTimersByTimeAsync(1); expect(scheduleCalls).toBe(calls); expect(ui.getState().schedulesState).toBe("stale")
})
it("unknown/stale actual history renderer preserves records and never calls offline an empty history", async () => {
  const ui = await setup(); stopRuns = ui.getState().watchScheduleRuns("s1"); await vi.advanceTimersByTimeAsync(1)
  const { ScheduleRunsView } = await import("@/components/dashboard/schedule-strip")
  runs = async () => new Response("offline", { status: 503 }); await ui.getState().refreshScheduleRuns()
  const html = renderToStaticMarkup(createElement(ScheduleRunsView, { snapshot: ui.getState().scheduleRuns, onRefresh: () => {} }))
  expect(html).toContain("known"); expect(html).toContain("最近已知"); expect(html).toContain("重试")
  const unknown = renderToStaticMarkup(createElement(ScheduleRunsView, { snapshot: { data: null, state: "unknown", error: "unavailable" }, onRefresh: () => {} }))
  expect(unknown).toContain("运行历史未知"); expect(unknown).not.toContain("暂无运行记录")
})
