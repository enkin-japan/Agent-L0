"use client"

import { useCallback, useEffect, useState } from "react"
import { ChevronLeft, ChevronRight, Lock, X } from "lucide-react"

import { getDeck } from "@/api/agent"
import { useChatStore } from "@/store/useChatStore"
import { useT } from "@/store/useUiStore"
import type { DeckSpec } from "@/types/deck"
import { ScaledSlide } from "./slide-renderer"

/**
 * Deck 实时预览（检视面板 Deck 页签）：
 * 单一真相是后端 deck.json，本组件只是投影——Agent 每改一页（deck_set_slide 落盘）
 * 这里就重取重渲。触发时机：liveActivity 变化（工具事件流）+ 生成结束沿 + 会话切换，
 * 统一 400ms 去抖，避免连续工具调用时抖动。
 */
export function DeckPreview({ sessionId }: { sessionId: string }) {
  const t = useT()
  const [state, setState] = useState<{ id: string; deck: DeckSpec | null; loaded: boolean }>({
    id: sessionId,
    deck: null,
    loaded: false,
  })
  const [openIdx, setOpenIdx] = useState<number | null>(null)
  const activity = useChatStore((s) => s.liveActivity[sessionId])
  const generating = useChatStore((s) => !!s.generating[sessionId])

  const deck = state.id === sessionId ? state.deck : null
  const loaded = state.id === sessionId && state.loaded

  const refetch = useCallback(() => {
    let cancelled = false
    getDeck(sessionId)
      .then((d) => { if (!cancelled) setState({ id: sessionId, deck: d, loaded: true }) })
      .catch(() => { if (!cancelled) setState({ id: sessionId, deck: null, loaded: true }) })
    return () => { cancelled = true }
  }, [sessionId])

  useEffect(() => {
    const timer = setTimeout(refetch, 400)
    return () => clearTimeout(timer)
  }, [refetch, activity, generating])

  // 放大浏览的键盘导航
  useEffect(() => {
    if (openIdx === null || !deck) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpenIdx(null)
      if (e.key === "ArrowRight") setOpenIdx((i) => Math.min((i ?? 0) + 1, deck.slides.length - 1))
      if (e.key === "ArrowLeft") setOpenIdx((i) => Math.max((i ?? 0) - 1, 0))
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [openIdx, deck])

  if (!loaded) {
    return <p className="rounded-xl border border-border bg-muted/30 px-3 py-2.5 text-xs text-muted-foreground">{t.inspector.loading}</p>
  }
  if (!deck) {
    return <p className="rounded-xl border border-border bg-muted/30 px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">{t.inspector.deckNone}</p>
  }

  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="text-sm font-semibold leading-snug">{deck.meta.title}</p>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          {t.inspector.deckMeta(deck.slides.length, deck.meta.theme)}
        </p>
      </div>

      <div className="flex flex-col gap-2.5">
        {deck.slides.map((slide, i) => (
          <div key={slide.id} className="group">
            <div className="mb-1 flex items-center gap-1.5 text-[10px] text-muted-foreground/70">
              <span className="font-mono">{i + 1}</span>
              <span>·</span>
              <span>{slide.layout}</span>
              {slide.lock?.locked && <Lock className="size-2.5" />}
            </div>
            <ScaledSlide
              slide={slide}
              meta={deck.meta}
              sessionId={sessionId}
              className="cursor-zoom-in overflow-hidden rounded-lg border border-border shadow-sm transition-shadow group-hover:shadow-md"
              onClick={() => setOpenIdx(i)}
            />
          </div>
        ))}
        {deck.slides.length === 0 && (
          <p className="rounded-xl border border-border bg-muted/30 px-3 py-2.5 text-xs text-muted-foreground">{t.inspector.deckEmpty}</p>
        )}
      </div>

      {/* 放大浏览浮层 */}
      {openIdx !== null && deck.slides[openIdx] && (
        <div
          className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-black/70 p-6 backdrop-blur-sm"
          onClick={() => setOpenIdx(null)}
        >
          <div
            className="w-full max-w-[min(92vw,calc(85vh*16/9))]"
            onClick={(e) => e.stopPropagation()}
          >
            <ScaledSlide
              slide={deck.slides[openIdx]}
              meta={deck.meta}
              sessionId={sessionId}
              className="overflow-hidden rounded-xl shadow-2xl"
            />
          </div>
          <div className="flex items-center gap-4 text-white/90" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="rounded-full bg-white/10 p-2 hover:bg-white/20 disabled:opacity-30"
              disabled={openIdx === 0}
              onClick={() => setOpenIdx(openIdx - 1)}
            >
              <ChevronLeft className="size-5" />
            </button>
            <span className="font-mono text-sm">{openIdx + 1} / {deck.slides.length}</span>
            <button
              type="button"
              className="rounded-full bg-white/10 p-2 hover:bg-white/20 disabled:opacity-30"
              disabled={openIdx === deck.slides.length - 1}
              onClick={() => setOpenIdx(openIdx + 1)}
            >
              <ChevronRight className="size-5" />
            </button>
            <button
              type="button"
              className="rounded-full bg-white/10 p-2 hover:bg-white/20"
              onClick={() => setOpenIdx(null)}
            >
              <X className="size-5" />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
