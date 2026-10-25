import Foundation

/// One parent directory for every durable local copy (history bodies, mirrored
/// attachments, manifests), so the cache is a single place to inspect or delete.
/// Tests pass explicit roots; nothing here is configurable at runtime.
enum OfflineCacheRoot {
    static var applicationSupport: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("MacAgent", isDirectory: true)
    }
}
