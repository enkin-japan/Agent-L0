import SwiftUI

/// 会话资产图片（render_chart 产物等）：AsyncImage 带不了 Authorization 头，
/// 走 AgentClient.fetchAsset（URLCache 长缓存）+ 进程内 NSCache 双层缓存。
struct AssetImage: View {
    let path: String
    /// 消息流里的图限宽 560；deck 块里传 nil 填满所在块。
    var maxWidth: CGFloat? = 560
    @EnvironmentObject var store: AgentStore
    @State private var loaded: CrossPlatformImage?
    @State private var failed = false

    private static let cache: NSCache<NSString, CrossPlatformImage> = {
        let cache = NSCache<NSString, CrossPlatformImage>()
        cache.countLimit = 32
        cache.totalCostLimit = 64 * 1024 * 1024
        return cache
    }()

    var body: some View {
        Group {
            if let image = loaded {
                imageView(image)
                    .aspectRatio(contentMode: .fit)
                    .frame(maxWidth: maxWidth ?? .infinity)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10)
                        .stroke(Color.primary.opacity(0.1)))
            } else if failed {
                Label("图片加载失败：\((path as NSString).lastPathComponent)",
                      systemImage: "photo.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ProgressView()
                    .frame(width: 120, height: 80)
            }
        }
        .task(id: (store.client?.cacheNamespace ?? "") + ":" + path) { await load() }
    }

    private func load() async {
        loaded = nil
        failed = false
        guard let client = store.client else { failed = true; return }
        let key = "\(client.cacheNamespace):\(path)" as NSString
        if let cached = Self.cache.object(forKey: key) {
            loaded = cached
            return
        }
        do {
            let data = try await client.fetchAsset(path: path)
            guard !Task.isCancelled, store.client === client else { return }
            if let image = CrossPlatformImage(data: data) {
                Self.cache.setObject(image, forKey: key, cost: data.count)
                loaded = image
            } else {
                failed = true
            }
        } catch {
            guard !Task.isCancelled, store.client === client else { return }
            failed = true
        }
    }

    private func imageView(_ image: CrossPlatformImage) -> some View {
        #if os(macOS)
        Image(nsImage: image).resizable()
        #else
        Image(uiImage: image).resizable()
        #endif
    }
}

#if os(macOS)
typealias CrossPlatformImage = NSImage
#else
typealias CrossPlatformImage = UIImage
#endif
