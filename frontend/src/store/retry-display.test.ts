import { readFileSync } from "node:fs"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { HistoryResponse } from "@/types/agent-api"

const fixture = JSON.parse(readFileSync(new URL("../../../protocol/fixtures/016-task-retry-inputs.history.json", import.meta.url), "utf8"))
const queued = JSON.parse(readFileSync(new URL("../../../protocol/fixtures/016-task-retry-inputs.events.jsonl", import.meta.url), "utf8").trim().split("\n")[3])
let history: HistoryResponse, reads: number, failHistory: boolean, data: Map<string, string>
let streams: ReadableStreamDefaultController<Uint8Array>[], held: ((r: Response) => void) | undefined
let holdHistory: boolean, submit: ((r: Response) => void) | undefined, branchIndex: number | undefined
const prefix = (n: number): HistoryResponse => ({ status: true, session_id: "A", count: n, turns: fixture.turns.slice(0, n),
  display_turn_indices: fixture.prefix_display_turn_indices[n - 1], retry_groups: fixture.prefix_retry_groups[n - 1], replay_after: 0 })
beforeEach(() => {
  vi.resetModules(); history = prefix(3); reads = 0; failHistory = false; holdHistory = false; held = undefined; submit = undefined; branchIndex = undefined; streams = []; data = new Map()
  vi.stubGlobal("localStorage", { getItem: (k: string) => k === "macagent-lang" ? "zh" : data.get(k) ?? null, setItem: (k: string, v: string) => data.set(k, v) })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/history")) { reads++; if (holdHistory) { holdHistory = false; return new Promise<Response>((r) => { held = r }) }; if (failHistory) return Response.json({}, { status: 500 }); return Response.json(history) }
    if (url.endsWith("/turn")) return Response.json({ status: true, turn: null, last_seq: 0 })
    if (url.endsWith("/task")) return Response.json({ active: false })
    if (url.endsWith("/turns")) return new Promise<Response>((r) => { submit = r })
    if (url.endsWith("/turns/last")) { history = prefix(history.count - 1); return Response.json({ status: true, remaining_turns: history.count }) }
    if (url.endsWith("/branch")) { branchIndex = JSON.parse(String(init?.body)).node_index; return Response.json({ status: false, response: "synthetic branch recorded" }) }
    if (url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(c) { streams.push(c); init?.signal?.addEventListener("abort", () => { try { c.close() } catch { /* already closed */ } }, { once: true }) } }))
    throw Error(`unexpected synthetic request ${url}`)
  }))
})
afterEach(() => { for (const c of streams) { try { c.close() } catch { /* closed */ } }; vi.unstubAllGlobals() })
async function store() {
  const { useChatStore: s } = await import("./useChatStore")
  s.setState({ activeId: "A", conversations: ["A", "B"].map((id) => ({ id, title: id, updatedAt: 0, turnCount: 99, historyLoaded: false, messages: [] })) })
  await s.getState().loadHistory("A"); await vi.waitFor(() => expect(streams.length).toBeGreaterThan(0))
  return s
}
async function emit(index: number, thin = false, stream = streams.at(-1)!, seq = 20 + index) {
  const record = history.turns[index] ?? fixture.turns[index]
  stream.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ seq, ts: 2000 + index, type: "turn_ended", turn_id: `t_${index}`, data: thin ? { status: record.status, turn_index: index } : { ...record, turn_index: index } })}\n\n`))
  await new Promise((r) => setTimeout(r, 10))
}
for (let n = 1; n <= fixture.turns.length; n++) it(`consumes shared 016 prefix ${n} server projection, keeping canonical indices`, async () => {
  history = prefix(n); const s = await store(), conv = s.getState().conversations[0]
  expect(conv.messages.filter((m) => m.role === "user").map((m) => m.turnIndex)).toEqual(history.display_turn_indices)
  expect(conv.canonicalTurns?.length).toBe(n)
  expect(conv.lastCanonicalTurn?.index).toBe(n - 1)
  expect(conv.retryGroups).toEqual(history.retry_groups)
})
it("old servers fall back to all canonical turns; malformed display indices never hide arbitrary records", async () => {
  history = { ...prefix(3), display_turn_indices: [2, 2], retry_groups: undefined }
  const s = await store(); expect(s.getState().conversations[0].messages.filter((m) => m.role === "user").map((m) => m.turnIndex)).toEqual([0, 1, 2])
})
it("missing optional projection fields preserve old-server all-turn display", async () => {
  history = { ...prefix(3), display_turn_indices: undefined, retry_groups: undefined }
  const s = await store(); expect(s.getState().conversations[0].messages.filter((m) => m.role === "user").map((m) => m.turnIndex)).toEqual([0, 1, 2])
})
it("switching away releases raw attempts as well as display messages", async () => {
  const s = await store(); s.getState().selectConversation("B")
  const previous = s.getState().conversations.find((c) => c.id === "A")!
  expect(previous.canonicalTurns).toBeUndefined(); expect(previous.retryGroups).toBeUndefined()
})
it("thin terminal still missing from a successful history read is a sync failure, not an infinite restore loop", async () => {
  const s = await store(); await emit(5, true)
  await vi.waitFor(() => expect(s.getState().conversations[0].retrySyncError).toBe(true))
  await new Promise((r) => setTimeout(r, 40))
  expect(reads).toBe(2); expect(streams).toHaveLength(1)
  expect(s.getState().conversations[0].messages.find((m) => m.role === "user")?.turnIndex).toBe(2)
})
it("an ordinary terminal with a missing huge canonical index restores instead of allocating a sparse transcript", async () => {
  const s = await store()
  streams.at(-1)!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ seq: 30, type: "turn_ended", turn_id: "gap", data: { ...fixture.turns[0], turn_index: 1000000 } })}\n\n`))
  await vi.waitFor(() => expect(s.getState().conversations[0].retrySyncError).toBe(true))
  expect(s.getState().conversations[0].canonicalTurns).toHaveLength(3)
  expect(s.getState().conversations[0].messages.filter((m) => m.role === "user").map((m) => m.turnIndex)).toEqual([2])
})
it("hidden old full and thin terminals are already covered, without append or another restore", async () => {
  const s = await store(); await emit(1); await emit(1, true, streams.at(-1)!, 40)
  expect(reads).toBe(1); expect(s.getState().conversations[0].messages.filter((m) => m.role === "user").map((m) => m.turnIndex)).toEqual([2])
})
it("session index turnCount is not canonical coverage: missing thin retry restores the server projection", async () => {
  const s = await store(); history = prefix(6); await emit(5, true)
  await vi.waitFor(() => expect(reads).toBe(2)); await vi.waitFor(() => expect(s.getState().conversations[0].messages.find((m) => m.role === "user")?.turnIndex).toBe(5))
})
it("new retry terminal reconciles preview then refreshes projection without clearing unrelated pending submission", async () => {
  const s = await store(); const sending = s.getState().sendMessage("new ordinary submission", [])
  const pending = JSON.parse(data.get("macagent-outbox-v1")!).A
  history = prefix(6); await emit(5)
  await vi.waitFor(() => expect(reads).toBe(2))
  expect(JSON.parse(data.get("macagent-outbox-v1")!).A.receipt).toBe(pending.receipt)
  expect(s.getState().generating.A).toBe(true)
  expect(s.getState().conversations[0].messages.filter((m) => m.turnIndex === undefined && m.role === "user")).toHaveLength(1)
  submit!(Response.json({ status: true, turn_id: "new", seq: 50 }, { status: 202 })); await sending
})
it("retry projection history failure retains winner and explicit retryable sync feedback", async () => {
  const s = await store(); failHistory = true; await emit(5)
  await vi.waitFor(() => expect(reads).toBe(2))
  expect(s.getState().conversations[0].messages.find((m) => m.role === "user")?.turnIndex).toBe(2)
  expect(s.getState().conversations[0].retrySyncError).toBe(true)
  failHistory = false; history = prefix(6); await s.getState().loadHistory("A")
  expect(s.getState().conversations[0].retrySyncError).toBe(false)
  expect(s.getState().conversations[0].messages.find((m) => m.role === "user")?.turnIndex).toBe(5)
})
it("late projection HTTP cannot overwrite a reselected ABA conversation", async () => {
  const s = await store(); holdHistory = true; await emit(5); await vi.waitFor(() => expect(held).toBeDefined())
  s.getState().selectConversation("B"); await s.getState().loadHistory("B")
  history = prefix(4); s.getState().selectConversation("A"); await s.getState().loadHistory("A")
  held!(Response.json(prefix(6))); await new Promise((r) => setTimeout(r, 10))
  expect(s.getState().conversations.find((c) => c.id === "A")!.lastCanonicalTurn?.index).toBe(3)
})
it("delete removes the canonical tail and reloads the previous winner, branch keeps winner index", async () => {
  history = prefix(6); const s = await store(); await s.getState().branchFrom("A", s.getState().conversations[0].messages.at(-1)!.turnIndex!)
  expect(branchIndex).toBe(5); expect(await s.getState().deleteLastTurn("A")).toBe(true)
  expect(s.getState().conversations[0].messages.find((m) => m.role === "user")?.turnIndex).toBe(2)
  expect(s.getState().conversations[0].lastCanonicalTurn?.index).toBe(4)
})
it("actual audit SSR shows every canonical attempt, status/time/current version and receipts, with read-only actions", async () => {
  history = prefix(5); history.turns = history.turns.map((t, index) => ({ ...t, timestamp: 1000 + index, prompt: `input ${index}`, response: [{ role: "assistant", content: `answer ${index}` }] }))
  history.turns[4] = { ...history.turns[4], execution_receipts: { items: [{ seq: 9, ts: 1004, type: "tool_result", name: "write_file", detail: "report.txt", status: "ok" }], truncated: false } }
  const s = await store(), { RetryAttempts } = await import("@/components/dashboard/chat-message"), { TooltipProvider } = await import("@/components/ui/tooltip")
  const conv = s.getState().conversations[0]
  const html = renderToStaticMarkup(createElement(TooltipProvider, {}, createElement(RetryAttempts, { group: conv.retryGroups![0], turns: conv.canonicalTurns!, onBranch: () => {} })))
  expect(html).toContain("尝试记录"); expect(html).toContain("最近一次：已取消"); expect(html).toContain("当前展示版本")
  expect(html).toContain("report.txt"); expect(html).toContain("不代表外部操作成功"); expect(html).toContain("1970")
  for (let i = 0; i < 5; i++) expect(html).toContain(`轮次 ${i + 1}`)
  expect(html).toContain("复制"); expect(html).toContain("分支"); expect(html).not.toContain("删除"); expect(html).not.toContain("编辑")
  expect(conv.lastCanonicalTurn?.prompt).toBe("input 4")
})
it("shared 016 retry approval input remains ordinary lifecycle input", async () => {
  const s = await store(); streams.at(-1)!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(queued)}\n\n`))
  await vi.waitFor(() => expect(s.getState().generating.A).toBe(true))
  expect(s.getState().conversations[0].messages.filter((m) => m.turnIndex === undefined && m.role === "user")).toHaveLength(1)
})
for (const count of [2, 4, 5]) it(`live failed/warning/cancelled retry keeps server winner and exposes newest status (prefix=${count})`, async () => {
  history = prefix(count - 1); const s = await store(); history = prefix(count); await emit(count - 1)
  await vi.waitFor(() => expect(reads).toBe(2))
  expect(s.getState().conversations[0].messages.filter((m) => m.role === "user").map((m) => m.turnIndex)).toEqual(history.display_turn_indices)
  expect(s.getState().conversations[0].retryGroups).toEqual(history.retry_groups)
})
it("own retry terminal before submit ACK refreshes projection and consumes only its bound receipt", async () => {
  history = prefix(1); const s = await store(); const sending = s.getState().sendMessage("retry input", [])
  const pending = JSON.parse(data.get("macagent-outbox-v1")!).A
  streams.at(-1)!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ seq: 10, type: "turn_queued", turn_id: "t_1", data: { prompt: "retry input", client_turn_id: pending.key, retry_of: "source" } })}\n\n`))
  await new Promise((r) => setTimeout(r, 5)); history = prefix(2); await emit(1)
  await vi.waitFor(() => expect(reads).toBe(2)); expect(JSON.parse(data.get("macagent-outbox-v1")!).A).toBeUndefined()
  submit!(Response.json({ status: true, turn_id: "t_1", seq: 10 }, { status: 202 })); await sending
  expect(s.getState().generating.A).toBe(false)
  expect(s.getState().conversations[0].messages.filter((m) => m.turnIndex === undefined && m.role === "user")).toHaveLength(0)
})
it("terminal without queued may precede ACK: recovery preserves receipt until ACK binds the terminal", async () => {
  history = prefix(1); const s = await store(); const sending = s.getState().sendMessage("retry input", [])
  const pending = JSON.parse(data.get("macagent-outbox-v1")!).A
  history = prefix(2); await emit(1); await vi.waitFor(() => expect(reads).toBe(2))
  expect(JSON.parse(data.get("macagent-outbox-v1")!).A.receipt).toBe(pending.receipt)
  submit!(Response.json({ status: true, turn_id: "t_1", seq: 10 }, { status: 202 })); await sending
  expect(s.getState().generating.A).toBe(false)
  expect(s.getState().conversations[0].messages.filter((m) => m.turnIndex === undefined && m.role === "user")).toHaveLength(0)
})
it("new terminal waits for an in-flight old history read then takes one fresh canonical projection", async () => {
  const s = await store(); holdHistory = true
  s.setState({ conversations: s.getState().conversations.map((c) => ({ ...c, historyLoaded: false })) })
  const loading = s.getState().loadHistory("A"); await vi.waitFor(() => expect(held).toBeDefined())
  await emit(5); history = prefix(6); held!(Response.json(prefix(3))); await loading
  await vi.waitFor(() => expect(s.getState().conversations[0].messages.find((m) => m.role === "user")?.turnIndex).toBe(5))
  expect(reads).toBe(3)
})
it("ordinary terminal arriving during history read retains canonical tail metadata and edit input", async () => {
  const s = await store(); holdHistory = true
  s.setState({ conversations: s.getState().conversations.map((c) => ({ ...c, historyLoaded: false })) })
  const loading = s.getState().loadHistory("A"); await vi.waitFor(() => expect(held).toBeDefined())
  const oldHistory = prefix(3)
  history = { ...prefix(4), turns: [...oldHistory.turns, { ...fixture.turns[0], prompt: "actual new tail" }] }
  await emit(3); held!(Response.json(oldHistory)); await loading
  expect(s.getState().conversations[0].lastCanonicalTurn?.prompt).toBe("actual new tail")
  expect(s.getState().conversations[0].canonicalTurns?.[3]?.prompt).toBe("actual new tail")
  expect(s.getState().conversations[0].turnCount).toBe(4)
})
it("old attachment cleanup after projection recovery cannot remove the new same-session handle", async () => {
  const s = await store(); history = prefix(6); await emit(5)
  await vi.waitFor(() => expect(streams).toHaveLength(2))
  const newStream = streams[1]
  history = prefix(7); await emit(6, false, newStream, 50)
  expect(reads).toBe(2)
  expect(s.getState().conversations[0].messages.filter((m) => m.role === "user").map((m) => m.turnIndex)).toEqual([5, 6])
  expect(s.getState().conversations[0].lastCanonicalTurn?.index).toBe(6)
})
it("actual ChatArea offers one read-only audit drawer and labels editing/deleting the last canonical attempt", async () => {
  history = prefix(5); history.turns = history.turns.map((t, index) => ({ ...t, prompt: `input ${index}` }))
  const s = await store(), { ChatArea } = await import("@/components/dashboard/chat-area"), { TooltipProvider } = await import("@/components/ui/tooltip")
  const noop = () => {}
  const html = renderToStaticMarkup(createElement(TooltipProvider, {}, createElement(ChatArea, {
    conversation: s.getState().conversations[0], thinkingEnabled: false, generating: false, turnPhase: "none", control: null,
    notice: null, metrics: null, sidebarHidden: false, pendingTools: null, suspended: false, taskProgress: null, activity: null, contextOpen: false,
    onToggleContext: noop, onApproval: noop, onSend: noop, onStop: noop, onSuspend: noop, onResume: noop, onAbandon: noop, onBranch: noop,
    onUpdateLast: noop, onDeleteLast: noop, onRename: noop, onDismissNotice: noop, onOpenMobileSidebar: noop, onShowSidebar: noop,
  })))
  expect(html.match(/data-retry-display-index=/g)).toHaveLength(1)
  expect(html).toContain("编辑/删除作用于最近一次真实尝试：轮次 5")
  const buttons = [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map((m) => m[0])
  expect(buttons.filter((button) => button.includes(">编辑<"))).toHaveLength(1)
  expect(buttons.filter((button) => button.includes(">删除<"))).toHaveLength(1)
})

it.each([
  { label: "missing groups", groups: undefined },
  { label: "invalid groups", groups: [{ root_index: 0, display_index: 2, turn_indices: [0, 999] }] },
  { label: "incomplete groups", groups: [{ root_index: 0, display_index: 2, turn_indices: [0, 2] }] },
])("partial audit metadata ($label) keeps every canonical attempt visible", async ({ groups }) => {
  history = { ...prefix(3), retry_groups: groups }
  const s = await store(), conv = s.getState().conversations[0]
  expect(conv.messages.filter((m) => m.role === "user").map((m) => m.turnIndex)).toEqual([0, 1, 2])
  expect(conv.retryGroups).toEqual([])
})
