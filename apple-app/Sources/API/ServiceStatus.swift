import Foundation

/// Only the backend API was verified; inference and knowledge capabilities are separate.
enum BackendAccess: Equatable {
    case authenticated
    case unauthorized
    case reachable
    case offline
}

/// Shared decoding of the data service's authoritative maintenance status.
struct RagRetierStatus: Decodable {
    struct TierInfo: Decodable {
        let tier: String?
    }
    struct RebuildInfo: Decodable {
        let state: String
        let docsTotal: Int?
        let docsDone: Int?
        let docsFailed: Int?
        let failedDocs: [String]?
        let staleTotal: Int?
        let staleRestored: Int?
        let startedAt: String?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case state, error
            case docsTotal = "docs_total", docsDone = "docs_done", docsFailed = "docs_failed"
            case failedDocs = "failed_docs", staleTotal = "stale_total", staleRestored = "stale_restored"
            case startedAt = "started_at"
        }
    }
    let store: TierInfo?
    let compute: TierInfo?
    let mismatch: Bool?
    let interrupted: Bool?
    let needsRebuild: Bool?
    let computeError: String?
    let rebuild: RebuildInfo
    enum CodingKeys: String, CodingKey {
        case store, compute, mismatch, interrupted, rebuild
        case needsRebuild = "needs_rebuild", computeError = "compute_error"
    }

    enum Availability: Equatable { case unknown, computeOffline, rebuilding, needsRebuild, reachable }
    var availability: Availability {
        if rebuild.state == "running" { return .rebuilding }
        if computeError != nil { return .computeOffline }
        if needsRebuild == true { return .needsRebuild }
        return compute == nil ? .unknown : .reachable
    }
}
