import { readFileSync } from "node:fs"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { EventEnvelope } from "@/types/protocol"

const fixture = JSON.parse(readFileSync(new URL("../../../protocol/fixtures/014-history-replay-boundary.restore.json", import.meta.url), "utf8"))
const events: EventEnvelope[] = readFileSync(new URL("../../../protocol/fixtures/014-history-replay-boundary.events.jsonl", import.meta.url), "utf8").trim().split("\n").map((line) => JSON.parse(line))
let history: (sid: string) => Promise<Response>
let turn: (sid: string) => Promise<Response>
let submit: () => Promise<Response>
let streams: Array<{ sid: string; after: number; ctl: ReadableStreamDefaultController<Uint8Array>; signal?: AbortSignal | null }>
let calls: string[]
function deferred() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>((r) => { resolve = r })
  return { promise, resolve }
}
beforeEach(() => {
  vi.resetModules()
  streams = []; calls = []
  history = async (sid) => Response.json({ ...fixture.history_before, session_id: sid })
  turn = async () => Response.json(fixture.turn_after)
  submit = async () => Response.json({ status: true, turn_id: "t_submit", seq: 5 }, { status: 202 })
  const storage = new Map<string, string>()
  vi.stubGlobal("localStorage", { getItem: (k: string) => storage.get(k) ?? null, setItem: (k: string, v: string) => storage.set(k, v) })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const sid = url.split("/chat/")[1]?.split("/")[0]
    calls.push(url)
    if (url.endsWith("/history")) return history(sid)
    if (url.endsWith("/turn")) return turn(sid)
    if (url.endsWith("/turns") || url.endsWith("/turns/resume")) return submit()
    if (url.endsWith("/task")) return Response.json({ active: false })
    if (url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(ctl) {
      streams.push({ sid, after: Number(url.split("after=")[1]), ctl, signal: init?.signal })
      init?.signal?.addEventListener("abort", () => ctl.close(), { once: true })
    } }))
    throw Error(`unexpected synthetic request ${url}`)
  }))
})
afterEach(() => { for (const s of streams) { try { s.ctl.close() } catch { /* aborted */ } }; vi.unstubAllGlobals() })
async function setup(load = true) {
  const { useChatStore: store } = await import("./useChatStore")
  store.setState({ activeId: "A", conversations: ["A", "B"].map((id) => ({ id, title: id, updatedAt: 0, turnCount: 0, historyLoaded: false, messages: [] })) })
  if (load) { await store.getState().loadHistory("A"); await vi.waitFor(() => expect(streams.length).toBe(1)) }
  return store
}
async function emit(index: number, envs: EventEnvelope[]) {
  streams[index].ctl.enqueue(new TextEncoder().encode(envs.filter((e) => e.seq > streams[index].after).map((e) => `data: ${JSON.stringify(e)}\n\n`).join("")))
  await new Promise((r) => setTimeout(r, 5))
}
for (const included of [false, true]) {
  it(`history -> terminal -> turn(null) recovers exactly once (included=${included})`, async () => {
    history = async () => Response.json(included ? fixture.history_including : fixture.history_before)
    const store = await setup()
    expect(streams[0].after).toBe(fixture.expected_after)
    await emit(0, events)
    expect(store.getState().conversations[0].messages.map((m) => m.content)).toEqual(fixture.expected_messages)
    expect(store.getState().conversations[0].turnCount).toBe(1)
  })
}
it("thin persisted terminal re-enters the same recovery entrance", async () => {
  const store = await setup()
  history = async () => Response.json(fixture.history_including)
  await emit(0, [events[0], fixture.thin_terminal])
  await vi.waitFor(() => expect(streams.length).toBe(2))
  expect(streams[0].signal?.aborted).toBe(true)
  expect(store.getState().conversations[0].messages.map((m) => m.content)).toEqual(fixture.expected_messages)
})
it("terminal without queued retains the prompt, and older replay cannot regress count", async () => {
  history = async () => Response.json({ ...fixture.history_including, count: 2, turns: [...fixture.history_including.turns, { status: "success", prompt: "second", response: [{ role: "assistant", content: "second answer" }] }] })
  const store = await setup()
  await emit(0, events)
  expect(store.getState().conversations[0].turnCount).toBe(2)
  expect(store.getState().conversations[0].messages).toHaveLength(4)
})
it("coalesces initial history; failed history stays retryable and no turn starts early", async () => {
  const d = deferred(); history = () => d.promise
  const store = await setup(false)
  const first = store.getState().loadHistory("A"), second = store.getState().loadHistory("A")
  await Promise.resolve()
  expect(calls.filter((s) => s.endsWith("/history"))).toHaveLength(1)
  expect(calls.some((s) => s.endsWith("/turn"))).toBe(false)
  d.resolve(Response.json({}, { status: 500 })); await Promise.all([first, second])
  expect(store.getState().conversations[0].historyLoaded).toBe(false)
  history = async () => Response.json(fixture.history_before)
  await store.getState().loadHistory("A")
  await vi.waitFor(() => expect(streams.length).toBe(1))
})
for (const result of ["accepted", "duplicate", "conflict", "error"]) {
  for (const aba of [false, true]) {
  it(`late A submit ${result} cannot affect ${aba ? "A -> B -> A" : "selected B"}`, async () => {
    const store = await setup(); const d = deferred(); submit = () => d.promise
    const send = store.getState().sendMessage("late A", [])
    store.getState().selectConversation("B"); await store.getState().loadHistory("B")
    await vi.waitFor(() => expect(streams.length).toBe(2))
    if (aba) {
      store.getState().selectConversation("A"); await store.getState().loadHistory("A")
      await vi.waitFor(() => expect(streams.length).toBe(3))
    }
    const before = store.getState()
    d.resolve(Response.json({ status: true, duplicate: result === "duplicate", reason: "synthetic" }, { status: result === "conflict" ? 409 : result === "error" ? 500 : 202 }))
    await send
    expect(store.getState()).toBe(before)
    expect(streams[aba ? 2 : 1].signal?.aborted).toBe(false)
    expect(streams.length).toBe(aba ? 3 : 2)
  })
  }
}
it("late history from an earlier A selection cannot overwrite current A", async () => {
  const d = deferred(); history = () => d.promise
  const store = await setup(false); const old = store.getState().loadHistory("A")
  store.getState().selectConversation("B"); history = async (sid) => Response.json({ ...fixture.history_including, session_id: sid }); await store.getState().loadHistory("B")
  store.getState().selectConversation("A"); await store.getState().loadHistory("A")
  d.resolve(Response.json({ ...fixture.history_including, turns: [{ status: "success", prompt: "stale prompt", response: [{ role: "assistant", content: "stale answer" }] }] })); await old
  expect(store.getState().conversations[0].messages.map((m) => m.content)).toEqual(fixture.expected_messages)
})

