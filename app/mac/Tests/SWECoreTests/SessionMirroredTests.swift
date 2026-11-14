import Foundation
import Testing
@testable import SWECore

/// Mirrored sessions on the Mac (#1918): swe lists a session whose
/// transcript the harness pruned from swe's read-only copy, marked
/// `mirrored`. `swe-session-mirrored.json` is real `swe session 5 --json`
/// output from a throwaway HOME (one live session, one mirrored, its source
/// deleted after `swe session mirror`). `swe-session-restore.json` is built
/// to the shape docs/json-contracts.md gives `swe session restore --json`
/// in the restore PR, since the command is not merged yet.
@Suite struct SessionMirroredTests {
    private func client(_ name: String) throws -> (SweClient, FakeRunner) {
        let runner = FakeRunner(.init(status: 0, stdout: try fixture(name), stderr: ""))
        return (SweClient(runner: runner), runner)
    }

    @Test func theListingCarriesMirroredAndAMirroredRowNeverResumes() async throws {
        let (swe, _) = try client("swe-session-mirrored")
        let list = try await swe.sessions(days: nil, limit: 5)
        let live = try #require(list.sessions.first { $0.title == "live heron question" })
        let copy = try #require(list.sessions.first { $0.title == "quokka migration plan" })
        #expect(!live.mirrored)
        #expect(live.resume != nil)
        #expect(copy.mirrored)
        #expect(copy.resume == nil)
        #expect(copy.status == nil)
    }

    @Test func aRowThatSaysMirroredLosesAnyResumeItCarries() throws {
        let json = #"{"id":"s","harness":"claude","last_active_at":1,"mirrored":true,"#
            + #""resume":{"argv":["claude","--resume","s"],"cwd":"/w"}}"#
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let session = try decoder.decode(SessionInfo.self, from: Data(json.utf8))
        #expect(session.mirrored)
        #expect(session.resume == nil)
    }

    @Test func anOlderSweWithoutTheFieldListsLiveSessions() async throws {
        let (swe, _) = try client("swe-session")
        #expect(try await swe.sessions(days: nil, limit: 5).sessions.allSatisfy { !$0.mirrored })
    }

    @Test func theSavedListKeepsTheFlag() throws {
        let session = SessionInfo(id: "s", harness: "claude", agent: "Claude Code", cwd: nil, title: nil,
                                  titleSource: nil, lastActiveAt: Date(timeIntervalSince1970: 1), status: nil,
                                  resume: nil, mirrored: true)
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        #expect(try decoder.decode(SessionInfo.self, from: encoder.encode(session)) == session)
    }

