import MarkdownUI
import SwiftUI

/// 块级图片没有通用网络 loader；只有通过白名单的路径才能创建 AssetImage。
struct SessionAssetImageProvider: ImageProvider {
    @ViewBuilder func makeImage(url: URL?) -> some View {
        if let path = MarkdownImagePolicy.assetPath(for: url) {
            AssetImage(path: path)
        } else if let url {
            Link(L("查看图片"), destination: url)
        } else {
            Text(L("图片地址无效"))
        }
    }
}

/// 行内图片也走既有带鉴权的 fetchAsset，绝不退回 MarkdownUI 默认网络加载。
struct SessionAssetInlineImageProvider: InlineImageProvider {
    let fetchAsset: (String) async throws -> Data

    func image(with url: URL, label: String) async throws -> Image {
        guard let path = MarkdownImagePolicy.assetPath(for: url) else {
            throw URLError(.unsupportedURL)
        }
        let data = try await fetchAsset(path)
        #if os(macOS)
        guard let image = NSImage(data: data) else { throw URLError(.cannotDecodeContentData) }
        return Image(nsImage: image)
        #else
        guard let image = UIImage(data: data) else { throw URLError(.cannotDecodeContentData) }
        return Image(uiImage: image)
        #endif
    }
}
