import Foundation

/// What the session web view does with a navigation. Same rule as the Mac
/// app's `HubNavigation`, on the `swe-hub://` origin instead of the hub's.
public enum ShellNavigation: Equatable, Sendable {
    /// Load it in the web view.
    case allow
    /// Hand it to the delegate (the app opens it in the browser or mail).
    case openExternally
    /// Drop it.
    case cancel

    /// Only the pinned hub's page origin loads in the view, plus
    /// `about:blank`/`about:srcdoc` and the page's own `blob:` URLs. A link
    /// the user clicked to an http(s) site or a `mailto:` goes out to the
    /// delegate; anything else (a script or redirect moving the view, the
    /// hub's real `http://` origin, `file:`) never loads.
    public static func decide(_ url: URL?, endpoint: HubEndpoint, userInitiated: Bool) -> ShellNavigation {
        guard let url, let scheme = url.scheme?.lowercased() else { return .cancel }
        switch scheme {
        case HubEndpoint.scheme:
            return endpoint.isPage(url) ? .allow : .cancel
        case "about":
            return ["blank", "srcdoc"].contains(url.absoluteString.dropFirst("about:".count).lowercased())
                ? .allow : .cancel
        case "blob":
            // blob:swe-hub://<id>/<uuid>
            let inner = URL(string: String(url.absoluteString.dropFirst("blob:".count)))
            return inner.map(endpoint.isPage) == true ? .allow : .cancel
        case "http", "https", "mailto":
            return userInitiated ? .openExternally : .cancel
        default:
            return .cancel
        }
    }
}
