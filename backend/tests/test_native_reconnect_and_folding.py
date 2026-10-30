"""Native client: reconnect resume, approval policy on iPhone, mid-turn folding, receipt dedupe.

Sections (2026-10-30):
  A1  attachLoop resumes a dropped stream from the consumed cursor (PROTOCOL §2.4/§2.5);
      /turn re-anchoring only on first attach or a rejected cursor, seeding the preview from
      the snapshot instead of replaying the turn; replayed turn_queued never adds a 2nd bubble.
  A2  ApprovalPolicySection is cross-platform and listed on the iOS settings screen.
  A3  mid-turn assistant text (followed by tool steps, not the last text) is a 「中途说明」 note
      folded into the execution group; the live preview moves streamed text into the steps area.
  A4  cancelled-turn receipts merge into the response rows they describe (one per call_id).

Two layers: source pins (regex over Swift, mutated on in-memory copies) and a behavioural
harness that compiles the production Store/API/SSE with a synthetic URLProtocol (no network,
no services). Behavioural mutants are compiled from temp copies; sources are never rewritten.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "apple-app/Sources"


def _strip(text):
    return re.sub(r"//[^\n]*", "", text)


def _read(rel):
    return _strip((SRC / rel).read_text())


def _expect_red(check, *args):
    try:
        check(*args)
    except AssertionError:
        return
    raise AssertionError("mutant survived the pin")


def _mutate(text, old, new):
    assert text.count(old) == 1, f"mutation anchor not unique/missing: {old!r}"
    return text.replace(old, new)


# ---------------------------------------------------------------- A1 reconnect

def _attach_loop(store):
    m = re.search(r"private func attachLoop\(.*?\n    \}\n", store, re.S)
    assert m, "attachLoop missing"
    return m.group(0)


def assert_reconnect_contract(store):
    loop = _attach_loop(store)
    # every iteration starts from the consumed cursor
    assert re.search(r"\n\s*var after = cursor\n", loop), "resume must start from the consumed cursor"
    assert "if !anchored || cursorRejected {" in loop, "/turn re-anchoring must be limited to first attach or a rejected cursor"
    first = re.search(r"if !anchored \{(.*?)\n                    \} else if let seeded = reanchorFromSnapshot\(ts\) \{(.*?)\n                    \} else \{", loop, re.S)
    assert first, "first-attach / rejected-cursor branches missing"
    assert "after = min(replayAfter ?? 0," in first.group(1), "first attach must keep the history lower bound"
    assert "anchored = true" in first.group(1)
    # outside the first-attach block nothing rewinds to replayAfter / first_seq
    rest = loop.replace(first.group(1), "")
    assert "replayAfter" not in rest and "firstSeq" not in rest, "reconnect path rewinds the cursor"
    assert "after = seeded" in first.group(2), "rejected cursor must re-anchor via the /turn snapshot"
    catch = loop[loop.index("} catch {"):]
    assert "anchored = false" not in catch, "a transport drop must not drop the anchor (it would rewind)"
    assert re.search(r"if error is AttachCursorRejected \|\| Self\.isCursorRejection\(error\) \{ cursorRejected = true \}", catch), \
        "rejected-cursor detection missing"
    assert re.search(r"env\.type == \"stream_attached\".*?last < Double\(after\).*?throw AttachCursorRejected\(\)", loop, re.S), \
        "stream_attached last_seq behind the cursor must count as a rejected cursor"
    assert re.search(r"\(400\.\.<500\)\.contains\(code\) && code != 404", store), "4xx (except 404) = rejected cursor"
    # snapshot seeding: same turn → partial_text/partial_thinking/task, resume after its last_seq
    re_anchor = re.search(r"private func reanchorFromSnapshot\(.*?\n    \}\n", store, re.S)
    assert re_anchor, "reanchorFromSnapshot missing"
    body = re_anchor.group(0)
    assert "turnId == liveTurnId" in body and "seedPreview(from: turn)" in body
    assert "return turn.lastSeq ?? ts.lastSeq ?? cursor" in body, "seeded re-anchor must not rewind to first_seq"
    assert "firstSeq" not in body
    seed = re.search(r"private func seedPreview\(from turn: TurnStateSnapshot\) \{.*?\n    \}\n", store, re.S)
    assert seed, "seedPreview missing"
    for field in ("turn.partialText", "turn.partialThinking", "turn.task"):
        assert field in seed.group(0), f"preview not seeded from {field}"


def assert_bubble_dedupe(store):
    queued = re.search(r"private func onTurnQueued\(_ env: EventEnvelope\) \{.*?\n    \}\n", store, re.S)
    assert queued, "onTurnQueued missing"
    body = queued.group(0)
    i, j = body.find("let previousLiveTurnId = liveTurnId"), body.find("liveTurnId = env.turnId")
    assert 0 <= i < j, "previous live turn must be captured before it is overwritten"
    guard = re.search(r"\} else if let existing = liveUserMessageId, previousLiveTurnId != nil, previousLiveTurnId == env\.turnId,\s*"
                      r"messages\.contains\(where: \{ \$0\.id == existing \}\) \{\s*\} else \{", body)
    assert guard, "replayed turn_queued dedupe guard missing"
    assert body.index(guard.group(0)) < body.index("messages.append(message)"), "dedupe must precede the append"


def test_reconnect_contract():
    store = _read("Store/AgentStore.swift")
    assert_reconnect_contract(store)
    assert_bubble_dedupe(store)


def test_mutation_resume_uses_replay_after_fails():
    store = _read("Store/AgentStore.swift")
    _expect_red(assert_reconnect_contract,
                _mutate(store, "                var after = cursor\n", "                var after = replayAfter ?? cursor\n"))


def test_mutation_drop_clears_anchor_fails():
    store = _read("Store/AgentStore.swift")
    old = "                if case AgentError.http(404, _) = error { return }\n"
    _expect_red(assert_reconnect_contract, _mutate(store, old, "                anchored = false\n" + old))


def test_mutation_reanchor_rewinds_fails():
    store = _read("Store/AgentStore.swift")
    _expect_red(assert_reconnect_contract, _mutate(store, "return turn.lastSeq ?? ts.lastSeq ?? cursor",
                                                   "return (turn.firstSeq ?? 1) - 1"))


def test_mutation_dedupe_removed_fails():
    store = _read("Store/AgentStore.swift")
    m = re.search(r"\} else if let existing = liveUserMessageId, previousLiveTurnId != nil.*?\{\s*\} else \{", store, re.S)
    assert m
    _expect_red(assert_bubble_dedupe, store.replace(m.group(0), "} else {"))


# ---------------------------------------------------------------- A2 approval policy on iPhone

def _if_stack_at(text, index):
    """#if stack (innermost last) at a character offset; entries are (condition, in_else)."""
    stack = []
    for line in text[:index].split("\n"):
        s = line.strip()
        if s.startswith("#if "):
            stack.append([s[4:].strip(), False])
        elif s.startswith("#else") and stack:
            stack[-1][1] = True
        elif s.startswith("#endif") and stack:
            stack.pop()
    return stack


