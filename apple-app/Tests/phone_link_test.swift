import Combine
import Foundation

// Compiled with the real Sources/App/PhoneLink.swift (macOS branch); only the
// settings object and the L10n lookup are replaced. No keychain, no tailscale CLI.
// MacDirectory (the multi-Mac list logic) is platform-neutral, so the iPhone's
// merge / routing rules are tested here as pure functions. The stateful iOS
// paths in PhoneLink (readSynced, select, remove, claim) are only type-checked
// by the Xcode build; they delegate every decision to these functions.
func L(_ zh: String) -> String { zh }

@MainActor
final class AppSettings: ObservableObject {
    static let localBackend = "http://127.0.0.1:14159"
    @Published var apiKey = ""
    var phoneSyncEnabled = true
    var baseURL: URL? = URL(string: "http://127.0.0.1:14159")
}

var failures = 0
func check(_ condition: Bool, _ label: String) {
    if !condition {
        failures += 1
        print("FAIL: \(label)")
    }
}

func json(_ text: String) -> Data { Data(text.utf8) }

@main
struct PhoneLinkTest {
    static func main() {
        let code = "AbCdEfGhIjKlMnOpQrStUv_-"

        // ---- PairingLink: the QR payload survives a round trip ----
        let base = URL(string: "http://m6.tail8d18fd.ts.net:14159")!
        let link = PairingLink(base: base, code: code)
        check(link != nil, "valid link builds")
        if let link {
            check(PairingLink(url: link.url) == link, "round trip through macagent://pair")
            check(link.url.scheme == "macagent" && link.url.host() == "pair", "link shape")
            check(link.host == "m6.tail8d18fd.ts.net", "host shown in the confirmation")
        }
        let https = PairingLink(url: URL(string: "macagent://pair?u=https://m6.tail8d18fd.ts.net&c=\(code)")!)
        check(https?.base.absoluteString == "https://m6.tail8d18fd.ts.net", "https link parses")

        // ---- PairingLink: anything else is refused before the user is even asked ----
        let refused = [
            "macagent://session/abc?u=https://m6.ts.net&c=\(code)",          // other deep link
            "macagent://pair?u=https://m6.ts.net",                            // no code
            "macagent://pair?c=\(code)",                                      // no address
            "macagent://pair?u=javascript:alert(1)&c=\(code)",                // not http(s)
            "macagent://pair?u=file:///etc/passwd&c=\(code)",
            "macagent://pair?u=https://user:pw@m6.ts.net&c=\(code)",          // credentials in URL
            "macagent://pair?u=https://m6.ts.net/api&c=\(code)",              // path
            "macagent://pair?u=https://m6.ts.net/?x=1&c=\(code)",             // query
            "macagent://pair?u=https://&c=\(code)",                           // no host
            "macagent://pair?u=https://m6.ts.net&c=short",                    // code too short
            "macagent://pair?u=https://m6.ts.net&c=\(String(repeating: "a", count: 65))",
            "macagent://pair?u=https://m6.ts.net&c=%E9%85%8D%E5%AF%B9%E7%A0%81%E9%85%8D%E5%AF%B9%E7%A0%81%E9%85%8D%E5%AF%B9", // non-ASCII
            "https://pair?u=https://m6.ts.net&c=\(code)",                     // wrong scheme
            "macagent://pair?u=https://evil.example.com&c=\(code)",           // not a tailnet name
            "macagent://pair?u=https://m6.ts.net.evil.com&c=\(code)",
            "macagent://pair?u=http://100.84.98.77:14159&c=\(code)",          // raw IP: never what serve hands out
            "macagent://pair?u=https://ts.net&c=\(code)",
        ]
        for text in refused {
            check(PairingLink(url: URL(string: text)!) == nil, "refused: \(text)")
        }
        check(PairingLink(base: base, code: code + "/") == nil, "code charset is strict")

        // ---- TailnetParse: the address the iPhone should use ----
        // Shape observed on the production Mac (2026-10-25): plain HTTP on tailnet port 14159.
        let httpServe = json("""
        {"TCP":{"14159":{"HTTP":true}},
         "Web":{"m6.tail8d18fd.ts.net:14159":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:14159"}}}}}
        """)
        check(TailnetParse.servedURL(httpServe)?.absoluteString == "http://m6.tail8d18fd.ts.net:14159",
              "http serve on 14159")
        let httpsServe = json("""
        {"TCP":{"443":{"HTTPS":true}},
         "Web":{"m6.tail8d18fd.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:14159/"}}}}}
        """)
        check(TailnetParse.servedURL(httpsServe)?.absoluteString == "https://m6.tail8d18fd.ts.net",
              "https on 443 drops the default port")
        let both = json("""
        {"TCP":{"443":{"HTTPS":true},"14159":{"HTTP":true}},
         "Web":{"m6.ts.net:14159":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:14159"}}},
                "m6.ts.net:443":{"Handlers":{"/":{"Proxy":"localhost:14159"}}}}}
        """)
        check(TailnetParse.servedURL(both)?.absoluteString == "https://m6.ts.net", "https preferred")
        let other = json("""
        {"TCP":{"443":{"HTTPS":true}},
         "Web":{"m6.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3000"}}}}}
        """)
        check(TailnetParse.servedURL(other) == nil, "a different local service is not the backend")
        check(TailnetParse.portInUse(other, "443"), "443 taken by the other service")
        check(!TailnetParse.portInUse(other, "14159"), "14159 free")
        check(TailnetParse.servedURL(json("{}")) == nil, "nothing served")
        check(TailnetParse.servedURL(json("not json")) == nil, "garbage")

        let status = json("""
        {"BackendState":"Running","CertDomains":["m6.tail8d18fd.ts.net"],
         "Self":{"DNSName":"m6.tail8d18fd.ts.net."}}
        """)
        check(TailnetParse.selfNode(status) == .init(name: "m6", running: true, canHTTPS: true), "self node")
        let stopped = json(#"{"BackendState":"Stopped","Self":{"DNSName":""}}"#)
        check(TailnetParse.selfNode(stopped)?.running == false, "stopped daemon")
        check(TailnetParse.selfNode(stopped)?.canHTTPS == false, "no cert domains")

        // ---- SharedKeychain: an unprovisioned build is told to use the QR code ----
        check(SharedKeychain.describe(errSecMissingEntitlement).contains("二维码"), "missing entitlement message")

        // ---- Multi-Mac: each Mac writes only its own item, tagged with its id ----
        let macA = "0b6c4c1e-77d2-4f0e-9a51-6f1e2d3c4b5a"
        let macB = "9f1d2e3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f"
        let now = Date(timeIntervalSince1970: 1_761_400_000)
        let out = MacDirectory.outgoing(url: "http://m6.ts.net:14159", ticket: "tA", mac: "m6", macId: macA, now: now)
        check(out.macId == macA, "outgoing item carries the Mac's id")
        check(out.key == nil && out.ticket == "tA", "outgoing item carries the request ticket, never the shared key")
        check(SharedKeychain.account(for: macA) == "connection." + macA, "per-Mac account")
        let encoded = try! JSONEncoder().encode(out)
        check(SharedKeychain.decode([(SharedKeychain.account(for: macA), encoded)]) == [out], "item round trip")
        check(out.sameContent(MacDirectory.outgoing(url: out.url, ticket: "tA", mac: "m6", macId: macA,
                                                     now: now.addingTimeInterval(60))),
              "timestamp alone is not a change")
        check(!out.sameContent(MacDirectory.outgoing(url: out.url, ticket: "tA2", mac: "m6", macId: macA, now: now)),
              "a rotated ticket is a change")

        func conn(_ url: String, mac: String, macId: String?, key: String? = nil, ticket: String? = nil) -> PhoneConnection {
            PhoneConnection(url: url, mac: mac, macId: macId, key: key, ticket: ticket, updated: nil)
        }
        func saved(_ id: String, macId: String?, url: String, source: ConnectionSource = .icloud,
                   credential: Credential = .deviceToken, name: String = "m6", custom: Bool = false) -> SavedMac {
            SavedMac(id: id, macId: macId, name: name, url: url, source: source, addedAt: now,
                     removedFromICloud: false, credential: credential, customName: custom)
        }

        // ---- Reading: an item must match its account; the 75 global item has no id ----
        func item(_ c: PhoneConnection) -> Data { try! JSONEncoder().encode(c) }
        let legacy = conn("http://m6.ts.net:14159", mac: "m6", macId: macA, key: "old")
        let legacy75 = Data(#"{"url":"http://m6.ts.net:14159","key":"old","mac":"m6"}"#.utf8)   // build 75's exact shape
        let decoded = SharedKeychain.decode([
            (SharedKeychain.legacyAccount, item(legacy)),                                     // 75: id ignored
            (SharedKeychain.account(for: macB), item(out)),                                   // A's body under B's name
            (SharedKeychain.account(for: macA), item(conn("u", mac: "m", macId: nil))),        // no id
            ("connection.bad", item(conn("u", mac: "m", macId: "bad"))),
            (SharedKeychain.account(for: macA), Data("garbage".utf8)),
        ])
        check(decoded.count == 1 && decoded.first?.macId == nil && decoded.first?.key == "old",
              "only the legacy item survives, without an id")
        check(SharedKeychain.decode([(SharedKeychain.legacyAccount, legacy75)]).first?.key == "old",
              "build 75 items still decode")

        // ---- The Mac deletes only its own 75 item ----
        let mine = conn("http://m6.ts.net:14159", mac: "m6", macId: nil, key: "k")
        check(MacDirectory.ownsLegacy(mine, name: "m6", url: "http://m6.ts.net:14159"), "own legacy by url+name")
        check(MacDirectory.ownsLegacy(mine, name: "", url: "http://m6.ts.net:14159"), "own legacy by url")
        check(MacDirectory.ownsLegacy(mine, name: "m6", url: "https://m6.ts.net"), "own legacy by name")
        let theirs = conn("http://studio.ts.net:14159", mac: "studio", macId: nil, key: "k")
        check(!MacDirectory.ownsLegacy(theirs, name: "m6", url: "http://m6.ts.net:14159"), "another Mac's legacy item stays")
        let unnamed = conn("http://x.ts.net", mac: "", macId: nil, key: "k")
        check(!MacDirectory.ownsLegacy(unnamed, name: "", url: "http://m6.ts.net:14159"), "empty names never match")
        var tagged = mine
        tagged.macId = macB
        check(!MacDirectory.ownsLegacy(tagged, name: "m6", url: mine.url), "a per-Mac item is not legacy")

        // ---- iPhone: merge keeps Macs apart ----
        var counter = 0
        func newId() -> String { counter += 1; return "saved-\(counter)" }
        let a = conn("http://m6.ts.net:14159", mac: "m6", macId: macA, ticket: "tA")
        let b = conn("http://studio.ts.net:14159", mac: "studio", macId: macB, ticket: "tB")
        let two = MacDirectory.merge([], synced: [a, b], hidden: [], now: now, newId: newId)
        check(two.macs.map(\.macId) == [macA, macB] && two.macs.allSatisfy { $0.source == .icloud },
              "two Macs, two entries")
        check(two.macs.allSatisfy { $0.credential == .none }, "a Mac read from iCloud still needs the Mac's approval")
        check(two.tickets == ["saved-1": "tA", "saved-2": "tB"] && two.keys.isEmpty,
              "each entry gets its own Mac's ticket and no key")

        // The current Mac (A) is never rewritten with another Mac's address or ticket.
        let current = saved("cur", macId: macA, url: "http://m6.ts.net:14159")
        let rotatedB = conn("https://studio.ts.net", mac: "studio", macId: macB, ticket: "tB2")
        let onlyB = MacDirectory.merge([current], synced: [rotatedB], hidden: [], now: now, newId: newId)
        var gone = current
        gone.removedFromICloud = true
        check(onlyB.macs.first == gone, "A untouched by B's item (only flagged as gone from iCloud)")
        check(onlyB.tickets["cur"] == nil && onlyB.keys["cur"] == nil, "A's credentials never replaced by B's")
        check(onlyB.macs.count == 2 && onlyB.macs[1].macId == macB, "B added beside A")
        // Same Mac changed its address / rotated its ticket: applied to that entry only, token kept.
        let movedA = conn("https://m6.ts.net", mac: "m6", macId: macA, ticket: "tA2")
        let healed = MacDirectory.merge([current], synced: [movedA, rotatedB], hidden: [], now: now, newId: newId)
        check(healed.macs[0].id == "cur" && healed.macs[0].url == "https://m6.ts.net" && healed.tickets["cur"] == "tA2"
              && healed.macs[0].credential == .deviceToken, "current Mac's new address is picked up in place")
        // A name the user chose on the iPhone survives iCloud updates.
        let renamed = saved("cur", macId: macA, url: a.url, name: "书房", custom: true)
        check(MacDirectory.merge([renamed], synced: [a], hidden: [], now: now, newId: newId).macs[0].name == "书房",
              "custom name kept")

        // 75 connection migrated without an id adopts it by address and keeps its shared key for the upgrade.
        let migrated = MacDirectory.migrate(url: " http://m6.ts.net:14159 ", source: .icloud, now: now, id: "old")!
        check(migrated.url == "http://m6.ts.net:14159" && migrated.name == "m6" && migrated.credential == .sharedKey,
              "migration trims, names, and marks the shared key for upgrade")
        check(MacDirectory.migrate(url: "  ", source: .manual, now: now, id: "x") == nil, "nothing to migrate")
        let adopted = MacDirectory.merge([migrated], synced: [a], hidden: [], now: now, newId: newId)
        check(adopted.macs.count == 1 && adopted.macs[0].id == "old" && adopted.macs[0].macId == macA
              && adopted.macs[0].credential == .sharedKey, "legacy entry adopts the Mac's id")

        // QR / manual entries are not silently overwritten, nor flagged.
        let paired = saved("qr", macId: macA, url: "http://m6.ts.net:14159", source: .qr)
        let keepQR = MacDirectory.merge([paired], synced: [movedA], hidden: [], now: now, newId: newId)
        check(keepQR.macs == [paired] && keepQR.keys.isEmpty && keepQR.tickets.isEmpty, "scanned connection kept as is")
        check(MacDirectory.merge([paired], synced: [], hidden: [], now: now, newId: newId).macs == [paired],
              "non-iCloud entry not flagged as removed")

        // A legacy synced item (75 Mac) matches by address only, and never replaces a device token.
        let legacyB = conn("http://studio.ts.net:14159", mac: "studio", macId: nil, key: "kL")
        let withLegacy = MacDirectory.merge([current], synced: [legacyB], hidden: [], now: now, newId: newId)
        check(withLegacy.keys["cur"] == nil && withLegacy.macs.count == 2 && withLegacy.macs[1].macId == nil
              && withLegacy.macs[1].credential == .sharedKey, "75 item from another Mac becomes its own entry")
        let tokenHolder = saved("tok", macId: nil, url: legacyB.url, name: "studio")
        let notDowngraded = MacDirectory.merge([tokenHolder], synced: [legacyB], hidden: [], now: now, newId: newId)
        check(notDowngraded.keys.isEmpty && notDowngraded.macs[0].credential == .deviceToken,
              "a stale shared key never replaces this device's token")

        // Deleted Macs stay deleted.
        let hiddenMerge = MacDirectory.merge([], synced: [a, legacyB],
                                             hidden: [macA, MacDirectory.hideKey(macId: nil, url: legacyB.url)],
                                             now: now, newId: newId)
        check(hiddenMerge.macs.isEmpty, "hidden Macs are not added back")

        // ---- Scanning / manual entry ----
        let byId = MacDirectory.upsert([current], url: "https://m6.ts.net", macId: macA, source: .qr,
                                       credential: .deviceToken, now: now, newId: newId)
        check(byId.id == "cur" && byId.macs.count == 1 && byId.macs[0].source == .qr
              && byId.macs[0].url == "https://m6.ts.net", "scan of a known Mac updates it")
        let byURL = MacDirectory.upsert([migrated], url: migrated.url, macId: nil, source: .manual,
                                        credential: .sharedKey, now: now, newId: newId)
        check(byURL.id == "old" && byURL.macs.count == 1 && byURL.macs[0].source == .manual, "same address updates")
        let fresh = MacDirectory.upsert([current], url: "http://new.ts.net", macId: nil, source: .manual,
                                        credential: .sharedKey, now: now, newId: newId)
        check(fresh.macs.count == 2 && fresh.id != "cur" && fresh.macs[1].credential == .sharedKey, "new address appends")

        // ---- Routing a notification / approval back to its Mac ----
        let list = [current, saved("b", macId: macB, url: b.url, name: "studio")]
        check(MacDirectory.route(src: macB, macs: list, current: "cur") == "b", "routed to the source Mac")
        check(MacDirectory.route(src: "unknown-mac-id", macs: list, current: "cur") == "cur", "unknown source: current")
        check(MacDirectory.route(src: nil, macs: list, current: "cur") == "cur", "old backend: current")

        // Approvals are stricter: an unknown source never lands on another known Mac.
        check(MacDirectory.approvalTarget(src: macB, macs: list, current: "cur") == "b", "approval to source")
        check(MacDirectory.approvalTarget(src: nil, macs: list, current: "cur") == "cur", "approval, old backend")
        check(MacDirectory.approvalTarget(src: "unknown-mac-id", macs: list, current: "cur") == nil,
              "approval from an unknown Mac is not sent to the current, identified Mac")
        let manualList = [saved("man", macId: nil, url: "http://m6.ts.net:14159", source: .manual, credential: .sharedKey)]
        check(MacDirectory.approvalTarget(src: "unknown-mac-id", macs: manualList, current: "man") == "man",
              "unidentified current Mac may be the source")

        // A claim whose instance_id collides with a saved Mac at another address is a new entry.
        let spoof = MacDirectory.upsert([current], url: "http://evil.ts.net", macId: macA, source: .qr,
                                        credential: .deviceToken, now: now, newId: newId)
        check(spoof.id != "cur" && spoof.macs[0] == current && spoof.macs[1].macId == nil,
              "colliding instance_id cannot repoint a saved Mac")

        // A legacy twin of a per-Mac item (same address) is ignored. Keychain order is arbitrary: check both.
        let twin = conn(a.url, mac: "m6", macId: nil, key: "stale")
        for order in [[twin, a], [a, twin]] {
            let dual = MacDirectory.merge([], synced: order, hidden: [], now: now, newId: newId)
            check(dual.macs.count == 1 && dual.macs[0].macId == macA && dual.keys.isEmpty
                  && dual.macs[0].credential == .none,
                  "legacy twin of a per-Mac item is ignored (\(order[0].macId == nil ? "legacy first" : "tagged first"))")
        }
        // Address changed before the Mac's first 76 write: adopt by tailnet name.
        let movedMac = MacDirectory.merge([migrated], synced: [movedA], hidden: [], now: now, newId: newId)
        check(movedMac.macs.count == 1 && movedMac.macs[0].id == "old" && movedMac.macs[0].macId == macA
              && movedMac.tickets["old"] == "tA2", "legacy entry adopted by name after an address change")
        // A Mac deleted while it was on 75 stays deleted after it starts publishing its id.
        let stillHidden = MacDirectory.merge([], synced: [a], hidden: [MacDirectory.hideKey(macId: nil, url: a.url)],
                                             now: now, newId: newId)
        check(stillHidden.macs.isEmpty, "hidden by address survives the Mac gaining an id")

        // Mac side: items under an old id of this Mac (same address) are cleaned up; others stay.
        let staleItems = [a, b, conn(a.url, mac: "m6", macId: "1111aaaa-old-identity", ticket: "x"),
                          conn(a.url, mac: "m6", macId: nil, key: "old")]
        check(MacDirectory.staleOwnItems(staleItems, macId: macA, url: a.url) == ["1111aaaa-old-identity"],
              "only this Mac's old identities are stale")

        // A backend's self-reported instance_id is only recorded if no other saved Mac uses it.
        let withA = [current, saved("rogue", macId: nil, url: "http://rogue.ts.net", source: .manual,
                                    credential: .sharedKey, name: "rogue")]
        check(MacDirectory.adoptableId(macA, for: "rogue", macs: withA) == nil,
              "a hand-entered backend cannot claim a saved Mac's id")
        check(MacDirectory.adoptableId(macB, for: "rogue", macs: withA) == macB, "an unused id is adopted")
        check(MacDirectory.adoptableId("bad", for: "rogue", macs: withA) == nil, "invalid id refused")
        // The iCloud item wins over a non-iCloud entry claiming its id from another host.
        let squatter = saved("sq", macId: macA, url: "http://rogue.ts.net", source: .manual, name: "rogue")
        let reclaimed = MacDirectory.merge([squatter], synced: [a], hidden: [], now: now, newId: newId)
        check(reclaimed.macs.count == 2 && reclaimed.macs[0].macId == nil
              && reclaimed.macs[1].macId == macA && reclaimed.macs[1].url == a.url,
              "a squatting entry loses the id to the real Mac's iCloud item")
        check(MacDirectory.route(src: macA, macs: reclaimed.macs, current: "sq") == reclaimed.macs[1].id,
              "approvals for the real Mac go to the real Mac")

        check(MacDirectory.validMacId(macA) && !MacDirectory.validMacId("short")
              && !MacDirectory.validMacId("../../x/../../y") && !MacDirectory.validMacId(String(repeating: "a", count: 65)),
              "mac id charset and length")

        if failures > 0 {
            print("\(failures) failure(s)")
            exit(1)
        }
        print("phone link: all checks passed")
    }
}
