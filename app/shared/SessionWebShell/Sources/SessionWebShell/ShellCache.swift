import CryptoKit
import Foundation

/// A saved hub response: what the handler answers with when the hub can't
/// be reached.
public struct CachedResponse: Codable, Equatable, Sendable {
    public var status: Int
    public var headers: [String: String]
    public var body: Data
    public var savedAt: Date

    public init(status: Int, headers: [String: String], body: Data, savedAt: Date = Date()) {
        self.status = status
        self.headers = headers
        self.body = body
        self.savedAt = savedAt
    }
}

/// The handler's offline store. Keys are the page URL's path and query
/// (`/session/index.html`, `/v1/sessions/k/snapshot?x=1`), so one store
/// belongs to one hub. Called on the main actor, so a store keeps its
/// own work small (the disk store writes off the main thread).
@MainActor
public protocol ShellCache: AnyObject {
    func cachedResponse(for key: String) -> CachedResponse?
    func store(_ response: CachedResponse, for key: String)
}

/// When the handler saves a response and when it answers from the cache.
public enum CachePolicy {
    /// The largest body the handler keeps; bigger responses pass through
    /// unsaved.
    public static let maxBodyBytes = 8 << 20

    /// The cache key for a page URL: its percent-encoded path and query.
    public static func key(for pageURL: URL) -> String {
        let parts = URLComponents(url: pageURL, resolvingAgainstBaseURL: false)
        let path = parts?.percentEncodedPath ?? "/"
        let query = parts?.percentEncodedQuery.map { "?" + $0 } ?? ""
        return (path.isEmpty ? "/" : path) + query
    }

    /// Only plain GETs that are not streams may be saved or answered from
    /// the cache: a POST is a send, and an event stream has no "last copy".
    public static func isCacheable(_ request: URLRequest) -> Bool {
        let method = (request.httpMethod ?? "GET").uppercased()
        guard method == "GET" else { return false }
        let accept = request.value(forHTTPHeaderField: "Accept")?.lowercased() ?? ""
        return !accept.contains("text/event-stream")
    }

    /// Whether to save a response the hub sent for a cacheable request:
    /// a 200 that isn't a stream, isn't marked `no-store`, and fits.
    public static func shouldStore(status: Int, headers: [String: String], bodyBytes: Int) -> Bool {
        guard status == 200, bodyBytes <= maxBodyBytes else { return false }
        let lower = Dictionary(headers.map { ($0.key.lowercased(), $0.value.lowercased()) },
                               uniquingKeysWith: { first, _ in first })
        if lower["content-type"]?.contains("text/event-stream") == true { return false }
        if lower["cache-control"]?.contains("no-store") == true { return false }
        return true
    }

    /// The outcomes `fallback` chooses between.
    public enum Fallback: Equatable, Sendable {
        /// Answer with the saved copy.
        case serveCached
        /// Pass the failure (or the hub's error status) through to the page.
        case passThrough
    }

    /// The hub can't be reached (`error`), or something in front of it
    /// answered that it is down (`status` 502, 503 or 504, which
    /// `tailscale serve` sends while the hub is stopped): answer a cacheable
    /// request from the cache when there is a copy, otherwise pass the
    /// failure through. Any other status is the hub's real answer.
    public static func fallback(for request: URLRequest, error: Error?, status: Int?,
                                hasCopy: Bool) -> Fallback {
        guard hasCopy, isCacheable(request) else { return .passThrough }
        if let status { return [502, 503, 504].contains(status) ? .serveCached : .passThrough }
        guard let error else { return .passThrough }
        return isUnreachable(error) ? .serveCached : .passThrough
    }

    /// Network failures that mean "the hub isn't there", as opposed to a
    /// cancel or a refusal by the shell itself.
    public static func isUnreachable(_ error: Error) -> Bool {
        let error = error as NSError
        guard error.domain == NSURLErrorDomain else { return false }
        return [
            NSURLErrorCannotConnectToHost, NSURLErrorNotConnectedToInternet,
            NSURLErrorTimedOut, NSURLErrorNetworkConnectionLost, NSURLErrorCannotFindHost,
            NSURLErrorDNSLookupFailed, NSURLErrorInternationalRoamingOff,
            NSURLErrorDataNotAllowed, NSURLErrorSecureConnectionFailed,
        ].contains(error.code)
    }
}

/// A `ShellCache` on disk: one JSON file per key under `directory`, named
/// by the key's SHA-256. Reads are synchronous (small files, needed before
/// the handler can answer); writes go to a background queue.
@MainActor
public final class DiskShellCache: ShellCache {
    public let directory: URL
    private var memory: [String: CachedResponse] = [:]
    private let writer = DispatchQueue(label: "swe.session-shell.cache", qos: .utility)

    public init(directory: URL) {
        self.directory = directory
    }

    public func cachedResponse(for key: String) -> CachedResponse? {
        if let hit = memory[key] { return hit }
        guard let data = try? Data(contentsOf: file(for: key)),
              let entry = try? JSONDecoder().decode(CachedResponse.self, from: data)
        else { return nil }
        memory[key] = entry
        return entry
    }

    public func store(_ response: CachedResponse, for key: String) {
        memory[key] = response
        let file = file(for: key)
        let directory = directory
        writer.async {
            guard let data = try? JSONEncoder().encode(response) else { return }
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try? data.write(to: file, options: .atomic)
        }
    }

    /// Waits for queued writes; for tests and for an app about to exit.
    public func flush() {
        writer.sync {}
    }

    /// Drops every saved response.
    public func removeAll() {
        memory.removeAll()
        let directory = directory
        writer.sync { _ = try? FileManager.default.removeItem(at: directory) }
    }

    func file(for key: String) -> URL {
        let name = SHA256.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent(name + ".json")
    }
}