def assert_policy_cross_platform(settings):
    decl = re.search(r"\n(?:private )?struct ApprovalPolicySection: View \{", settings)
    assert decl, "ApprovalPolicySection missing"
    assert not decl.group(0).lstrip().startswith("private"), "section must be visible to both platform branches"
    assert not any(cond == "os(macOS)" and not in_else for cond, in_else in _if_stack_at(settings, decl.start())), \
        "ApprovalPolicySection is wrapped in #if os(macOS)"
    ios = settings.find('ApprovalPolicySection(title: L("审批策略"))')
    assert ios >= 0, "iOS settings screen does not list the 审批策略 section"
    stack = _if_stack_at(settings, ios)
    assert any((c == "os(macOS)" and e) or (c == "os(iOS)" and not e) for c, e in stack), \
        "审批策略 section is not in the iOS branch"
    conn = settings.find("connectionSections", settings.rfind("Form {", 0, ios))
    assert 0 <= conn < ios, "section must sit under 连接设置"
    assert 'ApprovalPolicySection(title: L("权限"))' in settings, "macOS 权限 tab unchanged"
    body = settings[decl.start():]
    assert ".task(id: store.client.map { ObjectIdentifier($0) }) { await load() }" in body, "GET on appear"
    assert "if current(cls) == mode { mirror(cls, previous) }" in body, "picker reverts on PATCH failure"
    assert 'L("权限设置保存失败：") + Self.serverMessage(error)' in body, "server message shown inline"


