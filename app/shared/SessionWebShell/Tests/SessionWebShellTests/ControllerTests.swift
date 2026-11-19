import Foundation
import Testing
import WebKit
@testable import SessionWebShell

extension MainQueueSuites {
@MainActor
@Suite(.timeLimit(.minutes(1)))
struct ControllerTests {
    func controller() -> SessionWebController {
        SessionWebController(endpoint: endpoint(), cache: nil,
                             dataStore: .nonPersistent(), credential: { nil })
    }

    /// Runs `script` in the page and returns its string result.
    func evaluate(_ web: WKWebView, _ script: String) async -> String? {
        await withCheckedContinuation { done in
            web.evaluateJavaScript(script) { value, _ in done.resume(returning: value as? String) }
        }
    }

    /// Loads a blank page and waits until its document has finished.
    func loadAndWait(_ web: WKWebView) async {
        web.loadHTMLString("<!doctype html><title>t</title><p>page</p>", baseURL: nil)
        for _ in 0..<100 {
            try? await Task.sleep(for: .milliseconds(50))
            if await evaluate(web, "document.title + ':' + document.readyState") == "t:complete" { return }
        }
    }

    @Test func aReloadAfterTheKeyboardWentAwayHasNoGap() async {
        let shell = controller()
        shell.setKeyboardInset(336)
        shell.setAppearance(.dark)   // used to bake 336 into the next load
        shell.setKeyboardInset(0)
        #expect(shell.startScriptSource?.contains("'--keyboard-inset','0.0px'") == true)
        #expect(shell.startScriptSource?.contains("'--keyboard-inset','336.0px'") == false)
        #expect(shell.startScriptSource?.contains("dataset.theme='dark'") == true)

        // And the reloaded page itself starts with no inset, in dark.
        await loadAndWait(shell.webView)
        let inset = await evaluate(shell.webView,
                                   "document.documentElement.style.getPropertyValue('--keyboard-inset')")
        #expect(inset == "0.0px")
        #expect(await evaluate(shell.webView, "document.documentElement.dataset.theme") == "dark")
    }

    @Test func theStartScriptFollowsEveryInsetChange() {
        let shell = controller()
        shell.setKeyboardInset(120)
        #expect(shell.startScriptSource?.contains("'--keyboard-inset','120.0px'") == true)
        shell.setKeyboardInset(0)
        #expect(shell.startScriptSource?.contains("'--keyboard-inset','0.0px'") == true)
        #expect(shell.webView.configuration.userContentController.userScripts.count == 1)
    }

    @Test func observersAreRemovedWhenTheBagGoes() async {
        let center = NotificationCenter()
        let name = Notification.Name("SessionWebShellTests.ping")
        let hits = Counter()
        var bag: ObserverTokens? = ObserverTokens()
        bag?.observe(name, in: center, queue: nil) { _ in hits.bump() }
        bag?.observe(name, in: center, queue: nil) { _ in hits.bump() }
        #expect(bag?.count == 2)
        center.post(name: name, object: nil)
        #expect(hits.value == 2)
        bag = nil
        center.post(name: name, object: nil)
        #expect(hits.value == 2, "an observer outlived its bag")
    }

    @Test func aReleasedControllerLeavesNoObserverBehind() {
        weak var released: SessionWebController?
        weak var bag: ObserverTokens?
        // WebKit autoreleases objects it hands back; drain them so the
        // controller really goes.
        autoreleasepool {
            let shell = controller()
            released = shell
            bag = shell.observers
            #if canImport(UIKit)
            #expect(shell.observers.count == 2)  // the keyboard frame and hide observers
            #endif
        }
        #expect(released == nil, "the controller was kept alive")
        #expect(bag == nil, "the observer bag outlived its controller")
    }
}
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    var value: Int { lock.withLock { count } }
    func bump() { lock.withLock { count += 1 } }
}
