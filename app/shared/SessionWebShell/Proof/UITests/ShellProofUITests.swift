import XCTest

/// The G2 proof: the session web view inside an iPhone app, loaded through
/// the `swe-hub://` scheme handler from Proof/stub_hub.py (which
/// run-proof.sh starts). Each test leaves its numbers as JSON and its
/// screenshots as PNG in `PROOF_SHOT_DIR` (pass it to xcodebuild as
/// `TEST_RUNNER_PROOF_SHOT_DIR`) and as XCTest attachments.
final class ShellProofUITests: XCTestCase {
    let credential = "fake-proof-credential-0000"
    let control = URL(string: "http://127.0.0.1:8766")!
    var shotDir: URL? {
        ProcessInfo.processInfo.environment["PROOF_SHOT_DIR"].map { URL(fileURLWithPath: $0) }
    }

    override func setUpWithError() throws {
        continueAfterFailure = false
        try FileManager.default.createDirectory(at: probeFile.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        let health = try XCTUnwrap(controlCall("/health", method: "GET") as? [String: Any],
                                   "the stub hub is not running: start Proof/stub_hub.py (run-proof.sh does)")
        if health["hub_running"] as? Bool != true { _ = controlCall("/hub/start") }
    }

    override func tearDown() {
        _ = controlCall("/hub/start")
    }

    // MARK: a. Streaming

    func testAStreamingArrivesEventByEvent() throws {
        _ = controlCall("/requests/clear")
        let app = launch(reset: true, query: "stream=10&interval=200&es=3")
        let mid = try wait(app, "half the events", timeout: 20) { p in
            self.arrivals(p).count >= 5
        }
        shot("streaming")
        let done = try wait(app, "the stream to end", timeout: 20) { p in
            self.page(p)["stream"].flatMap { ($0 as? [String: Any])?["state"] as? String } == "done"
        }
        let arrivals = self.arrivals(done)
        XCTAssertEqual(arrivals.count, 10)
        let arrived = arrivals.compactMap { ($0["arrived"] as? NSNumber)?.doubleValue }
        let sent = arrivals.compactMap { ($0["sent"] as? NSNumber)?.doubleValue }
        let gaps = zip(arrived, arrived.dropFirst()).map { $1 - $0 }
        let latency = zip(arrived, sent).map { $0 - $1 }
        let reads = arrivals.compactMap { ($0["read"] as? NSNumber)?.intValue }
        record("streaming", [
            "arrivedMs": arrived, "sentMs": sent, "gapsMs": gaps, "latencyMs": latency,
            "streamReadIndex": reads, "midProbeArrivals": self.arrivals(mid).count,
            "eventSource": page(done)["eventSource"] ?? NSNull(),
            "resourceTimingEntries": (page(done)["resources"] as? [String])?.count ?? -1,
        ])
        // One by one: every event its own read, about 200 ms apart, each
        // soon after the hub wrote it. A buffered body would land all ten
        // in one read at the end (gaps near 0, latency up to 1.8 s). A cold
        // first launch shows some jitter (one event late by ~140 ms while
        // the page lays out), so the bounds are on the median and the worst.
        XCTAssertEqual(Set(reads).count, 10, "events shared a read: \(reads)")
        for gap in gaps { XCTAssertGreaterThan(gap, 20, "gaps \(gaps)") }
        XCTAssertEqual(median(gaps), 205, accuracy: 40, "gaps \(gaps)")
        XCTAssertGreaterThan((arrived.last ?? 0) - (arrived.first ?? 0), 1500)
        XCTAssertLessThan(median(latency), 30, "latency \(latency)")
        XCTAssertLessThan(latency.max() ?? 0, 400, "latency \(latency)")

        // New rows kept the timeline pinned to its end.
        let timeline = try XCTUnwrap(page(done)["timeline"] as? [String: Any])
        XCTAssertLessThan(num(timeline["distanceFromEnd"]), 4)

        // The POST body went through and the hub saw the shell's headers.
        let post = try XCTUnwrap(page(done)["post"] as? [String: Any])
        XCTAssertEqual(post["state"] as? String, "ok")
        XCTAssertEqual(post["echoed"] as? String, #"{"text":"hello from the page","n":42}"#)
        XCTAssertEqual(post["authOK"] as? Bool, true)
        XCTAssertEqual(post["pairOK"] as? Bool, true)
        XCTAssertEqual(post["originOK"] as? Bool, true)
        let seen = try XCTUnwrap(controlCall("/requests", method: "GET") as? [[String: Any]])
        XCTAssertFalse(seen.isEmpty)
        for request in seen {
            let headers = request["headers"] as? [String: String] ?? [:]
            XCTAssertEqual(headers["Authorization"], "Bearer \(credential)", "\(request["path"] ?? "")")
            XCTAssertEqual(headers["X-Swe-Pair"], "1")
            XCTAssertEqual(headers["Origin"], "http://127.0.0.1:8765")
        }
        // Nothing the page can read carries the credential.
        let pageText = String(decoding: try JSONSerialization.data(withJSONObject: page(done)), as: UTF8.self)
        XCTAssertFalse(pageText.contains(credential))
        let resources = page(done)["resources"] as? [String] ?? []
        // WebKit lists no Resource Timing entries for custom-scheme loads,
        // so this list is empty; any entry there must still be on the scheme.
        for url in resources { XCTAssertTrue(url.hasPrefix("swe-hub://proof/"), url) }
        // The bridge reached native code, decoded.
        XCTAssertEqual(native(done)["bridge"] as? [String], ["wake"])
        XCTAssertEqual(native(done)["refusals"] as? Int, 0)
        shot("streaming-done")
    }

    // MARK: b. Storage

    func testBIndexedDBSurvivesARelaunch() throws {
        let token = UUID().uuidString
        var app = launch(reset: true, query: "token=\(token)")
        let first = try wait(app, "the first IndexedDB write", timeout: 20) { p in
            self.idb(p)["state"] as? String == "ok"
        }
        let written = try XCTUnwrap(idb(first)["current"] as? [String: Any])
        XCTAssertEqual(written["token"] as? String, token)
        XCTAssertEqual(written["launches"] as? Int, 1)
        XCTAssertTrue(idb(first)["previous"] is NSNull)
        app.terminate()

        app = launch(reset: false, query: nil)
        let second = try wait(app, "the IndexedDB read after relaunch", timeout: 20) { p in
            self.idb(p)["state"] as? String == "ok"
        }
        let previous = try XCTUnwrap(idb(second)["previous"] as? [String: Any], "IndexedDB came back empty")
        XCTAssertEqual(previous["token"] as? String, token)
        XCTAssertEqual((idb(second)["current"] as? [String: Any])?["launches"] as? Int, 2)
        record("storage", ["origin": page(second)["origin"] ?? NSNull(), "first": idb(first), "second": idb(second)])
    }

    // MARK: c. Scrolling

    func testCOnlyTheTimelineScrolls() throws {
        let app = launch(reset: true, query: nil)
        let start = try wait(app, "the page", timeout: 20) { p in
            (self.page(p)["snapshot"] as? [String: Any])?["state"] as? String == "ok"
        }
        let t0 = try XCTUnwrap(page(start)["timeline"] as? [String: Any])
        XCTAssertLessThan(num(t0["distanceFromEnd"]), 4, "the timeline did not open at its end")
        let web = app.webViews.firstMatch
        // Back through the history, then at the end push past it, then drag
        // on the parts of the page that don't scroll (composer and banner).
        web.swipeDown(velocity: .fast)
        web.swipeDown(velocity: .fast)
        let up = try wait(app, "the timeline to scroll", timeout: 5) { p in
            self.num((self.page(p)["timeline"] as? [String: Any])?["scrollTop"]) < self.num(t0["scrollTop"]) - 200
        }
        let scrolledBy = num(t0["scrollTop"]) - num((page(up)["timeline"] as? [String: Any])?["scrollTop"])
        drag(web, from: 0.97, to: 0.6)
        drag(web, from: 0.97, to: 1.0)
        drag(web, from: 0.01, to: 0.4)
        web.swipeUp(velocity: .fast)
        web.swipeUp(velocity: .fast)
        web.swipeUp(velocity: .fast)
        web.swipeUp(velocity: .fast)
        let end = try wait(app, "the timeline back at its end", timeout: 10) { p in
            self.num((self.page(p)["timeline"] as? [String: Any])?["distanceFromEnd"]) < 4
        }
        web.swipeUp(velocity: .fast)
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        let after = try XCTUnwrap(probe(app))
        record("scrolling", ["start": page(start)["timeline"] ?? NSNull(), "scrolledBy": scrolledBy,
                             "end": page(end)["timeline"] ?? NSNull(), "native": native(after),
                             "maxWindowScroll": page(after)["maxWindowScroll"] ?? NSNull()])
        XCTAssertGreaterThan(scrolledBy, 200)
        XCTAssertEqual(num(native(after)["outerOffsetY"]), 0)
        XCTAssertEqual(num(native(after)["outerResets"]), 0, "WebKit moved the outer scroll view")
        XCTAssertEqual(num(page(after)["maxWindowScroll"]), 0, "the document scrolled")
        XCTAssertEqual(num(native(after)["zoom"]), 1)
        shot("scrolling")
    }

    // MARK: d. Keyboard

    func testDComposerSitsOnTheKeyboard() throws {
        let app = launch(reset: true, query: nil)
        let before = try wait(app, "the page", timeout: 20) { p in
            (self.page(p)["snapshot"] as? [String: Any])?["state"] as? String == "ok"
        }
        let closedBottom = num((page(before)["composer"] as? [String: Any])?["bottom"])
        let webHeight = num(native(before)["webHeight"])
        let safeBottom = num(native(before)["safeBottom"])
        XCTAssertEqual(closedBottom, webHeight - safeBottom, accuracy: 1, "composer not on the home indicator inset")

        let field = app.webViews.textViews.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5), "no keyboard")
        let open = try wait(app, "the composer to sit on the keyboard", timeout: 5) { p in
            let top = self.num(self.native(p)["keyboardTop"])
            let bottom = self.num((self.page(p)["composer"] as? [String: Any])?["bottom"])
            return top > 0 && abs(bottom - top) < 1.5
        }
        // Let the animation finish, then measure again.
        RunLoop.current.run(until: Date().addingTimeInterval(0.8))
        let settled = try XCTUnwrap(probe(app))
        let keyboardTop = num(native(settled)["keyboardTop"])
        let composer = try XCTUnwrap(page(settled)["composer"] as? [String: Any])
        let keyboardFrame = app.keyboards.firstMatch.frame
        shot("keyboard-settled")
        record("keyboard-raw", ["probe": settled, "xcuiFieldFrame": "\(field.frame)",
                                "xcuiKeyboardFrame": "\(keyboardFrame)"])
        XCTAssertEqual(num(composer["bottom"]), keyboardTop, accuracy: 1.5)
        XCTAssertEqual(num(native(settled)["outerOffsetY"]), 0)
        XCTAssertEqual(num(page(settled)["windowScrollY"]), 0)
        XCTAssertEqual(page(settled)["activeElement"] as? String, "input")
        // XCUITest's own view of the screen agrees. Its keyboard element is
        // the keys only: WebKit's form bar (up, down, done) and the
        // suggestion row sit above it, about 112 pt on iOS 27, and the
        // keyboard notification's frame (what the shell uses) includes
        // both. So the field ends above the keys, within that band.
        XCTAssertLessThanOrEqual(field.frame.maxY, keyboardFrame.minY + 1)
        XCTAssertGreaterThan(field.frame.maxY, keyboardFrame.minY - 140)
        let webTop = num(native(settled)["webTopInWindow"])
        XCTAssertEqual(Double(field.frame.maxY) - webTop, num(composer["bottom"]) - 8, accuracy: 2,
                       "the field XCUITest sees is not where the page put it")

        field.typeText("Typed above the keyboard")
        // The timeline still scrolls with the keyboard up.
        let t0 = num((page(settled)["timeline"] as? [String: Any])?["scrollTop"])
        drag(app.webViews.firstMatch, from: 0.15, to: 0.45)
        let scrolled = try wait(app, "the timeline to scroll with the keyboard up", timeout: 5) { p in
            self.num((self.page(p)["timeline"] as? [String: Any])?["scrollTop"]) < t0 - 50
        }
        XCTAssertTrue(app.keyboards.firstMatch.exists, "scrolling dismissed the keyboard")
        shot("keyboard-open")

        let done = app.toolbars.buttons["Done"]
        if done.exists { done.tap() } else { app.navigationBars.buttons["Hide keyboard"].tap() }
        let closed = try wait(app, "the layout to come back", timeout: 5) { p in
            Double((self.page(p)["keyboardInset"] as? String ?? "").replacingOccurrences(of: "px", with: "")) == 0
                && abs(self.num((self.page(p)["composer"] as? [String: Any])?["bottom"]) - closedBottom) < 1
        }
        RunLoop.current.run(until: Date().addingTimeInterval(0.6))
        let after = try XCTUnwrap(probe(app))
        XCTAssertFalse(app.keyboards.firstMatch.exists)
        XCTAssertEqual(num((page(after)["composer"] as? [String: Any])?["bottom"]), closedBottom, accuracy: 1)
        XCTAssertEqual(num(native(after)["outerOffsetY"]), 0)
        record("keyboard", [
            "closedComposerBottom": closedBottom, "webHeight": webHeight, "safeBottom": safeBottom,
            "open": ["keyboardTop": keyboardTop, "keyboardHeight": native(settled)["keyboardHeight"] ?? NSNull(),
                     "insetSent": native(settled)["keyboardInsetSent"] ?? NSNull(),
                     "pageInset": page(settled)["keyboardInset"] ?? NSNull(), "composer": composer,
                     "visual": page(settled)["visual"] ?? NSNull(),
                     "xcuiFieldMaxY": field.frame.maxY, "xcuiKeyboardMinY": keyboardFrame.minY,
                     "webTopInWindow": native(settled)["webTopInWindow"] ?? NSNull()],
            "firstMatchProbe": native(open),
            "scrolledWithKeyboard": t0 - num((page(scrolled)["timeline"] as? [String: Any])?["scrollTop"]),
            "outerResets": native(after)["outerResets"] ?? NSNull(),
            "outerMaxAsked": native(after)["outerMaxAsked"] ?? NSNull(),
            "maxVisualOffset": page(after)["maxVisualOffset"] ?? NSNull(),
            "closed": page(closed)["composer"] ?? NSNull(),
        ])
        shot("keyboard-closed")
    }