def test_policy_section_contract():
    assert_policy_cross_platform(_read("Views/SettingsView.swift"))


def test_mutation_policy_back_in_macos_only_fails():
    settings = _read("Views/SettingsView.swift")
    m = re.search(r"\nstruct ApprovalPolicySection: View \{", settings)
    mutant = settings[:m.start()] + "\n#if os(macOS)" + settings[m.start():] + "\n#endif\n"
    _expect_red(assert_policy_cross_platform, mutant)


def test_mutation_policy_missing_on_ios_fails():
    settings = _read("Views/SettingsView.swift")
    _expect_red(assert_policy_cross_platform,
                _mutate(settings, '                ApprovalPolicySection(title: L("审批策略"))\n', ""))


# ---------------------------------------------------------------- A3 folding

def assert_folding_contract(chat, store, view):
    assert re.search(r"let lastTextIndex = response\.lastIndex \{ msg in\s*msg\.role == \"assistant\" && \(msg\.toolCalls \?\? \[\]\)\.isEmpty", chat), \
        "last assistant text index missing"
    assert re.search(r"if let lastStepIndex, responseIndex < lastStepIndex, responseIndex != lastTextIndex \{\s*"
                     r"out\.append\(\.note\(content, thoughts: msg\.thoughts\)\)\s*\} else \{\s*out\.append\(\.assistant\(", chat), \
        "text followed by a tool step (and not the last text) must become a note"
    assert "ToolStep(id: $0.id, label: L(\"中途说明\")" in chat and "isNote: true" in chat, "note not folded into the group"
    assert "invocationCount(run.filter { $0.kind == .tool })" in chat, "notes must not count as steps"
    assert re.search(r"if env\.type == \"tool_started\" \{[\s\S]{0,400}?foldStreamedTextIntoNote\(beforeStep: i\)", store), \
        "live text not folded on tool start (and only on tool_started — never on tool_call_pending)"
    assert not re.search(r"\"tool_call_pending\"[^\n]*\{\s*foldStreamedTextIntoNote", store), "must not fold while awaiting approval"
    fold = re.search(r"private func foldStreamedTextIntoNote\(beforeStep: Int\) \{.*?\n    \}\n", store, re.S)
    assert fold and "liveNotes.append(" in fold.group(0) and 'streamText = ""' in fold.group(0), "live text must move, not copy"
    assert "ProcessNoteRow(text: text)" in view and "store.liveNotes" in view, "live preview does not render notes"
    assert "DisclosureGroup {" in view[view.find("private var streamingPreview"):], "thinking disclosure kept"


def _folding_sources():
    return _read("Store/ChatMessage.swift"), _read("Store/AgentStore.swift"), _read("Views/ChatView.swift")


def test_folding_contract():
    assert_folding_contract(*_folding_sources())


def test_mutation_fold_condition_inverted_fails():
    chat, store, view = _folding_sources()
    _expect_red(assert_folding_contract, _mutate(chat, "responseIndex < lastStepIndex", "responseIndex > lastStepIndex"), store, view)


def test_mutation_last_text_guard_removed_fails():
    chat, store, view = _folding_sources()
    _expect_red(assert_folding_contract, _mutate(chat, ", responseIndex != lastTextIndex {", " {"), store, view)


# ---------------------------------------------------------------- A4 receipts

