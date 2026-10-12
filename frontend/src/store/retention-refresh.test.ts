import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { SessionRetentionPolicy } from "@/types/agent-api"

let policy: SessionRetentionPolicy
let generation: string
let controllers: ReadableStreamDefaultController<Uint8Array>[]
let holdNext: boolean
let release: (() => void) | undefined
let stop: (() => void) | undefined
let chatController: ReadableStreamDefaultController<Uint8Array> | undefined
let chatAborted: boolean

function attach(g: string) {
  return new TextEncoder().encode(`data: ${JSON.stringify({ type: "stream_attached", feed_seq: 0, data: { generation: g } })}\n\n`)
}

beforeEach(() => {
  vi.resetModules()
  vi.useFakeTimers()
  controllers = []
  policy = { enabled: true, retention_days: 30, empty_ttl_hours: 24, scan_interval_seconds: 3600 }
  generation = "first"
  holdNext = false
  release = undefined
  chatController = undefined
  chatAborted = false
  const storage = new Map<string, string>()
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value) })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/history")) return Response.json({ status: true, turns: [{ status: "success", prompt: "old", response: [{ role: "assistant", content: "answer" }] }] })
    if (url.endsWith("/turn")) return Response.json({ turn: null, last_seq: 1 })
    if (url.includes("/chat/") && url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(controller) {
      chatController = controller
      init?.signal?.addEventListener("abort", () => { chatAborted = true; try { controller.close() } catch { /* already ended */ } }, { once: true })
    } }))
    if (url.endsWith("/sessions")) {
      const result = { status: true, sessions: [], retention_policy: { ...policy } }
      if (holdNext) {
        holdNext = false
        return new Promise<Response>((resolve) => { release = () => resolve(Response.json(result)) })
      }
      return Response.json(result)
    }
    if (url.endsWith("/tasks")) return Response.json({ status: true, tasks: [] })
    if (url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(controller) {
      controllers.push(controller)
      controller.enqueue(attach(generation))
      init?.signal?.addEventListener("abort", () => { try { controller.close() } catch { /* already ended */ } }, { once: true })
    } }))
    throw new Error(`Unexpected fake request ${url}`)
  }))
})
afterEach(() => {
  stop?.()
  release?.()
  for (const c of controllers) { try { c.close() } catch { /* already ended */ } }
  try { chatController?.close() } catch { /* already ended */ }
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function setup() {
  const { useChatStore: chat } = await import("./useChatStore")
  const { useUiStore: ui } = await import("./useUiStore")
  await chat.getState().reconcileSessions()
  ui.getState().startSchedulerPolling()
  stop = () => ui.getState().stopSchedulerPolling()
  await vi.waitFor(() => expect(controllers.length).toBe(1))
  await vi.advanceTimersByTimeAsync(1)
  return chat
}

describe("retention policy follows the existing feed and snapshot loop", () => {
  it("refreshes policy during the existing slow reconciliation tick", async () => {
    const chat = await setup()
    policy = { ...policy, retention_days: 7 }
    await vi.advanceTimersByTimeAsync(30000)
    await vi.waitFor(() => expect(chat.getState().retentionPolicy?.retention_days).toBe(7))
  })

  it("invalidates and reloads policy when the backend generation changes", async () => {
    const chat = await setup()
    policy = { ...policy, enabled: false }
    holdNext = true
    generation = "second"
    controllers[0].enqueue(attach(generation))
    await vi.waitFor(() => expect(chat.getState().retentionPolicy).toBeNull())
    expect(release).toBeDefined()
    release!()
    release = undefined
    await vi.waitFor(() => expect(chat.getState().retentionPolicy?.enabled).toBe(false))
  })

  it("cannot replace a fresh policy with a late older index response", async () => {
    const chat = await setup()
    stop?.()
    holdNext = true
    const old = chat.getState().reconcileSessions()
    await vi.waitFor(() => expect(release).toBeDefined())
    policy = { ...policy, retention_days: 7 }
    await chat.getState().reconcileSessions()
    release!()
    release = undefined
    await old
    expect(chat.getState().retentionPolicy?.retention_days).toBe(7)
  })

  it("detaches a removed active conversation during periodic index reconciliation", async () => {
    const chat = await setup()
    stop?.()
    chat.setState({ activeId: "removed", conversations: [{ id: "removed", title: "old", updatedAt: 0,
      turnCount: 1, pinned: false, historyLoaded: false, messages: [] }] })
    await chat.getState().loadHistory("removed")
    await vi.waitFor(() => expect(chatController).toBeDefined())
    chat.setState({ pendingApprovals: { removed: [] }, generating: { removed: true } })
    await chat.getState().reconcileSessions()
    expect(chat.getState().activeId).not.toBe("removed")
    expect(chatAborted).toBe(true)
    expect(chat.getState().pendingApprovals.removed).toBeUndefined()
    expect(chat.getState().generating.removed).toBeUndefined()
  })
})
