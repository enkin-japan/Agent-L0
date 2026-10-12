import { readFileSync } from "node:fs"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { ChatResponse } from "@/types/agent-api"

const events = readFileSync(new URL("../../../protocol/fixtures/015-cancelled-execution-receipts.events.jsonl", import.meta.url), "utf8").trim().split("\n").map((line) => JSON.parse(line))
const terminal = events.at(-1).data as ChatResponse
let stream: ReadableStreamDefaultController<Uint8Array> | undefined
let history: ChatResponse[]
beforeEach(() => {
  vi.resetModules(); stream = undefined; history = []
  vi.stubGlobal("localStorage", { getItem: (k: string) => k === "macagent-lang" ? "zh" : null, setItem: () => {} })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/history")) return Response.json({ status: true, count: history.length, turns: history, replay_after: history.length ? 5 : 0 })
    if (url.endsWith("/turn")) return Response.json({ turn: null, last_seq: history.length ? 5 : 0 })
    if (url.endsWith("/task")) return Response.json({ active: false })
    if (url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(c) { stream = c; init?.signal?.addEventListener("abort", () => c.close(), { once: true }) } }))
    throw Error(`unexpected synthetic request ${url}`)
  }))
})
afterEach(() => { try { stream?.close() } catch { /* closed */ }; vi.unstubAllGlobals() })
async function messages(record: ChatResponse, live: boolean) {
  try { stream?.close() } catch { /* previous comparison stream already closed */ }
  stream = undefined
  history = live ? [] : [record]
  const { useChatStore: s } = await import("./useChatStore")
  s.setState({ activeId: "facts", conversations: [{ id: "facts", title: "facts", updatedAt: 0, turnCount: 0, historyLoaded: false, messages: [] }] })
  await s.getState().loadHistory("facts"); await vi.waitFor(() => expect(stream).toBeDefined())
  if (live) { stream!.enqueue(new TextEncoder().encode(events.map((e) => `data: ${JSON.stringify(e.type === "turn_ended" ? { ...e, data: record } : e)}\n\n`).join(""))); await new Promise((r) => setTimeout(r, 5)) }
  return s.getState().conversations[0].messages
}
async function html(record: ChatResponse, live: boolean) {
  const values = await messages(record, live)
  const { ChatMessage } = await import("@/components/dashboard/chat-message")
  const { TooltipProvider } = await import("@/components/ui/tooltip")
  return { values, html: values.map((message) => renderToStaticMarkup(createElement(TooltipProvider, {}, createElement(ChatMessage, { message })))).join("") }
}
for (const live of [false, true]) {
  it(`cancelled 015 exposes bounded real start/return facts and truncation (live=${live})`, async () => {
    const output = await html(terminal, live)
    expect(output.html).toContain("<details")
    expect(output.html).toContain("执行事实")
    expect(output.html).toContain("report.txt")
    expect(output.html).toContain("仅表示工具已开始")
    expect(output.html).toContain("仅表示工具已返回")
    expect(output.html).toContain("不代表外部操作成功")
    expect(output.html).toContain("省略或截短")
    expect(output.html).toContain("不会自动撤销")
    expect(output.html).not.toContain("可直接重新发送")
    const fact = output.values.find((m) => m.executionFacts !== undefined)!.executionFacts!
    expect(fact.items.map((item) => item.type)).toEqual(["tool_started", "tool_result"])
  })
  for (const receipts of [undefined, { items: [], truncated: false }]) it(`missing/empty facts remain honest (live=${live}, missing=${!receipts})`, async () => {
    const record = { ...terminal, execution_receipts: receipts }
    const output = await html(record, live)
    expect(output.html).toContain("不能据此确认没有外部影响")
  })
  it(`failure hides traceback and retains execution facts (live=${live})`, async () => {
    const output = await html({ ...terminal, status: "fail", response: [{ role: "system", content: "RAW_INTERNAL_TRACEBACK" }] }, live)
    expect(output.html).not.toContain("RAW_INTERNAL_TRACEBACK")
    expect(output.html).toContain("report.txt")
    expect(output.html).toContain("不会自动撤销")
  })
}
it("live and history use the same canonical message projection", async () => {
  const historical = await messages(terminal, false)
  // A distinct store incarnation is needed for the live replay comparison.
  vi.resetModules()
  const live = await messages(terminal, true)
  const normalize = (values: typeof live) => values.map(({ id: _id, ...value }) => { void _id; return value })
  expect(normalize(live)).toEqual(normalize(historical))
})
it("client rendering bounds malformed oversized receipt data and marks omitted source", async () => {
  const items = Array.from({ length: 70 }, (_, i) => ({ seq: i, ts: i, type: "tool_result" as const, name: "write_file", detail: "d".repeat(2200), status: "ok" }))
  const output = await html({ ...terminal, execution_receipts: { items, truncated: false } }, false)
  const facts = output.values.find((m) => m.executionFacts !== undefined)!.executionFacts!
  expect(facts.items).toHaveLength(64)
  expect(facts.items.every((item) => item.detail.length <= 2000)).toBe(true)
  expect(facts.truncated).toBe(true)
})
it("successful transcript tool rows do not duplicate an execution facts panel", async () => {
  const output = await html({ ...terminal, status: "success", response: [{ role: "tool", tool_responses: [{ name: "write_file", response: "report.txt" }] }, { role: "assistant", content: "done" }] }, false)
  expect(output.html).toContain("report.txt")
  expect(output.html).not.toContain("执行事实")
})

it("one oversized detail independently marks source truncation without exceeding the count bound", async () => {
  const output = await html({ ...terminal, execution_receipts: { items: [{ seq: 4, ts: 4, type: "tool_result", name: "write_file", detail: "d".repeat(2200), status: "ok" }], truncated: false } }, false)
  const facts = output.values.find((m) => m.executionFacts !== undefined)!.executionFacts!
  expect(facts.items).toHaveLength(1)
  expect(facts.truncated).toBe(true)
  expect(facts.items[0].truncated).toBe(true)
  expect(output.html).toContain("省略或截短")
})
