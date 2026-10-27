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

/// Stands in for the tailscale CLI: answers status / serve status from its own state and can
/// hold `serve ... off` open, so a sync can be attempted while a stop is still in progress.
@MainActor
final class FakeTailscale {
    var served: Bool
    var calls: [[String]] = []
    var holdOff = false
    var failOff = false
    private(set) var waitingOff = false
    private var release: CheckedContinuation<Void, Never>?
    init(served: Bool) { self.served = served }

    static let status = Data("""
    {"BackendState":"Running","CertDomains":["m6.example.ts.net"],"Self":{"DNSName":"m6.example.ts.net."}}
    """.utf8)
    static let serve = Data("""
    {"TCP":{"443":{"HTTPS":true}},"Web":{"m6.example.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:14159"}}}}}
    """.utf8)

    func run(_ args: [String]) async -> TailscaleCLI.Output? {
        calls.append(args)
        let ok = { (out: Data) in TailscaleCLI.Output(code: 0, out: out, err: "") }
        if args == ["status", "--json"] { return ok(Self.status) }
        if args == ["serve", "status", "--json"] { return ok(served ? Self.serve : Data("{}".utf8)) }
        if args.last == "off" {
            if holdOff {
                waitingOff = true
                await withCheckedContinuation { release = $0 }
                waitingOff = false
            }
            if failOff { return TailscaleCLI.Output(code: 1, out: Data(), err: "synthetic serve off failure") }
            served = false
            return ok(Data())
        }
        if args.contains("--bg") {
            served = true
            return ok(Data())
        }
        return TailscaleCLI.Output(code: 1, out: Data(), err: "unexpected tailscale call")
    }

    func releaseOff() {
        release?.resume()
        release = nil
    }
}

@MainActor
final class FakeEntries {
    var writes: [PhoneConnection] = []
    var deletes: [String] = []
}

@MainActor
func eventually(_ condition: () -> Bool) async -> Bool {
    for _ in 0..<300 {
        if condition() { return true }
        try? await Task.sleep(nanoseconds: 10_000_000)
    }
    return condition()
}

/// Every effect is synthetic: no CLI, no keychain, no request to a local backend.
/// Throwaway preference domain: stop requests are persisted per Mac.
func scratchDefaults() -> UserDefaults {
    let suite = "phone-link-test." + UUID().uuidString
    scratchSuites.append(suite)
    return UserDefaults(suiteName: suite)!
}
var scratchSuites: [String] = []

@MainActor
func fakeEffects(_ tailscale: FakeTailscale, _ entries: FakeEntries,
                 defaults: UserDefaults = scratchDefaults(),
                 probe: @escaping () -> Int? = { 401 }) -> PhoneLink.Effects {
    var effects = PhoneLink.Effects()
    effects.defaults = defaults
    effects.probeAuth = { _ in probe() }
    effects.bundleLayout = { false }
    effects.tailscale = { await tailscale.run($0) }
    effects.ticket = { _, _ in "ticket-one" }
    effects.rotateTicket = { _, _ in "ticket-two" }
    effects.pending = { _, _ in [] }
    effects.devices = { _, _ in throw URLError(.cannotConnectToHost) }
    effects.writeEntry = { connection in
        MainActor.assumeIsolated { entries.writes.append(connection) }
        return errSecSuccess
    }
    effects.deleteEntry = { id in
        MainActor.assumeIsolated { entries.deletes.append(id) }
        return errSecSuccess
    }
    effects.deleteLegacy = { errSecSuccess }
    effects.readEntries = { ([], errSecSuccess) }
    effects.macId = { "fixture-mac-0001" }
    return effects
}