    // MARK: e. Offline launch

    func testEOfflineLaunchShowsTheSavedCopy() throws {
        var app = launch(reset: true, query: nil)
        let online = try wait(app, "the live snapshot", timeout: 20) { p in
            (self.page(p)["snapshot"] as? [String: Any])?["state"] as? String == "ok"
        }
        let live = try XCTUnwrap(page(online)["snapshot"] as? [String: Any])
        XCTAssertEqual(live["source"] as? String, "network")
        let token = try XCTUnwrap(live["token"] as? String)
        // Give the cache's background write a moment, then stop the hub.
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        app.terminate()
        _ = controlCall("/hub/stop")
        XCTAssertEqual((controlCall("/health", method: "GET") as? [String: Any])?["hub_running"] as? Bool, false)
        XCTAssertNil(hubReachable(), "the hub still answers")

        let launched = Date()
        app = launch(reset: false, query: nil)
        let offline = try wait(app, "the saved copy", timeout: 20) { p in
            (self.page(p)["snapshot"] as? [String: Any])?["state"] as? String == "ok"
        }
        let shownAfter = Date().timeIntervalSince(launched)
        let saved = try XCTUnwrap(page(offline)["snapshot"] as? [String: Any])
        XCTAssertEqual(saved["source"] as? String, "cache")
        XCTAssertEqual(saved["token"] as? String, token, "not the last saved snapshot")
        XCTAssertGreaterThanOrEqual(num(page(offline)["rows"]), 2000)
        let served = native(offline)["served"] as? [String: String] ?? [:]
        XCTAssertEqual(served["/index.html"], "cache")
        XCTAssertEqual(served["/proof.js"], "cache")
        XCTAssertTrue((page(offline)["banner"] as? String ?? "").hasPrefix("Offline: showing the copy saved at"))
        XCTAssertTrue(app.webViews.staticTexts.containing(NSPredicate(format: "label BEGINSWITH 'Offline'"))
            .firstMatch.waitForExistence(timeout: 5))
        record("offline", ["liveToken": token, "saved": saved, "served": served,
                           "secondsToSavedCopy": shownAfter, "rows": page(offline)["rows"] ?? NSNull(),
                           "post": page(offline)["post"] ?? NSNull()])
        shot("offline")
    }

