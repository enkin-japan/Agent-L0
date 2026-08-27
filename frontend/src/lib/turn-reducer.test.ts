// 协议夹具回放测试(PROTOCOL.md §5):读 protocol/fixtures/ 的事件流逐行归约,
// 断言终态 deep-equal expected.json。原生 app 的 XCTest 消费同一批夹具——
// 这里绿 + 那里绿 = 两端行为一致(TS 与 Swift 无法共享代码,共享行为标准)。
import { readdirSync, readFileSync, writeFileSync } from "node:fs"
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

  it("EMIT_FIXTURE_EXPECTED 指向真实夹具", () => {
    // 复审 S10:环境变量拼错/指向无 events 的名字时,整套绿灯 + 什么都没写 + 零诊断
    // ——收据机制唯一能哑火的方式。大小写也须精确(复审 N20:本机文件系统大小写不敏感)。
    const emit = process.env.EMIT_FIXTURE_EXPECTED
    if (emit) expect(fixtureNames).toContain(emit)
  })

  for (const name of fixtureNames) {
    it(name, () => {
      const events = readFileSync(join(FIXTURES_DIR, `${name}.events.jsonl`), "utf-8")
        .split("\n")
        .filter((l) => l.trim())
        .map((l) => JSON.parse(l) as EventEnvelope)
      const finalState = events.reduce(reduceTurnEvent, initialTurnState())
      // 夹具录制器的 refresh 半边(record_fixture.py 流程第 2 步):期望终态由归约器
      // 重生成、人审 diff 后入库。生成**必红**(确认收据)——挂着环境变量跑=门被关掉。
      if (process.env.EMIT_FIXTURE_EXPECTED === name) {
        writeFileSync(
          join(FIXTURES_DIR, `${name}.expected.json`),
          JSON.stringify(stripInternal(finalState), null, 2) + "\n",
        )
        throw new Error(
          `${name}.expected.json 已生成(这不是事故,是确认收据):人工审「应该的终态」`
          + `而非「当前的终态」,再不带 EMIT_FIXTURE_EXPECTED 双端回放收绿。`,
        )
      }
      const expected = JSON.parse(
        readFileSync(join(FIXTURES_DIR, `${name}.expected.json`), "utf-8"),
      )
      expect(stripInternal(finalState)).toEqual(expected)
    })
  }
})
