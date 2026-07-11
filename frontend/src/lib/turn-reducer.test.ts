// 协议夹具回放测试(PROTOCOL.md §5):读 protocol/fixtures/ 的事件流逐行归约,
// 断言终态 deep-equal expected.json。原生 app 的 XCTest 消费同一批夹具——
// 这里绿 + 那里绿 = 两端行为一致(TS 与 Swift 无法共享代码,共享行为标准)。
import { readdirSync, readFileSync } from "node:fs"
import { join } from "node:path"

import { describe, expect, it } from "vitest"

import type { EventEnvelope } from "@/types/protocol"
import { initialTurnState, reduceTurnEvent } from "./turn-reducer"

const FIXTURES_DIR = join(__dirname, "../../../protocol/fixtures")

/** 比对前剥除 `_` 前缀的内部簿记字段(协议允许归约器携带,夹具只锁公共形状)。 */
function stripInternal(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripInternal)
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (!k.startsWith("_")) out[k] = stripInternal(v)
    }
    return out
  }
  return value
}

const fixtureNames = readdirSync(FIXTURES_DIR)
  .filter((f) => f.endsWith(".events.jsonl"))
  .map((f) => f.replace(/\.events\.jsonl$/, ""))
  .sort()

describe("turn-reducer 协议夹具回放", () => {
  it("发现夹具", () => {
    expect(fixtureNames.length).toBeGreaterThan(0)
  })

  for (const name of fixtureNames) {
    it(name, () => {
      const events = readFileSync(join(FIXTURES_DIR, `${name}.events.jsonl`), "utf-8")
        .split("\n")
        .filter((l) => l.trim())
        .map((l) => JSON.parse(l) as EventEnvelope)
      const expected = JSON.parse(
        readFileSync(join(FIXTURES_DIR, `${name}.expected.json`), "utf-8"),
      )
      const finalState = events.reduce(reduceTurnEvent, initialTurnState())
      expect(stripInternal(finalState)).toEqual(expected)
    })
  }
})
