// SplitStack Agent API 封装（见 API.md §3 / §7）
// 注意：POST /chat 是同步阻塞接口，耗时 = 模型完整生成时间，不要加短超时。

import type {
  BranchRequest,
  BranchResponse,
  CancelResponse,
  ChatRequestParams,
  ChatResponse,
  DeleteSessionResponse,
  DeleteTurnResponse,
  HistoryResponse,
  SessionListResponse,
  PinResponse,
  LiveActivity,
  MemoryStatusResponse,
  ScheduleActionResponse,
  ScheduleListResponse,
  ScheduleRunsResponse,
  SystemPromptResponse,
  TaskListResponse,
  TaskSnapshot,
  TaskStatusResponse,
  ToolsResponse,
  UiStateResponse,
} from "@/types/agent-api"
import type { DeckSpec } from "@/types/deck"
import type { EventEnvelope, TurnAccepted, TurnStateResponse } from "@/types/protocol"
import { parseSSE } from "./sse"

// 默认同源相对路径：请求由 Vite dev server 的 /api 代理转发到本机 Agent（127.0.0.1:14159），
// 前端对 Tailscale 内网开放时，其他设备无需直连 Agent 端口。
// 鉴权：Authorization Bearer 由代理（dev: Vite / prod: Nginx）注入，前端不持有 key。
const BASE_URL: string = import.meta.env.VITE_AGENT_API_BASE ?? ""

/** 组装对话请求的 multipart 表单（sendChat 与 sendChatStream 共用）。 */
function buildChatForm(params: ChatRequestParams): FormData {
  const form = new FormData()
  form.append("prompt", params.prompt)
  if (params.system) form.append("system", params.system)
  if (params.enable_thinking !== undefined)
    form.append("enable_thinking", String(params.enable_thinking))
  if (params.thinking_budget !== undefined)
    form.append("thinking_budget", String(params.thinking_budget))
  if (params.max_tokens !== undefined)
    form.append("max_tokens", String(params.max_tokens))
  if (params.temperature !== undefined)
    form.append("temperature", String(params.temperature))
  if (params.min_p !== undefined) form.append("min_p", String(params.min_p))
  params.files?.forEach((f) => form.append("files", f))
  return form
}

// ---------- 事件账本协议 v1（PROTOCOL.md §2.4~§2.6）:轮次是可寻址资源 ----------

/** 受理冲突（409）：会话已有排队/运行中的轮次,或决议/恢复的目标态不存在。 */
export class TurnConflictError extends Error {
  reason: string
  constructor(reason: string) {
    super(reason)
    this.reason = reason
  }
}

async function acceptedOrConflict(res: Response): Promise<TurnAccepted> {
  if (res.status === 409) {
    const body = (await res.json().catch(() => ({}))) as { reason?: string }
    throw new TurnConflictError(body.reason ?? "conflict")
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 提交新轮次：受理即返（202 {turn_id, seq}），执行在后台,过程经 attachEvents 观察。 */
export async function submitTurn(
  sessionId: string,
  params: ChatRequestParams,
): Promise<TurnAccepted> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/turns`, {
    method: "POST",
    body: buildChatForm(params),
  })
  return acceptedOrConflict(res)
}

/** 审批决议（202 受理即返）：结果经事件流回来（resumed → … → turn_ended）。 */
export async function submitApproval(sessionId: string, approve: boolean): Promise<TurnAccepted> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/turns/approval`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approve }),
  })
  return acceptedOrConflict(res)
}

/** 恢复挂起轮次（202 受理即返）：结果经事件流回来。 */
export async function submitResume(sessionId: string): Promise<TurnAccepted> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/turns/resume`, {
    method: "POST",
  })
  return acceptedOrConflict(res)
}

/** 全局任务 feed 信封（PROTOCOL.md §2.7）：跨会话生命周期事件 + 独立 feed_seq 游标。 */
export interface FeedEnvelope {
  feed_seq: number
  session_id?: string
  turn_id?: string | null
  ts?: number
  type: string
  data?: Record<string, unknown>
}

/** 附着全局任务 feed（SSE）：重放 feed_seq > after 后转实时续播。断开由调用方带游标重连。 */
export async function* attachGlobalFeed(
  after: number,
  signal?: AbortSignal,
): AsyncGenerator<FeedEnvelope> {
  const res = await fetch(`${BASE_URL}/api/v1/events?after=${after}`, {
    headers: { Accept: "text/event-stream" },
    signal,
  })
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
  for await (const frame of parseSSE(res.body)) {
    yield JSON.parse(frame.data) as FeedEnvelope
  }
}

/** 当前轮次的可恢复状态（无活跃轮次时 turn 为 null）+ 账本游标 last_seq。 */
export async function getTurnState(sessionId: string): Promise<TurnStateResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/turn`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/**
 * 附着事件流（SSE）：重放 seq > after 的事件后转实时续播,长连贯穿多个轮次。
 * 逐帧产出事件信封;流被服务端正常收尾(会话删除)时自然结束,网络断开抛错,
 * 由调用方带游标重连(断开是常态,不是异常)。首帧 stream_attached(seq=0)一并产出。
 */
