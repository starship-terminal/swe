import CryptoKit
import Foundation

/// One hub the shell talks to: the name the page sees (`swe-hub://<id>/`)
/// and the real origin every request goes to. The page never learns the
/// real origin or the credential; it only ever sees `swe-hub://<id>`.
public struct HubEndpoint: Hashable, Sendable {
    /// The custom scheme every session page loads from.
    public static let scheme = "swe-hub"

    /// The page-facing host, a lowercase DNS label (WebKit lowercases URL
    /// hosts, so an uppercase id would never match its own requests).
    public let id: String
    /// The hub's own origin: http or https, a host, an optional port, and
    /// nothing else.
    public let origin: URL

    public enum Problem: Error, Equatable, Sendable {
        case badID(String)
        case badOrigin(String)
    }

    public init(id: String, origin: URL) throws {
        guard Self.isLabel(id) else { throw Problem.badID(id) }
        guard let scheme = origin.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = origin.host(percentEncoded: false), !host.isEmpty,
              origin.user == nil, origin.password == nil,
              origin.query == nil, origin.fragment == nil,
              origin.path.isEmpty || origin.path == "/"
        else { throw Problem.badOrigin(origin.absoluteString) }
        var parts = URLComponents()
        parts.scheme = scheme
        parts.host = host.lowercased()
        parts.port = origin.port
        guard let clean = parts.url else { throw Problem.badOrigin(origin.absoluteString) }
        self.id = id
        self.origin = clean
    }

    /// `http://127.0.0.1:8765`: the value the hub's Origin check expects.
    public var originString: String { origin.absoluteString }

    /// The page's own root, `swe-hub://<id>/`.
    public var pageRoot: URL { URL(string: "\(Self.scheme)://\(id)/")! }

    /// A page URL for `path` (with an optional query), on this hub.
    public func pageURL(_ path: String, query: String? = nil) -> URL? {
        var parts = URLComponents()
        parts.scheme = Self.scheme
        parts.host = id
        parts.path = path.hasPrefix("/") ? path : "/" + path
        parts.percentEncodedQuery = query
        return parts.url
    }

    /// Whether `url` is this hub's page origin: the custom scheme and this
    /// id, with no port or user info.
    public func isPage(_ url: URL) -> Bool {
        url.scheme?.lowercased() == Self.scheme
            && url.host(percentEncoded: false)?.lowercased() == id
            && url.port == nil && url.user == nil && url.password == nil
    }

    /// Whether `url` is on the hub's real origin (same scheme, host, port).
    public func isHubOrigin(_ url: URL) -> Bool {
        url.scheme?.lowercased() == origin.scheme
            && url.host(percentEncoded: false)?.lowercased() == origin.host(percentEncoded: false)
            && Self.effectivePort(url) == Self.effectivePort(origin)
            && url.user == nil && url.password == nil
    }

    /// A per-hub web data store id: the same UUID every launch, so the
    /// page's IndexedDB survives a restart and no two hubs share one.
    /// Same shape as the Mac app's `HubOrigin.dataStoreID`.
    public var dataStoreID: UUID {
        var bytes = Array(SHA256.hash(data: Data("swe.session-shell:\(id)@\(originString)".utf8)).prefix(16))
        bytes[6] = (bytes[6] & 0x0F) | 0x50
        bytes[8] = (bytes[8] & 0x3F) | 0x80
        return UUID(uuid: (bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
                           bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]))
    }

    static func effectivePort(_ url: URL) -> Int? {
        if let port = url.port { return port }
        switch url.scheme?.lowercased() {
        case "http": return 80
        case "https": return 443
        default: return nil
        }
    }

    static func isLabel(_ id: String) -> Bool {
        guard (1...63).contains(id.utf8.count), id.first != "-", id.last != "-" else { return false }
        return id.utf8.allSatisfy { ($0 >= 0x61 && $0 <= 0x7A) || ($0 >= 0x30 && $0 <= 0x39) || $0 == 0x2D }
    }
}
