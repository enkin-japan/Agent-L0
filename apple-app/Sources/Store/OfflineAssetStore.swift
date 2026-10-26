import Foundation
import CryptoKit

/// Durable, connection-scoped copies. Large media is moved from URLSession's
/// download file, never buffered in memory by the synchronizer.
actor OfflineAssetStore {
    /// Automatic mirroring budget. A single huge file is left on demand instead of
    /// filling the device, and the mirrored total stays inside a platform budget.
    /// Neither limit applies to an explicit open/download requested by the user.
    struct MirrorLimits: Sendable, Equatable {
        var perFile: Int64
        var total: Int64
        static let production = MirrorLimits(perFile: 100 * 1024 * 1024,
            total: platformTotal)
        #if os(iOS)
        static let platformTotal: Int64 = 1024 * 1024 * 1024
        #else
        static let platformTotal: Int64 = 5 * 1024 * 1024 * 1024
        #endif
    }
    /// Deleted sessions keep a fence against downloads that were already in flight.
    /// Only the newest deletions can still have one, so older ids are forgotten.
    static let retainedDeletions = 500
    private let root: URL
    let limits: MirrorLimits
    init(root: URL? = nil, limits: MirrorLimits = .production) {
        self.root = (root ?? OfflineCacheRoot.applicationSupport)
            .appendingPathComponent("assets", isDirectory: true)
        self.limits = limits
    }

    private func hash(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private func directory(_ namespace: String, _ sid: String) -> URL {
        root.appendingPathComponent(OfflineCacheRoot.partition(namespace)).appendingPathComponent(hash(sid))
    }

    private func location(_ namespace: String, _ sid: String, _ key: String) -> URL {
        directory(namespace, sid).appendingPathComponent(hash(key.removingPercentEncoding ?? key))
    }

    private struct Membership: Codable {
        var listed: Set<String> = []
        var deleted: Set<String> = []
        // Optional for compatibility with the original membership JSON.
        var epochs: [String: Int]?
        var retainedInputs: [String: Set<OfflineInputReference>]?
        var inputPaths: [String: Set<String>]?
        /// Deletion order of the retained fences, newest last.
        var deletionOrder: [String]?
    }
    private func membershipURL(_ namespace: String) -> URL {
        root.appendingPathComponent(OfflineCacheRoot.partition(namespace)).appendingPathComponent("membership.json")
    }
    private func membership(_ namespace: String) throws -> Membership {
        let url = membershipURL(namespace)
        guard FileManager.default.fileExists(atPath: url.path) else { return Membership() }
        return try JSONDecoder().decode(Membership.self, from: Data(contentsOf: url))
    }
    private func checkAllowed(_ namespace: String, _ sid: String, key: String? = nil,
                              expectedEpoch: Int? = nil) throws {
        let record = try membership(namespace)
        guard !record.deleted.contains(sid) else { throw CocoaError(.fileWriteNoPermission) }
        if let expectedEpoch, expectedEpoch != (record.epochs?[sid] ?? 0) { throw CancellationError() }
        if let key, let input = OfflineInputReference.assetPath(key),
           let retained = record.retainedInputs?[sid], !retained.contains(input) {
            throw CocoaError(.fileReadNoSuchFile)
        }
    }
    func epoch(namespace: String, sid: String) throws -> Int {
        try checkAllowed(namespace, sid)
        return try membership(namespace).epochs?[sid] ?? 0
    }
    func validate(namespace: String, sid: String, expectedEpoch: Int, key: String? = nil) throws {
        try Task.checkCancellation()
        try checkAllowed(namespace, sid, key: key, expectedEpoch: expectedEpoch)
    }
    private func writeMembership(_ record: Membership, namespace: String) throws {
        let url = membershipURL(namespace)
        try prepare(url)
        try JSONEncoder().encode(record).write(to: url, options: .atomic)
        try protect(url)
    }
    private func rememberInput(_ key: String, namespace: String, sid: String) throws {
        guard OfflineInputReference.assetPath(key) != nil else { return }
        var record = try membership(namespace)
        if record.inputPaths == nil { record.inputPaths = [:] }
        var paths = record.inputPaths?[sid] ?? []
        paths.insert(key); record.inputPaths?[sid] = paths
        try writeMembership(record, namespace: namespace)
    }
    /// Only a manifest fetched in the current epoch can authorize new input paths.
    func acceptManifest(_ manifest: OfflineFileManifest, namespace: String, sid: String, expectedEpoch: Int) throws {
        try validate(namespace: namespace, sid: sid, expectedEpoch: expectedEpoch)
        guard manifest.status, manifest.session_id == sid else { throw CocoaError(.fileReadCorruptFile) }
        var record = try membership(namespace)
        if record.retainedInputs == nil { record.retainedInputs = [:] }
        record.retainedInputs?[sid] = Set(manifest.files.compactMap { OfflineInputReference(kind: $0.kind, path: $0.name) })
        try writeMembership(record, namespace: namespace)
    }
    /// Delete only input originals no longer referenced by the remaining canonical turns.
    /// Publish the epoch/retention before touching bytes, so queued old downloads cannot revive them.
    func invalidateInputs(retaining retained: Set<OfflineInputReference>, namespace: String, sid: String) throws {
        var record = try membership(namespace)
        guard !record.deleted.contains(sid) else { return }
        let oldData = try? Data(contentsOf: location(namespace, sid, "manifest"))
        let old = oldData.flatMap { try? JSONDecoder().decode(OfflineFileManifest.self, from: $0) }
        let next = (record.epochs?[sid] ?? 0) + 1
        if record.epochs == nil { record.epochs = [:] }
        record.epochs?[sid] = next
        if record.retainedInputs == nil { record.retainedInputs = [:] }
        record.retainedInputs?[sid] = retained
        let known = (record.inputPaths?[sid] ?? []).union((old?.files ?? []).filter { $0.kind != "assets" }.map { $0.cachePath(sessionId: sid) })
        let removed = known.filter { path in OfflineInputReference.assetPath(path).map { !retained.contains($0) } ?? false }
        record.inputPaths?[sid] = known.subtracting(removed)
        try writeMembership(record, namespace: namespace)
        for key in removed { try? FileManager.default.removeItem(at: location(namespace, sid, key)) }
        if let old {
            let files = old.files.filter { entry in
                entry.kind == "assets" || OfflineInputReference(kind: entry.kind, path: entry.name).map { retained.contains($0) } == true
            }
            try save(JSONEncoder().encode(OfflineFileManifest(status: old.status, session_id: sid, files: files)),
                namespace: namespace, sid: sid, key: "manifest", expectedEpoch: next)
        }
    }
    private func protect(_ url: URL) throws {
        var attributes: [FileAttributeKey: Any] = [.posixPermissions: 0o600]
        #if os(iOS)
        // Mirrored media stays unreadable while the device is locked; an already
        // open handle keeps playing, so a running preview is not cut off.
        attributes[.protectionKey] = FileProtectionType.completeUnlessOpen
        #endif
        try FileManager.default.setAttributes(attributes, ofItemAtPath: url.path)
        var resource = url
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try resource.setResourceValues(values)
    }
    private func prepare(_ url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        var excludedRoot = root
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try excludedRoot.setResourceValues(values)
    }

    /// Reading a cached copy makes it recently used, which is what keeps it from
    /// being the next eviction candidate. Never a failure path: losing the stamp
    /// only makes an in-use file look older than it is.
    private func touch(_ url: URL) {
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        var resource = url
        var values = URLResourceValues(); values.contentAccessDate = Date()
        try? resource.setResourceValues(values)
    }

    /// Control files (membership / manifest / file list / deck) are tiny and are what
    /// makes the mirrored media interpretable offline; eviction never reclaims them.
    /// Matched by file name, which is the hash of the key alone, so no path
    /// normalization can make a control file look like evictable media.
    private var controlFileNames: Set<String> {
        Set(["manifest", "files", "deck", "installed"].map(hash)).union(["membership.json"])
    }

    /// The outcome of one admission decision. `evicted` names the sessions whose
    /// mirrored copies were dropped to make room — their saved state is no longer
    /// complete and the caller must stop advertising them as fully mirrored.
    struct Admission: Sendable, Equatable {
        var accepted: Bool
        var evicted: Set<String> = []
    }

    /// Automatic mirroring admission: refuse a single oversized file outright, and
    /// make room for an accepted one by dropping the least recently accessed
    /// mirrored media of this connection. A refused file stays downloadable.
    /// `markEvicted` records the losing sessions before any byte is removed: if it
    /// fails, nothing is evicted and the file is refused, so a session can never have
    /// lost its copies while its record still says it is fully mirrored.
    func admitAutomatic(size: Int64, namespace: String,
                        markEvicted: @Sendable (Set<String>) async throws -> Void = { _ in }) async -> Admission {
        guard size >= 0, size <= limits.perFile, size <= limits.total else { return Admission(accepted: false) }
        // Plan first, mark, then delete.
        guard var plan = evictionPlan(size: size, namespace: namespace) else { return Admission(accepted: true) }
        let losing = Set(plan.files.compactMap(\.owner))
        if !losing.isEmpty {
            do { try await markEvicted(losing) } catch { return Admission(accepted: false) }
        }
        var evicted = Set<String>()
        for candidate in plan.files {
            guard plan.total + size > limits.total else { break }
            guard (try? FileManager.default.removeItem(at: candidate.url)) != nil else { continue }
            plan.total -= candidate.size
            if let owner = candidate.owner { evicted.insert(owner) }
        }
        return Admission(accepted: plan.total + size <= limits.total, evicted: evicted)
    }

    private struct EvictionPlan {
        var total: Int64
        var files: [(url: URL, size: Int64, owner: String?)]
    }

    /// nil = the file fits without evicting anything. Otherwise the least recently
    /// accessed mirrored files that would have to go, oldest first.
    private func evictionPlan(size: Int64, namespace: String) -> EvictionPlan? {
        let base = root.appendingPathComponent(OfflineCacheRoot.partition(namespace), isDirectory: true)
        let keys: [URLResourceKey] = [.isRegularFileKey, .fileSizeKey, .contentAccessDateKey, .contentModificationDateKey]
        guard let walk = FileManager.default.enumerator(at: base, includingPropertiesForKeys: keys) else { return nil }
        let protected = controlFileNames
        // Only the hashed directory name is on disk; map it back through the
        // sessions this connection knows, so eviction can be attributed.
        let record = try? membership(namespace)
        var owners: [String: String] = [:]
        for sid in (record?.listed ?? []).union(record?.deleted ?? []) { owners[hash(sid)] = sid }
        var total: Int64 = 0
        var candidates: [(url: URL, size: Int64, used: Date, owner: String?)] = []
        for case let url as URL in walk {
            guard let values = try? url.resourceValues(forKeys: Set(keys)),
                  values.isRegularFile == true else { continue }
            let bytes = Int64(values.fileSize ?? 0)
            total += bytes
            guard !protected.contains(url.lastPathComponent) else { continue }
            candidates.append((url, bytes, values.contentAccessDate ?? values.contentModificationDate ?? .distantPast,
                               owners[url.deletingLastPathComponent().lastPathComponent]))
        }
        guard total + size > limits.total else { return nil }
        var files: [(url: URL, size: Int64, owner: String?)] = []
        var room = total
        for candidate in candidates.sorted(by: { $0.used < $1.used }) {
            guard room + size > limits.total else { break }
            files.append((candidate.url, candidate.size, candidate.owner))
            room -= candidate.size
        }
        return EvictionPlan(total: total, files: files)
    }

    func data(namespace: String, sid: String, key: String) -> Data? {
        guard (try? checkAllowed(namespace, sid, key: key)) != nil else { return nil }
        let url = location(namespace, sid, key)
        guard let data = try? Data(contentsOf: url) else { return nil }
        touch(url)
        return data
    }

    func save(_ data: Data, namespace: String, sid: String, key: String, expectedEpoch: Int? = nil) throws {
        try Task.checkCancellation()
        try checkAllowed(namespace, sid, key: key, expectedEpoch: expectedEpoch)
        let url = location(namespace, sid, key)
        try prepare(url)
        try data.write(to: url, options: .atomic)
        try protect(url)
        try rememberInput(key, namespace: namespace, sid: sid)
    }

    func file(namespace: String, sid: String, path: String) -> URL? {
        guard (try? checkAllowed(namespace, sid, key: path)) != nil else { return nil }
        let url = location(namespace, sid, path)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        touch(url)
        return url
    }

    func install(_ temporary: URL, namespace: String, sid: String, path: String, expectedEpoch: Int? = nil) throws {
        try Task.checkCancellation()
        try checkAllowed(namespace, sid, key: path, expectedEpoch: expectedEpoch)
        let dest = location(namespace, sid, path)
        try prepare(dest)
        // Download file and destination may be on different volumes. Copy first,
        // then atomically publish within the cache directory.
        let staging = dest.deletingLastPathComponent().appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: staging) }
        try FileManager.default.copyItem(at: temporary, to: staging)
        try protect(staging)
        if FileManager.default.fileExists(atPath: dest.path) {
            _ = try FileManager.default.replaceItemAt(dest, withItemAt: staging)
        } else {
            try FileManager.default.moveItem(at: staging, to: dest)
        }
        try rememberInput(path, namespace: namespace, sid: sid)
    }

    func remove(namespace: String, sid: String, key: String, expectedEpoch: Int? = nil) {
        guard (try? checkAllowed(namespace, sid, expectedEpoch: expectedEpoch)) != nil else { return }
        try? FileManager.default.removeItem(at: location(namespace, sid, key))
    }

    /// Publish the fence for newly removed sessions and forget the oldest ones.
    private func fenceDeletions(removed: Set<String>, in record: inout Membership) {
        var epochs = record.epochs ?? [:]
        for sid in removed { epochs[sid] = (epochs[sid] ?? 0) + 1 }
        record.epochs = epochs
        let listed = record.listed
        var order = (record.deletionOrder ?? []).filter { !removed.contains($0) && !listed.contains($0) }
        order.append(contentsOf: removed.sorted())
        guard order.count > Self.retainedDeletions else { record.deletionOrder = order; return }
        var dropped: [String] = []
        var remaining = order.count
        order.removeAll { id in
            guard remaining > Self.retainedDeletions else { return false }
            remaining -= 1; dropped.append(id); return true
        }
        for id in dropped {
            record.deleted.remove(id); record.epochs?[id] = nil
            record.retainedInputs?[id] = nil; record.inputPaths?[id] = nil
        }
        record.deletionOrder = order
    }

    /// Returns true when whole sessions were reclaimed: that is free space, which is
    /// the moment a mirror left incomplete by the budget deserves another attempt.
    @discardableResult
    func retainSessions(_ ids: Set<String>, namespace: String, isCurrent: @Sendable () -> Bool = { true }) -> Bool {
        guard !Task.isCancelled, isCurrent() else { return false }
        var reclaimed = false
        do {
            var record = try membership(namespace)
            record.deleted.formUnion(record.listed.subtracting(ids))
            record.deleted.subtract(ids)
            let removed = record.listed.subtracting(ids)
            record.listed = ids
            fenceDeletions(removed: removed, in: &record)
            let fence = membershipURL(namespace)
            try prepare(fence)
            try JSONEncoder().encode(record).write(to: fence, options: .atomic)
            try protect(fence)
            let keep = Set(ids.map(hash))
            for child in (try? FileManager.default.contentsOfDirectory(at: fence.deletingLastPathComponent(),
                includingPropertiesForKeys: [.isDirectoryKey])) ?? [] {
                if (try? child.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true,
                   !keep.contains(child.lastPathComponent) {
                    if (try? FileManager.default.removeItem(at: child)) != nil { reclaimed = true }
                }
            }
        } catch { return reclaimed }
        return reclaimed
    }

    @discardableResult
    func removeSessions(_ ids: Set<String>, namespace: String) -> Bool {
        var reclaimed = false
        do {
            var record = try membership(namespace)
            record.deleted.formUnion(ids); record.listed.subtract(ids)
            fenceDeletions(removed: ids, in: &record)
            let fence = membershipURL(namespace)
            try prepare(fence)
            try JSONEncoder().encode(record).write(to: fence, options: .atomic)
            try protect(fence)
            for id in ids {
                if (try? FileManager.default.removeItem(at: directory(namespace, id))) != nil { reclaimed = true }
            }
        } catch { return reclaimed }
        return reclaimed
    }

}

struct OfflineFileManifest: Codable {
    let status: Bool
    let session_id: String
    let files: [OfflineFileEntry]
}

struct OfflineFileEntry: Codable {
    let path: String
    let name: String
    let kind: String
    let size: Int64
    let revision: String

    func cachePath(sessionId: String) -> String {
        kind == "assets" ? "/api/v1/files/\(sessionId)/assets/\(name)" : path
    }
}

/// Identity of an uploaded original; never an arbitrary local path.
struct OfflineInputReference: Codable, Hashable {
    let kind: String
    let name: String
    init?(kind: String, path: String) {
        guard ["image", "audio", "attach"].contains(kind) else { return nil }
        let name = ((path.removingPercentEncoding ?? path) as NSString).lastPathComponent
        guard !name.isEmpty, name != ".", name != "..", !name.contains("\\") else { return nil }
        self.kind = kind; self.name = name
    }
    static func assetPath(_ path: String) -> OfflineInputReference? {
        guard path.hasPrefix("/api/v1/files/") else { return nil }
        let parts = (path.removingPercentEncoding ?? path).split(separator: "/")
        guard parts.count >= 6 else { return nil }
        return OfflineInputReference(kind: String(parts[parts.count - 2]), path: String(parts.last!))
    }
}
