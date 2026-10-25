import Foundation
func L(_ value: String) -> String { value }

@main struct OfflineAssetsTransportTest {
    static func main() async throws {
        let mode = CommandLine.arguments[1]
        let root = URL(fileURLWithPath: CommandLine.arguments[2])
        let client = AgentClient(baseURL: URL(string: CommandLine.arguments[3])!, apiKey: "fixture-only", cacheRoot: root)
        if mode == "legacy" {
            let first = try await client.sessionFiles(sessionId: "LEGACY")
            let second = try await client.sessionFiles(sessionId: "LEGACY")
            precondition(first.isEmpty && second.count == 1, "legacy backend file lists refresh after an empty cached response")
            print("PASS legacy backend file list refresh")
            return
        }
        if mode == "budget" {
            // Automatic mirroring leaves an over-cap file on the server; history
            // text and the small originals still land on disk, and the skipped
            // output remains downloadable on demand.
            let limited = AgentClient(baseURL: URL(string: CommandLine.arguments[3])!, apiKey: "fixture-only",
                cacheRoot: root, mirrorLimits: .init(perFile: 65536, total: 10 * 1024 * 1024))
            let ok = await limited.syncOfflineAssets(sessionId: "A")
            precondition(ok.state == .partial && ok.evicted.isEmpty,
                         "a file left out by the budget is reported as partial, not as a failure")
            let skipped = await limited.offlineAssets.data(namespace: limited.cacheNamespace, sid: "A",
                key: "/api/v1/files/A/assets/report.txt")
            precondition(skipped == nil, "an over-cap output is never mirrored automatically")
            let mirrored = await limited.offlineAssets.data(namespace: limited.cacheNamespace, sid: "A",
                key: "/api/v1/files/A/offline/attach/source.txt")
            precondition(mirrored == Data("original upload".utf8), "small originals are still mirrored")
            let listed = try await limited.sessionFiles(sessionId: "A")
            precondition(listed.count == 1 && listed[0].name == "report.txt", "the skipped output stays listed")
            let fetched = try await limited.fetchAsset(path: listed[0].assetPath(sessionId: "A"))
            precondition(fetched == Data(repeating: 65, count: 262144), "the skipped output downloads on demand")
            print("PASS asset mirroring budget")
            return
        }
        if mode == "sync" {
            let ok = await client.syncOfflineAssets(sessionId: "A")
            precondition(ok.state == .complete, "all files and deck snapshot synchronize")
            // A refused or failing server answer must reach the caller even when a
            // cached list exists; only lost transport reads the cache.
            try await client.offlineAssets.save(JSONEncoder().encode(SessionFilesResponse(status: true, files: [])),
                namespace: client.cacheNamespace, sid: "B", key: "files")
            do {
                _ = try await client.sessionFiles(sessionId: "B")
                preconditionFailure("a server error must not be answered from the cache")
            } catch {}
        }
        let list = try await client.sessionFiles(sessionId: "A")
        precondition(list.count == 1 && list[0].name == "report.txt", "offline files listing")
        let path = list[0].assetPath(sessionId: "A")
        let data = try await client.fetchAsset(path: path)
        precondition(data == Data(repeating: 65, count: 262144), "durable full asset bytes")
        let playback = await client.assetPlaybackSource(path: path)
        precondition(playback?.url.isFileURL == true && playback?.headers.isEmpty == true, "offline media uses local file")
        let exported = try await client.downloadAsset(path: path, suggestedName: "report.txt")
        precondition(exported.lastPathComponent == "report.txt", "exports retain filename")
        let exportedData = try Data(contentsOf: exported)
        precondition(exportedData == data, "export bytes")
        // One export location per cached asset: repeated previews overwrite it
        // instead of leaving a full temporary copy behind every time.
        let again = try await client.downloadAsset(path: path, suggestedName: "report.txt")
        let againData = try Data(contentsOf: again)
        precondition(again == exported && againData == data,
                     "a second export reuses the same temporary copy")
        let input = await client.offlineAssets.data(namespace: client.cacheNamespace, sid: "A",
            key: "/api/v1/files/A/offline/attach/source.txt")
        precondition(input == Data("original upload".utf8), "input is also offline")
        let inputList = await client.cachedInputFiles(sessionId: "A")
        precondition(Set(inputList.map(\.kind)) == ["attach", "image", "audio"], "all uploaded input kinds have an offline entry")
        let original = try await client.downloadAttachment(sessionId: "A",
            attachment: AttachmentInfo(path: "/old/device/source.txt", type: "text", name: "原件.txt"))
        let originalBytes = try Data(contentsOf: original)
        precondition(original.lastPathComponent == "原件.txt" && originalBytes == input, "original opens through manifest without touching absolute path")
        do {
            _ = try await client.downloadAttachment(sessionId: "A",
                attachment: AttachmentInfo(path: "/etc/passwd", type: nil, name: "private"))
            preconditionFailure("undeclared local paths must never open")
        } catch {}
        print("PASS asset transport \(mode)")
    }
}
