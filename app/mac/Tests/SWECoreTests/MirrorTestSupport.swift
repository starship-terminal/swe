import Foundation
import Testing
@testable import SWECore

/// A throwaway home with Claude Code and Codex transcripts, swe's
/// transcript mirror beside them, a ledger and a search index: the
/// fixtures `SearchMirrorTests`, `LedgerMirrorTests` and
/// `TranscriptMirrorTests` share. Nothing here reads the real home.
///
/// `mirrorPass()` does what `swe session mirror` does to the layout (a
/// copy per live transcript at the same relative path, the source's mtime
/// kept, a manifest entry naming the source) without running swe.
final class MirrorHome: @unchecked Sendable {
    static let claudeID = "11111111-2222-3333-4444-555555555555"
    static let otherID = "22222222-2222-3333-4444-555555555555"
    static let thread = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
    static let stem = "rollout-2026-09-01T10-00-00-\(thread)"
    static let laterStem = "rollout-2026-09-03T10-00-00-\(thread)"
    static let mtime: TimeInterval = 1_790_000_000

    let temp: TempDir
    /// The home, resolved the way the app resolves the paths it indexes.
    let home: URL
    let ledger: Ledger
    let store: SearchStore
    let indexer: SearchIndexer

    /// - Parameter at: a home to use instead of a fresh temporary one
    ///   (a cross-writer run that hands the home to swe afterwards).
    init(bufferBudget: Int? = nil, at: URL? = nil) throws {
        temp = try TempDir()
        home = (at ?? temp.url).resolvingSymlinksInPath()
        ledger = try Ledger(directory: home.appending(path: ".swe/data/ledger"))
        let store = try SearchStore(directory: home.appending(path: ".swe/data/search"))
        self.store = store
        indexer = bufferBudget.map { SearchIndexer(store: store, bufferBudget: $0) } ?? SearchIndexer(store: store)
    }

    var claudeHome: URL { home.appending(path: ".claude") }
    var codexHome: URL { home.appending(path: ".codex") }
    var mirrorRoot: URL { home.appending(path: ".swe/data/transcripts") }

    /// SWE's accounts root. No profile is registered here, so a home
    /// under it is one the scan never reads (`account(_:_:)`).
    var accountsRoot: URL { home.appending(path: ".swe/accounts") }

    var mirror: TranscriptMirror {
        .standard(root: mirrorRoot, claudeHomes: [claudeHome], codexHomes: [codexHome],
                  accounts: accountsRoot)
    }

    /// An account home on disk that no profile names, as swe finds it
    /// (`accounts/<vendor>/<id>/home`).
    func account(_ vendor: String, _ id: String) -> URL {
        accountsRoot.appending(path: "\(vendor)/\(id)/home")
    }

    /// The roots a scan reads; `withMirror: false` is a writer that knows
    /// nothing about the mirror (the Swift writer before #1918).
    func roots(withMirror: Bool = true) -> LedgerRoots {
        LedgerRoots(claudeHomes: [claudeHome], codexHomes: [codexHome],
                    mirror: withMirror ? mirror : nil)
    }

    // MARK: Transcripts

    static func claudeLines(_ id: String, _ prompts: [String], start: Int = 0, usage: Bool = false) -> String {
        prompts.enumerated().map { offset, prompt -> String in
            let n = offset + start
            let stamp = String(format: "2026-10-01T10:%02d", n)
            let user: [String: Any] = [
                "type": "user", "sessionId": id, "cwd": "/work/proj", "timestamp": "\(stamp):00.000Z",
                "message": ["role": "user", "content": prompt],
            ]
            var message: [String: Any] = [
                "content": [["type": "text", "text": "done \(n)"]], "stop_reason": "end_turn",
            ]
            var assistant: [String: Any] = [
                "type": "assistant", "sessionId": id, "timestamp": "\(stamp):05.000Z",
            ]
            if usage {
                message["id"] = "msg_\(id.prefix(8))_\(n)"
                message["model"] = "claude-sonnet-4-5"
                message["usage"] = ["input_tokens": 100, "output_tokens": 20 + n]
                assistant["requestId"] = "req_\(id.prefix(8))_\(n)"
                assistant["cwd"] = "/work/proj"
            }
            assistant["message"] = message
            return [user, assistant].map(Self.line).joined()
        }.joined()
    }

