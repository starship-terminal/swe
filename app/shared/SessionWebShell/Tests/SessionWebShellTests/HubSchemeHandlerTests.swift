import Foundation
import Testing
@testable import SessionWebShell

// MARK: Stub network

/// A scripted hub behind URLSession. Each test owns one port, so tests
/// running in parallel never share a script.
final class StubProtocol: URLProtocol, @unchecked Sendable {
    typealias Route = @Sendable (URLRequest, StubReply) -> Void
    private static let lock = NSLock()
    nonisolated(unsafe) private static var routes: [Int: Route] = [:]
    nonisolated(unsafe) private static var seen: [URLRequest] = []
    private var reply: StubReply?

    static func route(port: Int, _ route: @escaping Route) {
        lock.withLock { routes[port] = route }
    }

    static func requests(port: Int?) -> [URLRequest] {
        lock.withLock { seen.filter { $0.url?.port == port } }
    }

    static func requests(host: String) -> [URLRequest] {
        lock.withLock { seen.filter { $0.url?.host() == host } }
    }

    static func configuration() -> URLSessionConfiguration {
        let config = HubSchemeHandler.defaultConfiguration()
        config.protocolClasses = [StubProtocol.self]
        return config
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let request = request
        let route = Self.lock.withLock { () -> Route? in
            Self.seen.append(request)
            return Self.routes[request.url?.port ?? 80]
        }
        let reply = StubReply(self)
        self.reply = reply
        guard let route else {
            reply.fail(URLError(.cannotConnectToHost))
            return
        }
        DispatchQueue.global().async { route(request, reply) }
    }

    override func stopLoading() { reply?.stop() }
}

final class StubReply: @unchecked Sendable {
    private let lock = NSLock()
    private weak var proto: URLProtocol?
    private var stopped = false
    init(_ proto: URLProtocol) { self.proto = proto }

    var isStopped: Bool { lock.withLock { stopped } }
    func stop() { lock.withLock { stopped = true } }

    private func client(_ body: (URLProtocol, URLProtocolClient) -> Void) {
        lock.withLock {
            guard !stopped, let proto, let client = proto.client else { return }
            body(proto, client)
        }
    }

    func respond(_ status: Int = 200, _ headers: [String: String] = [:], url: URL? = nil) {
        client { proto, client in
            let response = HTTPURLResponse(url: url ?? proto.request.url!, statusCode: status,
                                           httpVersion: "HTTP/1.1", headerFields: headers)!
            client.urlProtocol(proto, didReceive: response, cacheStoragePolicy: .notAllowed)
        }
    }

    func send(_ text: String) {
        client { proto, client in client.urlProtocol(proto, didLoad: Data(text.utf8)) }
    }

    func finish() {
        client { proto, client in client.urlProtocolDidFinishLoading(proto) }
    }

    func fail(_ error: Error) {
        client { proto, client in client.urlProtocol(proto, didFailWithError: error) }
    }

    func redirect(to location: String) {
        client { proto, client in
            let target = URL(string: location)!
            let response = HTTPURLResponse(url: proto.request.url!, statusCode: 302, httpVersion: "HTTP/1.1",
                                           headerFields: ["Location": location])!
            var next = proto.request
            next.url = target
            client.urlProtocol(proto, wasRedirectedTo: next, redirectResponse: response)
        }
    }
}

// MARK: Recording task

@MainActor
final class FakeTask: SchemeTask {
    let request: URLRequest
    private(set) var response: HTTPURLResponse?
    private(set) var chunks: [(at: Date, data: Data)] = []
    private(set) var finished = false
    private(set) var endedAt: Date?
    private(set) var error: Error?
    private(set) var callsAfterEnd = 0
    private var waiter: CheckedContinuation<Void, Never>?

    init(_ url: String, method: String = "GET", headers: [String: String] = [:], body: Data? = nil) {
        var request = URLRequest(url: URL(string: url)!)
        request.httpMethod = method
        request.httpBody = body
        for (name, value) in headers { request.setValue(value, forHTTPHeaderField: name) }
        self.request = request
    }

    var id: ObjectIdentifier { ObjectIdentifier(self) }
    var ended: Bool { finished || error != nil }
    var body: String { String(decoding: chunks.reduce(Data()) { $0 + $1.data }, as: UTF8.self) }
    var headers: [String: String] { (response?.allHeaderFields as? [String: String]) ?? [:] }

