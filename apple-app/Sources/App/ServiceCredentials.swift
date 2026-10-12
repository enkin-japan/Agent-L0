import Foundation

/// Each service edge keeps its own credential. This value type never reads storage.
struct ServiceCredentials {
    var agent: String
    var inference: String
    var ragData: String
    var ragCompute: String

    var environment: [String: String] {
        var values = ["INFER_API_KEY": inference,
                      "RAG_API_KEY": ragData, "RAG_COMPUTE_API_KEY": ragCompute]
            .mapValues(Self.cleanKey)
            .filter { !$0.value.isEmpty }
        // The client and identity probe use the stored AGENT value verbatim.
        if !agent.isEmpty { values["AGENT_API_KEY"] = agent }
        return values
    }

    func applying(to inherited: [String: String]) -> [String: String] {
        var result = inherited
        for name in ["AGENT_API_KEY", "INFER_API_KEY", "RAG_API_KEY", "RAG_COMPUTE_API_KEY"] {
            result.removeValue(forKey: name)
        }
        result.merge(environment) { _, setting in setting }
        return result
    }

    /// Older installs stored both RAG credentials in one account. The settings
    /// owner persists this migration once; it never rotates the old value.
    static func restoredCompute(saved: String, legacyData: String, configured: Bool) -> String {
        configured || !saved.isEmpty ? saved : legacyData
    }

    static func cleanKey(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let m = s.range(of: #"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*="#,
                           options: .regularExpression) {
            s = String(s[m.upperBound...])
        }
        if s.count >= 2, let f = s.first, f == s.last, f == "\"" || f == "'" {
            s = String(s.dropFirst().dropLast())
        }
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
