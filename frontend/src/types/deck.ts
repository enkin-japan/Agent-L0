// Deck Spec 类型（后端 framework/ppt/spec.py 的镜像）。
// 单一真相是 sessions/{id}/deck/deck.json；本前端只做投影（渲染），不落业务逻辑。

export interface DeckGrid {
  col: number
  row: number
  colSpan: number
  rowSpan: number
}

export interface DeckBlockContent {
  text?: string
  bullets?: string[]
  /** chart/image 资产文件名（sessions/{id}/assets/ 下）；null = 未生成（image 占位） */
  asset?: string | null
  alt?: string
  /** icon 块的 lucide 图标名 */
  name?: string
}

export interface DeckBlock {
  id: string
  type: "text" | "image" | "chart" | "icon"
  role: "title" | "subtitle" | "body" | "caption" | "emphasis"
  grid: DeckGrid
  content: DeckBlockContent
  visual_request?: { kind: string; prompt?: string } | null
}

export interface DeckSlide {
  id: string
  layout: "title" | "section" | "bullets" | "two_col" | "chart" | "quote" | "image_full"
  blocks: DeckBlock[]
  notes?: string
  lock?: { locked: boolean; dirty_by?: string | null }
}

export interface DeckPalette {
  primary: string
  accent: string
  bg: string
  fg: string
  muted: string
  surface: string
  series?: string[]
}

export interface DeckMeta {
  title: string
  aspect: string
  theme: string
  tokens: {
    palette: DeckPalette
    font: { heading: string; body: string }
  }
}

export interface DeckSpec {
  meta: DeckMeta
  slides: DeckSlide[]
}
