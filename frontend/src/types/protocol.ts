// 事件账本协议 v1 的类型(根 PROTOCOL.md §2)。与 Swift 端 Models 同源于协议文档,
// 行为一致性由 protocol/fixtures/ 夹具锁定(见 lib/turn-reducer.test.ts)。

import type { PendingTool, TaskSnapshot } from "@/types/agent-api"

/** 事件信封:seq 会话内单调递增(重连游标),data 形状随 type。 */
export interface EventEnvelope {
  seq: number
  turn_id: string | null
  ts: number
  type: string
  /** 事件负载:字段随 type,消费方按需窄化;未知类型/字段一律忽略(向前兼容)。 */
  data: Record<string, unknown>
}

export type TurnStateKind = "none" | "queued" | "running" | "awaiting_approval" | "suspended"

/** GET /chat/{sid}/turn 的 turn 字段(进行中轮次的可恢复状态)。 */
export interface TurnStateSnapshot {
  turn_id: string
  state: Exclude<TurnStateKind, "none">
  since: number
  queue_position?: number
  prompt: string
  partial_text: string
  partial_thinking: string
  pending_tools: PendingTool[]
  task: TaskSnapshot | null
  first_seq: number
  last_seq: number
}

export interface TurnStateResponse {
  status: boolean
  turn: TurnStateSnapshot | null
  last_seq: number
}

/** POST /turns(/approval|/resume) 的 202 受理回执。 */
export interface TurnAccepted {
  status: boolean
  turn_id: string
  seq?: number
  /** B3 幂等键命中：202 返回的是既有轮（可能早已完结）,客户端须重新对账而非等事件。 */
  duplicate?: boolean
}
