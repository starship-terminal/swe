import Foundation
import Testing
@testable import SWECore

/// `SearchIndexer` is the second consumer of the ledger's transcript read:
/// one scan feeds the usage ledger and the FTS index together, and the
/// index's own `files` rows decide what each pass owes — skip, current,
/// append, or whole.
@Suite struct SearchIndexerTests {
    /// A claude user line in the spelling `SearchClaudeDecoder` reads. The
    /// ledger's parser drops it (no `usage`), which is fine — the cursor
    /// still advances and the index still gets the text.
    private func userLine(_ text: String, session: String = "s1",
                          ts: String = "2026-10-05T12:00:00.000Z") -> String {
        #"{"type":"user","timestamp":"\#(ts)","sessionId":"\#(session)","cwd":"/tmp/p","message":{"role":"user","content":"\#(text)"}}"#
            + "\n"
    }

    private func assistantLine(_ text: String, session: String = "s1",
                               ts: String = "2026-10-05T12:00:01.000Z") -> String {
        #"{"type":"assistant","timestamp":"\#(ts)","sessionId":"\#(session)","message":{"role":"assistant","content":[{"type":"text","text":"\#(text)"}]}}"#
            + "\n"
    }

    struct Setup {
        let temp: TempDir
        let home: URL
        let file: URL
        let ledger: Ledger
        let store: SearchStore
        let indexer: SearchIndexer
        var roots: LedgerRoots { LedgerRoots(claudeHomes: [home]) }
        /// The path the scan keys on — temp dirs sit under /var, a symlink.
        var path: String { file.resolvingSymlinksInPath().path }

        init(bufferBudget: Int? = nil) throws {
            temp = try TempDir()
            home = temp.path("claude")
            file = home.appendingPathComponent("projects/-p/s1.jsonl")
            try FileManager.default.createDirectory(
                at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            ledger = try Ledger(directory: temp.path("cache"))
            store = try SearchStore(directory: temp.path("data/search"))
            let s = store
            indexer = bufferBudget.map { SearchIndexer(store: s, bufferBudget: $0) }
                ?? SearchIndexer(store: s)
        }

        /// Connects the index to the scan.
        func attach() async { await ledger.setIndexer(indexer) }
        func write(_ text: String) throws { try Data(text.utf8).write(to: file) }
        func append(_ text: String) throws { try FileManager.default.append(text, to: file) }
        func row() throws -> SearchFileRow? { try store.fileRow(for: path) }
        func fts(_ query: String) throws -> [Int64] {
            try store.ftsRowIDs(query)
        }
        func stat() -> SearchFileStat {
            let info = FileInfo(path: path)!
            return SearchFileStat(
                ino: Int64(info.inode), mtimeMs: info.modified * 1000, sizeBytes: Int64(info.size))
        }
    }