@main
struct PhoneLinkTest {
    @MainActor
    static func main() async {
        let code = "AbCdEfGhIjKlMnOpQrStUv_-"

        // ---- PairingLink: the QR payload survives a round trip ----
        let base = URL(string: "http://m6.example.ts.net:14159")!
        let link = PairingLink(base: base, code: code)
        check(link != nil, "valid link builds")
        if let link {
            check(PairingLink(url: link.url) == link, "round trip through macagent://pair")
            check(link.url.scheme == "macagent" && link.url.host() == "pair", "link shape")
            check(link.host == "m6.example.ts.net", "host shown in the confirmation")
        }
        let https = PairingLink(url: URL(string: "macagent://pair?u=https://m6.example.ts.net&c=\(code)")!)
        check(https?.base.absoluteString == "https://m6.example.ts.net", "https link parses")

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
         "Web":{"m6.example.ts.net:14159":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:14159"}}}}}
        """)
        check(TailnetParse.servedURL(httpServe)?.absoluteString == "http://m6.example.ts.net:14159",
              "http serve on 14159")
        let httpsServe = json("""
        {"TCP":{"443":{"HTTPS":true}},
         "Web":{"m6.example.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:14159/"}}}}}
        """)
        check(TailnetParse.servedURL(httpsServe)?.absoluteString == "https://m6.example.ts.net",
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
        {"BackendState":"Running","CertDomains":["m6.example.ts.net"],
         "Self":{"DNSName":"m6.example.ts.net."}}
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
        let two = MacDirectory.merge([], synced: [a, b], pinned: [], now: now, newId: newId)
        check(two.macs.map(\.macId) == [macA, macB] && two.macs.allSatisfy { $0.source == .icloud },
              "two Macs, two entries")
        check(two.macs.allSatisfy { $0.credential == .none }, "a Mac read from iCloud still needs the Mac's approval")
        check(two.tickets == ["saved-1": "tA", "saved-2": "tB"] && two.legacyKeys.isEmpty,
              "each entry gets its own Mac's ticket and no key")

        // The current Mac (A) is never rewritten with another Mac's address or ticket.
        let current = saved("cur", macId: macA, url: "http://m6.ts.net:14159")
        let rotatedB = conn("https://studio.ts.net", mac: "studio", macId: macB, ticket: "tB2")
        let onlyB = MacDirectory.merge([current], synced: [rotatedB], pinned: [], now: now, newId: newId)
        var gone = current
        gone.removedFromICloud = true
        check(onlyB.macs.first == gone, "A untouched by B's item (only flagged as gone from iCloud)")
        check(onlyB.tickets["cur"] == nil && onlyB.legacyKeys["cur"] == nil, "A's credentials never replaced by B's")
        check(onlyB.macs.count == 2 && onlyB.macs[1].macId == macB, "B added beside A")
        // Same Mac changed its address / rotated its ticket: applied to that entry only, token kept.
        let movedA = conn("https://m6.ts.net", mac: "m6", macId: macA, ticket: "tA2")
        let healed = MacDirectory.merge([current], synced: [movedA, rotatedB], pinned: [], now: now, newId: newId)
        check(healed.macs[0].id == "cur" && healed.macs[0].url == "https://m6.ts.net" && healed.tickets["cur"] == "tA2"
              && healed.macs[0].credential == .deviceToken, "current Mac's new address is picked up in place")
        // A name the user chose on the iPhone survives iCloud updates.
        let renamed = saved("cur", macId: macA, url: a.url, name: "书房", custom: true)
        check(MacDirectory.merge([renamed], synced: [a], pinned: [], now: now, newId: newId).macs[0].name == "书房",
              "custom name kept")

        // 75 connection migrated without an id adopts it by address and keeps its shared key for the upgrade.
        let migrated = MacDirectory.migrate(url: " http://m6.ts.net:14159 ", source: .icloud, now: now, id: "old")!
        check(migrated.url == "http://m6.ts.net:14159" && migrated.name == "m6" && migrated.credential == .sharedKey,
              "migration trims, names, and marks the shared key for upgrade")
        check(MacDirectory.migrate(url: "  ", source: .manual, now: now, id: "x") == nil, "nothing to migrate")
        let adopted = MacDirectory.merge([migrated], synced: [a], pinned: [], now: now, newId: newId)
        check(adopted.macs.count == 1 && adopted.macs[0].id == "old" && adopted.macs[0].macId == macA
              && adopted.macs[0].credential == .sharedKey, "legacy entry adopts the Mac's id")

        // QR / manual entries are not silently overwritten, nor flagged.
        let paired = saved("qr", macId: macA, url: "http://m6.ts.net:14159", source: .qr)
        let keepQR = MacDirectory.merge([paired], synced: [movedA], pinned: [], now: now, newId: newId)
        check(keepQR.macs == [paired] && keepQR.legacyKeys.isEmpty && keepQR.tickets.isEmpty, "scanned connection kept as is")
        check(MacDirectory.merge([paired], synced: [], pinned: [], now: now, newId: newId).macs == [paired],
              "non-iCloud entry not flagged as removed")

        // A legacy synced item (75 Mac) matches by address only, and never replaces a device token.
        // Its shared key never becomes a local credential (build 78): iCloud makes the Mac discoverable,
        // pairing is a tap (POST /pair/upgrade with that key, then the device token is stored).
        let legacyB = conn("http://studio.ts.net:14159", mac: "studio", macId: nil, key: "kL")
        let withLegacy = MacDirectory.merge([current], synced: [legacyB], pinned: [], now: now, newId: newId)
        check(withLegacy.legacyKeys["cur"] == nil && withLegacy.macs.count == 2 && withLegacy.macs[1].macId == nil
              && withLegacy.macs[1].credential == .none && withLegacy.macs[1].pairingState == .unpaired,
              "75 item from another Mac is listed as its own entry, unpaired, with no credential")
        check(withLegacy.macs.count == 2 && withLegacy.legacyKeys == [withLegacy.macs[1].id: "kL"],
              "the 75 item's shared key is only held for the tap that pairs it")
        let tokenHolder = saved("tok", macId: nil, url: legacyB.url, name: "studio")
        let notDowngraded = MacDirectory.merge([tokenHolder], synced: [legacyB], pinned: [], now: now, newId: newId)
        check(notDowngraded.legacyKeys.isEmpty && notDowngraded.macs[0].credential == .deviceToken,
              "a stale shared key never replaces this device's token")

        // ---- Unpairing (build 78: 取消配对 replaces hiding a deleted Mac) ----
        // A synced entry with a local credential is paired; one read from iCloud only is not.
        check(saved("p", macId: macA, url: a.url).pairingState == .paired
              && saved("s", macId: nil, url: a.url, credential: .sharedKey).pairingState == .paired,
              "a synced entry with a local credential reads paired")
        check(two.macs.allSatisfy { $0.pairingState == .unpaired }, "an entry known only from iCloud reads unpaired")
        // Unpairing an iCloud Mac keeps the same row, unpaired, and the next merge still lists it.
        let pairedA = saved("pa", macId: macA, url: a.url)
        let listed = [pairedA, saved("pb", macId: macB, url: b.url, name: "studio")]
        if let unpaired = MacDirectory.unpair(listed, id: "pa") {
            check(unpaired.macs.count == 2 && unpaired.macs[0].id == "pa" && unpaired.macs[0].pairingState == .unpaired
                  && unpaired.macs[1] == listed[1], "unpairing keeps the Mac listed, unpaired, others untouched")
            let next = MacDirectory.merge(unpaired.macs, synced: [a, b], pinned: [], now: now, newId: newId)
            check(next.macs.map(\.id) == ["pa", "pb"] && next.macs[0].pairingState == .unpaired
                  && next.macs[1].pairingState == .paired && next.dropped.isEmpty,
                  "after unpairing, the next merge still lists the Mac, unpaired")
            check(next.tickets["pa"] == "tA", "the unpaired Mac gets its iCloud ticket back for re-pairing")
            // The local credential and request ticket are gone (accounts DenyApprovalIntent reads).
            check(unpaired.clearedAccounts == ["mac-key.pa", "mac-ticket.pa"],
                  "unpairing clears the Mac's local credential and ticket")
            check(unpaired.forgetSelf, "unpairing a device token asks the Mac to revoke it (forgetSelf)")
        } else {
            check(false, "unpairing keeps the Mac listed, unpaired, others untouched")
        }
        check(MacDirectory.unpair(listed, id: "nope") == nil, "unpairing an unknown id does nothing")
        // Unpairing a 75 Mac stays unpaired: the next merge with the same shared-key item re-creates
        // no credential (what the 77 hidden set used to prevent by hiding the Mac).
        for held in [Credential.deviceToken, .sharedKey] {
            let legacyRow = saved("lg", macId: nil, url: legacyB.url, credential: held, name: "studio")
            let again = MacDirectory.unpair([legacyRow], id: "lg").map {
                MacDirectory.merge($0.macs, synced: [legacyB], pinned: [], now: now, newId: newId)
            }
            check(again?.macs.map(\.id) == ["lg"] && again?.macs[0].credential == Credential.none
                  && again?.legacyKeys == ["lg": "kL"],
                  "after unpairing a 75 Mac (\(held)), merging its shared-key item re-creates no credential")
        }
        // Only a device token is revoked on the Mac; a shared key is just dropped locally.
        let shared = saved("sk", macId: nil, url: legacyB.url, credential: .sharedKey, name: "studio")
        check(MacDirectory.unpair([shared], id: "sk").map { !$0.forgetSelf && $0.clearedAccounts == ["mac-key.sk", "mac-ticket.sk"] } == true,
              "a shared key is cleared without forgetSelf")
        // An iCloud Mac that already stopped syncing has nothing to re-pair with: it leaves the list.
        var stoppedMac = saved("st", macId: macB, url: b.url, name: "studio")
        stoppedMac.removedFromICloud = true
        check(MacDirectory.unpair([pairedA, stoppedMac], id: "st")
                .map { $0.macs == [pairedA] && $0.forgetSelf && $0.clearedAccounts == ["mac-key.st", "mac-ticket.st"] } == true,
              "unpairing an iCloud Mac that stopped syncing removes it from the list")
        // A scanned Mac leaves the list; if it is also in iCloud the next merge lists it again, unpaired.
        let scanned = saved("qr", macId: macA, url: a.url, source: .qr)
        if let gone = MacDirectory.unpair([scanned], id: "qr") {
            check(gone.macs.isEmpty && gone.forgetSelf, "an unpaired scanned Mac leaves the list")
            let back = MacDirectory.merge(gone.macs, synced: [a], pinned: [], now: now, newId: newId)
            check(back.macs.count == 1 && back.macs[0].macId == macA && back.macs[0].pairingState == .unpaired,
                  "an unpaired scanned Mac still in iCloud is listed again, unpaired")
        } else {
            check(false, "an unpaired scanned Mac leaves the list")
        }
        // An unpaired row whose iCloud item is gone leaves the list (nothing to re-pair with);
        // a paired one is only flagged.
        // Only a non-empty read that lacks the item drops it; an empty read (iCloud signed out) is no information.
        let otherMac = conn("http://other.ts.net:14159", mac: "other", macId: "cccccccc-other", ticket: "tC")
        let unpairedGone = saved("ug", macId: macB, url: b.url, credential: .none, name: "studio")
        let pruned = MacDirectory.merge([pairedA, unpairedGone], synced: [otherMac], pinned: [], now: now, newId: newId)
        check(pruned.macs.map(\.id) == ["pa", "saved-\(counter)"] && pruned.macs[0].removedFromICloud
              && pruned.dropped == ["ug"], "an unpaired Mac that left iCloud is dropped; a paired one is flagged")
        let emptyRead = MacDirectory.merge([pairedA, unpairedGone], synced: [], pinned: [], now: now, newId: newId)
        check(emptyRead.dropped.isEmpty && emptyRead.macs.map(\.id) == ["pa", "ug"],
              "an empty iCloud read drops nothing")
        // The current Mac and the one being paired are never dropped; a row with any credential never is.
        let sharedGone = saved("sg", macId: nil, url: "http://old.ts.net:14159", credential: .sharedKey, name: "old")
        let rows = [pairedA, unpairedGone, sharedGone, saved("u2", macId: "dddddddd-gone", url: "http://u2.ts.net", credential: .none)]
        for pinned in [Set<String>(), ["ug"], ["u2"], ["ug", "u2"]] {
            let result = MacDirectory.merge(rows, synced: [otherMac], pinned: pinned, now: now, newId: newId)
            let unpairedIds = Set(rows.filter { $0.credential == .none }.map(\.id))
            check(Set(result.dropped) == unpairedIds.subtracting(pinned)
                  && result.macs.filter { $0.credential != .none }.count == 2
                  && pinned.allSatisfy { id in result.macs.contains { $0.id == id } },
                  "dropped holds only unpaired rows outside pinned (\(pinned.sorted()))")
        }
        // Tap routing: the 75 shared key only pairs a row with no usable local credential;
        // a rejected device token always goes back through the Mac's approval.
        check(MacDirectory.pairingRoute(credential: .none, hasKey: false, legacyKey: "kL") == .legacyUpgrade(key: "kL"),
              "an unpaired 75 Mac pairs through the upgrade")
        check(MacDirectory.pairingRoute(credential: .sharedKey, hasKey: false, legacyKey: "kL") == .legacyUpgrade(key: "kL"),
              "a shared-key row whose local key is gone pairs through the upgrade")
        check(MacDirectory.pairingRoute(credential: .sharedKey, hasKey: true, legacyKey: "kL") == .request
              && MacDirectory.pairingRoute(credential: .deviceToken, hasKey: false, legacyKey: "kL") == .request
              && MacDirectory.pairingRoute(credential: .deviceToken, hasKey: true, legacyKey: "kL") == .request,
              "a device token (even a rejected one) never pairs through the shared key")
        check(MacDirectory.pairingRoute(credential: .none, hasKey: false, legacyKey: nil) == .request
              && MacDirectory.pairingRoute(credential: .none, hasKey: false, legacyKey: "") == .request,
              "no shared key: the ticket handshake")
        let sharedRow = saved("sr", macId: nil, url: legacyB.url, credential: .sharedKey, name: "studio")
        check(MacDirectory.merge([sharedRow], synced: [legacyB], pinned: [], now: now, newId: newId).legacyKeys == ["sr": "kL"]
              && MacDirectory.merge([sharedRow], synced: [legacyB], pinned: [], now: now, newId: newId).macs == [sharedRow],
              "a shared-key row gets the 75 key for its tap, and its credential is not rewritten")

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
        let unpairedList = [current, saved("b", macId: macB, url: b.url, credential: .none, name: "studio")]
        check(MacDirectory.route(src: macB, macs: unpairedList, current: "cur") == "cur",
              "a notification from an unpaired source Mac falls back to the current Mac")

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
            let dual = MacDirectory.merge([], synced: order, pinned: [], now: now, newId: newId)
            check(dual.macs.count == 1 && dual.macs[0].macId == macA && dual.legacyKeys.isEmpty
                  && dual.macs[0].credential == .none,
                  "legacy twin of a per-Mac item is ignored (\(order[0].macId == nil ? "legacy first" : "tagged first"))")
        }
        // Address changed before the Mac's first 76 write: adopt by tailnet name.
        let movedMac = MacDirectory.merge([migrated], synced: [movedA], pinned: [], now: now, newId: newId)
        check(movedMac.macs.count == 1 && movedMac.macs[0].id == "old" && movedMac.macs[0].macId == macA
              && movedMac.tickets["old"] == "tA2", "legacy entry adopted by name after an address change")

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
        let reclaimed = MacDirectory.merge([squatter], synced: [a], pinned: [], now: now, newId: newId)
        check(reclaimed.macs.count == 2 && reclaimed.macs[0].macId == nil
              && reclaimed.macs[1].macId == macA && reclaimed.macs[1].url == a.url,
              "a squatting entry loses the id to the real Mac's iCloud item")
        var pairedReal = reclaimed.macs
        if pairedReal.count == 2 { pairedReal[1].credential = .deviceToken }
        check(pairedReal.count == 2 && MacDirectory.route(src: macA, macs: pairedReal, current: "sq") == pairedReal[1].id,
              "approvals for the real Mac go to the real Mac")
        // A notification from a Mac this iPhone is not paired with never switches (switching would pair).
        check(MacDirectory.route(src: macA, macs: reclaimed.macs, current: "sq") == "sq",
              "a notification from an unpaired Mac stays on the current Mac")

        check(MacDirectory.validMacId(macA) && !MacDirectory.validMacId("short")
              && !MacDirectory.validMacId("../../x/../../y") && !MacDirectory.validMacId(String(repeating: "a", count: 65)),
              "mac id charset and length")

        // ---- Approval routing: one rule for the app and the Live Activity "deny" ----
        do {
            let current = saved("cur", macId: macA, url: "https://a.example.ts.net", source: .icloud, name: "A")
            let other = saved("oth", macId: macB, url: "https://b.example.ts.net", source: .icloud, name: "B")
            let manual = saved("man", macId: nil, url: "https://m.example.ts.net", source: .manual, name: "M")
            let stored = try! JSONEncoder().encode([current, other, manual])   // what PhoneLink.persist stores
            let macs = [current, other, manual]
            // What the intent must do for a given approvalTarget answer: send to that Mac's
            // address, or refuse. Never the legacy connection while a list exists.
            func expectedIntent(_ id: String?) -> ApprovalRouting.IntentTarget {
                guard let id, let mac = macs.first(where: { $0.id == id }) else { return .refuse }
                return .mac(id, URL(string: mac.url)!)
            }
            let cases: [(String?, String?, String?, String)] = [
                (macB, "cur", "oth", "src matching a saved Mac goes to that Mac"),
                (nil, "cur", "cur", "no src falls back to the current Mac"),
                ("unknown-mac-id", "cur", nil, "unmatched src with an identified current Mac is refused"),
                ("unknown-mac-id", "man", "man", "unmatched src may go to a current Mac without an id"),
                (nil, nil, nil, "no src and no current Mac is refused"),
            ]
            for (src, currentId, expected, label) in cases {
                let app = MacDirectory.approvalTarget(src: src, macs: macs, current: currentId)
                check(app == expected, "approvalTarget: " + label)
                check(ApprovalRouting.intentTarget(list: stored, currentId: currentId, src: src) == expectedIntent(app),
                      "intent path agrees with approvalTarget: " + label)
            }
            check(ApprovalRouting.intentTarget(list: nil, currentId: nil, src: macA) == .legacy,
                  "no saved list: the intent uses the legacy connection")
            check(ApprovalRouting.intentTarget(list: stored, currentId: "cur", src: macA)
                  == .mac("cur", URL(string: "https://a.example.ts.net")!), "the intent carries the Mac's address")
            let broken = try! JSONEncoder().encode([saved("bad", macId: macA, url: "no scheme", source: .icloud)])
            check(ApprovalRouting.intentTarget(list: broken, currentId: "bad", src: macA) == .refuse,
                  "a chosen Mac without a usable address is refused, not sent to the legacy connection")
        }

        // ---- Selecting a Mac: a fresh credential is adopted even after a rejection ----
        do {
            check(MacDirectory.needsHandshake(credential: .deviceToken, hasKey: true, isCurrent: true,
                                              currentRejected: true, freshCredential: false),
                  "a current Mac that rejected this iPhone asks again")
            check(!MacDirectory.needsHandshake(credential: .deviceToken, hasKey: true, isCurrent: true,
                                               currentRejected: true, freshCredential: true),
                  "after re-scanning a Mac that rejected this iPhone, the new ticket is used without /pair/request")
            check(MacDirectory.needsHandshake(credential: .none, hasKey: false, isCurrent: false,
                                              currentRejected: false, freshCredential: true)
                  && MacDirectory.needsHandshake(credential: .deviceToken, hasKey: false, isCurrent: false,
                                                 currentRejected: false, freshCredential: true),
                  "no credential on hand still means a handshake")
            check(!MacDirectory.needsHandshake(credential: .deviceToken, hasKey: true, isCurrent: false,
                                               currentRejected: true, freshCredential: false),
                  "another Mac is not affected by the current one's rejection")
        }

        // ---- Claim placement after waiting for the old ticket to be forgotten ----
        do {
            let url = "https://q.example.ts.net"
            let planned = MacDirectory.upsert([saved("x1", macId: macA, url: url, source: .qr)], url: url, macId: macA,
                                              source: .qr, credential: .deviceToken, now: now, newId: newId)
            check(planned.id == "x1", "fixture: the claim lands on the saved Mac")
            let same = MacDirectory.finalizeClaim(planned.macs, plannedId: "x1", url: url, macId: macA, now: now)
            check(same.id == "x1" && same.orphan == nil, "an unchanged list keeps the planned entry")
            // While waiting, the planned entry was removed and iCloud brought the Mac back as another row.
            let replaced = [saved("y2", macId: macA, url: url, source: .icloud)]
            let moved = MacDirectory.finalizeClaim(replaced, plannedId: "x1", url: url, macId: macA, now: now)
            check(moved.id == "y2" && moved.orphan == "x1",
                  "a claim whose row changed while waiting saves under the final id and reports the planned one")
            let fresh = MacDirectory.finalizeClaim([], plannedId: "new-id-0001", url: url, macId: macA, now: now)
            check(fresh.id == "new-id-0001" && fresh.orphan == nil && fresh.macs.count == 1,
                  "a new row keeps the planned id")
        }

        // ---- Exposing to the tailnet requires the backend's own authentication ----
        // The backend lets loopback through without a key and a keyless backend grants
        // owner rights to everyone; serve hands that loopback to the whole tailnet.
        do {
            let settings = AppSettings()
            let tailscale = FakeTailscale(served: false)
            var probe: Int? = 401
            let link = PhoneLink()
            link.configure(settings: settings, effects: fakeEffects(tailscale, FakeEntries(), probe: { probe }))
            await link.refresh()
            check(link.exposure == .notServed(https: true, portTaken: false), "fixture starts not served")
            check(link.exposeRefusal != nil, "no API key: exposing is refused up front")
            await link.expose()
            check(!tailscale.calls.contains { $0.contains("--bg") }, "expose refuses without a backend API key")
            check(link.actionError == link.exposeRefusal, "the refusal says why")
            check(link.exposeRefusal?.contains(".env.local") == true, "repo layout points at .env.local")
            settings.apiKey = "fifteen-chars-k"
            check(link.exposeRefusal != nil, "a key shorter than 16 characters is refused too")
            settings.apiKey = String(repeating: "k", count: 32)
            check(link.exposeRefusal == nil, "a real key allows asking the backend")
            // The app holds a key, but the backend answers without one: authentication is off.
            probe = 200
            await link.expose()
            check(!tailscale.calls.contains { $0.contains("--bg") }, "a backend that lets unauthenticated requests through is not exposed")
            check(link.actionError?.contains("后端未启用鉴权") == true, "the refusal names the missing authentication")
            probe = nil
            await link.expose()
            check(!tailscale.calls.contains { $0.contains("--bg") }, "an unanswered probe does not expose")
            probe = 404
            await link.expose()
            check(!tailscale.calls.contains { $0.contains("--bg") } && link.actionError?.contains("版本过旧") == true,
                  "a backend without the pairing API is not exposed and says why")
            probe = 401
            await link.expose()
            check(tailscale.calls.contains(["serve", "--bg", "--https=443", PhoneLink.target]),
                  "a backend that answers 401 without a key is exposed")
            check(link.exposure == .served(URL(string: "https://m6.example.ts.net")!), "served after expose")
            check(link.authWarning == nil, "an authenticated backend carries no warning")
        }
        do {
            let link = PhoneLink()
            var effects = fakeEffects(FakeTailscale(served: false), FakeEntries())
            effects.bundleLayout = { true }
            link.configure(settings: AppSettings(), effects: effects)
            check(link.exposeRefusal?.contains("重新生成密钥") == true && link.exposeRefusal?.contains(".env.local") == false,
                  "bundle layout is told to regenerate the key in Settings")
        }

        // ---- Already served at startup without authentication: a persistent warning ----
        do {
            let settings = AppSettings()
            settings.apiKey = String(repeating: "w", count: 32)
            let tailscale = FakeTailscale(served: true)
            let link = PhoneLink()
            var probe: Int? = 200
            link.configure(settings: settings, effects: fakeEffects(tailscale, FakeEntries(), probe: { probe }))
            await link.refresh()
            check(link.authWarning?.contains("未启用鉴权") == true, "served without authentication warns")
            check(!tailscale.calls.contains { $0.last == "off" }, "the warning does not stop serve by itself")
            probe = 401
            await link.refresh()
            check(link.authWarning == nil, "the warning clears once the backend requires a key")
            probe = 200
            await link.refresh()
            check(link.authWarning?.contains("未启用鉴权") == true, "a backend that loses its key warns again")
            probe = nil
            await link.refresh()
            check(link.authWarning?.contains("未启用鉴权") == true, "no answer keeps the last verdict")
            probe = 404
            await link.refresh()
            check(link.authWarning?.contains("版本过旧") == true, "an old backend is flagged as unconfirmed")
            check(PhoneLink.authRecheckInterval <= 60, "the watcher re-checks at least every minute")
        }

        // ---- Stopping: no sync may write the iCloud entry back while serve winds down ----
        do {
            let settings = AppSettings()
            settings.apiKey = String(repeating: "s", count: 32)
            let tailscale = FakeTailscale(served: true)
            let entries = FakeEntries()
            let link = PhoneLink()
            link.configure(settings: settings, effects: fakeEffects(tailscale, entries))
            await link.refresh()
            check(entries.writes.count == 1 && link.syncOK, "a served Mac writes its entry")
            tailscale.holdOff = true
            let stop = Task { await link.unexpose() }
            let held = await eventually { tailscale.waitingOff }
            check(held && link.stopping && link.stopRequested, "stop is in progress while serve winds down")
            check(entries.deletes == ["fixture-mac-0001"], "the entry is deleted first")
            // Everything that normally re-syncs: the toggle, a key rotation, the watcher.
            link.setSyncEnabled(true)
            settings.apiKey = String(repeating: "r", count: 32)
            try? await Task.sleep(nanoseconds: 100_000_000)
            check(entries.writes.count == 1, "no sync writes the entry back while stopping")
            tailscale.releaseOff()
            await stop.value
            check(!link.stopping && !link.stopRequested && link.exposure == .notServed(https: true, portTaken: false),
                  "stopped and not served")
            check(entries.writes.count == 1, "nothing was written after the stop either")
        }

        // ---- A failed `serve off` keeps the stop request until serve is really gone ----
        do {
            let settings = AppSettings()
            settings.apiKey = String(repeating: "f", count: 32)
            let tailscale = FakeTailscale(served: true)
            let entries = FakeEntries()
            let link = PhoneLink()
            link.configure(settings: settings, effects: fakeEffects(tailscale, entries))
            await link.refresh()
            tailscale.failOff = true
            await link.unexpose()
            check(link.stopRequested && !link.stopping && link.exposure == .served(URL(string: "https://m6.example.ts.net")!),
                  "a failed stop stays requested while still served")
            check(link.actionError?.contains("synthetic serve off failure") == true, "the failure is shown")
            link.setSyncEnabled(true)
            await link.refresh()
            try? await Task.sleep(nanoseconds: 100_000_000)
            check(entries.writes.count == 1, "a failed stop does not let sync write the entry back")
            tailscale.failOff = false
            await link.unexpose()
            check(!link.stopRequested && link.exposure == .notServed(https: true, portTaken: false),
                  "retrying the stop finishes it")
            check(entries.writes.count == 1, "still nothing written back")
        }

        // ---- A failed stop survives a restart: the next launch does not write the entry back ----
        do {
            let defaults = scratchDefaults()
            let settings = AppSettings()
            settings.apiKey = String(repeating: "p", count: 32)
            let tailscale = FakeTailscale(served: true)
            let entries = FakeEntries()
            let first = PhoneLink()
            first.configure(settings: settings, effects: fakeEffects(tailscale, entries, defaults: defaults))
            await first.refresh()
            tailscale.failOff = true
            await first.unexpose()
            check(first.stopRequested && defaults.bool(forKey: PhoneLink.stopRequestedKey("fixture-mac-0001")),
                  "a failed stop is remembered on disk")
            let writes = entries.writes.count
            // Relaunch: a new PhoneLink over the same preferences, serve still running.
            let relaunched = PhoneLink()
            relaunched.configure(settings: settings, effects: fakeEffects(tailscale, entries, defaults: defaults))
            await relaunched.refresh()
            try? await Task.sleep(nanoseconds: 100_000_000)
            check(relaunched.stopRequested && entries.writes.count == writes,
                  "after a restart a failed stop still keeps the iCloud entry deleted")
            tailscale.failOff = false
            await relaunched.unexpose()
            check(!relaunched.stopRequested && !defaults.bool(forKey: PhoneLink.stopRequestedKey("fixture-mac-0001")),
                  "a finished stop clears the remembered request")
        }

        // ---- Request polling only while a tailnet entrance exists ----
        check(PhoneLink.pollsRequests(.served(URL(string: "https://m6.example.ts.net")!)), "served: poll requests")
        check(!PhoneLink.pollsRequests(.notServed(https: true, portTaken: false))
              && !PhoneLink.pollsRequests(.checking) && !PhoneLink.pollsRequests(.offline)
              && !PhoneLink.pollsRequests(.noTailscale), "not served: no request polling")

        // ---- Upgrade backoff: 1, 2, 4 ... minutes, capped at an hour, survives a restart ----
        check(UpgradeBackoff.delay(afterFailures: 1) == 60 && UpgradeBackoff.delay(afterFailures: 2) == 120
              && UpgradeBackoff.delay(afterFailures: 3) == 240, "backoff doubles from one minute")
        check(UpgradeBackoff.delay(afterFailures: 7) == 3600 && UpgradeBackoff.delay(afterFailures: 200) == 3600,
              "backoff is capped at one hour")
        do {
            let suite = "phone-link-test." + UUID().uuidString
            let defaults = UserDefaults(suiteName: suite)!
            defer { defaults.removePersistentDomain(forName: suite) }
            let mac = UpgradeBackoff.fingerprint(url: "https://m6.example.ts.net", key: "shared-key")
            check(!mac.contains("shared-key") && mac.count == 64, "only a hash of address and key is kept")
            var backoff = UpgradeBackoff(defaults: defaults)
            let t0 = Date(timeIntervalSince1970: 1_000_000)
            check(backoff.allows(mac, now: t0), "never failed: allowed")
            backoff.failed(mac, now: t0)
            check(!backoff.allows(mac, now: t0.addingTimeInterval(30)),
                  "connection changes inside the window do not retry")
            check(backoff.allows(mac, now: t0.addingTimeInterval(60)), "retried after a minute")
            backoff.failed(mac, now: t0.addingTimeInterval(60))
            let restarted = UpgradeBackoff(defaults: defaults)
            check(!restarted.allows(mac, now: t0.addingTimeInterval(170))
                  && restarted.allows(mac, now: t0.addingTimeInterval(180)),
                  "the second failure's two-minute wait survives an app restart")
            check(String(decoding: defaults.data(forKey: UpgradeBackoff.defaultsKey) ?? Data(), as: UTF8.self)
                  .contains("shared-key") == false, "the key never reaches UserDefaults")
            let changed = UpgradeBackoff.fingerprint(url: "https://m6.example.ts.net", key: "new key")
            check(restarted.allows(changed, now: t0.addingTimeInterval(61)), "a configuration change resets the backoff")
            backoff.succeeded(mac)
            check(UpgradeBackoff(defaults: defaults).allows(mac, now: t0.addingTimeInterval(61)), "success resets, durably")
        }

        // ---- Re-pairing the same Mac: the old device ticket is forgotten before the new one is saved ----
        do {
            final class Log: @unchecked Sendable {
                let lock = NSLock()
                var events: [String] = []
                func add(_ e: String) { lock.lock(); events.append(e); lock.unlock() }
            }
            let base = URL(string: "https://m6.example.ts.net")!
            let order = Log()
            await TicketHandover.replace(previous: "old-ticket", at: base,
                                         forget: { url, token in order.add("forget:\(url.host() ?? ""):\(token)") },
                                         save: { order.add("save") }, log: { _ in })
            check(order.events == ["forget:m6.example.ts.net:old-ticket", "save"],
                  "forgetSelf with the old ticket happens before the new ticket is saved")
            let fresh = Log()
            await TicketHandover.replace(previous: nil, at: base, forget: { _, _ in fresh.add("forget") },
                                         save: { fresh.add("save") }, log: { _ in })
            check(fresh.events == ["save"], "no previous ticket: nothing to forget")
            let failing = Log()
            var logged: [String] = []
            await TicketHandover.replace(previous: "old-ticket", at: base,
                                         forget: { _, _ in failing.add("forget"); throw URLError(.timedOut) },
                                         save: { failing.add("save") }, log: { logged.append($0) })
            check(failing.events == ["forget", "save"] && logged.count == 1
                  && !logged[0].contains("old-ticket"), "a failed forget is logged (without the ticket) and does not block")
            let hanging = Log()
            let started = Date()
            await TicketHandover.replace(previous: "old-ticket", at: base, timeout: 0.2,
                                         forget: { _, _ in try await Task.sleep(nanoseconds: 5_000_000_000) },
                                         save: { hanging.add("save") }, log: { _ in })
            check(hanging.events == ["save"] && Date().timeIntervalSince(started) < 2,
                  "a forget that never answers is abandoned after the timeout")
            check(TicketHandover.timeout == 5, "the production timeout is five seconds")
        }

        // ---- Handshake polling survives a few transient errors, not a real answer ----
        let blip = URLError(.networkConnectionLost)
        check((1..<HandshakePolling.maxConsecutiveErrors).allSatisfy {
            HandshakePolling.keepsWaiting(after: blip, consecutive: $0) }, "transient errors keep waiting")
        check(!HandshakePolling.keepsWaiting(after: blip, consecutive: HandshakePolling.maxConsecutiveErrors),
              "five in a row ends the wait")
        check(HandshakePolling.keepsWaiting(after: PairingAPI.Failure.http(502), consecutive: 1)
              && HandshakePolling.keepsWaiting(after: PairingAPI.Failure.server(503, nil), consecutive: 1)
              && HandshakePolling.keepsWaiting(after: PairingAPI.Failure.server(500, "Internal error"), consecutive: 1),
              "a 5xx while the backend restarts is transient, with or without a detail")
        check(!HandshakePolling.keepsWaiting(after: PairingAPI.Failure.http(404), consecutive: 1)
              && !HandshakePolling.keepsWaiting(after: PairingAPI.Failure.refused("gone"), consecutive: 1)
              && !HandshakePolling.keepsWaiting(after: PairingAPI.Failure.badResponse, consecutive: 1)
              && !HandshakePolling.keepsWaiting(after: URLError(.cancelled), consecutive: 1),
              "an answer from the Mac or a cancellation ends the wait at once")

        for suite in scratchSuites { UserDefaults.standard.removePersistentDomain(forName: suite) }
        if failures > 0 {
            print("\(failures) failure(s)")
            exit(1)
        }
        print("phone link: all checks passed")
    }
}