    func didReceive(_ response: URLResponse) {
        if ended { callsAfterEnd += 1 }
        self.response = response as? HTTPURLResponse
    }

    func didReceive(_ data: Data) {
        if ended { callsAfterEnd += 1 }
        chunks.append((Date(), data))
    }

    func didFinish() {
        if ended { callsAfterEnd += 1 }
        finished = true
        endedAt = Date()
        waiter?.resume()
        waiter = nil
    }

    func didFailWithError(_ error: Error) {
        if ended { callsAfterEnd += 1 }
        self.error = error
        waiter?.resume()
        waiter = nil
    }

    func waitForEnd() async {
        if ended { return }
        await withCheckedContinuation { waiter = $0 }
    }
}

func pause(_ seconds: Double) async {
    try? await Task.sleep(for: .seconds(seconds))
}

// MARK: Tests

@MainActor
@Suite(.timeLimit(.minutes(1)))
struct HubSchemeHandlerTests {
    func handler(port: Int, cache: ShellCache? = nil, credential: String? = fakeCredential) -> HubSchemeHandler {
        HubSchemeHandler(endpoint: endpoint("http://127.0.0.1:\(port)"), cache: cache,
                         configuration: StubProtocol.configuration(), credential: { credential })
    }

    func tempCache() -> DiskShellCache {
        DiskShellCache(directory: FileManager.default.temporaryDirectory
            .appendingPathComponent("shell-handler-\(UUID())"))
    }

    @Test func streamsEachChunkAsTheHubSendsIt() async {
        let port = 20001
        StubProtocol.route(port: port) { _, reply in
            reply.respond(200, ["Content-Type": "text/event-stream"])
            for i in 0..<4 {
                reply.send("data: {\"i\":\(i)}\n\n")
                Thread.sleep(forTimeInterval: 0.15)
            }
            reply.finish()
        }
        let shell = handler(port: port)
        let task = FakeTask("swe-hub://box/v1/sessions/k/stream", headers: ["Accept": "text/event-stream"])
        let started = Date()
        shell.start(task, id: task.id)
        await task.waitForEnd()
        #expect(task.finished)
        #expect(task.response?.statusCode == 200)
        #expect(task.chunks.count == 4, "one didReceive per hub write, not one lump")
        // The first event reached the page while the hub was still writing
        // the rest (it sleeps 0.15 s between writes, 0.45 s after the first).
        // Spacing between later chunks is left out: on a loaded machine the
        // main queue can deliver two queued chunks back to back.
        let offsets = task.chunks.map { $0.at.timeIntervalSince(started) }
        let end = task.endedAt?.timeIntervalSince(started) ?? 0
        #expect(end - (offsets.first ?? end) > 0.3, "chunks \(offsets) arrived only at the end (\(end))")
        #expect(task.body.contains(#"{"i":3}"#))
        #expect(shell.inFlight == 0)
    }

