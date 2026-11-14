import Foundation
import Testing
@testable import SWECore

/// `Ledger.scan` and swe's transcript mirror (#1918). Usage is never
/// double counted and never lost: events recorded for a session stay after
/// the harness prunes its transcript, a copy never adds an event a second
/// time, and a session the ledger never read before it was pruned is read
/// once from its copy.
@Suite struct LedgerMirrorTests {
    typealias H = MirrorHome

    /// The totals a scan of the same transcripts, all live, gives.
    private func reference(_ build: (H) async throws -> Void) async throws -> UsageTotals {
        let h = try H()
        try await build(h)
        try await h.scan(withMirror: false, index: false)
        return await h.ledger.totals(in: allTime)
    }

    @Test func eventsOfAPrunedSessionStayAndItsCopyAddsNone() async throws {
        let expected = try await reference { try $0.claude(H.claudeID, "a", "b", usage: true) }
        let h = try H()
        let source = try h.claude(H.claudeID, "a", "b", usage: true)
        try await h.scan(index: false)
        #expect(await h.ledger.totals(in: allTime) == expected)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        let report = try await h.scan(index: false)
        #expect(await h.ledger.totals(in: allTime) == expected)
        // The source's cursor covers the copy, so it is not even read.
        #expect(report.filesRead == 0)
        #expect(report.eventsWritten == 0)
    }

    @Test func aSessionPrunedBeforeTheLedgerReadItIsReadOnceFromItsCopy() async throws {
        let expected = try await reference { try $0.claude(H.claudeID, "a", "b", "c", usage: true) }
        let h = try H()
        let source = try h.claude(H.claudeID, "a", "b", "c", usage: true)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        let first = try await h.scan(index: false)
        #expect(first.filesRead == 1)
        #expect(await h.ledger.totals(in: allTime) == expected)
        let second = try await h.scan(index: false)
        #expect(second.filesRead == 0)
        #expect(second.eventsWritten == 0)
        #expect(await h.ledger.totals(in: allTime) == expected)
        // Filed under the harness home, as a live scan files it, so the
        // Sessions page joins it to the mirrored row.
        let usage = await h.ledger.bySession(in: allTime, harness: "claude")
        #expect(usage.map(\.accountHome) == [h.claudeHome.path])
        #expect(usage.map(\.sessionId) == [H.claudeID])
    }

    @Test func aCopyHoldingEventsTheLedgerLacksAddsOnlyThose() async throws {
        let expected = try await reference { h in
            let url = try h.claude(H.claudeID, "a", usage: true)
            try h.grow(url, H.claudeID, "later", mtime: H.mtime + 100, usage: true)
        }
        let h = try H()
        let source = try h.claude(H.claudeID, "a", usage: true)
        try await h.scan(index: false)
        // Grown while the app was closed, mirrored by swe, then pruned.
        try h.grow(source, H.claudeID, "later", mtime: H.mtime + 100, usage: true)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan(index: false)
        #expect(await h.ledger.totals(in: allTime) == expected)
        try await h.scan(index: false)
        #expect(await h.ledger.totals(in: allTime) == expected)
    }

    /// The harness rewrote the transcript after the ledger read it (new
    /// usage, a shorter file), swe mirrored the rewrite, then the harness
    /// pruned it. The source's cursor reads past the copy's size, which
    /// proves nothing about its bytes: the copy is read, and its new event
    /// counts once.
    @Test func aRewrittenThenPrunedTranscriptKeepsItsNewUsage() async throws {
        func rewrite(_ h: H, _ url: URL) throws {
            try h.write(H.claudeLines(H.claudeID, ["x"], start: 5, usage: true), to: url, mtime: H.mtime + 100)
        }
        let expected = try await reference { h in
            let url = try h.claude(H.claudeID, "a", "b", usage: true)
            try await h.scan(withMirror: false, index: false)
            try rewrite(h, url)
        }
        let h = try H()
        let source = try h.claude(H.claudeID, "a", "b", usage: true)
        try await h.scan(index: false)
        try rewrite(h, source)
        #expect(try Data(contentsOf: source).count < H.claudeLines(H.claudeID, ["a", "b"], usage: true).utf8.count)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan(index: false)
        #expect(await h.ledger.totals(in: allTime) == expected)
        #expect(await h.ledger.totals(in: allTime).eventCount == 3)
        try await h.scan(index: false)
        #expect(await h.ledger.totals(in: allTime) == expected)
    }

