import Foundation
import cmark_gfm
import cmark_gfm_extensions

/// Markdown 图片只允许既有会话资产相对路径；外部地址须由用户点击链接。
enum MarkdownImagePolicy {
    static func assetPath(for url: URL?) -> String? {
        guard let url, url.baseURL == nil,
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme == nil, parts.host == nil,
              parts.query == nil, parts.fragment == nil else { return nil }
        let components = parts.path.components(separatedBy: "/")
        guard components.count == 7,
              Array(components.prefix(4)) == ["", "api", "v1", "files"],
              components[5] == "assets",
              validComponent(components[4]), validComponent(components[6]) else { return nil }
        return parts.percentEncodedPath
    }

    private static func validComponent(_ value: String) -> Bool {
        !value.isEmpty && value != "." && value != ".."
            && !value.contains("\\")
            && !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
    }

    /// 使用与 MarkdownUI 相同的 GFM parser 处理引用式/转义/嵌套图片，代码节点不改。
    /// provider 仍独立检查 URL：解析树改写是展示策略，不是网络边界的唯一防线。
    static func linksForExternalImages(_ markdown: String) -> String {
        cmark_gfm_core_extensions_ensure_registered()
        guard let parser = cmark_parser_new(CMARK_OPT_DEFAULT) else { return markdown }
        defer { cmark_parser_free(parser) }
        for name in ["autolink", "strikethrough", "tagfilter", "tasklist", "table"] {
            if let ext = cmark_find_syntax_extension(name) {
                cmark_parser_attach_syntax_extension(parser, ext)
            }
        }
        cmark_parser_feed(parser, markdown, markdown.utf8.count)
        guard let document = cmark_parser_finish(parser) else { return markdown }
        defer { cmark_node_free(document) }
        rewriteImages(in: document)
        guard let rendered = cmark_render_commonmark(document, CMARK_OPT_DEFAULT, 0) else {
            return markdown
        }
        defer { free(rendered) }
        return String(cString: rendered)
    }

    private static func rewriteImages(in parent: UnsafeMutablePointer<cmark_node>, insideLink: Bool = false) {
        var child = cmark_node_first_child(parent)
        while let node = child {
            child = cmark_node_next(node)
            // An image may itself become a link below. Its nested image alt must
            // already be text, otherwise the rewrite would create nested links.
            let kind = cmark_node_get_type(node)
            rewriteImages(in: node, insideLink: insideLink || kind == CMARK_NODE_LINK || kind == CMARK_NODE_IMAGE)
            guard cmark_node_get_type(node) == CMARK_NODE_IMAGE,
                  let source = cmark_node_get_url(node),
                  assetPath(for: URL(string: String(cString: source))) == nil else { continue }
            // LINK 不能嵌 LINK：重序列化后 CommonMark 会把外层链接变成普通文本。
            // 已在链接中时只显示 alt，目标地址始终由既有祖先 LINK 持有。
            if insideLink, let text = cmark_node_new(CMARK_NODE_TEXT) {
                let alt = plainText(in: node)
                cmark_node_set_literal(text, alt.isEmpty ? L("查看图片") : alt)
                cmark_node_replace(node, text)
                cmark_node_free(node)
                continue
            }
            guard let link = cmark_node_new(CMARK_NODE_LINK) else { continue }
            cmark_node_set_url(link, source)
            cmark_node_set_title(link, cmark_node_get_title(node))
            while let label = cmark_node_first_child(node) {
                cmark_node_unlink(label)
                cmark_node_append_child(link, label)
            }
            // 空 alt 也保留一个可见、可点击的链接。
            if cmark_node_first_child(link) == nil, let label = cmark_node_new(CMARK_NODE_TEXT) {
                cmark_node_set_literal(label, L("查看图片"))
                cmark_node_append_child(link, label)
            }
            cmark_node_replace(node, link)
            cmark_node_free(node)
        }
    }

    private static func plainText(in node: UnsafeMutablePointer<cmark_node>) -> String {
        if let literal = cmark_node_get_literal(node) { return String(cString: literal) }
        if cmark_node_get_type(node) == CMARK_NODE_SOFTBREAK || cmark_node_get_type(node) == CMARK_NODE_LINEBREAK {
            return " "
        }
        var result = ""
        var child = cmark_node_first_child(node)
        while let current = child {
            result += plainText(in: current)
            child = cmark_node_next(current)
        }
        return result
    }
}
