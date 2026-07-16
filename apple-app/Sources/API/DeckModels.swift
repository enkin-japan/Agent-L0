import Foundation

// Deck Spec（frontend/src/types/deck.ts 的 Swift 镜像；单一真相是后端 deck.json，
// 本端只做投影渲染，不落业务逻辑）。

struct DeckGrid: Codable {
    let col: Int
    let row: Int
    let colSpan: Int
    let rowSpan: Int
}

struct DeckBlockContent: Codable {
    let text: String?
    let bullets: [String]?
    /// chart/image 的资产文件名（sessions/{id}/assets/ 下）；nil = 未生成占位
    let asset: String?
    let alt: String?
    /// icon 块的 lucide 图标名
    let name: String?
}

struct DeckVisualRequest: Codable {
    let kind: String?
    let prompt: String?
}

struct DeckBlock: Codable, Identifiable {
    let id: String
    let type: String    // text | image | chart | icon
    let role: String    // title | subtitle | body | caption | emphasis
    let grid: DeckGrid
    let content: DeckBlockContent
    let visualRequest: DeckVisualRequest?

    enum CodingKeys: String, CodingKey {
        case id, type, role, grid, content
        case visualRequest = "visual_request"
    }
}

struct DeckSlide: Codable, Identifiable {
    let id: String
    let layout: String  // title | section | bullets | two_col | chart | quote | image_full
    let blocks: [DeckBlock]
    let notes: String?
}

struct DeckPalette: Codable {
    let primary: String
    let accent: String
    let bg: String
    let fg: String
    let muted: String
    let surface: String
}

struct DeckFont: Codable {
    let heading: String
    let body: String
}

struct DeckTokens: Codable {
    let palette: DeckPalette
    let font: DeckFont
}

struct DeckMeta: Codable {
    let title: String
    let tokens: DeckTokens
}

struct DeckSpec: Codable {
    let meta: DeckMeta
    let slides: [DeckSlide]
}

struct DeckEnvelope: Codable {
    let status: Bool
    let deck: DeckSpec?
}
