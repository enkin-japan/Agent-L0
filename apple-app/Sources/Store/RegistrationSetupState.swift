import Foundation
import Combine

/// Two independent read-only projections for the registration settings panel.
/// An unavailable tool catalog must not hide a usable pending queue (or vice versa).
@MainActor
final class RegistrationSetupState: ObservableObject {
    @Published private(set) var items: [RegistrationItem]?
    @Published private(set) var tools: [ApiToolItem] = []
    @Published private(set) var queueError: String?
    @Published private(set) var toolsError: String?
    @Published private(set) var loadingQueue = false
    @Published private(set) var loadingTools = false
    private(set) var client: AgentClient?
    private var requestID = UUID()
    private var active = false

    func isCurrent(_ client: AgentClient) -> Bool { active && self.client === client }
    func invalidate() { active = false; requestID = UUID() }
    func removePending(_ id: String) { items?.removeAll { $0.id == id } }

    func reload(client: AgentClient?, currentClient: @escaping () -> AgentClient?) async {
        let id = UUID()
        requestID = id
        active = true
        if self.client !== client { items = nil; tools = []; queueError = nil; toolsError = nil }
        self.client = client
        guard let client else {
            loadingQueue = false; loadingTools = false
            queueError = L("尚未配置可用连接。")
            toolsError = queueError
            return
        }
        let operation = ApiToolOperation(client: client) {
            self.active && self.requestID == id && currentClient() === client
        }
        loadingQueue = true; loadingTools = true
        async let queue: Void = loadQueue(operation)
        async let tools: Void = loadTools(operation)
        _ = await (queue, tools)
    }

    private func loadQueue(_ operation: ApiToolOperation) async {
        do {
            items = try await operation.perform { try await $0.listRegistrations() }
            queueError = nil
            loadingQueue = false
        } catch is CancellationError { return }
        catch { queueError = error.localizedDescription; loadingQueue = false }
    }

    private func loadTools(_ operation: ApiToolOperation) async {
        do {
            tools = try await operation.perform { try await $0.listApiTools().tools }
            toolsError = nil
            loadingTools = false
        } catch is CancellationError { return }
        catch { toolsError = error.localizedDescription; loadingTools = false }
    }
}
