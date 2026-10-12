import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { readFileSync } from "node:fs"
import type { DeleteSessionResponse } from "@/types/agent-api"
const shared017 = JSON.parse(readFileSync(new URL("../../../protocol/fixtures/017-delete-absent.responses.json", import.meta.url), "utf8")) as Record<"absent" | "deleted" | "failed", DeleteSessionResponse>
let data: Map<string, string>, deletes: string[], releaseDelete: Map<string, (r: Response) => void>
let streams: ReadableStreamDefaultController<Uint8Array>[], posts: string[]
let submitResponse: () => Promise<Response>
let stopScheduleRuns: (() => void) | undefined
const scheduleTasks = ["s1", "s2"].map((id) => ({ id, name: id, prompt: "synthetic", enabled: true, running: false, spec: { type: "daily", at: "08:00" }, last_run: null, next_run: 1 }))
beforeEach(() => {
  stopScheduleRuns = undefined
  resolveSchedule = undefined
  vi.resetModules(); data = new Map(); deletes = []; posts = []; releaseDelete = new Map(); streams = []
  submitResponse = async () => Response.json({ status: true, turn_id: "t", seq: 1 }, { status: 202 })
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === "macagent-lang" ? "zh" : data.get(key) ?? null, setItem: (key: string, value: string) => data.set(key, value) })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const sid = url.match(/\/chat\/([^/]+)$/)?.[1]
    if (init?.method === "DELETE" && sid) { deletes.push(sid); return new Promise<Response>((r) => { releaseDelete.set(sid, r) }) }
    if (init?.method === "POST") { posts.push(url); return submitResponse() }
    if (url.endsWith("/schedules")) return Response.json({ status: true, schedules: scheduleTasks, count: 2 })
    if (url.endsWith("/runs?limit=20")) return Response.json({ status: true, runs: [], count: 0 })
    if (url.match(/\/schedules\/[^/]+$/) && init?.method === "DELETE") { posts.push(url); return submitResponse() }
    if (url.endsWith("/history")) return Response.json({ status: true, turns: [], count: 0, replay_after: 0 })
    if (url.endsWith("/turn")) return Response.json({ turn: null, last_seq: 0 })
    if (url.endsWith("/task")) return Response.json({ active: false })
    if (url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(c) { streams.push(c); init?.signal?.addEventListener("abort", () => { try { c.close() } catch { /* closed */ } }, { once: true }) } }))
    throw Error(`Unexpected synthetic request ${url}`)
  }))
})
afterEach(async () => {
  for (const resolve of releaseDelete.values()) resolve(Response.json({ status: false, response: "synthetic cleanup" }))
  resolveSchedule?.(Response.json({ status: false, response: "synthetic cleanup" }))
  await new Promise((r) => setTimeout(r, 1))
  stopScheduleRuns?.(); for (const c of streams) { try { c.close() } catch { /* closed */ } }; vi.unstubAllGlobals()
})
async function store() {
  const { useChatStore: s } = await import("./useChatStore")
  s.setState({ activeId: "A", conversations: ["A", "B", "C"].map((id) => ({ id, title: id, updatedAt: 0, turnCount: 0, historyLoaded: false, messages: [] })) })
  return s
}
for (const business of [false, true]) it(`single deletion keeps data and attachment until confirmed, and preserves it on failure (business=${business})`, async () => {
  const s = await store(); await s.getState().loadHistory("A"); const pending = s.getState().deleteConversation("A")
  await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  expect(s.getState().conversations.some((c) => c.id === "A")).toBe(true)
  expect(s.getState().deletingSessions.A).toBe(true)
  releaseDelete.get("A")!(business ? Response.json({ status: false, response: "synthetic refusal" }) : new Response("offline", { status: 503 }))
  expect((await pending).status).toBe("failed")
  expect(s.getState().activeId).toBe("A"); expect(s.getState().conversations.some((c) => c.id === "A")).toBe(true)
  expect(s.getState().deletingSessions.A).toBeUndefined()
})
it("three deletions await sequential receipts; one failure remains and retry deletes only that id", async () => {
  const s = await store(); const deleting = s.getState().deleteConversations(["A", "B", "C"])
  await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  releaseDelete.get("A")!(Response.json({ status: true })); await vi.waitFor(() => expect(deletes).toEqual(["A", "B"]))
  releaseDelete.get("B")!(Response.json({ status: false, response: "B refused" })); await vi.waitFor(() => expect(deletes).toEqual(["A", "B", "C"]))
  releaseDelete.get("C")!(Response.json({ status: true })); const receipts = await deleting
  expect(receipts.map((r) => [r.id, r.status])).toEqual([["A", "deleted"], ["B", "failed"], ["C", "deleted"]])
  expect(s.getState().conversations.map((c) => c.id)).toEqual(["B"])
  const retry = s.getState().deleteConversations(receipts.filter((r) => r.status === "failed").map((r) => r.id))
  await vi.waitFor(() => expect(deletes).toEqual(["A", "B", "C", "B"]))
  releaseDelete.get("B")!(Response.json({ status: true })); expect((await retry)[0].status).toBe("deleted")
})
it("a deleting session rejects submission/edit/approval/resume but other sessions can submit", async () => {
  const s = await store(); const deleting = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toHaveLength(1))
  await s.getState().sendMessage("must not write A", [])
  await s.getState().updateLastTurn("must not edit A")
  await s.getState().respondApproval("A", true, { turnId: "t", seq: 1 }); await s.getState().resumeTask("A")
  expect(posts).toEqual([]); expect(s.getState().notice).toContain("正在删除")
  s.getState().selectConversation("B"); await s.getState().sendMessage("B can submit", [])
  expect(posts.some((url) => url.endsWith("/chat/B/turns"))).toBe(true)
  releaseDelete.get("A")!(Response.json({ status: true })); await deleting
  expect(s.getState().activeId).toBe("B"); expect(s.getState().generating.B).toBe(true)
})
it("late A delete after ABA cannot detach current A or clear a newer shared outbox receipt", async () => {
  const s = await store(); data.set("macagent-outbox-v1", JSON.stringify({ A: { text: "old", ts: Date.now(), key: "old-key", receipt: "old-receipt" } }))
  const pending = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toHaveLength(1))
  s.getState().selectConversation("B"); s.getState().selectConversation("A"); await s.getState().loadHistory("A")
  data.set("macagent-outbox-v1", JSON.stringify({ A: { text: "new", ts: Date.now(), key: "new-key", receipt: "new-receipt" } }))
  releaseDelete.get("A")!(Response.json({ status: true })); expect((await pending).status).toBe("changed")
  expect(s.getState().conversations.some((c) => c.id === "A")).toBe(true)
  expect(JSON.parse(data.get("macagent-outbox-v1")!).A.receipt).toBe("new-receipt")
  expect(s.getState().activeId).toBe("A")
})
it("duplicate delete commands coalesce a single HTTP request", async () => {
  const s = await store(); const a = s.getState().deleteConversation("A"), b = s.getState().deleteConversation("A")
  await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  releaseDelete.get("A")!(Response.json({ status: true })); expect((await a).status).toBe("deleted"); expect((await b).status).toBe("deleted")
})
it("confirmed deletion only clears its captured outbox receipt, preserving another tab's newer receipt", async () => {
  const s = await store(); data.set("macagent-outbox-v1", JSON.stringify({ A: { text: "old", ts: Date.now(), key: "old-key", receipt: "old-receipt" } }))
  const pending = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toHaveLength(1))
  data.set("macagent-outbox-v1", JSON.stringify({ A: { text: "new", ts: Date.now(), key: "new-key", receipt: "new-receipt" } }))
  releaseDelete.get("A")!(Response.json({ status: true })); expect((await pending).status).toBe("deleted")
  expect(JSON.parse(data.get("macagent-outbox-v1")!).A.receipt).toBe("new-receipt")
})
it("queued batch target reselected before its DELETE is not submitted from the old batch", async () => {
  const s = await store(); const pending = s.getState().deleteConversations(["A", "B"])
  await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  s.getState().selectConversation("B"); releaseDelete.get("A")!(Response.json({ status: true }))
  await new Promise((r) => setTimeout(r, 1)); expect(deletes).toEqual(["A"])
  expect((await pending).map((r) => r.status)).toEqual(["deleted", "changed"])
  expect(deletes).toEqual(["A"]); expect(s.getState().activeId).toBe("B")
})
it("DELETE refuses a current submit HTTP, but returned unknown outbox is not a permanent deletion block", async () => {
  const s = await store(); let resolve!: (r: Response) => void
  submitResponse = () => new Promise((r) => { resolve = r }); const sending = s.getState().sendMessage("unknown request", [])
  await vi.waitFor(() => expect(resolve).toBeDefined())
  const rejectedDelete = s.getState().deleteConversation("A")
  await new Promise((r) => setTimeout(r, 1)); expect(deletes).toEqual([])
  expect((await rejectedDelete).status).toBe("failed")
  resolve(new Response("lost ack", { status: 503 })); await sending
  expect(JSON.parse(data.get("macagent-outbox-v1")!).A).toBeDefined()
  const deleting = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  releaseDelete.get("A")!(Response.json({ status: true })); expect((await deleting).status).toBe("deleted")
})
it("a reentrant delete started during optimistic projection is rechecked immediately before submit POST", async () => {
  const s = await store(); let pending: Promise<unknown> | undefined
  const unsubscribe = s.subscribe((state) => { if (!pending && state.conversations[0].messages.some((m) => m.role === "user")) { pending = Promise.resolve(); pending = s.getState().deleteConversation("A") } })
  await s.getState().sendMessage("must not race DELETE", []); unsubscribe()
  expect(deletes).toEqual(["A"]); expect(posts).toEqual([])
  releaseDelete.get("A")!(Response.json({ status: true })); await pending
})
it("background deletion failure is retained beside its target, without a notice in the selected other chat", async () => {
  const s = await store(); s.getState().selectConversation("B")
  const pending = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  releaseDelete.get("A")!(Response.json({ status: false, response: "A refusal" })); await pending
  expect(s.getState().deletionResults.A.message).toContain("A refusal")
  expect(s.getState().activeId).toBe("B"); expect(s.getState().notice).toBeNull()
})
it("fresh never-submitted local draft retires on the structured remote not_found receipt", async () => {
  const s = await store(); s.setState({ activeId: "", conversations: [] }); s.getState().newConversation()
  const id = s.getState().activeId
  expect(s.getState().conversations[0].turnCount).toBe(0)
  expect(s.getState().conversations[0].messages).toEqual([])
  const pending = s.getState().deleteConversation(id); await vi.waitFor(() => expect(deletes).toEqual([id]))
  releaseDelete.get(id)!(Response.json(shared017.absent))
  const result = await pending
  expect(result.status).toBe("deleted"); expect(s.getState().conversations.some((c) => c.id === id)).toBe(false)
  expect(result.message).toContain("本地记录已移除"); expect(result.message).not.toContain(shared017.absent.response)
})
it("cached remote conversation already deleted elsewhere retires on not_found despite non-empty history metadata", async () => {
  const s = await store(); s.setState({ conversations: s.getState().conversations.map((c) => c.id === "A" ? { ...c, turnCount: 99, messages: [{ id: "archived", role: "user", content: "cached" }] } : c) })
  const pending = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  releaseDelete.get("A")!(Response.json({ ...shared017.absent, response: "❌未发现session" }))
  const result = await pending
  expect(result.status).toBe("deleted"); expect(s.getState().conversations.some((c) => c.id === "A")).toBe(false)
  expect(result.message).toContain("本地记录已移除"); expect(result.message).not.toContain("❌")
})
for (const reason of [undefined, "locked", "unknown_reason", "not_found_but_locked"]) it(`ordinary false/old peer keeps an empty draft even if text says absent (reason=${reason})`, async () => {
  const s = await store(); const pending = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  releaseDelete.get("A")!(Response.json({ status: false, frozen: false, reason, response: "not found / 不存在 / 存在しません" }))
  expect((await pending).status).toBe("failed"); expect(s.getState().conversations.some((c) => c.id === "A")).toBe(true)
})
it("not_found still respects ABA ownership and cannot clear the reselected view's newer receipt", async () => {
  const s = await store(); const pending = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  s.getState().selectConversation("B"); s.getState().selectConversation("A")
  data.set("macagent-outbox-v1", JSON.stringify({ A: { text: "new", ts: Date.now(), key: "new-key", receipt: "new-receipt" } }))
  releaseDelete.get("A")!(Response.json(shared017.absent))
  expect((await pending).status).toBe("changed"); expect(s.getState().conversations.some((c) => c.id === "A")).toBe(true)
  expect(JSON.parse(data.get("macagent-outbox-v1")!).A.receipt).toBe("new-receipt")
})
it("shared017 failed receipt with nullable reason remains failed and keeps its actual rejection detail", async () => {
  const response: DeleteSessionResponse = { ...shared017.failed, reason: null }
  const s = await store(); const pending = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  releaseDelete.get("A")!(Response.json(response)); const result = await pending
  expect(result.status).toBe("failed"); expect(result.message).toContain(shared017.failed.response)
  expect(s.getState().conversations.some((c) => c.id === "A")).toBe(true)
})
it("shared017 ordinary successful deletion keeps its ordinary receipt text", async () => {
  const s = await store(); const pending = s.getState().deleteConversation("A"); await vi.waitFor(() => expect(deletes).toEqual(["A"]))
  releaseDelete.get("A")!(Response.json(shared017.deleted)); const result = await pending
  expect(result.status).toBe("deleted"); expect(result.message).toBe(shared017.deleted.response)
})

