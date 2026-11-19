import SessionWebShell
import UIKit
import WebKit

/// The proof harness: a native navigation bar over the session web view,
/// as the iPhone embed (G18) would show it. Launch environment:
/// - `PROOF_HUB`: the stub hub's origin (default http://127.0.0.1:8765)
/// - `PROOF_QUERY`: query for the page (`stream=10&interval=200`, `token=…`)
/// - `PROOF_RESET=1`: wipe the shell cache and the web data store first
/// - `PROOF_APPEARANCE`: `light` or `dark`
///
/// - `PROOF_PROBE_FILE`: where to write the page's and the shell's state
///   as JSON every 250 ms, for the UI tests to read. A file, not an
///   accessibility label: querying the app's accessibility tree from the
///   test while the page loads stalled the page (see the G2 spec).
@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     configurationForConnecting session: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default", sessionRole: session.role)
        config.delegateClass = SceneDelegate.self
        return config
    }
}

/// iOS 27 stops an app that has no scene lifecycle at launch.
final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        guard let scene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: scene)
        window.rootViewController = UINavigationController(rootViewController: ProofViewController())
        window.makeKeyAndVisible()
        self.window = window
    }
}

/// The harness's fake credential. Not a secret: the stub hub accepts only
/// this string, and the tests check it never reaches the page.
let proofCredential = "fake-proof-credential-0000"

final class ProofViewController: UIViewController, SessionWebDelegate {
    private let env = ProcessInfo.processInfo.environment
    private var shell: SessionWebController?
    private var timer: Timer?

    private var served: [String: String] = [:]
    private var refusals = 0
    private var outerResets = 0
    private var outerMaxAsked: CGFloat = 0
    private var bridge: [String] = []
    private var keyboardTop: CGFloat?
    private var keyboardHeight: CGFloat = 0
    private var started = Date()

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "Session"
        view.backgroundColor = .systemBackground
        navigationItem.rightBarButtonItem = UIBarButtonItem(
            title: "Hide keyboard", primaryAction: UIAction { [weak self] _ in self?.view.endEditing(true) })
        NotificationCenter.default.addObserver(
            forName: UIResponder.keyboardWillChangeFrameNotification, object: nil, queue: .main
        ) { [weak self] note in
            let end = (note.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? NSValue)?.cgRectValue
            MainActor.assumeIsolated { self?.keyboardMoved(end) }
        }
        NotificationCenter.default.addObserver(
            forName: UIResponder.keyboardWillHideNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.keyboardTop = nil
                self?.keyboardHeight = 0
            }
        }
        Task { await start() }
    }

    private func start() async {
        let origin = URL(string: env["PROOF_HUB"] ?? "http://127.0.0.1:8765")!
        let endpoint = try! HubEndpoint(id: "proof", origin: origin)
        let cacheDir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SessionWebShell/proof")
        let cache = DiskShellCache(directory: cacheDir)
        if env["PROOF_RESET"] == "1" {
            cache.removeAll()
            // Clear the store's data rather than `WKWebsiteDataStore.remove(forIdentifier:)`:
            // as the first WebKit call in the process, remove crashed
            // inside WebKit on the iOS 27.1 simulator (WTF::RunLoop::dispatch).
            let store = WKWebsiteDataStore(forIdentifier: endpoint.dataStoreID)
            await store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast)
        }
        let appearance = ShellAppearance(rawValue: env["PROOF_APPEARANCE"] ?? "light") ?? .light
        overrideUserInterfaceStyle = appearance == .dark ? .dark : .light
        navigationController?.overrideUserInterfaceStyle = overrideUserInterfaceStyle
        let shell = SessionWebController(endpoint: endpoint, cache: cache, appearance: appearance,
                                         credential: { proofCredential })
        shell.delegate = self
        shell.handler.onServe = { [weak self] key, source in self?.served[key] = source.rawValue }
        shell.handler.onRefuse = { [weak self] _ in self?.refusals += 1 }
        shell.onOuterScrollReset = { [weak self] asked in
            guard let self else { return }
            outerResets += 1
            outerMaxAsked = max(outerMaxAsked, abs(asked.y), abs(asked.x))
        }
        shell.webView.isInspectable = true
        shell.webView.accessibilityIdentifier = "session-web"
        let web = shell.webView
        web.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(web)
        NSLayoutConstraint.activate([
            web.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            web.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            web.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        self.shell = shell
        started = Date()
        shell.load(path: "/index.html", query: env["PROOF_QUERY"].flatMap { $0.isEmpty ? nil : $0 })
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refreshProbe() }
        }
    }

    private func keyboardMoved(_ end: CGRect?) {
        guard let end, let web = shell?.webView, let window = web.window else { return }
        let frame = web.convert(end, from: window.screen.coordinateSpace)
        keyboardTop = frame.minY
        keyboardHeight = frame.height
    }

    private func refreshProbe() {
        guard let shell else { return }
        let web = shell.webView
        let native: [String: Any] = [
            "served": served,
            "refusals": refusals,
            "outerResets": outerResets,
            "outerMaxAsked": outerMaxAsked,
            "outerOffsetY": web.scrollView.contentOffset.y,
            "outerOffsetX": web.scrollView.contentOffset.x,
            "zoom": web.scrollView.zoomScale,
            "webHeight": web.bounds.height,
            "webWidth": web.bounds.width,
            "webTopInWindow": web.convert(CGPoint.zero, to: nil).y,
            "safeBottom": view.safeAreaInsets.bottom,
            "keyboardTop": keyboardTop.map { Double($0) } ?? -1,
            "keyboardHeight": keyboardHeight,
            "keyboardInsetSent": shell.keyboardInset,
            "bridge": bridge,
            "uptime": Date().timeIntervalSince(started),
            "url": web.url?.absoluteString ?? "",
            "launchID": env["PROOF_LAUNCH_ID"] ?? "",
        ]
        web.evaluateJavaScript("window.proofState ? JSON.stringify(window.proofState()) : 'null'") { [weak self] value, _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                var page: Any = NSNull()
                if let text = value as? String, let data = text.data(using: .utf8),
                   let parsed = try? JSONSerialization.jsonObject(with: data) {
                    page = parsed
                }
                let all: [String: Any] = ["native": native, "page": page]
                if let data = try? JSONSerialization.data(withJSONObject: all, options: [.sortedKeys]) {
                    if let path = self.env["PROOF_PROBE_FILE"], !path.isEmpty {
                        try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
                    }
                }
            }
        }
    }

    // MARK: SessionWebDelegate

    func sessionWeb(_ controller: SessionWebController, openExternally url: URL) {
        bridge.append("external:\(url.absoluteString)")
    }

    func sessionWeb(_ controller: SessionWebController, bridge message: Result<BridgeMessage, BridgeError>) {
        switch message {
        case .success(.wake): bridge.append("wake")
        case .success(.back): bridge.append("back")
        case .success(.open(let key)): bridge.append("open:\(key)")
        case .failure(let error): bridge.append("error:\(error)")
        }
    }
}
