import { create } from "zustand"

import {
  attachEvents,
  branchSession,
  cancelTask,
  deleteLastTurn as apiDeleteLastTurn,
  deleteSession,
  getHistory,
  getTaskStatus,
  getTurnState,
  HttpError,
  listSessions,
  setSessionMeta,
  setSessionPin,
  submitApproval,
  submitResume,
  submitTurn,
  suspendTask as apiSuspendTask,
  TurnConflictError,
} from "@/api/agent"
import type {
  ChatResponse,
  CancelResponse,
  LiveActivity,
  PendingTool,
  SessionRetentionPolicy,
  TaskSnapshot,
} from "@/types/agent-api"
import type { EventEnvelope, TurnAccepted, TurnStateKind, TurnStateResponse } from "@/types/protocol"
import {
  initialTurnState,
  reduceTurnEvent,
  type NormalizedState,
} from "@/lib/turn-reducer"
import {
  newSessionId,
  nextMessageId,
  turnToMessages,
  historyProjection,
  type ChatMessage,
  type Conversation,
  type MessageAttachment,
} from "@/lib/agent-data"
import { currentDict, useUiStore } from "@/store/useUiStore"

const STORAGE_KEY = "macagent-chat-v1"
/** 发送暂存（outbox）：提交前落 localStorage,202 受理即清——浏览器在 multipart
 *  上传中途被杀(刷新)时,这是唯一能证明"有话没送达"的凭据(PROTOCOL.md §5)。 */
const OUTBOX_KEY = "macagent-outbox-v1"
/** 语言无关的「新对话」标题哨兵：存储用此值，展示时按当前语言翻译（见 lib/agent-data 的 convTitle）。 */
export const NEW_CHAT_TITLE = "__NEW_CHAT__"

/** 落盘到 localStorage 的部分：会话元数据（消息刷新后从 /history 恢复）。 */
type PersistedState = {
  conversations: Array<
    Pick<Conversation, "id" | "title" | "updatedAt" | "turnCount" | "pinned">
  >
  activeId: string
  thinkingEnabled: boolean
}

function freshConversation(): Conversation {
  return {
    id: newSessionId(),
    title: NEW_CHAT_TITLE,
    updatedAt: Date.now(),
    messages: [],
    // 新会话在服务端还不存在，没有历史可拉
    historyLoaded: true,
    turnCount: 0,
    pinned: false,
  }
}

function loadInitialState() {
  let persisted: PersistedState | null = null
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) persisted = JSON.parse(raw) as PersistedState
  } catch {
    // 存储损坏时从零开始
  }

  const conversations: Conversation[] = (persisted?.conversations ?? []).map(
    (c) => ({ ...c, messages: [], historyLoaded: false }),
  )
  if (conversations.length === 0) conversations.push(freshConversation())

  const activeId = conversations.some((c) => c.id === persisted?.activeId)
    ? (persisted!.activeId as string)
    : conversations[0].id

  return {
    conversations,
    activeId,
    thinkingEnabled: persisted?.thinkingEnabled ?? false,
  }
}

// ---------- 事件附着（账本协议）：模块级簿记,不进 zustand ----------
// 单附着策略：同一时刻只对活跃会话保持一条 /events 长连（HTTP/1.1 每源连接数有限,
// 全量附着会饿死其他请求）;切走即断开并置 historyLoaded=false,切回经
// /history + /turn + 重放完整恢复。后台会话的调度状态仍由 useUiStore 的 /tasks 轮询兜底。
const attachCtls = new Map<string, AbortController>()
const cursors = new Map<string, number>()
const liveTurns = new Map<string, NormalizedState>()
/** 预览气泡的合并刷新计时器：token/thinking 增量攒 ~90ms 投影一次（与原生端
 *  AgentStore 的流式节流同拍）。归约本身每事件照做，节流的只是 UI 投影。 */
const previewFlush = new Map<string, ReturnType<typeof setTimeout>>()

/** 撤销消息附件的本地 blob 预览 URL。createObjectURL 不 revoke 会把整个 Blob
 *  （原图字节）钉在内存里直到页面关闭——实测 Safari 内存重载的确定性泄漏项。 */
function revokePreviewUrls(messages: ChatMessage[]) {
  for (const m of messages) {
    for (const a of m.attachments ?? []) {
      if (a.previewUrl) {
        try { URL.revokeObjectURL(a.previewUrl) } catch { /* 已失效则忽略 */ }
      }
    }
  }
}
/** 活跃轮的界面簿记：预览气泡/用户气泡的消息 id 与轮次 id 的配对。 */
const liveMsg = new Map<string, { turnId?: string; userId?: string; assistantId?: string; receipt?: string }>()
/** 本 tab 乐观追加的用户气泡（turn_queued 到达时被收编,不重复建）。 */
const optimistic = new Map<string, { userId: string; prompt: string; clientTurnId: string; turnId?: string; seq?: number; receipt: string }>()
const lastEndedIds = new Map<string, string>()

/** 用户已在本地「放弃」的挂起会话：后端句柄要等下条消息才真正回收，期间 /tasks 对账
 *  （useUiStore.syncSuspendedSessions）须跳过这些 id，否则放弃的横幅每 3s 被轮询复活。 */
export const locallyAbandonedSuspended = new Set<string>()

// ---- 发送暂存(outbox) ----
type OutboxEntry = { text: string; ts: number; key?: string; hinted?: boolean; receipt?: string }
type OutboxSnapshot = { all: Record<string, OutboxEntry>; readable: boolean }
// Storage failures must not block submission. Keep at most 32 recent session overlays
// in this tab; tombstones belong to captured receipts, never to an entire session id.
const volatileOutbox = new Map<string, { entry: OutboxEntry | null; receipt?: string; baseIdentity?: string }>()
const VOLATILE_OUTBOX_LIMIT = 32

function outboxIdentity(entry?: OutboxEntry): string | undefined {
  return entry?.receipt ?? (entry ? JSON.stringify([entry.key, entry.text, entry.ts]) : undefined)
}

function persistedOutbox(): OutboxSnapshot {
  try {
    return { all: JSON.parse(localStorage.getItem(OUTBOX_KEY) ?? "{}"), readable: true }
  } catch {
    return { all: {}, readable: false }
  }
}

function mergedOutbox(snapshot: OutboxSnapshot): Record<string, OutboxEntry> {
  const all = { ...snapshot.all }
  for (const [sid, overlay] of volatileOutbox) {
    const disk = all[sid]
    if (!snapshot.readable || !disk || outboxIdentity(disk) === overlay.baseIdentity
        || (overlay.receipt !== undefined && disk?.receipt === overlay.receipt)) {
      if (overlay.entry) all[sid] = overlay.entry
      else delete all[sid]
    }
    else volatileOutbox.delete(sid) // a different disk receipt supersedes this obsolete overlay
    // Another tab has written a different receipt: preserve that newer disk entry.
  }
  return all
}

function rememberOutbox(sid: string, entry: OutboxEntry | null, receipt: string | undefined, baseIdentity: string | undefined) {
  volatileOutbox.delete(sid)
  volatileOutbox.set(sid, { entry, receipt, baseIdentity })
  if (volatileOutbox.size > VOLATILE_OUTBOX_LIMIT) volatileOutbox.delete(volatileOutbox.keys().next().value!)
}

function outboxAll(): Record<string, OutboxEntry> {
  return mergedOutbox(persistedOutbox())
}

function outboxSet(sid: string, text: string, key?: string, hinted?: boolean): OutboxEntry {
  const snapshot = persistedOutbox()
  const all = mergedOutbox(snapshot)
  const entry = hinted && all[sid]
    ? { ...all[sid], hinted, receipt: all[sid].receipt ?? newSessionId() }
    : { text, ts: Date.now(), key, hinted, receipt: newSessionId() }
  all[sid] = entry
  if (snapshot.readable) {
    try {
      localStorage.setItem(OUTBOX_KEY, JSON.stringify(all))
      volatileOutbox.clear() // merged entries/tombstones are now persisted
      return entry
    } catch { /* quota: retain this receipt in this tab */ }
  }
  rememberOutbox(sid, entry, entry.receipt, outboxIdentity(snapshot.all[sid]))
  return entry // never depend on reading storage back to obtain a submit receipt
}

/** 提交幂等键（B3）：同会话同文重发 = 上一次提交的重试,复用旧键让服务端判重;
 *  换了内容才换键。键随 outbox 持久——蜂窝网下「请求已达、202 回程丢失」是常态,
 *  用户看到「无法连接」后手动重发,不复用键就是一次货真价实的重复执行。 */