def assert_receipt_dedupe(chat):
    assert "for item in mergeReceipts(facts.items, into: &out) {" in chat, "receipts appended without merging"
    merge = re.search(r"fileprivate static func mergeReceipts\(.*?\n    \}\n", chat, re.S)
    assert merge, "mergeReceipts missing"
    body = merge.group(0)
    assert 'seen.insert(item.type + "\\u{1F}" + callId).inserted' in body, "duplicate (call_id, type) receipts not collapsed"
    assert "receipts[$0].callId == callId" in body, "started/result not paired by call_id"
    assert re.search(r"if !started, let existing = out\.firstIndex\(where: \{ isRow\(\$0, \.result, item\.name\) && sameContent", body), \
        "returns must be matched by content"
    assert "pending.count == open.count" in body and "isRow(out[$0], .call, name)" in body, "call fallback must require equal counts"
    assert "isRow(out[$0], .result, name)" not in body, "no ordinal guessing for returns"
    assert "return receipts.indices.filter { !merged.contains($0) }" in body, "only unmatched receipts may append"


def test_receipt_dedupe_contract():
    assert_receipt_dedupe(_read("Store/ChatMessage.swift"))


def test_mutation_receipt_dedupe_removed_fails():
    chat = _read("Store/ChatMessage.swift")
    _expect_red(assert_receipt_dedupe, _mutate(chat, "for item in mergeReceipts(facts.items, into: &out) {",
                                               'for item in facts.items where item.type == "tool_started" || item.type == "tool_result" {'))


# ---------------------------------------------------------------- behavioural harness

