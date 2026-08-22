/**
 * 全局 feed 的代际围栏（B2，PROTOCOL §2.7）。
 *
 * feed_seq 纯内存、后端每次重启归零，而客户端游标跨重连保留——不重锚的话，重启后的
 * 新事件全部撞上「feed_seq <= 游标」被两侧静默丢弃，依赖 feed 逐条触发的通知真丢
 * （任务中心状态靠 30s 慢对账兜底，但事件本身不补发）。
 * 语义：`stream_attached.data.generation` 变化 = 游标作废，清零重连（重启后环里只有
 * 新代事件，after=0 重放不会重复通知，反而恰好把崩溃恢复的补发事件送到）。
 * 会话事件流**不适用**本围栏：其 seq 因服务端水位余量跨重启单调，旧游标仍有效。
 */
export interface FeedFenceState {
  generation: string | null
}

export type FeedFenceVerdict = "continue" | "reanchor"

export function feedFence(state: FeedFenceState, frameGeneration: unknown): FeedFenceVerdict {
  const gen =
    typeof frameGeneration === "string" && frameGeneration.length > 0 ? frameGeneration : null
  if (gen === null) return "continue" // 旧后端无字段：维持现状（不假装看得见代际）
  if (state.generation !== null && state.generation !== gen) {
    state.generation = gen
    return "reanchor"
  }
  state.generation = gen // 首帧：认领代际
  return "continue"
}
