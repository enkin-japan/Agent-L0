// 事件归约器(PROTOCOL.md §5 的 web 端实现)。
// 纯函数:reduce(state, envelope) -> state,零依赖、不碰 zustand/DOM——
// 行为由 protocol/fixtures/ 夹具锁定,web 与原生 app 各自实现、共享同一批夹具。
// `_` 前缀字段是内部簿记(比对前由测试运行器剥除),其余形状与协议规范严格一致。

import type { PendingTool, TaskSnapshot } from "@/types/agent-api"
import type { EventEnvelope, TurnStateKind } from "@/types/protocol"

export interface NormalizedStep {
  kind: "tool" | "plan"
  label: string
  detail: string
  status: "running" | "ok" | "timeout" | "failed"
  /** 内部簿记:tool_started/result 的配对键(夹具比对时剥除)。 */
  _callId?: string
}

export interface NormalizedPreview {
  prompt: string
  text: string
  thinking: string
  steps: NormalizedStep[]
  task: TaskSnapshot | null
}

export interface NormalizedState {
  turn_state: TurnStateKind
  /** 已完结轮次的消息(turn_ended 对账并入:user 前置 + response 原样)。 */
  transcript: Array<Record<string, unknown>>
  preview: NormalizedPreview
  pending_tools: PendingTool[]
}

export function initialTurnState(): NormalizedState {
  return {
    turn_state: "none",
    transcript: [],
    preview: emptyPreview(),
    pending_tools: [],
  }
}

function emptyPreview(): NormalizedPreview {
  return { prompt: "", text: "", thinking: "", steps: [], task: null }
}

export function reduceTurnEvent(state: NormalizedState, env: EventEnvelope): NormalizedState {
  const d = env.data ?? {}
  switch (env.type) {
    case "turn_queued":
      // 新轮开始:预览整体重置(上一轮已以 turn_ended 收尾或被放弃收尾)
      return {
        ...state,
        turn_state: "queued",
        preview: { ...emptyPreview(), prompt: String(d.prompt ?? "") },
        pending_tools: [],
      }

    case "turn_started":
      return { ...state, turn_state: "running" }

    case "token_delta":
      return patchPreview(state, { text: state.preview.text + String(d.text ?? "") })

    case "thinking_delta":
      return patchPreview(state, { thinking: state.preview.thinking + String(d.text ?? "") })

    case "tool_started":
      return patchPreview(state, {
        steps: [...state.preview.steps, {
          kind: "tool", label: String(d.name ?? ""), detail: "",
          status: "running", _callId: asStr(d.call_id),
        }],
      })

    case "tool_progress": {
      // 执行中的进度:按 call_id 更新对应 running 步的 detail(无 call_id 则更新最后一个 running 步)
      const label = [d.label, d.detail].filter(Boolean).map(String).join(" · ")
      const callId = asStr(d.call_id)
      const steps = [...state.preview.steps]
      for (let i = steps.length - 1; i >= 0; i--) {
        const match = callId ? steps[i]._callId === callId : steps[i].status === "running"
        if (match && steps[i].status === "running") {
          steps[i] = { ...steps[i], detail: label }
          break
        }
      }
      return patchPreview(state, { steps })
    }

    case "tool_result": {
      const callId = asStr(d.call_id)
      const steps = state.preview.steps.map((s) =>
        s._callId !== undefined && s._callId === callId
          ? { ...s, detail: String(d.response ?? ""), status: statusOf(d.status) }
          : s,
      )
      return patchPreview(state, { steps })
    }

    case "plan_updated":
      return patchPreview(state, { task: (d.task ?? null) as TaskSnapshot | null })

    case "step_completed":
      return patchPreview(state, {
        steps: [...state.preview.steps, {
          kind: "plan", label: String(d.title ?? ""), detail: String(d.result ?? ""), status: "ok",
        }],
      })

    case "step_failed":
      return patchPreview(state, {
        steps: [...state.preview.steps, {
          kind: "plan", label: String(d.title ?? ""), detail: String(d.reason ?? ""), status: "failed",
        }],
      })

    case "awaiting_approval":
      return {
        ...state,
        turn_state: "awaiting_approval",
        pending_tools: (d.pending_tools ?? []) as PendingTool[],
      }

    case "suspended":
      return { ...state, turn_state: "suspended" }

    case "resumed":
      // 重新排队(随后有新的 turn_started);审批卡随决议收起
      return { ...state, turn_state: "queued", pending_tools: [] }

    case "turn_ended": {
      // 唯一权威终态:预览撤下,轮次消息(user 前置 + response 原样)并入 transcript
      const response = Array.isArray(d.response) ? (d.response as Array<Record<string, unknown>>) : []
      return {
        turn_state: "none",
        transcript: [
          ...state.transcript,
          { role: "user", content: String(d.prompt ?? "") },
          ...response,
        ],
        preview: emptyPreview(),
        pending_tools: [],
      }
    }

    default:
      return state // 未知事件类型:忽略(向前兼容,协议硬性要求)
  }
}

function patchPreview(state: NormalizedState, patch: Partial<NormalizedPreview>): NormalizedState {
  return { ...state, preview: { ...state.preview, ...patch } }
}

function statusOf(v: unknown): NormalizedStep["status"] {
  return v === "timeout" ? "timeout" : "ok"
}

function asStr(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined
}
