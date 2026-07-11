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
  getUiState,
  listSessions,
  putUiState,
  setSessionPin,
  submitApproval,
  submitResume,
  submitTurn,
  suspendTask as apiSuspendTask,
  TurnConflictError,
} from "@/api/agent"
import type {
  ChatResponse,
  LiveActivity,
  PendingTool,
  TaskSnapshot,
} from "@/types/agent-api"
import type { EventEnvelope } from "@/types/protocol"
import {
  initialTurnState,
  reduceTurnEvent,
  type NormalizedState,
} from "@/lib/turn-reducer"
import {
  mapResponseMessages,
  newSessionId,
  nextMessageId,
  turnToMessages,
  type ChatMessage,
  type Conversation,
  type MessageAttachment,
  type Workspace,
} from "@/lib/agent-data"
import { currentDict } from "@/store/useUiStore"

const STORAGE_KEY = "macagent-chat-v1"
/** 发送暂存（outbox）：提交前落 localStorage,202 受理即清——浏览器在 multipart
 *  上传中途被杀(刷新)时,这是唯一能证明"有话没送达"的凭据(PROTOCOL.md §5)。 */
const OUTBOX_KEY = "macagent-outbox-v1"
/** 语言无关的「新对话」标题哨兵：存储用此值，展示时按当前语言翻译（见 lib/agent-data 的 convTitle）。 */
export const NEW_CHAT_TITLE = "__NEW_CHAT__"

/** 落盘到 localStorage 的部分：会话元数据（消息刷新后从 /history 恢复）。 */
type PersistedState = {
  workspaces: Workspace[]
  conversations: Array<
    Pick<Conversation, "id" | "title" | "updatedAt" | "workspaceId" | "turnCount" | "pinned">
  >
  activeId: string
  thinkingEnabled: boolean
}

function freshConversation(workspaceId: string | null = null): Conversation {
  return {
    id: newSessionId(),
    title: NEW_CHAT_TITLE,
    updatedAt: Date.now(),
    workspaceId,
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
    workspaces: persisted?.workspaces ?? [],
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
/** 活跃轮的界面簿记：预览气泡/用户气泡的消息 id 与轮次 id 的配对。 */
const liveMsg = new Map<string, { turnId?: string; userId?: string; assistantId?: string }>()
/** 本 tab 乐观追加的用户气泡（turn_queued 到达时被收编,不重复建）。 */
const optimistic = new Map<string, { userId: string; prompt: string }>()

/** 用户已在本地「放弃」的挂起会话：后端句柄要等下条消息才真正回收，期间 /tasks 对账
 *  （useUiStore.syncSuspendedSessions）须跳过这些 id，否则放弃的横幅每 3s 被轮询复活。 */
export const locallyAbandonedSuspended = new Set<string>()

// ---- 发送暂存(outbox) ----
function outboxAll(): Record<string, { text: string; ts: number }> {
  try {
    return JSON.parse(localStorage.getItem(OUTBOX_KEY) ?? "{}")
  } catch {
    return {}
  }
}

function outboxSet(sid: string, text: string) {
  const all = outboxAll()
  all[sid] = { text, ts: Date.now() }
  try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(all)) } catch { /* 配额 */ }
}

function outboxClear(sid: string) {
  const all = outboxAll()
  if (sid in all) {
    delete all[sid]
    try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(all)) } catch { /* 配额 */ }
  }
}

// ---- UI 状态服务端同步（客户端迁移）----
// localStorage 仍是本地缓存/离线兜底；服务端为跨设备权威（last-write-wins）。
let _hydrating = false // hydrate 落地本地时不回写服务端（防止把刚拉下来的又推一遍）
let _uiPushTimer: ReturnType<typeof setTimeout> | null = null
let _lastPushedRaw = ""

