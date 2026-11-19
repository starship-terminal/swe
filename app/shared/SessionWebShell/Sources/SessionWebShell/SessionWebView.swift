import Foundation
import WebKit
#if canImport(UIKit)
import UIKit
#endif

/// The app's light or dark choice, handed to the page as `data-theme` on
/// `<html>` (the web app's own Settings switch uses the same attribute)
/// and as the view's appearance, so `prefers-color-scheme` agrees.
public enum ShellAppearance: String, Sendable {
    case light
    case dark
}

/// What the app does with the shell's outward requests.
@MainActor
public protocol SessionWebDelegate: AnyObject {
    /// A link the user clicked to another origin (http, https, mailto).
    func sessionWeb(_ controller: SessionWebController, openExternally url: URL)
    /// A `sweNative` call from the page, decoded (or why it was refused).
    func sessionWeb(_ controller: SessionWebController, bridge message: Result<BridgeMessage, BridgeError>)
}

/// The JavaScript the shell runs to pass native state into the page. Pure
/// strings, so tests can check them.
public enum ShellScript {
    /// Sets `--keyboard-inset` (CSS px; one CSS px is one point at the
    /// shell's fixed zoom) and `--keyboard-duration` on `<html>`.
    public static func keyboard(inset: Double, duration: Double) -> String {
        let px = max(0, (inset * 100).rounded() / 100)
        let ms = max(0, Int((duration * 1000).rounded()))
        return "document.documentElement.style.setProperty('--keyboard-inset','\(px)px');"
            + "document.documentElement.style.setProperty('--keyboard-duration','\(ms)ms');"
    }

    /// Sets `--safe-area-top` and `--safe-area-bottom` (CSS px) on `<html>`:
    /// the view's own safe area, from UIKit. WebKit fills in
    /// `env(safe-area-inset-*)` itself, but on the proof's first loads it
    /// sometimes read 0 at first paint, so the page prefers these.
    public static func safeArea(top: Double, bottom: Double) -> String {
        let t = max(0, (top * 100).rounded() / 100)
        let b = max(0, (bottom * 100).rounded() / 100)
        return "document.documentElement.style.setProperty('--safe-area-top','\(t)px');"
            + "document.documentElement.style.setProperty('--safe-area-bottom','\(b)px');"
    }

    /// Sets `data-theme` and `color-scheme` on `<html>`.
    public static func appearance(_ appearance: ShellAppearance) -> String {
        "document.documentElement.dataset.theme='\(appearance.rawValue)';"
            + "document.documentElement.style.colorScheme='\(appearance.rawValue)';"
    }

    /// Runs at document start on every load: the bridge, then the current
    /// appearance and keyboard inset, so the first paint is already right.
    static func documentStart(appearance: ShellAppearance, inset: Double,
                              safeTop: Double = 0, safeBottom: Double = 0) -> String {
        NativeBridge.userScript + "\n" + Self.appearance(appearance) + Self.keyboard(inset: inset, duration: 0)
            + Self.safeArea(top: safeTop, bottom: safeBottom)
    }
}

/// Builds the session web view with the settings the session view needs.
public enum SessionWebViewFactory {
    /// The configuration: the `swe-hub` handler, the hub's own persistent
    /// data store (IndexedDB for the outbox survives a relaunch), the
    /// bridge, and no pop-up windows.
    @MainActor
    public static func configuration(handler: HubSchemeHandler, bridge: BridgeReceiver,
                                     dataStore: WKWebsiteDataStore) -> WKWebViewConfiguration {
        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(handler, forURLScheme: HubEndpoint.scheme)
        config.websiteDataStore = dataStore
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        config.userContentController.add(bridge, contentWorld: .page, name: NativeBridge.handlerName)
        #if canImport(UIKit)
        config.allowsInlineMediaPlayback = true
        // Text stays at the page's size; the page follows Dynamic Type itself.
        config.ignoresViewportScaleLimits = false
        #endif
        return config
    }

