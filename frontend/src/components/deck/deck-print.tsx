"use client"

import { useEffect, useState } from "react"

import { getDeck } from "@/api/agent"
import type { DeckSpec } from "@/types/deck"
import { DESIGN_H, DESIGN_W, SlideCanvas } from "./slide-renderer"

/**
 * 打印视图（/print/deck?session=xxx）：PDF 导出的渲染载体。
 * 后端 deck_export_pdf 用无头 Chrome（Playwright）加载本页并 page.pdf()——
 * 渲染器与预览完全同一份（SlideCanvas），保证「预览即所得」。
 * 每页一张 1280×720 幻灯片，@page 尺寸与之相同 → PDF 无边距整页出片。
 * 本页不进聊天 store，不触发会话副作用。
 */
export function DeckPrint() {
  const [deck, setDeck] = useState<DeckSpec | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sessionId = new URLSearchParams(window.location.search).get("session") ?? ""

  useEffect(() => {
    if (!sessionId) return
    getDeck(sessionId)
      .then((d) => {
        if (!d) setError("no deck")
        else {
          setDeck(d)
          document.title = d.meta.title
        }
      })
      .catch((e) => setError(String(e)))
  }, [sessionId])

  const displayError = sessionId ? error : "missing ?session="
  if (displayError) return <div id="deck-print-error">{displayError}</div>
  if (!deck) return <div id="deck-print-loading">loading…</div>

  return (
    <div>
      <style>{`
        @page { size: ${DESIGN_W}px ${DESIGN_H}px; margin: 0; }
        html, body { margin: 0; padding: 0; }
        .deck-print-page { width: ${DESIGN_W}px; height: ${DESIGN_H}px; overflow: hidden; break-after: page; }
      `}</style>
      {deck.slides.map((slide) => (
        <div key={slide.id} className="deck-print-page">
          <SlideCanvas slide={slide} meta={deck.meta} sessionId={sessionId} />
        </div>
      ))}
      {/* 无头导出的完成信标：Playwright 等待该节点出现即可开始打印 */}
      <div id="deck-print-ready" style={{ display: "none" }} data-slides={deck.slides.length} />
    </div>
  )
}
