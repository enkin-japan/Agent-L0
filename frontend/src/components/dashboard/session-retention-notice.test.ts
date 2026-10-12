import { renderToStaticMarkup } from "react-dom/server"
import { createElement } from "react"
import { describe, expect, it, vi, afterEach } from "vitest"
import { getDict } from "@/lib/i18n"
import { SessionRetentionNotice } from "./session-retention-notice"
import { setSessionPin } from "@/api/agent"

afterEach(() => vi.unstubAllGlobals())

describe("server retention policy", () => {
  it("uses server values without inventing a default or exact deletion time", () => {
    const html = renderToStaticMarkup(createElement(SessionRetentionNotice, { text: getDict("en").retention,
      policy: { enabled: true, retention_days: 7.5, empty_ttl_hours: 3, scan_interval_seconds: 120 } }))
    expect(html).toContain("7.5 days")
    expect(html).toContain("3 hours")
    expect(html).toContain("Pinned, scheduled and in-progress")
    expect(html).toContain("periodically")
    expect(html).not.toContain("30 days")
  })

  it("distinguishes an unknown policy from disabled cleanup", () => {
    const text = getDict("en").retention
    expect(renderToStaticMarkup(createElement(SessionRetentionNotice, { text }))).toContain(text.unknown)
    const html = renderToStaticMarkup(createElement(SessionRetentionNotice, { text,
      policy: { enabled: false, retention_days: 7, empty_ttl_hours: 3, scan_interval_seconds: 120 } }))
    expect(html).toContain(text.disabled)
    expect(html).not.toContain("7 days")
    expect(html).not.toContain("periodically")
  })

  it("rejects an HTTP200 pin response that did not persist", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ status: false, response: "not saved" }))))
    await expect(setSessionPin("synthetic", true)).rejects.toThrow("not saved")
  })

  it("projects the received policy through the actual session store", async () => {
    const storage = new Map<string, string>()
    vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value) })
    const { useChatStore } = await import("@/store/useChatStore")
    const policy = { enabled: false, retention_days: 42, empty_ttl_hours: 2, scan_interval_seconds: 123 }
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ status: true, sessions: [], retention_policy: policy }))))
    await useChatStore.getState().reconcileSessions()
    expect(useChatStore.getState().retentionPolicy).toEqual(policy)
  })
})
