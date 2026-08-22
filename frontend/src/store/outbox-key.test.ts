import { afterEach, beforeEach, describe, expect, it } from "vitest"

/**
 * B3 提交幂等键的客户端半边：同会话同文重发复用旧键（服务端据此判重不二次执行），
 * 换文换键、清仓换键。outbox 落 localStorage——node 环境先垫一个最小实现。
 */
const store = new Map<string, string>()
const shim = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
  key: () => null,
  length: 0,
} as Storage
beforeEach(() => {
  store.clear()
  globalThis.localStorage = shim
})
afterEach(() => {
  // @ts-expect-error 撤垫片,不让它泄漏进别的测试文件
  delete globalThis.localStorage
})

async function load() {
  // 动态 import：确保垫片先就位（模块只在函数体内碰 localStorage,静态导入也安全,
  // 但动态更能表达依赖次序）
  return await import("./useChatStore")
}

describe("outboxKeyFor", () => {
  it("同会话同文重发复用旧键", async () => {
    const { outboxKeyFor } = await load()
    const k1 = outboxKeyFor("s1", "你好")
    store.set("macagent-outbox-v1", JSON.stringify({ s1: { text: "你好", ts: 1, key: k1 } }))
    expect(outboxKeyFor("s1", "你好")).toBe(k1)
  })

  it("换了内容就换键", async () => {
    const { outboxKeyFor } = await load()
    const k1 = outboxKeyFor("s1", "你好")
    store.set("macagent-outbox-v1", JSON.stringify({ s1: { text: "你好", ts: 1, key: k1 } }))
    expect(outboxKeyFor("s1", "别的话")).not.toBe(k1)
  })

  it("outbox 已清(202 受理/turn_ended 对账后)→ 新键", async () => {
    const { outboxKeyFor } = await load()
    const k1 = outboxKeyFor("s1", "你好")
    store.set("macagent-outbox-v1", JSON.stringify({ s1: { text: "你好", ts: 1, key: k1 } }))
    expect(outboxKeyFor("s1", "你好")).toBe(k1)     // 阳性对照:清仓前确实会复用
    store.delete("macagent-outbox-v1")              // 模拟 202/turn_ended 清仓
    expect(outboxKeyFor("s1", "你好")).not.toBe(k1)
  })

  it("旧格式条目(无 key 字段)不会被误用", async () => {
    const { outboxKeyFor } = await load()
    store.set("macagent-outbox-v1", JSON.stringify({ s1: { text: "你好", ts: 1 } }))
    const k = outboxKeyFor("s1", "你好")
    expect(typeof k).toBe("string")
    expect(k.length).toBeGreaterThan(10)
  })
})
