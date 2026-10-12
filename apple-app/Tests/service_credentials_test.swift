import Foundation

// Platform stores are synthetic; production AppSettings is compiled unchanged
// except replacing its Keychain declaration and UserDefaults.standard reference.
enum Keychain {
    static var values: [String: String] = [:]
    static func load(_ account: String) -> String { values[account] ?? "" }
    static func save(_ account: String, _ value: String) { values[account] = value }
    static func loadAPIKey() -> String { load("agent-api-key") }
    static func saveAPIKey(_ value: String) { save("agent-api-key", value) }
}
final class FixtureDefaults {
    static let standard = FixtureDefaults()
    var values: [String: Any] = [:]
    func string(forKey key: String) -> String? { values[key] as? String }
    func object(forKey key: String) -> Any? { values[key] }
    func bool(forKey key: String) -> Bool { values[key] as? Bool ?? false }
    func set(_ value: Any, forKey key: String) { values[key] = value }
}
enum AppLanguage: String { case system }
enum L10n { static var current = AppLanguage.system }
struct AgentClient { let baseURL: URL; let apiKey: String }

@main
struct ServiceCredentialsTest {
    @MainActor
    static func main() {
        // Upgrade from an empty legacy account must stay empty even after the
        // sidecar creates its independent local data credential.
        let emptyLegacy = AppSettings()
        emptyLegacy.ragApiKey = "auto-data-fixture"
        precondition(AppSettings().ragComputeApiKey.isEmpty)
        Keychain.values = [:]
        FixtureDefaults.standard.values = [:]
        let credentials = ServiceCredentials(agent: "agent-fixture", inference: "infer-fixture",
                                             ragData: "data-fixture", ragCompute: "compute-fixture")
        let env = credentials.environment
        precondition(Set(env.values).count == 4)
        precondition(env["RAG_API_KEY"] == "data-fixture")
        precondition(env["RAG_COMPUTE_API_KEY"] == "compute-fixture")
        for rawAgent in ["\"agent-fixture\"", "AGENT_API_KEY=agent-fixture"] {
            var noncanonical = credentials
            noncanonical.agent = rawAgent
            precondition(noncanonical.environment["AGENT_API_KEY"] == rawAgent)
        }
        // The transitional remote data service still receives its own old key.
        var edited = credentials
        edited.ragCompute = "export RAG_COMPUTE_API_KEY=\"new-compute-fixture\"\n"
        precondition(edited.environment["RAG_COMPUTE_API_KEY"] == "new-compute-fixture")
        precondition(edited.environment["RAG_API_KEY"] == env["RAG_API_KEY"])
        precondition(ServiceCredentials.restoredCompute(saved: "", legacyData: "legacy",
                                                        configured: false) == "legacy")
        precondition(ServiceCredentials.restoredCompute(saved: "saved", legacyData: "legacy",
                                                        configured: false) == "saved")
        precondition(ServiceCredentials.restoredCompute(saved: "", legacyData: "legacy",
                                                        configured: true).isEmpty)
        let settings = AppSettings()
        settings.ragApiKey = "data-fixture"
        settings.ragComputeApiKey = "new-compute-fixture"
        let reloaded = AppSettings()
        precondition(reloaded.ragApiKey == "data-fixture")
        precondition(reloaded.ragComputeApiKey == "new-compute-fixture")
        reloaded.ragComputeApiKey = ""
        precondition(AppSettings().ragComputeApiKey.isEmpty)
        // Upgrade preserves both the old data credential and the old compute use.
        Keychain.values = ["rag-api-key": "legacy-fixture"]
        FixtureDefaults.standard.values = [:]
        let legacy = AppSettings()
        precondition(legacy.ragApiKey == "legacy-fixture")
        precondition(legacy.ragComputeApiKey == "legacy-fixture")
        legacy.ragComputeApiKey = "corrected-compute-fixture"
        precondition(AppSettings().ragApiKey == "legacy-fixture")
        precondition(AppSettings().ragComputeApiKey == "corrected-compute-fixture")
        precondition(ServiceCredentials(agent: "", inference: "", ragData: "", ragCompute: "")
            .environment.isEmpty)
        let cleared = ServiceCredentials(agent: "", inference: "", ragData: "", ragCompute: "")
            .applying(to: env.merging(["PATH": "/synthetic/bin"]) { a, _ in a })
        precondition(cleared == ["PATH": "/synthetic/bin"])
        print("service credentials: independent edges, legacy restore, explicit clear, synthetic reload passed")
    }
}