function uiStatePayload(s: { workspaces: Workspace[]; conversations: Conversation[]; thinkingEnabled: boolean }) {
  // 同步范围 = PersistedState 减 activeId（焦点是设备本地偏好，跨设备同步会互抢）
  return {
    workspaces: s.workspaces,
    conversations: s.conversations.map(
      ({ id, title, updatedAt, workspaceId, turnCount, pinned }) => ({
        id, title, updatedAt, workspaceId, turnCount, pinned,
      }),
    ),
    thinkingEnabled: s.thinkingEnabled,
  }
}

function pushUiStateNow(s: { workspaces: Workspace[]; conversations: Conversation[]; thinkingEnabled: boolean }) {
  const payload = uiStatePayload(s)
  const raw = JSON.stringify(payload)
  if (raw === _lastPushedRaw) return
  _lastPushedRaw = raw
  // fire-and-forget：失败静默，下次变更会带全量重推（last-write-wins 自愈）
  void putUiState(payload).catch(() => { _lastPushedRaw = "" })
}

export interface LastMetrics {
  promptTokens?: number
  generationTokens?: number
  generationTps?: number
}

interface ChatState {
  workspaces: Workspace[]
  conversations: Conversation[]
  activeId: string
  thinkingEnabled: boolean
  /** sessionId -> 有轮次在跑（queued/running，事件流驱动） */
  generating: Record<string, boolean>
  /** sessionId -> 等待用户授权的敏感工具调用（删库等不可逆操作） */
  pendingApprovals: Record<string, PendingTool[]>
  /** sessionId -> 是否有一个已挂起、可恢复的任务（suspended；等待 resume/放弃） */
  suspendedSessions: Record<string, boolean>
  /** sessionId -> 多步任务进度快照（plan_updated 事件驱动；任务跨轮存续时保留） */
  taskProgress: Record<string, TaskSnapshot | null>
  /** sessionId -> 实时步骤流（事件归约的 preview.steps 投影，仅生成期间有值） */
  liveActivity: Record<string, LiveActivity | null>
  /** 顶部横幅提示（分支失败等业务性错误） */
  notice: string | null
  lastMetrics: LastMetrics | null

  selectConversation: (id: string) => void
  newConversation: (workspaceId: string | null) => void
  /** 打开定时任务的专属会话（schedule_{id}）：本地无该会话条目则以任务名补建后跳转。 */
  openScheduleSession: (sessionId: string, title: string) => void
  renameConversation: (id: string, title: string) => void
  moveConversation: (id: string, workspaceId: string | null) => void
  deleteConversation: (id: string) => void
  /** 批量删除会话（多选清理）：逐个同步后端删除并从本地移除。 */
  deleteConversations: (ids: string[]) => void
  /** 置/撤会话 pin（乐观更新 + 后端落盘，失败回滚）：pin 的会话不被后台自动清扫。 */
  togglePin: (id: string, pinned: boolean) => void
  /** 反向映射对账：以后端会话索引为真相，剔除已被回收的幽灵条目、补入孤儿会话、同步 pin。 */
  reconcileSessions: () => Promise<void>
  addWorkspace: (name: string) => void
  renameWorkspace: (id: string, name: string) => void
  /** 删除工作区：其内会话回归「最近对话」（workspaceId 置 null），不删任何会话。 */
  deleteWorkspace: (id: string) => void
  setThinking: (value: boolean) => void
  clearNotice: () => void

