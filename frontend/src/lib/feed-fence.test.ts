import { describe, expect, it } from "vitest"

import { feedFence, type FeedFenceState } from "./feed-fence"

/**
 * B2 代际围栏（store 层契约，不在 protocol/fixtures 覆盖面内——夹具只喂纯归约器，
 * 而两端 dispatch 对 stream_attached 首行 return，游标语义全在 store 层）。
 */
describe("feedFence", () => {
  it("首帧认领代际,同代际重连不重锚", () => {
    const st: FeedFenceState = { generation: null }
    expect(feedFence(st, "boot-a")).toBe("continue")
    expect(st.generation).toBe("boot-a")
    expect(feedFence(st, "boot-a")).toBe("continue") // 断线重连,同代际
  })

  it("代际变化 = 游标作废,要求重锚", () => {
    const st: FeedFenceState = { generation: "boot-a" }
    expect(feedFence(st, "boot-b")).toBe("reanchor")
    expect(st.generation).toBe("boot-b")
    // 重锚后的下一帧(新连接首帧)是同代际,不再重锚——不许无限重连
    expect(feedFence(st, "boot-b")).toBe("continue")
  })

  it("旧后端无字段:维持现状,不假装看得见代际", () => {
    const st: FeedFenceState = { generation: null }
    expect(feedFence(st, undefined)).toBe("continue")
    expect(st.generation).toBeNull()
    // 已认领代际后字段消失(降级/代理剥字段):同样不重锚
    st.generation = "boot-a"
    expect(feedFence(st, undefined)).toBe("continue")
    expect(feedFence(st, "")).toBe("continue")
    expect(st.generation).toBe("boot-a")
  })

  it("非字符串垃圾不接受", () => {
    const st: FeedFenceState = { generation: "boot-a" }
    expect(feedFence(st, 42)).toBe("continue")
    expect(feedFence(st, { v: 1 })).toBe("continue")
    expect(st.generation).toBe("boot-a")
  })
})
