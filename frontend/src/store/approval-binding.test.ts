import { readFileSync } from "node:fs"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { EventEnvelope } from "@/types/protocol"

// Real store + API + SSE parser, with an entirely in-process fetch transport.
const events: EventEnvelope[] = readFileSync(new URL("../../../protocol/fixtures/010-approval-replaced-long-arguments.events.jsonl", import.meta.url), "utf8")
  .trim().split("\n").map((line) => JSON.parse(line))
let streams: Map<string, ReadableStreamDefaultController<Uint8Array>>
let requests: Array<{ sid: string; body: { approve: boolean; expected_turn_id?: string; expected_approval_seq?: number } }>
let response: (() => Promise<Response>) | undefined
const cardA = { turnId: "t_card_a", seq: 3 }

beforeEach(() => {
  vi.resetModules()
  streams = new Map()
  requests = []
  response = undefined
  const storage = new Map<string, string>()
  vi.stubGlobal("localStorage", { getItem: (k: string) => storage.get(k) ?? null, setItem: (k: string, v: string) => storage.set(k, v) })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const sid = url.split("/chat/")[1]?.split("/")[0]
    if (url.includes("/events?")) {
      const stream = new ReadableStream<Uint8Array>({ start(controller) {
        streams.set(sid, controller)
        init?.signal?.addEventListener("abort", () => controller.close(), { once: true })
      } })
      return new Response(stream)
    }
    if (url.endsWith("/turn")) return Response.json({ turn: null, last_seq: 0 })
    if (url.endsWith("/history")) return Response.json({ turns: [] })
    if (url.endsWith("/turns/approval")) {
      requests.push({ sid, body: JSON.parse(String(init?.body)) })
      return response ? response() : Response.json({ turn_id: "t_card_a", seq: 4 }, { status: 202 })
    }
    throw new Error(`Unexpected fake request ${url}`)
  }))
})
afterEach(() => {
  for (const controller of streams.values()) { try { controller.close() } catch { /* already aborted */ } }
  vi.unstubAllGlobals()
})

async function setup() {
  const { useChatStore: store } = await import("./useChatStore")
  store.setState({ activeId: "s1", conversations: ["s1", "s2"].map((id) => ({ id, title: id, updatedAt: 0, turnCount: 0, pinned: false, historyLoaded: true, messages: [] })) })
  await store.getState().loadHistory("s1")
  await vi.waitFor(() => expect(streams.has("s1")).toBe(true))
  return store
}
async function emit(sid: string, envelopes: EventEnvelope[]) {
  streams.get(sid)!.enqueue(new TextEncoder().encode(envelopes.map((env) => `data: ${JSON.stringify(env)}\n\n`).join("")))
  await new Promise((resolve) => setTimeout(resolve, 0))
}
function deferred() {
  let resolve!: (value: Response) => void
  const promise = new Promise<Response>((r) => { resolve = r })
  response = () => promise
  return resolve
}

