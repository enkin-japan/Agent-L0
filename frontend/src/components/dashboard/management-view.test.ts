import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { DeletionReceipt } from "@/store/useChatStore"

const probe = vi.hoisted(() => ({ selection: new Set<string>(), sidebarHooks: 0, buttons: [] as Array<Record<string, unknown>>, feedback: [] as unknown[] }))
const observe = vi.hoisted(() => (props: Record<string, unknown> | null) => { if (props?.onClick) probe.buttons.push(props) })
// Execute actual SSR and actual event callbacks. Only the initial sidebar
// selection is seeded; setter updater functions are observed after SSR instead
// of requesting a browser render. No delete/selection implementation is copied.
vi.mock("react", async (original) => {
  const actual = await original<typeof import("react")>()
  return { ...actual,
    createElement: (...args: Parameters<typeof actual.createElement>) => { observe({ ...args[1], children: args.slice(2) }); return actual.createElement(...args) },
    useState: (initial: unknown) => {
      const sidebar = new Error().stack?.includes("AgentSidebar") === true
      const index = sidebar ? ++probe.sidebarHooks : 0
      const seed = sidebar && initial instanceof Set ? probe.selection : sidebar && index === 2 ? true : initial
      const [value] = actual.useState(seed)
      return [value, (next: unknown) => {
        if (sidebar && initial instanceof Set) probe.selection = typeof next === "function" ? (next as (s: Set<string>) => Set<string>)(probe.selection) : next as Set<string>
        else if (sidebar) probe.feedback.push(next)
      }]
    } }
})
vi.mock("react/jsx-runtime", async (original) => {
  const actual = await original<typeof import("react/jsx-runtime")>()
  return { ...actual,
    jsx: (...args: Parameters<typeof actual.jsx>) => { observe(args[1]); return actual.jsx(...args) },
    jsxs: (...args: Parameters<typeof actual.jsxs>) => { observe(args[1]); return actual.jsxs(...args) } }
})
vi.mock("react/jsx-dev-runtime", async (original) => {
  const actual = await original<typeof import("react/jsx-dev-runtime")>()
  return { ...actual, jsxDEV: (...args: Parameters<typeof actual.jsxDEV>) => { observe(args[1]); return actual.jsxDEV(...args) } }
})
beforeEach(() => {
  vi.resetModules(); probe.selection = new Set(["A", "B", "C"]); probe.sidebarHooks = 0; probe.buttons = []; probe.feedback = []
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === "macagent-lang" ? "zh" : key === "macagent-chat-v1" ? JSON.stringify({ activeId: "A", conversations: ["A", "B", "C"].map((id) => ({ id, title: id, turnCount: 0, updatedAt: 0 })) }) : null, setItem: () => {} })
  vi.stubGlobal("window", { confirm: () => false })
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })
function button(label: string) {
  const item = probe.buttons.find((props) => props.children === label || (Array.isArray(props.children) && props.children.includes(label)))
  expect(item, `actual button ${label}`).toBeDefined()
  return item!.onClick as () => Promise<void>
}
async function sidebar(onDeleteMany: (ids: string[]) => Promise<DeletionReceipt[] | null>) {
  const { AgentSidebar } = await import("./agent-sidebar"), { TooltipProvider } = await import("@/components/ui/tooltip")
  const noop = () => {}
  renderToStaticMarkup(createElement(TooltipProvider, {}, createElement(AgentSidebar, {
    conversations: ["A", "B", "C"].map((id) => ({ id, title: id, turnCount: 0, updatedAt: 0, historyLoaded: true, messages: [] })), activeId: "A",
    onDeleteMany, onDelete: noop, onSelect: noop, onNewChat: noop, onOpenScheduleSession: noop, onRename: noop, onTogglePin: noop, onCollapse: noop,
    thinkingEnabled: false, onThinkingChange: noop,
  })))
}
it("actual sidebar callback retains failed selection and any choices added while awaiting confirmed receipts", async () => {
  let resolve!: (value: DeletionReceipt[]) => void
  const onDeleteMany = vi.fn(() => new Promise<DeletionReceipt[]>((r) => { resolve = r }))
  await sidebar(onDeleteMany); const pending = button("删除")()
  expect(onDeleteMany).toHaveBeenCalledWith(["A", "B", "C"])
  probe.selection.add("D")
  resolve([{ id: "A", status: "deleted", message: "done" }, { id: "B", status: "failed", message: "offline" }, { id: "C", status: "deleted", message: "done" }])
  await pending; expect([...probe.selection]).toEqual(["B", "D"])
  expect(probe.feedback).toContain("B: offline")
})
it("actual sidebar cancellation and thrown failure preserve the selected ids", async () => {
  await sidebar(async () => null); await button("删除")(); expect([...probe.selection]).toEqual(["A", "B", "C"])
})
it("actual sidebar rejected transport keeps selection and reports the error", async () => {
  await sidebar(async () => { throw new Error("synthetic offline") }); await button("删除")()
  expect([...probe.selection]).toEqual(["A", "B", "C"]); expect(probe.feedback.some((value) => typeof value === "string" && value.includes("synthetic offline"))).toBe(true)
})
it("exiting selection invalidates an old receipt, which cannot clear a later selection", async () => {
  let resolve!: (value: DeletionReceipt[]) => void
  await sidebar(() => new Promise((r) => { resolve = r })); const pending = button("删除")()
  await button("完成")(); probe.selection = new Set(["A"])
  resolve([{ id: "A", status: "deleted", message: "old" }]); await pending
  expect([...probe.selection]).toEqual(["A"])
})
it("actual App confirmation cancellation reaches the sidebar as cancellation, with no DELETE", async () => {
  const { useChatStore: chat } = await import("@/store/useChatStore")
  const deleting = vi.spyOn(chat.getState(), "deleteConversations")
  const { default: Page } = await import("@/App")
  renderToStaticMarkup(createElement(Page)); await button("删除")()
  expect(deleting).not.toHaveBeenCalled(); expect([...probe.selection]).toEqual(["A", "B", "C"])
  expect(probe.feedback).not.toContain("已删除所选会话。")
})
it("actual ChatArea disables input only for the supplied deleting target and shows the pending explanation", async () => {
  const { ChatArea } = await import("./chat-area"), { TooltipProvider } = await import("@/components/ui/tooltip")
  const noop = () => {}
  const html = renderToStaticMarkup(createElement(TooltipProvider, {}, createElement(ChatArea, {
    conversation: { id: "A", title: "A", messages: [], updatedAt: 0, turnCount: 0, historyLoaded: true }, managementPending: true,
    thinkingEnabled: false, generating: false, turnPhase: "none", control: null, notice: null, metrics: null, sidebarHidden: false,
    pendingTools: null, suspended: false, taskProgress: null, activity: null, contextOpen: false, onToggleContext: noop, onApproval: noop,
    onSend: noop, onStop: noop, onSuspend: noop, onResume: noop, onAbandon: noop, onBranch: noop, onUpdateLast: noop, onDeleteLast: noop,
    onRename: noop, onDismissNotice: noop, onOpenMobileSidebar: noop, onShowSidebar: noop,
  })))
  expect(html).toContain("正在删除此会话"); expect(html.match(/<textarea[^>]*disabled=""/)).not.toBeNull()
})