export function outboxKeyFor(sid: string, text: string): string {
  const pending = outboxAll()[sid]
  if (pending?.key && pending.text === text) return pending.key
  // crypto.randomUUID 仅安全上下文可用——内网 http 访问(vite 绑 0.0.0.0 的既有形态)
  // 下是 undefined,必须走 getRandomValues 降级(首审 C1;实现复用 newSessionId)
  return newSessionId()
}

function outboxClear(sid: string, expected: string | OutboxEntry) {
  const snapshot = persistedOutbox()
  const all = mergedOutbox(snapshot)
  const current = all[sid]
  const identity = typeof expected === "string" ? expected : outboxIdentity(expected)
  if (!current || outboxIdentity(current) !== identity) return
  delete all[sid]
  if (snapshot.readable) {
    try {
      localStorage.setItem(OUTBOX_KEY, JSON.stringify(all))
      volatileOutbox.clear()
      return
    } catch { /* quota: mask only this receipt and its captured stale disk base */ }
  }
  rememberOutbox(sid, null, current.receipt, outboxIdentity(snapshot.all[sid]))
}

export interface LastMetrics {
  promptTokens?: number
  generationTokens?: number
  generationTps?: number
}

export type TaskControlAction = "cancel" | "suspend" | "resume"
export interface TaskTurnView { turnId: string | null; phase: TurnStateKind; seq: number }
export interface TaskControlView {
  requestId: string
  sessionId: string
  turnId: string | null
  action: TaskControlAction
  status: "sending" | "accepted" | "rejected" | "failed"
  message: string
}
interface TaskControlRequest extends TaskControlView { selectionEpoch: number; selectedId: string; afterSeq: number }

export class PendingDeletionError extends Error {}

export type DeletionReceipt = { id: string; status: "deleted" | "failed" | "changed"; message: string }

interface ChatState {
  conversations: Conversation[]
  retentionPolicy: SessionRetentionPolicy | null
  activeId: string
  thinkingEnabled: boolean
  /** sessionId -> 有轮次在跑（queued/running，事件流驱动） */
  generating: Record<string, boolean>
  /** sessionId -> 等待用户授权的敏感工具调用（删库等不可逆操作） */
  pendingApprovals: Record<string, PendingTool[]>
  /** 卡片展示时的轮次与审批事件身份；决议期间保留，生命周期事件/切走即失效。 */
  pendingApprovalTargets: Record<string, { turnId: string; seq: number }>
  /** sessionId -> 是否有一个已挂起、可恢复的任务（suspended；等待 resume/放弃） */
  suspendedSessions: Record<string, boolean>
  /** sessionId -> 多步任务进度快照（plan_updated 事件驱动；任务跨轮存续时保留） */
  taskProgress: Record<string, TaskSnapshot | null>
  /** sessionId -> 实时步骤流（事件归约的 preview.steps 投影，仅生成期间有值） */
  liveActivity: Record<string, LiveActivity | null>
  /** 顶部横幅提示（分支失败等业务性错误） */
  notice: string | null
  lastMetrics: LastMetrics | null
  /** Bounded journal projections, not a second lifecycle owner. */
  turnStates: Record<string, TaskTurnView>
  controlRequests: Record<string, TaskControlRequest>
  /** Mirror of the existing selection epoch, for other scoped UI requests. */
  selectionVersion: number
  deletingSessions: Record<string, boolean>
  /** Shared short-lived HTTP boundary, owned by this store for all session writers. */
  withSessionWrite: <T>(id: string, request: () => Promise<T>) => Promise<T>
  deletionResults: Record<string, DeletionReceipt>
  observeTaskEvent: (id: string, event: Pick<EventEnvelope, "type" | "seq" | "turn_id">) => void
  invalidateTaskControls: () => void

  selectConversation: (id: string) => void
  newConversation: () => void
  /** 打开定时任务的专属会话（schedule_{id}）：本地无该会话条目则以任务名补建后跳转。 */
  openScheduleSession: (sessionId: string, title: string) => void
  renameConversation: (id: string, title: string) => void
  deleteConversation: (id: string) => Promise<DeletionReceipt>
  /** 批量删除会话（多选清理）：逐个同步后端删除并从本地移除。 */
  deleteConversations: (ids: string[]) => Promise<DeletionReceipt[]>
  /** 置/撤会话 pin（乐观更新 + 后端落盘，失败回滚）：pin 的会话不被后台自动清扫。 */
  togglePin: (id: string, pinned: boolean) => void
  /** 反向映射对账：以后端会话索引为真相，剔除已被回收的幽灵条目、补入孤儿会话、同步 pin。 */
  reconcileSessions: () => Promise<void>
  setThinking: (value: boolean) => void
  clearNotice: () => void

  /** 加载服务端历史 + 附着事件流（App 在 activeId 变化时调用；账本协议的统一恢复入口）。 */
  loadHistory: (id: string) => Promise<void>
  sendMessage: (text: string, files: File[]) => Promise<void>
  /** 对挂起的敏感操作作出决议：approve=true 执行，false 拒绝（202 受理，结果走事件流）。 */
  respondApproval: (id: string, approve: boolean, expectedTarget: { turnId: string; seq: number }) => Promise<void>
  /** 删除最后一轮问答：后端同步回退记忆/KV/落盘，成功后从服务端重载消息。 */
  deleteLastTurn: (id: string) => Promise<boolean>
  /** 更新最后一次发言：删除最后一轮后按新内容重新发送生成。 */
  updateLastTurn: (text: string) => Promise<void>
  /** Stop future execution; model memory rolls back, completed external operations do not. */
  stopGenerating: (id: string, source?: "chat" | "global") => Promise<void>
  /** 挂起当前运行中的任务：后端在安全点让出；suspended 事件从流上回来置入可恢复态。 */
  suspendTask: (id: string) => Promise<void>
  /** 恢复一个已挂起的任务（202 受理，续跑过程走事件流）。 */
  resumeTask: (id: string) => Promise<void>
  /** 放弃一个已挂起的任务：清除本地可恢复态（后端挂起轮会在下次发消息时被丢弃回收）。 */
  abandonSuspended: (id: string) => void
  /** 从某个历史节点派生新会话（API.md §3.4），成功后切换过去。 */
  branchFrom: (id: string, nodeIndex: number) => Promise<void>
}

