import Foundation

/// Effective configuration reported by the shared launcher. Configured does not
/// imply that a service is reachable, authenticated or able to run a task.
struct EdgeConfiguration: Decodable, Equatable {
    let status: String
    let missing: [String]

    var incomplete: Bool { status == "incomplete" }

    #if os(macOS)
    static func read(lib: URL, environment: [String: String]) async throws -> EdgeConfiguration {
        try await Task.detached {
            let process = Process()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: "/bin/bash")
            process.arguments = [lib.path, "configuration"]
            process.environment = environment
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            try process.run()
            // Read through EOF before decoding; process exit alone does not mean
            // a background readability handler has consumed the final JSON.
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                return EdgeConfiguration(status: "unknown", missing: [])
            }
            return try JSONDecoder().decode(EdgeConfiguration.self, from: data)
        }.value
    }
    #endif
}
