import { create } from "zustand"

import {
  branchSession,
  cancelTask,
  deleteLastTurn as apiDeleteLastTurn,
  deleteSession,
  getHistory,
  getTaskStatus,
  getUiState,
  putUiState,
  resumeTask as apiResumeTask,
  sendApproval,
  sendChatStream,
  suspendTask as apiSuspendTask,
} from "@/api/agent"
import type { ChatResponse, LiveActivity, PendingTool, TaskSnapshot } from "@/types/agent-api"
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
/** 语言无关的「新对话」标题哨兵：存储用此值，展示时按当前语言翻译（见 lib/agent-data 的 convTitle）。 */
export const NEW_CHAT_TITLE = "__NEW_CHAT__"

/** 落盘到 localStorage 的部分：会话元数据（消息刷新后从 /history 恢复）。 */
type PersistedState = {
  workspaces: Workspace[]
  conversations: Array<
    Pick<Conversation, "id" | "title" | "updatedAt" | "workspaceId" | "turnCount">
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

// 每个会话进行中的请求：停止时优先走后端 /task/cancel 真取消，
// 网络不可达时才退回本地 abort（仅中断前端等待）
const controllers = new Map<string, AbortController>()

/** 用户已在本地「放弃」的挂起会话：后端句柄要等下条消息才真正回收，期间 /tasks 对账
 *  （useUiStore.syncSuspendedSessions）须跳过这些 id，否则放弃的横幅每 3s 被轮询复活。 */
export const locallyAbandonedSuspended = new Set<string>()

// 生成期间的任务进度轮询定时器（sessionId -> interval id）
const taskPollers = new Map<string, ReturnType<typeof setInterval>>()

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
      ({ id, title, updatedAt, workspaceId, turnCount }) => ({
        id, title, updatedAt, workspaceId, turnCount,
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
  /** sessionId -> 是否有请求在途 */
  generating: Record<string, boolean>
  /** sessionId -> 等待用户授权的敏感工具调用（删库等不可逆操作） */
  pendingApprovals: Record<string, PendingTool[]>
  /** sessionId -> 是否有一个已挂起、可恢复的任务（suspended 终态；等待 resume/放弃） */
  suspendedSessions: Record<string, boolean>
  /** sessionId -> 多步任务进度快照（生成期间轮询更新；任务跨轮存续时保留） */
  taskProgress: Record<string, TaskSnapshot | null>
  /** sessionId -> 实时步骤流（当前动作 + 已完成步骤，仅生成期间有值） */
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
  addWorkspace: (name: string) => void
  renameWorkspace: (id: string, name: string) => void
  /** 删除工作区：其内会话回归「最近对话」（workspaceId 置 null），不删任何会话。 */
  deleteWorkspace: (id: string) => void
  setThinking: (value: boolean) => void
  clearNotice: () => void

  /** 启动时从服务端拉取 UI 状态（工作区/会话元数据），实现换设备无缝迁移。
   *  服务端有状态→整体替换本地（activeId 保持设备本地）；服务端为空且本地有旧数据→
   *  一次性迁移上云；请求失败→静默用 localStorage（离线可用性不回退）。 */
  hydrateFromServer: () => Promise<void>
  /** 懒加载服务端历史（App 在 activeId 变化时调用）。 */
  loadHistory: (id: string) => Promise<void>
  sendMessage: (text: string, files: File[]) => Promise<void>
  /** 对挂起的敏感操作作出决议：approve=true 执行，false 拒绝。 */
  respondApproval: (id: string, approve: boolean) => Promise<void>
  /** 删除最后一轮问答：后端同步回退记忆/KV/落盘，成功后从服务端重载消息。 */
  deleteLastTurn: (id: string) => Promise<boolean>
  /** 更新最后一次发言：删除最后一轮后按新内容重新发送生成。 */
  updateLastTurn: (text: string) => Promise<void>
  /** 真取消：请求后端在最近检查点停止并整轮回滚；后端不可达时退回本地中断等待。 */
  stopGenerating: (id: string) => void
  /** 挂起当前运行中的任务：后端在安全点让出，本轮以 suspended 收尾，转入可恢复态。 */
  suspendTask: (id: string) => void
  /** 恢复一个已挂起的任务：从挂起处续跑（阻塞式，续跑可能多步）。 */
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

  /** 按 id 局部更新一条消息（真流式 token 逐段填充临时预览气泡）。 */
  function updateMessage(id: string, msgId: string, patch: Partial<ChatMessage>) {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id
          ? { ...c, messages: c.messages.map((m) => (m.id === msgId ? { ...m, ...patch } : m)) }
          : c,
      ),
    }))
  }

  /** 移除一条消息（turn_end 到达后撤下临时预览，改用权威消息对账）。 */
  function removeMessage(id: string, msgId: string) {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id ? { ...c, messages: c.messages.filter((m) => m.id !== msgId) } : c,
      ),
    }))
  }

  /** 生成期间轮询多步任务进度 + 实时步骤流（1.2s 间隔），把黑盒等待变成可见的动作流。 */
  function startTaskPolling(convId: string) {
    stopTaskPolling(convId)
    const timer = setInterval(() => {
      getTaskStatus(convId)
        .then((res) => {
          set((s) => ({
            taskProgress: { ...s.taskProgress, [convId]: res.active ? (res.task ?? null) : null },
            liveActivity: { ...s.liveActivity, [convId]: res.activity ?? null },
          }))
        })
        .catch(() => {})
    }, 1200)
    taskPollers.set(convId, timer)
  }

  function stopTaskPolling(convId: string) {
    const timer = taskPollers.get(convId)
    if (timer !== undefined) {
      clearInterval(timer)
      taskPollers.delete(convId)
    }
  }

  /** 轮次结束后收尾：任务可能跨轮存续（如模型中途提问）需再取一次；实时步骤流清空。 */
  function refreshTaskOnce(convId: string) {
    set((s) => ({ liveActivity: { ...s.liveActivity, [convId]: null } }))
    getTaskStatus(convId)
      .then((res) => {
        set((s) => ({
          taskProgress: { ...s.taskProgress, [convId]: res.active ? (res.task ?? null) : null },
        }))
      })
      .catch(() => {})
  }

  /** 统一消费一次对话/审批响应：追加消息、维护审批挂起态与轮次计数。 */
  function applyChatResponse(convId: string, res: ChatResponse, turnIndex: number) {
    if (res.status === "pending_approval") {
      // 模型请求执行敏感操作：先展示工具调用消息，挂起等待用户决议；
      // 该轮尚未落盘，不推进 turnCount
      appendMessages(convId, mapResponseMessages(res.response ?? [], turnIndex))
      set((s) => ({
        pendingApprovals: { ...s.pendingApprovals, [convId]: res.pending_tools ?? [] },
        lastMetrics: {
          promptTokens: res.prompt_tokens,
          generationTokens: res.generation_tokens,
          generationTps: res.generation_tps,
        },
      }))
      return
    }

    if (res.status === "suspended") {
      // 任务已在安全点让出，转入可恢复态。已产出的局部消息展示为 warning；该轮尚未落盘，
      // 不推进 turnCount、不校准 history（正规去向是 resumeTask 续跑，或发新消息=放弃）。
      appendMessages(convId, mapResponseMessages(res.response ?? [], turnIndex, "warning"))
      set((s) => ({ suspendedSessions: { ...s.suspendedSessions, [convId]: true } }))
      return
    }

    if (res.status === "cancelled") {
      // 后端已整轮回滚（用户消息与中间产物均未进入模型记忆），仅存档取消记录
      appendMessages(convId, [
        {
          id: nextMessageId(),
          role: "system",
          content: currentDict().store.cancelled,
          variant: "warning",
        },
      ])
    } else if (res.status === "fail") {
      // response 里是含 Python traceback 的 system 消息，不直接展示原文
      const traceback = res.response?.find((m) => m.role === "system")
      console.error("模型引擎故障:", traceback && "content" in traceback ? traceback.content : res)
      appendMessages(convId, [
        {
          id: nextMessageId(),
          role: "system",
          content: currentDict().store.engineFail,
          variant: "error",
        },
      ])
    } else {
      const messages = mapResponseMessages(res.response ?? [], turnIndex)
      if (res.status === "warning" && !messages.some((m) => m.role === "assistant" && m.content)) {
        messages.push({
          id: nextMessageId(),
          role: "assistant",
          content: currentDict().store.warningNoConclusion,
          variant: "warning",
        })
      }
      appendMessages(convId, messages)
      patchConversation(convId, { turnCount: turnIndex + 1 })
      set({
        lastMetrics: {
          promptTokens: res.prompt_tokens,
          generationTokens: res.generation_tokens,
          generationTps: res.generation_tps,
        },
      })
    }

    // 后台校准服务端真实轮次数：fail 轮次同样会落盘占用节点下标，
    // 不校准会导致后续消息的 turnIndex 偏移、分支节点错位
    getHistory(convId)
      .then((h) => {
        if (typeof h.count === "number") patchConversation(convId, { turnCount: h.count })
      })
      .catch(() => {})
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

    selectConversation: (id) => set({ activeId: id }),

    newConversation: (workspaceId) => {
      const fresh = freshConversation(workspaceId)
      set((s) => ({
        conversations: [fresh, ...s.conversations],
        activeId: fresh.id,
      }))
    },

    openScheduleSession: (sessionId, title) => {
      const existing = get().conversations.find((c) => c.id === sessionId)
      if (existing) {
        set({ activeId: sessionId })
        return
      }
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
      controllers.get(id)?.abort()
      stopTaskPolling(id)
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
      if (!conv || conv.historyLoaded || get().generating[id]) return
      // 先置位，避免重复请求
      patchConversation(id, { historyLoaded: true })
      try {
        const history = await getHistory(id)
        const messages = (history.turns ?? []).flatMap((turn, index) =>
          turnToMessages(turn, index),
        )
        set((s) => ({
          conversations: s.conversations.map((c) =>
            // 加载期间用户可能已开始输入新消息，不覆盖非空会话
            c.id === id && c.messages.length === 0
              ? { ...c, messages, turnCount: history.count ?? 0 }
              : c,
          ),
        }))
      } catch (e) {
        console.warn("加载会话历史失败:", e)
        patchConversation(id, { historyLoaded: false })
      }
    },

    sendMessage: async (text, files) => {
      const convId = get().activeId
      const conv = get().conversations.find((c) => c.id === convId)
      if (!conv || get().generating[convId]) return

      // 镜像后端规则：挂起任务存在时发新消息 = 放弃该挂起任务（routes 已按 warning 收尾）。
      // 本地同步清掉可恢复态（App 层已就"放弃"二次确认）。后端句柄随本条消息回收，
      // 本地放弃标记一并清除（此后 /tasks 对账恢复权威）。
      locallyAbandonedSuspended.delete(convId)
      if (get().suspendedSessions[convId]) {
        set((s) => {
          const next = { ...s.suspendedSessions }
          delete next[convId]
          return { suspendedSessions: next }
        })
      }

      // 镜像后端规则：待审批中发新消息 = 放弃审批（后端把挂起的工具调用作废、该轮 warning 落盘）。
      // 不清的话审批横幅残留，用户点批准只会得到"没有等待审批的操作"。
      if (get().pendingApprovals[convId]) {
        set((s) => {
          const next = { ...s.pendingApprovals }
          delete next[convId]
          return { pendingApprovals: next }
        })
      }

      const attachments: MessageAttachment[] = files.map((f) => ({
        name: f.name,
        kind: f.type.startsWith("audio/")
          ? ("audio" as const)
          : f.type.startsWith("image/")
            ? ("image" as const)
            : ("file" as const), // PDF/文档/代码等：后端摄入层解析或懒登记
        previewUrl: f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined,
      }))

      const optimisticTurnIndex = conv.turnCount
      appendMessages(convId, [
        {
          id: nextMessageId(),
          role: "user",
          content: text,
          attachments: attachments.length > 0 ? attachments : undefined,
          turnIndex: optimisticTurnIndex,
        },
      ])
      if (conv.title === NEW_CHAT_TITLE) {
        patchConversation(convId, { title: text.slice(0, 30) || conv.title })
      }
      set((s) => ({ generating: { ...s.generating, [convId]: true } }))

      const controller = new AbortController()
      controllers.set(convId, controller)
      // 真流式退掉了 interval 轮询：任务进度/实时步骤改由 SSE 的 task_progress 事件推送；
      // /task 仅保留为轮末一次性 catch-up（refreshTaskOnce，同步跨轮任务态并清空实时步骤）。

      // 先放一个临时预览气泡，token 逐段填充；turn_end 到达后撤下、改用权威 response 对账
      const assistantId = nextMessageId()
      let streamed = ""
      let ended = false
      appendMessages(convId, [
        { id: assistantId, role: "assistant", content: "", streaming: true, turnIndex: optimisticTurnIndex },
      ])
      try {
        await sendChatStream(
          convId,
          {
            prompt: text,
            enable_thinking: get().thinkingEnabled,
            files: files.length > 0 ? files : undefined,
          },
          {
            onToken: (delta) => {
              streamed += delta
              updateMessage(convId, assistantId, { content: streamed })
            },
            onThinking: (text) => {
              // 思考块整块到达：挂到临时气泡的 thoughts（可折叠展示）
              updateMessage(convId, assistantId, { thoughts: text })
            },
            onTaskProgress: ({ task, activity }) => {
              // 推送替代轮询：直接刷新任务计划与实时步骤流
              set((s) => ({
                taskProgress: { ...s.taskProgress, [convId]: task },
                liveActivity: { ...s.liveActivity, [convId]: activity },
              }))
            },
            onTurnEnd: (res) => {
              // 流为预览、turn_end 为真：撤下临时气泡，用权威 response 对账（含所有终态）
              ended = true
              removeMessage(convId, assistantId)
              applyChatResponse(convId, res, optimisticTurnIndex)
            },
          },
          controller.signal,
        )
        // 流结束却没等到 turn_end（异常闭流/断线）：撤下占位，走 /task 断线补偿——
        // 后端可能已把本轮跑完并落盘，此时重载权威历史补回结果；否则如实提示仍在生成。
        if (!ended) {
          removeMessage(convId, assistantId)
          try {
            const st = await getTaskStatus(convId)
            if (!st.generating) {
              const h = await getHistory(convId)
              const msgs = (h.turns ?? []).flatMap((t, i) => turnToMessages(t, i))
              patchConversation(convId, {
                messages: msgs,
                turnCount: h.count ?? 0,
                historyLoaded: true,
              })
            } else {
              appendMessages(convId, [
                {
                  id: nextMessageId(),
                  role: "system",
                  content: currentDict().store.disconnectStillGenerating,
                  variant: "warning",
                },
              ])
            }
          } catch {
            appendMessages(convId, [
              {
                id: nextMessageId(),
                role: "system",
                content: currentDict().store.disconnectIncomplete,
                variant: "warning",
              },
            ])
          }
        }
      } catch (e) {
        removeMessage(convId, assistantId)
        if (e instanceof DOMException && e.name === "AbortError") {
          appendMessages(convId, [
            {
              id: nextMessageId(),
              role: "system",
              content: currentDict().store.stoppedWaiting,
              variant: "warning",
            },
          ])
        } else {
          console.error("请求模型服务失败:", e)
          appendMessages(convId, [
            {
              id: nextMessageId(),
              role: "system",
              content: currentDict().store.cannotConnect,
              variant: "error",
            },
          ])
        }
      } finally {
        controllers.delete(convId)
        stopTaskPolling(convId)
        refreshTaskOnce(convId)
        set((s) => ({ generating: { ...s.generating, [convId]: false } }))
      }
    },

    respondApproval: async (id, approve) => {
      const conv = get().conversations.find((c) => c.id === id)
      const tools = get().pendingApprovals[id]
      if (!conv || !tools || get().generating[id]) return

      set((s) => {
        const next = { ...s.pendingApprovals }
        delete next[id]
        return { pendingApprovals: next, generating: { ...s.generating, [id]: true } }
      })

      const controller = new AbortController()
      controllers.set(id, controller)
      startTaskPolling(id)
      try {
        // 阻塞式等待：批准后后端要执行工具并让模型收尾
        const res = await sendApproval(id, approve, controller.signal)
        applyChatResponse(id, res, conv.turnCount)
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") {
          appendMessages(id, [
            {
              id: nextMessageId(),
              role: "system",
              content: currentDict().store.approvalStoppedWaiting,
              variant: "warning",
            },
          ])
        } else {
          console.error("提交审批决议失败:", e)
          // 网络失败时决议未送达服务端：恢复挂起态，允许用户重试
          set((s) => ({ pendingApprovals: { ...s.pendingApprovals, [id]: tools } }))
          appendMessages(id, [
            {
              id: nextMessageId(),
              role: "system",
              content: currentDict().store.approvalCannotConnect,
              variant: "error",
            },
          ])
        }
      } finally {
        controllers.delete(id)
        stopTaskPolling(id)
        refreshTaskOnce(id)
        set((s) => ({ generating: { ...s.generating, [id]: false } }))
      }
    },

    abandonSuspended: (id) => {
      // 仅清本地可恢复态；后端的挂起轮会在下次发消息时被判为放弃并回收（routes 已处理）。
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
      // 优先真取消：后端在最近检查点停止生成并整轮回滚，流式 turn_end 会以
      // cancelled 状态正常返回；后端不可达或无在途轮次时退回本地中断等待
      cancelTask(id)
        .then((res) => {
          if (!res.status) controllers.get(id)?.abort()
        })
        .catch(() => controllers.get(id)?.abort())
    },

    suspendTask: (id) => {
      // 挂起（区别于取消）：后端在当前步骤完成后的安全点让出 worker，本轮以 suspended
      // 收尾。流式 turn_end 携 suspended 经 applyChatResponse 置入可恢复态，无需额外处理。
      apiSuspendTask(id).catch((e) => console.warn("挂起任务失败:", e))
    },

    resumeTask: async (id) => {
      const conv = get().conversations.find((c) => c.id === id)
      if (!conv || !get().suspendedSessions[id] || get().generating[id]) return
      // 进入续跑：清可恢复态、置 generating
      locallyAbandonedSuspended.delete(id)
      set((s) => {
        const next = { ...s.suspendedSessions }
        delete next[id]
        return { suspendedSessions: next, generating: { ...s.generating, [id]: true } }
      })
      const controller = new AbortController()
      controllers.set(id, controller)
      startTaskPolling(id)
      try {
        // 阻塞式续跑：从挂起处接着走，可能多步、耗时（与审批续跑同语义）
        const res = await apiResumeTask(id, controller.signal)
        applyChatResponse(id, res, conv.turnCount)
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") {
          appendMessages(id, [
            {
              id: nextMessageId(),
              role: "system",
              content: currentDict().store.resumeStoppedWaiting,
              variant: "warning",
            },
          ])
        } else {
          console.error("恢复任务失败:", e)
          // 网络失败：续跑未送达，回退到可恢复态允许重试
          set((s) => ({ suspendedSessions: { ...s.suspendedSessions, [id]: true } }))
          appendMessages(id, [
            {
              id: nextMessageId(),
              role: "system",
              content: currentDict().store.resumeCannotConnect,
              variant: "error",
            },
          ])
        }
      } finally {
        controllers.delete(id)
        stopTaskPolling(id)
        refreshTaskOnce(id)
        set((s) => ({ generating: { ...s.generating, [id]: false } }))
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
      ({ id, title, updatedAt, workspaceId, turnCount }) => ({
        id,
        title,
        updatedAt,
        workspaceId,
        turnCount,
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
