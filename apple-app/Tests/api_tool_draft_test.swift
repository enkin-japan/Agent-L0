import Foundation

@main
struct ApiToolDraftTest {
    static func main() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let item = try JSONDecoder().decode(ApiToolItem.self, from: data)
        var draft = ApiToolDraft(item: item)
        precondition(draft.approval == "require")
        switch CommandLine.arguments[2] {
        case "key": draft.secret = "synthetic-secret"
        case "timeout": draft.timeout = "23"
        case "approval-default": draft.approval = "default"
        case "parameter-description", "empty-description", "false-description":
            draft.params[0].desc = "updated description"
        default: break
        }
        let encoded = try JSONEncoder().encode(draft.changes())
        print(String(decoding: encoded, as: UTF8.self))
    }
}
