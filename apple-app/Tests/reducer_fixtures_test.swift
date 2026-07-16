// 协议夹具回放测试(PROTOCOL.md §5):与 web 端 vitest 读同一批 protocol/fixtures,
// 逐行归约断言终态 deep-equal——两端行为一致性的可执行标准。
// 运行:./run_reducer_fixtures.sh(swiftc 组装 JSONValue+ProtocolModels+TurnReducer)
import Foundation

@main
struct ReducerFixturesTest {
    static func main() throws {
        let args = CommandLine.arguments
        let dir = args.count > 1 ? args[1] : "../../protocol/fixtures"
        let fm = FileManager.default
        let names = try fm.contentsOfDirectory(atPath: dir)
            .filter { $0.hasSuffix(".events.jsonl") }
            .map { String($0.dropLast(".events.jsonl".count)) }
            .sorted()
        guard !names.isEmpty else {
            print("FAIL: \(dir) 里没有夹具"); exit(1)
        }

        var failed = 0
        for name in names {
            let eventsRaw = try String(contentsOfFile: "\(dir)/\(name).events.jsonl", encoding: .utf8)
            let expectedRaw = try Data(contentsOf: URL(fileURLWithPath: "\(dir)/\(name).expected.json"))
            let expected = try JSONDecoder().decode(JSONValue.self, from: expectedRaw)

            var state = NormalizedState()
            for line in eventsRaw.split(separator: "\n") where !line.trimmingCharacters(in: .whitespaces).isEmpty {
                let env = try JSONDecoder().decode(EventEnvelope.self, from: Data(line.utf8))
                state = TurnReducer.reduce(state, env)
            }
            let actual = state.toJSONValue()
            if actual == expected {
                print("✓ \(name)")
            } else {
                failed += 1
                print("✗ \(name)")
                let enc = JSONEncoder(); enc.outputFormatting = [.sortedKeys]
                print("  expected:", String(data: (try? enc.encode(expected)) ?? Data(), encoding: .utf8) ?? "")
                print("  actual:  ", String(data: (try? enc.encode(actual)) ?? Data(), encoding: .utf8) ?? "")
            }
        }
        if failed > 0 { print("== \(failed) FAILED =="); exit(1) }
        print("== ALL \(names.count) FIXTURES PASS ==")
    }
}
