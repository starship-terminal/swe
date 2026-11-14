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
}