    /// The view. On iPhone only the page's own timeline scrolls: the outer
    /// scroll view is off, never bounces and ignores the safe area (the
    /// page lays itself out with `env(safe-area-inset-*)` and
    /// `--keyboard-inset`). No back/forward swipes, no link previews.
    @MainActor
    public static func makeWebView(configuration: WKWebViewConfiguration) -> WKWebView {
        let view = ShellWebView(frame: .zero, configuration: configuration)
        view.allowsBackForwardNavigationGestures = false
        view.allowsLinkPreview = false
        #if canImport(UIKit)
        let scroll = view.scrollView
        scroll.isScrollEnabled = false
        scroll.bounces = false
        scroll.alwaysBounceVertical = false
        scroll.alwaysBounceHorizontal = false
        scroll.bouncesZoom = false
        scroll.contentInsetAdjustmentBehavior = .never
        scroll.automaticallyAdjustsScrollIndicatorInsets = false
        scroll.showsVerticalScrollIndicator = false
        scroll.showsHorizontalScrollIndicator = false
        scroll.scrollsToTop = false
        view.isOpaque = false
        #else
        view.allowsMagnification = false
        #endif
        return view
    }
}

/// The web view, reporting safe-area changes so the shell can pass them on.
final class ShellWebView: WKWebView {
    var onSafeAreaChange: (() -> Void)?
    #if canImport(UIKit)
    override func safeAreaInsetsDidChange() {
        super.safeAreaInsetsDidChange()
        onSafeAreaChange?()
    }
    #endif
}

/// One hub's session web view: the handler, the bridge, the navigation
/// rule, the keyboard inset and the appearance, around one `WKWebView`.
@MainActor
public final class SessionWebController: NSObject {
    public let endpoint: HubEndpoint
    public let handler: HubSchemeHandler
    public let webView: WKWebView
    public weak var delegate: SessionWebDelegate?
    public private(set) var appearance: ShellAppearance
    public private(set) var keyboardInset: Double = 0
    /// Called whenever WebKit moved the outer scroll view and the shell put
    /// it back (the offset WebKit asked for); the proof counts these.
    public var onOuterScrollReset: ((CGPoint) -> Void)?

    private let bridge: BridgeReceiver
    #if canImport(UIKit)
    private var offsetWatch: NSKeyValueObservation?
    private var keyboardWatch: [NSObjectProtocol] = []
    private var resetting = false
    #endif

    public init(endpoint: HubEndpoint, cache: ShellCache?, appearance: ShellAppearance = .light,
                dataStore: WKWebsiteDataStore? = nil,
                credential: @escaping @MainActor () -> String?) {
        self.endpoint = endpoint
        self.appearance = appearance
        handler = HubSchemeHandler(endpoint: endpoint, cache: cache, credential: credential)
        var forward: ((Result<BridgeMessage, BridgeError>) -> Void)?
        bridge = BridgeReceiver(endpoint: endpoint) { forward?($0) }
        let config = SessionWebViewFactory.configuration(
            handler: handler, bridge: bridge,
            dataStore: dataStore ?? WKWebsiteDataStore(forIdentifier: endpoint.dataStoreID))
        webView = SessionWebViewFactory.makeWebView(configuration: config)
        super.init()
        forward = { [weak self] result in
            guard let self else { return }
            self.delegate?.sessionWeb(self, bridge: result)
        }
        webView.navigationDelegate = self
        webView.uiDelegate = self
        installStartScript()
        applyNativeAppearance()
        #if canImport(UIKit)
        pinOuterScroll()
        trackKeyboard()
        (webView as? ShellWebView)?.onSafeAreaChange = { [weak self] in self?.safeAreaChanged() }
        #endif
    }

    /// Loads `path` on the hub through the shell.
    public func load(path: String, query: String? = nil) {
        guard let url = endpoint.pageURL(path, query: query) else { return }
        webView.load(URLRequest(url: url))
    }

    public func setAppearance(_ appearance: ShellAppearance) {
        self.appearance = appearance
        installStartScript()
        applyNativeAppearance()
        webView.evaluateJavaScript(ShellScript.appearance(appearance))
    }

    /// Hands the page the height (points) the keyboard covers at the bottom
    /// of the view. iOS calls this itself from the keyboard notifications;
    /// it is public for tests and for a host with its own keyboard logic.
    public func setKeyboardInset(_ inset: Double, duration: Double = 0) {
        keyboardInset = max(0, inset)
        webView.evaluateJavaScript(ShellScript.keyboard(inset: keyboardInset, duration: duration))
    }

