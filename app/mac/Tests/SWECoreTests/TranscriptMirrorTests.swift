import Foundation
import Testing
@testable import SWECore

/// The read side of swe's transcript mirror (#1918): identity, the layout
/// inverse, the manifest's tolerance, the walk and the pruned-copy pick
/// that search and the ledger share. Mirrors swe's
/// `search_writer.transcript_identity`, `transcript_mirror.source_candidates`
/// and `_index_copies`'s candidate step.
@Suite struct TranscriptMirrorTests {
    typealias H = MirrorHome

    @Test func identityComesFromTheFileNameAlone() {
        let id = "AAAAAAAA-2222-3333-4444-555555555555"
        #expect(TranscriptMirror.identity(harness: "claude", path: "/x/-p/\(id).jsonl") == id.lowercased())
        // A Claude stem must be a whole uuid; anything else is its own file.
        #expect(TranscriptMirror.identity(harness: "claude", path: "/x/-p/agent-\(id).jsonl")
                == "/x/-p/agent-\(id).jsonl")
        #expect(TranscriptMirror.identity(harness: "claude", path: "/x/notes.jsonl") == "/x/notes.jsonl")
        // A Codex rollout's first uuid is its thread, whatever follows.
        #expect(TranscriptMirror.identity(
            harness: "codex", path: "/c/rollout-2026-09-01T10-00-00-\(H.thread)_\(id).jsonl") == H.thread)
        #expect(TranscriptMirror.identity(harness: "codex", path: "/c/odd.jsonl") == "/c/odd.jsonl")
    }

    @Test func theLayoutNamesWhereACopyCameFrom() throws {
        let h = try H()
        let m = h.mirror
        let projects = h.claudeHome.appending(path: "projects").resolvingSymlinksInPath().path
        let sessions = h.codexHome.appending(path: "sessions").resolvingSymlinksInPath().path
        let archived = h.codexHome.appending(path: "archived_sessions").resolvingSymlinksInPath().path
        #expect(m.sourceCandidates("claude/-p/x.jsonl") == ["\(projects)/-p/x.jsonl"])
        // Dated: sessions/ first, then archived_sessions/. Flat: only archived.
        #expect(m.sourceCandidates("codex/2026/09/01/r.jsonl")
                == ["\(sessions)/2026/09/01/r.jsonl", "\(archived)/2026/09/01/r.jsonl"])
        #expect(m.sourceCandidates("codex/r.jsonl") == ["\(archived)/r.jsonl"])
        // External copies are folders named by a hash: nothing to name.
        #expect(m.sourceCandidates("claude/-external-0123456789ab/x.jsonl").isEmpty)
        #expect(m.sourceCandidates("codex/external/0123456789ab/0/r.jsonl").isEmpty)
        // An account no current home has.
        #expect(m.sourceCandidates("accounts/claude/gone/-p/x.jsonl").isEmpty)
    }

    @Test func anAccountHomeMapsToItsOwnFolder() {
        let m = TranscriptMirror.standard(
            root: URL(fileURLWithPath: "/m"),
            claudeHomes: [URL(fileURLWithPath: "/u/.claude"),
                          URL(fileURLWithPath: "/u/.swe/accounts/claude/work/home")],
            codexHomes: [URL(fileURLWithPath: "/u/.codex")])
        #expect(m.homes.map(\.prefix) == ["claude", "accounts/claude/work", "codex"])
        #expect(m.home(for: "accounts/claude/work/-p/x.jsonl", harness: "claude")?.home.path
                == "/u/.swe/accounts/claude/work/home")
        #expect(m.home(for: "claude/-p/x.jsonl", harness: "claude")?.home.path == "/u/.claude")
    }

    @Test func aMissingOrMalformedManifestReadsAsEmpty() throws {
        let h = try H()
        #expect(h.mirror.manifest().isEmpty)
        try FileManager.default.createDirectory(at: h.mirrorRoot, withIntermediateDirectories: true)
        for text in ["{not json", "[]", #"{"files": []}"#, #"{"files": {"a": 3}}"#] {
            try Data(text.utf8).write(to: h.mirrorRoot.appending(path: "manifest.json"))
            #expect(h.mirror.manifest().isEmpty)
        }
    }

    @Test func theWalkPrunesHiddenNamesSubagentsAndLinkedFolders() throws {
        let h = try H()
        for rel in ["claude/-p/a.jsonl", "claude/-p/.hidden.jsonl", "claude/.dot/b.jsonl",
                    "claude/-p/subagents/c.jsonl", "claude/-p/d.jsonl.shrunk-1", "codex/2026/09/01/r.jsonl",
                    "accounts/claude/work/-q/e.jsonl"] {
            try h.write("{}\n", to: h.mirrorRoot.appending(path: rel))
        }
        try FileManager.default.createSymbolicLink(
            at: h.mirrorRoot.appending(path: "claude/linked"), withDestinationURL: h.mirrorRoot.appending(path: "codex"))
        #expect(h.mirror.copies().map(\.rel)
                == ["claude/-p/a.jsonl", "codex/2026/09/01/r.jsonl", "accounts/claude/work/-q/e.jsonl"])
    }

    /// The ledger reads subagent copies; search does not, so the walk
    /// keeps `subagents/` only when asked. Links stay unfollowed either way.
    @Test func theWalkKeepsSubagentsOnlyWhenAsked() throws {
        let h = try H()
        for rel in ["claude/-p/\(H.claudeID).jsonl", "claude/-p/\(H.claudeID)/subagents/agent-a1.jsonl",
                    "claude/-p/\(H.claudeID)/subagents/.hidden.jsonl",
                    "claude/-p/\(H.claudeID)/subagents/agent-a1.meta.json"] {
            try h.write("{}\n", to: h.mirrorRoot.appending(path: rel))
        }
        let elsewhere = h.temp.path("elsewhere")
        try h.write("{}\n", to: elsewhere.appending(path: "agent-b2.jsonl"))
        try FileManager.default.createDirectory(at: h.mirrorRoot.appending(path: "claude/-p/\(H.otherID)"),
                                                withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(
            at: h.mirrorRoot.appending(path: "claude/-p/\(H.otherID)/subagents"), withDestinationURL: elsewhere)
        #expect(h.mirror.copies().map(\.rel) == ["claude/-p/\(H.claudeID).jsonl"])
        let all = h.mirror.copies(subagents: true)
        #expect(all.map(\.rel) == ["claude/-p/\(H.claudeID).jsonl",
                                   "claude/-p/\(H.claudeID)/subagents/agent-a1.jsonl"])
        #expect(all.map(\.isSubagent) == [false, true])
    }

    @Test func aPrunedSubagentCopyIsACandidateOfItsOwnIdentityOnlyWhenAsked() throws {
        let h = try H()
        let session = try h.claude(H.claudeID, "a")
        let sub = try h.subagent(of: session, "agent-a1")
        try h.mirrorPass()
        try h.prune(session)
        #expect(h.mirror.pruned(live: []).map(\.source) == [session.path])
        let all = h.mirror.pruned(live: [], subagents: true)
        #expect(all.map(\.source) == [session.path, sub.path])
        #expect(all.last?.identity == TranscriptMirror.SessionIdentity(harness: "claude", id: sub.path))
        // A live subagent file is never stood for by its copy.
        try h.write("{}\n", to: sub)
        #expect(h.mirror.pruned(live: [], subagents: true).map(\.source) == [session.path])
    }

    /// swe's `_copy_trees`: a tree behind a link reads as absent, so one
    /// account's copies are never filed twice.
    @Test func aLinkedAccountsFolderIsNotRead() throws {
        let h = try H()
        let elsewhere = h.temp.path("elsewhere")
        try h.write("{}\n", to: elsewhere.appending(path: "claude/work/-q/e.jsonl"))
        try h.write("{}\n", to: h.mirrorRoot.appending(path: "claude/-p/a.jsonl"))
        try FileManager.default.createSymbolicLink(at: h.mirrorRoot.appending(path: "accounts"),
                                                   withDestinationURL: elsewhere)
        #expect(h.mirror.copies().map(\.rel) == ["claude/-p/a.jsonl"])
    }

    @Test func onlyCopiesOfGoneSourcesNoLiveFileCarriesArePruned() throws {
        let h = try H()
        let live = try h.claude(H.claudeID, "a")
        let gone = try h.claude(H.otherID, "b")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: gone)
        let liveIDs: Set = [TranscriptMirror.SessionIdentity(harness: "claude", path: live.path)]
        #expect(h.mirror.pruned(live: liveIDs).map(\.source) == [gone.path])
        // The manifest lost or broken: the layout names the same source.
        try h.lose(.missing)
        #expect(h.mirror.pruned(live: liveIDs).map(\.source) == [gone.path])
        try h.lose(.corrupt)
        #expect(h.mirror.pruned(live: liveIDs).map(\.source) == [gone.path])
    }

    @Test func twoCopiesOfOneSessionAreBothCandidatesOfOneIdentity() throws {
        let h = try H()
        let a = try h.claude(H.claudeID, "a", folder: "-a")
        let b = try h.claude(H.claudeID, "a", "b", folder: "-b", mtime: H.mtime + 10)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: a)
        try FileManager.default.removeItem(at: b)
        let pruned = h.mirror.pruned(live: [])
        #expect(Set(pruned.map(\.source)) == [a.path, b.path])
        #expect(Set(pruned.map(\.identity)).count == 1)
    }

    @Test func aManifestEntryOfAnotherHarnessIsIgnored() throws {
        let h = try H()
        let source = try h.claude(H.claudeID, "a")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: source)
        // A tampered entry naming a codex source for a claude copy: the
        // layout answers instead.
        let same = try h.write(H.claudeLines(H.claudeID, ["a"]), to: h.temp.path("same.jsonl"))
        try h.copy(same.path,
                   rel: "claude/-work-proj/\(H.claudeID).jsonl", harness: "codex", source: "/elsewhere/x.jsonl")
        try h.saveManifest()
        #expect(h.mirror.pruned(live: []).map(\.source) == [source.path])
    }

    @Test func theRootFollowsItsEnvironmentOverride() {
        let home = URL(fileURLWithPath: "/u")
        #expect(TranscriptMirror.defaultRoot(home: home, environment: [:]).path == "/u/.swe/data/transcripts")
        #expect(TranscriptMirror.defaultRoot(home: home, environment: ["SWE_TRANSCRIPTS_DIR": "/tmp/m"]).path
                == "/tmp/m")
    }
}