    @Test func restoreBuildsTheCommandForEachHome() {
        let defaults: Set = ["/u/.claude", "/u/.codex"]
        #expect(SessionRestore.arguments(id: "abc", accountHome: nil, defaultHomes: defaults)
                == ["session", "restore", "abc", "--account=default", "--json"])
        #expect(SessionRestore.arguments(id: "abc", accountHome: "/u/.claude", defaultHomes: defaults)
                == ["session", "restore", "abc", "--account=default", "--json"])
        #expect(SessionRestore.arguments(id: "abc", accountHome: "/u/.swe/accounts/claude/work/home",
                                         defaultHomes: defaults)
                == ["session", "restore", "abc", "--account=work", "--json"])
    }

    @Test func restoreDecodesWhatSweDid() async throws {
        let (swe, runner) = try client("swe-session-restore")
        let restored = try await swe.restoreSession(id: "11111111-2222-3333-4444-555555555555", accountHome: nil)
        #expect(runner.calls == [["session", "restore", "11111111-2222-3333-4444-555555555555",
                                  "--account=default", "--json"]])
        #expect(restored.harness == "claude")
        #expect(restored.target.hasSuffix(".claude/projects/-tmp-swe-fixture/11111111-2222-3333-4444-555555555555.jsonl"))
        #expect(restored.bytes == 749)
        #expect(restored.how == "cloned")
        #expect(SessionRestore.success(restored).hasPrefix("Restored the transcript to "))
    }

    @Test func aRefusalSaysWhatSweSaid() async throws {
        let runner = FakeRunner(.init(status: 1, stdout: Data(),
                                      stderr: "\u{1B}[31mNot restored: a file already sits at the target\u{1B}[0m\n"))
        await #expect {
            _ = try await SweClient(runner: runner).restoreSession(id: "s", accountHome: nil)
        } throws: { error in
            SessionRestore.failure(error) == "Couldn't restore: Not restored: a file already sits at the target"
        }
    }

    /// The bundled swe from before the restore command: its own words.
    @Test func anSweWithoutRestoreSaysSoPlainly() async throws {
        let runner = FakeRunner(.init(status: 1, stdout: Data(), stderr: "\u{1B}[31mUnknown argument: restore\u{1B}[0m\n"))
        await #expect {
            _ = try await SweClient(runner: runner).restoreSession(id: "s", accountHome: nil)
        } throws: { error in
            SessionRestore.failure(error) == "This swe can't restore transcripts yet. Update swe, then try again."
        }
    }

    @Test func aMirroredHubRowCannotForkAttachOrSend() throws {
        let decoder = HubWire.decoder
        let row = try decoder.decode(HubSessionRow.self, from: Data(
            #"{"key":"claude:s1","mirrored":true,"ownership":"attached","attachable":true,"can_fork":true}"#.utf8))
        #expect(row.isMirrored)
        #expect(!row.isAttached)
        #expect(!HubSessionMeta(row).canFork)
        #expect(HubSessionMeta(row).mirrored)
        let live = try decoder.decode(HubSessionRow.self, from: Data(#"{"key":"claude:s2","can_fork":true}"#.utf8))
        #expect(!live.isMirrored)
        #expect(HubSessionMeta(live).canFork)
    }

    @Test func thePickerListFitsEveryHarnessBeforeItScrolls() {
        // The AGENTS.md Compile tab: "All" and thirteen harnesses, rows of
        // 28pt, 21pt of padding and divider.
        #expect(!PopoverList.scrolls(rows: 14, rowHeight: 28, chrome: 21))
        #expect(PopoverList.height(rows: 14, rowHeight: 28, chrome: 21) == 413)
        // The old cap cut that list off.
        #expect(PopoverList.height(rows: 14, rowHeight: 28, chrome: 21) > 360)
        // A short menu keeps its own height.
        #expect(PopoverList.height(rows: 5, rowHeight: 28, chrome: 21) == 161)
        // A long one stops at the cap and scrolls.
        #expect(PopoverList.scrolls(rows: 30, rowHeight: 28, chrome: 21))
        #expect(PopoverList.height(rows: 30, rowHeight: 28, chrome: 21) == PopoverList.maxHeight)
    }
}

extension HubStubbed {
@MainActor
@Suite struct MirroredChatTests {
    let scratch: TempDir
    init() throws { scratch = try TempDir() }

    /// A mirrored chat reads its history once and asks for nothing that
    /// only a live session has: no stream, approval or queue.
    @Test(.timeLimit(.minutes(1))) func aMirroredChatReadsHistoryOnly() async throws {
        HubStub.requests = []
        let store = HubDeviceStore(url: scratch.url.appending(path: "app-device.json"))
        try store.save(HubCredential(deviceId: "d1", credential: "secret"), for: "127.0.0.1:8477")
        HubStub.handler = { req in
            switch req.url?.path {
            case "/v1/sessions":
                return (200, Data(#"{"sessions": [{"key": "claude:s1", "mirrored": true, "ownership": "observed"}]}"#.utf8))
            case "/v1/sessions/claude:s1/messages":
                return (200, Data(#"""
                {"messages": [{"type": "message.appended", "seq": 1, "data": {"kind": "user", "text": "saved"}}],
                 "has_more": false}
                """#.utf8))
            default: return (200, Data("{}".utf8))
            }
        }
        let hub = LocalHub(locator: FakeLocator(address: "127.0.0.1:8477"), store: store) {
            HubClient(baseURL: $0, credential: $1, session: HubStub.session(), streamSession: HubStub.session())
        }
        let chat = SessionChatModel(key: "claude:s1", hub: hub)
        await chat.start()
        #expect(chat.isMirrored)
        #expect(chat.items.map(\.id) == ["m1"])
        #expect(!chat.loading)
        #expect(!chat.canSend)
        #expect(!chat.canAttach)
        await chat.attach()
        chat.stop()
        await chat.settleReads()
        let paths = HubStub.requests.compactMap { $0.url?.path }
        #expect(!paths.contains { $0.hasSuffix("/events") || $0.hasSuffix("/queue") || $0 == "/v1/inbox" })
        #expect(!paths.contains { $0.hasSuffix("/control") })
    }
}
}
