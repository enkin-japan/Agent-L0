import Foundation
import CryptoKit

/// Reading projection only. Never restores an active turn, approval, or execution authority.
/// Actor isolation serializes atomic file IO off MainActor; histories are never retained here.
actor OfflineHistoryStore {
    struct Index: Codable {
        var sessions: [SessionMetaItem] = []
        var retentionPolicy: SessionRetentionPolicy?
        var selectedSessionId: String?
        var versions: [String: Version] = [:]
        var invalidated: Set<String>?
        var epochs: [String: Int]?
        /// Index version reached by the last attachment mirroring pass.
        var assetVersions: [String: Version]?
        /// Sessions whose mirror is knowingly incomplete at that version: files the
        /// budget refused, or copies evicted to make room for another session.
        /// Kept out of `needsAssetSync` on purpose — re-mirroring at once would only
        /// evict the session that just took the space. Retried when the index
        /// version changes or when deleted sessions free room.
        var assetPartial: Set<String>?
        /// Eviction counter per session; a pass compares it against the value it
        /// started with, so a mark raised mid-pass cannot be erased by its result.
        var assetMarks: [String: Int]?
        /// Removal order of the retained tombstones, newest last.
        var tombstones: [String]?
    }
    struct Version: Codable, Equatable {
        let updated: Double?
        let count: Int?
        init(_ item: SessionMetaItem) { updated = item.lastUpdated; count = item.turnCount }
    }
    /// A tombstone blocks resurrection by a response that was already in flight when
    /// the session disappeared. Only the newest removals can still have one in flight,
    /// so the older entries are forgotten instead of growing the index forever.
    static let retainedTombstones = 500
    let root: URL
    init(root: URL? = nil) {
        self.root = root ?? OfflineCacheRoot.applicationSupport
            .appendingPathComponent("OfflineHistory", isDirectory: true)
    }
    nonisolated static func digest(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
    nonisolated static func namespace(baseURL: URL, apiKey: String) -> String {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: true)
        let scheme = components?.scheme?.lowercased(), host = components?.host?.lowercased()
        components?.scheme = scheme
        components?.host = host
        components?.fragment = nil
        // Hash the identity as one opaque value; credentials never become paths or file contents.
        return digest((components?.string ?? baseURL.absoluteString) + "\n" + digest(apiKey))
    }
    private func directory(_ namespace: String) -> URL {
        root.appendingPathComponent(OfflineCacheRoot.partition(namespace), isDirectory: true)
    }
    private func historyURL(_ sessionId: String, _ namespace: String) -> URL {
        directory(namespace).appendingPathComponent("history-" + Self.digest(sessionId) + ".json")
    }
    /// The index exists but cannot be read right now (iOS: device locked). Not a
    /// failure of the connection: callers skip this round's index writes and go on.
    struct IndexUnreadable: Error {
        let underlying: Error
    }
    /// Readers treat an unreadable index as empty: nothing cached is shown.
    func loadIndex(namespace: String) -> Index {
        (try? readIndex(namespace: namespace)) ?? Index()
    }
    /// nil = the index exists but is unreadable (callers keep what they already show).
    func readableIndex(namespace: String) -> Index? {
        try? readIndex(namespace: namespace)
    }
    /// The base of every index write. Only a missing file is an empty index. Any other
    /// read failure throws, so the write is skipped instead of replacing the real index
    /// with an empty one — on iOS the file is `.completeUnlessOpen` and cannot be read
    /// while the device is locked, which is exactly when background refreshes run.
    /// An unparsable file is still rebuilt from empty: it holds nothing to preserve.
    private func readIndex(namespace: String) throws -> Index {
        let url = directory(namespace).appendingPathComponent("index.json")
        let data: Data
        do { data = try Data(contentsOf: url) }
        catch CocoaError.fileReadNoSuchFile, CocoaError.fileNoSuchFile { return Index() }
        catch { throw IndexUnreadable(underlying: error) }
        return (try? JSONDecoder().decode(Index.self, from: data)) ?? Index()
    }
    private func write<T: Encodable>(_ value: T, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try JSONEncoder().encode(value).write(to: url, options: .atomic)
        var attributes: [FileAttributeKey: Any] = [.posixPermissions: 0o600]
        #if os(iOS)
        // Conversation text stays unreadable while the device is locked; an already
        // open handle keeps working, so a background sync is not interrupted.
        attributes[.protectionKey] = FileProtectionType.completeUnlessOpen
        #endif
        try FileManager.default.setAttributes(attributes, ofItemAtPath: url.path)
        // Local conversation data should not migrate through device/cloud backups.
        // A refused exclusion is a real failure: the caller must not advertise the
        // write as a saved copy on this device.
        var resource = url
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try resource.setResourceValues(values)
    }
    private func writeIndex(_ index: Index, namespace: String) throws {
        try write(index, to: directory(namespace).appendingPathComponent("index.json"))
    }
    /// Called only after a successful authoritative index response. Empty means confirmed empty.
    @discardableResult
    func saveIndex(_ sessions: [SessionMetaItem], retentionPolicy: SessionRetentionPolicy?, namespace: String,
                   isCurrent: @Sendable () -> Bool = { true }) throws -> Bool {
        guard !Task.isCancelled, isCurrent() else { return false }
        var index = try readIndex(namespace: namespace)
        let valid = Set(sessions.map(\.sessionId))
        let removed = Set(index.sessions.map(\.sessionId)).union(index.versions.keys).subtracting(valid)
        for id in removed {
            guard !Task.isCancelled, isCurrent() else { return false }
            advanceEpoch(id, index: &index)
            try? FileManager.default.removeItem(at: historyURL(id, namespace))
        }
        index.sessions = sessions; index.retentionPolicy = retentionPolicy
        index.versions = index.versions.filter { valid.contains($0.key) }
        index.assetVersions = index.assetVersions?.filter { valid.contains($0.key) }
        index.assetPartial = index.assetPartial?.intersection(valid)
        index.assetMarks = index.assetMarks?.filter { valid.contains($0.key) }
        pruneTombstones(&index, listed: valid)
        // A newly selected draft may not be listed until its first submit settles.
        // Restore already checks membership; clear only a previously known removal.
        if let selected = index.selectedSessionId, removed.contains(selected) { index.selectedSessionId = nil }
        guard !Task.isCancelled, isCurrent() else { return false }
        try writeIndex(index, namespace: namespace)
        return true
    }
    func saveSelection(_ sessionId: String?, namespace: String) throws {
        var index = try readIndex(namespace: namespace); index.selectedSessionId = sessionId
        try writeIndex(index, namespace: namespace)
    }
    func loadHistory(sessionId: String, namespace: String, matchingVersion: Version? = nil) -> HistoryResponse? {
        let index = loadIndex(namespace: namespace)
        guard index.invalidated?.contains(sessionId) != true else { return nil }
        if let matchingVersion, index.versions[sessionId] != matchingVersion { return nil }
        guard let data = try? Data(contentsOf: historyURL(sessionId, namespace)),
              let value = try? JSONDecoder().decode(HistoryResponse.self, from: data),
              value.status, value.sessionId == sessionId else { return nil }
        return value
    }
    @discardableResult
    func saveHistory(_ history: HistoryResponse, metadata: SessionMetaItem?, namespace: String, requireListed: Bool = false, expectedEpoch: Int? = nil) throws -> Bool {
        guard history.status, !Task.isCancelled else { return false }
        if let expectedEpoch, expectedEpoch != epoch(sessionId: history.sessionId, namespace: namespace) { return false }
        var index = try readIndex(namespace: namespace)
        let listed = index.sessions.first { $0.sessionId == history.sessionId }
        // A late foreground response must not borrow a newer index's version.
        if let metadata, let listed, Version(metadata) != Version(listed) { return false }
        if metadata == nil, index.versions[history.sessionId] != nil { return false }
        if requireListed {
            guard let metadata, let listed, Version(listed) == Version(metadata) else { return false }
        }
        // Within one index version, prefer an already saved complete newer snapshot.
        // Confirmed deletion advances the epoch/removes the old file before a shorter save.
        if index.versions[history.sessionId] == metadata.map(Version.init),
           let existing = loadHistory(sessionId: history.sessionId, namespace: namespace),
           existing.turns.count > history.turns.count { return false }
        try write(history, to: historyURL(history.sessionId, namespace))
        index = try readIndex(namespace: namespace)
        index.invalidated?.remove(history.sessionId)
        if let metadata { index.versions[history.sessionId] = Version(metadata) }
        else { index.versions[history.sessionId] = nil }
        try writeIndex(index, namespace: namespace)
        return true
    }
    /// Compare complete public records off MainActor; a matching count/ref alone is insufficient.
    func canonicalTurnsMatch(_ first: [Turn], _ second: [Turn]) -> Bool {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        guard let lhs = try? encoder.encode(first), let rhs = try? encoder.encode(second) else { return false }
        return lhs == rhs
    }
    func epoch(sessionId: String, namespace: String) -> Int {
        loadIndex(namespace: namespace).epochs?[sessionId] ?? 0
    }
    private func advanceEpoch(_ sessionId: String, index: inout Index) {
        if index.epochs == nil { index.epochs = [:] }
        let next = (index.epochs?[sessionId] ?? 0) + 1
        index.epochs?[sessionId] = next
        index.assetVersions?[sessionId] = nil
        index.assetPartial?.remove(sessionId)
        index.assetMarks?[sessionId] = nil
        var order = index.tombstones ?? []
        order.removeAll { $0 == sessionId }
        order.append(sessionId)
        index.tombstones = order
    }
    /// Keep the newest removals only. An id the server still lists keeps its epoch
    /// regardless of age: a late response for a live session must stay fenced.
    private func pruneTombstones(_ index: inout Index, listed: Set<String>) {
        guard var order = index.tombstones, order.count > Self.retainedTombstones else { return }
        var dropped: [String] = []
        var remaining = order.count
        order.removeAll { id in
            guard remaining > Self.retainedTombstones, !listed.contains(id) else { return false }
            remaining -= 1; dropped.append(id); return true
        }
        for id in dropped {
            index.epochs?[id] = nil
            index.invalidated?.remove(id)
        }
        index.tombstones = order
    }
    /// A confirmed tail deletion invalidates only this body. Persist the tombstone
    /// before removing bytes so an interrupted cleanup cannot resurrect it on restart.
    func invalidateHistory(sessionId: String, namespace: String) throws {
        var index = try readIndex(namespace: namespace)
        advanceEpoch(sessionId, index: &index)
        index.versions[sessionId] = nil
        if index.invalidated == nil { index.invalidated = [] }
        index.invalidated?.insert(sessionId)
        try writeIndex(index, namespace: namespace)
        try? FileManager.default.removeItem(at: historyURL(sessionId, namespace))
    }
    func needsHistory(_ item: SessionMetaItem, namespace: String) -> Bool {
        let index = loadIndex(namespace: namespace)
        return item.active == true || index.versions[item.sessionId] != Version(item) ||
            !FileManager.default.fileExists(atPath: historyURL(item.sessionId, namespace).path)
    }
    /// Attachments are re-listed only when the session itself changed. An unchanged
    /// index version means the last pass already mirrored every declared file.
    func needsAssetSync(_ item: SessionMetaItem, namespace: String) -> Bool {
        loadIndex(namespace: namespace).assetVersions?[item.sessionId] != Version(item)
    }
    /// Taken before a mirroring pass starts. Any eviction mark raised while that pass
    /// runs changes this counter, and the pass can then no longer report "complete":
    /// the bytes it believed it had written are already gone.
    func beginAssetSync(sessionId: String, namespace: String) -> Int {
        loadIndex(namespace: namespace).assetMarks?[sessionId] ?? 0
    }
    /// Recorded after a pass that reached the end of the manifest. `complete: false`
    /// means the budget left files out: the version still advances (the 15s refresh
    /// must not retry it forever) but the session is not advertised as fully saved.
    func recordAssetSync(_ item: SessionMetaItem, namespace: String, complete: Bool, token: Int) throws {
        var index = try readIndex(namespace: namespace)
        guard index.sessions.contains(where: { $0.sessionId == item.sessionId }),
              index.invalidated?.contains(item.sessionId) != true else { return }
        if index.assetVersions == nil { index.assetVersions = [:] }
        index.assetVersions?[item.sessionId] = Version(item)
        let evictedDuringPass = (index.assetMarks?[item.sessionId] ?? 0) != token
        if complete, !evictedDuringPass { index.assetPartial?.remove(item.sessionId) }
        else { index.assetPartial = (index.assetPartial ?? []).union([item.sessionId]) }
        try writeIndex(index, namespace: namespace)
    }
    /// Mirrored copies of these sessions were dropped to make room for another one.
    /// A session being mirrored for the first time has no record yet and is marked
    /// all the same — its running pass reads the raised counter and stays partial.
    func markAssetsEvicted(_ ids: Set<String>, namespace: String) throws {
        guard !ids.isEmpty else { return }
        var index = try readIndex(namespace: namespace)
        index.assetPartial = (index.assetPartial ?? []).union(ids)
        var marks = index.assetMarks ?? [:]
        for id in ids { marks[id] = (marks[id] ?? 0) + 1 }
        index.assetMarks = marks
        try writeIndex(index, namespace: namespace)
    }
    func partialAssetSessions(namespace: String) -> Set<String> {
        let index = loadIndex(namespace: namespace)
        return (index.assetPartial ?? []).intersection(index.sessions.map(\.sessionId))
    }
    /// Space was reclaimed (sessions disappeared): every mirror the budget had cut
    /// short becomes eligible again on the next pass.
    func retryPartialAssetSessions(namespace: String) throws {
        var index = try readIndex(namespace: namespace)
        guard let partial = index.assetPartial, !partial.isEmpty else { return }
        for id in partial { index.assetVersions?[id] = nil }
        index.assetPartial = []
        try writeIndex(index, namespace: namespace)
    }
    func removeSessions(_ ids: Set<String>, namespace: String) throws {
        var index = try readIndex(namespace: namespace)
        for id in ids {
            advanceEpoch(id, index: &index)
            try? FileManager.default.removeItem(at: historyURL(id, namespace)); index.versions[id] = nil
        }
        index.sessions.removeAll { ids.contains($0.sessionId) }
        if let selected = index.selectedSessionId, ids.contains(selected) { index.selectedSessionId = nil }
        try writeIndex(index, namespace: namespace)
    }
}

