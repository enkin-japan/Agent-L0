import Foundation
#if os(iOS)
import Network
#endif

/// Whether this device may mirror attachments automatically right now.
/// On iOS a cellular/hotspot path (`isExpensive`) or Low Data Mode
/// (`isConstrained`) carries history text only; media waits for a cheap path and
/// stays downloadable on demand. macOS has no metered-path concept to respect.
final class NetworkPathPolicy: @unchecked Sendable {
    static let shared = NetworkPathPolicy()

    #if os(iOS)
    private let monitor = NWPathMonitor()
    private let lock = NSLock()
    /// Restricted until the monitor reports an actual path: the first pass after
    /// launch must not mirror media on cellular while the answer is still unknown.
    private var restricted = true

    private init() {
        monitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            self.lock.lock()
            self.restricted = path.isExpensive || path.isConstrained
            self.lock.unlock()
        }
        monitor.start(queue: DispatchQueue(label: "dev.enkin.macagent.network-path"))
    }

    var allowsAutomaticMirroring: Bool {
        lock.lock(); defer { lock.unlock() }
        return !restricted
    }
    #else
    private init() {}
    var allowsAutomaticMirroring: Bool { true }
    #endif
}