    private func installStartScript() {
        let content = webView.configuration.userContentController
        content.removeAllUserScripts()
        #if canImport(UIKit)
        let safe = webView.safeAreaInsets
        let (safeTop, safeBottom) = (Double(safe.top), Double(safe.bottom))
        #else
        let (safeTop, safeBottom) = (0.0, 0.0)
        #endif
        content.addUserScript(WKUserScript(
            source: ShellScript.documentStart(appearance: appearance, inset: keyboardInset,
                                              safeTop: safeTop, safeBottom: safeBottom),
            injectionTime: .atDocumentStart, forMainFrameOnly: true, in: .page))
    }

    private func applyNativeAppearance() {
        #if canImport(UIKit)
        webView.overrideUserInterfaceStyle = appearance == .dark ? .dark : .light
        #else
        webView.appearance = NSAppearance(named: appearance == .dark ? .darkAqua : .aqua)
        #endif
    }

    #if canImport(UIKit)
    /// The view's safe area moved (it joined a window, rotated): tell the
    /// page now and on every later load.
    private func safeAreaChanged() {
        let safe = webView.safeAreaInsets
        installStartScript()
        webView.evaluateJavaScript(ShellScript.safeArea(top: Double(safe.top), bottom: Double(safe.bottom)))
    }

    /// WebKit still moves the outer scroll view on its own when an input
    /// takes focus (to "reveal" it above the keyboard), even with scrolling
    /// off. The page already sits above the keyboard by `--keyboard-inset`,
    /// so that move would push it up twice: put the offset back at once.
    private func pinOuterScroll() {
        offsetWatch = webView.scrollView.observe(\.contentOffset, options: [.new]) { [weak self] scroll, _ in
            MainActor.assumeIsolated {
                guard let self, !self.resetting, scroll.contentOffset != .zero else { return }
                let asked = scroll.contentOffset
                self.resetting = true
                scroll.contentOffset = .zero
                self.resetting = false
                self.onOuterScrollReset?(asked)
            }
        }
    }

    private func trackKeyboard() {
        let center = NotificationCenter.default
        let change: (Notification) -> Void = { [weak self] note in
            MainActor.assumeIsolated { self?.keyboardChanged(note) }
        }
        keyboardWatch = [
            center.addObserver(forName: UIResponder.keyboardWillChangeFrameNotification,
                               object: nil, queue: .main, using: change),
            center.addObserver(forName: UIResponder.keyboardWillHideNotification,
                               object: nil, queue: .main, using: change),
        ]
    }

    private func keyboardChanged(_ note: Notification) {
        let info = note.userInfo ?? [:]
        let duration = (info[UIResponder.keyboardAnimationDurationUserInfoKey] as? NSNumber)?.doubleValue ?? 0
        guard note.name != UIResponder.keyboardWillHideNotification,
              let end = (info[UIResponder.keyboardFrameEndUserInfoKey] as? NSValue)?.cgRectValue,
              let window = webView.window
        else {
            setKeyboardInset(0, duration: duration)
            return
        }
        let frame = webView.convert(end, from: window.screen.coordinateSpace)
        setKeyboardInset(KeyboardInset.overlap(viewHeight: webView.bounds.height,
                                               keyboardTop: frame.minY,
                                               keyboardHeight: frame.height),
                         duration: duration)
    }
    #endif
}

/// The keyboard arithmetic, kept pure.
public enum KeyboardInset {
    /// How much of a view of `viewHeight` the keyboard covers, given the
    /// keyboard's top edge in the view's coordinates. A floating or
    /// undocked keyboard (zero height, or off the bottom) covers nothing.
    public static func overlap(viewHeight: Double, keyboardTop: Double, keyboardHeight: Double) -> Double {
        guard keyboardHeight > 0 else { return 0 }
        return min(viewHeight, max(0, viewHeight - keyboardTop))
    }
}

extension SessionWebController: WKNavigationDelegate, WKUIDelegate {
    public func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                        decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        let newWindow = action.targetFrame == nil
        let clicked = newWindow || action.navigationType == .linkActivated
        switch ShellNavigation.decide(action.request.url, endpoint: endpoint, userInitiated: clicked) {
        case .allow where newWindow:
            decisionHandler(.cancel)
            webView.load(action.request)
        case .allow:
            decisionHandler(.allow)
        case .openExternally:
            decisionHandler(.cancel)
            if let url = action.request.url { delegate?.sessionWeb(self, openExternally: url) }
        case .cancel:
            decisionHandler(.cancel)
        }
    }

    public func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    public func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                        for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        nil
    }
}
