import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { TaskControlView } from "@/store/useChatStore"
import type { TurnStateKind } from "@/types/protocol"

beforeEach(() => { vi.resetModules(); vi.stubGlobal("localStorage", { getItem: () => "zh", setItem: () => {} }) })
afterEach(() => vi.unstubAllGlobals())
async function chat(phase: TurnStateKind, control: TaskControlView | null = null) {
  const { ChatArea } = await import("./chat-area")
  const { TooltipProvider } = await import("@/components/ui/tooltip")
  const noop = () => {}
  return renderToStaticMarkup(createElement(TooltipProvider, {}, createElement(ChatArea, {
    conversation: { id: "A", title: "A", messages: [], updatedAt: 0, historyLoaded: true, turnCount: 0 },
    thinkingEnabled: false, generating: ["running", "queued"].includes(phase), turnPhase: phase, control,
    notice: null, metrics: null, sidebarHidden: false, pendingTools: null, suspended: phase === "suspended",
    taskProgress: null, activity: null, contextOpen: false,
    onToggleContext: noop, onApproval: noop, onSend: noop, onStop: noop, onSuspend: noop, onResume: noop,
    onAbandon: noop, onBranch: noop, onUpdateLast: noop, onDeleteLast: noop, onRename: noop,
    onDismissNotice: noop, onOpenMobileSidebar: noop, onShowSidebar: noop,
  })))
}
it("the actual queued chat omits suspend and explains stopping external effects", async () => {
  const html = await chat("queued")
  expect(html).not.toContain("挂起任务（")
  expect(html).toContain("停止后续执行（已完成的操作不会自动撤销）")
  expect(html).toContain("排队中")
})
it("running chat offers suspend and leaves acknowledged request visible", async () => {
  const html = await chat("running", { requestId: "r", sessionId: "A", turnId: "T1", action: "suspend", status: "accepted", message: "挂起请求已受理，等待安全点" })
  expect(html).toContain("挂起任务（")
  expect(html).toContain("挂起请求已受理，等待安全点")
  const suspend = [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map((m) => m[0]).find((button) => button.includes("挂起任务（"))
  expect(suspend).toContain("disabled=")
})
for (const kind of ["summarize", "prewarm"]) it(`global ${kind} workers offer no misleading stop guarantee`, async () => {
  const { SchedulerStrip } = await import("./scheduler-strip")
  const html = renderToStaticMarkup(createElement(SchedulerStrip, {
    tasks: { status: true, running: [kind], queued: 0, suspended: 0, tasks: [{ session_id: kind, status: "running", kind, running_seconds: 1, waiting_seconds: 0 }] },
    tasksState: "fresh", tasksError: null, conversations: [], activeId: "A", onSelect: () => {},
  }))
  expect(html).toContain("当前没有可确认的轮次")
  expect(html).toContain(kind === "summarize" ? "摘要" : "预热")
  expect(html).not.toContain("停止后续执行")
})
