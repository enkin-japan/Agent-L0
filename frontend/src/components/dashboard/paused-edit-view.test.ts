import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { Conversation, ChatMessage as Message } from "@/lib/agent-data"

const captured = vi.hoisted(() => ({ messages: [] as Array<{ message: Message; editable: boolean; editPrompt?: string; editNote?: string }> }))
// Transparent wrapper: real ChatArea, React SSR and ChatMessage still render.
vi.mock("./chat-message", async (original) => {
  const actual = await original<typeof import("./chat-message")>()
  return { ...actual, ChatMessage: (props: typeof captured.messages[number]) => {
    captured.messages.push(props)
    return createElement(actual.ChatMessage, props)
  } }
})
beforeEach(() => { captured.messages = []; vi.stubGlobal("localStorage", { getItem: () => "zh", setItem: () => {} }) })
afterEach(() => vi.unstubAllGlobals())
const archived = { status: "success" as const, prompt: "ARCHIVED_A", image: [], audio: [], enable_thinking: false, max_tokens: 1, temperature: 1, min_p: 0, response: [] }
async function render(phase: "awaiting_approval" | "suspended" | "none", prompt: string, archive = true, retry = false) {
  const { ChatArea } = await import("./chat-area")
  const { TooltipProvider } = await import("@/components/ui/tooltip")
  const noop = () => {}
  const conversation: Conversation = { id: "A", title: "A", updatedAt: 0, historyLoaded: true, turnCount: archive ? 2 : 0,
    messages: [
      ...(archive ? [{ id: "a-user", role: "user" as const, turnIndex: 0, content: archived.prompt }, { id: "a-answer", role: "assistant" as const, turnIndex: 0, content: "A answer" }] : []),
      ...(!retry ? [{ id: "b-user", role: "user" as const, content: prompt, attachments: [{ name: "separate-report.pdf", kind: "file" as const }] }] : []),
    ],
    lastCanonicalTurn: archive ? { index: 1, prompt: "CLOSED_RETRY_TAIL" } : undefined,
    canonicalTurns: archive ? [archived, { ...archived, status: "cancelled" as const, prompt: "CLOSED_RETRY_TAIL" }] : [],
    retryGroups: archive ? [{ root_index: 0, display_index: 0, turn_indices: [0, 1] }] : [],
  }
  const html = renderToStaticMarkup(createElement(TooltipProvider, {}, createElement(ChatArea, {
    conversation, thinkingEnabled: false, generating: false, turnPhase: phase, control: null,
    notice: null, metrics: null, sidebarHidden: false, pendingTools: phase === "awaiting_approval" ? [{ name: "write_file" }] : null,
    suspended: phase === "suspended", taskProgress: null, activity: null, contextOpen: false,
    onToggleContext: noop, onApproval: noop, onSend: noop, onStop: noop, onSuspend: noop, onResume: noop, onAbandon: noop,
    onBranch: noop, onUpdateLast: noop, onDeleteLast: noop, onRename: noop, onDismissNotice: noop, onOpenMobileSidebar: noop, onShowSidebar: noop,
  })))
  return { html, editable: captured.messages.find((props) => props.editable)! }
}
for (const phase of ["awaiting_approval", "suspended"] as const) {
  for (const scenario of [
    { name: "archived A, paused B", prompt: "PAUSED_B", archive: true },
    { name: "empty raw input with attachment", prompt: "", archive: true },
    { name: "literal newlines and paperclip separate from attachment", prompt: "first line\n📎 this is raw input\nlast line", archive: true },
    { name: "first paused turn without archived input", prompt: "FIRST_PAUSED_B", archive: false },
  ]) it(`${phase}: ${scenario.name} keeps actual raw input without a closed retry override`, async () => {
    const { html, editable } = await render(phase, scenario.prompt, scenario.archive)
    expect(editable.message.id).toBe("b-user")
    expect(editable.message.content).toBe(scenario.prompt)
    expect(editable.editPrompt).toBeUndefined()
    expect(editable.editNote).toBeUndefined()
    expect(captured.messages.every((props) => props.editPrompt === undefined && props.editNote === undefined)).toBe(true)
    expect(editable.message.attachments).toEqual([{ name: "separate-report.pdf", kind: "file" }])
    expect(html).toContain("separate-report.pdf")
    expect(html).not.toContain("编辑/删除作用于最近一次真实尝试")
  })
}
it("closed retry still edits canonical tail and retains its recent-attempt note", async () => {
  const { html, editable } = await render("none", "", true, true)
  expect(editable.message.content).toBe("ARCHIVED_A")
  expect(editable.editPrompt).toBe("CLOSED_RETRY_TAIL")
  expect(editable.editNote).toContain("轮次 2")
  expect(html).toContain("编辑/删除作用于最近一次真实尝试：轮次 2")
})
