import Foundation
import Testing
@testable import SWECore

/// The two writers agree on mirror copies (#1918): the Swift writer, run
/// over the same transcripts and steps, leaves the same search rows swe's
/// own writer left. `Fixtures/mirror/python-index.json` is what the real
/// Python indexer wrote (`Fixtures/mirror/generate_python_index.py` runs
/// `swe session mirror` and `swe session index` from source in a throwaway
/// HOME); this test replays the same steps in Swift and compares every
/// `files` row, session field, message text and `mirror_copy:` mark, paths
/// relative to the home. A further Swift pass, with the mirror and without
/// it (the mirror-blind writer), then changes nothing.
@Suite struct SearchMirrorCrossWriterTests {
    typealias H = MirrorHome
    static let a = "11111111-2222-3333-4444-555555555555"
    static let b = "22222222-2222-3333-4444-555555555555"
    static let c = "33333333-2222-3333-4444-555555555555"

    private func fixture(_ name: String) throws -> URL {
        let base = try #require(Bundle.module.url(forResource: "Fixtures", withExtension: nil))
        return base.appending(path: "mirror/\(name)")
    }

    @discardableResult
    private func place(_ h: H, _ name: String, _ rel: String, mtime: TimeInterval = H.mtime,
                       append: Bool = false) throws -> URL {
        let text = try String(contentsOf: fixture(name), encoding: .utf8)
        return try h.write(text, to: h.home.appending(path: rel), mtime: mtime, append: append)
    }

    /// The same steps `generate_python_index.py` runs, the mirror passes
    /// done by `MirrorHome.mirrorPass` and the index passes by the Swift
    /// scan.
    private func replay(at home: URL? = nil) async throws -> H {
        let h = try H(at: home)
        let pruned = try place(h, "claude-pruned.jsonl", ".claude/projects/-work-proj/\(Self.a).jsonl")
        try place(h, "claude-live.jsonl", ".claude/projects/-work-proj/\(Self.b).jsonl")
        let grown = try place(h, "claude-grown.jsonl", ".claude/projects/-work-other/\(Self.c).jsonl")
        let codex = try place(h, "codex-pruned.jsonl", ".codex/sessions/2026/09/01/\(H.stem).jsonl")
        try h.mirrorPass()
        try FileManager.default.removeItem(at: pruned)
        try FileManager.default.removeItem(at: codex)
        try await h.scan()
        try place(h, "claude-grown-more.jsonl", ".claude/projects/-work-other/\(Self.c).jsonl",
                  mtime: H.mtime + 100, append: true)
        try h.mirrorPass()
        try FileManager.default.removeItem(at: grown)
        try await h.scan()
        return h
    }

    /// The index as `generate_python_index.py` dumps it.
    private func dump(_ h: H) throws -> [String: Any] {
        let store = h.store
        var files: [[String: Any]] = []
        let rows = try store.db.prepare("""
            SELECT f.path, f.byte_offset, f.mtime_ms, f.size_bytes, f.state, s.id, s.agent,
                   s.session_id, s.file_path, s.title, s.cwd, s.created_at, s.updated_at, s.message_count
            FROM files f LEFT JOIN sessions s ON s.id = f.session_row_id
            """)
        func value(_ text: String?) -> Any { text ?? NSNull() }
        while try rows.step() {
            let path = rows.text(0) ?? ""
            let mark = try store.meta("mirror_copy:" + path).map { mark -> [String: Any] in
                let parts = mark.split(separator: " ")
                return ["agent": String(parts[0]), "names_session": String(parts[1]) == String(rows.int(5))]
            }
            files.append([
                "path": h.rel(path), "byte_offset": rows.int(1), "mtime_ms": rows.double(2),
                "size_bytes": rows.int(3), "state": rows.text(4) ?? "", "agent": value(rows.text(6)),
                "session_id": value(rows.text(7)), "file_path": value(rows.text(8).map(h.rel)),
                "title": value(rows.text(9)), "cwd": value(rows.text(10)),
                "created_at": value(rows.text(11)), "updated_at": value(rows.text(12)),
                "message_count": rows.int(13), "mark": mark ?? NSNull(),
            ])
        }
        files.sort { ($0["path"] as! String) < ($1["path"] as! String) }
        var messages: [[String: Any]] = []
        let texts = try store.db.prepare("""
            SELECT s.file_path, m.role, m.ts, f.user_text, f.assistant_text, f.tool_text, f.identifiers
            FROM messages m JOIN sessions s ON s.id = m.session_row_id
            JOIN messages_fts f ON f.rowid = m.id ORDER BY s.file_path, m.id
            """)
        while try texts.step() {
            messages.append([
                "file_path": h.rel(texts.text(0) ?? ""), "role": texts.text(1) ?? "", "ts": value(texts.text(2)),
                "user_text": texts.text(3) ?? "", "assistant_text": texts.text(4) ?? "",
                "tool_text": texts.text(5) ?? "", "identifiers": texts.text(6) ?? "",
            ])
        }
        return ["files": files, "messages": messages,
                "sessions": try store.db.scalar("SELECT count(*) FROM sessions")]
    }

    /// Both sides as canonical JSON text, so a mismatch prints readably.
    private func canonical(_ object: Any) throws -> String {
        String(decoding: try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .prettyPrinted]),
               as: UTF8.self)
    }

    @Test func swiftLeavesTheRowsSweLeftForTheSameSteps() async throws {
        let h = try await replay()
        let python = try JSONSerialization.jsonObject(with: Data(contentsOf: fixture("python-index.json")))
        #expect(try canonical(dump(h)) == canonical(python))
    }

    /// Not a CI test: with `SWE_MIRROR_CROSS_HOME` set it replays the
    /// steps into that folder and leaves it, so swe can be run over the
    /// Swift-written index (`HOME=<folder> PYTHONPATH=src python3 -m swe.cli
    /// session index`) and its rows compared.
    @Test(.enabled(if: ProcessInfo.processInfo.environment["SWE_MIRROR_CROSS_HOME"] != nil))
    func replayIntoAFolderForSwe() async throws {
        let folder = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SWE_MIRROR_CROSS_HOME"]!)
        let h = try await replay(at: folder)
        try Data(try canonical(dump(h)).utf8).write(to: folder.appending(path: "swift-index.json"))
    }

    /// Not a CI test either: with `SWE_MIRROR_SWE_HOME` naming a home
    /// `generate_python_index.py <folder>` built and kept, it runs the
    /// Swift writer over the index swe wrote and expects no row to change.
    @Test(.enabled(if: ProcessInfo.processInfo.environment["SWE_MIRROR_SWE_HOME"] != nil))
    func scanAFolderSweIndexed() async throws {
        let folder = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SWE_MIRROR_SWE_HOME"]!)
        let h = try H(at: folder)
        let before = try canonical(dump(h))
        let report = try await h.scan()
        #expect(report.filesIndexed == 0)
        #expect(try canonical(dump(h)) == before)
        let python = try JSONSerialization.jsonObject(
            with: Data(contentsOf: folder.appending(path: "python-index.json")))
        #expect(try canonical(dump(h)) == canonical(python))
    }

    @Test func swiftAgreesWithItsOwnRowsOnTheNextPass() async throws {
        let h = try await replay()
        let before = try canonical(dump(h))
        try await h.scan()
        try await h.scan(withMirror: false)
        #expect(try canonical(dump(h)) == before)
    }
}