// Cross-controller journeys use the same actual chat/UI stores and HTTP fake.
let resolveSchedule: ((response: Response) => void) | undefined
const scheduleWrites = () => posts.filter((url) => url.includes("/schedules/"))
async function setupSchedule(id = "s1") {
  const chat = await store(), { useUiStore: ui } = await import("./useUiStore")
  chat.setState({ activeId: "schedule_s1", conversations: ["schedule_s1", "schedule_s2"].map((sid) => ({ id: sid, title: sid, messages: [], updatedAt: 0, historyLoaded: false, turnCount: 0 })) })
  ui.setState({ schedulesState: "fresh", schedulesError: null, schedules: { status: true, count: 2, schedules: scheduleTasks } })
  stopScheduleRuns = ui.getState().watchScheduleRuns(id)
  await vi.waitFor(() => expect(ui.getState().scheduleRuns?.state).toBe("fresh"))
  return { chat, ui }
}
for (const kind of ["run", "toggle", "cancel", "delete"] as const) it(`pending session DELETE prevents ${kind} HTTP for the same schedule session`, async () => {
  const { chat, ui } = await setupSchedule(); const deleting = chat.getState().deleteConversation("schedule_s1")
  await vi.waitFor(() => expect(deletes).toEqual(["schedule_s1"]))
  submitResponse = () => new Promise((r) => { resolveSchedule = r }); const running = ui.getState().actSchedule("s1", kind, true)
  await new Promise((r) => setTimeout(r, 1))
  expect(scheduleWrites()).toEqual([]); expect(await running).toBe(false)
  expect(ui.getState().scheduleAction?.status).toBe("rejected")
  releaseDelete.get("schedule_s1")!(Response.json({ status: false, response: "synthetic refusal" })); await deleting
})
for (const kind of ["run", "toggle", "cancel", "delete"] as const) it(`pending ${kind} HTTP prevents session DELETE until its real response returns`, async () => {
  const { chat, ui } = await setupSchedule(); submitResponse = () => new Promise((r) => { resolveSchedule = r }); const running = ui.getState().actSchedule("s1", kind, true)
  await vi.waitFor(() => expect(scheduleWrites()).toHaveLength(1))
  const deleting = chat.getState().deleteConversation("schedule_s1")
  await new Promise((r) => setTimeout(r, 1))
  expect(deletes).toEqual([]); expect((await deleting).status).toBe("failed")
  resolveSchedule!(Response.json({ status: true, response: "accepted" })); await running
})
for (const success of [false, true]) it(`closing/ABA detail does not release the real HTTP before response (success=${success})`, async () => {
  const { chat, ui } = await setupSchedule(); submitResponse = () => new Promise((r) => { resolveSchedule = r }); const running = ui.getState().actSchedule("s1", "run")
  await vi.waitFor(() => expect(scheduleWrites()).toHaveLength(1))
  stopScheduleRuns!(); stopScheduleRuns = ui.getState().watchScheduleRuns("s2"); stopScheduleRuns(); stopScheduleRuns = ui.getState().watchScheduleRuns("s1")
  const refusal = await chat.getState().deleteConversation("schedule_s1")
  expect(refusal.status).toBe("failed"); expect(deletes).toEqual([])
  resolveSchedule!(success ? Response.json({ status: true, response: "accepted" }) : new Response("synthetic HTTP failure", { status: 503 }))
  expect(await running).toBe(false) // old response no longer owns current detail feedback
  expect(ui.getState().scheduleAction).toBeNull()
  const deleting = chat.getState().deleteConversation("schedule_s1")
  await vi.waitFor(() => expect(deletes).toEqual(["schedule_s1"]))
  releaseDelete.get("schedule_s1")!(Response.json({ status: false, response: "refused" })); await deleting
})
it("positive: a pending DELETE for s1 allows an independent run for s2", async () => {
  const { chat, ui } = await setupSchedule("s2"); const deleting = chat.getState().deleteConversation("schedule_s1")
  await vi.waitFor(() => expect(deletes).toEqual(["schedule_s1"]))
  submitResponse = () => new Promise((r) => { resolveSchedule = r }); const running = ui.getState().actSchedule("s2", "run")
  await vi.waitFor(() => expect(scheduleWrites()).toHaveLength(1))
  resolveSchedule!(Response.json({ status: true, response: "accepted" })); expect(await running).toBe(true)
  releaseDelete.get("schedule_s1")!(Response.json({ status: false, response: "refused" })); await deleting
})
it("positive: returned run HTTP permits later deletion and returned DELETE refusal permits run", async () => {
  const { chat, ui } = await setupSchedule(); submitResponse = () => new Promise((r) => { resolveSchedule = r }); const running = ui.getState().actSchedule("s1", "run")
  await vi.waitFor(() => expect(scheduleWrites()).toHaveLength(1))
  resolveSchedule!(Response.json({ status: false, response: "run not accepted" })); expect(await running).toBe(false)
  const deleting = chat.getState().deleteConversation("schedule_s1"); await vi.waitFor(() => expect(deletes).toEqual(["schedule_s1"]))
  releaseDelete.get("schedule_s1")!(Response.json({ status: false, response: "delete not accepted" })); expect((await deleting).status).toBe("failed")
  submitResponse = () => new Promise((r) => { resolveSchedule = r }); const retry = ui.getState().actSchedule("s1", "run"); await vi.waitFor(() => expect(scheduleWrites()).toHaveLength(2))
  resolveSchedule!(Response.json({ status: true, response: "accepted" })); expect(await retry).toBe(true)
})