    static func codexLines(_ thread: String, _ prompts: [String]) -> String {
        var lines = [Self.line(["type": "session_meta", "payload": ["id": thread, "cwd": "/work/other"]])]
        for prompt in prompts {
            lines.append(Self.line(["type": "response_item", "payload": [
                "type": "message", "role": "user",
                "content": [["type": "input_text", "text": prompt]],
            ]]))
        }
        return lines.joined()
    }

    static func line(_ object: [String: Any]) -> String {
        let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        return String(decoding: data, as: UTF8.self) + "\n"
    }

    @discardableResult
    func write(_ text: String, to url: URL, mtime: TimeInterval = MirrorHome.mtime, append: Bool = false) throws -> URL {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        if append, FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.append(text, to: url)
        } else {
            try Data(text.utf8).write(to: url)
        }
        try setMtime(mtime, url)
        return url
    }

    func setMtime(_ mtime: TimeInterval, _ url: URL) throws {
        try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: mtime)],
                                              ofItemAtPath: url.path)
    }

    @discardableResult
    func claude(_ id: String = MirrorHome.claudeID, _ prompts: String..., folder: String = "-work-proj",
                mtime: TimeInterval = MirrorHome.mtime, usage: Bool = false) throws -> URL {
        try write(Self.claudeLines(id, prompts.isEmpty ? ["fix the login bug"] : prompts, usage: usage),
                  to: claudeHome.appending(path: "projects/\(folder)/\(id).jsonl"), mtime: mtime)
    }

    @discardableResult
    func grow(_ url: URL, _ id: String, _ prompts: String..., mtime: TimeInterval, usage: Bool = false) throws -> URL {
        try write(Self.claudeLines(id, prompts, start: 10, usage: usage), to: url, mtime: mtime, append: true)
    }

    @discardableResult
    func codex(_ stem: String = MirrorHome.stem, _ prompts: String..., under: String = "sessions/2026/09/01",
               mtime: TimeInterval = MirrorHome.mtime, thread: String = MirrorHome.thread) throws -> URL {
        try write(Self.codexLines(thread, prompts.isEmpty ? ["add dark mode"] : prompts),
                  to: codexHome.appending(path: "\(under)/\(stem).jsonl"), mtime: mtime)
    }

    // MARK: The mirror

    /// The manifest as swe writes it: `{version, files: {rel: entry}}`.
    private(set) var manifest: [String: [String: Any]] = [:]

    /// Copies every live Claude and Codex transcript into the mirror at
    /// its relative path, keeping the source's mtime, and records it.
    func mirrorPass() throws {
        let trees: [(String, URL, [String])] = [
            ("claude", claudeHome, ["projects"]), ("codex", codexHome, ["sessions", "archived_sessions"]),
        ]
        for (harness, harnessHome, subdirs) in trees {
            for sub in subdirs {
                let base = harnessHome.appending(path: sub)
                for path in TranscriptMirror.walk(base.path) {
                    let rest = String(path.dropFirst(base.resolvingSymlinksInPath().path.count + 1))
                    try copy(path, rel: "\(harness)/\(rest)", harness: harness, source: path)
                }
            }
        }
        try saveManifest()
    }

    /// One copy at `rel` holding `path`'s bytes and mtime, recorded as
    /// coming from `source` (nil leaves it out of the manifest).
    func copy(_ path: String, rel: String, harness: String, source: String?) throws {
        let target = mirrorRoot.appending(path: rel)
        try FileManager.default.createDirectory(at: target.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try? FileManager.default.removeItem(at: target)
        try FileManager.default.copyItem(atPath: path, toPath: target.path)
        let mtime = (try FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as? Date)!
        try setMtime(mtime.timeIntervalSince1970, target)
        if let source {
            manifest[rel] = ["harness": harness, "source": source, "account_home": ""]
        }
    }

    func saveManifest() throws {
        let body: [String: Any] = ["version": 1, "files": manifest]
        let data = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
        try FileManager.default.createDirectory(at: mirrorRoot, withIntermediateDirectories: true)
        try data.write(to: mirrorRoot.appending(path: "manifest.json"))
    }

    enum Loss { case missing, corrupt }

    func lose(_ how: Loss) throws {
        let url = mirrorRoot.appending(path: "manifest.json")
        switch how {
        case .missing: try FileManager.default.removeItem(at: url)
        case .corrupt: try Data("{not json".utf8).write(to: url)
        }
    }

    /// Every copy's relative path and bytes.
    func copies() -> [String: Data] {
        var out: [String: Data] = [:]
        for copy in mirror.copies() {
            out[copy.rel] = FileManager.default.contents(atPath: copy.path)
        }
        return out
    }

    // MARK: Actions

    @discardableResult
    func scan(withMirror: Bool = true, index: Bool = true) async throws -> ScanReport {
        await ledger.setIndexer(index ? indexer : nil)
        return try await ledger.scan(roots: roots(withMirror: withMirror))
    }

    // MARK: What the index holds

    /// A path as a test compares it: relative to the home.
    func rel(_ path: String) -> String {
        let prefixes = [home.path, "/private" + home.path]
        for prefix in prefixes where path.hasPrefix(prefix + "/") {
            return String(path.dropFirst(prefix.count + 1))
        }
        return path
    }

    struct FileRow: Equatable, CustomStringConvertible {
        var path: String
        var byteOffset: Int64
        var mtimeMs: Double
        var sizeBytes: Int64?
        var state: String
        var agent: String?
        var sessionID: String?
        var filePath: String?
        var messageCount: Int64?
        var description: String {
            "\(path) @\(byteOffset) \(state) \(agent ?? "-") \(sessionID ?? "-") n=\(messageCount ?? -1)"
        }
    }

    /// `files` joined to its session, paths relative to the home.
    func files() throws -> [FileRow] {
        let statement = try store.db.prepare("""
            SELECT f.path, f.byte_offset, f.mtime_ms, f.size_bytes, f.state,
                   s.agent, s.session_id, s.file_path, s.message_count
            FROM files f LEFT JOIN sessions s ON s.id = f.session_row_id ORDER BY f.path
            """)
        var rows: [FileRow] = []
        while try statement.step() {
            rows.append(FileRow(path: rel(statement.text(0) ?? ""), byteOffset: statement.int(1),
                                mtimeMs: statement.double(2),
                                sizeBytes: statement.isNull(3) ? nil : statement.int(3),
                                state: statement.text(4) ?? "", agent: statement.text(5),
                                sessionID: statement.text(6), filePath: statement.text(7).map(rel),
                                messageCount: statement.isNull(8) ? nil : statement.int(8)))
        }
        return rows.sorted { $0.path < $1.path }
    }

    /// `(agent, session id, file path)` of every session row.
    func sessions(agent: String? = nil) throws -> [[String]] {
        let statement = try store.db.prepare(
            "SELECT agent, session_id, file_path, message_count FROM sessions ORDER BY id")
        var out: [[String]] = []
        while try statement.step() {
            let row = [statement.text(0) ?? "", statement.text(1) ?? "", rel(statement.text(2) ?? ""),
                       String(statement.int(3))]
            if agent == nil || row[0] == agent { out.append(row) }
        }
        return out
    }

    /// Every live message's text with the path its session is filed under.
    func messages() throws -> [String] {
        let statement = try store.db.prepare("""
            SELECT s.file_path, m.role, f.user_text, f.assistant_text, f.tool_text FROM messages m
            JOIN sessions s ON s.id = m.session_row_id
            JOIN messages_fts f ON f.rowid = m.id ORDER BY s.file_path, m.id
            """)
        var out: [String] = []
        while try statement.step() {
            out.append([rel(statement.text(0) ?? ""), statement.text(1) ?? "", statement.text(2) ?? "",
                        statement.text(3) ?? "", statement.text(4) ?? ""].joined(separator: "|"))
        }
        return out
    }

    /// The session ids a search for `word` returns, one per hit.
    func hits(_ word: String) throws -> [String] {
        try SearchEngine(directory: store.directory).search(query: word).map(\.sessionID)
    }

    /// The `mirror_copy:` marks, by relative path.
    func marks() throws -> [String: String] {
        let statement = try store.db.prepare(
            "SELECT key, value FROM meta WHERE key LIKE 'mirror_copy:%' ORDER BY key")
        var out: [String: String] = [:]
        while try statement.step() {
            let key = statement.text(0) ?? ""
            out[rel(String(key.dropFirst("mirror_copy:".count)))] = statement.text(1)
        }
        return out
    }

    /// Everything a search sees, for an equality check across states.
    func snapshot() throws -> String {
        let files = try files().map { row -> String in
            // A copy filed under a source path keeps the copy's inode; the
            // ino is left out, the rest must match.
            row.description + " mtime=\(row.mtimeMs) size=\(row.sizeBytes ?? -1)"
        }
        return (files + (try sessions().map { $0.joined(separator: ",") }) + (try messages()))
            .joined(separator: "\n")
    }
}
