import Foundation
import Testing
@testable import SessionWebShell

let fakeCredential = "fake-proof-credential-0000"

func endpoint(_ origin: String = "http://127.0.0.1:8765", id: String = "box") -> HubEndpoint {
    try! HubEndpoint(id: id, origin: URL(string: origin)!)
}

@Suite struct HubEndpointTests {
    @Test func acceptsAnOriginAndLowercasesIt() throws {
        let hub = try HubEndpoint(id: "box", origin: URL(string: "http://Box.Tail.ts.net:8477/")!)
        #expect(hub.originString == "http://box.tail.ts.net:8477")
        #expect(hub.pageRoot.absoluteString == "swe-hub://box/")
    }

    @Test(arguments: ["Box", "", "-box", "box-", "bo x", "box.tail", String(repeating: "a", count: 64)])
    func refusesIDsThatAreNotOneLowercaseLabel(_ id: String) {
        #expect(throws: HubEndpoint.Problem.badID(id)) {
            try HubEndpoint(id: id, origin: URL(string: "http://127.0.0.1:1")!)
        }
    }

    @Test(arguments: ["ftp://box", "http://box/app", "http://box/?a=1", "http://u:p@box", "file:///tmp", "http://box/#x"])
    func refusesAnythingButABareOrigin(_ origin: String) {
        #expect(throws: HubEndpoint.Problem.self) {
            try HubEndpoint(id: "box", origin: URL(string: origin)!)
        }
    }

    @Test func dataStoreIsStablePerHubAndDistinctBetweenHubs() {
        #expect(endpoint().dataStoreID == endpoint().dataStoreID)
        #expect(endpoint().dataStoreID != endpoint(id: "other").dataStoreID)
        #expect(endpoint().dataStoreID != endpoint("http://127.0.0.1:9").dataStoreID)
    }

    @Test func hubOriginCountsDefaultPorts() {
        let hub = endpoint("https://box.ts.net")
        #expect(hub.isHubOrigin(URL(string: "https://box.ts.net:443/x")!))
        #expect(!hub.isHubOrigin(URL(string: "http://box.ts.net/x")!))
        #expect(!hub.isHubOrigin(URL(string: "https://box.ts.net:8443/x")!))
        #expect(!hub.isHubOrigin(URL(string: "https://evil.ts.net/x")!))
    }
}

@Suite struct RequestRewritingTests {
    let rewriter = RequestRewriter(endpoint: endpoint())

    @Test func keepsPathAndQueryAndPinsTheOrigin() throws {
        let url = try rewriter.hubURL(for: URL(string: "swe-hub://box/v1/sessions/a%2Fb/stream?last=12&epoch=e#frag")!)
        #expect(url.absoluteString == "http://127.0.0.1:8765/v1/sessions/a%2Fb/stream?last=12&epoch=e")
        #expect(try rewriter.hubURL(for: URL(string: "swe-hub://box")!).absoluteString == "http://127.0.0.1:8765/")
    }

    @Test func dotSegmentsCannotLeaveTheOrigin() throws {
        let url = try rewriter.hubURL(for: URL(string: "swe-hub://box/../../etc/passwd")!)
        #expect(url.host() == "127.0.0.1")
        #expect(url.port == 8765)
    }