HARNESS = r'''
import Foundation
final class RecoveryTransport: URLProtocol {
    static let lock = NSLock()
    static var handler: (RecoveryTransport) -> Void = { _ in fatalError("unconfigured") }
    static var requests: [URLRequest] = [], held: [RecoveryTransport] = []
    static func matching(_ suffix: String) -> [URLRequest] { lock.lock(); defer { lock.unlock() }; return requests.filter { $0.url!.path.hasSuffix(suffix) } }
    static func hold(_ t: RecoveryTransport) { lock.lock(); held.append(t); lock.unlock() }
    static var heldCount: Int { lock.lock(); defer { lock.unlock() }; return held.count }
    static func popHeld() -> RecoveryTransport { lock.lock(); defer { lock.unlock() }; return held.removeFirst() }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.lock.lock(); Self.requests.append(request); Self.lock.unlock(); Self.handler(self) }
    override func stopLoading() {}
    private var headersSent = false
    func respond(_ body: Any, status: Int = 200) {
        deliver(try! JSONSerialization.data(withJSONObject: body), status: status, type: "application/json", finish: true)
    }
    func deliver(_ data: Data, status: Int = 200, type: String = "text/event-stream", finish: Bool = false) {
        if !headersSent {
            headersSent = true
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
                headerFields: ["Content-Type": type])!, cacheStoragePolicy: .notAllowed)
        }
        if !data.isEmpty { client?.urlProtocol(self, didLoad: data) }
        if finish { client?.urlProtocolDidFinishLoading(self) }
    }
    func drop() { client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost)) }
    var after: String { request.url!.query ?? "" }
}
func check(_ condition: @autoclosure () -> Bool, _ message: String) { if !condition() { print("FAIL: \(message)"); exit(1) } }
func frames(_ values: [[String: Any]]) -> Data {
    Data(values.map { "data: " + String(data: try! JSONSerialization.data(withJSONObject: $0), encoding: .utf8)! + "\n\n" }.joined().utf8)
}
func ev(_ seq: Int, _ type: String, _ data: [String: Any] = [:]) -> [String: Any] {
    ["seq": seq, "turn_id": "t1", "ts": Double(seq), "type": type, "data": data]
}
func attached(_ last: Int) -> [String: Any] {
    ["seq": 0, "turn_id": NSNull(), "ts": 0.0, "type": "stream_attached", "data": ["v": 1, "last_seq": last, "generation": "g"]]
}
func turn(_ value: [String: Any]) -> Turn {
    try! JSONDecoder().decode(Turn.self, from: JSONSerialization.data(withJSONObject: value))
}
@main struct ReconnectFoldingTests {
    @MainActor static func wait(_ condition: () -> Bool, _ message: String) async {
        for _ in 0..<800 { if condition() { return }; try? await Task.sleep(nanoseconds: 10_000_000) }
        check(false, "timeout \(message)")
    }
    @MainActor static func nextEvents() async -> RecoveryTransport {
        await wait({ RecoveryTransport.heldCount > 0 }, "events attachment")
        return RecoveryTransport.popHeld()
    }
    @MainActor static func main() async throws {
        let mode = CommandLine.arguments[1]
        if mode == "fold" {
            let rows = ChatMessage.from(turn: turn(["status": "success", "prompt": "q", "response": [
                ["role": "assistant", "content": "Let me check"],
                ["role": "assistant", "tool_calls": [["name": "web", "arguments": ["q": "x"]]]],
                ["role": "tool", "tool_responses": [["name": "web", "response": "r"]]],
                ["role": "assistant", "content": "Answer"]]]), turnIndex: 0)
            check(rows.map(\.kind) == [.user, .note, .tool, .tool, .assistant], "mid-turn text is a note, last text a bubble: \(rows.map(\.kind))")
            let shown = ChatMessage.groupedForDisplay(rows)
            check(shown.map(\.kind) == [.user, .toolGroup, .assistant], "note folds into the execution group")
            check(shown[1].toolSteps.first?.isNote == true && shown[1].toolInvocationCount == 1, "note is a group item, not a step")
            let pending = ChatMessage.from(turn: turn(["status": "cancelled", "prompt": "q", "response": [
                ["role": "assistant", "content": "Shall I delete it?"],
                ["role": "assistant", "tool_calls": [["name": "rm", "arguments": [:]]]]]]))
            check(pending.contains { $0.kind == .assistant && $0.text == "Shall I delete it?" }, "last text stays a bubble even before a step")
            let question = ChatMessage.from(turn: turn(["status": "success", "prompt": "q", "response": [
                ["role": "assistant", "tool_calls": [["name": "plan", "arguments": [:]]]],
                ["role": "tool", "tool_responses": [["name": "plan", "response": "ok"]]],
                ["role": "assistant", "content": "Which file?"]]]))
            check(question.last?.kind == .assistant, "a turn ending in a question keeps the bubble")
            print("PASS fold"); exit(0)
        }
        if mode == "receipts" {
            func receipt(_ seq: Int, _ type: String, _ name: String, _ call: String, _ detail: String, _ status: String? = nil, truncated: Bool = false) -> [String: Any] {
                var r: [String: Any] = ["seq": seq, "ts": Double(100 + seq), "type": type, "name": name, "call_id": call, "detail": detail]
                if let status { r["status"] = status }
                if truncated { r["truncated"] = true }
                return r
            }
            let response: [[String: Any]] = [
                ["role": "assistant", "tool_calls": [["name": "write_file", "arguments": ["path": "a.txt"]]]],
                ["role": "tool", "tool_responses": [["name": "write_file", "response": "wrote a.txt fully"]]]]
            let rows = ChatMessage.from(turn: turn(["status": "cancelled", "prompt": "q", "response": response,
                "execution_receipts": ["truncated": false, "items": [
                    receipt(1, "tool_started", "write_file", "c1", ""), receipt(2, "tool_result", "write_file", "c1", "wrote a", "ok", truncated: true),
                    receipt(2, "tool_result", "write_file", "c1", "wrote a", "ok", truncated: true),
                    receipt(3, "tool_started", "delete_file", "c2", "{}")]]]))
            let tools = rows.filter { $0.kind == .tool }
            check(tools.count == 3, "represented receipts merge, the unmatched one appends once: \(tools.map { $0.toolLabel ?? "" })")
            // 并入的 started 回执不另起行，但要在调用行标签上留「· 已开始」（执行已开始是安全信号）
            check(tools.filter { $0.toolLabel?.contains("已开始") == true }.map(\.stepName) == ["write_file", "delete_file"]
                  && tools[0].stepRole == .call, "merged started receipt marks the call row, no second row: \(tools.map { $0.toolLabel ?? "" })")
            check(tools[1].toolLabel == "write_file · 已返回" && tools[1].stepStatus == "ok" && tools[0].stepTimestamp == 101, "receipt facts land on the response rows")
            let denied: [[String: Any]] = [response[0], ["role": "tool", "tool_responses": [["name": "write_file", "response": "The user denied this tool call."]]]]
            let exact = ChatMessage.from(turn: turn(["status": "cancelled", "prompt": "q", "response": denied + response,
                "execution_receipts": ["truncated": false, "items": [receipt(5, "tool_started", "write_file", "c5", ""),
                    receipt(6, "tool_result", "write_file", "c5", "wrote a.txt fully", "ok")]]]))
            check(exact.filter { $0.kind == .tool }.count == 4, "exact result match merges even when counts differ")
            let exactTools = exact.filter { $0.kind == .tool }
            check(exactTools[2].stepTimestamp == 105 && exactTools[3].stepStatus == "ok", "started pairs with the call before its result by call_id")
            check(exactTools[0].stepTimestamp == nil && exactTools[1].stepStatus == nil, "denied call never inherits the later execution")
            print("PASS receipts"); exit(0)
        }
        // ---- store modes: synthetic transport, production AgentStore
        var turnCalls = 0
        RecoveryTransport.handler = { t in
            let path = t.request.url!.path
            if path.hasSuffix("/history") {
                t.respond(["status": true, "session_id": "A", "count": 0, "turns": [], "replay_after": 1, "generation": "g"])
            } else if path.hasSuffix("/turn") {
                RecoveryTransport.lock.lock(); turnCalls += 1; let n = turnCalls; RecoveryTransport.lock.unlock()
                var snap: [String: Any] = ["turn_id": "t1", "state": "running", "prompt": "hello", "first_seq": 2, "last_seq": 2]
                if n > 1 { snap["partial_text"] = "abcdef"; snap["partial_thinking"] = "hmm"; snap["last_seq"] = 9 }
                t.respond(["status": true, "turn": snap, "last_seq": snap["last_seq"]!, "generation": "g"])
            } else if path.hasSuffix("/events") { RecoveryTransport.hold(t) }
            else if path.hasSuffix("/deck") { t.respond([:], status: 404) }
            else if path.hasSuffix("/sessions") { t.respond(["status": true, "sessions": []]) }
            else { t.respond(["status": true]) }
        }
        let store = AgentStore(settings: AppSettings())
        func bubbles() -> Int { store.messages.filter { $0.kind == .user && $0.text == "hello" }.count }
        store.select(sessionId: "A")
        let first = await nextEvents()
        check(first.after == "after=1", "first attach keeps the history lower bound: \(first.after)")
        switch mode {
        case "resume":
            first.deliver(frames([attached(4), ev(2, "turn_queued", ["prompt": "hello"]), ev(3, "turn_started"),
                                  ev(4, "token_delta", ["text": "partial "])]))
            await wait({ store.streamText == "partial " }, "first stream rendered")
            first.drop()
            let second = await nextEvents()
            check(second.after == "after=4", "drop must resume from the consumed cursor, got \(second.after)")
            second.deliver(frames([attached(5), ev(5, "token_delta", ["text": "more"])]))
            await wait({ store.streamText == "partial more" }, "resumed stream continues the preview")
            check(bubbles() == 1, "one user bubble across the drop")
            second.deliver(frames([ev(6, "turn_ended", ["status": "success", "prompt": "hello", "turn_index": 0,
                                                         "response": [["role": "assistant", "content": "final"]]])]))
            await wait({ store.messages.contains { $0.text == "final" } }, "terminal")
            check(store.messages.map(\.text) == ["hello", "final"], "terminal once, no stray bubble: \(store.messages.map(\.text))")
        case "rejected":
            first.deliver(frames([attached(3), ev(2, "turn_queued", ["prompt": "hello"]), ev(3, "token_delta", ["text": "abc"])]))
            await wait({ store.streamText == "abc" }, "first stream rendered")
            first.drop()
            let second = await nextEvents()
            check(second.after == "after=3", "drop resumes from the consumed cursor")
            second.deliver(Data(), status: 400, finish: true)
            let third = await nextEvents()
            check(third.after == "after=9", "rejected cursor re-anchors at the snapshot's last_seq: \(third.after)")
            check(store.streamText == "abcdef" && store.thinkingText == "hmm", "preview seeded from /turn partials")
            check(bubbles() == 1, "seeded re-anchor adds no bubble")
            third.deliver(frames([attached(10), ev(10, "token_delta", ["text": "!"])]))
            await wait({ store.streamText == "abcdef!" }, "stream continues after the seed")
        case "fold-live":
            first.deliver(frames([attached(6), ev(2, "turn_queued", ["prompt": "hello"]), ev(3, "token_delta", ["text": "Looking up"]),
                                  ev(4, "tool_started", ["name": "web", "call_id": "c1"]),
                                  ev(5, "tool_result", ["call_id": "c1", "response": "r", "status": "ok"]),
                                  ev(6, "token_delta", ["text": "Final"])]))
            await wait({ store.streamText == "Final" }, "final answer streams fresh")
            check(store.liveNotes == [LiveNote(text: "Looking up", beforeStep: 0)], "streamed text moved into the steps area")
            check(store.liveStepItems.count == 1, "step kept")
        default: check(false, "unknown mode")
        }
        print("PASS " + mode); exit(0)
    }
}
'''

