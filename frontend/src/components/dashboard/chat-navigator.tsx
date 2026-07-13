"use client"

import {
  memo,
  useCallback,
  useLayoutEffect,
  useState,
  type RefObject,
} from "react"

import { cn } from "@/lib/utils"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

export type NavigatorItem = {
  id: string
  content: string
}

/** memo：items 由 ChatArea 保证内容不变则引用不变——流式增量期间本组件整体跳过,
 *  内容高度变化由内部 ResizeObserver 自行驱动重测,不依赖父级重渲染。 */
export const ChatNavigator = memo(function ChatNavigator({
  scrollRef,
  items,
  recomputeKey,
}: {
  scrollRef: RefObject<HTMLDivElement | null>
  items: NavigatorItem[]
  recomputeKey: string | number
}) {
  const [positions, setPositions] = useState<Record<string, number>>({})

  const recompute = useCallback(() => {
    const scrollEl = scrollRef.current
    if (!scrollEl) return
    const containerTop = scrollEl.getBoundingClientRect().top
    const total = scrollEl.scrollHeight
    const next: Record<string, number> = {}
    for (const item of items) {
      // 确保你的 Message 组件上添加了 data-msg-id={item.id}
      const el = scrollEl.querySelector<HTMLElement>(
        `[data-msg-id="${item.id}"]`,
      )
      if (!el) continue
      const msgTop =
        el.getBoundingClientRect().top - containerTop + scrollEl.scrollTop
      next[item.id] = total > 0 ? Math.min(1, Math.max(0, msgTop / total)) : 0
    }
    setPositions(next)
  }, [items, scrollRef])

  useLayoutEffect(() => {
    recompute()
    const scrollEl = scrollRef.current
    if (!scrollEl) return

    const observer = new ResizeObserver(() => recompute())
    observer.observe(scrollEl)
    if (scrollEl.firstElementChild) observer.observe(scrollEl.firstElementChild)
    window.addEventListener("resize", recompute)

    return () => {
      observer.disconnect()
      window.removeEventListener("resize", recompute)
    }
  }, [recompute, recomputeKey, scrollRef])

  function scrollToMessage(id: string) {
    const scrollEl = scrollRef.current
    if (!scrollEl) return
    const el = scrollEl.querySelector<HTMLElement>(`[data-msg-id="${id}"]`)
    if (!el) return
    const containerTop = scrollEl.getBoundingClientRect().top
    const msgTop =
      el.getBoundingClientRect().top - containerTop + scrollEl.scrollTop
    scrollEl.scrollTo({ top: Math.max(0, msgTop - 24), behavior: "smooth" })
  }

  if (items.length === 0) return null

  return (
    <div
      aria-hidden={false}
      className="pointer-events-none absolute inset-y-4 right-1.5 z-10 hidden w-6 sm:block"
    >
      <div className="relative h-full">
        {items.map((item) => (
          <div
            key={item.id}
            className="absolute inset-x-0 -translate-y-1/2"
            style={{ top: `${(positions[item.id] ?? 0) * 100}%` }}
          >
            <Tooltip>
              {/* 使用 asChild 以符合 shadcn/ui 的 Tooltip 标准 */}
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={() => scrollToMessage(item.id)}
                  aria-label="Jump to message"
                  className="group pointer-events-auto flex w-full items-center py-1.5"
                >
                  <span
                    className={cn(
                      "block h-0.5 w-full rounded-full bg-muted-foreground/30 transition-all",
                      "group-hover:h-1 group-hover:bg-foreground/70",
                    )}
                  />
                </button>
              </TooltipTrigger>
              <TooltipContent side="left" className="max-w-xs text-pretty">
                {item.content}
              </TooltipContent>
            </Tooltip>
          </div>
        ))}
      </div>
    </div>
  )
})