    @Test func aCopyOfALiveSessionIsNeverRead() async throws {
        let expected = try await reference {
            try $0.claude(H.claudeID, "a", folder: "-live", mtime: H.mtime, usage: true)
        }
        let h = try H()
        try h.claude(H.claudeID, "a", folder: "-live", mtime: H.mtime, usage: true)
        let other = h.temp.path("other.jsonl")
        try h.write(H.claudeLines(H.claudeID, ["a", "b"], usage: true), to: other, mtime: H.mtime + 100)
        try h.copy(other.path, rel: "claude/-gone/\(H.claudeID).jsonl", harness: "claude",
                   source: h.claudeHome.appending(path: "projects/-gone/\(H.claudeID).jsonl").path)
        try h.saveManifest()
        try await h.scan(index: false)
        // The copy's second event has a key of its own: read, it would count.
        #expect(await h.ledger.totals(in: allTime) == expected)
    }

    /// A session live only in an account home no profile names: the scan
    /// reads no usage there, and its copy is not read in its place.
    @Test func aCopyOfASessionLiveOnlyInAnUnregisteredAccountHomeIsNeverRead() async throws {
        let h = try H()
        let gone = try h.claude(H.claudeID, "a", "b", folder: "-gone", usage: true)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: gone)
        try h.write(H.claudeLines(H.claudeID, ["a", "b"], usage: true),
                    to: h.account("claude", "spare").appending(path: "projects/-p/\(H.claudeID).jsonl"))
        try await h.scan(index: false)
        #expect(await h.ledger.totals(in: allTime).eventCount == 0)
    }

    @Test func aLostManifestStillFindsTheCopyAndCountsItOnce() async throws {
        let expected = try await reference { try $0.claude(H.claudeID, "a", "b", usage: true) }
        for loss in [H.Loss.missing, .corrupt] {
            let h = try H()
            let source = try h.claude(H.claudeID, "a", "b", usage: true)
            try h.mirrorPass()
            try FileManager.default.removeItem(at: source)
            try h.lose(loss)
            try await h.scan(index: false)
            try await h.scan(index: false)
            #expect(await h.ledger.totals(in: allTime) == expected)
            #expect(await h.ledger.totals(in: allTime).eventCount == 2)
        }
    }

    @Test func aMirrorBlindScanReadsNoCopy() async throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "a", usage: true)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        try await h.scan(withMirror: false, index: false)
        #expect(await h.ledger.totals(in: allTime).eventCount == 0)
    }

    @Test func anAccountCopyIsFiledUnderItsAccountHome() async throws {
        let h = try H()
        let account = h.home.appending(path: ".swe/accounts/claude/work/home")
        let source = try h.write(H.claudeLines(H.claudeID, ["a"], usage: true),
                                 to: account.appending(path: "projects/-p/\(H.claudeID).jsonl"))
        try h.copy(source.path, rel: "accounts/claude/work/-p/\(H.claudeID).jsonl", harness: "claude",
                   source: source.path)
        try h.saveManifest()
        try FileManager.default.removeItem(at: source)
        await h.ledger.setIndexer(nil)
        let mirror = TranscriptMirror.standard(root: h.mirrorRoot, claudeHomes: [h.claudeHome, account],
                                               codexHomes: [h.codexHome])
        try await h.ledger.scan(roots: LedgerRoots(claudeHomes: [h.claudeHome, account],
                                                   codexHomes: [h.codexHome], mirror: mirror))
        let usage = await h.ledger.bySession(in: allTime, harness: "claude")
        #expect(usage.map(\.accountHome) == [account.path])
    }
}
