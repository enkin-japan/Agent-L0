import Foundation

@main
enum DeckTest {
    static func main() throws {
        let path = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/Volumes/enkin/project/MacAgent/backend/sessions/decktest2-32333/deck/deck.json"
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        let deck = try JSONDecoder().decode(DeckSpec.self, from: data)
        print("✅ 解码成功：《\(deck.meta.title)》 \(deck.slides.count) 页")
        print("   palette: bg=\(deck.meta.tokens.palette.bg) primary=\(deck.meta.tokens.palette.primary)")
        for slide in deck.slides {
            let types = slide.blocks.map { "\($0.type)/\($0.role)" }.joined(separator: ", ")
            let assets = slide.blocks.compactMap { $0.content.asset }
            print("   [\(slide.layout)] \(slide.blocks.count) blocks: \(types)"
                  + (assets.isEmpty ? "" : "  assets: \(assets.joined(separator: ","))"))
        }
        // 网格越界检查：渲染器假设 col/row ∈ [1,12] 且 span 不出界
        for slide in deck.slides {
            for block in slide.blocks {
                let g = block.grid
                if g.col < 1 || g.row < 1 || g.col + g.colSpan - 1 > 12 || g.row + g.rowSpan - 1 > 12 {
                    print("   ⚠️ 越界 grid: \(block.id) \(g)")
                }
            }
        }
        print("== deck decode: OK ==")
    }
}
