import Foundation
import Testing
@testable import SessionWebShell

@Suite struct BridgeDecodingTests {
    @Test func decodesTheThreeCalls() throws {
        #expect(try NativeBridge.decode(["v": 1, "type": "wake"]) == .wake)
        #expect(try NativeBridge.decode(["v": 1, "type": "back"]) == .back)
        #expect(try NativeBridge.decode(["v": 1, "type": "open", "key": "mac:claude:abc-123"])
            == .open(key: "mac:claude:abc-123"))
    }

    @Test func refusesMalformedBodies() {
        #expect(throws: BridgeError.notAnObject) { try NativeBridge.decode("wake") }
        #expect(throws: BridgeError.notAnObject) { try NativeBridge.decode([1, 2]) }
        #expect(throws: BridgeError.badVersion) { try NativeBridge.decode(["type": "wake"]) }
        #expect(throws: BridgeError.badVersion) { try NativeBridge.decode(["v": 2, "type": "wake"]) }
        #expect(throws: BridgeError.badVersion) { try NativeBridge.decode(["v": "1", "type": "wake"]) }
        #expect(throws: BridgeError.unknownType("eval")) { try NativeBridge.decode(["v": 1, "type": "eval"]) }
        #expect(throws: BridgeError.unknownType("")) { try NativeBridge.decode(["v": 1]) }
    }

    @Test(arguments: [nil, "", " key", "key ", "a\nb", "a\u{0}b", String(repeating: "k", count: 257)] as [String?])
    func refusesBadKeys(_ key: String?) {
        var body: [String: Any] = ["v": 1, "type": "open"]
        if let key { body["key"] = key }
        #expect(throws: BridgeError.badKey) { try NativeBridge.decode(body) }
    }

    @Test func numericKeyIsNotAString() {
        #expect(throws: BridgeError.badKey) { try NativeBridge.decode(["v": 1, "type": "open", "key": 42]) }
    }

    @Test func onlyThePinnedHubsMainFrameIsTrusted() {
        let hub = endpoint()
        #expect(NativeBridge.isTrusted(scheme: "swe-hub", host: "box", port: 0, isMainFrame: true, endpoint: hub))
        #expect(!NativeBridge.isTrusted(scheme: "swe-hub", host: "box", port: 0, isMainFrame: false, endpoint: hub))
        #expect(!NativeBridge.isTrusted(scheme: "swe-hub", host: "other", port: 0, isMainFrame: true, endpoint: hub))
        #expect(!NativeBridge.isTrusted(scheme: "http", host: "127.0.0.1", port: 8765, isMainFrame: true, endpoint: hub))
        #expect(!NativeBridge.isTrusted(scheme: "https", host: "box", port: 0, isMainFrame: true, endpoint: hub))
    }

    @Test func theUserScriptDefinesTheBridgeAndCarriesNoSecret() {
        let script = NativeBridge.userScript
        #expect(script.contains("sweNative"))
        #expect(script.contains("messageHandlers.sweNative"))
        for call in ["wake()", "open(key)", "back()"] { #expect(script.contains(call)) }
        #expect(!script.contains("Bearer"))
        let start = ShellScript.documentStart(appearance: .dark, inset: 0)
        #expect(!start.contains(fakeCredential))
        #expect(!start.contains("127.0.0.1"))
    }
}

@Suite struct NavigationTests {
    let hub = endpoint()

    @Test func onlyThePageOriginLoads() {
        func decide(_ s: String, clicked: Bool = false) -> ShellNavigation {
            ShellNavigation.decide(URL(string: s), endpoint: hub, userInitiated: clicked)
        }
        #expect(decide("swe-hub://box/session/") == .allow)
        #expect(decide("swe-hub://other/") == .cancel)
        #expect(decide("swe-hub://box:1/") == .cancel)
        #expect(decide("about:blank") == .allow)
        #expect(decide("about:srcdoc") == .allow)
        #expect(decide("about:config") == .cancel)
        #expect(decide("blob:swe-hub://box/6f1c") == .allow)
        #expect(decide("blob:https://evil.example/6f1c") == .cancel)
        // The hub's real origin is not the page's: loading it would leave
        // the shell (and its credential handling) behind.
        #expect(decide("http://127.0.0.1:8765/") == .cancel)
        #expect(decide("http://127.0.0.1:8765/", clicked: true) == .openExternally)
        #expect(decide("https://github.com/x", clicked: true) == .openExternally)
        #expect(decide("https://github.com/x") == .cancel)
        #expect(decide("mailto:a@b.c", clicked: true) == .openExternally)
        #expect(decide("file:///etc/passwd", clicked: true) == .cancel)
        #expect(decide("javascript:alert(1)", clicked: true) == .cancel)
        #expect(ShellNavigation.decide(nil, endpoint: hub, userInitiated: true) == .cancel)
    }
}

@Suite struct ShellScriptTests {
    @Test func keyboardSetsTheCSSVariables() {
        #expect(ShellScript.keyboard(inset: 336.333, duration: 0.25)
            == "document.documentElement.style.setProperty('--keyboard-inset','336.33px');"
            + "document.documentElement.style.setProperty('--keyboard-duration','250ms');")
        #expect(ShellScript.keyboard(inset: -4, duration: -1).contains("'0.0px'"))
    }

    @Test func safeAreaSetsTheCSSVariables() {
        #expect(ShellScript.safeArea(top: 0, bottom: 34)
            == "document.documentElement.style.setProperty('--safe-area-top','0.0px');"
            + "document.documentElement.style.setProperty('--safe-area-bottom','34.0px');")
        #expect(ShellScript.documentStart(appearance: .light, inset: 0, safeTop: 0, safeBottom: 34)
            .contains("'--safe-area-bottom','34.0px'"))
    }

    @Test func appearanceSetsThemeAndColorScheme() {
        #expect(ShellScript.appearance(.dark)
            == "document.documentElement.dataset.theme='dark';document.documentElement.style.colorScheme='dark';")
    }

    @Test func keyboardOverlap() {
        #expect(KeyboardInset.overlap(viewHeight: 800, keyboardTop: 464, keyboardHeight: 336) == 336)
        #expect(KeyboardInset.overlap(viewHeight: 800, keyboardTop: 900, keyboardHeight: 336) == 0)
        #expect(KeyboardInset.overlap(viewHeight: 800, keyboardTop: 464, keyboardHeight: 0) == 0)
        #expect(KeyboardInset.overlap(viewHeight: 800, keyboardTop: -10, keyboardHeight: 900) == 800)
    }
}
