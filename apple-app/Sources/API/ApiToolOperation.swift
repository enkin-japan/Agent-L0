import Foundation

/// Settings operations belong to the connection on which their form was opened.
/// A delayed response may not report success or open a form on another backend.
@MainActor
struct ApiToolOperation {
    let client: AgentClient?
    let isCurrent: () -> Bool

    static func editor(client: AgentClient?, connectionIsCurrent: @escaping () -> Bool,
                       presentationIsCurrent: @escaping () -> Bool) -> ApiToolOperation {
        ApiToolOperation(client: client) { connectionIsCurrent() && presentationIsCurrent() }
    }

    func perform<T>(_ action: (AgentClient) async throws -> T) async throws -> T {
        guard let client, isCurrent() else { throw CancellationError() }
        do {
            let result = try await action(client)
            guard isCurrent(), !Task.isCancelled else { throw CancellationError() }
            return result
        } catch {
            guard isCurrent(), !Task.isCancelled else { throw CancellationError() }
            throw error
        }
    }
}