  /** 启动时从服务端拉取 UI 状态（工作区/会话元数据），实现换设备无缝迁移。 */
  hydrateFromServer: () => Promise<void>
  /** 加载服务端历史 + 附着事件流（App 在 activeId 变化时调用；账本协议的统一恢复入口）。 */
  loadHistory: (id: string) => Promise<void>
  sendMessage: (text: string, files: File[]) => Promise<void>
  /** 对挂起的敏感操作作出决议：approve=true 执行，false 拒绝（202 受理，结果走事件流）。 */
  respondApproval: (id: string, approve: boolean) => Promise<void>
  /** 删除最后一轮问答：后端同步回退记忆/KV/落盘，成功后从服务端重载消息。 */
  deleteLastTurn: (id: string) => Promise<boolean>
  /** 更新最后一次发言：删除最后一轮后按新内容重新发送生成。 */
  updateLastTurn: (text: string) => Promise<void>
  /** 真取消：后端在最近检查点停止并整轮回滚；turn_ended(cancelled) 从事件流回来收尾。 */
  stopGenerating: (id: string) => void
  /** 挂起当前运行中的任务：后端在安全点让出；suspended 事件从流上回来置入可恢复态。 */
  suspendTask: (id: string) => void
  /** 恢复一个已挂起的任务（202 受理，续跑过程走事件流）。 */
  resumeTask: (id: string) => Promise<void>
  /** 放弃一个已挂起的任务：清除本地可恢复态（后端挂起轮会在下次发消息时被丢弃回收）。 */
  abandonSuspended: (id: string) => void
  /** 从某个历史节点派生新会话（API.md §3.4），成功后切换过去。 */
  branchFrom: (id: string, nodeIndex: number) => Promise<void>
}

