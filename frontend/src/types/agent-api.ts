// Agent API 类型定义（与 API.md §6 保持一致）

export interface ToolCall {
  type: "function" | "error"
  name?: string
  arguments?: Record<string, unknown>
  raw_input?: string
  error_msg?: string
}

export type ResponseMessage =
  | { role: "assistant"; content: string; thoughts?: string }
  | { role: "assistant"; tool_calls: ToolCall[]; thoughts?: string }
  | { role: "tool"; tool_responses: { name: string; response: string }[] }
  | { role: "system"; content: string }

/** 等待用户授权的敏感工具调用（status === "pending_approval" 时返回）。 */
export interface PendingTool {
  name: string
  arguments?: Record<string, unknown>
}

export interface ChatResponse {
  status: "success" | "fail" | "warning" | "pending_approval" | "cancelled" | "suspended"
  prompt: string
  image: string[]
  audio: string[]
  /** 摄入解析层产物：解析/转写文本（带 provenance 标记，已注入上下文）。 */
  ingested_text?: string
  /** 摄入层登记的原始附件（PDF/文档/代码等）：{path,type,name}。 */
  attachments?: Array<{ path?: string; type?: string; name?: string }> | null
  enable_thinking: boolean
  max_tokens: number
  temperature: number
  min_p: number
  response: ResponseMessage[]
  pending_tools?: PendingTool[] | null
  prompt_tokens?: number
  generation_tokens?: number
  prompt_tps?: number
  generation_tps?: number
  peak_memory?: number
}

export interface DeleteSessionResponse {
  status: boolean
  frozen: boolean
  response: string
}

export interface DeleteTurnResponse {
  status: boolean
  session_id: string
  /** 回退后剩余的历史轮次数 */
  remaining_turns?: number | null
  response: string
}

/** 后端会话索引条目（反向映射：磁盘真相，用于对账 localStorage 与展示可删会话）。 */
export interface SessionMetaItem {
  session_id: string
  title: string
  turn_count: number
  created_at?: number | null
  last_updated?: number | null
  pinned: boolean
  parent_session_id?: string | null
  active: boolean
}

export interface SessionListResponse {
  status: boolean
  count: number
  sessions: SessionMetaItem[]
}

export interface PinResponse {
  status: boolean
  session_id: string
  pinned: boolean
  response: string
}

/** 进行中轮次预写标记：轮首落盘、轮末清除；存在即代表有一轮尚未落进 turns。 */

export interface HistoryResponse {
  status: boolean
  session_id: string
  count: number
  turns: ChatResponse[]
  /** 独立于 turns（不参与分支下标）；无进行中轮次时为 null。 */
}

export interface BranchRequest {
  node_index: number
  new_session_id?: string
}

export interface BranchResponse {
  status: boolean
  session_id?: string
  parent_session_id?: string
  branch_point?: number
  turns?: number
  kv_cache_reused?: boolean
  response: string
}

/** 已注册工具的明细（GET /api/v1/tools）。 */
export interface ToolInfo {
  name: string
  description: string
  parameters: {
    properties: Record<string, { type?: string; description?: string }>
    required: string[]
  }
  /** 敏感操作：执行前会挂起等待用户审批 */
  requires_approval: boolean
}

export interface ToolsResponse {
  status: boolean
  count: number
  tools: ToolInfo[]
}

export interface SystemPromptResponse {
  status: boolean
  session_id: string
  system_prompt: string
  /** "session"：该会话在用的提示词；"default"：会话尚未创建，返回默认值 */
  source: "session" | "default"
}

/** 多步任务计划中的一步（TaskState 快照）。 */
export interface TaskStep {
  id: number
  title: string
  status: "pending" | "doing" | "done" | "failed" | "skipped"
  note?: string
}

/** 多步任务快照（GET /task，生成期间轮询展示进度）。 */
export interface TaskSnapshot {
  goal: string
  plan: TaskStep[]
  replan_count: number
  step_iterations: number
}

/** 实时步骤流中的一条已完成步骤。 */
export interface ActivityStep {
  kind: "tool" | "plan" | "note" | string
  label: string
  detail?: string
}

/** 实时步骤流快照（轮询 /task 获取，把黑盒等待变成看得见的动作）。 */
export interface LiveActivity {
  /** running=推理/执行中；awaiting_approval=等待授权；done=已完成；idle=空闲 */
  phase: "idle" | "running" | "awaiting_approval" | "done" | string
  /** 当前正在做的一句话（"思考中…" / "调用工具：run_shell_command"） */
  current: string
  steps: ActivityStep[]
}

