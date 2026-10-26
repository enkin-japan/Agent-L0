import Foundation
import CryptoKit

/// One parent directory for every durable local copy (history bodies, mirrored
/// attachments, manifests), so the cache is a single place to inspect or delete.
/// Tests pass explicit roots; nothing here is configurable at runtime.
enum OfflineCacheRoot {
    static var applicationSupport: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("MacAgent", isDirectory: true)
    }

    /// The stores that keep one directory per connection namespace, by subdirectory.
    static let partitionedStores = ["assets", "OfflineHistory"]

    /// Directory name of one connection namespace. Both stores use this one function,
    /// so the pruning below can never compute a name that differs from the store's.
    static func partition(_ namespace: String) -> String {
        SHA256.hash(data: Data(namespace.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// A namespace is the hash of (address, credential), so every ticket change, re-consent,
    /// QR pairing or address change starts a new partition and the old one is never written
    /// again. The per-partition budgets do not see it and the system does not clean
    /// Application Support: delete every partition that no current connection maps to.
    /// Only directories named like a partition are touched; anything else is left alone.
    @discardableResult
    static func pruneOrphans(keeping namespaces: Set<String>, root: URL? = nil) -> [URL] {
        let root = root ?? applicationSupport
        let kept = Set(namespaces.map(partition))
        let manager = FileManager.default
        var removed: [URL] = []
        for store in partitionedStores {
            let parent = root.appendingPathComponent(store, isDirectory: true)
            guard let children = try? manager.contentsOfDirectory(
                at: parent, includingPropertiesForKeys: [.isDirectoryKey]) else { continue }
            for child in children where isPartitionName(child.lastPathComponent)
                && !kept.contains(child.lastPathComponent) {
                guard (try? child.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true else { continue }
                if (try? manager.removeItem(at: child)) != nil { removed.append(child) }
            }
        }
        return removed
    }

    /// ASCII 0-9a-f only: Character.isHexDigit also accepts full-width digits and letters.
    private static func isPartitionName(_ name: String) -> Bool {
        name.utf8.count == 64 && name.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
}

/// Every prune goes through here, one at a time. The set to keep is computed when a
/// prune actually runs, not when it was requested: a snapshot taken earlier could miss
/// a partition that became current in between. `keep` returning nil skips the round.
actor OfflineCachePruner {
    static let shared = OfflineCachePruner()
    private var tail: Task<Void, Never>?

    nonisolated func schedule(root: URL? = nil, keep: @escaping @Sendable () async -> Set<String>?) {
        Task { await self.enqueue(root: root, keep: keep) }
    }

    /// Returns once this prune (and everything queued before it) has finished.
    func enqueue(root: URL? = nil, keep: @escaping @Sendable () async -> Set<String>?) async {
        let previous = tail
        let next = Task {
            await previous?.value
            guard let namespaces = await keep() else { return }
            OfflineCacheRoot.pruneOrphans(keeping: namespaces, root: root)
        }
        tail = next
        await next.value
    }
}