it("old A attachment exit cannot delete the newer A handle", async () => {
  const store = await setup(false)
  const originalFetch = globalThis.fetch
  const old = deferred()
  let held = false
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (!held && url.includes("/chat/A/events?")) { held = true; return old.promise }
    return originalFetch(url, init)
  }))
  await store.getState().loadHistory("A")
  await vi.waitFor(() => expect(held).toBe(true))
  store.getState().selectConversation("B"); await store.getState().loadHistory("B")
  await vi.waitFor(() => expect(streams.length).toBe(1))
  store.getState().selectConversation("A"); await store.getState().loadHistory("A")
  await vi.waitFor(() => expect(streams.length).toBe(2))
  old.resolve(new Response("")) // normal old stream completion after newer A attached
  await new Promise((r) => setTimeout(r, 5))
  await store.getState().loadHistory("A")
  await new Promise((r) => setTimeout(r, 5))
  expect(streams.length).toBe(2)
})
it("legacy history starts at zero; 404 ends attachment and valid submit reattaches", async () => {
  history = async () => Response.json({ turns: [], count: 0 })
  const originalFetch = globalThis.fetch
  let missing = true, eventCalls = 0
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("/events?")) { eventCalls++; if (missing) return Response.json({}, { status: 404 }) }
    return originalFetch(url, init)
  }))
  const store = await setup(false)
  await store.getState().loadHistory("A")
  await vi.waitFor(() => expect(eventCalls).toBe(1))
  await new Promise((r) => setTimeout(r, 1100))
  expect(eventCalls).toBe(1)
  missing = false
  await store.getState().sendMessage("valid", [])
  await vi.waitFor(() => expect(streams.length).toBe(1))
  expect(streams[0].after).toBe(0)
})
it("fresh submit completed before first history still reconciles its optimistic bubble", async () => {
  history = async () => Response.json({ ...fixture.history_including, replay_after: 4 })
  submit = async () => Response.json({ status: true, turn_id: "t_restore", seq: 2 }, { status: 202 })
  const store = await setup(false)
  await store.getState().sendMessage("restore prompt", [])
  await vi.waitFor(() => expect(streams.length).toBe(1))
  await emit(0, events)
  expect(store.getState().conversations[0].messages.map((m) => m.content)).toEqual(fixture.expected_messages)
  expect(store.getState().generating.A).toBe(false)
})