export async function* attachEvents(
  sessionId: string,
  after: number,
  signal?: AbortSignal,
): AsyncGenerator<EventEnvelope> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/events?after=${after}`, {
    headers: { Accept: "text/event-stream" },
    signal,
  })
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
  for await (const frame of parseSSE(res.body)) {
    yield JSON.parse(frame.data) as EventEnvelope
  }
}

export async function deleteSession(
  sessionId: string,
): Promise<DeleteSessionResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}`, {
    method: "DELETE",
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 删除会话最后一轮问答：后端同步回退内存记忆、KV Cache 与落盘日志。 */
export async function deleteLastTurn(
  sessionId: string,
): Promise<DeleteTurnResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/turns/last`, {
    method: "DELETE",
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function getHistory(sessionId: string): Promise<HistoryResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/history`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 会话索引（后端磁盘真相）：反向映射，供前端对账 localStorage、展示可筛选删除的会话。 */
export async function listSessions(): Promise<SessionListResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/sessions`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 置/撤会话 pin：pin 的会话不被后台自动清扫回收（手动「保留」）。 */
export async function setSessionPin(
  sessionId: string,
  pinned: boolean,
): Promise<PinResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/pin`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pinned }),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 模型当前可注册的工具清单（服务启动时探活确定，运行期间不变）。 */
export async function getTools(): Promise<ToolsResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/tools`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 某会话的系统提示词（只读查询，不会触发服务端建会话）。 */
export async function getSystemPrompt(
  sessionId: string,
): Promise<SystemPromptResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/system`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 全局单 worker 调度快照（跨会话）：谁在跑 / 排队 / 挂起。只读，不触发任何调度动作。 */
export async function getTasks(): Promise<TaskListResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/tasks`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 会话记忆/上下文压缩状态（只读，不触发会话创建/恢复）。供「记忆」面板可视化有界记忆。 */
export async function getMemoryStatus(
  sessionId: string,
): Promise<MemoryStatusResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/memory`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 多步任务进度快照（只读轮询，不触发会话创建）。 */
export async function getTaskStatus(
  sessionId: string,
  signal?: AbortSignal,
): Promise<TaskStatusResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/task`, { signal })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 真取消：后端在最近的检查点停止生成，整轮回滚（区别于仅中断前端等待）。 */
export async function cancelTask(sessionId: string): Promise<CancelResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/task/cancel`, {
    method: "POST",
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 挂起当前运行中的任务（区别于取消）：后端在当前步骤完成后的安全点让出 worker，
 *  KV 与任务状态常驻内存待 resumeTask 续跑。仅对"正在占用 worker"的任务有效。 */
export async function suspendTask(sessionId: string): Promise<CancelResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/task/suspend`, {
    method: "POST",
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function branchSession(
  sessionId: string,
  body: BranchRequest,
): Promise<BranchResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/${sessionId}/branch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

// ---------- 定时任务（schedule）：创建/编辑走对话（schedule_* 工具 + 审批卡片），
// 这里只有不经过模型的直接控制：列表 / 启停 / 立即运行 / 中止本次运行 / 删除 / 历史 ----------

/** 定时任务清单（定义 + 是否在运行 + 最近一次运行摘要）。侧栏低频轮询。 */
export async function listSchedules(): Promise<ScheduleListResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/schedules`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 启停定时任务。重新启用时下次触发从当前时刻重算（不追补停用期间的触发）。 */
export async function toggleSchedule(id: string, enabled: boolean): Promise<ScheduleActionResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/schedules/${id}/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 立即运行一次（不影响既定节奏）；已在运行则后端拒绝（status=false）。 */
export async function runScheduleNow(id: string): Promise<ScheduleActionResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/schedules/${id}/run`, { method: "POST" })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 中止本次运行（区别于停用任务）：后端在最近检查点停止并整轮回滚。 */
export async function cancelScheduleRun(id: string): Promise<ScheduleActionResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/schedules/${id}/cancel`, { method: "POST" })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 删除定时任务（定义 + 运行历史；专属会话保留在聊天记录里）。 */
export async function deleteSchedule(id: string): Promise<ScheduleActionResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/schedules/${id}`, { method: "DELETE" })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 运行历史（新在前），每条含指向专属会话的 session_id。 */
export async function getScheduleRuns(id: string, limit = 20): Promise<ScheduleRunsResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/schedules/${id}/runs?limit=${limit}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 读取服务端保存的 UI 状态（工作区/会话元数据）；从未写入过则 state 为 null。 */
export async function getUiState(): Promise<UiStateResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/ui-state`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 整体覆盖写入 UI 状态（last-write-wins；服务端原子落盘）。 */
export async function putUiState(state: Record<string, unknown>): Promise<UiStateResponse> {
  const res = await fetch(`${BASE_URL}/api/v1/ui-state`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state }),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/** 读取会话 Deck Spec（PPT 单一真相的投影）；该会话没有 deck 时返回 null。 */
export async function getDeck(sessionId: string): Promise<DeckSpec | null> {
  const res = await fetch(`${BASE_URL}/api/v1/files/${sessionId}/deck`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const data = (await res.json()) as { status: boolean; deck: DeckSpec }
  return data.deck ?? null
}