    @Test(arguments: [
        ("https://box/x", ShellRefusal.wrongScheme("https")),
        ("file:///etc/passwd", ShellRefusal.wrongScheme("file")),
        ("swe-hub://other/x", ShellRefusal.wrongHub("swe-hub://other/x")),
        ("swe-hub://box:8765/x", ShellRefusal.wrongHub("swe-hub://box:8765/x")),
        ("swe-hub://u:p@box/x", ShellRefusal.wrongHub("swe-hub://u:p@box/x")),
        ("swe-hub://127.0.0.1/x", ShellRefusal.wrongHub("swe-hub://127.0.0.1/x")),
    ])
    func refusesEverythingOffThePinnedHub(_ input: String, _ refusal: ShellRefusal) {
        #expect(throws: refusal) { try rewriter.hubURL(for: URL(string: input)!) }
        #expect(throws: refusal) {
            try rewriter.hubRequest(for: URLRequest(url: URL(string: input)!), credential: fakeCredential)
        }
    }

    @Test func injectsTheShellsHeadersAndDropsThePagesCopies() throws {
        var page = URLRequest(url: URL(string: "swe-hub://box/v1/sessions/k/send")!)
        page.httpMethod = "POST"
        page.httpBody = Data(#"{"text":"hi"}"#.utf8)
        page.setValue("application/json", forHTTPHeaderField: "Content-Type")
        page.setValue("Bearer stolen", forHTTPHeaderField: "Authorization")
        page.setValue("swe-hub://box", forHTTPHeaderField: "Origin")
        page.setValue("0", forHTTPHeaderField: "X-Swe-Pair")
        page.setValue("a=b", forHTTPHeaderField: "Cookie")
        page.setValue("swe-hub://box/index.html", forHTTPHeaderField: "Referer")
        let out = try rewriter.hubRequest(for: page, credential: fakeCredential)
        #expect(out.url?.absoluteString == "http://127.0.0.1:8765/v1/sessions/k/send")
        #expect(out.httpMethod == "POST")
        #expect(out.httpBody == page.httpBody)
        #expect(out.value(forHTTPHeaderField: "Authorization") == "Bearer \(fakeCredential)")
        #expect(out.value(forHTTPHeaderField: "X-Swe-Pair") == "1")
        #expect(out.value(forHTTPHeaderField: "Origin") == "http://127.0.0.1:8765")
        #expect(out.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(out.value(forHTTPHeaderField: "Cookie") == nil)
        #expect(out.value(forHTTPHeaderField: "Referer") == nil)
        #expect(out.httpShouldHandleCookies == false)
    }

    @Test func noCredentialSendsNoBearerButStillTheOwnerGateHeaders() throws {
        var page = URLRequest(url: URL(string: "swe-hub://box/v1/status")!)
        page.setValue("Bearer stolen", forHTTPHeaderField: "Authorization")
        for credential in [nil, ""] as [String?] {
            let out = try rewriter.hubRequest(for: page, credential: credential)
            #expect(out.value(forHTTPHeaderField: "Authorization") == nil)
            #expect(out.value(forHTTPHeaderField: "X-Swe-Pair") == "1")
            #expect(out.value(forHTTPHeaderField: "Origin") == "http://127.0.0.1:8765")
        }
    }

    @Test func theCredentialIsOnlyEverInTheAuthorizationHeader() throws {
        let page = URLRequest(url: URL(string: "swe-hub://box/v1/sessions?x=1")!)
        let out = try rewriter.hubRequest(for: page, credential: fakeCredential)
        #expect(!out.url!.absoluteString.contains(fakeCredential))
        #expect(out.httpBody.map { String(decoding: $0, as: UTF8.self).contains(fakeCredential) } != true)
        let carrying = (out.allHTTPHeaderFields ?? [:]).filter { $0.value.contains(fakeCredential) }
        #expect(Array(carrying.keys) == ["Authorization"])
    }

    @Test func timeoutApplies() throws {
        let out = try rewriter.hubRequest(for: URLRequest(url: URL(string: "swe-hub://box/")!),
                                          credential: nil, timeout: 3)
        #expect(out.timeoutInterval == 3)
    }

    @Test func redirectsStayOnTheHubOrTheyAreRefused() throws {
        let same = URLRequest(url: URL(string: "http://127.0.0.1:8765/session/")!)
        let followed = try rewriter.followRedirect(same, credential: fakeCredential)
        #expect(followed.value(forHTTPHeaderField: "Authorization") == "Bearer \(fakeCredential)")
        #expect(followed.value(forHTTPHeaderField: "X-Swe-Pair") == "1")
        for away in ["http://evil.example/", "http://127.0.0.1:9999/", "https://127.0.0.1:8765/"] {
            #expect(throws: ShellRefusal.leavesOrigin(away)) {
                try rewriter.followRedirect(URLRequest(url: URL(string: away)!), credential: fakeCredential)
            }
        }
    }
}

@Suite struct ResponseShapingTests {
    let rewriter = RequestRewriter(endpoint: endpoint())

    @Test func pageSeesItsOwnURLAndNoneOfTheNativeHeaders() {
        let pageURL = URL(string: "swe-hub://box/v1/status")!
        let response = rewriter.pageResponse(
            for: pageURL, status: 201,
            headers: [
                "Content-Type": "application/json", "Set-Cookie": "s=1", "Content-Encoding": "gzip",
                "Content-Length": "99", "WWW-Authenticate": "Bearer", "X-Echo": "Bearer \(fakeCredential)",
                "Cache-Control": "no-cache",
            ],
            credential: fakeCredential, source: .network)
        #expect(response.url == pageURL)
        #expect(response.statusCode == 201)
        let fields = response.allHeaderFields as! [String: String]
        #expect(fields["Content-Type"] == "application/json")
        #expect(fields["Cache-Control"] == "no-cache")
        #expect(fields[ShellSource.header] == "network")
        for gone in ["Set-Cookie", "Content-Encoding", "Content-Length", "WWW-Authenticate", "X-Echo"] {
            #expect(fields[gone] == nil, "\(gone) reached the page")
        }
        #expect(!fields.values.contains { $0.contains(fakeCredential) })
    }

    @Test func cacheSourceIsMarked() {
        let response = rewriter.pageResponse(for: URL(string: "swe-hub://box/")!, status: 200,
                                             headers: [:], credential: nil, source: .cache)
        #expect(response.value(forHTTPHeaderField: ShellSource.header) == "cache")
    }
}
