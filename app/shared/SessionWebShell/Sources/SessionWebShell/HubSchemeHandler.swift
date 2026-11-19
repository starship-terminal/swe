import Foundation
import WebKit

/// The part of `WKURLSchemeTask` the handler uses, so tests can drive the
/// handler with a recording fake instead of a live web view.
@MainActor
public protocol SchemeTask: AnyObject {
    var request: URLRequest { get }
    func didReceive(_ response: URLResponse)
    func didReceive(_ data: Data)
    func didFinish()
    func didFailWithError(_ error: Error)
}

/// Answers every `swe-hub://<id>/...` request the page makes by sending it
/// to the pinned hub with the device's credential, and streams the hub's
/// body back chunk by chunk as URLSession delivers it, so an event stream
/// reaches the page as it is produced. When the hub can't be reached it
/// answers cacheable GETs (page assets, the last snapshot) from `cache`.
///
/// The credential comes from `credential` on each request and only ever
/// goes into the outgoing `Authorization` header: nothing the page can
/// read (its URLs, the response it gets, the cache) carries it.
@MainActor
public final class HubSchemeHandler: NSObject, WKURLSchemeHandler {
    public let rewriter: RequestRewriter
    public let cache: ShellCache?
    /// The idle timeout for cacheable GETs, so a hub that is gone falls
    /// back to the saved copy quickly. Streams and sends keep URLSession's
    /// default.
    public var cacheableTimeout: TimeInterval = 6
    /// Called for every answered request with its cache key and source,
    /// and for every refusal; the proof harness reports these.
    public var onServe: ((String, ShellSource) -> Void)?
    public var onRefuse: ((ShellRefusal) -> Void)?

    private let credential: @MainActor () -> String?
    private let session: URLSession
    private var running: [ObjectIdentifier: Forward] = [:]

    /// - Parameters:
    ///   - credential: the device's bearer for this hub, read per request;
    ///     nil sends none (the owner's tailnet devices need none).
    ///   - configuration: tests pass one with a stub `URLProtocol`.
    public init(endpoint: HubEndpoint, cache: ShellCache? = nil,
                configuration: URLSessionConfiguration = HubSchemeHandler.defaultConfiguration(),
                credential: @escaping @MainActor () -> String?) {
        rewriter = RequestRewriter(endpoint: endpoint)
        self.cache = cache
        self.credential = credential
        // Delegate callbacks on the main queue: WKURLSchemeTask is answered
        // on the main thread, and the per-task delegates assume it.
        session = URLSession(configuration: configuration, delegate: nil, delegateQueue: .main)
        super.init()
    }

    deinit {
        session.invalidateAndCancel()
    }

    /// No URL cache, no cookie jar, no stored credentials: the shell's own
    /// cache and the provider closure are the only state.
    public nonisolated static func defaultConfiguration() -> URLSessionConfiguration {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.urlCredentialStorage = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return config
    }

    public var endpoint: HubEndpoint { rewriter.endpoint }

    // MARK: WKURLSchemeHandler

    public func webView(_ webView: WKWebView, start urlSchemeTask: any WKURLSchemeTask) {
        start(WebKitTask(urlSchemeTask), id: ObjectIdentifier(urlSchemeTask))
    }

    public func webView(_ webView: WKWebView, stop urlSchemeTask: any WKURLSchemeTask) {
        stop(id: ObjectIdentifier(urlSchemeTask))
    }

    // MARK: The work, reachable from tests

    /// Starts answering `task`. `id` names it for `stop(id:)`.
    public func start(_ task: SchemeTask, id: ObjectIdentifier) {
        let request = task.request
        let cacheable = cache != nil && CachePolicy.isCacheable(request)
        let outgoing: URLRequest
        let credential = credential()
        do {
            outgoing = try rewriter.hubRequest(for: request, credential: credential,
                                               timeout: cacheable ? cacheableTimeout : nil)
        } catch {
            onRefuse?(error)
            task.didFailWithError(Self.nsError(error))
            return
        }
        let forward = Forward(handler: self, task: task, pageRequest: request,
                              cacheable: cacheable, credential: credential)
        running[id] = forward
        forward.finished = { [weak self] in self?.running[id] = nil }
        let dataTask = session.dataTask(with: outgoing)
        dataTask.delegate = forward
        forward.dataTask = dataTask
        dataTask.resume()
    }

    /// WebKit no longer wants the answer (navigation away, `AbortController`,
    /// a closed `EventSource`): cancel the hub request and never touch the
    /// task again, since WebKit raises on a stopped task.
    public func stop(id: ObjectIdentifier) {
        guard let forward = running.removeValue(forKey: id) else { return }
        forward.stop()
    }

    /// How many requests are in flight; tests use it.
    public var inFlight: Int { running.count }

    fileprivate func serve(_ key: String, _ source: ShellSource) { onServe?(key, source) }
    fileprivate func refused(_ refusal: ShellRefusal) { onRefuse?(refusal) }

    public static let errorDomain = "SessionWebShell"

    static func nsError(_ refusal: ShellRefusal) -> NSError {
        NSError(domain: errorDomain, code: 1,
                userInfo: [NSLocalizedDescriptionKey: "The session shell refused this request (\(refusal))."])
    }
}

