import Foundation

@main struct OfflineAssetsTest {
    static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = OfflineAssetStore(root: root)
        let file = root.appendingPathComponent("download")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("first revision".utf8).write(to: file)
        let path = "/api/v1/files/A/assets/report.pdf"
        await store.retainSessions([], namespace: "connection1")
        try await store.install(file, namespace: "connection1", sid: "A", path: path)
        let restored = OfflineAssetStore(root: root)
        let first = await restored.data(namespace: "connection1", sid: "A", key: path)
        precondition(first == Data("first revision".utf8), "durable across store instances")
        let foreign = await restored.file(namespace: "connection2", sid: "A", path: path)
        precondition(foreign == nil, "connections cannot share asset content")
        try Data("new revision".utf8).write(to: file)
        try await store.install(file, namespace: "connection1", sid: "A", path: path)
        let updated = await restored.data(namespace: "connection1", sid: "A", key: path)
        precondition(updated == Data("new revision".utf8), "same-name content refresh")
        let local = await restored.file(namespace: "connection1", sid: "A", path: path)!
        let permissions = try FileManager.default.attributesOfItem(atPath: local.path)[.posixPermissions] as! NSNumber
        precondition(permissions.intValue == 0o600, "private files")
        // Some macOS versions return false from URLResourceValues even when
        // Time Machine honors the explicit exclusion. Verify the OS policy and
        // its marker, so a temporary-directory exclusion cannot hide a regression.
        func output(_ executable: String, _ arguments: [String]) throws -> Data {
            let process = Process(); let pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = arguments; process.standardOutput = pipe
            process.standardError = FileHandle.nullDevice
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            precondition(process.terminationStatus == 0, "backup policy probe must succeed")
            return data
        }
        let hexData = try output("/usr/bin/xattr", ["-px", "com.apple.metadata:com_apple_backup_excludeItem", local.path])
        let hex = Array(String(decoding: hexData, as: UTF8.self).filter { !$0.isWhitespace })
        precondition(hex.count.isMultiple(of: 2), "backup marker must be valid hex")
        let marker = Data(stride(from: 0, to: hex.count, by: 2).map {
            UInt8(String(hex[$0...($0 + 1)]), radix: 16)!
        })
        let exclusion = try PropertyListSerialization.propertyList(from: marker, format: nil) as? String
        precondition(exclusion == "com.apple.backupd", "explicit backup exclusion must be persisted")
        let policy = try output("/usr/bin/tmutil", ["isexcluded", local.path])
        precondition(String(decoding: policy, as: UTF8.self).contains("[Excluded]"),
                     "macOS must exclude offline attachments from backup")
        await store.retainSessions(["A", "B"], namespace: "connection1")
        try await store.save(Data("retained".utf8), namespace: "connection1", sid: "B", key: "files")
        await store.retainSessions(["B"], namespace: "connection1")
        let deleted = await store.file(namespace: "connection1", sid: "A", path: path)
        let kept = await store.data(namespace: "connection1", sid: "B", key: "files")
        precondition(deleted == nil && kept != nil, "confirmed deletion reclaims only removed sessions")
        do {
            try await restored.install(file, namespace: "connection1", sid: "A", path: path)
            preconditionFailure("late download must not resurrect a deleted session")
        } catch {}
        // Automatic mirroring budget. The production limits are the policy itself:
        // one file over 100 MB stays on demand, and the mirrored total is bounded.
        precondition(OfflineAssetStore.MirrorLimits.production.perFile == 100 * 1024 * 1024,
                     "automatic mirroring skips files larger than 100 MB")
        #if os(iOS)
        precondition(OfflineAssetStore.MirrorLimits.production.total == 1024 * 1024 * 1024,
                     "iOS mirrors at most 1 GB")
        #else
        precondition(OfflineAssetStore.MirrorLimits.production.total == 5 * 1024 * 1024 * 1024,
                     "macOS mirrors at most 5 GB")
        #endif
        let budgeted = OfflineAssetStore(root: root.appendingPathComponent("budget"),
                                         limits: .init(perFile: 4096, total: 9000))
        await budgeted.retainSessions(["A"], namespace: "connection1")
        let oversized = await budgeted.admitAutomatic(size: 4097, namespace: "connection1")
        let accepted = await budgeted.admitAutomatic(size: 4096, namespace: "connection1")
        precondition(!oversized.accepted && accepted.accepted && accepted.evicted.isEmpty,
                     "the per-file cap decides admission before any transfer")
        let old = "/api/v1/files/A/assets/old.bin", new = "/api/v1/files/A/assets/new.bin"
        try await budgeted.save(Data(repeating: 1, count: 4096), namespace: "connection1", sid: "A", key: old)
        try await Task.sleep(nanoseconds: 20_000_000)
        try await budgeted.save(Data(repeating: 2, count: 4096), namespace: "connection1", sid: "A", key: new)
        let recent = await budgeted.data(namespace: "connection1", sid: "A", key: new)
        precondition(recent != nil, "the newer copy is the recently used one")
        let room = await budgeted.admitAutomatic(size: 4096, namespace: "connection1")
        let evicted = await budgeted.file(namespace: "connection1", sid: "A", path: old)
        let survivor = await budgeted.file(namespace: "connection1", sid: "A", path: new)
        precondition(room.accepted && evicted == nil && survivor != nil,
                     "a full cache evicts the least recently accessed copy, not the rest")
        precondition(room.evicted == ["A"],
                     "admission reports whose mirrored copies paid for the new one")
        // Deletion fences are bounded the same way: the newest deletions still refuse
        // a late download, the oldest are forgotten instead of growing the record.
        let bounded = OfflineAssetStore(root: root.appendingPathComponent("bounded"))
        await bounded.retainSessions(Set((0..<600).map { "S\($0)" }), namespace: "connection1")
        await bounded.retainSessions([], namespace: "connection1")
        var fenced = 0
        for index in 0..<600 {
            if (try? await bounded.epoch(namespace: "connection1", sid: "S\(index)")) == nil { fenced += 1 }
        }
        precondition(fenced == OfflineAssetStore.retainedDeletions, "deletion fences stay bounded")
        print("PASS offline asset restart / namespace / refresh / permissions / deletion / budget / bounds")
    }
}
