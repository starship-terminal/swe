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

    func entry(_ byte: UInt8, _ count: Int = 1000) -> CachedResponse {
        CachedResponse(status: 200, headers: [:], body: Data(repeating: byte, count: count),
                       savedAt: Date(timeIntervalSince1970: 1))
    }

    func tempDir() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("shell-cache-\(UUID())")
    }

    @Test func memoryDropsTheLeastRecentlyUsedPastItsBudget() {
        let dir = tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let cache = DiskShellCache(directory: dir, memoryBudget: 2500, diskBudget: 1 << 20)
        cache.store(entry(1), for: "/a")
        cache.store(entry(2), for: "/b")
        _ = cache.cachedResponse(for: "/a")  // a is now the most recently used
        cache.store(entry(3), for: "/c")     // 3,000 bytes: one has to go
        #expect(cache.memoryKeys == ["/a", "/c"])
        // b is still on disk, under the folder's own budget, and reading it
        // back pushes the oldest (a) out of memory.
        cache.flush()
        #expect(cache.cachedResponse(for: "/b") == entry(2))
        #expect(cache.memoryKeys == ["/c", "/b"])
    }

    @Test func diskDropsTheLeastRecentlyUsedFilePastItsBudget() throws {
        let dir = tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let probe = DiskShellCache(directory: dir)
        probe.store(entry(1), for: "/a")
        probe.flush()
        let fileSize = try #require(try FileManager.default.attributesOfItem(
            atPath: probe.file(for: "/a").path)[.size] as? Int)
        // Room for two files, not three.
        let budget = fileSize * 2 + fileSize / 2
        let cache = DiskShellCache(directory: dir, diskBudget: budget)
        cache.store(entry(2), for: "/b")
        cache.flush()
        // A fresh instance reads a from disk: that read makes a more recent than b.
        let reader = DiskShellCache(directory: dir, diskBudget: budget)
        #expect(reader.cachedResponse(for: "/a") == entry(1))
        reader.flush()
        cache.store(entry(3), for: "/c")
        cache.flush()
        let after = DiskShellCache(directory: dir)
        #expect(after.cachedResponse(for: "/a") == entry(1), "the recently read entry was dropped")
        #expect(after.cachedResponse(for: "/b") == nil, "the least recently used entry stayed")
        #expect(after.cachedResponse(for: "/c") == entry(3))
    }

    @Test func oneEntryOverTheBudgetIsStillKept() {
        let dir = tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let cache = DiskShellCache(directory: dir, memoryBudget: 500, diskBudget: 500)
        cache.store(entry(1), for: "/big")
        cache.flush()
        #expect(cache.memoryKeys == ["/big"])
        #expect(DiskShellCache(directory: dir).cachedResponse(for: "/big") == entry(1))
    }

    @Test func budgetsAreNamedAndHoldTheLargestReply() {
        #expect(DiskShellCache.memoryBudgetBytes == 16 << 20)
        #expect(DiskShellCache.diskBudgetBytes == 64 << 20)
        #expect(DiskShellCache.memoryBudgetBytes >= CachePolicy.maxBodyBytes)
    }
}
