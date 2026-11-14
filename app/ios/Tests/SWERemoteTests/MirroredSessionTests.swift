import XCTest
@testable import SWERemote

/// Mirrored sessions (#1918): the harness deleted the transcript and swe
/// lists a read-only copy. Nothing that attaches, resumes, continues,
/// forks or sends is offered for one, whatever its ownership says.
@MainActor
final class MirroredSessionTests: XCTestCase {
    private func row(_ key: String = "claude:1", mirrored: Bool? = true, ownership: String? = "observed",
                     attachable: Bool? = true, canFork: Bool? = true, status: String? = nil,
                     pinned: Bool = false) -> SessionRow {
        var r = SessionRow(key: key, harness: "claude", agent: "Claude Code", cwd: "/Users/demo/api", title: "Old work",
                           lastActiveAt: 1_000, status: status, machineId: "m1", ownership: ownership, attachable: attachable)
        r.canFork = canFork
        r.mirrored = mirrored
        r.pinned = pinned
        return r
    }

    // MARK: Decoding

    func testDecodesMirroredWhenPresent() throws {
        let json = #"""
        {"sessions":[{"key":"claude:1","harness":"claude","status":null,"resume":null,"mirrored":true,
          "ownership":"observed","attachable":false,"can_fork":false,"last_active_at":5}]}
        """#
        let r = try Hub.decoder.decode(SessionsResponse.self, from: Data(json.utf8)).sessions[0]
        XCTAssertEqual(r.mirrored, true)
        XCTAssertTrue(r.isMirrored)
    }

    func testAnOlderHubWithoutTheFieldReadsAsNotMirrored() throws {
        let json = #"""
        {"sessions":[{"key":"claude:1","harness":"claude","status":"done","ownership":"observed","attachable":true,
          "can_fork":true},
          {"key":"codex:2","harness":"codex","mirrored":false,"status":"done","ownership":"attached","attachable":true}]}
        """#
        let rows = try Hub.decoder.decode(SessionsResponse.self, from: Data(json.utf8)).sessions
        XCTAssertNil(rows[0].mirrored)
        XCTAssertFalse(rows[0].isMirrored)
        XCTAssertEqual(rows[1].mirrored, false)
        XCTAssertFalse(rows[1].isMirrored)
        XCTAssertEqual(SessionAction.trailing(rows[0]), [.pin, .rename, .fork], "a live row is unchanged")
        XCTAssertEqual(SessionRowMarks(rows[0]).tag, nil)
    }

    // MARK: The list row

    func testAMirroredRowShowsTheTagAndNoLiveMarks() {
        let marks = SessionRowMarks(row())
        XCTAssertEqual(marks.tag, "Mirrored")
        XCTAssertFalse(marks.showsStatus)
        XCTAssertFalse(marks.showsOwnership)
    }

    func testALiveRowKeepsItsStatusAndOwnershipMarks() {
        let marks = SessionRowMarks(row(mirrored: nil, status: "done"))
        XCTAssertNil(marks.tag)
        XCTAssertTrue(marks.showsStatus)
        XCTAssertTrue(marks.showsOwnership)
    }

    func testTheWordingMatchesTheWebApp() {
        XCTAssertEqual(Mirrored.notice, "Its transcript was deleted by the harness. swe kept a read-only copy.")
        XCTAssertEqual(Mirrored.chatUnavailable, "This session's chat is not available yet.")
        XCTAssertEqual(Mirrored.composerPlaceholder, "Read-only: a copy of a deleted transcript")
        XCTAssertTrue(Mirrored.tagAccessibility.hasPrefix("Mirrored. "))
        XCTAssertTrue(Mirrored.tagAccessibility.contains(Mirrored.notice))
    }

    // MARK: Actions

    func testNoAttachDetachOrForkOnAMirroredRow() {
        let r = row()
        XCTAssertEqual(SessionAction.leading(r), [.open])
        XCTAssertEqual(SessionAction.trailing(r), [.pin, .rename], "pin and rename stay, fork goes")
        XCTAssertEqual(SessionAction.menu(r), [.open, .pin, .rename])
        XCTAssertFalse(r.canAttach)
        XCTAssertFalse(r.offersFork)
        XCTAssertEqual(SessionAction.trailing(row(pinned: true)), [.unpin, .rename])
    }

    func testMirroredBeatsAttached() {
        // The supervisor may still hold an attached entry for the key.
        let r = row(ownership: "attached", status: "active")
        XCTAssertFalse(r.isAttached)
        XCTAssertFalse(r.canAttach)
        XCTAssertFalse(r.canSwitchModel)
        XCTAssertEqual(SessionAction.leading(r), [.open], "no Detach either")
        XCTAssertEqual(ChatBar.of(r), .mirroredComposer)
        XCTAssertTrue(ShareTargets.sessions([r]).isEmpty, "the share sheet doesn't offer it")
        // The same row, not mirrored, is a normal attached session.
        let live = row(mirrored: false, ownership: "attached", status: "idle")
        XCTAssertTrue(live.isAttached)
        XCTAssertEqual(ChatBar.of(live), .composer)
        XCTAssertEqual(ShareTargets.sessions([live]).map(\.key), ["claude:1"])
    }

    func testAnOwnershipEventCannotMakeAMirroredRowAttached() {
        let app = AppModel(hub: nil)
        app.applyLists(sessions: [row()], machines: nil, inbox: [], attachedAtStart: [])
        app.patch("claude:1", ownership: "attached")
        XCTAssertEqual(app.session("claude:1")?.ownership, "attached")
        XCTAssertEqual(app.session("claude:1")?.isAttached, false)
        XCTAssertEqual(ChatBar.of(app.session("claude:1")), .mirroredComposer)
    }

    func testAttachingAMirroredSessionNeverReachesTheHub() async {
        let app = AppModel(hub: nil)
        app.applyLists(sessions: [row()], machines: nil, inbox: [], attachedAtStart: [])
        do {
            _ = try await app.attach("claude:1")
            XCTFail("a mirrored copy can't be attached")
        } catch let error as HubError {
            XCTAssertEqual(error.code, "mirrored")
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testTheWidgetNeverCountsAMirroredRow() {
        let live = row("a", mirrored: nil, status: "active")
        let stale = row("b", mirrored: true, status: "followup")
        let busy = row("c", mirrored: true, status: "active")
        let s = WidgetSummary.make(hubId: "h", hubName: "mac", sessions: [live, stale, busy], inbox: [],
                                   at: Date(timeIntervalSince1970: 1))
        XCTAssertEqual(s.needsYouCount, 0)
        XCTAssertEqual(s.workingCount, 1)
        XCTAssertEqual(s.working.map(\.key), ["a"])
    }

    // MARK: The chat

    func testTheChatBarFollowsTheRow() {
        XCTAssertEqual(ChatBar.of(row()), .mirroredComposer)
        XCTAssertEqual(ChatBar.of(row(mirrored: nil, ownership: "attached")), .composer)
        XCTAssertEqual(ChatBar.of(row(mirrored: nil, ownership: "observed")), .attachCard)
        XCTAssertEqual(ChatBar.of(nil), .attachCard, "an unknown row is the old read-only card")
    }

    func testTheEmptyStateIsCalmForAMirroredSession() {
        let m = row()
        XCTAssertEqual(ChatEmpty.of(row: m, loading: true, loadError: nil, hasItems: false, hasStreaming: false), .skeleton)
        XCTAssertEqual(ChatEmpty.of(row: m, loading: false, loadError: "Not found", hasItems: false, hasStreaming: false),
                       .mirroredUnavailable, "a load failure is not an error here")
        XCTAssertEqual(ChatEmpty.of(row: m, loading: false, loadError: nil, hasItems: false, hasStreaming: false),
                       .mirroredUnavailable)
        XCTAssertNil(ChatEmpty.of(row: m, loading: false, loadError: nil, hasItems: true, hasStreaming: false),
                     "the saved chat shows when there is one")
        // A live session keeps its error and its empty card.
        let live = row(mirrored: nil)
        XCTAssertEqual(ChatEmpty.of(row: live, loading: false, loadError: "Not found", hasItems: false, hasStreaming: false),
                       .error("Not found"))
        XCTAssertEqual(ChatEmpty.of(row: live, loading: false, loadError: nil, hasItems: false, hasStreaming: false), .noMessages)
        XCTAssertNil(ChatEmpty.of(row: live, loading: false, loadError: nil, hasItems: false, hasStreaming: true))
        // A chat opened before the list loaded has no row yet: today's states.
        XCTAssertEqual(ChatEmpty.of(row: nil, loading: false, loadError: "x", hasItems: false, hasStreaming: false), .error("x"))
    }

    func testAMirroredChatCannotSendInterruptOrAttach() async {
        let app = AppModel(hub: nil)
        app.applyLists(sessions: [row(ownership: "attached")], machines: nil, inbox: [], attachedAtStart: [])
        let chat = app.chat("claude:1")
        XCTAssertTrue(chat.isMirrored)
        chat.draft = "hello"
        await chat.send()
        XCTAssertEqual(chat.draft, "hello", "a send is refused before it clears the box")
        XCTAssertTrue(chat.items.isEmpty)
        await chat.interrupt()
        await chat.attach()
        XCTAssertFalse(chat.attaching)
        XCTAssertNil(chat.attachError)
        XCTAssertTrue(chat.items.isEmpty, "no error line either")
    }

    // MARK: Review fixes (a cached chat, a late flag, every write, the inbox)

    private func hubApp(_ rows: [SessionRow]) -> AppModel {
        let app = AppModel(hub: PairedHub(baseURL: URL(string: "http://127.0.0.1:8477")!,
                                          credential: Credential(deviceId: "d", credential: "c", name: nil)))
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MirrorHubStub.self]
        app.clientSession = URLSession(configuration: config)
        app.applyLists(sessions: rows, machines: nil, inbox: [], attachedAtStart: [])
        MirrorHubStub.reset()
        return app
    }

    private func settle(_ until: () -> Bool) async {
        for _ in 0..<400 where !until() { try? await Task.sleep(nanoseconds: 5_000_000) }
    }

    func testACachedChatThatBecameMirroredReloadsItsHistory() async {
        let app = hubApp([row(mirrored: nil, ownership: "observed")])
        MirrorHubStub.messages = [MirrorHubStub.message(1, "first")]
        let chat = app.chat("claude:1")
        await chat.start()
        chat.stop()
        XCTAssertEqual(chat.items.count, 1)
        // The harness deleted the transcript; a message arrived meanwhile.
        app.applyLists(sessions: [row()], machines: nil, inbox: [], attachedAtStart: [])
        MirrorHubStub.messages = [MirrorHubStub.message(1, "first"), MirrorHubStub.message(2, "second")]
        MirrorHubStub.reset()
        await chat.start()
        XCTAssertEqual(chat.items.count, 2, "the cached mirrored chat loads what it missed")
        XCTAssertFalse(chat.hasStream)
        XCTAssertFalse(MirrorHubStub.paths.contains { $0.hasSuffix("/queue") }, "no queue read for a copy")
        chat.stop()
    }

    func testAFlagArrivingDuringTheQueueReadOpensNoStream() async {
        let app = hubApp([row(mirrored: nil, ownership: "observed")])
        MirrorHubStub.messages = [MirrorHubStub.message(1, "first")]
        MirrorHubStub.holdQueue = true
        let chat = app.chat("claude:1")
        let started = Task { await chat.start() }
        await settle { MirrorHubStub.paths.contains { $0.hasSuffix("/queue") } }
        // The list lands and says mirrored while the queue is still out.
        app.applyLists(sessions: [row()], machines: nil, inbox: [], attachedAtStart: [])
        chat.mirroredLearned()
        MirrorHubStub.releaseQueue()
        await started.value
        XCTAssertFalse(chat.hasStream, "the pending start must not open a stream")
        chat.stop()
    }

    func testEveryWritePathRefusesAMirroredChat() async {
        let app = hubApp([row(ownership: "attached")])
        let chat = app.chat("claude:1")
        chat.draft = "hi"
        await chat.send()
        await chat.sendNow("c1")
        await chat.remove("c1")
        await chat.saveEdit()
        await chat.retry(ChatItem(id: "send-x", kind: .user(text: "hi", send: .failed("Couldn't send"))))
        await chat.decide("r1", decision: "allow")
        await chat.switchModel(to: "claude-opus-4-5")
        await chat.interrupt()
        await chat.attach()
        let upload = try? await chat.attachments.upload("a.txt", URL(fileURLWithPath: "/dev/null"), { _ in })
        XCTAssertNil(upload)
        XCTAssertTrue(MirrorHubStub.paths.isEmpty, "nothing reached the hub: \(MirrorHubStub.paths)")
        XCTAssertEqual(chat.draft, "hi")
    }

    func testTheInboxLeavesOutApprovalsForMirroredSessions() {
        let app = AppModel(hub: nil)
        let gh = InboxItem(key: "gh:1", machineId: nil, harness: nil, agent: nil, title: "PR", cwd: nil,
                           status: nil, lastActiveAt: 7, requestId: nil, kind: "github")
        let stale = InboxItem(key: "claude:1", machineId: "m1", harness: "claude", agent: nil, title: "old", cwd: "/w",
                              status: "approval", lastActiveAt: 5, requestId: "r1", kind: nil)
        let live = InboxItem(key: "claude:2", machineId: "m1", harness: "claude", agent: nil, title: "live", cwd: "/w",
                             status: "approval", lastActiveAt: 6, requestId: "r2", kind: nil)
        app.applyLists(sessions: [row("claude:1", mirrored: true, status: nil),
                                  row("claude:2", mirrored: nil, status: "approval")],
                       machines: nil, inbox: [stale, live, gh], attachedAtStart: [])
        XCTAssertEqual(app.inboxRows.map(\.key), ["claude:2", "gh:1"], "the page drops the copy and keeps GitHub rows")
        XCTAssertEqual(app.waitingOnYou.map(\.key), ["claude:2"])
        XCTAssertEqual(app.inboxBadge, 1, "the badge agrees with the widget")
        let rows = app.displaySessions
        XCTAssertEqual(rows.first { $0.key == "claude:1" }?.status, nil, "not marked approval in the list")
        XCTAssertEqual(rows.first { $0.key == "claude:2" }?.status, "approval")
    }

    func testAnOutstandingApprovalDoesNotCountForAMirroredWidgetRow() {
        let mirrored = row("claude:1", mirrored: true, status: nil)
        let live = row("claude:2", mirrored: nil, status: "approval")
        let items = [
            InboxItem(key: "claude:1", machineId: "m1", harness: "claude", agent: nil, title: "old", cwd: "/w",
                      status: "approval", lastActiveAt: 5, requestId: "r1", kind: nil),
            InboxItem(key: "claude:2", machineId: "m1", harness: "claude", agent: nil, title: "live", cwd: "/w",
                      status: "approval", lastActiveAt: 6, requestId: "r2", kind: nil),
        ]
        let s = WidgetSummary.make(hubId: "h", hubName: "mac", sessions: [mirrored, live], inbox: items,
                                   at: Date(timeIntervalSince1970: 1))
        XCTAssertEqual(s.needsYou.map(\.key), ["claude:2"])
        XCTAssertEqual(s.needsYouCount, 1)
    }
}

/// Answers the hub's chat routes from memory and records each path.
final class MirrorHubStub: URLProtocol {
    private static let lock = NSLock()
    private static var _paths: [String] = []
    private static var _messages: [String] = []
    private static var _holdQueue = false
    private static var held: [MirrorHubStub] = []
    static var paths: [String] { lock.withLock { _paths } }
    static var messages: [String] {
        get { lock.withLock { _messages } }
        set { lock.withLock { _messages = newValue } }
    }
    static var holdQueue: Bool {
        get { lock.withLock { _holdQueue } }
        set { lock.withLock { _holdQueue = newValue } }
    }
    static func reset() { lock.withLock { _paths = []; _holdQueue = false; held = [] } }
    static func message(_ seq: Int, _ text: String) -> String {
        #"{"type":"message.appended","seq":\#(seq),"data":{"kind":"assistant","text":"\#(text)"}}"#
    }
    static func releaseQueue() {
        let pending = lock.withLock { () -> [MirrorHubStub] in _holdQueue = false; let h = held; held = []; return h }
        for stub in pending { stub.answer(#"{"sends":[]}"#) }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        let path = request.url?.path ?? ""
        let hold = Self.lock.withLock { () -> Bool in
            Self._paths.append(path)
            if path.hasSuffix("/queue"), Self._holdQueue { Self.held.append(self); return true }
            return false
        }
        if hold { return }
        if path.hasSuffix("/queue") { return answer(#"{"sends":[]}"#) }
        if path.hasSuffix("/messages") {
            let list = Self.messages.joined(separator: ",")
            return answer(#"{"messages":[\#(list)],"hasMore":false}"#)
        }
        answer("{}")
    }
    private func answer(_ body: String) {
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil,
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
}