needs_swift = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")


def _compile(tmp, *, store_text=None, chat_text=None):
    store = SRC / "Store/AgentStore.swift"
    chat = SRC / "Store/ChatMessage.swift"
    if store_text is not None:
        store = tmp / "AgentStore.swift"; store.write_text(store_text)
    if chat_text is not None:
        chat = tmp / "ChatMessage.swift"; chat.write_text(chat_text)
    harness = tmp / "reconnect_folding_test.swift"
    harness.write_text(HARNESS)
    binary = tmp / "reconnect_folding"
    result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp / "modules"),
        *map(str, sorted((SRC / "API").glob("*.swift"))), *map(str, sorted((SRC / "Store").glob("Offline*.swift"))),
        str(SRC / "Store/TurnReducer.swift"), str(chat), str(store),
        str(ROOT / "apple-app/Tests/recovery_support.swift"), str(harness), "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


def _run(binary, mode):
    return subprocess.run([str(binary), mode], capture_output=True, text=True, timeout=30)


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return _compile(tmp_path_factory.mktemp("reconnect-folding"))


@needs_swift
@pytest.mark.parametrize("mode", ["resume", "rejected", "fold-live", "fold", "receipts"])
def test_native_behaviour(harness, mode):
    result = _run(harness, mode)
    assert result.returncode == 0, result.stdout + result.stderr