    @Test func aScanIndexesTheTranscriptForFullTextSearch() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("fix the flaky test in Ledger") + assistantLine("found the flake"))
        let size = Int64(try Data(contentsOf: s.file).count)

        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesRead == 1)
        #expect(report.filesIndexed == 1)
        #expect(try s.fts("flaky").count == 1)
        #expect(try s.fts("flake").count == 1)
        let row = try #require(try s.row())
        #expect(row.state == .current)
        #expect(row.byteOffset == size)
        #expect(row.sizeBytes == size)
        let sessionRowID = try #require(row.sessionRowID)
        let session = try #require(try s.store.sessionRow(sessionRowID))
        #expect(session.fields.agent == "claude")
        #expect(session.fields.sessionID == "s1")
        // The first user prompt is the fallback session title.
        #expect(session.fields.title == "fix the flaky test in Ledger")
        #expect(session.fields.cwd == "/tmp/p")
        #expect(session.fields.createdAt == "2026-10-05T12:00:00.000Z")
        #expect(session.fields.updatedAt == "2026-10-05T12:00:01.000Z")
        #expect(session.fields.messageCount == 2)
    }

    @Test func anUnchangedFileIsNotReindexed() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("first prompt"))
        try await s.ledger.scan(roots: s.roots)
        #expect(try s.store.status().messageCount == 1)

        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesUnchanged == 1)
        #expect(report.filesRead == 0)
        #expect(report.filesIndexed == 0)
        #expect(try s.store.status().messageCount == 1)
    }

    @Test func anAppendedFileIndexesOnlyTheNewLines() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("first prompt"))
        try await s.ledger.scan(roots: s.roots)
        let before = try #require(try s.row())

        try s.append(assistantLine("zebra reply", ts: "2026-10-05T12:00:02.000Z"))
        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesRead == 1)
        #expect(report.filesRescanned == 0)
        #expect(report.filesIndexed == 1)
        let after = try #require(try s.row())
        // The append continued the same session generation.
        #expect(after.sessionRowID == before.sessionRowID)
        #expect(after.byteOffset > before.byteOffset)
        #expect(try s.store.status().messageCount == 2)
        #expect(try s.fts("zebra").count == 1)
        // Session fields accumulate across the append.
        let session = try #require(try s.store.sessionRow(after.sessionRowID ?? -1))
        #expect(session.fields.messageCount == 2)
        #expect(session.fields.updatedAt == "2026-10-05T12:00:02.000Z")
    }

    @Test func aRewrittenFileStartsANewSessionGeneration() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("quokka prompt") + userLine("second quokka", ts: "2026-10-05T12:00:01.000Z"))
        try await s.ledger.scan(roots: s.roots)
        let before = try #require(try s.row())
        let oldSession = try #require(before.sessionRowID)
        #expect(try s.fts("quokka").count == 2)

        // A shorter rewrite rescans: the old session is replaced in one
        // transaction and its messages drain at the end of the scan.
        try s.write(userLine("fennec question"))
        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesRescanned == 1)
        #expect(report.filesIndexed == 1)
        let after = try #require(try s.row())
        #expect(after.sessionRowID != oldSession)
        #expect(try s.store.sessionRow(oldSession) == nil)
        #expect(try s.fts("quokka").isEmpty)
        #expect(try s.fts("fennec").count == 1)
        #expect(try s.store.status().messageCount == 1)
    }

    @Test(skippedAsRoot) func anUnreadableFileAccumulatesFailuresThenHoldsOut() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("hello"))
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: s.path)

        for round in 1...3 {
            let report = try await s.ledger.scan(roots: s.roots)
            #expect(report.notes.count == 1, "scan \(round)")
            let row = try #require(try s.row())
            #expect(row.state == .failed)
            #expect(row.failCount == round)
        }

        // Three failures at the same mtime hold the file out: the index no
        // longer feeds on it, so a fourth scan adds no fourth failure.
        #expect(s.indexer.readNeed(path: s.path, stat: s.stat(), ledgerOffset: nil) == .skip)
        _ = try await s.ledger.scan(roots: s.roots)
        #expect(try s.row()?.failCount == 3)

        // Only a stat change releases the holdout.
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: s.path)
        try s.append(userLine("back again", ts: "2026-10-05T12:00:02.000Z"))
        let report = try await s.ledger.scan(roots: s.roots)
        #expect(report.filesIndexed == 1)
        let row = try #require(try s.row())
        #expect(row.state == .current && row.failCount == 0)
        #expect(try s.fts("again").count == 1)
    }

    @Test func subagentTranscriptsAreNeverIndexed() async throws {
        let s = try Setup()
        await s.attach()
        let sub = s.home.appendingPathComponent("projects/-p/subagents/agent-1.jsonl")
        try FileManager.default.createDirectory(
            at: sub.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(userLine("subagent chatter").utf8).write(to: sub)

        let report = try await s.ledger.scan(roots: s.roots)

        // The ledger still reads it for tokens; the index never sees it.
        #expect(report.filesRead == 1)
        #expect(report.filesIndexed == 0)
        #expect(try s.store.files().isEmpty)
        #expect(try s.store.status().messageCount == 0)
    }

    /// `~/.swe/accounts/claude/subagents/home`: the folder named
    /// `subagents` is the account's id, above the transcript root, so its
    /// sessions are indexed like any other home's (#1946); a subagent
    /// transcript below the root is still left out. swe's
    /// `test_an_account_whose_id_is_subagents_is_indexed` pins the same.
    @Test func anAccountWhoseIDIsSubagentsIsIndexed() async throws {
        let s = try Setup()
        let home = s.temp.path(".swe/accounts/claude/subagents/home")
        let session = home.appendingPathComponent("projects/-work-acct/sess-acct.jsonl")
        let sub = home.appendingPathComponent("projects/-work-acct/sess-acct/subagents/agent-a1.jsonl")
        try FileManager.default.createDirectory(
            at: sub.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(userLine("quokka account plan", session: "sess-acct").utf8).write(to: session)
        try Data(userLine("wombat delegation chatter", session: "sess-acct").utf8).write(to: sub)

        // The ledger already read both, as it did while the index skipped
        // the account: its cursors are current, the index holds no row.
        let roots = LedgerRoots(claudeHomes: [home])
        #expect(try await s.ledger.scan(roots: roots).filesRead == 2)
        await s.attach()

        let report = try await s.ledger.scan(roots: roots)

        // The next pass picks the session up with no rebuild; the subagent
        // transcript stays a ledger-only file.
        #expect(report.filesRead == 1)
        #expect(report.filesIndexed == 1)
        #expect(try s.store.files().map(\.path) == [session.resolvingSymlinksInPath().path])
        #expect(try s.fts("quokka").count == 1)
        #expect(try s.fts("wombat").isEmpty)
    }

    /// The rule swe's `should_index` applies, case for case
    /// (`test_only_folders_below_the_transcript_root_count`).
    @Test func onlyFoldersBelowTheTranscriptRootCount() {
        let root = "/h/.swe/accounts/claude/subagents/home/projects"
        #expect(SearchIndexer.shouldIndex(path: root + "/-p/s.jsonl", root: root))
        #expect(!SearchIndexer.shouldIndex(path: root + "/-p/s/subagents/a.jsonl", root: root))
        #expect(!SearchIndexer.shouldIndex(path: root + "/subagents/a.jsonl", root: root))
        #expect(!SearchIndexer.shouldIndex(path: root + "/-p/subagents/deeper/a.jsonl", root: root))
        // Outside the root (a linked file's target elsewhere): the walk
        // already decided, by the folders it entered.
        #expect(SearchIndexer.shouldIndex(path: "/elsewhere/subagents/a.jsonl", root: root))
        #expect(SearchIndexer.shouldIndex(path: root + "-other/subagents/a.jsonl", root: root))
        // A file named `subagents` is no folder.
        #expect(SearchIndexer.shouldIndex(path: root + "/-p/subagents", root: root))
    }

    @Test func aTranscriptWithNothingToDecodeStillRecordsItsCursor() async throws {
        let s = try Setup()
        await s.attach()
        // Progress/system lines carry no session id and no messages.
        try Data((#"{"type":"progress","sessionId":"s1","data":{}}"# + "\n").utf8).write(to: s.file)
        let size = Int64(try Data(contentsOf: s.file).count)

        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesIndexed == 1)
        let row = try #require(try s.row())
        #expect(row.state == .current && row.byteOffset == size)
        #expect(row.sessionRowID == nil)
        #expect(try s.store.status().sessionCount == 0)
    }

    @Test func devinTranscriptDocumentsIndexBesideTheDatabase() async throws {
        let temp = try TempDir()
        let devin = temp.path("devin/cli")
        let transcripts = devin.appendingPathComponent("transcripts")
        try FileManager.default.createDirectory(at: transcripts, withIntermediateDirectories: true)
        let document = #"""
        {
          "schema_version": "ATIF-v1.7",
          "session_id": "alluring-collard",
          "working_directory": "/repo/demo",
          "steps": [
            {"step_id": 1, "timestamp": "2026-10-19T14:40:53Z", "source": "system", "message": "You are Devin..."},
            {"step_id": 9, "timestamp": "2026-10-19T14:41:15Z", "source": "user", "message": "which drives exist?"},
            {"step_id": 13, "timestamp": "2026-10-19T14:41:28Z", "source": "agent", "message": "Found them: aardvark and bobcat."}
          ]
        }
        """#
        try Data(document.utf8).write(to: transcripts.appendingPathComponent("alluring-collard.json"))

        let ledger = try Ledger(directory: temp.path("cache"))
        let store = try SearchStore(directory: temp.path("data/search"))
        await ledger.setIndexer(SearchIndexer(store: store))
        let report = try await ledger.scan(roots: LedgerRoots(
            devinDatabases: [devin.appendingPathComponent("sessions.db")]))

        #expect(report.filesIndexed == 1)
        #expect(try store.db.scalar("SELECT count(*) FROM messages_fts") == 2)
        let statement = try store.db.prepare(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?")
        statement.bind([.text("aardvark")])
        #expect(try statement.step())
        let sessionRowID = try #require(try store.files().first?.sessionRowID)
        let session = try #require(try store.sessionRow(sessionRowID))
        #expect(session.fields.agent == "devin")
        #expect(session.fields.sessionID == "alluring-collard")
        #expect(session.fields.title == "which drives exist?")
    }

    @Test func aLedgerWithoutAnIndexerWritesNoSearchRows() async throws {
        let s = try Setup()
        // setIndexer is never called: the feature is off.
        try s.write(userLine("unindexed"))

        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesRead == 1)
        #expect(report.filesIndexed == 0)
        #expect(try s.store.files().isEmpty)
        #expect(try s.store.status().sessionCount == 0)
    }

    @Test func aLargeWholeReadCommitsInChunks() async throws {
        let s = try Setup(bufferBudget: 100)
        await s.attach()
        // Each line's text exceeds the tiny budget: every line flushes.
        try s.write(userLine(String(repeating: "aardvark ", count: 30))
            + assistantLine(String(repeating: "bobcat ", count: 30))
            + userLine(String(repeating: "cheetah ", count: 30),
                         ts: "2026-10-05T12:00:02.000Z"))
        let size = Int64(try Data(contentsOf: s.file).count)

        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesIndexed == 1)
        #expect(s.indexer.midReadFlushes >= 1)
        let row = try #require(try s.row())
        #expect(row.state == .current)
        // The finishing commit moved the cursor off the partial sentinel.
        #expect(row.byteOffset == size)
        // Every chunked insert landed once, under one session.
        #expect(try s.store.status().messageCount == 3)
        #expect(try s.fts("aardvark").count == 1)
        #expect(try s.fts("cheetah").count == 1)
        let session = try #require(try s.store.sessionRow(row.sessionRowID ?? -1))
        #expect(session.fields.messageCount == 3)
        #expect(session.fields.updatedAt == "2026-10-05T12:00:02.000Z")
    }

    @Test func aReadAbandonedMidFlushReReadsWhole() async throws {
        let s = try Setup(bufferBudget: 50)
        let path = s.path
        let lines = userLine(String(repeating: "aardvark ", count: 20))
            + userLine(String(repeating: "bobcat ", count: 20),
                       ts: "2026-10-05T12:00:01.000Z")
        try s.write(lines)

        // A read that flushed, then never finished — the process died.
        s.indexer.beginFile(path: path, harness: .claude)
        for line in lines.split(separator: "\n") {
            s.indexer.consume(line: Data(line.utf8))
        }
        #expect(s.indexer.midReadFlushes >= 1)
        let parked = try #require(try s.row())
        #expect(parked.byteOffset == SearchStore.partialFileOffset)
        let partialSession = try #require(parked.sessionRowID)
        // The parked row owes a whole read, not an append.
        #expect(s.indexer.readNeed(path: path, stat: s.stat(), ledgerOffset: nil) == .whole)
        s.indexer.beginFile(path: path, harness: .claude) // drop the stale slot

        let report = try await { () async throws -> ScanReport in
            await s.attach()
            return try await s.ledger.scan(roots: s.roots)
        }()

        #expect(report.filesIndexed == 1)
        let row = try #require(try s.row())
        #expect(row.state == .current && row.byteOffset > 0)
        // The partial generation was replaced, not appended to: each
        // message exists exactly once and the old session row is gone.
        #expect(try s.store.sessionRow(partialSession) == nil)
        #expect(try s.store.status().messageCount == 2)
        #expect(try s.fts("aardvark").count == 1)
    }

    @Test func anAppendedReadBuffersWithoutFlushing() async throws {
        let s = try Setup(bufferBudget: 50)
        await s.attach()
        try s.write(userLine("first prompt"))
        try await s.ledger.scan(roots: s.roots)
        let before = try #require(try s.row())

        // Well past the budget — but an append must never commit mid-file:
        // a committed tail chunk whose cursor never moved would re-read and
        // duplicate on the next pass.
        try s.append(assistantLine(String(repeating: "zebra ", count: 40),
                                   ts: "2026-10-05T12:00:02.000Z"))
        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesIndexed == 1)
        #expect(s.indexer.midReadFlushes == 0)
        let after = try #require(try s.row())
        #expect(after.sessionRowID == before.sessionRowID)
        #expect(try s.store.status().messageCount == 2)
        #expect(try s.fts("zebra").count == 1)
    }

    @Test func anIndexCommitFailureFailsTheFileNotTheScan() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("doomed prompt"))
        // Break the index's write path only: messages inserts fail, the
        // files table stays writable so the failure itself can be recorded.
        try s.store.db.execute("ALTER TABLE messages DROP COLUMN role")

        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesRead == 1)
        #expect(report.filesIndexed == 0)
        #expect(report.notes.contains { $0.contains("Index failed") })
        let row = try #require(try s.row())
        #expect(row.state == .failed && row.failCount == 1)
    }

    @Test func anAppendedPromptDoesNotRetitleTheSession() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("first prompt"))
        try await s.ledger.scan(roots: s.roots)
        let before = try #require(try s.row())

        // The appended span's first user text is a title candidate for a
        // fresh decoder; primed, it must not overwrite the stored title.
        try s.append(userLine("aardvark second prompt", ts: "2026-10-05T12:00:02.000Z"))
        let report = try await s.ledger.scan(roots: s.roots)

        #expect(report.filesRescanned == 0)
        let after = try #require(try s.row())
        #expect(after.sessionRowID == before.sessionRowID)
        let session = try #require(try s.store.sessionRow(after.sessionRowID ?? -1))
        #expect(session.fields.title == "first prompt")
        #expect(session.fields.messageCount == 2)
        #expect(try s.fts("aardvark").count == 1)
    }

    @Test func anAppendedExplicitTitleStillRetitles() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("first prompt"))
        try await s.ledger.scan(roots: s.roots)

        // Only prompts are suppressed on append — an explicit title
        // record in the new span still renames the session.
        try s.append(#"{"type":"custom-title","sessionId":"s1","customTitle":"Renamed session"}"# + "\n")
        _ = try await s.ledger.scan(roots: s.roots)

        let row = try #require(try s.row())
        let session = try #require(try s.store.sessionRow(row.sessionRowID ?? -1))
        #expect(session.fields.title == "Renamed session")
    }

    @Test func anAppendedPaginatedCodexRolloutIndexesLikeAWholeRead() async throws {
        let temp = try TempDir()
        let home = temp.path("codex")
        let dir = home.appendingPathComponent("sessions/2026/10/20")
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        func meta(_ id: String) -> String {
            #"{"timestamp":"2026-10-20T02:55:33.500Z","type":"session_meta","payload":{"id":"\#(id)","cwd":"/repo/app","history_mode":"paginated","title":"Fix admission loop"}}"#
        }
        func itemCompleted(_ type: String, _ text: String, _ ts: String) -> String {
            #"{"type":"event_msg","timestamp":"\#(ts)","payload":{"type":"item_completed","item":{"type":"\#(type)","content":[{"type":"text","text":"\#(text)"}]}}}"#
        }
        func head(_ id: String) -> String {
            [meta(id),
             itemCompleted("UserMessage", "aardvark first prompt", "t1"),
             itemCompleted("AgentMessage", "bobcat first reply", "t2")]
                .map { $0 + "\n" }.joined()
        }
        // A paginated tail: the response_item duplicates an item_completed
        // turn and must stay skipped; the item_completed rows carry it.
        let tail = [
            #"{"type":"response_item","timestamp":"t3","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"cheetah dup prompt"}]}}"#,
            itemCompleted("UserMessage", "dingo second prompt", "t4"),
            itemCompleted("AgentMessage", "emu second reply", "t5"),
        ].map { $0 + "\n" }.joined()

        let wholeFile = dir.appendingPathComponent("rollout-whole.jsonl")
        try Data((head("thread-whole") + tail).utf8).write(to: wholeFile)
        let appendFile = dir.appendingPathComponent("rollout-append.jsonl")
        try Data(head("thread-append").utf8).write(to: appendFile)

        let ledger = try Ledger(directory: temp.path("cache"))
        let store = try SearchStore(directory: temp.path("data/search"))
        await ledger.setIndexer(SearchIndexer(store: store))
        _ = try await ledger.scan(roots: LedgerRoots(codexHomes: [home]))

        try FileManager.default.append(tail, to: appendFile)
        let report = try await ledger.scan(roots: LedgerRoots(codexHomes: [home]))
        #expect(report.filesRescanned == 0)

        func texts(_ sessionRowID: Int64?) throws -> [String] {
            // The role's own column holds the text; the other two are "".
            let statement = try store.db.prepare("""
                SELECT messages.role,
                       coalesce(nullif(messages_fts.user_text, ''),
                                nullif(messages_fts.assistant_text, ''),
                                nullif(messages_fts.tool_text, ''))
                FROM messages JOIN messages_fts ON messages_fts.rowid = messages.id
                WHERE messages.session_row_id = ? ORDER BY messages.id
                """)
            statement.bind([.int(sessionRowID ?? -1)])
            var rows: [String] = []
            while try statement.step() {
                rows.append("\(statement.text(0) ?? "")|\(statement.text(1) ?? "")")
            }
            return rows
        }
        let wholeRow = try #require(
            try store.fileRow(for: wholeFile.resolvingSymlinksInPath().path))
        let appendRow = try #require(
            try store.fileRow(for: appendFile.resolvingSymlinksInPath().path))
        // The append continued the same session generation and decoded the
        // tail in the paginated shape — response_item skipped, the
        // item_completed rows indexed — matching the whole read.
        #expect(appendRow.sessionRowID != nil)
        #expect(try texts(appendRow.sessionRowID) == texts(wholeRow.sessionRowID))
        #expect(try texts(appendRow.sessionRowID) == [
            "user|aardvark first prompt", "assistant|bobcat first reply",
            "user|dingo second prompt", "assistant|emu second reply"])
        let session = try #require(try store.sessionRow(appendRow.sessionRowID ?? -1))
        #expect(session.fields.title == "Fix admission loop")
        #expect(session.fields.messageCount == 4)
    }

    @Test func appendNeedsAnAppendableHarness() async throws {
        let s = try Setup()
        await s.attach()
        try s.write(userLine("first prompt"))
        try await s.ledger.scan(roots: s.roots)
        try s.append(assistantLine("more", ts: "2026-10-05T12:00:02.000Z"))
        // The index cursor sits where the ledger resumes: an appendable
        // harness takes the appended lines, anything else re-reads whole.
        let offset = UInt64(bitPattern: try #require(try s.row()).byteOffset)
        #expect(s.indexer.readNeed(path: s.path, stat: s.stat(),
                                 ledgerOffset: offset, harness: .claude) == .append)
        #expect(s.indexer.readNeed(path: s.path, stat: s.stat(),
                                 ledgerOffset: offset, harness: .devin) == .whole)
        #expect(s.indexer.readNeed(path: s.path, stat: s.stat(),
                                 ledgerOffset: offset) == .whole)
    }
}
