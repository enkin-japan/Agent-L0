import Combine
import Foundation

// Compiled with the real Sources/App/PhoneLink.swift (macOS branch); only the
// settings object and the L10n lookup are replaced. No keychain, no tailscale CLI.
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

        if failures > 0 {
            print("\(failures) failure(s)")
            exit(1)
        }
        print("phone link: all checks passed")
    }
}
