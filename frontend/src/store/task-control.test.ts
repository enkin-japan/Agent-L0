import { readFileSync } from "node:fs"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
const fixture = readFileSync(new URL("../../../protocol/fixtures/015-cancelled-execution-receipts.events.jsonl", import.meta.url), "utf8").trim().split("\n").map((line) => JSON.parse(line))
let streams: Map<string, ReadableStreamDefaultController<Uint8Array>>
let calls: string[]
let reply: () => Promise<Response>
let snapshot: () => Promise<Response>
beforeEach(() => {
  vi.resetModules(); streams = new Map(); calls = []
  reply = async () => Response.json({ status: true, response: "accepted" })
  snapshot = async () => Response.json({ turn: null, last_seq: 0 })
  const data = new Map<string, string>([["macagent-lang", "zh"]])
  vi.stubGlobal("localStorage", { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => data.set(k, v) })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const sid = url.split("/chat/")[1]?.split("/")[0] ?? "feed"
    if (url.endsWith("/task/cancel") || url.endsWith("/task/suspend") || url.endsWith("/turns/resume")) { calls.push(url); return reply() }
    if (init?.method === "DELETE") return Response.json({ status: true, frozen: false, response: "deleted" })
    if (url.endsWith("/history")) return Response.json({ status: true, count: 0, turns: [], replay_after: 0 })
    if (url.endsWith("/turn")) return snapshot()
    if (url.endsWith("/task")) return Response.json({ active: false })
    if (url.endsWith("/tasks")) return Response.json({ status: true, tasks: [], running: [], queued: 0, suspended: 0 })
    if (url.endsWith("/sessions")) return Response.json({ status: true, sessions: [] })
    if (url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(c) {
      streams.set(sid, c); init?.signal?.addEventListener("abort", () => c.close(), { once: true })
    } }))
    throw Error(`unexpected synthetic request ${url}`)
  }))
})
afterEach(() => { for (const c of streams.values()) { try { c.close() } catch { /* already closed */ } }; vi.useRealTimers(); vi.unstubAllGlobals() })
async function setup() {
  const { useChatStore: s } = await import("./useChatStore")
  s.setState({ activeId: "A", conversations: ["A", "B"].map((id) => ({ id, title: id, updatedAt: 0, turnCount: 0, historyLoaded: false, messages: [] })) })
  await s.getState().loadHistory("A"); await vi.waitFor(() => expect(streams.has("A")).toBe(true))
  return s
}
async function emit(sid: string, type: string, seq: number, turnId = "T1", data: Record<string, unknown> = {}) {
  streams.get(sid)!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ type, seq, turn_id: turnId, ts: seq, data })}\n\n`))
  await new Promise((r) => setTimeout(r, 5))
}
async function running(s: Awaited<ReturnType<typeof setup>>) {
  await emit("A", "turn_queued", 1, "T1", { prompt: "p" })
  await emit("A", "turn_started", 2)
  expect(s.getState().turnStates.A.phase).toBe("running")
}
function delayed() { let release!: (r: Response) => void; const p = new Promise<Response>((r) => { release = r }); reply = () => p; return release }
it("queued is not suspendable; running phase permits suspension", async () => {
  const s = await setup(); await emit("A", "turn_queued", 1, "T1", { prompt: "p" })
  await s.getState().suspendTask("A"); expect(calls).toHaveLength(0)
  await emit("A", "turn_started", 2)
  await s.getState().suspendTask("A"); expect(calls).toHaveLength(1)
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  expect(s.getState().generating.A).toBe(true)
})
for (const action of ["cancel", "suspend"] as const) {
  for (const status of [200, 500]) it(`${action} reports business false / HTTP failure without inventing a journal terminal (${status})`, async () => {
    const s = await setup(); await running(s)
    reply = async () => Response.json({ status: false, response: "synthetic rejected at checkpoint" }, { status })
    await (action === "cancel" ? s.getState().stopGenerating("A") : s.getState().suspendTask("A"))
    await new Promise((r) => setTimeout(r, 5))
    expect(s.getState().controlRequests.A.status).toBe(status === 200 ? "rejected" : "failed")
    if (status === 200) expect(s.getState().controlRequests.A.message).toContain("synthetic rejected")
    expect(s.getState().turnStates.A.phase).toBe("running")
    expect(s.getState().generating.A).toBe(true)
  })
}
it("sending -> accepted waits beyond 15s for an actual pause event", async () => {
  const s = await setup(); await running(s); const release = delayed()
  vi.useFakeTimers()
  const pending = s.getState().suspendTask("A")
  expect(s.getState().controlRequests.A.status).toBe("sending")
  release(Response.json({ status: true })); await pending
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  await vi.advanceTimersByTimeAsync(16000); vi.useRealTimers()
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  expect(s.getState().generating.A).toBe(true)
  await emit("A", "suspended", 3)
  expect(s.getState().controlRequests.A).toBeUndefined()
  expect(s.getState().suspendedSessions.A).toBe(true)
})
for (const end of ["suspended", "turn_ended"]) for (const status of [200, 500]) {
  it(`late ${status} control ack after ${end} cannot revive pending state`, async () => {
    const s = await setup(); await running(s); const release = delayed()
    const pending = s.getState().suspendTask("A")
    await emit("A", end, 3, "T1", end === "turn_ended" ? { ...fixture.at(-1).data, turn_index: 0 } : {})
    const before = s.getState()
    release(Response.json({ status: true }, { status })); await pending
    expect(s.getState()).toBe(before)
  })
}
it("a later cancel request supersedes pending suspend; old ack cannot replace it", async () => {
  const s = await setup(); await running(s); const release = delayed()
  const old = s.getState().suspendTask("A")
  reply = async () => Response.json({ status: true })
  await s.getState().stopGenerating("A")
  expect(s.getState().controlRequests.A.action).toBe("cancel")
  release(Response.json({ status: false, response: "old rejection" })); await old
  expect(s.getState().controlRequests.A.action).toBe("cancel")
  expect(s.getState().controlRequests.A.status).toBe("accepted")
})
for (const change of ["new-turn", "switch", "aba"]) {
  it(`late control response cannot affect ${change}`, async () => {
    const s = await setup(); await running(s); const release = delayed(); const old = s.getState().suspendTask("A")
    if (change === "new-turn") { await emit("A", "turn_queued", 3, "T2", { prompt: "new" }); await emit("A", "turn_started", 4, "T2") }
    else {
      s.getState().selectConversation("B"); await s.getState().loadHistory("B")
      await vi.waitFor(() => expect(streams.has("B")).toBe(true))
      if (change === "aba") {
        streams.delete("A")
        s.getState().selectConversation("A"); await s.getState().loadHistory("A")
        await vi.waitFor(() => expect(streams.has("A")).toBe(true))
      }
    }
    const before = s.getState()
    release(Response.json({ status: false, response: "stale" })); await old
    expect(s.getState()).toBe(before)
  })
}
it("resume ack after journal resumed cannot restore suspended flags", async () => {
  const s = await setup(); await running(s); await emit("A", "suspended", 3)
  const release = delayed(); const old = s.getState().resumeTask("A")
  await emit("A", "resumed", 4); await emit("A", "turn_started", 5)
  const before = s.getState(); release(Response.json({ reason: "stale" }, { status: 409 })); await old
  expect(s.getState()).toBe(before)
  expect(s.getState().suspendedSessions.A).toBeUndefined()
})
it("global stop binds a one-shot turn snapshot and reports rejection on its own task", async () => {
  const s = await setup(); const { useUiStore: ui } = await import("./useUiStore")
  s.getState().selectConversation("B"); await s.getState().loadHistory("B")
  ui.setState({ tasksState: "fresh" })
  snapshot = async () => Response.json({ turn: { turn_id: "T-A", state: "running", last_seq: 10 }, last_seq: 10 })
  reply = async () => Response.json({ status: false, response: "background rejected" })
  const before = s.getState().conversations.find((c) => c.id === "B")
  await s.getState().stopGenerating("A", "global")
  expect(s.getState().controlRequests.A.message).toContain("background rejected")
  expect(s.getState().conversations.find((c) => c.id === "B")).toBe(before)
  expect(s.getState().controlRequests.B).toBeUndefined()
})
it("unknown global tasks and workers without a turn do not pretend cancellable", async () => {
  const s = await setup(); const { useUiStore: ui } = await import("./useUiStore")
  ui.setState({ tasksState: "unknown" }); await s.getState().stopGenerating("A", "global")
  expect(calls).toHaveLength(0)
  ui.setState({ tasksState: "fresh" }); await s.getState().stopGenerating("A", "global")
  expect(calls).toHaveLength(0)
  expect(s.getState().controlRequests.A.status).toBe("rejected")
})
it("global target snapshot from an old selection cannot issue a new POST", async () => {
  const s = await setup(); const { useUiStore: ui } = await import("./useUiStore"); ui.setState({ tasksState: "fresh" })
  let release!: (r: Response) => void; snapshot = () => new Promise((r) => { release = r })
  const old = s.getState().stopGenerating("A", "global")
  s.getState().selectConversation("B")
  release(Response.json({ turn: { turn_id: "old-A", state: "running", last_seq: 1 }, last_seq: 1 })); await old
  expect(calls).toHaveLength(0)
})
it("queued stop remains accepted through worker admission until journal terminal", async () => {
  const s = await setup(); await emit("A", "turn_queued", 1, "T1", { prompt: "p" })
  await s.getState().stopGenerating("A")
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  await emit("A", "turn_started", 2)
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  await emit("A", "turn_ended", 3, "T1", { ...fixture.at(-1).data, turn_index: 0 })
  expect(s.getState().controlRequests.A).toBeUndefined()
})
it("a newer queued turn invalidates a global target read before any POST", async () => {
  const s = await setup(); const { useUiStore: ui } = await import("./useUiStore"); ui.setState({ tasksState: "fresh" })
  let release!: (r: Response) => void; snapshot = () => new Promise((r) => { release = r })
  const old = s.getState().stopGenerating("A", "global")
  await emit("A", "turn_queued", 8, "T2", { prompt: "new target" })
  const before = s.getState()
  release(Response.json({ turn: { turn_id: "T1", state: "running" }, last_seq: 5 })); await old
  expect(calls).toHaveLength(0)
  expect(s.getState()).toBe(before)
})
it("the actual global feed closes background control feedback without attaching its session", async () => {
  const s = await setup(); s.getState().selectConversation("B"); await s.getState().loadHistory("B")
  const { useUiStore: ui } = await import("./useUiStore")
  ui.getState().startSchedulerPolling()
  try {
    await vi.waitFor(() => { expect(streams.has("feed")).toBe(true); expect(ui.getState().tasksState).toBe("fresh") })
    snapshot = async () => Response.json({ turn: { turn_id: "background", state: "running" }, last_seq: 10 })
    await s.getState().stopGenerating("A", "global")
    expect(s.getState().controlRequests.A.status).toBe("accepted")
    const b = s.getState().conversations.find((c) => c.id === "B")
    streams.get("feed")!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ feed_seq: 1, seq: 11, session_id: "A", turn_id: "background", type: "turn_ended", data: { status: "cancelled" } })}\n\n`))
    await vi.waitFor(() => expect(s.getState().controlRequests.A).toBeUndefined())
    expect(s.getState().conversations.find((c) => c.id === "B")).toBe(b)
  } finally { ui.getState().stopSchedulerPolling() }
})
it("journal projections and feedback are bounded while retaining the selected turn", async () => {
  const s = await setup(); await running(s)
  for (let i = 0; i < 70; i++) s.getState().observeTaskEvent(`background-${i}`, { type: "turn_queued", seq: 1, turn_id: `T-${i}` })
  expect(Object.keys(s.getState().turnStates)).toHaveLength(64)
  expect(s.getState().turnStates.A.turnId).toBe("T1")
  expect(s.getState().turnStates["background-0"]).toBeUndefined()
})