export const useChatStore = create<ChatState>((set, get) => {
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
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id ? { ...c, messages: c.messages.filter((m) => !drop.has(m.id)) } : c,
      ),
    }))
  }

  function setGenerating(id: string, value: boolean) {
    set((s) => ({ generating: { ...s.generating, [id]: value } }))
  }

  // ---------- 事件附着与归约（账本协议核心） ----------

  function ensureAttached(sid: string) {
    if (attachCtls.has(sid)) return
    const ctl = new AbortController()
    attachCtls.set(sid, ctl)
    void attachLoop(sid, ctl)
  }

  /** 断开附着并清空该会话的活跃轮簿记。reload=true 时置 historyLoaded=false,
   *  下次激活经 /history + /turn + 重放完整恢复(切走-切回的对账路径)。 */
  function detach(sid: string, reload = true) {
    attachCtls.get(sid)?.abort()
    attachCtls.delete(sid)
    cursors.delete(sid)
    liveTurns.delete(sid)
    liveMsg.delete(sid)
    optimistic.delete(sid)
    if (reload) patchConversation(sid, { historyLoaded: false })
  }

  async function attachLoop(sid: string, ctl: AbortController) {
    let backoff = 1000
    while (!ctl.signal.aborted) {
      try {
        let after = cursors.get(sid)
        if (after === undefined) {
          // 初次附着：/turn 定游标——有活跃轮从轮首整段重放(完整重建直播视图),
          // 无活跃轮从账本末尾只收新事件(落盘轮次由 /history 提供,避免重复对账)。
          const ts = await getTurnState(sid)
          after = ts.turn ? ts.turn.first_seq - 1 : ts.last_seq
          cursors.set(sid, after)
          if (!ts.turn) {
            // 无活跃轮：清掉可能残留的进行中标志（如后台完结后切回）
            setGenerating(sid, false)
          }
        }
        for await (const env of attachEvents(sid, after, ctl.signal)) {
          backoff = 1000
          if (env.seq > 0) {
            const cur = cursors.get(sid) ?? 0
            if (env.seq <= cur) continue // 重连重放的重复事件
            cursors.set(sid, env.seq)
          }
          dispatch(sid, env)
        }
        break // 服务端正常收流（会话被删除）：结束附着
      } catch (e) {
        if (ctl.signal.aborted) break
        if (String(e).includes("404")) break // 会话尚不存在：首条消息受理后会再附着
        // 断开是常态：静默指数退避重连（游标在,断线期间的事件重连即补）
        await new Promise((r) => setTimeout(r, backoff))
        backoff = Math.min(backoff * 2, 10000)
      }
    }
    attachCtls.delete(sid)
  }

  /** 事件分发：归约器推进活跃轮状态,再把结果投影进 UI store。 */
  function dispatch(sid: string, env: EventEnvelope) {
    if (env.type === "stream_attached") return
    const next = reduceTurnEvent(liveTurns.get(sid) ?? initialTurnState(), env)
    liveTurns.set(sid, next)

    switch (env.type) {
      case "turn_queued":
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
        syncPreviewBubble(sid, next)
        break
      case "tool_started":
      case "tool_progress":
      case "tool_result":
      case "step_completed":
      case "step_failed":
        syncActivity(sid, next)
        break
      case "plan_updated":
        set((s) => ({ taskProgress: { ...s.taskProgress, [sid]: next.preview.task } }))
        syncActivity(sid, next)
        break
      case "awaiting_approval":
        setGenerating(sid, false)
        set((s) => ({ pendingApprovals: { ...s.pendingApprovals, [sid]: next.pending_tools } }))
        pruneEmptyPreview(sid)
        break
      case "suspended":
        setGenerating(sid, false)
        set((s) => ({ suspendedSessions: { ...s.suspendedSessions, [sid]: true } }))
        pruneEmptyPreview(sid)
        break
      case "turn_ended":
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
    if (opt) {
      userId = opt.userId // 收编乐观气泡（内容即 prompt）
      optimistic.delete(sid)
    } else {
      userId = nextMessageId()
      appendMessages(sid, [{ id: userId, role: "user", content: prompt }])
    }
    liveMsg.set(sid, { turnId: env.turn_id ?? undefined, userId })

    const conv = get().conversations.find((c) => c.id === sid)
    if (conv && conv.title === NEW_CHAT_TITLE && prompt) {
      patchConversation(sid, { title: prompt.slice(0, 30) })
    }
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

  /** 实时步骤流投影：归约的 preview.steps → ActivityCard 的 LiveActivity 形状。 */
  function syncActivity(sid: string, live: NormalizedState) {
    const runningTool = [...live.preview.steps].reverse().find((s) => s.status === "running")
    const activity: LiveActivity = {
      phase: live.turn_state === "running" || live.turn_state === "queued" ? "running" : live.turn_state,
      current: runningTool ? runningTool.label + (runningTool.detail ? `（${runningTool.detail}）` : "") : "",
      steps: live.preview.steps
        .filter((s) => s.status !== "running")
        .map((s) => ({ kind: s.kind, label: s.label, detail: s.detail })),
    }
    set((s) => ({ liveActivity: { ...s.liveActivity, [sid]: activity } }))
  }

  /** 权威终态：撤下预览簿记,按 turn_ended 的完整轮次记录对账（含 user 消息）。 */
  function onTurnEnded(sid: string, env: EventEnvelope) {
    const data = env.data as unknown as ChatResponse & { turn_index?: number }
    const ids = liveMsg.get(sid)
    removeMessages(sid, [ids?.userId, ids?.assistantId])
    liveMsg.delete(sid)
    optimistic.delete(sid)
    outboxClear(sid)

    const conv = get().conversations.find((c) => c.id === sid)
    const turnIndex = typeof data.turn_index === "number"
      ? data.turn_index
      : (conv?.turnCount ?? 0)
    // 对账去重：/history 响应与 turn_ended 事件到达顺序不定,该轮已由历史渲染则跳过
    if (!conv?.messages.some((m) => m.turnIndex === turnIndex)) {
      applyEndedTurn(sid, data, turnIndex)
    } else {
      patchConversation(sid, { turnCount: turnIndex + 1 })
    }

    setGenerating(sid, false)
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
    getTaskStatus(sid)
      .then((res) => {
        set((s) => ({
          taskProgress: { ...s.taskProgress, [sid]: res.active ? (res.task ?? null) : null },
        }))
      })
      .catch(() => {})
  }

  /** 把一轮终态渲染进消息流（沿用旧 turn_end 对账口径,终态本地化提示不变）。 */
  function applyEndedTurn(convId: string, res: ChatResponse, turnIndex: number) {
    if (res.status === "cancelled") {
      // 后端已整轮回滚（用户消息与中间产物均未进入模型记忆），仅存档取消记录
      appendMessages(convId, [
        {
          id: nextMessageId(),
          role: "user",
          content: res.prompt,
          turnIndex,
        },
        {
          id: nextMessageId(),
          role: "system",
          content: currentDict().store.cancelled,
          variant: "warning",
          turnIndex,
        },
      ])
    } else if (res.status === "fail") {
      // response 里是含 Python traceback 的 system 消息，不直接展示原文
      const traceback = res.response?.find((m) => m.role === "system")
      console.error("模型引擎故障:", traceback && "content" in traceback ? traceback.content : res)
      appendMessages(convId, [
        { id: nextMessageId(), role: "user", content: res.prompt, turnIndex },
        {
          id: nextMessageId(),
          role: "system",
          content: currentDict().store.engineFail,
          variant: "error",
          turnIndex,
        },
      ])
    } else {
      const messages = [
        { id: nextMessageId(), role: "user" as const, content: res.prompt, turnIndex },
        ...mapResponseMessages(res.response ?? [], turnIndex,
          res.status === "warning" ? "warning" : "error"),
      ]
      if (res.status === "warning" && !messages.some((m) => m.role === "assistant" && m.content)) {
        messages.push({
          id: nextMessageId(),
          role: "assistant",
          content: currentDict().store.warningNoConclusion,
          variant: "warning",
          turnIndex,
        })
      }
      appendMessages(convId, messages)
    }
    patchConversation(convId, { turnCount: turnIndex + 1 })
    set({
      lastMetrics: {
        promptTokens: res.prompt_tokens,
        generationTokens: res.generation_tokens,
        generationTps: res.generation_tps,
      },
    })
  }

  return {
    ...loadInitialState(),
    generating: {},
    pendingApprovals: {},
    suspendedSessions: {},
    taskProgress: {},
    liveActivity: {},
    notice: null,
    lastMetrics: null,

    selectConversation: (id) => {
      const prev = get().activeId
      if (prev && prev !== id) detach(prev) // 单附着：切走即断,回来经历史+重放对账
      set({ activeId: id })
    },

    newConversation: (workspaceId) => {
      const prev = get().activeId
      if (prev) detach(prev)
      const fresh = freshConversation(workspaceId)
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
        workspaceId: null,
        messages: [],
        historyLoaded: false,
        turnCount: 0,
      }
      set((s) => ({ conversations: [conv, ...s.conversations], activeId: sessionId }))
    },

    renameConversation: (id, title) => {
      if (title.trim()) patchConversation(id, { title: title.trim(), updatedAt: Date.now() })
    },

    moveConversation: (id, workspaceId) => patchConversation(id, { workspaceId }),

    deleteConversation: (id) => {
      detach(id, false)
      // 服务端同步删除该 session 的全部落盘数据；失败也不阻塞本地移除
      deleteSession(id).catch((e) => console.warn("删除服务端会话失败:", e))
      set((s) => {
        let next = s.conversations.filter((c) => c.id !== id)
        if (next.length === 0) next = [freshConversation()]
        const pendingNext = { ...s.pendingApprovals }
        delete pendingNext[id]
        const suspNext = { ...s.suspendedSessions }
        delete suspNext[id]
        const taskNext = { ...s.taskProgress }
        delete taskNext[id]
        const actNext = { ...s.liveActivity }
        delete actNext[id]
        return {
          conversations: next,
          activeId: s.activeId === id ? next[0].id : s.activeId,
          pendingApprovals: pendingNext,
          suspendedSessions: suspNext,
          taskProgress: taskNext,
          liveActivity: actNext,
        }
      })
    },

    deleteConversations: (ids) => {
      const idSet = new Set(ids)
      if (idSet.size === 0) return
      for (const id of idSet) {
        detach(id, false)
        deleteSession(id).catch((e) => console.warn("批量删除服务端会话失败:", e))
      }
      set((s) => {
        let next = s.conversations.filter((c) => !idSet.has(c.id))
        if (next.length === 0) next = [freshConversation()]
        const drop = (rec: Record<string, unknown>) => {
          const out = { ...rec }
          for (const id of idSet) delete out[id]
          return out
        }
        return {
          conversations: next,
          activeId: idSet.has(s.activeId) ? next[0].id : s.activeId,
          pendingApprovals: drop(s.pendingApprovals) as typeof s.pendingApprovals,
          suspendedSessions: drop(s.suspendedSessions) as typeof s.suspendedSessions,
          taskProgress: drop(s.taskProgress) as typeof s.taskProgress,
          liveActivity: drop(s.liveActivity) as typeof s.liveActivity,
        }
      })
    },

    togglePin: (id, pinned) => {
      // 乐观更新本地，失败回滚（不改 updatedAt：pin 不算内容变更，不该抬高排序时间）
      patchConversation(id, { pinned })
      setSessionPin(id, pinned).catch((e) => {
        console.warn("pin 落盘失败，回滚:", e)
        patchConversation(id, { pinned: !pinned })
      })
    },

    reconcileSessions: async () => {
      let index
      try {
        index = await listSessions()
      } catch {
        return // 后端不可达：静默沿用本地
      }
      // schedule_ 前缀会话由定时任务条独立管理，不并入普通会话列表
      const onDisk = new Map(
        index.sessions
          .filter((m) => !m.session_id.startsWith("schedule_"))
          .map((m) => [m.session_id, m]),
      )
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
            workspaceId: null,
            messages: [],
            historyLoaded: false,
            turnCount: m.turn_count,
            pinned: m.pinned,
          })
        }
        let next = [...synced, ...orphans]
        if (next.length === 0) next = [freshConversation()]
        return {
          conversations: next,
          activeId: next.some((c) => c.id === s.activeId) ? s.activeId : next[0].id,
        }
      })
    },

    addWorkspace: (name) => {
      const id = `${Date.now()}-w`
      set((s) => ({ workspaces: [...s.workspaces, { id, name }] }))
    },

    renameWorkspace: (id, name) => {
      const trimmed = name.trim()
      if (!trimmed) return
      set((s) => ({
        workspaces: s.workspaces.map((w) => (w.id === id ? { ...w, name: trimmed } : w)),
      }))
    },

    deleteWorkspace: (id) => {
      // 只删分组不删内容：会话回归「最近对话」，服务端数据不动
      set((s) => ({
        workspaces: s.workspaces.filter((w) => w.id !== id),
        conversations: s.conversations.map((c) =>
          c.workspaceId === id ? { ...c, workspaceId: null } : c,
        ),
      }))
    },

    setThinking: (value) => set({ thinkingEnabled: value }),

    clearNotice: () => set({ notice: null }),

    hydrateFromServer: async () => {
      try {
        const res = await getUiState()
        const remote = res.state as Partial<PersistedState> | null
        if (remote && Array.isArray(remote.workspaces) && Array.isArray(remote.conversations)) {
          // 服务端为准：整体替换工作区与会话元数据（消息仍按需从 /history 拉取）
          const conversations: Conversation[] = remote.conversations.map((c) => ({
            ...c,
            messages: [],
            historyLoaded: false,
          }))
          if (conversations.length === 0) conversations.push(freshConversation())
          _hydrating = true
          try {
            set((s) => ({
              workspaces: remote.workspaces as Workspace[],
              conversations,
              thinkingEnabled: !!remote.thinkingEnabled,
              // activeId 是设备本地偏好：仍存在则保留，否则回退到第一条
              activeId: conversations.some((c) => c.id === s.activeId)
                ? s.activeId
                : conversations[0].id,
            }))
          } finally {
            _hydrating = false
          }
          void get().loadHistory(get().activeId)
        } else {
          // 服务端从未写入：把本设备 localStorage 的既有状态一次性迁移上云
          pushUiStateNow(get())
        }
      } catch {
        // 后端不可达：静默沿用 localStorage 启动值
      }
    },

    loadHistory: async (id) => {
      const conv = get().conversations.find((c) => c.id === id)
      if (!conv) return
      ensureAttached(id) // 统一恢复入口：历史（落盘轮）+ 附着（活跃轮重放 + 实时续播）
      if (conv.historyLoaded || get().generating[id]) return
      // 先置位，避免重复请求
      patchConversation(id, { historyLoaded: true })
      try {
        const history = await getHistory(id)
        const messages = (history.turns ?? []).flatMap((turn, index) =>
          turnToMessages(turn, index),
        )
        const count = history.count ?? 0
        set((s) => ({
          conversations: s.conversations.map((c) =>
            // 加载期间事件流可能已开始追加活跃轮气泡，保留已有尾部;
            // 但事件流抢先对账过的已落盘轮次(turnIndex < count)以历史为准去重
            c.id === id
              ? {
                  ...c,
                  messages: [
                    ...messages,
                    ...c.messages.filter(
                      (m) => m.turnIndex === undefined || m.turnIndex >= count,
                    ),
                  ],
                  turnCount: count,
                }
              : c,
          ),
        }))
        // 发送暂存回收：上次刷新可能死在 multipart 上传途中——凭据仅剩 outbox
        const pending = outboxAll()[id]
        if (pending) {
          outboxClear(id)
          const lastUser = [...(history.turns ?? [])].reverse()[0]?.prompt
          if (lastUser !== pending.text) {
            appendMessages(id, [{
              id: nextMessageId(),
              role: "system",
              content: currentDict().store.outboxMaybeLost(pending.text),
              variant: "warning",
            }])
          }
        }
      } catch (e) {
        console.warn("加载会话历史失败:", e)
        patchConversation(id, { historyLoaded: false })
      }
    },

    sendMessage: async (text, files) => {
      const convId = get().activeId
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
      optimistic.set(convId, { userId, prompt: text })
      outboxSet(convId, text)
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
        await submitTurn(convId, {
          prompt: text,
          enable_thinking: get().thinkingEnabled,
          files: files.length > 0 ? files : undefined,
        })
        outboxClear(convId)
        ensureAttached(convId) // 首条消息后会话才在服务端存在（此前附着会 404 退出）
      } catch (e) {
        outboxClear(convId)
        optimistic.delete(convId)
        removeMessages(convId, [userId])
        setGenerating(convId, false)
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

    respondApproval: async (id, approve) => {
      if (!get().pendingApprovals[id]) return
      const tools = get().pendingApprovals[id]
      // 乐观收卡 + 置生成中；事件流的 resumed/turn_started 会确认（或 409 时回滚）
      set((s) => {
        const next = { ...s.pendingApprovals }
        delete next[id]
        return { pendingApprovals: next, generating: { ...s.generating, [id]: true } }
      })
      try {
        await submitApproval(id, approve)
      } catch (e) {
        setGenerating(id, false)
        if (e instanceof TurnConflictError) {
          // 决议目标已不存在（另一端已处理/已过期）：以服务端为准重载
          detach(id)
          void get().loadHistory(id)
        } else {
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

    deleteLastTurn: async (id) => {
      if (get().generating[id]) return false
      try {
        const res = await apiDeleteLastTurn(id)
        if (!res.status) {
          set({ notice: res.response || currentDict().store.deleteTurnFailed })
          return false
        }
        // 后端已同步回退（含撤销未完结的审批轮）：清除本地审批挂起态，
        // 重置消息并从服务端重新拉取，保证与后端记忆/落盘一致
        detach(id, false)
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
                    historyLoaded: false,
                    turnCount: res.remaining_turns ?? Math.max(0, c.turnCount - 1),
                    updatedAt: Date.now(),
                  }
                : c,
            ),
          }
        })
        await get().loadHistory(id)
        return true
      } catch (e) {
        console.error("删除最后一轮失败:", e)
        set({ notice: currentDict().store.deleteTurnFailedConn })
        return false
      }
    },

    updateLastTurn: async (text) => {
      const convId = get().activeId
      if (get().generating[convId] || !text.trim()) return
      // 先回退最后一轮（后端同步记忆与落盘），成功后按新内容走完整发送链路
      const ok = await get().deleteLastTurn(convId)
      if (!ok) return
      await get().sendMessage(text, [])
    },

    stopGenerating: (id) => {
      // 真取消：后端在最近检查点停止生成并整轮回滚，turn_ended(cancelled) 从事件流回来收尾
      cancelTask(id).catch((e) => console.warn("取消失败:", e))
    },

    suspendTask: (id) => {
      // 挂起（区别于取消）：后端在安全点让出 worker，suspended 事件从流上回来置入可恢复态
      apiSuspendTask(id).catch((e) => console.warn("挂起任务失败:", e))
    },

    resumeTask: async (id) => {
      if (!get().suspendedSessions[id] || get().generating[id]) return
      locallyAbandonedSuspended.delete(id)
      set((s) => {
        const next = { ...s.suspendedSessions }
        delete next[id]
        return { suspendedSessions: next, generating: { ...s.generating, [id]: true } }
      })
      try {
        await submitResume(id)
        ensureAttached(id) // 续跑过程走事件流（已附着则幂等）
      } catch (e) {
        setGenerating(id, false)
        if (e instanceof TurnConflictError) {
          // 挂起句柄已不存在（已被恢复/回收）：以服务端为准重载
          detach(id)
          void get().loadHistory(id)
        } else {
          console.error("恢复任务失败:", e)
          set((s) => ({ suspendedSessions: { ...s.suspendedSessions, [id]: true } }))
          appendMessages(id, [{
            id: nextMessageId(),
            role: "system",
            content: currentDict().store.resumeCannotConnect,
            variant: "error",
          }])
        }
      }
    },

    branchFrom: async (id, nodeIndex) => {
      const parent = get().conversations.find((c) => c.id === id)
      if (!parent) return
      try {
        const res = await branchSession(id, { node_index: nodeIndex })
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
          workspaceId: parent.workspaceId,
          messages: [],
          historyLoaded: false, // 交给 loadHistory 从服务端拉取分支携带的历史
          turnCount: res.turns ?? 0,
        }
        const prev = get().activeId
        if (prev) detach(prev)
        set((s) => ({
          conversations: [branch, ...s.conversations],
          activeId: branch.id,
        }))
      } catch (e) {
        console.error("创建分支失败:", e)
        set({ notice: currentDict().store.branchFailedConn })
      }
    },
  }
})

// 元数据持久化：消息本体不落地（刷新后通过 /history 恢复文本历史）
useChatStore.subscribe((s) => {
  const data: PersistedState = {
    workspaces: s.workspaces,
    conversations: s.conversations.map(
      ({ id, title, updatedAt, workspaceId, turnCount, pinned }) => ({
        id,
        title,
        updatedAt,
        workspaceId,
        turnCount,
        pinned,
      }),
    ),
    activeId: s.activeId,
    thinkingEnabled: s.thinkingEnabled,
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data))
  } catch {
    // 忽略配额等存储错误
  }
  // 服务端同步：防抖 1s 的 fire-and-forget PUT（内容不变时 pushUiStateNow 自去重）
  if (!_hydrating) {
    if (_uiPushTimer !== null) clearTimeout(_uiPushTimer)
    _uiPushTimer = setTimeout(() => {
      _uiPushTimer = null
      pushUiStateNow(useChatStore.getState())
    }, 1000)
  }
})
