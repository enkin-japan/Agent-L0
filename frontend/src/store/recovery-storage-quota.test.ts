import { afterEach, beforeEach, expect, it, vi } from "vitest"

let disk: Map<string, string>
let posts: string[]
let reply: () => Promise<Response>
let streams: ReadableStreamDefaultController<Uint8Array>[]
let unreadable: boolean
let blocked: boolean
beforeEach(() => {
  vi.resetModules()
  disk = new Map(); posts = []; streams = []; unreadable = false; blocked = true
  reply = async () => Response.json({ status: true, turn_id: "new", seq: 5 }, { status: 202 })
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => { if (unreadable) throw new DOMException("synthetic blocked storage", "SecurityError"); return disk.get(key) ?? null },
    setItem: (key: string, value: string) => { if (blocked) throw new DOMException("synthetic quota", "QuotaExceededError"); disk.set(key, value) },
  })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/turns")) { posts.push(String((init!.body as FormData).get("client_turn_id"))); return reply() }
    if (url.endsWith("/history")) return Response.json({ status: true, count: 0, turns: [], replay_after: 0 })
    if (url.endsWith("/turn")) return Response.json({ turn: null, last_seq: 0 })
    if (url.endsWith("/task")) return Response.json({ active: false })
    if (url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(c) {
      streams.push(c); init?.signal?.addEventListener("abort", () => c.close(), { once: true })
    } }))
    throw Error(`Unexpected synthetic request ${url}`)
  }))
})
afterEach(() => { for (const c of streams) { try { c.close() } catch { /* already closed */ } }; vi.unstubAllGlobals() })
async function setup() {
  const module = await import("./useChatStore")
  module.useChatStore.setState({ activeId: "A", conversations: [{ id: "A", title: "A", updatedAt: 0, turnCount: 0, historyLoaded: false, messages: [] }] })
  return module
}
for (const getUnavailable of [false, true]) {
  it(`quota failure does not prevent POST or 202 cleanup (getUnavailable=${getUnavailable})`, async () => {
    const { useChatStore: s, outboxKeyFor } = await setup()
    unreadable = getUnavailable
    await s.getState().sendMessage("keep this input", [])
    expect(posts).toHaveLength(1)
    expect(outboxKeyFor("A", "keep this input")).not.toBe(posts[0])
  })
}
it("same-page failed retry retains key; 202 tombstone prevents a stale disk entry resurfacing", async () => {
  disk.set("macagent-outbox-v1", JSON.stringify({ A: { text: "retry input", ts: 1, key: "persisted-original-key", receipt: "stale-receipt" } }))
  reply = async () => Response.json({}, { status: 500 })
  const { useChatStore: s, outboxKeyFor } = await setup()
  await s.getState().sendMessage("retry input", [])
  expect(posts[0]).toBe("persisted-original-key")
  expect(outboxKeyFor("A", "retry input")).toBe(posts[0])
  reply = async () => Response.json({ status: true, turn_id: "new", seq: 5 }, { status: 202 })
  await s.getState().sendMessage("retry input", [])
  expect(posts[1]).toBe(posts[0])
  expect(outboxKeyFor("A", "retry input")).not.toBe(posts[0])
  expect(JSON.parse(disk.get("macagent-outbox-v1")!).A.key).toBe(posts[0]) // unchanged disk must remain masked
})
it("old terminal cannot clear a quota-only pending receipt or lose its failure retry key", async () => {
  let release!: (response: Response) => void
  reply = () => new Promise((r) => { release = r })
  const { useChatStore: s, outboxKeyFor } = await setup()
  await s.getState().loadHistory("A")
  await vi.waitFor(() => expect(streams.length).toBe(1))
  const pending = s.getState().sendMessage("new input", [])
  const key = posts[0]
  const events = [
    { seq: 1, turn_id: "old", type: "turn_queued", data: { prompt: "old input", client_turn_id: "old-key" } },
    { seq: 2, turn_id: "old", type: "turn_ended", data: { status: "success", prompt: "old input", response: [{ role: "assistant", content: "old answer" }], turn_index: 0 } },
  ]
  streams[0].enqueue(new TextEncoder().encode(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("")))
  await new Promise((r) => setTimeout(r, 5))
  expect(outboxKeyFor("A", "new input")).toBe(key)
  expect(s.getState().generating.A).toBe(true)
  expect(s.getState().conversations[0].messages.filter((m) => m.content === "new input")).toHaveLength(1)
  release(Response.json({}, { status: 500 })); await pending
  expect(outboxKeyFor("A", "new input")).toBe(key)
})

it("an old receipt tombstone preserves a newer disk receipt when storage recovers", async () => {
  disk.set("macagent-outbox-v1", JSON.stringify({ A: { text: "retry input", ts: 1, key: "original-key", receipt: "original-receipt" } }))
  const { useChatStore: s, outboxKeyFor } = await setup()
  await s.getState().sendMessage("retry input", [])
  expect(outboxKeyFor("A", "retry input")).not.toBe("original-key")
  const external = { text: "external input", ts: Date.now(), key: "external-key", receipt: "external-receipt" }
  disk.set("macagent-outbox-v1", JSON.stringify({ A: external }))
  expect(outboxKeyFor("A", "external input")).toBe("external-key")
  blocked = false
  s.getState().newConversation()
  await s.getState().sendMessage("another input", [])
  expect(JSON.parse(disk.get("macagent-outbox-v1")!).A).toEqual(external)
  expect(outboxKeyFor("A", "external input")).toBe("external-key")
})

it("quota fallback retains only the most recent 32 session entries", async () => {
  reply = async () => Response.json({}, { status: 500 })
  const { useChatStore: s, outboxKeyFor } = await setup()
  const ids = Array.from({ length: 33 }, (_, index) => `quota-${index}`)
  s.setState({ activeId: ids[0], conversations: ids.map((id) => ({ id, title: id, updatedAt: 0, turnCount: 0, historyLoaded: false, messages: [] })) })
  for (const id of ids) { s.getState().selectConversation(id); await s.getState().sendMessage("retry input", []) }
  expect(outboxKeyFor(ids[0], "retry input")).not.toBe(posts[0])
  expect(outboxKeyFor(ids[32], "retry input")).toBe(posts[32])
})
it("202 also drops its obsolete memory receipt after another tab replaces the disk entry", async () => {
  let release!: (response: Response) => void
  reply = () => new Promise((r) => { release = r })
  const { useChatStore: s, outboxKeyFor } = await setup()
  await s.getState().loadHistory("A")
  await vi.waitFor(() => expect(streams.length).toBe(1))
  const pending = s.getState().sendMessage("own input", [])
  const key = posts[0]
  disk.set("macagent-outbox-v1", JSON.stringify({ A: { text: "external input", ts: Date.now(), key: "external-key", receipt: "external-receipt" } }))
  release(Response.json({ status: true, turn_id: "new", seq: 5 }, { status: 202 })); await pending
  expect(outboxKeyFor("A", "external input")).toBe("external-key")
  unreadable = true
  expect(outboxKeyFor("A", "own input")).not.toBe(key)
})
