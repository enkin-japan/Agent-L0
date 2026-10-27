import Foundation

/// Only the backend API was verified; inference and knowledge capabilities are separate.
enum BackendAccess: Equatable {
    case authenticated
    case unauthorized
    case reachable
    case offline
}

/// Readiness reported by the backend; absent/unknown fields never imply readiness.
/// `probing` / `unreachable` / `unauthorized` come only from `compute_node_state`
/// (Contract H); an older backend that omits it keeps the compute_state mapping.
enum ComputeAvailability: Equatable {
    case unknown, starting, ready, unavailable, unsupported
    case probing, unreachable, unauthorized
}

struct BackendHealth: Decodable {
    let computeReady: Bool?
    let computeState: String?
    /// Contract H (build 78): absent on older backends, so every one stays optional.
    var engineReady: Bool? = nil
    var computeNodeState: String? = nil
    var ragState: String? = nil
    enum CodingKeys: String, CodingKey {
        case computeReady = "compute_ready", computeState = "compute_state"
        case engineReady = "engine_ready", computeNodeState = "compute_node_state", ragState = "rag_state"
    }
}

struct BackendConnectionStatus {
    let access: BackendAccess
    let health: BackendHealth?
    var compute: ComputeAvailability {
        guard access == .authenticated else { return .unknown }
        guard let health else { return .unknown }
        // A stopping/stopped assembly is not usable whatever the last node probe said.
        if health.computeState == "stopping" || health.computeState == "stopped" { return .unavailable }
        // Latest probe of the remote inference node. "local" (not remote) and nil/unknown
        // values fall through: there the assembly's compute_state is the right signal.
        switch health.computeNodeState {
        case "ready": return .ready
        case "probing": return .probing
        case "unreachable": return .unreachable
        case "unauthorized": return .unauthorized
        default: break
        }
        if health.computeReady == true { return .ready }
        if health.computeReady == nil, health.computeState == nil { return .unsupported }
        switch health.computeState {
        case "starting": return .starting
        case "retrying", "unavailable": return .unavailable
        default: return health.computeReady == false ? .unavailable : .unknown
        }
    }

    /// Authenticated, but the backend says its engine is not installed yet.
    /// nil field (older backend) never reports "starting".
    var engineStarting: Bool {
        access == .authenticated && health?.engineReady == false
    }

    /// Data-plane qualifier for the 「后端启动中」 label: only the two failure values the
    /// backend reports are surfaced; "ready" / "probing" / "skipped" / unknown ⇒ nil.
    var ragNotice: String? {
        switch health?.ragState {
        case "unreachable": return "unreachable"
        case "unauthorized": return "unauthorized"
        default: return nil
        }
    }

    /// Still coming up: authenticated and either the engine is not installed or the first
    /// node probe has not answered. Settled failures (unreachable / unauthorized) and
    /// older backends that omit the fields are not "starting".
    var stillStarting: Bool {
        access == .authenticated && (health?.engineReady == false || health?.computeNodeState == "probing")
    }

    /// Monitor cadence: 2 s while offline or still starting; 15 s for every settled state.
    var pollIntervalSeconds: UInt64 {
        if access == .offline || stillStarting { return 2 }
        return 15
    }
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
