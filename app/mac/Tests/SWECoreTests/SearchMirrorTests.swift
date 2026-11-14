import Foundation
import Testing
@testable import SWECore

/// Search over transcript mirror copies (#1918), the Swift twin of
/// `tests/test_search_mirror.py`. Each suite pins one invariant of the
/// rule in docs/ported-logic.md (`SearchMirror.indexCopies`,
/// `SearchMirror.copiedRows`, the store's fenced deletes):
///
/// 1. never worse than no mirror: live sessions search exactly as with no
///    mirror, in every mirror state;
/// 2. one session, one set of rows;
/// 3. nothing deleted for bookkeeping, and a row is replaced only by a
///    copy at least as complete;
/// 4. newer wins;
/// 5. staged reads with an interleaved second writer move and remove only
///    the exact message ids they wrote;
/// 6. deletes are fenced on unchanged rows and paths still gone;
/// 7. the same keys and marks as swe, and no DDL or schema change.
@Suite struct SearchMirrorTests {
    typealias H = MirrorHome

    // MARK: Goal

    @Test func aSessionTheIndexNeverReadIsFoundThroughItsCopyOnce() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "quokka migration plan")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan()
        #expect(try h.hits("quokka") == [H.claudeID])
        // Filed under the transcript's own path, never the copy's.
        #expect(try h.sessions() == [["claude", H.claudeID, ".claude/projects/-work-proj/\(H.claudeID).jsonl", "2"]])
        #expect(try h.files().allSatisfy { !$0.path.hasPrefix(".swe/data/transcripts") })
        #expect(try h.marks() == [".claude/projects/-work-proj/\(H.claudeID).jsonl":
                                  "claude \(try h.store.fileRow(for: source.path)?.sessionRowID ?? -1)"])
    }

    @Test func aPrunedCodexRolloutIsFoundThroughItsCopy() async throws {
        let h = try H()
        let source = try h.codex(H.stem, "wombat dashboards")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan()
        #expect(try h.hits("wombat").count == 1)
        #expect(try h.sessions(agent: "codex").map { $0[2] } == [h.rel(source.path)])
    }

    @Test func aSecondPassReadsNoCopy() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "quokka")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        let first = try await h.scan()
        let before = try h.snapshot()
        let second = try await h.scan()
        #expect(first.filesIndexed == 1)
        #expect(second.filesIndexed == 0)
        #expect(try h.snapshot() == before)
    }

    // MARK: 1. Never worse than no mirror

    /// Two homes with the same live transcripts: one with the mirror in
    /// `state`, one with none. Their indexes must match row for row.
    private func liveMatchesNoMirror(_ state: (H) throws -> Void) async throws {
        func live(_ h: H) throws {
            let claude = try h.claude(H.claudeID, "fix the login bug")
            try h.grow(claude, H.claudeID, "and the logout", mtime: H.mtime + 50)
            try h.codex(H.stem, "add dark mode")
        }
        let plain = try H()
        try live(plain)
        try await plain.scan(withMirror: false)
        let mirrored = try H()
        try live(mirrored)
        try state(mirrored)
        try await mirrored.scan()
        try await mirrored.scan()
        #expect(try mirrored.snapshot() == plain.snapshot())
        #expect(try mirrored.hits("logout") == plain.hits("logout"))
    }

    @Test func liveSessionsSearchAsWithNoMirror() async throws {
        try await liveMatchesNoMirror { try $0.mirrorPass() }
    }

    @Test func liveSessionsSearchAsWithNoMirrorWhenTheManifestIsLost() async throws {
        try await liveMatchesNoMirror { h in try h.mirrorPass(); try h.lose(.missing) }
        try await liveMatchesNoMirror { h in try h.mirrorPass(); try h.lose(.corrupt) }
    }

    @Test func aStaleCopyChangesNoLiveSession() async throws {
        try await liveMatchesNoMirror { h in
            try h.mirrorPass()
            // The copy predates the growth: older and shorter.
            let source = h.claudeHome.appending(path: "projects/-work-proj/\(H.claudeID).jsonl").path
            let stale = h.temp.path("stale.jsonl")
            try h.write(H.claudeLines(H.claudeID, ["fix the login bug"]), to: stale, mtime: H.mtime - 100)
            try h.copy(stale.path, rel: "claude/-work-proj/\(H.claudeID).jsonl", harness: "claude", source: source)
            try h.saveManifest()
        }
    }

    @Test func aNewerLongerCopyInAForeignFolderChangesNoLiveSession() async throws {
        try await liveMatchesNoMirror { h in
            try h.mirrorPass()
            // The same session id in another project folder whose source is
            // gone, newer and longer than the live file: a copy never
            // speaks for a session a live file carries.
            let big = h.temp.path("big.jsonl")
            try h.write(H.claudeLines(H.claudeID, ["fix the login bug", "and the logout", "zebra"]),
                        to: big, mtime: H.mtime + 500)
            let gone = h.claudeHome.appending(path: "projects/-elsewhere/\(H.claudeID).jsonl").path
            try h.copy(big.path, rel: "claude/-elsewhere/\(H.claudeID).jsonl", harness: "claude", source: gone)
            try h.saveManifest()
        }
        try await liveMatchesNoMirror { h in
            try h.mirrorPass()
            try h.lose(.missing)
            let big = h.temp.path("big.jsonl")
            try h.write(H.claudeLines(H.claudeID, ["zebra", "and more"]), to: big, mtime: H.mtime + 500)
            try h.copy(big.path, rel: "claude/-elsewhere/\(H.claudeID).jsonl", harness: "claude", source: nil)
        }
    }

    /// A live file the index holds no session for (nothing in it decoded
    /// yet) still carries its session: a newer, longer copy of it in
    /// another folder is never read.
    @Test func aLiveFileWithNoIndexedSessionStillShadowsItsCopies() async throws {
        let h = try H()
        let live = try h.write(H.line(["type": "file-history-snapshot"]),
                               to: h.claudeHome.appending(path: "projects/-b/\(H.claudeID).jsonl"))
        let big = h.temp.path("big.jsonl")
        try h.write(H.claudeLines(H.claudeID, ["cormorant"]), to: big, mtime: H.mtime + 500)
        try h.copy(big.path, rel: "claude/-a/\(H.claudeID).jsonl", harness: "claude",
                   source: h.claudeHome.appending(path: "projects/-a/\(H.claudeID).jsonl").path)
        try h.saveManifest()
        try await h.scan()
        #expect(try h.hits("cormorant").isEmpty)
        #expect(try h.files().map(\.path) == [h.rel(live.path)])
    }

    /// A session live only in an account home no profile names still
    /// carries its session. The scan never reads that home, but swe's
    /// pass lists every `accounts/<vendor>/*/home` on disk, so the copy
    /// from a home the scan reads is never read by either writer.
    @Test func aSessionLiveOnlyInAnUnregisteredAccountHomeIsNeverReadFromItsCopy() async throws {
        let h = try H()
        let gone = try h.claude(H.claudeID, "kestrel", folder: "-a")
        // A session live nowhere is still found through its copy.
        let pruned = try h.claude(H.otherID, "osprey", folder: "-a")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: gone)
        try FileManager.default.removeItem(at: pruned)
        try h.write(H.claudeLines(H.claudeID, ["kestrel"]),
                    to: h.account("claude", "spare").appending(path: "projects/-b/\(H.claudeID).jsonl"))
        try await h.scan()
        #expect(try h.hits("kestrel").isEmpty)
        #expect(try h.hits("osprey") == [H.otherID])
        #expect(try h.files().map(\.path) == [h.rel(pruned.path)])
    }

    /// The same through a linked account folder, which swe's glob follows.
    @Test func aRolloutLiveOnlyInALinkedUnregisteredAccountHomeIsNeverReadFromItsCopy() async throws {
        let h = try H()
        let gone = try h.codex(H.stem, "kestrel")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: gone)
        let elsewhere = h.temp.path("elsewhere")
        try h.write(H.codexLines(H.thread, ["kestrel"]),
                    to: elsewhere.appending(path: "home/sessions/2026/09/03/\(H.laterStem).jsonl"))
        try FileManager.default.createDirectory(at: h.accountsRoot.appending(path: "codex"),
                                                withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: h.accountsRoot.appending(path: "codex/linked"),
                                                   withDestinationURL: elsewhere)
        try await h.scan()
        #expect(try h.hits("kestrel").isEmpty)
        #expect(try h.files().isEmpty)
    }

    // MARK: 2. One session, one set of rows

    @Test func aRolloutMirroredThenArchivedBeforeAnyPassIsOneSession() async throws {
        let h = try H()
        let dated = try h.codex(H.stem, "otter archive")
        try h.mirrorPass()
        // Codex archives the rollout flat; the dated source is gone.
        let archived = h.codexHome.appending(path: "archived_sessions/\(H.stem).jsonl")
        try FileManager.default.createDirectory(at: archived.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try FileManager.default.moveItem(at: dated, to: archived)
        try await h.scan()
        try await h.scan()
        #expect(try h.sessions(agent: "codex").map { $0[2] } == [h.rel(archived.path)])
        #expect(try h.hits("otter").count == 1)
    }

    @Test func aClaudeSessionInTwoFoldersBothPrunedIsOneSessionTheNewest() async throws {
        let h = try H()
        let first = try h.claude(H.claudeID, "pelican", folder: "-a", mtime: H.mtime)
        let second = try h.claude(H.claudeID, "pelican", "puffin", folder: "-b", mtime: H.mtime + 100)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: first)
        try FileManager.default.removeItem(at: second)
        try await h.scan()
        try await h.scan()
        #expect(try h.sessions().map { $0[2] } == [h.rel(second.path)])
        #expect(try h.hits("puffin") == [H.claudeID])
    }

    @Test func aSessionLiveInOneFolderIsNeverReadFromItsCopyInAnother() async throws {
        let h = try H()
        let gone = try h.claude(H.claudeID, "heron", folder: "-a", mtime: H.mtime + 100)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: gone)
        let live = try h.claude(H.claudeID, "heron", folder: "-b", mtime: H.mtime)
        try await h.scan()
        #expect(try h.sessions().map { $0[2] } == [h.rel(live.path)])
    }

    @Test func aThreadWithSeveralPrunedRolloutsIsOneSession() async throws {
        let h = try H()
        let first = try h.codex(H.stem, "first rollout", mtime: H.mtime)
        let later = try h.codex(H.laterStem, "first rollout", "resumed lynx", under: "sessions/2026/09/03",
                                mtime: H.mtime + 100)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: first)
        try FileManager.default.removeItem(at: later)
        try await h.scan()
        #expect(try h.sessions(agent: "codex").map { $0[2] } == [h.rel(later.path)])
        #expect(try h.hits("lynx").count == 1)
    }

    @Test func aSessionBackAtAnotherPathAndPrunedAgainCountsOnce() async throws {
        let h = try H()
        let a = try h.claude(H.claudeID, "ibis", folder: "-a", mtime: H.mtime)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: a)
        try await h.scan()  // filed under -a from its copy
        // The session comes back in -b, is read live, then pruned too.
        let b = try h.claude(H.claudeID, "ibis", "egret", folder: "-b", mtime: H.mtime + 100)
        try await h.scan()
        // The live read retired the copy row under -a in its own commit.
        #expect(try h.sessions().map { $0[2] } == [h.rel(b.path)])
        try h.mirrorPass()
        try FileManager.default.removeItem(at: b)
        try await h.scan()
        try await h.scan()
        #expect(try h.sessions().map { $0[2] } == [h.rel(b.path)])
        #expect(try h.hits("ibis") == [H.claudeID])
    }

    // MARK: 3. Nothing lost

    @Test func losingTheManifestRemovesNoRowAndNoCopy() async throws {
        for loss in [H.Loss.missing, .corrupt] {
            let h = try H()
            let source = try h.claude(H.claudeID, "quokka")
            try h.codex(H.stem, "wombat")
            try h.mirrorPass()
            try FileManager.default.removeItem(at: source)
            try await h.scan()
            let before = try h.snapshot()
            let copies = h.copies()
            try h.lose(loss)
            try await h.scan()
            try await h.scan()
            #expect(try h.snapshot() == before)
            #expect(h.copies() == copies)
            #expect(try h.hits("quokka") == [H.claudeID])
        }
    }

    @Test func aCopyMissingFromTheManifestIsStillFound() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "manatee")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try h.lose(.missing)
        try await h.scan()
        #expect(try h.hits("manatee") == [H.claudeID])
        #expect(try h.sessions().map { $0[2] } == [h.rel(source.path)])
    }

    @Test func anOlderOrShorterCopyNeverReplacesARow() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "first words", mtime: H.mtime)
        try h.mirrorPass()  // the copy holds only "first words"
        try h.grow(source, H.claudeID, "later stingray", mtime: H.mtime + 100)
        try await h.scan()  // the row holds both
        try FileManager.default.removeItem(at: source)
        let before = try h.snapshot()
        try await h.scan()
        #expect(try h.snapshot() == before)
        #expect(try h.hits("stingray") == [H.claudeID])
    }

    @Test func aNewerButShorterSiblingChangesNothing() async throws {
        let h = try H()
        let a = try h.claude(H.claudeID, "long", "history", "kept", folder: "-a", mtime: H.mtime)
        try await h.scan(withMirror: false)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: a)
        // A copy in another folder: newer, but shorter than the row.
        let short = h.temp.path("short.jsonl")
        try h.write(H.claudeLines(H.claudeID, ["x"]), to: short, mtime: H.mtime + 100)
        try h.copy(short.path, rel: "claude/-b/\(H.claudeID).jsonl", harness: "claude",
                   source: h.claudeHome.appending(path: "projects/-b/\(H.claudeID).jsonl").path)
        try h.saveManifest()
        let before = try h.snapshot()
        try await h.scan()
        #expect(try h.snapshot() == before)
    }

    /// The copy of the row's own path is newer but shorter (the source was
    /// rewritten before swe copied it): it stands for nothing it lost.
    @Test func aNewerButShorterCopyOfTheSamePathChangesNothing() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "long", "history", "kept", mtime: H.mtime)
        try await h.scan()
        let short = h.temp.path("short.jsonl")
        try h.write(H.claudeLines(H.claudeID, ["x"]), to: short, mtime: H.mtime + 100)
        try h.copy(short.path, rel: "claude/-work-proj/\(H.claudeID).jsonl", harness: "claude", source: source.path)
        try h.saveManifest()
        try FileManager.default.removeItem(at: source)
        let before = try h.snapshot()
        try await h.scan()
        #expect(try h.snapshot() == before)
        #expect(try h.hits("history") == [H.claudeID])
    }

    @Test func twoFilesNamedWithoutASessionIdAreNeverMerged() async throws {
        let h = try H()
        let notes = try h.write(H.claudeLines("notes", ["kingfisher"]),
                                to: h.claudeHome.appending(path: "projects/-a/notes.jsonl"))
        let other = try h.write(H.claudeLines("notes", ["kestrel"]),
                                to: h.claudeHome.appending(path: "projects/-b/notes.jsonl"), mtime: H.mtime + 10)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: notes)
        try FileManager.default.removeItem(at: other)
        try await h.scan()
        #expect(Set(try h.sessions().map { $0[2] }) == [h.rel(notes.path), h.rel(other.path)])
        #expect(try h.hits("kingfisher").count == 1)
        #expect(try h.hits("kestrel").count == 1)
    }

    @Test func anExternalCopyWithNoKnownSourceIsLeftAlone() async throws {
        let h = try H()
        let outside = h.temp.path("outside.jsonl")
        try h.write(H.claudeLines(H.claudeID, ["albatross"]), to: outside)
        // swe's layout for a linked transcript outside the roots: its
        // folder is a hash, so a lost manifest leaves no source to name.
        try h.copy(outside.path, rel: "claude/-external-0123456789ab/\(H.claudeID).jsonl",
                   harness: "claude", source: nil)
        try await h.scan()
        #expect(try h.files().isEmpty)
    }

    // MARK: 4. Newer wins

    @Test func aCopyHoldingRepliesTheIndexLacksWins() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "first question", mtime: H.mtime)
        try await h.scan()
        // Grown while indexing was off, mirrored, then pruned.
        try h.grow(source, H.claudeID, "then the narwhal rollout", mtime: H.mtime + 100)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan()
        #expect(try h.hits("narwhal") == [H.claudeID])
        #expect(try h.hits("first") == [H.claudeID])
        #expect(try h.sessions().count == 1)
    }

    @Test func theNewestOfTwoFoldersWins() async throws {
        let h = try H()
        let a = try h.claude(H.claudeID, "salmon", folder: "-a", mtime: H.mtime)
        try await h.scan()
        let b = try h.claude(H.claudeID, "salmon", "trout", folder: "-b", mtime: H.mtime + 100)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: a)
        try FileManager.default.removeItem(at: b)
        try await h.scan()
        try await h.scan()
        #expect(try h.sessions().map { $0[2] } == [h.rel(b.path)])
        #expect(try h.hits("trout") == [H.claudeID])
    }

    // MARK: 5. Staged reads

    /// A copy read in pieces over an indexed row: after every piece the
    /// index still shows one session for it, holding the old text.
    @Test func aStagedReadShowsOneCompleteSessionAfterEveryPiece() async throws {
        // The old row's last word: a prefix of the copy never holds it, so
        // a piece shown in place of the row would lose it.
        let old = ["the first old words", "more old words", "then the capybara"]
        let prompts = (0..<6).map { "piece \($0) of the gecko story" }
        let h = try H(bufferBudget: 40)
        let source = try h.write(H.claudeLines(H.claudeID, old),
                                 to: h.claudeHome.appending(path: "projects/-work-proj/\(H.claudeID).jsonl"))
        await h.ledger.setIndexer(SearchIndexer(store: h.store))
        try await h.ledger.scan(roots: h.roots())
        #expect(try h.hits("capybara") == [H.claudeID])
        let grown = h.temp.path("grown.jsonl")
        try h.write(H.claudeLines(H.claudeID, old + prompts), to: grown, mtime: H.mtime + 100)
        try FileManager.default.removeItem(at: source)
        try h.copy(grown.path, rel: "claude/-work-proj/\(H.claudeID).jsonl", harness: "claude", source: source.path)
        try h.saveManifest()
        var seen: [Int] = []
        h.indexer.afterPiece = {
            seen.append((try? h.sessions().count) ?? -1)
            #expect((try? h.hits("capybara")) == [H.claudeID])
        }
        try await h.scan()
        #expect(!seen.isEmpty)
        #expect(seen.allSatisfy { $0 == 1 })
        #expect(try h.sessions().count == 1)
        #expect(try h.hits("gecko") == [H.claudeID])
        #expect(try h.hits("capybara") == [H.claudeID])
    }

    /// A second writer appends to the host between two pieces and moves
    /// its row: the read stops, and its cleanup removes only its own ids.
    @Test func aStagedReadThatLosesItsFenceTakesBackOnlyItsOwnMessages() async throws {
        let h = try H(bufferBudget: 40)
        let source = try h.claude(H.claudeID, "old capybara words", mtime: H.mtime)
        try await h.scan()
        let grown = h.temp.path("grown.jsonl")
        try h.write(H.claudeLines(H.claudeID, ["old capybara words"] + (0..<6).map { "gecko \($0)" }),
                    to: grown, mtime: H.mtime + 100)
        try FileManager.default.removeItem(at: source)
        try h.copy(grown.path, rel: "claude/-work-proj/\(H.claudeID).jsonl", harness: "claude", source: source.path)
        try h.saveManifest()
        let other = try SearchStore(directory: h.store.directory)
        var foreign: Int64?
        h.indexer.afterPiece = {
            guard foreign == nil, let row = try? other.fileRow(for: source.path),
                  let host = row.sessionRowID else { return }
            // swe appends a message to the host session and moves its row.
            _ = try? other.commitFile(
                path: source.path, expected: SearchFileFence(sessionRowID: host, byteOffset: row.byteOffset),
                stat: SearchFileStat(mtimeMs: row.mtimeMs + 1, sizeBytes: (row.sizeBytes ?? 0) + 1),
                byteOffset: row.byteOffset + 1, session: .existing(host),
                messages: [SearchMessageRow(role: "user", userText: "walrus from swe")])
            foreign = try? other.db.scalar("SELECT max(id) FROM messages")
        }
        try await h.scan()
        let id = try #require(foreign)
        // The foreign message survives on its session, and searches.
        #expect(try h.store.db.scalar("SELECT count(*) FROM messages WHERE id = \(id)") == 1)
        #expect(try h.hits("walrus") == [H.claudeID])
        // None of the read's own pieces stayed behind.
        #expect(try h.hits("gecko").isEmpty)
        #expect(try h.sessions().count == 1)
    }

    /// A second writer adds a message to the host without moving any row:
    /// the final switch moves only the read's own ids, never that one.
    @Test func theSwitchMovesOnlyTheExactIdsTheReadWrote() async throws {
        let h = try H(bufferBudget: 40)
        let source = try h.claude(H.claudeID, "old capybara words", mtime: H.mtime)
        try await h.scan()
        let grown = h.temp.path("grown.jsonl")
        try h.write(H.claudeLines(H.claudeID, ["old capybara words"] + (0..<6).map { "gecko \($0)" }),
                    to: grown, mtime: H.mtime + 100)
        try FileManager.default.removeItem(at: source)
        try h.copy(grown.path, rel: "claude/-work-proj/\(H.claudeID).jsonl", harness: "claude", source: source.path)
        try h.saveManifest()
        let other = try SearchStore(directory: h.store.directory)
        var foreign: (id: Int64, host: Int64)?
        h.indexer.afterPiece = {
            guard foreign == nil, let host = try? other.fileRow(for: source.path)?.sessionRowID else { return }
            try? other.db.execute("INSERT INTO messages(session_row_id, role, ts) VALUES (\(host), 'user', NULL)")
            if let id = try? other.db.scalar("SELECT max(id) FROM messages") { foreign = (id, host) }
        }
        try await h.scan()
        let f = try #require(foreign)
        let switched = try #require(try h.store.fileRow(for: source.path)?.sessionRowID)
        #expect(switched != f.host)
        #expect(try h.store.db.scalar(
            "SELECT count(*) FROM messages WHERE id = \(f.id) AND session_row_id = \(switched)") == 0)
        #expect(try h.hits("gecko") == [H.claudeID])
        #expect(try h.sessions().count == 1)
    }

    /// A live whole read that retires a copy row stages its pieces on the
    /// copy's session. When the scan stops for a reason of its own (the
    /// ledger's store failing, so `scanFile` rethrows) or another read
    /// takes the slot, the pieces go, as swe's `_read` takes them back in
    /// its `finally`: left behind they would repeat text on the old
    /// session, where no drain reaches them.
    @Test func anAbandonedStagedReadTakesBackItsPieces() async throws {
        let h = try H(bufferBudget: 40)
        let a = try h.claude(H.claudeID, "ibis", folder: "-a")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: a)
        try await h.scan()
        let marks = try h.store.copyMarks()
        let b = try h.claude(H.claudeID, "ibis", "pangolin one", "pangolin two", "pangolin three",
                             folder: "-b", mtime: H.mtime + 100)
        let before = try h.snapshot()
        for leave in [{ (i: SearchIndexer) in i.abandonFile(path: b.path) },
                      { (i: SearchIndexer) in i.beginFile(path: "/elsewhere/x.jsonl", harness: .claude) }] {
            let drop = SearchMirror.copiedRows(store: h.store, marks: marks, harness: "claude", path: b.path)
            #expect(drop.count == 1)
            h.indexer.beginFile(path: b.path, harness: .claude, drop: drop)
            try JSONLReader().read(b, from: 0) { h.indexer.consume(line: $0) }
            #expect(h.indexer.midReadFlushes > 0)
            leave(h.indexer)
            #expect(try h.hits("pangolin").isEmpty)
            #expect(try h.snapshot() == before)
        }
    }

    /// A copy the index cannot read says so in the scan's notes, as a
    /// failed live read does, instead of leaving the session silently
    /// missing from search.
    @Test func aFailedCopyReadIsAScanNote() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "quokka")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        let copy = h.mirrorRoot.appending(path: "claude/-work-proj/\(H.claudeID).jsonl")
        try FileManager.default.setAttributes([.posixPermissions: 0], ofItemAtPath: copy.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: copy.path) }
        let report = try await h.scan()
        #expect(report.notes.contains { $0.hasPrefix("Index failed ") && $0.contains(copy.lastPathComponent)
            && $0.contains("for \(source.path)") })
        #expect(try h.hits("quokka").isEmpty)
    }

    /// The source comes back mid read: no piece and no switch commits.
    @Test func aSourceRestoredMidReadStopsTheRead() async throws {
        let h = try H(bufferBudget: 40)
        let source = try h.claude(H.claudeID, "old capybara words", mtime: H.mtime)
        try await h.scan()
        let original = try Data(contentsOf: source)
        let grown = h.temp.path("grown.jsonl")
        try h.write(H.claudeLines(H.claudeID, ["old capybara words"] + (0..<6).map { "gecko \($0)" }),
                    to: grown, mtime: H.mtime + 100)
        try FileManager.default.removeItem(at: source)
        try h.copy(grown.path, rel: "claude/-work-proj/\(H.claudeID).jsonl", harness: "claude", source: source.path)
        try h.saveManifest()
        var restored = false
        h.indexer.afterPiece = {
            guard !restored else { return }
            restored = true
            try? original.write(to: source)
        }
        try await h.scan(withMirror: true)
        #expect(restored)
        #expect(try h.hits("gecko").isEmpty)
        #expect(try h.hits("capybara") == [H.claudeID])
    }

    // MARK: 6. Fenced deletes

    @Test func retireRefusesARowThatMovedOrAPathThatCameBack() async throws {
        let h = try H()
        let a = try h.claude(H.claudeID, "x", folder: "-a", mtime: H.mtime)
        let b = try h.claude(H.claudeID, "x", "y", folder: "-b", mtime: H.mtime + 10)
        try await h.scan(withMirror: false)
        let keep = try #require(try h.store.fileRow(for: b.path))
        var spare = try #require(try h.store.fileRow(for: a.path))
        // The spare's path is still on disk: nothing goes.
        #expect(try h.store.retire(keep: keep, rows: [spare]) == false)
        try FileManager.default.removeItem(at: a)
        // A row that moved since it was read: nothing goes.
        spare.mtimeMs -= 1
        #expect(try h.store.retire(keep: keep, rows: [spare]) == false)
        #expect(try h.sessions().count == 2)
        spare.mtimeMs += 1
        #expect(try h.store.retire(keep: keep, rows: [spare]) == true)
        #expect(try h.sessions().map { $0[2] } == [h.rel(b.path)])
    }

    @Test func aCommitThatDropsAChangedRowCommitsNothing() async throws {
        let h = try H()
        let a = try h.claude(H.claudeID, "x", folder: "-a")
        try await h.scan(withMirror: false)
        var row = try #require(try h.store.fileRow(for: a.path))
        try FileManager.default.removeItem(at: a)
        row.byteOffset += 1
        let committed = try h.store.commitFile(
            path: "/nowhere/new.jsonl", expected: nil, stat: SearchFileStat(mtimeMs: 1, sizeBytes: 1),
            byteOffset: 1, session: .fresh(SearchSessionFields(agent: "claude", filePath: "/nowhere/new.jsonl")),
            messages: [], mark: nil, drop: [row])
        #expect(committed == false)
        #expect(try h.store.fileRow(for: "/nowhere/new.jsonl") == nil)
        #expect(try h.sessions().count == 1)
    }

    // MARK: 7. Keys, marks and the live side

    @Test func aLiveReadRetiresTheCopyRowsOfItsSessionInTheSameCommit() async throws {
        let h = try H()
        let a = try h.claude(H.claudeID, "ibis", folder: "-a")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: a)
        try await h.scan()
        #expect(try h.marks().keys.contains(h.rel(a.path)))
        let b = try h.claude(H.claudeID, "ibis", "egret", folder: "-b", mtime: H.mtime + 100)
        try await h.scan()
        #expect(try h.sessions().map { $0[2] } == [h.rel(b.path)])
        #expect(try h.marks().isEmpty)
    }

    @Test func aRowReadFromALiveFileIsNeverRetiredByALiveSibling() async throws {
        let h = try H()
        let a = try h.claude(H.claudeID, "ibis", folder: "-a")
        try await h.scan(withMirror: false)  // read live, so no mark
        try FileManager.default.removeItem(at: a)
        try h.claude(H.claudeID, "ibis", "egret", folder: "-b", mtime: H.mtime + 100)
        try await h.scan()
        #expect(try h.sessions().count == 2)
    }

    @Test func aRestoredTranscriptReadsAsTheSameRowAndClearsTheMark() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "toucan")
        let bytes = try Data(contentsOf: source)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan()
        let row = try #require(try h.store.fileRow(for: source.path))
        // Restored with the same bytes and mtime: current, no new session.
        try bytes.write(to: source)
        try h.setMtime(H.mtime, source)
        try await h.scan()
        #expect(try h.store.fileRow(for: source.path)?.sessionRowID == row.sessionRowID)
        #expect(try h.sessions().count == 1)
        // Grown after the restore: replaced in place, and the row is the
        // file's own now.
        try h.grow(source, H.claudeID, "and the hornbill", mtime: H.mtime + 100)
        try await h.scan()
        #expect(try h.sessions().count == 1)
        #expect(try h.hits("hornbill") == [H.claudeID])
        #expect(try h.marks().isEmpty)
    }

    @Test func aMirrorBlindScanLeavesCopyRowsAlone() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "quokka")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan()
        let before = try h.snapshot()
        let marks = try h.marks()
        try await h.scan(withMirror: false)
        #expect(try h.snapshot() == before)
        #expect(try h.marks() == marks)
    }

    @Test func theCopyPassAddsNoTableAndKeepsTheSchemaVersion() async throws {
        let h = try H()
        let schema = { try h.store.db.prepare("SELECT name FROM sqlite_master ORDER BY name") }
        func names() throws -> [String] {
            let statement = try schema()
            var out: [String] = []
            while try statement.step() { out.append(statement.text(0) ?? "") }
            return out
        }
        let before = try names()
        let source = try h.claude(H.claudeID, "quokka")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan()
        #expect(try names() == before)
        #expect(try h.store.meta("schema_version") == "1")
    }

    @Test func aFullScanOnlyReadsCopies() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "quokka")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        await h.ledger.setIndexer(h.indexer)
        try await h.ledger.scan(roots: h.roots(), recentLimit: 10)
        #expect(try h.files().isEmpty)
    }
}
