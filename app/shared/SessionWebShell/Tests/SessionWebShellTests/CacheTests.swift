import Foundation
import Testing
@testable import SessionWebShell

@Suite struct CachePolicyTests {
    func request(_ method: String = "GET", accept: String? = nil) -> URLRequest {
        var r = URLRequest(url: URL(string: "swe-hub://box/v1/sessions/k/snapshot")!)
        r.httpMethod = method
        r.setValue(accept, forHTTPHeaderField: "Accept")
        return r
    }

    @Test func keyIsPathAndQuery() {
        #expect(CachePolicy.key(for: URL(string: "swe-hub://box/session/index.html")!) == "/session/index.html")
        #expect(CachePolicy.key(for: URL(string: "swe-hub://box/a%20b?x=1#f")!) == "/a%20b?x=1")
        #expect(CachePolicy.key(for: URL(string: "swe-hub://box")!) == "/")
    }

    @Test func onlyPlainGetsAreCacheable() {
        #expect(CachePolicy.isCacheable(request()))
        #expect(CachePolicy.isCacheable(request("get")))
        #expect(!CachePolicy.isCacheable(request("POST")))
        #expect(!CachePolicy.isCacheable(request("DELETE")))
        #expect(!CachePolicy.isCacheable(request(accept: "text/event-stream")))
    }

    @Test func storesOnlyWholeSuccessfulNonStreams() {
        #expect(CachePolicy.shouldStore(status: 200, headers: ["Content-Type": "text/html"], bodyBytes: 10))
        #expect(!CachePolicy.shouldStore(status: 404, headers: [:], bodyBytes: 10))
        #expect(!CachePolicy.shouldStore(status: 200, headers: ["content-type": "text/event-stream"], bodyBytes: 10))
        #expect(!CachePolicy.shouldStore(status: 200, headers: ["Cache-Control": "private, no-store"], bodyBytes: 10))
        #expect(!CachePolicy.shouldStore(status: 200, headers: [:], bodyBytes: CachePolicy.maxBodyBytes + 1))
    }

    @Test func fallbackChoice() {
        let down = URLError(.cannotConnectToHost)
        let get = request()
        // The hub is unreachable and there is a copy: serve it.
        #expect(CachePolicy.fallback(for: get, error: down, status: nil, hasCopy: true) == .serveCached)
        #expect(CachePolicy.fallback(for: get, error: URLError(.timedOut), status: nil, hasCopy: true) == .serveCached)
        #expect(CachePolicy.fallback(for: get, error: URLError(.notConnectedToInternet), status: nil, hasCopy: true) == .serveCached)
        // tailscale serve in front of a stopped hub.
        for status in [502, 503, 504] {
            #expect(CachePolicy.fallback(for: get, error: nil, status: status, hasCopy: true) == .serveCached)
        }
        // The hub's real answers stand.
        for status in [200, 401, 404, 500] {
            #expect(CachePolicy.fallback(for: get, error: nil, status: status, hasCopy: true) == .passThrough)
        }
        // No copy, a send, a stream, or a cancel: pass the failure through.
        #expect(CachePolicy.fallback(for: get, error: down, status: nil, hasCopy: false) == .passThrough)
        #expect(CachePolicy.fallback(for: request("POST"), error: down, status: nil, hasCopy: true) == .passThrough)
        #expect(CachePolicy.fallback(for: request(accept: "text/event-stream"), error: down, status: nil,
                                     hasCopy: true) == .passThrough)
        #expect(CachePolicy.fallback(for: get, error: URLError(.cancelled), status: nil, hasCopy: true) == .passThrough)
        #expect(CachePolicy.fallback(for: get, error: CocoaError(.fileNoSuchFile), status: nil,
                                     hasCopy: true) == .passThrough)
    }
}

@MainActor
@Suite struct DiskCacheTests {
    @Test func roundTripsAcrossInstances() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("shell-cache-\(UUID())")
        defer { try? FileManager.default.removeItem(at: dir) }
        let first = DiskShellCache(directory: dir)
        #expect(first.cachedResponse(for: "/index.html") == nil)
        let saved = CachedResponse(status: 200, headers: ["Content-Type": "text/html"],
                                   body: Data("<p>hi</p>".utf8), savedAt: Date(timeIntervalSince1970: 1))
        first.store(saved, for: "/index.html")
        #expect(first.cachedResponse(for: "/index.html") == saved)
        first.flush()
        let second = DiskShellCache(directory: dir)
        #expect(second.cachedResponse(for: "/index.html") == saved)
        #expect(second.cachedResponse(for: "/index.html?x") == nil)
        second.removeAll()
        #expect(DiskShellCache(directory: dir).cachedResponse(for: "/index.html") == nil)
    }
}