describe("approval card identity", () => {
  it("sends the displayed turn identity and keeps optional API compatibility", async () => {
    const store = await setup()
    await emit("s1", events.slice(0, 3))
    await store.getState().respondApproval("s1", true, cardA)
    expect(requests[0]).toEqual({ sid: "s1", body: { approve: true, expected_turn_id: "t_card_a", expected_approval_seq: 3 } })
    const { submitApproval } = await import("@/api/agent")
    await submitApproval("legacy", false)
    expect(requests[1].body).toEqual({ approve: false })
  })
  it("rejects the A card once B has replaced it, retaining B's full arguments", async () => {
    const store = await setup()
    await emit("s1", events)
    const before = store.getState().pendingApprovals.s1
    await store.getState().respondApproval("s1", true, cardA)
    expect(requests).toHaveLength(0)
    expect(store.getState().pendingApprovals.s1).toBe(before)
    expect(String(before[0].arguments.content)).toContain("APPROVAL_TAIL_VISIBLE")
  })
  it("server B rejects an unseen replacement when the visible A card is clicked", async () => {
    const store = await setup()
    await emit("s1", events.slice(0, 3))
    response = async () => requests.at(-1)?.body.expected_turn_id === "t_card_a"
      ? Response.json({ reason: "approval_target_mismatch" }, { status: 409 })
      : Response.json({ turn_id: "t_card_b", seq: 8 }, { status: 202 })
    await store.getState().respondApproval("s1", true, cardA)
    expect(requests[0].body.expected_turn_id).toBe("t_card_a")
    expect(store.getState().generating.s1).toBe(false)
  })
  it("rejects an old batch card when the same turn reaches another approval", async () => {
    const store = await setup()
    await emit("s1", events.slice(0, 3))
    await emit("s1", [{ ...events[2], seq: 4 }])
    await store.getState().respondApproval("s1", true, cardA)
    expect(requests).toHaveLength(0)
  })
  it("server rejects an old batch when the client has not received same-turn SSE updates", async () => {
    const batches: EventEnvelope[] = readFileSync(new URL("../../../protocol/fixtures/011-approval-batches-same-turn.events.jsonl", import.meta.url), "utf8")
      .trim().split("\n").map((line) => JSON.parse(line))
    const store = await setup()
    await emit("s1", batches.slice(0, 3))
    response = async () => requests.at(-1)?.body.expected_approval_seq === 3
      ? Response.json({ reason: "approval_target_mismatch" }, { status: 409 })
      : Response.json({ status: true, turn_id: "t_same_turn" }, { status: 202 })
    await store.getState().respondApproval("s1", true, { turnId: "t_same_turn", seq: 3 })
    expect(requests[0].body.expected_approval_seq).toBe(3)
    expect(store.getState().generating.s1).toBe(false)
  })
  for (const status of [202, 409, 500]) {
    it(`late ${status} from s1 cannot detach or mutate selected s2`, async () => {
      const store = await setup()
      await emit("s1", events.slice(0, 3))
      const resolve = deferred()
      const pending = store.getState().respondApproval("s1", true, cardA)
      store.getState().selectConversation("s2")
      await store.getState().loadHistory("s2")
      await vi.waitFor(() => expect(streams.has("s2")).toBe(true))
      await emit("s2", events)
      const before = store.getState()
      resolve(Response.json({ reason: "approval_target_mismatch" }, { status }))
      await pending
      expect(store.getState()).toBe(before)
      expect(store.getState().activeId).toBe("s2")
    })
    it(`late ${status} cannot restore A's tools or clear B's state in the same session`, async () => {
      const store = await setup()
      await emit("s1", events.slice(0, 3))
      const resolve = deferred()
      const pending = store.getState().respondApproval("s1", true, cardA)
      await emit("s1", events.slice(3))
      const before = store.getState()
      resolve(Response.json({ reason: "approval_target_mismatch" }, { status }))
      await pending
      expect(store.getState()).toBe(before)
    })
  }
  it("late failure after resumed cannot roll back the running turn", async () => {
    const store = await setup()
    await emit("s1", events.slice(0, 3))
    const resolve = deferred()
    const pending = store.getState().respondApproval("s1", true, cardA)
    await emit("s1", [{ ...events[1], seq: 4, type: "resumed", data: { trigger: "approve" } }])
    const before = store.getState()
    resolve(Response.json({ reason: "synthetic" }, { status: 500 }))
    await pending
    expect(store.getState()).toBe(before)
    expect(store.getState().generating.s1).toBe(true)
  })
  it("transport failure restores the same A card for an explicit retry", async () => {
    const store = await setup()
    await emit("s1", events.slice(0, 3))
    response = async () => { throw new TypeError("synthetic offline") }
    const tools = store.getState().pendingApprovals.s1
    await store.getState().respondApproval("s1", false, cardA)
    expect(store.getState().pendingApprovals.s1).toBe(tools)
    expect(store.getState().generating.s1).toBe(false)
  })
})