    @Test func sendsTheCredentialPairHeaderAndOriginToThePinnedHub() async throws {
        let port = 20002
        StubProtocol.route(port: port) { _, reply in
            reply.respond(200, ["Content-Type": "application/json"])
            reply.send("{}")
            reply.finish()
        }
        let task = FakeTask("swe-hub://box/v1/sessions/k/send?x=1", method: "POST",
                            headers: ["Authorization": "Bearer page-made-this", "Content-Type": "application/json"],
                            body: Data(#"{"text":"hi"}"#.utf8))
        let shell = handler(port: port)
        shell.start(task, id: task.id)
        await task.waitForEnd()
        let sent = try #require(StubProtocol.requests(port: port).last)
        #expect(sent.url?.absoluteString == "http://127.0.0.1:\(port)/v1/sessions/k/send?x=1")
        #expect(sent.httpMethod == "POST")
        #expect(sent.value(forHTTPHeaderField: "Authorization") == "Bearer \(fakeCredential)")
        #expect(sent.value(forHTTPHeaderField: "X-Swe-Pair") == "1")
        #expect(sent.value(forHTTPHeaderField: "Origin") == "http://127.0.0.1:\(port)")
        #expect(sent.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(Self.bodyText(sent) == #"{"text":"hi"}"#)
    }

    @Test func refusesOtherHubsWithoutTouchingTheNetwork() async {
        let port = 20003
        let shell = handler(port: port)
        var refusals: [ShellRefusal] = []
        shell.onRefuse = { refusals.append($0) }
        for url in ["swe-hub://other/v1/status", "swe-hub://box:1/v1/status", "https://box/v1/status"] {
            let task = FakeTask(url)
            shell.start(task, id: task.id)
            await task.waitForEnd()
            #expect((task.error as NSError?)?.domain == HubSchemeHandler.errorDomain)
            #expect(task.response == nil)
        }
        #expect(refusals.count == 3)
        #expect(StubProtocol.requests(port: port).isEmpty)
        #expect(StubProtocol.requests(host: "other").isEmpty)
    }

    @Test func aRedirectOffTheHubIsRefusedAndTheCredentialNeverLeaves() async {
        let port = 20004
        StubProtocol.route(port: port) { _, reply in reply.redirect(to: "http://evil.example/steal") }
        let shell = handler(port: port)
        var refusals: [ShellRefusal] = []
        shell.onRefuse = { refusals.append($0) }
        let task = FakeTask("swe-hub://box/v1/status")
        shell.start(task, id: task.id)
        await task.waitForEnd()
        #expect((task.error as NSError?)?.domain == HubSchemeHandler.errorDomain)
        #expect(refusals == [.leavesOrigin("http://evil.example/steal")])
        await pause(0.2)
        #expect(StubProtocol.requests(host: "evil.example").isEmpty)
    }

    @Test func aRedirectOnTheHubKeepsTheHeaders() async throws {
        let port = 20005
        StubProtocol.route(port: port) { request, reply in
            if request.url?.path() == "/app/" {
                reply.redirect(to: "http://127.0.0.1:\(port)/")
            } else {
                reply.respond(200, ["Content-Type": "text/html"])
                reply.send("root")
                reply.finish()
            }
        }
        let shell = handler(port: port)
        let task = FakeTask("swe-hub://box/app/")
        shell.start(task, id: task.id)
        await task.waitForEnd()
        #expect(task.body == "root")
        #expect(task.response?.url?.absoluteString == "swe-hub://box/app/")
        let second = try #require(StubProtocol.requests(port: port).last)
        #expect(second.url?.path() == "/")
        #expect(second.value(forHTTPHeaderField: "Authorization") == "Bearer \(fakeCredential)")
        #expect(second.value(forHTTPHeaderField: "X-Swe-Pair") == "1")
    }

    @Test func nothingThePageReadsCarriesTheCredential() async throws {
        let port = 20006
        StubProtocol.route(port: port) { _, reply in
            reply.respond(200, ["Content-Type": "text/html", "X-Echo": "Bearer \(fakeCredential)",
                                "Set-Cookie": "sid=\(fakeCredential)"])
            reply.send("<p>page</p>")
            reply.finish()
        }
        let cache = tempCache()
        defer { cache.removeAll() }
        let shell = handler(port: port, cache: cache)
        let task = FakeTask("swe-hub://box/index.html")
        shell.start(task, id: task.id)
        await task.waitForEnd()
        let response = try #require(task.response)
        #expect(response.url?.absoluteString == "swe-hub://box/index.html")
        #expect(!response.url!.absoluteString.contains(fakeCredential))
        #expect(!task.headers.values.contains { $0.contains(fakeCredential) })
        #expect(!task.body.contains(fakeCredential))
        cache.flush()
        let saved = try #require(cache.cachedResponse(for: "/index.html"))
        #expect(!saved.headers.values.contains { $0.contains(fakeCredential) })
        let onDisk = try Data(contentsOf: cache.file(for: "/index.html"))
        #expect(!String(decoding: onDisk, as: UTF8.self).contains(fakeCredential))
    }

    @Test func answersFromTheCacheWhileTheHubIsDown() async throws {
        let port = 20007
        StubProtocol.route(port: port) { _, reply in
            reply.respond(200, ["Content-Type": "application/json"])
            reply.send(#"{"saved":"one"}"#)
            reply.finish()
        }
        let cache = tempCache()
        defer { cache.removeAll() }
        let shell = handler(port: port, cache: cache)
        var served: [ShellSource] = []
        shell.onServe = { _, source in served.append(source) }
        let online = FakeTask("swe-hub://box/v1/sessions/k/snapshot")
        shell.start(online, id: online.id)
        await online.waitForEnd()
        #expect(online.headers[ShellSource.header] == "network")

        // Hub stopped: connection refused.
        StubProtocol.route(port: port) { _, reply in reply.fail(URLError(.cannotConnectToHost)) }
        let offline = FakeTask("swe-hub://box/v1/sessions/k/snapshot")
        shell.start(offline, id: offline.id)
        await offline.waitForEnd()
        #expect(offline.finished)
        #expect(offline.response?.statusCode == 200)
        #expect(offline.headers[ShellSource.header] == "cache")
        #expect(offline.body == #"{"saved":"one"}"#)

        // tailscale serve in front of a stopped hub answers 502.
        StubProtocol.route(port: port) { _, reply in
            reply.respond(502, ["Content-Type": "text/plain"])
            reply.send("bad gateway")
            reply.finish()
        }
        let proxied = FakeTask("swe-hub://box/v1/sessions/k/snapshot")
        shell.start(proxied, id: proxied.id)
        await proxied.waitForEnd()
        #expect(proxied.headers[ShellSource.header] == "cache")
        #expect(proxied.body == #"{"saved":"one"}"#)
        #expect(served == [.network, .cache, .cache])

        // Nothing saved for this one: the failure reaches the page.
        StubProtocol.route(port: port) { _, reply in reply.fail(URLError(.cannotConnectToHost)) }
        let missing = FakeTask("swe-hub://box/v1/other")
        shell.start(missing, id: missing.id)
        await missing.waitForEnd()
        #expect((missing.error as? URLError)?.code == .cannotConnectToHost)
        // A send never replays a saved answer.
        let send = FakeTask("swe-hub://box/v1/sessions/k/snapshot", method: "POST")
        shell.start(send, id: send.id)
        await send.waitForEnd()
        #expect((send.error as? URLError)?.code == .cannotConnectToHost)
        #expect(online.callsAfterEnd + offline.callsAfterEnd + proxied.callsAfterEnd == 0)
    }

    @Test func streamsAndFailuresAreNeverSaved() async {
        let port = 20008
        StubProtocol.route(port: port) { request, reply in
            if request.url?.path() == "/v1/stream" {
                reply.respond(200, ["Content-Type": "text/event-stream"])
                reply.send("data: 1\n\n")
            } else {
                reply.respond(404, ["Content-Type": "text/plain"])
                reply.send("missing")
            }
            reply.finish()
        }
        let cache = tempCache()
        defer { cache.removeAll() }
        let shell = handler(port: port, cache: cache)
        for path in ["/v1/stream", "/v1/missing"] {
            let task = FakeTask("swe-hub://box\(path)")
            shell.start(task, id: task.id)
            await task.waitForEnd()
            #expect(task.finished)
            #expect(cache.cachedResponse(for: path) == nil)
        }
    }

    @Test func stopEndsTheStreamAndTheTaskIsNeverTouchedAgain() async {
        let port = 20009
        StubProtocol.route(port: port) { _, reply in
            reply.respond(200, ["Content-Type": "text/event-stream"])
            reply.send("data: first\n\n")
            Thread.sleep(forTimeInterval: 0.3)
            reply.send("data: second\n\n")
            reply.finish()
        }
        let shell = handler(port: port)
        let task = FakeTask("swe-hub://box/v1/stream", headers: ["Accept": "text/event-stream"])
        shell.start(task, id: task.id)
        await pause(0.15)
        #expect(task.chunks.count == 1)
        shell.stop(id: task.id)
        #expect(shell.inFlight == 0)
        await pause(0.4)
        #expect(task.chunks.count == 1)
        #expect(!task.ended)
    }

    @Test func noCredentialStillPassesTheOwnerGate() async throws {
        let port = 20010
        StubProtocol.route(port: port) { _, reply in
            reply.respond(204)
            reply.finish()
        }
        let shell = handler(port: port, credential: nil)
        let task = FakeTask("swe-hub://box/v1/status")
        shell.start(task, id: task.id)
        await task.waitForEnd()
        let sent = try #require(StubProtocol.requests(port: port).last)
        #expect(sent.value(forHTTPHeaderField: "Authorization") == nil)
        #expect(sent.value(forHTTPHeaderField: "X-Swe-Pair") == "1")
    }

    static func bodyText(_ request: URLRequest) -> String? {
        if let body = request.httpBody { return String(decoding: body, as: UTF8.self) }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let n = stream.read(&buffer, maxLength: buffer.count)
            if n <= 0 { break }
            data.append(buffer, count: n)
        }
        return String(decoding: data, as: UTF8.self)
    }
}