    // MARK: Appearance

    func testFAppearanceReachesThePage() throws {
        let app = launch(reset: true, query: nil, appearance: "dark")
        let dark = try wait(app, "the dark theme", timeout: 20) { p in
            self.page(p)["theme"] as? String == "dark"
                && (self.page(p)["snapshot"] as? [String: Any])?["state"] as? String == "ok"
        }
        XCTAssertEqual(page(dark)["prefersDark"] as? Bool, true)
        record("appearance", ["theme": page(dark)["theme"] ?? NSNull(), "prefersDark": page(dark)["prefersDark"] ?? NSNull(),
                              "background": page(dark)["background"] ?? NSNull()])
        shot("dark")
    }

    // MARK: Helpers

    func launch(reset: Bool, query: String?, appearance: String = "light") -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["PROOF_HUB"] = "http://127.0.0.1:8765"
        app.launchEnvironment["PROOF_RESET"] = reset ? "1" : "0"
        app.launchEnvironment["PROOF_QUERY"] = query ?? ""
        app.launchEnvironment["PROOF_APPEARANCE"] = appearance
        // A fresh id per launch: a probe the previous instance wrote just
        // before it was terminated is never read as this one's.
        launchID = UUID().uuidString
        app.launchEnvironment["PROOF_LAUNCH_ID"] = launchID
        try? FileManager.default.removeItem(at: probeFile)
        app.launchEnvironment["PROOF_PROBE_FILE"] = probeFile.path
        app.launch()
        return app
    }

    /// The harness writes its state here (the simulator shares the Mac's
    /// file system); reading it never touches the accessibility tree.
    var probeFile: URL {
        (shotDir ?? FileManager.default.temporaryDirectory).appendingPathComponent("probe.json")
    }

    var launchID = ""

    func probe(_ app: XCUIApplication) -> [String: Any]? {
        guard let data = try? Data(contentsOf: probeFile),
              let p = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              native(p)["launchID"] as? String == launchID
        else { return nil }
        return p
    }

    func wait(_ app: XCUIApplication, _ what: String, timeout: TimeInterval,
              until condition: ([String: Any]) -> Bool) throws -> [String: Any] {
        let deadline = Date().addingTimeInterval(timeout)
        var last: [String: Any]?
        repeat {
            if let p = probe(app) {
                last = p
                if condition(p) { return p }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        } while Date() < deadline
        let dump = last.flatMap { try? JSONSerialization.data(withJSONObject: $0) }
            .map { String(decoding: $0, as: UTF8.self) } ?? "no probe"
        shot("timeout-\(name.filter { $0.isLetter })")
        throw ProofTimeout(errorDescription: "never saw \(what) in \(timeout)s; last probe: \(dump.prefix(4000))")
    }

    func page(_ p: [String: Any]) -> [String: Any] { p["page"] as? [String: Any] ?? [:] }
    func native(_ p: [String: Any]) -> [String: Any] { p["native"] as? [String: Any] ?? [:] }
    func idb(_ p: [String: Any]) -> [String: Any] { page(p)["idb"] as? [String: Any] ?? [:] }
    func arrivals(_ p: [String: Any]) -> [[String: Any]] {
        (page(p)["stream"] as? [String: Any])?["arrivals"] as? [[String: Any]] ?? []
    }
    func num(_ value: Any?) -> Double { (value as? NSNumber)?.doubleValue ?? .nan }
    func median(_ values: [Double]) -> Double {
        let sorted = values.sorted()
        return sorted.isEmpty ? .nan : sorted[sorted.count / 2]
    }

    /// A slow drag inside the web view between two heights (0 top, 1 bottom).
    func drag(_ element: XCUIElement, from: CGFloat, to: CGFloat) {
        let start = element.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: from))
        let end = element.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: to))
        start.press(forDuration: 0.05, thenDragTo: end, withVelocity: .default, thenHoldForDuration: 0.1)
    }

    func shot(_ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let shotDir {
            try? FileManager.default.createDirectory(at: shotDir, withIntermediateDirectories: true)
            try? screenshot.pngRepresentation.write(to: shotDir.appendingPathComponent("\(name).png"))
        }
    }

    func record(_ name: String, _ value: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
        else { return }
        add(XCTAttachment(data: data, uniformTypeIdentifier: "public.json"))
        if let shotDir {
            try? FileManager.default.createDirectory(at: shotDir, withIntermediateDirectories: true)
            try? data.write(to: shotDir.appendingPathComponent("\(name).json"))
        }
    }

    func controlCall(_ path: String, method: String = "POST") -> Any? {
        var request = URLRequest(url: control.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 5
        return fetch(request).flatMap { try? JSONSerialization.jsonObject(with: $0) }
    }

    /// The hub's status code, or nil when nothing answers on its port.
    func hubReachable() -> Int? {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:8765/index.html")!)
        request.timeoutInterval = 3
        var status: Int?
        let done = expectation(description: "hub probe")
        URLSession.shared.dataTask(with: request) { _, response, _ in
            status = (response as? HTTPURLResponse)?.statusCode
            done.fulfill()
        }.resume()
        wait(for: [done], timeout: 5)
        return status
    }

    func fetch(_ request: URLRequest) -> Data? {
        var result: Data?
        let done = expectation(description: "control \(request.url?.path ?? "")")
        URLSession.shared.dataTask(with: request) { data, _, _ in
            result = data
            done.fulfill()
        }.resume()
        wait(for: [done], timeout: 8)
        return result
    }
}

/// A wait that ran out: a failure, with the last probe in the message.
struct ProofTimeout: LocalizedError {
    var errorDescription: String?
}
