import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
let response: () => Promise<Response>, release: (() => void) | undefined
let streams: ReadableStreamDefaultController<Uint8Array>[]
let feeds: ReadableStreamDefaultController<Uint8Array>[], turnSeq: number, stopPolling: (() => void) | undefined
const list = { status: true, files: [{ name: "report.pdf", size: 1024, mtime: 1234 }] }
beforeEach(() => {
  vi.resetModules(); streams = []; feeds = []; turnSeq = 0; stopPolling = undefined; release = undefined; response = async () => Response.json(list)
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === "macagent-lang" ? "zh" : null, setItem: () => {} })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (/\/files\/[AB]$/.test(url)) return response()
    if (url.endsWith("/history")) return Response.json({ status: true, turns: [], count: 0, replay_after: 0 })
    if (url.endsWith("/turn")) return Response.json({ turn: null, last_seq: turnSeq })
    if (url.endsWith("/task")) return Response.json({ active: false })
    if (url.endsWith("/tasks")) return Response.json({ status: true, running: [], queued: 0, suspended: 0, tasks: [] })
    if (url.endsWith("/sessions")) return Response.json({ status: true, sessions: [] })
    if (url.includes("/api/v1/events?")) return new Response(new ReadableStream<Uint8Array>({ start(c) { feeds.push(c); init?.signal?.addEventListener("abort", () => { try { c.close() } catch { /* closed */ } }, { once: true }) } }))
    if (url.includes("/events?")) return new Response(new ReadableStream<Uint8Array>({ start(c) { streams.push(c); init?.signal?.addEventListener("abort", () => { try { c.close() } catch { /* closed */ } }, { once: true }) } }))
    throw Error(`Unexpected synthetic request ${url}`)
  }))
})
afterEach(() => { stopPolling?.(); release?.(); for (const c of [...streams, ...feeds]) { try { c.close() } catch { /* closed */ } }; vi.unstubAllGlobals() })
async function setup() {
  const { useChatStore: chat } = await import("./useChatStore"), { useUiStore: ui } = await import("./useUiStore")
  chat.setState({ activeId: "A", conversations: ["A", "B"].map((id) => ({ id, title: id, messages: [], turnCount: 0, historyLoaded: false, updatedAt: 0 })) })
  release = ui.getState().watchFiles("A")
  return { chat, ui }
}
it("refresh finds actual assets even without response links, with loading and fresh states", async () => {
  let resolve!: (r: Response) => void; response = () => new Promise((r) => { resolve = r })
  const { ui } = await setup(); await vi.waitFor(() => expect(resolve).toBeDefined())
  expect(ui.getState().files?.state).toBe("loading")
  resolve(Response.json(list)); await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  expect(ui.getState().files?.data).toEqual(list.files)
})
for (const business of [false, true]) it(`failure retains known files and explicit stale feedback (business=${business})`, async () => {
  const { ui } = await setup(); await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  response = async () => business ? Response.json({ status: false, files: [] }) : new Response("offline", { status: 503 })
  await ui.getState().refreshFiles()
  expect(ui.getState().files?.state).toBe("stale"); expect(ui.getState().files?.data).toEqual(list.files)
  expect(ui.getState().files?.error).toBeTruthy()
})
it("initial failure is unknown, distinct from an authoritative empty asset list", async () => {
  response = async () => new Response("offline", { status: 503 }); const { ui } = await setup()
  await vi.waitFor(() => expect(ui.getState().files?.error).toBeTruthy())
  expect(ui.getState().files?.state).toBe("unknown"); expect(ui.getState().files?.data).toBeNull()
  response = async () => Response.json({ status: true, files: [] }); await ui.getState().refreshFiles()
  expect(ui.getState().files?.state).toBe("fresh"); expect(ui.getState().files?.data).toEqual([])
})
it("late A HTTP result cannot overwrite B or a reselected A observer; old cleanup cannot release the new observer", async () => {
  let resolve!: (r: Response) => void; response = () => new Promise((r) => { resolve = r })
  const { chat, ui } = await setup(); await vi.waitFor(() => expect(resolve).toBeDefined()); const oldRelease = release!
  response = async () => Response.json({ status: true, files: [{ name: "new.pdf", size: 1, mtime: 2 }] })
  chat.getState().selectConversation("B"); release = ui.getState().watchFiles("B")
  await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  chat.getState().selectConversation("A"); release = ui.getState().watchFiles("A"); oldRelease()
  await vi.waitFor(() => expect(ui.getState().files?.sessionId).toBe("A")); await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  resolve(Response.json(list)); await new Promise((r) => setTimeout(r, 5))
  expect(ui.getState().files?.data?.[0].name).toBe("new.pdf"); expect(ui.getState().isWatchingFiles("A")).toBe(true)
})
it("unmounted observer's late failure cannot alter a new observer status or error", async () => {
  let resolve!: (r: Response) => void; response = () => new Promise((r) => { resolve = r })
  const { ui } = await setup(); await vi.waitFor(() => expect(resolve).toBeDefined()); release!()
  response = async () => Response.json(list); release = ui.getState().watchFiles("A")
  await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh")); resolve(new Response("old failure", { status: 503 }))
  await new Promise((r) => setTimeout(r, 5)); expect(ui.getState().files?.error).toBeNull(); expect(ui.getState().files?.state).toBe("fresh")
})
it("selection ABA alone rejects old file result even before the view replaces its observer", async () => {
  let resolve!: (r: Response) => void; response = () => new Promise((r) => { resolve = r })
  const { chat, ui } = await setup(); await vi.waitFor(() => expect(resolve).toBeDefined())
  chat.getState().selectConversation("B"); chat.getState().selectConversation("A")
  resolve(Response.json(list)); await new Promise((r) => setTimeout(r, 5))
  expect(ui.getState().files?.data).toBeNull(); expect(ui.getState().files?.error).toBeNull()
})
for (const status of ["success", "warning", "fail", "cancelled"]) it(`actual session SSE terminal refreshes existing files projection (${status})`, async () => {
  const { chat, ui } = await setup(); await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  await chat.getState().loadHistory("A"); await vi.waitFor(() => expect(streams).toHaveLength(1))
  response = async () => Response.json({ status: true, files: [{ name: "completed.txt", size: 50, mtime: 99 }] })
  streams[0].enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ seq: 2, type: "turn_ended", turn_id: "t", data: { status, prompt: "task", response: [], turn_index: 0 } })}\n\n`))
  await vi.waitFor(() => expect(ui.getState().files?.data?.[0].name).toBe("completed.txt"))
})
it("safe download URLs stay on the existing same-origin asset route with versioned mtime", async () => {
  const { sessionAssetUrl } = await import("@/api/agent")
  expect(sessionAssetUrl("A", "report.pdf", 1)).toBe("/api/v1/files/A/assets/report.pdf?mtime=1")
  expect(sessionAssetUrl("A", "report.pdf", 2)).not.toBe(sessionAssetUrl("A", "report.pdf", 1))
  for (const name of ["../secret", "foo..txt", "/etc/passwd", "https://outside/file", "a%2Fb", "a\n.txt"]) expect(sessionAssetUrl("A", name, 1)).toBeNull()
})
it("actual files SSR has download-only links, stale/unknown feedback and actual retention policy", async () => {
  const { ui, chat } = await setup(); await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  chat.setState({ retentionPolicy: { enabled: true, retention_days: 7, empty_ttl_hours: 2, scan_interval_seconds: 3 } })
  const { FilesView } = await import("@/components/dashboard/files-panel")
  let html = renderToStaticMarkup(createElement(FilesView, { sessionId: "A", snapshot: ui.getState().files, policy: chat.getState().retentionPolicy, onRefresh: ui.getState().refreshFiles }))
  expect(html).toContain("report.pdf"); expect(html).toContain("download="); expect(html).toContain("7"); expect(html).toContain("2")
  response = async () => new Response("offline", { status: 503 }); await ui.getState().refreshFiles()
  html = renderToStaticMarkup(createElement(FilesView, { sessionId: "A", snapshot: ui.getState().files, policy: chat.getState().retentionPolicy, onRefresh: ui.getState().refreshFiles })); expect(html).toContain("report.pdf"); expect(html).toContain("刷新")
  ui.setState({ files: { sessionId: "A", data: null, state: "unknown", error: "unavailable" } })
  html = renderToStaticMarkup(createElement(FilesView, { sessionId: "A", snapshot: ui.getState().files, policy: chat.getState().retentionPolicy, onRefresh: ui.getState().refreshFiles })); expect(html).toContain("未知"); expect(html).not.toContain("当前没有产物")
})
it("Inspector keeps Deck alongside the files entrance without squeezing five tabs into one row", async () => {
  const { InspectorPanel } = await import("@/components/dashboard/inspector-panel")
  const { currentDict } = await import("./useUiStore")
  const html = renderToStaticMarkup(createElement(InspectorPanel, { conversation: { id: "A", title: "A", messages: [], turnCount: 0, historyLoaded: true, updatedAt: 0 }, taskProgress: null, onClose: () => {} }))
  expect(html).toContain(currentDict().files.title); expect(html).toContain(currentDict().inspector.tabDeck)
  expect(html).toContain("grid-cols-3")
})
for (const seq of [5, 8]) it(`terminal refresh is independent of a prior /turn snapshot cursor ${seq}`, async () => {
  const { chat, ui } = await setup(); await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  turnSeq = seq; await chat.getState().loadHistory("A"); await vi.waitFor(() => expect(streams).toHaveLength(1))
  expect(chat.getState().turnStates.A.seq).toBe(seq)
  response = async () => Response.json({ status: true, files: [{ name: "snapshot-window.txt", size: 1, mtime: 10 }] })
  streams[0].enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ seq: 5, type: "turn_ended", turn_id: "old", data: { status: "cancelled", prompt: "old", response: [], turn_index: 0 } })}\n\n`))
  await vi.waitFor(() => expect(ui.getState().files?.data?.[0].name).toBe("snapshot-window.txt"))
  const reads = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/files/A")).length
  chat.getState().observeTaskEvent("A", { seq: 5, type: "turn_ended", turn_id: "old" })
  await new Promise((r) => setTimeout(r, 5))
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/files/A"))).toHaveLength(reads)
})
it("actual global feed generation replay does not duplicate refresh, while a new journal terminal does", async () => {
  const { chat, ui } = await setup(); await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  // Model the already-restored journal snapshot through the actual store projection.
  chat.getState().observeTaskEvent("A", { seq: 8, type: "turn_started", turn_id: "newer" })
  ui.getState().startSchedulerPolling(); stopPolling = () => ui.getState().stopSchedulerPolling()
  await vi.waitFor(() => expect(feeds).toHaveLength(1))
  const frame = (controller: ReadableStreamDefaultController<Uint8Array>, item: object) => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(item)}\n\n`))
  frame(feeds[0], { type: "stream_attached", data: { generation: "g1" } })
  response = async () => Response.json({ status: true, files: [{ name: "feed-window.txt", size: 1, mtime: 10 }] })
  frame(feeds[0], { type: "turn_ended", feed_seq: 10, seq: 5, session_id: "A", turn_id: "old", data: {} })
  await vi.waitFor(() => expect(ui.getState().files?.data?.[0].name).toBe("feed-window.txt"))
  const reads = () => vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/files/A")).length
  const before = reads(); frame(feeds[0], { type: "stream_attached", data: { generation: "g2" } })
  await vi.waitFor(() => expect(feeds).toHaveLength(2), { timeout: 3000 })
  frame(feeds[1], { type: "stream_attached", data: { generation: "g2" } })
  frame(feeds[1], { type: "turn_ended", feed_seq: 1, seq: 5, session_id: "A", turn_id: "old", data: {} })
  await new Promise((r) => setTimeout(r, 5)); expect(reads()).toBe(before)
  response = async () => Response.json({ status: true, files: [{ name: "new-generation.txt", size: 1, mtime: 10 }] })
  frame(feeds[1], { type: "turn_ended", feed_seq: 2, seq: 1000005, session_id: "A", turn_id: "new", data: {} })
  await vi.waitFor(() => expect(ui.getState().files?.data?.[0].name).toBe("new-generation.txt"))
  expect(reads()).toBe(before + 1)
})
it("a terminal for another session cannot refresh the current single observer", async () => {
  const { chat, ui } = await setup(); await vi.waitFor(() => expect(ui.getState().files?.state).toBe("fresh"))
  const reads = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/files/A")).length
  chat.getState().observeTaskEvent("B", { seq: 50, type: "turn_ended", turn_id: "other" })
  await new Promise((r) => setTimeout(r, 5))
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/files/A"))).toHaveLength(reads)
  expect(ui.getState().files?.sessionId).toBe("A")
})