export interface TaskStatusResponse {
  status: boolean
  session_id: string
  /** 是否存在进行中的多步任务（有非空计划） */
  active: boolean
  /** 该会话当前是否有轮次在推理中 */
  generating: boolean
  task?: TaskSnapshot | null
  activity?: LiveActivity | null
}

/** 服务端保存的前端 UI 状态（GET/PUT /api/v1/ui-state）。state 为前端自管的 JSON blob。 */
export interface UiStateResponse {
  status: boolean
  state: Record<string, unknown> | null
  updated_at: number | null
}

export interface CancelResponse {
  status: boolean
  session_id: string
  response: string
}

/** 全局任务视图里的一个任务句柄（GET /tasks 的 tasks[] 项）。 */
export interface TaskHandleInfo {
  session_id: string
  /** running=占用 worker；queued=排队；suspended=已挂起（KV 留内存待恢复）。 */
  status: "running" | "queued" | "suspended" | string
  /** chat=普通对话轮；resume=恢复续跑；summary=后台摘要压缩任务。 */
  kind: string
  priority?: number
  /** 排队等待时长（秒）；仅 queued 有意义。 */
  waiting_seconds: number
  /** 已运行时长（秒）；仅 running 有意义。 */
  running_seconds: number
}

/** 全局单 worker 调度快照（GET /api/v1/tasks，跨会话）。 */
export interface TaskListResponse {
  status: boolean
  /** 当前占用 worker 的 session_id；空闲则 null。 */
  running: string | null
  queued: number
  suspended: number
  tasks: TaskHandleInfo[]
}

/** 定时任务的调度规则（结构化四型，非 cron）。 */
export interface ScheduleSpec {
  type: "interval" | "daily" | "weekly" | "once" | string
  /** interval：每 N 分钟。 */
  minutes?: number
  /** daily/weekly："HH:MM"；once："YYYY-MM-DD HH:MM"。 */
  at?: string
  /** weekly：0=周一 .. 6=周日。 */
  weekday?: number
}

/** 定时任务的一次运行记录（runs.jsonl 一行）。 */
export interface ScheduleRun {
  run_id: string
  scheduled_for: number
  started_at: number
  finished_at: number | null
  /** success / fail / cancelled / warning / denied_approval（无人值守撞了审批墙）。 */
  status: string
  /** 最终答复首部摘要。 */
  summary: string
  /** 专属会话 id（schedule_{id}），点击可打开查看运行全文。 */
  session_id: string
}

/** 定时任务实体（GET /api/v1/schedules 的 schedules[] 项）。 */
export interface ScheduledTask {
  id: string
  name: string
  prompt: string
  spec: ScheduleSpec
  enabled: boolean
  created_at: number
  updated_at: number
  created_by_session: string
  /** 下次触发时刻（epoch 秒）；once 已触发/停用后为 null。 */
  next_run: number | null
  /** 是否正在执行（含排队/挂起等待续跑）。 */
  running: boolean
  /** 最近一次运行记录；从未运行过则 null。 */
  last_run: ScheduleRun | null
}

export interface ScheduleListResponse {
  status: boolean
  count: number
  schedules: ScheduledTask[]
}

export interface ScheduleActionResponse {
  status: boolean
  schedule_id: string
  response: string
  schedule: ScheduledTask | null
}

export interface ScheduleRunsResponse {
  status: boolean
  schedule_id: string
  count: number
  runs: ScheduleRun[]
}

/** 会话记忆/上下文压缩状态（GET /api/v1/chat/{sid}/memory，只读）。 */
export interface MemoryStatusResponse {
  status: boolean
  session_id: string
  /** 会话是否在内存（热）；冷会话为 last saved 快照。 */
  loaded: boolean
  /** 当前 model-facing 上下文规模（prompt token）。 */
  last_prompt_tokens: number
  /** 全量落盘历史轮次。 */
  total_turns: number
  /** 已并入滚动摘要的前导历史水位线。 */
  summarized_turns: number
  has_summary: boolean
  summary_chars: number
  /** 活跃期折叠阈值（prompt token 越过即折叠旧轮）。 */
  high_threshold: number
  low_threshold: number
  keep_tail_turns: number
  /** 被折叠原文是否沉淀进 RAG（可 rag_search 召回）。 */
  sediment_to_rag: boolean
}

export interface ChatRequestParams {
  prompt: string
  system?: string
  enable_thinking?: boolean
  thinking_budget?: number
  max_tokens?: number
  temperature?: number
  min_p?: number
  files?: File[]
}