BEHAVIOUR_MUTANTS = {
    # the original defect: a transport drop clears the anchor and rewinds to first_seq-1
    "drop-rewinds": ("store", "                if case AgentError.http(404, _) = error { return }\n",
                     "                anchored = false\n                if case AgentError.http(404, _) = error { return }\n", "resume"),
    "fold-inverted": ("chat", "responseIndex < lastStepIndex", "responseIndex > lastStepIndex", "fold"),
    "receipts-undeduped": ("chat", "for item in mergeReceipts(facts.items, into: &out) {",
                           'for item in facts.items where item.type == "tool_started" || item.type == "tool_result" {', "receipts"),
}


@needs_swift
@pytest.mark.parametrize("name", sorted(BEHAVIOUR_MUTANTS))
def test_native_behaviour_mutant_goes_red(tmp_path, name):
    target, old, new, mode = BEHAVIOUR_MUTANTS[name]
    path = SRC / ("Store/AgentStore.swift" if target == "store" else "Store/ChatMessage.swift")
    mutated = _mutate(path.read_text(), old, new)
    binary = _compile(tmp_path, **({"store_text": mutated} if target == "store" else {"chat_text": mutated}))
    result = _run(binary, mode)
    assert result.returncode != 0, f"behavioural mutant {name} survived: {result.stdout}"
    assert "FAIL:" in result.stdout, result.stdout + result.stderr