it("deleting an unrelated session does not erase the selected pending command", async () => {
  const s = await setup(); await running(s); const release = delayed()
  const pending = s.getState().suspendTask("A")
  s.getState().deleteConversation("B")
  expect(s.getState().controlRequests.A.status).toBe("sending")
  release(Response.json({ status: true })); await pending
  expect(s.getState().controlRequests.A.status).toBe("accepted")
})
for (const phase of ["awaiting_approval", "suspended"]) it(`known ${phase} does not submit a stop request`, async () => {
  const s = await setup(); await running(s)
  await emit("A", phase, 3, "T1", phase === "awaiting_approval" ? { pending_tools: [{ name: "write_file" }] } : {})
  await s.getState().stopGenerating("A")
  expect(calls).toHaveLength(0)
  expect(s.getState().controlRequests.A.status).toBe("rejected")
})
it("cancel is confirmed only by a terminal, not an approval pause", async () => {
  const s = await setup(); await running(s); await s.getState().stopGenerating("A")
  await emit("A", "awaiting_approval", 3, "T1", { pending_tools: [{ name: "write_file" }] })
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  await emit("A", "turn_ended", 4, "T1", { ...fixture.at(-1).data, turn_index: 0 })
  expect(s.getState().controlRequests.A).toBeUndefined()
})
it("resume uses its captured floor: historical pause does not confirm it and resumed equal to new snapshot does", async () => {
  snapshot = async () => Response.json({ status: true, turn: { turn_id: "T1", state: "suspended", first_seq: 1, last_seq: 10 }, last_seq: 10 })
  const s = await setup()
  await emit("A", "turn_queued", 1, "T1", { prompt: "p" }); await emit("A", "turn_started", 2); await emit("A", "suspended", 3)
  streams.get("A")!.close(); streams.delete("A"); await new Promise((r) => setTimeout(r, 5))
  snapshot = async () => Response.json({ status: true, turn: { turn_id: "T1", state: "queued", first_seq: 1, last_seq: 11 }, last_seq: 11 })
  reply = async () => Response.json({ status: true, turn_id: "T1" }, { status: 202 })
  await s.getState().resumeTask("A"); await vi.waitFor(() => expect(streams.has("A")).toBe(true))
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  await emit("A", "resumed", 4)
  await emit("A", "turn_started", 5)
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  await emit("A", "suspended", 10)
  expect(s.getState().controlRequests.A.status).toBe("accepted")
  await emit("A", "resumed", 11)
  expect(s.getState().controlRequests.A).toBeUndefined()
})

