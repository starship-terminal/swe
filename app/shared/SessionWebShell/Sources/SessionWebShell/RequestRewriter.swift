import Foundation

/// Why the shell refused a request instead of sending it to the hub.
public enum ShellRefusal: Error, Equatable, Sendable {
    /// Not `swe-hub://`.
    case wrongScheme(String?)
    /// A `swe-hub://` URL naming a hub other than the pinned one, or one
    /// carrying a port or user info.
    case wrongHub(String)
    /// A redirect, or a rewritten URL, that would leave the hub's origin.
    case leavesOrigin(String)
    /// A URL that could not be rebuilt on the hub's origin.
    case malformed(String)
}

/// Turns the page's `swe-hub://<id>/...` requests into requests to the
/// pinned hub, and the hub's responses back into what the page may see.
/// Pure: no networking, so every rule here has a unit test.
public struct RequestRewriter: Sendable {
    public let endpoint: HubEndpoint

    public init(endpoint: HubEndpoint) { self.endpoint = endpoint }

    /// Headers the page may not set. The shell owns the credential and the
    /// headers the hub's owner gate reads; cookies and the page's own
    /// `swe-hub://` referer mean nothing to the hub.
    static let ownedRequestHeaders: Set<String> = [
        "authorization", "cookie", "origin", "host", "referer",
        "x-swe-pair", "x-quiver-pair", "proxy-authorization",
        "connection", "content-length", "transfer-encoding",
    ]

    /// Headers the page never gets back. URLSession has already undone any
    /// content coding, so the hub's coding and length no longer describe
    /// the bytes; cookies and auth challenges stay native-side.
    static let droppedResponseHeaders: Set<String> = [
        "set-cookie", "set-cookie2", "www-authenticate", "authorization",
        "content-encoding", "content-length", "transfer-encoding", "connection",
        "keep-alive", "proxy-authenticate",
    ]

    /// The hub-side URL for a page URL. Path and query are kept byte for
    /// byte; scheme, host and port always come from the pinned origin.
    public func hubURL(for pageURL: URL) throws(ShellRefusal) -> URL {
        guard pageURL.scheme?.lowercased() == HubEndpoint.scheme else {
            throw .wrongScheme(pageURL.scheme)
        }
        guard endpoint.isPage(pageURL) else { throw .wrongHub(pageURL.absoluteString) }
        guard let source = URLComponents(url: pageURL, resolvingAgainstBaseURL: false) else {
            throw .malformed(pageURL.absoluteString)
        }
        var target = URLComponents()
        target.scheme = endpoint.origin.scheme
        target.host = endpoint.origin.host(percentEncoded: false)
        target.port = endpoint.origin.port
        let path = source.percentEncodedPath
        target.percentEncodedPath = path.isEmpty ? "/" : path
        target.percentEncodedQuery = source.percentEncodedQuery
        guard let url = target.url, endpoint.isHubOrigin(url) else {
            throw .malformed(pageURL.absoluteString)
        }
        return url
    }

    /// The request the shell sends for the page's `request`: same method,
    /// body and page headers, on the hub's origin, with the credential (when
    /// there is one), `X-Swe-Pair: 1` and the hub's own `Origin` added.
    public func hubRequest(for request: URLRequest, credential: String?,
                           timeout: TimeInterval? = nil) throws(ShellRefusal) -> URLRequest {
        guard let pageURL = request.url else { throw .malformed("") }
        var out = URLRequest(url: try hubURL(for: pageURL))
        out.httpMethod = request.httpMethod ?? "GET"
        if let body = request.httpBody {
            out.httpBody = body
        } else if let stream = request.httpBodyStream {
            out.httpBodyStream = stream
        }
        for (name, value) in request.allHTTPHeaderFields ?? [:]
        where !Self.ownedRequestHeaders.contains(name.lowercased()) {
            out.setValue(value, forHTTPHeaderField: name)
        }
        applyOwnedHeaders(to: &out, credential: credential)
        out.cachePolicy = .reloadIgnoringLocalCacheData
        out.httpShouldHandleCookies = false
        if let timeout { out.timeoutInterval = timeout }
        return out
    }

    /// A redirect the hub answered with: followed only while it stays on the
    /// hub's origin, with the shell's headers put back on (URLSession may
    /// drop them across a redirect). Anything else is refused, so the
    /// credential never goes to another host.
    public func followRedirect(_ proposed: URLRequest, credential: String?) throws(ShellRefusal) -> URLRequest {
        guard let url = proposed.url, endpoint.isHubOrigin(url) else {
            throw .leavesOrigin(proposed.url?.absoluteString ?? "")
        }
        var out = proposed
        applyOwnedHeaders(to: &out, credential: credential)
        return out
    }

    private func applyOwnedHeaders(to request: inout URLRequest, credential: String?) {
        if let credential, !credential.isEmpty {
            request.setValue("Bearer \(credential)", forHTTPHeaderField: "Authorization")
        } else {
            request.setValue(nil, forHTTPHeaderField: "Authorization")
        }
        request.setValue("1", forHTTPHeaderField: "X-Swe-Pair")
        request.setValue(endpoint.originString, forHTTPHeaderField: "Origin")
    }

    /// What the page receives for a hub response: the page's own URL (so
    /// the page stays same-origin with itself), the hub's status, and its
    /// headers minus the ones in `droppedResponseHeaders` and any whose
    /// value carries the credential. `source` says whether it came from the
    /// network or the offline cache.
    public func pageResponse(for pageURL: URL, status: Int, headers: [String: String],
                             credential: String?, source: ShellSource) -> HTTPURLResponse {
        var fields: [String: String] = [:]
        for (name, value) in headers {
            if Self.droppedResponseHeaders.contains(name.lowercased()) { continue }
            if let credential, !credential.isEmpty, value.contains(credential) { continue }
            fields[name] = value
        }
        fields[ShellSource.header] = source.rawValue
        return HTTPURLResponse(url: pageURL, statusCode: status, httpVersion: "HTTP/1.1",
                               headerFields: fields)!
    }
}

/// Where a response the page got came from; sent to the page as the
/// `X-Swe-Shell-Source` header so it can say "offline, showing a saved copy".
public enum ShellSource: String, Sendable {
    case network
    case cache

    public static let header = "X-Swe-Shell-Source"
}
