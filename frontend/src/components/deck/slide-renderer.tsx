"use client"

import { useEffect, useRef, useState } from "react"
import { icons, Image as ImageIcon } from "lucide-react"

import type { DeckBlock, DeckMeta, DeckSlide } from "@/types/deck"

// 设计坐标系：所有字号/间距按 1280×720 设定，容器里用 transform: scale 整体缩放。
// 这保证缩略图、放大预览、PDF 打印渲染的是同一份像素几何——单一真相不止于数据。
export const DESIGN_W = 1280
export const DESIGN_H = 720

/** lucide 图标名（rocket / bar-chart-2）→ 组件。未知名字降级为空心圆点。 */
function LucideByName({ name, ...props }: { name: string; size?: number; color?: string }) {
  const pascal = name
    .split(/[-_\s]+/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join("")
  const Icon = (icons as Record<string, React.ComponentType<typeof props>>)[pascal]
  if (Icon) return <Icon {...props} />
  return <span style={{ fontSize: (props.size ?? 24) * 0.8, color: props.color }}>●</span>
}

const ROLE_STYLE: Record<DeckBlock["role"], React.CSSProperties> = {
  title: { fontSize: 44, fontWeight: 700, lineHeight: 1.2 },
  subtitle: { fontSize: 26, fontWeight: 400, lineHeight: 1.35 },
  body: { fontSize: 22, fontWeight: 400, lineHeight: 1.5 },
  caption: { fontSize: 16, fontWeight: 400, lineHeight: 1.4 },
  emphasis: { fontSize: 34, fontWeight: 500, lineHeight: 1.4, fontStyle: "italic" },
}

// 居中布局（title/section/quote 的正文块居中对齐）
const CENTERED_LAYOUTS = new Set(["title", "section", "quote"])

function BlockView({
  block,
  slide,
  meta,
  sessionId,
}: {
  block: DeckBlock
  slide: DeckSlide
  meta: DeckMeta
  sessionId: string
}) {
  const { palette, font } = meta.tokens
  const centered = CENTERED_LAYOUTS.has(slide.layout)
  const g = block.grid
  const base: React.CSSProperties = {
    gridColumn: `${g.col} / span ${g.colSpan}`,
    gridRow: `${g.row} / span ${g.rowSpan}`,
    minWidth: 0,
    minHeight: 0,
    overflow: "hidden",
  }

  if (block.type === "text") {
    const isHeading = block.role === "title" || block.role === "subtitle"
    const color =
      block.role === "title" ? palette.primary
      : block.role === "caption" || block.role === "subtitle" ? palette.muted
      : palette.fg
    const style: React.CSSProperties = {
      ...base,
      ...ROLE_STYLE[block.role],
      color,
      fontFamily: isHeading || block.role === "emphasis" ? font.heading : font.body,
      textAlign: centered ? "center" : "left",
      display: "flex",
      flexDirection: "column",
      justifyContent: centered ? "center" : "flex-start",
    }
    if (block.content.bullets) {
      return (
        <div style={style}>
          <ul style={{ margin: 0, paddingLeft: "1.1em", display: "flex", flexDirection: "column", gap: "0.55em" }}>
            {block.content.bullets.map((b, i) => (
              <li key={i} style={{ listStyle: "disc" }}>
                {b}
              </li>
            ))}
          </ul>
        </div>
      )
    }
    return (
      <div style={style}>
        <span style={{ whiteSpace: "pre-wrap" }}>{block.content.text}</span>
      </div>
    )
  }

  if (block.type === "chart" || (block.type === "image" && block.content.asset)) {
    if (!block.content.asset) {
      // chart 尚未回填（后端渲染失败等异常态）：可视化提示而不是空白
      return (
        <div style={{ ...base, display: "flex", alignItems: "center", justifyContent: "center", background: palette.surface, borderRadius: 12, color: palette.muted, fontSize: 18 }}>
          chart pending…
        </div>
      )
    }
    return (
      <div style={{ ...base, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <img
          src={`/api/v1/files/${sessionId}/assets/${block.content.asset}`}
          alt={block.content.alt ?? ""}
          style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", borderRadius: 8 }}
        />
      </div>
    )
  }

  if (block.type === "image") {
    // 文生图占位块（生产机文生图子 agent 上线前的既定形态）：主题色风格化，标注意图
    return (
      <div
        style={{
          ...base,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 10,
          background: `linear-gradient(135deg, ${palette.surface}, ${palette.bg})`,
          border: `2px dashed ${palette.muted}55`,
          borderRadius: 16,
          color: palette.muted,
          padding: 24,
          textAlign: "center",
        }}
      >
        <ImageIcon size={44} strokeWidth={1.2} color={palette.muted} />
        <span style={{ fontSize: 17, fontWeight: 500 }}>{block.content.alt || "image"}</span>
        {block.visual_request?.prompt && (
          <span style={{ fontSize: 13, opacity: 0.75, maxWidth: "80%" }}>
            {block.visual_request.prompt}
          </span>
        )}
      </div>
    )
  }

  if (block.type === "icon") {
    return (
      <div style={{ ...base, display: "flex", alignItems: "center", justifyContent: "flex-start" }}>
        <LucideByName name={block.content.name ?? ""} size={40} color={palette.accent} />
      </div>
    )
  }

  return null
}

/** 单页幻灯片，按设计坐标（1280×720）渲染。外层自行负责缩放（见 ScaledSlide）。 */
export function SlideCanvas({
  slide,
  meta,
  sessionId,
}: {
  slide: DeckSlide
  meta: DeckMeta
  sessionId: string
}) {
  const { palette, font } = meta.tokens
  const isSection = slide.layout === "section" || slide.layout === "title"
  return (
    <div
      style={{
        width: DESIGN_W,
        height: DESIGN_H,
        background: isSection
          ? `linear-gradient(160deg, ${palette.bg} 60%, ${palette.surface})`
          : palette.bg,
        color: palette.fg,
        fontFamily: font.body,
        display: "grid",
        gridTemplateColumns: "repeat(12, 1fr)",
        gridTemplateRows: "repeat(12, 1fr)",
        gap: 8,
        padding: 56,
        boxSizing: "border-box",
        position: "relative",
        overflow: "hidden",
      }}
    >
      {slide.blocks.map((b) => (
        <BlockView key={b.id} block={b} slide={slide} meta={meta} sessionId={sessionId} />
      ))}
      {/* 主题色角标（section/title 页的轻装饰，从 tokens 派生，非自由样式） */}
      {isSection && (
        <div
          style={{
            position: "absolute",
            left: 0,
            bottom: 0,
            width: "100%",
            height: 10,
            background: `linear-gradient(90deg, ${palette.primary}, ${palette.accent})`,
          }}
        />
      )}
    </div>
  )
}

/** 自适应容器宽度的缩放包裹：测量容器宽 → transform scale，保持 16:9。 */
export function ScaledSlide({
  slide,
  meta,
  sessionId,
  className,
  onClick,
}: {
  slide: DeckSlide
  meta: DeckMeta
  sessionId: string
  className?: string
  onClick?: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      setWidth(entries[0]?.contentRect.width ?? 0)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const scale = width > 0 ? width / DESIGN_W : 0
  return (
    <div
      ref={ref}
      className={className}
      onClick={onClick}
      style={{ width: "100%", height: width * (DESIGN_H / DESIGN_W), position: "relative", overflow: "hidden" }}
    >
      {scale > 0 && (
        <div style={{ transform: `scale(${scale})`, transformOrigin: "top left", position: "absolute", top: 0, left: 0 }}>
          <SlideCanvas slide={slide} meta={meta} sessionId={sessionId} />
        </div>
      )}
    </div>
  )
}