it("global cached T1 converges to newer snapshot T2, rejects this click and permits a second explicit click", async () => {
  const s = await setup(); await emit("A", "turn_queued", 9, "T1", { prompt: "old" }); await emit("A", "turn_started", 10)
  const { useUiStore: ui } = await import("./useUiStore"); ui.setState({ tasksState: "fresh" })
  snapshot = async () => Response.json({ status: true, turn: { turn_id: "T2", state: "running", last_seq: 20 }, last_seq: 20 })
  await s.getState().stopGenerating("A", "global")
  expect(calls).toHaveLength(0)
  expect(s.getState().turnStates.A.turnId).toBe("T2")
  expect(s.getState().controlRequests.A.status).toBe("rejected")
  expect(s.getState().controlRequests.A.message).toContain("轮次已变化")
  await s.getState().stopGenerating("A", "global")
  expect(calls).toHaveLength(1)
  expect(s.getState().controlRequests.A.turnId).toBe("T2")
  expect(s.getState().controlRequests.A.status).toBe("accepted")
})
it("an older global read cannot replace a newer known T3 projection", async () => {
  const s = await setup(); await emit("A", "turn_queued", 9, "T1", { prompt: "old" }); await emit("A", "turn_started", 10)
  const { useUiStore: ui } = await import("./useUiStore"); ui.setState({ tasksState: "fresh" })
  let release!: (r: Response) => void; snapshot = () => new Promise((r) => { release = r })
  const pending = s.getState().stopGenerating("A", "global")
  await emit("A", "turn_queued", 30, "T3", { prompt: "latest" })
  const before = s.getState()
  release(Response.json({ status: true, turn: { turn_id: "T2", state: "running" }, last_seq: 20 })); await pending
  expect(s.getState()).toBe(before)
  expect(s.getState().turnStates.A.turnId).toBe("T3")
  expect(calls).toHaveLength(0)
})