export const useChatStore = create<ChatState>((set, get) => {
  let sessionIndexRequest = 0
  // Selection incarnation, not just sid: A -> B -> A invalidates old A callbacks too.
  let selectionEpoch = 0
  const historyLoads = new Map<string, { epoch: number; promise: Promise<void> }>()
  const requiredHistoryIndices = new Map<string, number>()
  const deletionRequests = new Map<string, { invalidated: boolean; promise: Promise<DeletionReceipt>; resolve: (receipt: DeletionReceipt) => void; receipt?: string }>()
  const inFlightWrites = new Map<string, number>()
  let managementNotice: { id: string; text: string } | null = null
  const isCurrent = (sid: string, epoch: number) => get().activeId === sid && selectionEpoch === epoch
  function managementFeedback(id: string, text: string) {
    if (get().activeId !== id) return
    managementNotice = { id, text }; set({ notice: text })
  }
  function guardDeleting(id: string) {
    if (!get().deletingSessions[id]) return false
    managementFeedback(id, currentDict().management.deletePending)
    return true
  }
  async function withSessionWrite<T>(id: string, request: () => Promise<T>): Promise<T> {
    // Final check immediately before HTTP. This tracks only this tab's current
    // Promise, not a durable outbox or a model task's execution lifetime.
    if (guardDeleting(id)) throw new PendingDeletionError(currentDict().management.deletePending)
    inFlightWrites.set(id, (inFlightWrites.get(id) ?? 0) + 1)
    try { return await request() }
    finally {
      const count = (inFlightWrites.get(id) ?? 1) - 1
      if (count) inFlightWrites.set(id, count)
      else inFlightWrites.delete(id)
    }
  }
  function selecting(id: string) {
    // Re-entering an in-flight target creates a new UI incarnation. Its old
    // successful DELETE receipt must not detach or erase this current view.
    const request = deletionRequests.get(id)
    if (request && get().activeId !== id) request.invalidated = true
    if (managementNotice && managementNotice.id !== id) {
      if (get().notice === managementNotice.text) set({ notice: null })
      managementNotice = null
    }
  }
  function patchConversation(id: string, patch: Partial<Conversation>) {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id ? { ...c, ...patch } : c,
      ),
    }))
  }

  function appendMessages(id: string, messages: ChatMessage[]) {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id
          ? { ...c, messages: [...c.messages, ...messages], updatedAt: Date.now() }
          : c,
      ),
    }))
  }

  /** 按 id 局部更新一条消息（预览气泡随 token/thinking 增量填充）。 */
  function updateMessage(id: string, msgId: string, patch: Partial<ChatMessage>) {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id
          ? { ...c, messages: c.messages.map((m) => (m.id === msgId ? { ...m, ...patch } : m)) }
          : c,
      ),
    }))
  }

  function removeMessages(id: string, msgIds: Array<string | undefined>) {
    const drop = new Set(msgIds.filter(Boolean) as string[])
    if (drop.size === 0) return
    // 被移除消息（乐观气泡）里的图片预览 blob URL 随之撤销，Blob 才能被回收
    const conv = get().conversations.find((c) => c.id === id)
    if (conv) revokePreviewUrls(conv.messages.filter((m) => drop.has(m.id)))
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id ? { ...c, messages: c.messages.filter((m) => !drop.has(m.id)) } : c,
      ),
    }))
  }

  function setGenerating(id: string, value: boolean) {
    set((s) => ({ generating: { ...s.generating, [id]: value } }))
  }

  function boundedTaskPatch<T>(record: Record<string, T>, id: string, value: T): Record<string, T> {
    const active = get().activeId
    const retained = active !== id && record[active] !== undefined ? [[active, record[active]] as [string, T]] : []
    return Object.fromEntries([...Object.entries(record).filter(([key]) => key !== id && key !== active),
      [id, value] as [string, T], ...retained].slice(-64))
  }

  function dropControl(id: string) {
    if (!get().controlRequests[id]) return
    set((s) => { const requests = { ...s.controlRequests }; delete requests[id]; return { controlRequests: requests } })
  }

  function observeTaskEvent(id: string, env: Pick<EventEnvelope, "type" | "seq" | "turn_id">) {
    const phases: Partial<Record<string, TurnStateKind>> = {
      turn_queued: "queued", turn_started: "running", resumed: "queued",
      awaiting_approval: "awaiting_approval", suspended: "suspended", turn_ended: "none",
    }
    const phase = phases[env.type]
    if (!phase) return
    if (env.type === "turn_ended") useUiStore.getState().observeFilesTerminal(id, env.seq)
    const previous = get().turnStates[id]
    const newerProjection = env.seq > (previous?.seq ?? -1)
    const turnId = phase === "none" ? null : env.turn_id
    if (newerProjection) {
      set((s) => ({ turnStates: boundedTaskPatch(s.turnStates, id, { turnId, phase, seq: env.seq }) }))
    }
    const request = get().controlRequests[id]
    // Projection may already include this event via /turn. Confirmation instead belongs
    // to the request's captured cursor floor, and only to its matching turn/action.
    if (!request || env.seq <= request.afterSeq) return
    if ((request.turnId !== null && env.turn_id !== null && request.turnId !== env.turn_id)
        || (request.turnId === null && env.type === "turn_queued")) { dropControl(id); return }
    const confirms = request.turnId !== null && request.turnId === env.turn_id && (
      env.type === "turn_ended"
      || (request.action === "suspend" && env.type === "suspended")
      || (request.action === "resume" && ["resumed", "turn_started"].includes(env.type)))
    if (confirms || (newerProjection && ["rejected", "failed"].includes(request.status))) dropControl(id)
  }

  function observeTurnSnapshot(id: string, snapshot: TurnStateResponse) {
    const seq = snapshot.last_seq ?? snapshot.turn?.last_seq ?? 0
    if (seq < (get().turnStates[id]?.seq ?? -1)) return
    const turn = snapshot.turn
    set((s) => ({ turnStates: boundedTaskPatch(s.turnStates, id,
      { turnId: turn?.turn_id ?? null, phase: turn?.state ?? "none", seq }) }))
  }

  function ownsControl(request: TaskControlRequest, requireTurn = true) {
    return get().controlRequests[request.sessionId]?.requestId === request.requestId
      && request.selectionEpoch === selectionEpoch && request.selectedId === get().activeId
      && (!requireTurn || request.turnId === get().turnStates[request.sessionId]?.turnId)
  }

  function controlFeedback(request: TaskControlRequest, status: TaskControlView["status"], message: string) {
    if (!ownsControl(request, false)) return
    set((s) => ({ controlRequests: boundedTaskPatch(s.controlRequests, request.sessionId, { ...request, status, message }) }))
  }

  async function requestControl(id: string, action: TaskControlAction, source: "chat" | "global" = "chat") {
    if (guardDeleting(id)) return
    const current = get().controlRequests[id]
    if (current && ["sending", "accepted"].includes(current.status)
        && !(action === "cancel" && current.action === "suspend")) return
    const request: TaskControlRequest = { requestId: newSessionId(), sessionId: id,
      turnId: get().turnStates[id]?.turnId ?? null, action, status: "sending",
      message: currentDict().controls.sending, selectionEpoch, selectedId: get().activeId,
      afterSeq: Math.max(get().turnStates[id]?.seq ?? 0, cursors.get(id) ?? 0) }
    set((s) => ({ controlRequests: boundedTaskPatch(s.controlRequests, id, request) }))
    const rejected = (message: string) => controlFeedback(request, "rejected", message)
    try {
      if (source === "global") {
        if (useUiStore.getState().tasksState !== "fresh") { rejected(currentDict().controls.snapshotUnavailable); return }
        // /tasks has no turn identity; bind one current /turn read, never another attachment.
        const snapshot = await getTurnState(id)
        if (!ownsControl(request, false)) return
        if (snapshot.status === false) { rejected(currentDict().controls.snapshotUnavailable); return }
        // A scoped authoritative read may reveal a new turn. Converge the bounded view
        // without posting this old click to that new turn; a later explicit click can bind it.
        observeTurnSnapshot(id, snapshot)
        if (request.turnId && request.turnId !== snapshot.turn?.turn_id) {
          rejected(currentDict().controls.targetChanged)
          return
        }
        request.turnId = snapshot.turn?.turn_id ?? null
        request.afterSeq = Math.max(request.afterSeq, snapshot.last_seq ?? snapshot.turn?.last_seq ?? 0, get().turnStates[id]?.seq ?? 0, cursors.get(id) ?? 0)
        set((s) => ({ controlRequests: boundedTaskPatch(s.controlRequests, id, { ...request }) }))
      }
      if (!ownsControl(request)) { dropControl(id); return }
      const phase = get().turnStates[id]?.phase
      if (!request.turnId) { rejected(currentDict().controls.noTurn); return }
      if (action === "suspend" && phase !== "running") { rejected(currentDict().controls.cannotSuspend); return }
      if (action === "cancel" && phase !== "running" && phase !== "queued") { rejected(currentDict().controls.cannotStop); return }
      if (action === "resume" && phase !== "suspended") { rejected(currentDict().controls.cannotResume); return }
      const response = await withSessionWrite<CancelResponse | TurnAccepted>(id, () => action === "cancel" ? cancelTask(id)
        : action === "suspend" ? apiSuspendTask(id) : submitResume(id))
      if (!ownsControl(request)) return
      if (response.status === false) {
        rejected("response" in response ? response.response || currentDict().controls.rejected : currentDict().controls.rejected)
        return
      }
      const labels = currentDict().controls
      controlFeedback(request, "accepted", action === "cancel" ? labels.cancelAccepted
        : action === "suspend" ? labels.suspendAccepted : labels.resumeAccepted)
      if (action === "resume" && get().activeId === id) ensureAttached(id)
    } catch (error) {
      if (!ownsControl(request, false)) return
      if (request.turnId && request.turnId !== get().turnStates[id]?.turnId) return
      const detail = error instanceof Error ? error.message : "HTTP request failed"
      controlFeedback(request, error instanceof TurnConflictError || error instanceof PendingDeletionError ? "rejected" : "failed",
        error instanceof TurnConflictError && action === "resume" ? currentDict().controls.cannotResume : currentDict().controls.failed(detail))
    }
  }

  // ---------- 事件附着与归约（账本协议核心） ----------

  function ensureAttached(sid: string) {
    if (get().activeId !== sid || attachCtls.has(sid)) return
    if (cursors.get(sid) === undefined) {
      void get().loadHistory(sid)
      return
    }
    const ctl = new AbortController()
    attachCtls.set(sid, ctl)
    void attachLoop(sid, ctl, selectionEpoch)
  }

  /** Same history -> turn -> events entrance, retaining other submission receipts/previews. */
  function restoreHistoryProjection(sid: string) {
    const epoch = selectionEpoch
    const existing = historyLoads.get(sid)
    const start = () => {
      if (!isCurrent(sid, epoch)) return
      const ctl = attachCtls.get(sid)
      ctl?.abort()
      if (attachCtls.get(sid) === ctl) attachCtls.delete(sid)
      patchConversation(sid, { historyLoaded: false })
      void get().loadHistory(sid)
    }
    // An already-started read can predate the terminal; finish it, then make this
    // terminal's one recovery read. This is event driven, not a compensation loop.
    if (existing?.epoch === epoch) void existing.promise.then(start)
    else start()
  }

  /** 断开附着并清空该会话的活跃轮簿记。reload=true 时置 historyLoaded=false,
   *  下次激活经 /history + /turn + 重放完整恢复(切走-切回的对账路径)。 */
  function detach(sid: string, reload = true) {
    if (get().activeId === sid) {
      selectionEpoch++
      set({ controlRequests: {}, selectionVersion: selectionEpoch }) // commands captured the departing selection incarnation
    } else dropControl(sid) // removing an unrelated session must not erase the selected command
    historyLoads.delete(sid)
    requiredHistoryIndices.delete(sid)
    set((s) => {
      const targets = { ...s.pendingApprovalTargets }
      delete targets[sid]
      return { pendingApprovalTargets: targets }
    })
    attachCtls.get(sid)?.abort()
    attachCtls.delete(sid)
    cursors.delete(sid)
    liveTurns.delete(sid)
    liveMsg.delete(sid)
    optimistic.delete(sid)
    lastEndedIds.delete(sid)
    cancelPreviewFlush(sid)
    if (reload) {
      // 内存有界：切走的会话消息就地释放,运行中也一样——活轮瞬态气泡的簿记(liveMsg/
      // 游标)已随附着断开,留着的气泡切回重放时收编不了,每进出一次多一份幽灵副本
      // (实测重复渲染根因)。切回经 /history + 整轮重放完整重建,与原生 select 同契约。
      const conv = get().conversations.find((c) => c.id === sid)
      if (conv && conv.messages.length > 0) {
        revokePreviewUrls(conv.messages)
        patchConversation(sid, { historyLoaded: false, messages: [], canonicalTurns: undefined,
          retryGroups: undefined, displayTurnIndices: undefined, lastCanonicalTurn: undefined })
      } else {
        patchConversation(sid, { historyLoaded: false, canonicalTurns: undefined,
          retryGroups: undefined, displayTurnIndices: undefined, lastCanonicalTurn: undefined })
      }
    }
  }

  async function attachLoop(sid: string, ctl: AbortController, epoch: number) {
    let backoff = 1000
    let anchored = false
    const ownsAttachment = () => !ctl.signal.aborted && isCurrent(sid, epoch) && attachCtls.get(sid) === ctl
    while (ownsAttachment()) {
      try {
        let after = cursors.get(sid) ?? 0
        if (!anchored) {
          const ts = await getTurnState(sid)
          if (!ownsAttachment()) return
          observeTurnSnapshot(sid, ts)
          // History's cursor is a lower bound. /turn can finish later; never skip that window.
          after = Math.min(after, ts.turn ? ts.turn.first_seq - 1 : ts.last_seq)
          cursors.set(sid, after)
          anchored = true
          if (!ts.turn && !optimistic.has(sid)) setGenerating(sid, false)
        }
        for await (const env of attachEvents(sid, after, ctl.signal)) {
          if (!ownsAttachment()) return
          backoff = 1000
          if (env.seq > 0) {
            const cur = cursors.get(sid) ?? 0
            if (env.seq <= cur) continue
            cursors.set(sid, env.seq)
          }
          dispatch(sid, env)
        }
        break
      } catch (e) {
        if (!ownsAttachment()) break
        if (e instanceof HttpError && e.status === 404) break
        await new Promise((r) => setTimeout(r, backoff))
        backoff = Math.min(backoff * 2, 10000)
      }
    }
    // Old same-sid task completion cannot delete a newer attachment.
    if (attachCtls.get(sid) === ctl) attachCtls.delete(sid)
  }

  /** 事件分发：归约器推进活跃轮状态,再把结果投影进 UI store。 */
  function dispatch(sid: string, env: EventEnvelope) {
    if (env.type === "stream_attached") return
    observeTaskEvent(sid, env)
    let next = reduceTurnEvent(liveTurns.get(sid) ?? initialTurnState(), env)
    // transcript 是夹具口径的对账字段,UI 从不读取——长期附着下随轮次无界累积
    // (每轮的完整 response 含工具结果),store 层不留存。归约器保持纯函数,夹具不受影响。
    if (next.transcript.length > 0) next = { ...next, transcript: [] }
    liveTurns.set(sid, next)

    // 这些账本事件使之前展示/提交中的审批失效，迟到 HTTP 回包不再拥有 UI 写权。
    if (["turn_queued", "turn_started", "resumed", "suspended", "turn_ended"].includes(env.type)) {
      set((s) => {
        const targets = { ...s.pendingApprovalTargets }
        delete targets[sid]
        return { pendingApprovalTargets: targets }
      })
    }

    switch (env.type) {
      case "turn_queued":
        cancelPreviewFlush(sid) // 上一轮迟到的投影计时器不得漏进新轮
        onTurnQueued(sid, env, String(env.data.prompt ?? ""))
        break
      case "turn_started":
      case "resumed":
        setGenerating(sid, true)
        set((s) => {
          const approvals = { ...s.pendingApprovals }
          delete approvals[sid]
          const susp = { ...s.suspendedSessions }
          delete susp[sid]
          return { pendingApprovals: approvals, suspendedSessions: susp }
        })
        break
      case "token_delta":
      case "thinking_delta":
        schedulePreviewFlush(sid)
        break
      case "tool_call_pending":
      case "tool_started":
      case "tool_progress":
      case "tool_result":
      case "step_completed":
      case "step_failed":
        syncActivity(sid, next)
        break
      case "queue_moved":
        // 排队位次可见(Phase 3):单 worker 忙碌时,黑盒排队变成明确位次
        set((s) => ({
          liveActivity: {
            ...s.liveActivity,
            [sid]: {
              phase: "running",
              current: currentDict().activity.queuedAt(Number(env.data.position ?? 0)),
              steps: [],
            },
          },
        }))
        break
      case "plan_updated":
        set((s) => ({ taskProgress: { ...s.taskProgress, [sid]: next.preview.task } }))
        syncActivity(sid, next)
        break
      case "awaiting_approval":
        flushPreviewNow(sid) // 暂停前把最后 ~90ms 的增量落进气泡（半截产出是决策上下文）
        setGenerating(sid, false)
        set((s) => {
          const targets = { ...s.pendingApprovalTargets }
          if (env.turn_id) targets[sid] = { turnId: env.turn_id, seq: env.seq }
          else delete targets[sid] // 身份不完整时不可提交无绑定决议
          return { pendingApprovals: { ...s.pendingApprovals, [sid]: next.pending_tools }, pendingApprovalTargets: targets }
        })
        pruneEmptyPreview(sid)
        break
      case "suspended":
        flushPreviewNow(sid)
        setGenerating(sid, false)
        set((s) => ({ suspendedSessions: { ...s.suspendedSessions, [sid]: true } }))
        pruneEmptyPreview(sid)
        break
      case "turn_ended":
        cancelPreviewFlush(sid) // 权威对账在即，未刷的增量随预览一并撤下
        onTurnEnded(sid, env)
        break
    }
  }

  /** 轮次受理：确保用户气泡存在（收编本 tab 的乐观气泡；其他来源——别的客户端/
   *  定时任务——则按事件补建），重置预览簿记。幂等：重连重放同一轮的 turn_queued
   *  只重置内容,不重复建气泡。 */
  function onTurnQueued(sid: string, env: EventEnvelope, prompt: string) {
    setGenerating(sid, true)
    const existing = liveMsg.get(sid)
    if (existing?.turnId === env.turn_id) return // 重放幂等

    let userId: string | undefined
    const opt = optimistic.get(sid)
    if (opt && (opt.turnId === env.turn_id || opt.clientTurnId === env.data.client_turn_id)) {
      userId = opt.userId
      opt.turnId = env.turn_id ?? undefined // bind by id; equal text is not evidence of ownership
      opt.seq = env.seq
    } else {
      userId = nextMessageId()
      appendMessages(sid, [{ id: userId, role: "user", content: prompt }])
    }
    const pending = outboxAll()[sid]
    const receipt = pending?.key && pending.key === env.data.client_turn_id ? pending.receipt : undefined
    liveMsg.set(sid, { turnId: env.turn_id ?? undefined, userId, receipt })

    const conv = get().conversations.find((c) => c.id === sid)
    if (conv && conv.title === NEW_CHAT_TITLE && prompt) {
      patchConversation(sid, { title: prompt.slice(0, 30) })
    }
  }

  /** 流式投影节流：每个 token/thinking 增量都直投会导致「重建 conversations 数组
   *  → 全列表重渲染 → 流式气泡 markdown 重解析」以每秒几十次的频率发生（实测
   *  Safari 内存重载的主因），攒 ~90ms 合并刷一次。 */
  function schedulePreviewFlush(sid: string) {
    if (previewFlush.has(sid)) return
    previewFlush.set(sid, setTimeout(() => {
      previewFlush.delete(sid)
      flushPreviewNow(sid)
    }, 90))
  }

  function cancelPreviewFlush(sid: string) {
    const timer = previewFlush.get(sid)
    if (timer !== undefined) {
      clearTimeout(timer)
      previewFlush.delete(sid)
    }
  }

  function flushPreviewNow(sid: string) {
    cancelPreviewFlush(sid)
    const live = liveTurns.get(sid)
    // 空内容不投影：迟到的计时器不会在 turn_ended/turn_queued 重置预览后造出空壳气泡
    if (live && (live.preview.text || live.preview.thinking)) syncPreviewBubble(sid, live)
  }

  /** 预览气泡（流式正文 + 思考）：懒创建——首个增量到达才挂气泡,避免审批/工具阶段的空壳。 */
  function syncPreviewBubble(sid: string, live: NormalizedState) {
    const ids = liveMsg.get(sid) ?? {}
    if (!ids.assistantId) {
      ids.assistantId = nextMessageId()
      liveMsg.set(sid, ids)
      appendMessages(sid, [{ id: ids.assistantId, role: "assistant", content: "", streaming: true }])
    }
    updateMessage(sid, ids.assistantId, {
      content: live.preview.text,
      thoughts: live.preview.thinking || undefined,
    })
  }

  /** 暂停态（审批/挂起）时收掉空预览气泡：有内容则保留（半截产出仍可见）。 */
  function pruneEmptyPreview(sid: string) {
    const ids = liveMsg.get(sid)
    const live = liveTurns.get(sid)
    if (ids?.assistantId && live && !live.preview.text && !live.preview.thinking) {
      removeMessages(sid, [ids.assistantId])
      liveMsg.set(sid, { ...ids, assistantId: undefined })
    }
    syncActivity(sid, live ?? initialTurnState())
  }

  /** 实时步骤流投影：归约的 preview.steps → ActivityCard 的 LiveActivity 形状。
   *  当前行优先级:执行中的工具 > 生成中预告的工具(tool_call_pending,执行前可见)。 */
  function syncActivity(sid: string, live: NormalizedState) {
    const runningTool = [...live.preview.steps].reverse().find((s) => s.status === "running")
    const pendingTool = [...live.preview.steps].reverse().find((s) => s.status === "pending")
    const current = runningTool
      ? runningTool.label + (runningTool.detail ? `（${runningTool.detail}）` : "")
      : pendingTool
        ? currentDict().activity.pendingTool(pendingTool.label)
        : ""
    const activity: LiveActivity = {
      phase: live.turn_state === "running" || live.turn_state === "queued" ? "running" : live.turn_state,
      current,
      steps: live.preview.steps
        .filter((s) => s.status !== "running" && s.status !== "pending")
        .map((s) => ({ kind: s.kind, label: s.label, detail: s.detail })),
    }
    set((s) => ({ liveActivity: { ...s.liveActivity, [sid]: activity } }))
  }

  /** 权威终态：撤下预览簿记,按 turn_ended 的完整轮次记录对账（含 user 消息）。 */
  function onTurnEnded(sid: string, env: EventEnvelope) {
    const data = env.data as unknown as ChatResponse & { turn_index?: number; discarded?: boolean }
    const ids = liveMsg.get(sid)
    const opt = optimistic.get(sid)
    const ownsSubmit = !!env.turn_id && opt?.turnId === env.turn_id
    if (env.turn_id) lastEndedIds.set(sid, env.turn_id)
    if (ids?.turnId === env.turn_id) {
      removeMessages(sid, [ids?.userId, ids?.assistantId])
      liveMsg.delete(sid)
      if (ids.receipt && outboxAll()[sid]?.receipt === ids.receipt) outboxClear(sid, ids.receipt)
    }
    if (ownsSubmit && opt) {
      removeMessages(sid, [opt.userId])
      optimistic.delete(sid)
      if (outboxAll()[sid]?.receipt === opt.receipt) outboxClear(sid, opt.receipt)
    }

    let needsProjection = false
    if (data.discarded !== true) {
      const conv = get().conversations.find((c) => c.id === sid)
      const turnIndex = Number.isInteger(data.turn_index) && data.turn_index! >= 0
        ? data.turn_index
        : (conv?.turnCount ?? 0)
      const index = turnIndex!
      const known = conv?.canonicalTurns?.[index] !== undefined
        || conv?.messages.some((m) => m.turnIndex === index)
      const full = Array.isArray(data.response) && typeof data.prompt === "string"
      // Hidden attempts remain canonical coverage. A new retry needs the server's
      // display projection; do not guess its winner from local root relationships.
      if (!known && (!full || data.retry_of !== undefined
          || (conv?.canonicalTurns !== undefined && index > conv.canonicalTurns.length))) {
        // Never allocate a sparse history from an arbitrary peer-supplied index.
        if (full && index <= (conv?.canonicalTurns?.length ?? 0)) rememberCanonicalTurn(sid, data, index)
        if (Number.isInteger(data.turn_index) && data.turn_index! >= 0) {
          requiredHistoryIndices.set(sid, Math.max(requiredHistoryIndices.get(sid) ?? -1, index))
        }
        needsProjection = true
      } else if (!known) {
        applyEndedTurn(sid, data, index)
      } else {
        patchConversation(sid, { turnCount: Math.max(conv?.turnCount ?? 0, index + 1) })
      }
    }

    setGenerating(sid, optimistic.has(sid))
    set((s) => {
      const approvals = { ...s.pendingApprovals }
      delete approvals[sid]
      const susp = { ...s.suspendedSessions }
      delete susp[sid]
      return {
        pendingApprovals: approvals,
        suspendedSessions: susp,
        liveActivity: { ...s.liveActivity, [sid]: null },
      }
    })
    // 任务可能跨轮存续（如模型中途向用户提问）：轮末一次性校准任务快照
    const epoch = selectionEpoch
    getTaskStatus(sid)
      .then((res) => {
        if (!isCurrent(sid, epoch)) return
        set((s) => ({
          taskProgress: { ...s.taskProgress, [sid]: res.active ? (res.task ?? null) : null },
        }))
      })
      .catch(() => {})
    if (needsProjection) restoreHistoryProjection(sid)
  }

  function rememberCanonicalTurn(id: string, turn: ChatResponse, index: number) {
    const conv = get().conversations.find((c) => c.id === id)
    const canonicalTurns = [...(conv?.canonicalTurns ?? [])]
    canonicalTurns[index] = turn
    const last = conv?.lastCanonicalTurn
    patchConversation(id, { canonicalTurns,
      lastCanonicalTurn: !last || index >= last.index ? { index, prompt: turn.prompt, turnRef: turn.turn_ref } : last,
      turnCount: Math.max(conv?.turnCount ?? 0, index + 1) })
  }

  /** 把一轮终态渲染进消息流（沿用旧 turn_end 对账口径,终态本地化提示不变）。 */
  function applyEndedTurn(convId: string, res: ChatResponse, turnIndex: number) {
    rememberCanonicalTurn(convId, res, turnIndex)
    appendMessages(convId, turnToMessages(res, turnIndex, currentDict().store))
    patchConversation(convId, { turnCount: Math.max(get().conversations.find((c) => c.id === convId)?.turnCount ?? 0, turnIndex + 1) })
    set({
      lastMetrics: {
        promptTokens: res.prompt_tokens,
        generationTokens: res.generation_tokens,
        generationTps: res.generation_tps,
      },
    })
  }

  async function deleteAndRestoreLastTurn(id: string): Promise<number | null> {
      if (guardDeleting(id)) return null
      const epoch = selectionEpoch
      if (get().generating[id]) return null
      try {
        const res = await withSessionWrite(id, () => apiDeleteLastTurn(id))
        if (!isCurrent(id, epoch)) return null
        if (!res.status) {
          set({ notice: res.response || currentDict().store.deleteTurnFailed })
          return null
        }
        // 后端已同步回退（含撤销未完结的审批轮）：清除本地审批挂起态，
        // 重置消息并从服务端重新拉取，保证与后端记忆/落盘一致
        detach(id, false)
        const restoredEpoch = selectionEpoch
        set((s) => {
          const pendingNext = { ...s.pendingApprovals }
          delete pendingNext[id]
          const suspNext = { ...s.suspendedSessions }
          delete suspNext[id] // 后端删轮时同步回退了挂起轮
          const taskNext = { ...s.taskProgress }
          delete taskNext[id] // 后端删轮时同步清除了任务状态
          return {
            pendingApprovals: pendingNext,
            suspendedSessions: suspNext,
            taskProgress: taskNext,
            conversations: s.conversations.map((c) =>
              c.id === id
                ? {
                    ...c,
                    messages: [],
                    canonicalTurns: undefined,
                    displayTurnIndices: undefined,
                    retryGroups: undefined,
                    lastCanonicalTurn: undefined,
                    historyLoaded: false,
                    turnCount: res.remaining_turns ?? Math.max(0, c.turnCount - 1),
                    updatedAt: Date.now(),
                  }
                : c,
            ),
          }
        })
        await get().loadHistory(id)
        if (!isCurrent(id, restoredEpoch)) return null
        return restoredEpoch
      } catch (e) {
        if (!isCurrent(id, epoch)) return null
        console.error("删除最后一轮失败:", e)
        set({ notice: currentDict().store.deleteTurnFailedConn })
        return null
      }
    }

  async function deleteConfirmedSessions(ids: string[]): Promise<DeletionReceipt[]> {
    const targets = [...new Set(ids)].map((id) => {
      if (inFlightWrites.has(id)) {
        const result: DeletionReceipt = { id, status: "failed", message: currentDict().management.waitForRequest }
        managementFeedback(id, result.message)
        set((s) => ({ deletionResults: boundedTaskPatch(s.deletionResults, id, result) }))
        return { id, request: { invalidated: false, promise: Promise.resolve(result), resolve: () => {}, receipt: undefined }, owned: false }
      }
      const existing = deletionRequests.get(id)
      if (existing) return { id, request: existing, owned: false }
      let resolve!: (receipt: DeletionReceipt) => void
      const promise = new Promise<DeletionReceipt>((r) => { resolve = r })
      const request = { invalidated: false, promise, resolve, receipt: outboxAll()[id]?.receipt }
      deletionRequests.set(id, request)
      set((s) => { const results = { ...s.deletionResults }; delete results[id]; return {
        deletingSessions: { ...s.deletingSessions, [id]: true }, deletionResults: results } })
      return { id, request, owned: true }
    })
    const results: DeletionReceipt[] = []
    for (const { id, request, owned } of targets) {
      if (!owned) { results.push(await request.promise); continue }
      let result: DeletionReceipt
      try {
        if (request.invalidated) {
          result = { id, status: "changed", message: currentDict().management.deleteChanged }
        } else {
          const response = await deleteSession(id)
          if (!response.status && response.reason !== "not_found") throw new Error(response.response || currentDict().management.rejected)
          if (request.invalidated) {
            result = { id, status: "changed", message: currentDict().management.deleteAcknowledgedChanged }
          } else {
            // Only the confirmed target is retired. Background A cannot detach B;
            // outbox cleanup is limited to the captured receipt, including other tabs.
            const conversation = get().conversations.find((c) => c.id === id)
            if (conversation) revokePreviewUrls(conversation.messages)
            detach(id, false)
            if (request.receipt) outboxClear(id, request.receipt)
            sessionIndexRequest++ // older /sessions cannot resurrect this confirmed deletion
            set((s) => {
              let next = s.conversations.filter((c) => c.id !== id)
              if (!next.length) next = [freshConversation()]
              const drop = <T,>(record: Record<string, T>) => { const value = { ...record }; delete value[id]; return value }
              return { conversations: next, activeId: s.activeId === id ? next[0].id : s.activeId,
                generating: drop(s.generating), pendingApprovals: drop(s.pendingApprovals), pendingApprovalTargets: drop(s.pendingApprovalTargets),
                suspendedSessions: drop(s.suspendedSessions), taskProgress: drop(s.taskProgress), liveActivity: drop(s.liveActivity),
                turnStates: drop(s.turnStates), controlRequests: drop(s.controlRequests) }
            })
            result = { id, status: "deleted", message: response.reason === "not_found"
              ? currentDict().management.alreadyAbsent : response.response || currentDict().management.deleted }
          }
        }
      } catch (error) {
        result = { id, status: "failed", message: currentDict().management.failed(error instanceof Error ? error.message : "HTTP failed") }
        if (!request.invalidated) managementFeedback(id, result.message)
      } finally {
        if (deletionRequests.get(id) === request) {
          deletionRequests.delete(id)
          set((s) => { const deleting = { ...s.deletingSessions }; delete deleting[id]; return { deletingSessions: deleting } })
        }
      }
      set((s) => ({ deletionResults: boundedTaskPatch(s.deletionResults, id, result) }))
      request.resolve(result); results.push(result)
    }
    return results
  }

  return {
    ...loadInitialState(),
    retentionPolicy: null,
    generating: {},
    pendingApprovals: {},
    pendingApprovalTargets: {},
    suspendedSessions: {},
    taskProgress: {},
    liveActivity: {},
    notice: null,
    lastMetrics: null,
    turnStates: {},
    controlRequests: {},
    selectionVersion: 0,
    deletingSessions: {},
    withSessionWrite,
    deletionResults: {},
    observeTaskEvent,
    invalidateTaskControls: () => set({ turnStates: {}, controlRequests: {} }),

    selectConversation: (id) => {
      selecting(id)
      const prev = get().activeId
      if (prev && prev !== id) detach(prev) // 单附着：切走即断,回来经历史+重放对账
      set({ activeId: id })
    },

    newConversation: () => {
      const prev = get().activeId
      if (prev) detach(prev)
      const fresh = freshConversation()
      selecting(fresh.id)
      set((s) => ({
        conversations: [fresh, ...s.conversations],
        activeId: fresh.id,
      }))
    },

    openScheduleSession: (sessionId, title) => {
      const existing = get().conversations.find((c) => c.id === sessionId)
      if (existing) {
        get().selectConversation(sessionId)
        return
      }
      const prev = get().activeId
      if (prev) detach(prev)
      // 会话由后端定时运行创建，前端首次打开时补建条目；historyLoaded=false 触发拉取运行全文
      const conv: Conversation = {
        id: sessionId,
        title,
        updatedAt: Date.now(),
        messages: [],
        historyLoaded: false,
        turnCount: 0,
      }
      selecting(sessionId)
      set((s) => ({ conversations: [conv, ...s.conversations], activeId: sessionId }))
    },

    renameConversation: (id, title) => {
      if (guardDeleting(id)) return
      const trimmed = title.trim()
      if (!trimmed) return
      const prev = get().conversations.find((c) => c.id === id)?.title
      patchConversation(id, { title: trimmed })  // 乐观;标题服务端一等,双端同源
      withSessionWrite(id, () => setSessionMeta(id, { title: trimmed })).catch((e) => {
        console.warn("改名落盘失败，回滚:", e)
        if (prev !== undefined) patchConversation(id, { title: prev })
      })
    },

    deleteConversation: async (id) => (await deleteConfirmedSessions([id]))[0],
    deleteConversations: deleteConfirmedSessions,

    togglePin: (id, pinned) => {
      if (guardDeleting(id)) return
      // 乐观更新本地，失败回滚（不改 updatedAt：pin 不算内容变更，不该抬高排序时间）
      patchConversation(id, { pinned })
      withSessionWrite(id, () => setSessionPin(id, pinned)).catch((e) => {
        console.warn("pin 落盘失败，回滚:", e)
        patchConversation(id, { pinned: !pinned })
        set({ notice: currentDict().retention.pinFailed })
        void get().reconcileSessions()
      })
    },

    reconcileSessions: async () => {
      const request = ++sessionIndexRequest
      let index
      try {
        index = await listSessions()
      } catch {
        return // 后端不可达：静默沿用本地
      }
      if (request !== sessionIndexRequest) return
      // schedule_ 前缀会话由定时任务条独立管理，不并入普通会话列表
      const onDisk = new Map(
        index.sessions
          .filter((m) => !m.session_id.startsWith("schedule_"))
          .map((m) => [m.session_id, m]),
      )
      const removed = get().conversations.filter((c) =>
        !onDisk.has(c.id) && c.turnCount !== 0 && !c.id.startsWith("schedule_"))
      const removedIds = new Set(removed.map((c) => c.id))
      for (const conversation of removed) {
        revokePreviewUrls(conversation.messages)
        detach(conversation.id, false)
      }
      set((s) => {
        const localIds = new Set(s.conversations.map((c) => c.id))
        // 1) 剔除幽灵：本地已落盘（turnCount>0）但后端已无 → 已被回收/外部删除。
        const kept = s.conversations.filter(
          (c) => onDisk.has(c.id) || c.turnCount === 0 || c.id.startsWith("schedule_"),
        )
        // 2) 同步匹配项的 pin（后端为真相）与轮数；用户改过的标题不被服务端派生标题覆盖
        const synced = kept.map((c) => {
          const m = onDisk.get(c.id)
          if (!m) return c
          const title =
            c.title === NEW_CHAT_TITLE && m.title ? m.title : c.title
          return { ...c, pinned: m.pinned, turnCount: m.turn_count, title }
        })
        // 3) 补入孤儿：后端有、本地无（清了缓存/换设备）→ 以服务端派生标题登记，可见可删
        const orphans: Conversation[] = []
        for (const [sid, m] of onDisk) {
          if (localIds.has(sid)) continue
          orphans.push({
            id: sid,
            title: m.title || NEW_CHAT_TITLE,
            updatedAt: (m.last_updated ?? m.created_at ?? Date.now() / 1000) * 1000,
            messages: [],
            historyLoaded: false,
            turnCount: m.turn_count,
            pinned: m.pinned,
          })
        }
        let next = [...synced, ...orphans]
        if (next.length === 0) next = [freshConversation()]
        const dropRemoved = <T>(record: Record<string, T>): Record<string, T> =>
          Object.fromEntries(Object.entries(record).filter(([id]) => !removedIds.has(id)))
        return {
          conversations: next,
          retentionPolicy: index.retention_policy ?? null,
          activeId: next.some((c) => c.id === s.activeId) ? s.activeId : next[0].id,
          generating: dropRemoved(s.generating),
          pendingApprovals: dropRemoved(s.pendingApprovals),
          pendingApprovalTargets: dropRemoved(s.pendingApprovalTargets),
          suspendedSessions: dropRemoved(s.suspendedSessions),
          taskProgress: dropRemoved(s.taskProgress),
          liveActivity: dropRemoved(s.liveActivity),
        }
      })
    },

    setThinking: (value) => set({ thinkingEnabled: value }),

    clearNotice: () => set({ notice: null }),

    loadHistory: async (id) => {
      if (get().activeId !== id) return
      const conv = get().conversations.find((c) => c.id === id)
      if (!conv) return
      if (conv.historyLoaded && cursors.has(id)) { ensureAttached(id); return }
      const epoch = selectionEpoch
      const existing = historyLoads.get(id)
      if (existing?.epoch === epoch) return existing.promise
      const request = { epoch, promise: Promise.resolve() }
      historyLoads.set(id, request)
      request.promise = (async () => {
        try {
          const history = await getHistory(id)
          if (!isCurrent(id, epoch)) return
          if (history.status === false) throw new Error("history unavailable")
          // A read that predates an observed terminal cannot become a successful
          // restore. Keep the winner and an explicit retryable sync message; do
          // not reattach/replay this missing thin terminal in an automatic loop.
          if ((requiredHistoryIndices.get(id) ?? -1) >= (history.turns?.length ?? 0)) throw new Error("history has not covered the observed terminal")
          requiredHistoryIndices.delete(id)
          const projection = historyProjection(history, currentDict().store)
          const messages = projection.messages
          const count = history.count ?? history.turns?.length ?? 0
          const acceptedSeq = optimistic.get(id)?.seq
          cursors.set(id, Math.min(history.replay_after ?? 0, acceptedSeq ? acceptedSeq - 1 : Infinity))
          // An accepted submit may have completed before this first history read; include its
          // queued/terminal identity even when history already contains the authoritative record.
          set((s) => ({
            conversations: s.conversations.map((c) => {
              if (c.id !== id) return c
              const covered = projection.canonicalTurns.length
              // A later ordinary terminal can arrive while this snapshot is in flight.
              // Its canonical coverage/tail input must survive along with its messages.
              const canonicalTurns = [...projection.canonicalTurns, ...(c.canonicalTurns?.slice(covered) ?? [])]
              return { ...c, ...projection, canonicalTurns, historyLoaded: true, retrySyncError: false,
                lastCanonicalTurn: c.lastCanonicalTurn && c.lastCanonicalTurn.index >= covered
                  ? c.lastCanonicalTurn : projection.lastCanonicalTurn,
                messages: [...messages, ...c.messages.filter((m) => m.turnIndex === undefined || m.turnIndex >= covered)],
                turnCount: Math.max(count, canonicalTurns.length) }
            }),
          }))
        // 发送暂存回收：上次刷新可能死在 multipart 上传途中——凭据仅剩 outbox。
        // **不清条目**（首审 H3）：清了键,用户按提示重发就换新键 = 服务端判不了重,
        // 恰好在跨刷新这条主路上把幂等做没了。提示只出一次(hinted),条目留到
        // 202/turn_ended 对账清仓;48h 后过期自清(防永久残留)。
        const pending = outboxAll()[id]
        if (pending) {
          if (Date.now() - pending.ts > 48 * 3600 * 1000) {
            outboxClear(id, pending)
          } else {
            if (!pending.hinted && !optimistic.has(id)) {
              outboxSet(id, pending.text, pending.key, true)
              appendMessages(id, [{
                id: nextMessageId(),
                role: "system",
                content: currentDict().store.outboxMaybeLost(pending.text),
                variant: "warning",
              }])
            }
          }
        }
          ensureAttached(id)
        } catch (e) {
          if (!isCurrent(id, epoch)) return
          console.warn("加载会话历史失败:", e)
          patchConversation(id, { historyLoaded: false, retrySyncError: true })
        } finally {
          if (historyLoads.get(id) === request) historyLoads.delete(id)
        }
      })()
      return request.promise
    },

    sendMessage: async (text, files) => {
      const convId = get().activeId
      if (guardDeleting(convId)) return
      const epoch = selectionEpoch
      const conv = get().conversations.find((c) => c.id === convId)
      if (!conv || get().generating[convId]) return

      // 本地放弃标记随新消息回收（后端 accept 时同步收尾暂停态旧轮,事件流会带回终态）
      locallyAbandonedSuspended.delete(convId)
      set((s) => {
        const susp = { ...s.suspendedSessions }
        delete susp[convId]
        const approvals = { ...s.pendingApprovals }
        delete approvals[convId]
        return { suspendedSessions: susp, pendingApprovals: approvals }
      })

      const attachments: MessageAttachment[] = files.map((f) => ({
        name: f.name,
        kind: f.type.startsWith("audio/")
          ? ("audio" as const)
          : f.type.startsWith("image/")
            ? ("image" as const)
            : ("file" as const), // PDF/文档/代码等：后端摄入层解析或懒登记
        previewUrl: f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined,
      }))

      // 乐观用户气泡 + 发送暂存（202 受理即清；刷新致死时 outbox 是唯一凭据）
      const userId = nextMessageId()
      // 键纪律(B3):纯文本同文重发复用旧键;**带附件不复用也不存键**——键只比对得了
      // 文本,同文换附件若被判重,新附件会被静默丢弃(首审 M6)
      const clientTurnId = files.length === 0 ? outboxKeyFor(convId, text) : newSessionId()
      const submittedOutbox = outboxSet(convId, text, files.length === 0 ? clientTurnId : undefined)
      const submission = { userId, prompt: text, clientTurnId, receipt: submittedOutbox.receipt!, turnId: undefined as string | undefined, seq: undefined as number | undefined }
      optimistic.set(convId, submission)
      appendMessages(convId, [
        {
          id: userId,
          role: "user",
          content: text,
          attachments: attachments.length > 0 ? attachments : undefined,
        },
      ])
      if (conv.title === NEW_CHAT_TITLE) {
        patchConversation(convId, { title: text.slice(0, 30) || conv.title })
      }
      setGenerating(convId, true)

      try {
        const res = await withSessionWrite(convId, () => submitTurn(convId, {
          prompt: text,
          enable_thinking: get().thinkingEnabled,
          client_turn_id: clientTurnId,
          files: files.length > 0 ? files : undefined,
        }))
        if (outboxAll()[convId]?.receipt === submission.receipt) outboxClear(convId, submission.receipt)
        if (!isCurrent(convId, epoch) || optimistic.get(convId) !== submission) return
        if (res.duplicate) {
          // 判重命中(首审 M11):返回的是既有轮,可能早已完结——干等事件会永远转圈。
          // 撤乐观气泡,走统一恢复入口重新对账(/history + /turn + 重放)。
          optimistic.delete(convId)
          removeMessages(convId, [userId])
          setGenerating(convId, false)
          detach(convId)
          void get().loadHistory(convId)
          return
        }
        submission.turnId = res.turn_id
        submission.seq = res.seq
        if (res.turn_id && lastEndedIds.get(convId) === res.turn_id) {
          // Terminal may arrive before 202. Receipt now binds that already-rendered record.
          removeMessages(convId, [submission.userId])
          optimistic.delete(convId)
          setGenerating(convId, false)
        } else if (res.turn_id && liveMsg.get(convId)?.turnId === res.turn_id) {
          const ids = liveMsg.get(convId)!
          if (ids.userId !== submission.userId) removeMessages(convId, [ids.userId])
          liveMsg.set(convId, { ...ids, userId: submission.userId })
        } else if (res.turn_id && res.seq && (cursors.get(convId) ?? 0) >= res.seq) {
          // Receipt overtook events without a retained matching lifecycle. Recover through
          // the canonical entrance rather than guessing which rendered prompt it belongs to.
          detach(convId)
          void get().loadHistory(convId)
          return
        }
        ensureAttached(convId) // 首条消息后会话才在服务端存在（此前附着会 404 退出）
      } catch (e) {
        // 网络类失败**不清 outbox**（B3）:请求可能已达、只是 202 回程丢了——键留着,
        // 用户重发同文即复用,服务端判重不二次执行。真冲突(409)才清。
        if (e instanceof TurnConflictError && outboxAll()[convId]?.receipt === submission.receipt) outboxClear(convId, submission.receipt)
        if (!isCurrent(convId, epoch) || optimistic.get(convId) !== submission || submission.turnId) return
        optimistic.delete(convId)
        removeMessages(convId, [userId])
        setGenerating(convId, false)
        if (e instanceof PendingDeletionError) return
        if (e instanceof TurnConflictError) {
          appendMessages(convId, [{
            id: nextMessageId(),
            role: "system",
            content: currentDict().store.turnConflict,
            variant: "warning",
          }])
        } else {
          console.error("提交轮次失败:", e)
          appendMessages(convId, [{
            id: nextMessageId(),
            role: "system",
            content: currentDict().store.cannotConnect,
            variant: "error",
          }])
        }
      }
    },

    respondApproval: async (id, approve, expectedTarget) => {
      if (guardDeleting(id)) return
      const epoch = selectionEpoch
      const target = get().pendingApprovalTargets[id]
      if (get().activeId !== id || !target || target.turnId !== expectedTarget.turnId || target.seq !== expectedTarget.seq || !get().pendingApprovals[id]) return
      const tools = get().pendingApprovals[id]
      // 乐观收卡 + 置生成中；事件流的 resumed/turn_started 会确认（或 409 时回滚）
      set((s) => {
        const next = { ...s.pendingApprovals }
        delete next[id]
        return { pendingApprovals: next, generating: { ...s.generating, [id]: true } }
      })
      try {
        await withSessionWrite(id, () => submitApproval(id, approve, expectedTarget.turnId, expectedTarget.seq))
      } catch (e) {
        // 切会话/新轮/新一批审批/已 resumed 后，旧请求不得回滚或重附着当前投影。
        if (!isCurrent(id, epoch) || get().pendingApprovalTargets[id] !== target) return
        setGenerating(id, false)
        if (e instanceof TurnConflictError) {
          // 决议目标已不存在（另一端已处理/已过期）：以服务端为准重载
          detach(id)
          void get().loadHistory(id)
        } else {
          if (e instanceof PendingDeletionError) {
            set((s) => ({ pendingApprovals: { ...s.pendingApprovals, [id]: tools } }))
            return
          }
          console.error("提交审批决议失败:", e)
          set((s) => ({ pendingApprovals: { ...s.pendingApprovals, [id]: tools } }))
          appendMessages(id, [{
            id: nextMessageId(),
            role: "system",
            content: currentDict().store.approvalCannotConnect,
            variant: "error",
          }])
        }
      }
    },

    abandonSuspended: (id) => {
      if (guardDeleting(id)) return
      // 仅清本地可恢复态；后端的挂起轮会在下次发消息时被判为放弃并回收（TurnRunner 处理）。
      locallyAbandonedSuspended.add(id)
      set((s) => {
        const next = { ...s.suspendedSessions }
        delete next[id]
        return { suspendedSessions: next }
      })
      appendMessages(id, [
        {
          id: nextMessageId(),
          role: "system",
          content: currentDict().store.abandoned,
          variant: "warning",
        },
      ])
    },

    deleteLastTurn: async (id) => (await deleteAndRestoreLastTurn(id)) !== null,

    updateLastTurn: async (text) => {
      const convId = get().activeId
      if (guardDeleting(convId)) return
      if (get().generating[convId] || !text.trim()) return
      // 先回退最后一轮（后端同步记忆与落盘），成功后按新内容走完整发送链路
      const restoredEpoch = await deleteAndRestoreLastTurn(convId)
      if (restoredEpoch === null || !isCurrent(convId, restoredEpoch)) return
      await get().sendMessage(text, [])
    },

    stopGenerating: (id, source = "chat") => requestControl(id, "cancel", source),
    suspendTask: (id) => requestControl(id, "suspend"),
    resumeTask: async (id) => {
      locallyAbandonedSuspended.delete(id)
      await requestControl(id, "resume", id === get().activeId ? "chat" : "global")
    },

    branchFrom: async (id, nodeIndex) => {
      if (guardDeleting(id)) return
      const epoch = selectionEpoch
      const parent = get().conversations.find((c) => c.id === id)
      if (!parent) return
      try {
        const res = await withSessionWrite(id, () => branchSession(id, { node_index: nodeIndex }))
        if (!isCurrent(id, epoch)) return
        if (!res.status || !res.session_id) {
          set({ notice: res.response || currentDict().store.branchFailed })
          return
        }
        const d = currentDict()
        const parentTitle = parent.title === NEW_CHAT_TITLE ? d.store.newChatTitle : parent.title
        const branch: Conversation = {
          id: res.session_id,
          title: `${parentTitle} · ${d.store.branchSuffix}`,
          updatedAt: Date.now(),
          messages: [],
          historyLoaded: false, // 交给 loadHistory 从服务端拉取分支携带的历史
          turnCount: res.turns ?? 0,
        }
        const prev = get().activeId
        if (prev) detach(prev)
        selecting(branch.id)
        set((s) => ({
          conversations: [branch, ...s.conversations],
          activeId: branch.id,
        }))
      } catch (e) {
        if (!isCurrent(id, epoch)) return
        console.error("创建分支失败:", e)
        set({ notice: currentDict().store.branchFailedConn })
      }
    },
  }
})

// 元数据持久化：消息本体不落地（刷新后通过 /history 恢复文本历史）
useChatStore.subscribe((s) => {
  const data: PersistedState = {
    conversations: s.conversations.map(
      ({ id, title, updatedAt, turnCount, pinned }) => ({
        id, title, updatedAt, turnCount, pinned,
      }),
    ),
    activeId: s.activeId,
    thinkingEnabled: s.thinkingEnabled,
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data))
  } catch {
    // 忽略配额等存储错误(会话元数据也可从 /sessions 对账恢复)
  }
})
