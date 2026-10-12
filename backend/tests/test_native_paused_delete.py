"""R11 shared discard fixtures through full production native Store/API/SSE."""
from pathlib import Path
import subprocess
import pytest
from .test_native_retry_transport import compile_retry, pytestmark
ROOT=Path(__file__).resolve().parents[2]
@pytest.fixture(scope="module")
def native_delete(tmp_path_factory):
    tmp=tmp_path_factory.mktemp("native-discard")
    header=(ROOT/"apple-app/Tests/recovery_transport_test.swift").read_text().split("@main struct RecoveryTests")[0]
    harness=tmp/"discard.swift"
    harness.write_text(header + r'''@main struct Main {
    @MainActor static func wait(_ value: () -> Bool) async {
        for _ in 0..<500 { if value() {return};try? await Task.sleep(nanoseconds:5_000_000) }
        check(false,"timed out actual store discard")
    }
    static func sse(_ t: RecoveryTransport,_ values:[[String:Any]]) {
        let bytes=values.map { "data: "+String(data:try! JSONSerialization.data(withJSONObject:$0),encoding:.utf8)!+"\n\n" }.joined()
        t.deliver(Data(bytes.utf8))
    }
    @MainActor static func main() async throws {
        let raw=try String(contentsOfFile:CommandLine.arguments[1],encoding:.utf8)
        let events=try raw.split(separator:"\n").map {try JSONSerialization.jsonObject(with:Data($0.utf8)) as! [String:Any]}
        let kept=events[2]["data"]!
        RecoveryTransport.handler={t in
            let p=t.request.url!.path
            if p.hasSuffix("/history") {t.respond(["status":true,"session_id":"r11","turns":[kept],"replay_after":0])}
            else if p.hasSuffix("/turn") {t.respond(["status":true,"turn":NSNull(),"last_seq":0])}
            else if p.hasSuffix("/events") {RecoveryTransport.hold(t);t.deliver(Data())}
            else if p.hasSuffix("/deck") {t.respond([:],status:404)}
            else {t.respond(["status":true,"sessions":[],"skills":[]])}
        }
        let store=AgentStore(settings:AppSettings());store.select(sessionId:"r11")
        await wait({RecoveryTransport.matching("/events").count==1})
        let stream=RecoveryTransport.takeHeld()
        sse(stream,Array(events.dropFirst(3).dropLast()))
        await wait({store.messages.count==3 && (store.isSuspended || store.pendingTools != nil)})
        if CommandLine.arguments[2]=="live" {sse(stream,[events.last!])}
        else {sse(stream,[["seq":9,"ts":1,"turn_id":"t_discard","type":"turn_ended","data":["status":"cancelled","discarded":true,"turn_index":900]]])}
        await wait({store.messages.count==2 && !store.isGenerating})
        check(store.messages.map(\.text)==["保留的输入","保留的答案"],"discard retains only archived original")
        check(store.canonicalTurns.count==1 && store.messages.allSatisfy{$0.turnIndex==0},"discard does not archive or change canonical count")
        check(store.pendingTools==nil && !store.isSuspended && !store.isGenerating,"paused state cleared")
        check(store.streamText.isEmpty && store.liveSteps.isEmpty,"preview retracted")
        sse(stream,[["seq":10,"ts":1,"turn_id":"next","type":"turn_queued","data":["prompt":"next"]],["seq":11,"ts":1,"turn_id":"next","type":"turn_ended","data":["status":"success","prompt":"next","response":[["role":"assistant","content":"next answer"]]]]])
        await wait({store.messages.contains{$0.text=="next answer"}})
        check(store.messages.suffix(2).allSatisfy{$0.turnIndex==1},"next canonical index unchanged by discard")
        print("PASS full Store discard");exit(0)
    }
}
''')
    return compile_retry(tmp,harness=harness)
@pytest.mark.parametrize("fixture",["012-discard-paused-approval","013-discard-paused-suspended"])
@pytest.mark.parametrize("mode",["live","replay"])
def test_native_discard(native_delete,fixture,mode):
    result=subprocess.run([str(native_delete),str(ROOT/f"protocol/fixtures/{fixture}.events.jsonl"),mode],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
