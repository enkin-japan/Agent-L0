import { readFileSync } from "node:fs"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { EventEnvelope } from "@/types/protocol"

let stream: ReadableStreamDefaultController<Uint8Array> | undefined
beforeEach(() => {
  vi.resetModules()
  stream = undefined
  const storage = new Map<string, string>()
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("/events?")) return new Response(new ReadableStream({ start(controller) {
      stream = controller
      init?.signal?.addEventListener("abort", () => controller.close(), { once: true })
    } }))
    if (url.endsWith("/turn")) return Response.json({ turn: null, last_seq: 0 })
    if (url.endsWith("/history")) return Response.json({ turns: [] })
    if (url.endsWith("/task")) return Response.json({ active: false })
    throw new Error(`Unexpected fake request ${url}`)
  }))
})
afterEach(() => { try { stream?.close() } catch { /* stream already closed */ }; vi.unstubAllGlobals() })

for (const kind of ["approval", "suspended"]) {
  for (const replay of [false, true]) {
    it(`discards ${kind} without changing history (${replay ? "persisted replay" : "live terminal"})`, async () => {
      const number = kind === "approval" ? "012" : "013"
      const events: EventEnvelope[] = readFileSync(new URL(`../../../protocol/fixtures/${number}-discard-paused-${kind}.events.jsonl`, import.meta.url), "utf8")
        .trim().split("\n").map((line) => JSON.parse(line))
      if (replay) events[events.length - 1].data = { status: "cancelled", discarded: true }
      const { useChatStore: store } = await import("./useChatStore")
      store.setState({ activeId: "r11", conversations: [{ id: "r11", title: "r11", updatedAt: 0, turnCount: 0, pinned: false, historyLoaded: true, messages: [] }] })
      await store.getState().loadHistory("r11")
      await vi.waitFor(() => expect(stream).toBeDefined())
      const emit = async (values: EventEnvelope[]) => {
        stream!.enqueue(new TextEncoder().encode(values.map((env) => `data: ${JSON.stringify(env)}\n\n`).join("")))
        await new Promise((resolve) => setTimeout(resolve, 0))
      }
      await emit(events.slice(0, 3))
      const before = store.getState().conversations[0].messages
      await emit(events.slice(3))
      expect(store.getState().conversations[0].turnCount).toBe(1)
      expect(store.getState().conversations[0].messages).toEqual(before)
      expect(store.getState().pendingApprovals.r11).toBeUndefined()
      expect(store.getState().suspendedSessions.r11).toBeUndefined()
      expect(store.getState().generating.r11).toBe(false)
    })
  }
}