/// Wraps WebKit's task in `SchemeTask`.
@MainActor
private final class WebKitTask: SchemeTask {
    let base: any WKURLSchemeTask
    init(_ base: any WKURLSchemeTask) { self.base = base }
    var request: URLRequest { base.request }
    func didReceive(_ response: URLResponse) { base.didReceive(response) }
    func didReceive(_ data: Data) { base.didReceive(data) }
    func didFinish() { base.didFinish() }
    func didFailWithError(_ error: Error) { base.didFailWithError(error) }
}

/// One page request in flight to the hub. Its URLSession callbacks run on
/// the main queue (the handler's session uses it), hence `assumeIsolated`.
private final class Forward: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private weak var handler: HubSchemeHandler?
    private let task: SchemeTask
    private let pageRequest: URLRequest
    private let cacheable: Bool
    private let credential: String?
    var dataTask: URLSessionDataTask?
    var finished: (@MainActor () -> Void)?

    private var stopped = false
    private var responded = false
    private var status = 0
    private var pageHeaders: [String: String] = [:]
    private var buffer = Data()
    private var overflow = false

    @MainActor
    init(handler: HubSchemeHandler, task: SchemeTask, pageRequest: URLRequest,
         cacheable: Bool, credential: String?) {
        self.handler = handler
        self.task = task
        self.pageRequest = pageRequest
        self.cacheable = cacheable
        self.credential = credential
    }

    private var key: String { CachePolicy.key(for: pageRequest.url!) }

    @MainActor
    func stop() {
        stopped = true
        // The task keeps its delegate until it completes (URLSession refuses
        // a delegate change after resume); `stopped` keeps it quiet.
        dataTask?.cancel()
        finished = nil
    }

    @MainActor
    private func done() {
        finished?()
        finished = nil
    }

    // MARK: URLSessionDataDelegate

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void) {
        let go = MainActor.assumeIsolated { () -> Bool in
            guard !stopped, let handler, let http = response as? HTTPURLResponse else { return false }
            let headers = Self.stringHeaders(http)
            if CachePolicy.fallback(for: pageRequest, error: nil, status: http.statusCode,
                                    hasCopy: cachedCopy() != nil) == .serveCached {
                serveCached()
                return false
            }
            let page = handler.rewriter.pageResponse(for: pageRequest.url!, status: http.statusCode,
                                                     headers: headers, credential: credential,
                                                     source: .network)
            status = http.statusCode
            pageHeaders = Self.stringHeaders(page).filter { $0.key != ShellSource.header }
            responded = true
            task.didReceive(page)
            handler.serve(key, .network)
            return true
        }
        completionHandler(go ? .allow : .cancel)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        MainActor.assumeIsolated {
            guard !stopped, responded else { return }
            task.didReceive(data)
            guard cacheable, !overflow else { return }
            if buffer.count + data.count > CachePolicy.maxBodyBytes {
                overflow = true
                buffer = Data()
            } else {
                buffer.append(data)
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        MainActor.assumeIsolated {
            guard !stopped else { return }
            defer { done() }
            if let error {
                if responded {
                    self.task.didFailWithError(error)
                } else if CachePolicy.fallback(for: pageRequest, error: error, status: nil,
                                               hasCopy: cachedCopy() != nil) == .serveCached {
                    serveCached()
                } else if (error as NSError).code != NSURLErrorCancelled {
                    self.task.didFailWithError(error)
                }
                return
            }
            guard responded else { return }
            self.task.didFinish()
            if cacheable, !overflow, let cache = handler?.cache,
               CachePolicy.shouldStore(status: status, headers: pageHeaders, bodyBytes: buffer.count) {
                cache.store(CachedResponse(status: status, headers: pageHeaders, body: buffer), for: key)
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        let next = MainActor.assumeIsolated { () -> URLRequest? in
            guard !stopped, let handler else { return nil }
            do throws(ShellRefusal) {
                return try handler.rewriter.followRedirect(request, credential: credential)
            } catch {
                handler.refused(error)
                stopped = true
                self.task.didFailWithError(HubSchemeHandler.nsError(error))
                task.cancel()
                done()
                return nil
            }
        }
        completionHandler(next)
    }

    // MARK: Cache

    @MainActor
    private func cachedCopy() -> CachedResponse? {
        guard cacheable else { return nil }
        return handler?.cache?.cachedResponse(for: key)
    }

    /// Answers from the saved copy. Only called before anything reached
    /// the page, so the task still wants a response.
    @MainActor
    private func serveCached() {
        guard let copy = cachedCopy(), let handler else { return }
        stopped = true
        dataTask?.cancel()
        let page = handler.rewriter.pageResponse(for: pageRequest.url!, status: copy.status,
                                                 headers: copy.headers, credential: credential,
                                                 source: .cache)
        task.didReceive(page)
        task.didReceive(copy.body)
        task.didFinish()
        handler.serve(key, .cache)
        done()
    }

    private static func stringHeaders(_ response: HTTPURLResponse) -> [String: String] {
        var out: [String: String] = [:]
        for (name, value) in response.allHeaderFields {
            if let name = name as? String { out[name] = "\(value)" }
        }
        return out
    }
}