/// Single classifier for "the network went away" versus "the server answered badly".
/// Cached reading should survive ordinary transport loss; authentication,
/// malformed responses, TLS failures and other unknown errors stay visible.
enum OfflineTransportFailure {
    static func matches(_ error: Error) -> Bool {
        guard let transport = error as? URLError else { return false }
        switch transport.code {
        case .cancelled, .notConnectedToInternet, .cannotConnectToHost, .cannotFindHost,
             .networkConnectionLost, .timedOut, .dnsLookupFailed,
             .internationalRoamingOff, .callIsActive, .dataNotAllowed:
            return true
        default:
            return false
        }
    }
}

/// Which connection partitions are still in use. Pure, so it is tested on the host.
/// `macs` are the saved connections that hold a credential; `keys` maps their id to it.
/// nil means "do not prune this round": any credential that reads back empty (the
/// keychain was unavailable, or an entry was lost) would otherwise make its live
/// partition look orphaned, and an empty result — no Mac and no key, or nothing with a
/// usable address — would delete every partition.
extension OfflineCacheRoot {
    struct Connection: Equatable, Sendable {
        let id: String
        let url: String
    }

    static func keepSet(macs: [Connection], keys: [String: String],
                        fallbackBaseURL: URL?, fallbackKey: String) -> Set<String>? {
        var namespaces = Set<String>()
        for mac in macs {
            guard let key = keys[mac.id], !key.isEmpty else { return nil }
            guard let url = URL(string: mac.url), url.scheme != nil else { continue }
            namespaces.insert(OfflineHistoryStore.namespace(baseURL: url, apiKey: key))
        }
        if let url = fallbackBaseURL, url.scheme != nil, !fallbackKey.isEmpty {
            namespaces.insert(OfflineHistoryStore.namespace(baseURL: url, apiKey: fallbackKey))
        }
        return namespaces.isEmpty ? nil : namespaces
    }
}
