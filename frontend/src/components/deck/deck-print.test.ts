import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { afterEach, describe, expect, it, vi } from "vitest"

import { DeckPrint } from "./deck-print"

afterEach(() => vi.unstubAllGlobals())

describe("DeckPrint 初始投影", () => {
  it.each(["", "?session="])("缺少会话时直接呈现打印错误：%s", (search) => {
    vi.stubGlobal("window", { location: { search } })

    const html = renderToStaticMarkup(createElement(DeckPrint))

    expect(html).toBe('<div id="deck-print-error">missing ?session=</div>')
  })

  it("有效会话在异步 deck 返回前保留 loading 信标", () => {
    vi.stubGlobal("window", { location: { search: "?session=synthetic-deck" } })

    const html = renderToStaticMarkup(createElement(DeckPrint))

    expect(html).toBe('<div id="deck-print-loading">loading…</div>')
  })
})
