import Foundation
import os
import SQLite3

/// What a `files` row still owes a reader.
///
/// `current`: the index's rows match the file at the stat the row records.
/// `due`: the index is behind on a span it cannot reach by appending, so the
/// next pass reads the file whole.
/// `failed`: the last read did not commit; `failCount` and `failedMtimeMs`
/// are what stop it being retried for ever.
public enum SearchFileState: String, Sendable, CaseIterable {
    case current, due, failed
}

/// The stat a `files` row records: what identified the file when it was read.
/// `dev`/`ino` are optional because a stat from a partial observation must
/// never overwrite a pair a real stat proved.
public struct SearchFileStat: Equatable, Sendable {
    public var dev: Int64?
    public var ino: Int64?
    public var mtimeMs: Double
    public var sizeBytes: Int64?

    public init(dev: Int64? = nil, ino: Int64? = nil, mtimeMs: Double, sizeBytes: Int64? = nil) {
        self.dev = dev
        self.ino = ino
        self.mtimeMs = mtimeMs
        self.sizeBytes = sizeBytes
    }
}

/// The pair a write fences on: the session its rows hang off and the cursor
/// they continue from. Re-read inside every write transaction — a remove or
/// another writer between the read and the commit means the buffered rows no
/// longer continue anything, and committing on top of that would resurrect a
/// deleted source or duplicate a span.
public struct SearchFileFence: Equatable, Sendable {
    public var sessionRowID: Int64?
    public var byteOffset: Int64

    public init(sessionRowID: Int64?, byteOffset: Int64) {
        self.sessionRowID = sessionRowID
        self.byteOffset = byteOffset
    }
}

/// One row of the `files` table: the indexer's whole memory between passes.
public struct SearchFileRow: Equatable, Sendable {
    public var path: String
    public var dev: Int64?
    public var ino: Int64?
    /// `SearchStore.partialFileOffset` (-1) marks a half-written file.
    public var byteOffset: Int64
    public var mtimeMs: Double
    public var sizeBytes: Int64?
    public var sessionRowID: Int64?
    public var state: SearchFileState
    public var failCount: Int
    public var failedMtimeMs: Double?

    /// Whether the row still owes a read on the assumption that its
    /// source's stat cannot have moved — the check a container scan runs
    /// before paying to enumerate the source at all. `due` and the
    /// partial sentinel are always behind; a `failed` row is behind until
    /// it has failed enough at this stat to be held out, which under an
    /// unchanged stat is exactly `failCount >= failuresBeforeHoldout`.
    var owesRead: Bool {
        if byteOffset == SearchStore.partialFileOffset { return true }
        switch state {
        case .current: return false
        case .due: return true
        case .failed: return failCount < SearchIndexer.failuresBeforeHoldout
        }
    }
}

/// Everything a `sessions` row carries. `agent` and `filePath` are required;
/// the rest is what the parser has decoded so far — a row a chunk commits is
/// written provisionally and the finishing commit fills it in.
public struct SearchSessionFields: Equatable, Sendable {
    public var agent: String
    public var sessionID: String
    public var filePath: String
    public var title: String
    public var cwd: String?
    public var cwdKey: String?
    public var createdAt: String?
    public var updatedAt: String?
    public var messageCount: Int

    public init(agent: String, sessionID: String = "", filePath: String, title: String = "",
                cwd: String? = nil, cwdKey: String? = nil,
                createdAt: String? = nil, updatedAt: String? = nil, messageCount: Int = 0) {
        self.agent = agent
        self.sessionID = sessionID
        self.filePath = filePath
        self.title = title
        self.cwd = cwd
        self.cwdKey = cwdKey
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.messageCount = messageCount
    }
}

/// A `sessions` row read back: its id plus its fields.
public struct SearchSession: Equatable, Sendable {
    public var id: Int64
    public var fields: SearchSessionFields
}

/// One indexable unit: a row in `messages` and the FTS row with the same id.
/// Fill the one column the role searches under (user, assistant or tool
/// text); `identifiers` carries the split form of the same text — camelCase,
/// snake_case and path pieces — so code tokens match.
public struct SearchMessageRow: Equatable, Sendable {
    public var role: String
    public var ts: String?
    public var userText: String
    public var assistantText: String
    public var toolText: String
    public var identifiers: String

    public init(role: String, ts: String? = nil,
                userText: String = "", assistantText: String = "",
                toolText: String = "", identifiers: String = "") {
        self.role = role
        self.ts = ts
        self.userText = userText
        self.assistantText = assistantText
        self.toolText = toolText
        self.identifiers = identifiers
    }
}

/// The whole of the indexer's progress report: rows per file state, and the
/// session and message totals beside them.
public struct SearchStatus: Equatable, Sendable {
    public var filesByState: [SearchFileState: Int] = [:]
    /// Sessions per agent. The one number that distinguishes an agent the
    /// index has read from one it has only listed.
    public var sessionsByAgent: [String: Int] = [:]
    public var sessionCount = 0
    public var messageCount = 0

    public init() {}
}

/// What one file's commit does to its session, inside `commitFile`'s single
/// transaction.
public enum IndexSessionCommit: Sendable, Equatable {
    /// No session: only the cursor advances (a read-through that held none).
    case none
    /// The rows continue this session — an append.
    case existing(Int64)
    /// A brand-new session row with these fields.
    case fresh(SearchSessionFields)
    /// A new generation: mint a row, point the file at it, delete the old row
    /// (its messages are orphaned for the drain to reclaim).
    case replace(SearchSessionFields)
}

/// The persisted search index: per-file cursors, session rows, and message
/// text in an FTS5 table. Adapted from Orca's `session-search-store.ts` and
/// `session-search-index-writer.ts`.
///
/// Writes are not thread-safe — like `LedgerStore`, one owner serializes
/// them (the `Ledger` actor's scan). `status()` is the exception: it runs
/// on its own read-only connection under a lock, so a UI can poll while a
/// scan writes. `clear()` is the other: a scan can still be inside an
/// index call when it lands — the actor binds the indexer across awaits,
/// so a detached indexer can outlive `setIndexer(nil)` — so `storeLock`
/// serializes the teardown against every method and calls that arrive
/// after it throw instead of touching a closed handle.
///
/// Two conventions come from Orca and are load-bearing:
///
/// - `messages_fts.rowid` equals `messages.id`. Both are inserted by hand in
///   the same transaction; there are no sync triggers and no foreign keys.
/// - Replacing a file's session mints a NEW `sessions` row and deletes only
///   the OLD row. Every retrieval joins `sessions`, so the old generation
///   stops answering the moment that commits; its messages are reclaimed
///   afterwards by `drainOrphanedMessages`, never deleted inline — deleting a
///   whole conversation inside a write transaction sizes it by the session
///   being replaced rather than by the chunk being written.
public final class SearchStore: @unchecked Sendable {
    /// `files.byte_offset` for a read that has not reached the end of the
    /// file. Not a byte offset, so an append is declined and the file is
    /// re-read whole; the rows it did write still answer searches.
    public static let partialFileOffset: Int64 = -1

    /// SWE's own schema version (see `SearchSchema`).
    public static var schemaVersion: Int { SearchSchema.version }

    /// Whether this build's SQLite can run the FTS5 index — a property of
    /// the linked library, not of the index file, so the check opens a
    /// throwaway in-memory database instead of the real one. Probed once.
    public static let fts5Supported: Bool = {
        guard let db = try? SQLiteDatabase() else { return false }
        return SearchSchema.probeFTS5(db)
    }()

    public let url: URL
    public let directory: URL
    /// Whether this build of SQLite has FTS5. When false the rest of the
    /// schema still exists and callers should degrade: skip indexing, answer
    /// no searches.
    public let fts5Available: Bool

    /// The write connection; nil after `clear()`. Internal so tests can read
    /// through it under `@testable`. Production code reaches it through
    /// `liveDB()` — the raw handle is only safe under `storeLock`.
    var db: SQLiteDatabase!
    /// A read-only connection `status()` opens lazily and shares — a WAL
    /// reader never queues behind a scan on the write connection, the same
    /// pattern as the ledger's `usageConnection`. The lock is what makes
    /// `status()` and `clear()` safe from threads the writer never sees.
    private let statusConnection = OSAllocatedUnfairLock<SQLiteDatabase?>(initialState: nil)
    /// Prepared statements reused across calls — `Statement.bind` resets, so
    /// a cached statement is always clean on entry. A scan prepares ~10
    /// statements per indexed file otherwise, plus two SELECTs per file even
    /// when nothing changed.
    private var statements: [String: Statement] = [:]

    /// Serializes `clear()` against every other method: the Ledger actor can
    /// still be inside an index call when the model tears the store down,
    /// so the lock makes the teardown atomic and `liveDB()` turns later
    /// calls into a thrown error instead of a nil-handle trap. Recursive
    /// because the transactional methods reach `cachedStatement` and each
    /// other through one hold. Internal so a test can hold it and prove
    /// `clear()` waits.
    let storeLock = NSRecursiveLock()

    /// The live write connection under `storeLock`, or an error once
    /// `clear()` ran.
    private func liveDB() throws -> SQLiteDatabase {
        try storeLock.withLock {
            guard let db else {
                throw SQLiteDatabase.Failure(
                    code: SQLITE_MISUSE, message: "search store is cleared")
            }
            return db
        }
    }

    private func cachedStatement(_ sql: String) throws -> Statement {
        try storeLock.withLock {
            if let statement = statements[sql] { return statement }
            let statement = try liveDB().prepare(sql)
            statements[sql] = statement
            return statement
        }
    }

    /// Finalize the cached statements before the handle closes — SQLite
    /// tolerates either order, but a statement finalized on a closed handle
    /// is an avoidable crash path.
    deinit { statements.removeAll() }

    /// - Parameter directory: where `search.sqlite` lives; production uses
    ///   `SearchLocation.directory(under:)` (`~/.swe/data/search/`).
    public init(directory: URL) throws {
        self.directory = directory
        url = directory.appending(path: SearchLocation.fileName)
        let opened = try SearchSchema.open(url: url)
        db = opened.connection
        fts5Available = opened.fts5Available
    }

    // MARK: - Files

    /// Every field the index holds for `path`, or nil when it holds nothing.
    public func fileRow(for path: String) throws -> SearchFileRow? {
        try storeLock.withLock {
            let statement = try cachedStatement("""
                SELECT path, dev, ino, byte_offset, mtime_ms, size_bytes, session_row_id,
                       state, fail_count, failed_mtime_ms
                FROM files WHERE path = ?
                """)
            statement.bind([.text(path)])
            guard try statement.step() else { return nil }
            let row = Self.fileRow(statement)
            // An unstepped statement keeps a WAL read cursor open, which holds
            // checkpointing hostage; `bind` resets it before reuse.
            statement.bind([])
            return row
        }
    }

    /// Every row this index holds, path-ordered for a deterministic listing.
    public func files() throws -> [SearchFileRow] {
        try storeLock.withLock {
            let statement = try cachedStatement("""
                SELECT path, dev, ino, byte_offset, mtime_ms, size_bytes, session_row_id,
                       state, fail_count, failed_mtime_ms
                FROM files ORDER BY path
                """)
            statement.bind([])
            var rows: [SearchFileRow] = []
            while try statement.step() {
                rows.append(Self.fileRow(statement))
            }
            return rows
        }
    }

    /// The rows under one path prefix — how synthetic container paths
    /// (`<db>#<id>`) enumerate just their own entries without dumping the
    /// whole table into memory.
    public func files(withPrefix prefix: String) throws -> [SearchFileRow] {
        try storeLock.withLock {
            // A [prefix, prefix+max-scalar) range — an index seek rather than a
            // `substr` scan, and no code-point counting questions.
            let statement = try cachedStatement("""
                SELECT path, dev, ino, byte_offset, mtime_ms, size_bytes, session_row_id,
                       state, fail_count, failed_mtime_ms
                FROM files WHERE path >= ? AND path < ? || char(1114111) ORDER BY path
                """)
            statement.bind([.text(prefix), .text(prefix)])
            var rows: [SearchFileRow] = []
            while try statement.step() {
                rows.append(Self.fileRow(statement))
            }
            return rows
        }
    }

    /// Just the pair a write fences on, for the cheap read outside a
    /// transaction.
    public func fileFence(for path: String) throws -> SearchFileFence? {
        try storeLock.withLock {
            let statement = try cachedStatement(
                "SELECT session_row_id, byte_offset FROM files WHERE path = ?")
            statement.bind([.text(path)])
            guard try statement.step() else { return nil }
            let fence = SearchFileFence(sessionRowID: statement.isNull(0) ? nil : statement.int(0),
                                      byteOffset: statement.int(1))
            statement.bind([])
            return fence
        }
    }

    private static func fileRow(_ row: Statement) -> SearchFileRow {
        SearchFileRow(
            path: row.text(0) ?? "",
            dev: row.isNull(1) ? nil : row.int(1),
            ino: row.isNull(2) ? nil : row.int(2),
            byteOffset: row.int(3),
            mtimeMs: row.double(4),
            sizeBytes: row.isNull(5) ? nil : row.int(5),
            sessionRowID: row.isNull(6) ? nil : row.int(6),
            state: SearchFileState(rawValue: row.text(7) ?? "") ?? .current,
            failCount: Int(row.int(8)),
            failedMtimeMs: row.isNull(9) ? nil : row.double(9))
    }

    /// Records where `path` was read up to. `byteOffset` may be
    /// `partialFileOffset`: the rows are a coherent prefix but nothing may
    /// append to them.
    ///
    /// Partial observations must never create a dev/ino pair that no stat
    /// proved, so a missing pair keeps the stored one rather than erasing it.
    /// A committed read also clears the row's `failed`/`due` state, so a
    /// recovered file stops re-reading on every pass.
    public func upsertFile(path: String, stat: SearchFileStat,
                           byteOffset: Int64, sessionRowID: Int64?) throws {
        try storeLock.withLock {
            try cachedStatement("""
                INSERT INTO files(path, dev, ino, byte_offset, mtime_ms, size_bytes, session_row_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  dev = CASE WHEN excluded.dev IS NOT NULL AND excluded.ino IS NOT NULL
                        THEN excluded.dev ELSE files.dev END,
                  ino = CASE WHEN excluded.dev IS NOT NULL AND excluded.ino IS NOT NULL
                        THEN excluded.ino ELSE files.ino END,
                  byte_offset = excluded.byte_offset, mtime_ms = excluded.mtime_ms,
                  size_bytes = excluded.size_bytes, session_row_id = excluded.session_row_id,
                  state = 'current', fail_count = 0, failed_mtime_ms = NULL
                """).bind([
                .text(path), stat.dev.map(SQLiteValue.int) ?? .null,
                stat.ino.map(SQLiteValue.int) ?? .null,
                .int(byteOffset), .double(stat.mtimeMs),
                stat.sizeBytes.map(SQLiteValue.int) ?? .null,
                sessionRowID.map(SQLiteValue.int) ?? .null,
            ]).run()
        }
    }

    /// Moves a row's read state.
    ///
    /// `failed` also counts the failure and records the stat it happened at,
    /// which is what lets the next pass tell "this file has never worked"
    /// from "this file changed since it last failed". A path with no row
    /// still gets one on `failed`: the common unreadable file is one the
    /// index never managed to hold, and with nowhere to write the count it
    /// would be retried on every pass for the life of the process. `current`
    /// and `due` only move an existing row.
    public func setFileState(_ path: String, _ state: SearchFileState, atMtimeMs: Double? = nil) throws {
        try storeLock.withLock {
            if state == .failed {
                try cachedStatement("""
                    INSERT INTO files(path, byte_offset, mtime_ms, state, fail_count, failed_mtime_ms)
                    VALUES (?, 0, ?, 'failed', 1, ?)
                    ON CONFLICT(path) DO UPDATE SET
                      state = 'failed',
                      fail_count = files.fail_count + 1,
                      failed_mtime_ms = excluded.failed_mtime_ms
                    """).bind([.text(path), .double(atMtimeMs ?? 0), .optionalDouble(atMtimeMs)]).run()
                return
            }
            try cachedStatement("""
                UPDATE files SET state = ?, fail_count = 0, failed_mtime_ms = NULL WHERE path = ?
                """).bind([.text(state.rawValue), .text(path)]).run()
        }
    }

    /// Deletes `path`'s row and the session it pointed at, in one
    /// transaction — a source proven gone. Its messages stop answering at
    /// once (every retrieval joins `sessions`) and the orphan drain
    /// reclaims them in its own bounded passes, the same lifecycle a
    /// `replaceSession` leaves behind. Orca's writer calls this
    /// `removeFile`.
    public func removeFile(_ path: String) throws {
        try storeLock.withLock {
            let db = try liveDB()
            try db.execute("BEGIN IMMEDIATE")
            do {
                if let sessionRowID = try fileFence(for: path)?.sessionRowID {
                    try deleteSession(sessionRowID)
                    try setOrphansPending()
                }
                try cachedStatement("DELETE FROM files WHERE path = ?").bind([.text(path)]).run()
                try cachedStatement("DELETE FROM meta WHERE key = ?")
                    .bind([.text(SearchDevin.markKey(path))]).run()
                try db.execute("COMMIT")
            } catch {
                try? db.execute("ROLLBACK")
                throw error
            }
        }
    }

    // MARK: - Sessions

    /// Inserts a `sessions` row and returns its id. `sessionID` and `title`
    /// may be empty: a row a chunk commits is written before the parser has
    /// said what the session is, and `updateSession` fills it in later.
    @discardableResult
    public func createSession(_ fields: SearchSessionFields) throws -> Int64 {
        try storeLock.withLock {
            try cachedStatement("""
                INSERT INTO sessions(agent, session_id, file_path, title, cwd, cwd_key,
                                     created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """).bind(Self.sessionValues(fields)).run()
            return try liveDB().lastInsertRowID
        }
    }

    /// Rewrites every field of a `sessions` row — the decoded session always
    /// wins over the provisional values a chunk wrote.
    public func updateSession(_ rowID: Int64, _ fields: SearchSessionFields) throws {
        try storeLock.withLock {
            try cachedStatement("""
                UPDATE sessions SET agent = ?, session_id = ?, file_path = ?, title = ?,
                    cwd = ?, cwd_key = ?, created_at = ?, updated_at = ?, message_count = ?
                WHERE id = ?
                """).bind(Self.sessionValues(fields) + [.int(rowID)]).run()
        }
    }

    /// Deletes one `sessions` row. Callers that do this inside their own
    /// transaction must also mark `orphans_pending`, or the drain never
    /// looks for the messages this cut loose.
    private func deleteSession(_ rowID: Int64) throws {
        try cachedStatement("DELETE FROM sessions WHERE id = ?").bind([.int(rowID)]).run()
    }

    private static func sessionValues(_ fields: SearchSessionFields) -> [SQLiteValue?] {
        [
            .text(fields.agent), .text(fields.sessionID), .text(fields.filePath),
            .text(fields.title), fields.cwd.map(SQLiteValue.text) ?? .null,
            fields.cwdKey.map(SQLiteValue.text) ?? .null,
            fields.createdAt.map(SQLiteValue.text) ?? .null,
            fields.updatedAt.map(SQLiteValue.text) ?? .null,
            .int(fields.messageCount),
        ]
    }

    /// A `sessions` row and its id, or nil when it is gone.
    public func sessionRow(_ id: Int64) throws -> SearchSession? {
        try storeLock.withLock {
            let statement = try cachedStatement("""
                SELECT agent, session_id, file_path, title, cwd, cwd_key,
                       created_at, updated_at, message_count
                FROM sessions WHERE id = ?
                """)
            statement.bind([.int(id)])
            guard try statement.step() else { return nil }
            let session = SearchSession(id: id, fields: SearchSessionFields(
                agent: statement.text(0) ?? "", sessionID: statement.text(1) ?? "",
                filePath: statement.text(2) ?? "", title: statement.text(3) ?? "",
                cwd: statement.text(4), cwdKey: statement.text(5),
                createdAt: statement.text(6), updatedAt: statement.text(7),
                messageCount: Int(statement.int(8))))
            statement.bind([])
            return session
        }
    }

    /// Points `path` at a fresh `sessions` row and deletes the old row only,
    /// in one transaction. The old generation's messages stop answering the
    /// moment this commits — every retrieval joins `sessions` — and the
    /// orphan drain reclaims them later.
    ///
    /// Fenced like `commitFile`: the record is re-read under the write lock
    /// and nothing happens when it moved. Returns the new row id, or nil when
    /// there is no `files` row or it no longer matches `expected`. A caller
    /// follows with `commitFile` expected at `(new id, stored offset)`, which
    /// a whole-file re-read commits atomically per Orca's writer.
    @discardableResult
    public func replaceSession(for path: String, expected: SearchFileFence?,
                               fields: SearchSessionFields) throws -> Int64? {
        try storeLock.withLock {
            let db = try liveDB()
            try db.execute("BEGIN IMMEDIATE")
            do {
                guard let actual = try fileFence(for: path),
                      actual.sessionRowID == expected?.sessionRowID,
                      actual.byteOffset == expected?.byteOffset else {
                    try db.execute("ROLLBACK")
                    return nil
                }
                let rowID = try createSession(fields)
                try cachedStatement("UPDATE files SET session_row_id = ? WHERE path = ?")
                    .bind([.int(rowID), .text(path)]).run()
                if let old = actual.sessionRowID {
                    try deleteSession(old)
                    try setOrphansPending()
                }
                try db.execute("COMMIT")
                return rowID
            } catch {
                try? db.execute("ROLLBACK")
                throw error
            }
        }
    }

    // MARK: - Per-file write transaction

    /// Commits one read's rows for `path` in a single `BEGIN IMMEDIATE`
    /// transaction: re-reads the `files` row and verifies it still equals
    /// `expected` (nil = no row expected), resolves the session (reusing,
    /// creating or replacing the row), writes the message and FTS rows, and
    /// advances the cursor — all or nothing.
    ///
    /// - Returns: false when the record moved under the read — it was removed,
    ///   or another writer moved the cursor these rows continue from — and
    ///   nothing was written. A read that never calls this leaves the index
    ///   exactly as it found it.
    /// - Parameters:
    ///   - session: what happens to the file's session — kept, created,
    ///     generation-swapped, or none (a file read through that held none,
    ///     which commits only its cursor). Required when `messages` is
    ///     non-empty.
    ///   - updating: when set, rewrites the session row's fields in the same
    ///     transaction, so a finishing commit publishes the decoded session
    ///     together with its last rows.
    ///   - mark: a Devin session's read position, stored under its
    ///     `SearchDevin.markKey` with the session the commit lands on.
    ///   - writesMark: whether `mark` is written at all; a nil `mark` then
    ///     deletes the one a last read left.
    ///   - expectedMtimeMs: also fences on the row's `mtime_ms`. An append
    ///     keeps the session and the offset, so without it an older read
    ///     could commit over a newer one.
    ///   - drop: other files' rows of the same session (a mirror copy's,
    ///     #1918), each fenced like `path` on its session, offset and
    ///     mtime, and deleted with its session once this commit's rows are
    ///     in, so search never sees the session twice or not at all.
    ///   - adopt: `(host, ids)`, the messages a staged read parked on the
    ///     session `host` (`stageMessages`), moved to this commit's session
    ///     by their exact ids: a message another writer added to `host`
    ///     meanwhile never moves with them.
    ///   - gone: paths that must still be absent from disk, checked inside
    ///     the transaction (a copy's source path and the dropped rows').
    ///   - copyMark: the agent, when the rows come from a mirror copy: the
    ///     commit records `mirror_copy:<path>` = `"<agent> <session>"`.
    ///     Any other commit for `path` deletes that key, since the row is
    ///     then the file's own (swe's `commit_file` does the same).
    @discardableResult
    func commitFile(
        path: String,
        expected: SearchFileFence?,
        stat: SearchFileStat,
        byteOffset: Int64,
        session: IndexSessionCommit,
        messages: [SearchMessageRow],
        updating fields: SearchSessionFields? = nil,
        mark: SearchDevin.Mark?,
        writesMark: Bool = false,
        expectedMtimeMs: Double? = nil,
        drop: [SearchFileRow] = [],
        adopt: (host: Int64, ids: [Int64])? = nil,
        gone: [String] = [],
        copyMark: String? = nil
    ) throws -> Bool {
        if case .none = session, !messages.isEmpty || fields != nil {
            throw SQLiteDatabase.Failure(
                code: SQLITE_MISUSE, message: "commitFile: messages and fields need a session")
        }
        return try storeLock.withLock {
            let db = try liveDB()
            try db.execute("BEGIN IMMEDIATE")
            do {
                let actual = try fileFence(for: path)
                guard actual?.sessionRowID == expected?.sessionRowID,
                      actual?.byteOffset == expected?.byteOffset,
                      try expectedMtimeMs.map({ try Self.mtime(of: path, in: db) == $0 }) ?? true,
                      try unchanged(drop), !gone.contains(where: TranscriptMirror.exists)
                else {
                    try db.execute("ROLLBACK")
                    return false
                }
                let sessionRowID: Int64?
                switch session {
                case .none:
                    sessionRowID = nil
                case .existing(let rowID):
                    sessionRowID = rowID
                case .fresh(let fields):
                    sessionRowID = try createSession(fields)
                case .replace(let fields):
                    sessionRowID = try createSession(fields)
                    // Mint first, then cut the old row loose: the generation swap
                    // is one commit, so retrieval never sees the file unparented.
                    if let old = actual?.sessionRowID {
                        try deleteSession(old)
                        try setOrphansPending()
                    }
                }
                if let adopt, let sessionRowID {
                    let move = try cachedStatement(
                        "UPDATE messages SET session_row_id = ? WHERE id = ? AND session_row_id = ?")
                    for id in adopt.ids {
                        try move.bind([.int(sessionRowID), .int(id), .int(adopt.host)]).run()
                    }
                }
                if let sessionRowID {
                    try insertMessages(under: sessionRowID, messages)
                    if let fields {
                        try updateSession(sessionRowID, fields)
                    }
                }
                try upsertFile(path: path, stat: stat, byteOffset: byteOffset, sessionRowID: sessionRowID)
                for old in drop {
                    if let oldSession = old.sessionRowID, oldSession != sessionRowID {
                        try deleteSession(oldSession)
                        try setOrphansPending()
                    }
                    try cachedStatement("DELETE FROM files WHERE path = ?").bind([.text(old.path)]).run()
                    try deleteMeta(Self.copyMarkKey(old.path))
                }
                if let copyMark, let sessionRowID {
                    try setMeta(Self.copyMarkKey(path), "\(copyMark) \(sessionRowID)")
                } else {
                    try deleteMeta(Self.copyMarkKey(path))
                }
                if writesMark, let mark, let sessionRowID {
                    try setMeta(SearchDevin.markKey(path), mark.value(session: sessionRowID))
                } else if writesMark, mark == nil {
                    try cachedStatement("DELETE FROM meta WHERE key = ?")
                        .bind([.text(SearchDevin.markKey(path))]).run()
                }
                try db.execute("COMMIT")
                return true
            } catch {
                try? db.execute("ROLLBACK")
                throw error
            }
        }
    }

    @discardableResult
    public func commitFile(
        path: String,
        expected: SearchFileFence?,
        stat: SearchFileStat,
        byteOffset: Int64,
        session: IndexSessionCommit,
        messages: [SearchMessageRow],
        updating fields: SearchSessionFields? = nil
    ) throws -> Bool {
        try commitFile(path: path, expected: expected, stat: stat, byteOffset: byteOffset,
                       session: session, messages: messages, updating: fields, mark: nil)
    }

    private static func mtime(of path: String, in db: SQLiteDatabase) throws -> Double? {
        let statement = try db.prepare("SELECT mtime_ms FROM files WHERE path = ?")
        statement.bind([.text(path)])
        return try statement.step() ? statement.double(0) : nil
    }

    /// One `meta` row, or nil: a Devin session's `SearchDevin.Mark`.
    public func meta(_ key: String) throws -> String? {
        try storeLock.withLock { try metaValue(key) }
    }

    // MARK: - Mirror copies (#1918)

    /// `meta` key prefix marking a `files` row a mirror copy put in:
    /// `mirror_copy:<path>` = `"<agent> <session row id>"`, swe's
    /// `COPY_MARK`.
    static let copyMarkPrefix = "mirror_copy:"

    static func copyMarkKey(_ path: String) -> String { copyMarkPrefix + path }

    private func deleteMeta(_ key: String) throws {
        try cachedStatement("DELETE FROM meta WHERE key = ?").bind([.text(key)]).run()
    }

    /// Inside a transaction: every row in `rows` still holds the session,
    /// offset and mtime it was read with (swe's `_unchanged`).
    private func unchanged(_ rows: [SearchFileRow]) throws -> Bool {
        guard !rows.isEmpty else { return true }
        let statement = try cachedStatement(
            "SELECT session_row_id, byte_offset, mtime_ms FROM files WHERE path = ?")
        for row in rows {
            statement.bind([.text(row.path)])
            guard try statement.step() else {
                statement.bind([])
                return false
            }
            let same = (statement.isNull(0) ? nil : statement.int(0)) == row.sessionRowID
                && statement.int(1) == row.byteOffset && statement.double(2) == row.mtimeMs
            statement.bind([])
            if !same { return false }
        }
        return true
    }

    /// Parks part of a staged whole read on `host`, a session already
    /// visible for the same session identity, leaving every `files` row as
    /// it is: search keeps seeing one session holding at least what it
    /// held. Fenced like `commitFile` (`path`'s row, `drop`, `gone`, and
    /// the host still there). Returns the ids of the messages written, or
    /// nil with nothing written when anything moved. The caller keeps the
    /// ids, since swe may add its own messages to the same session between
    /// two pieces.
    func stageMessages(path: String, expected: SearchFileFence?, drop: [SearchFileRow],
                       gone: [String], host: Int64, rows: [SearchMessageRow]) throws -> [Int64]? {
        try storeLock.withLock {
            let db = try liveDB()
            try db.execute("BEGIN IMMEDIATE")
            do {
                let actual = try fileFence(for: path)
                guard actual?.sessionRowID == expected?.sessionRowID,
                      actual?.byteOffset == expected?.byteOffset,
                      try unchanged(drop), !gone.contains(where: TranscriptMirror.exists),
                      try sessionRow(host) != nil
                else {
                    try db.execute("ROLLBACK")
                    return nil
                }
                let ids = try insertMessages(under: host, rows)
                try db.execute("COMMIT")
                return ids
            } catch {
                try? db.execute("ROLLBACK")
                throw error
            }
        }
    }

    /// Takes back what a staged read that will not finish parked on
    /// `host`: exactly the messages `ids` names, each only while it is
    /// still on `host`. Never "everything on the session past an id":
    /// swe may have appended to it.
    func discardStaged(host: Int64, ids: [Int64]) throws {
        guard !ids.isEmpty else { return }
        try storeLock.withLock {
            let db = try liveDB()
            try db.execute("BEGIN IMMEDIATE")
            do {
                let delete = try cachedStatement("DELETE FROM messages WHERE id = ? AND session_row_id = ?")
                let deleteFTS = fts5Available
                    ? try cachedStatement("DELETE FROM messages_fts WHERE rowid = ?") : nil
                for id in ids {
                    try delete.bind([.int(id), .int(host)]).run()
                    if db.changes > 0, let deleteFTS { try deleteFTS.bind([.int(id)]).run() }
                }
                try db.execute("COMMIT")
            } catch {
                try? db.execute("ROLLBACK")
                throw error
            }
        }
    }

    /// Deletes `rows` (other files' rows of one session, each under a path
    /// gone from disk) because `keep` stands for the session. One
    /// transaction: `keep` and every row must be unchanged since they were
    /// read and the paths still gone, or nothing is deleted.
    @discardableResult
    func retire(keep: SearchFileRow, rows: [SearchFileRow]) throws -> Bool {
        guard let kept = keep.sessionRowID else { return false }
        return try storeLock.withLock {
            let db = try liveDB()
            try db.execute("BEGIN IMMEDIATE")
            do {
                guard try unchanged([keep] + rows),
                      !rows.map(\.path).contains(where: TranscriptMirror.exists) else {
                    try db.execute("ROLLBACK")
                    return false
                }
                for row in rows {
                    if let session = row.sessionRowID, session != kept {
                        try deleteSession(session)
                        try setOrphansPending()
                    }
                    try cachedStatement("DELETE FROM files WHERE path = ?").bind([.text(row.path)]).run()
                    try deleteMeta(Self.copyMarkKey(row.path))
                }
                try db.execute("COMMIT")
                return true
            } catch {
                try? db.execute("ROLLBACK")
                throw error
            }
        }
    }

    /// The rows a mirror copy put in, by session identity: a range seek on
    /// `meta`. A mark speaks only while its session is still the row's,
    /// which the caller checks.
    func copyMarks() throws -> [TranscriptMirror.SessionIdentity: [(path: String, session: Int64)]] {
        try storeLock.withLock {
            // ':' + 1 is ';', so the range is exactly the prefix.
            let statement = try cachedStatement("SELECT key, value FROM meta WHERE key >= ? AND key < ?")
            statement.bind([.text(Self.copyMarkPrefix), .text("mirror_copy;")])
            defer { statement.bind([]) }
            var out: [TranscriptMirror.SessionIdentity: [(path: String, session: Int64)]] = [:]
            while try statement.step() {
                guard let key = statement.text(0), let value = statement.text(1) else { continue }
                let parts = value.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: false)
                guard parts.count == 2, TranscriptMirror.harnesses.contains(String(parts[0])),
                      !parts[1].isEmpty, parts[1].allSatisfy({ $0.isASCII && $0.isNumber }),
                      let session = Int64(parts[1]) else { continue }
                let agent = String(parts[0])
                let path = String(key.dropFirst(Self.copyMarkPrefix.count))
                out[TranscriptMirror.SessionIdentity(harness: agent, path: path), default: []]
                    .append((path, session))
            }
            return out
        }
    }

    /// Every `files` row with a session of one of `agents`, by
    /// `(agent, identity of its path)`: one read of the table, for the
    /// copies whose own row did not settle them (swe's `session_rows`).
    func sessionRows(agents: [String]) throws -> [TranscriptMirror.SessionIdentity: [SearchFileRow]] {
        guard !agents.isEmpty else { return [:] }
        return try storeLock.withLock {
            let marks = Array(repeating: "?", count: agents.count).joined(separator: ",")
            let statement = try liveDB().prepare("""
                SELECT f.path, f.dev, f.ino, f.byte_offset, f.mtime_ms, f.size_bytes, f.session_row_id,
                       f.state, f.fail_count, f.failed_mtime_ms, s.agent
                FROM files f JOIN sessions s ON s.id = f.session_row_id
                WHERE s.agent IN (\(marks))
                """)
            statement.bind(agents.map { .text($0) })
            var out: [TranscriptMirror.SessionIdentity: [SearchFileRow]] = [:]
            while try statement.step() {
                let row = Self.fileRow(statement)
                let agent = statement.text(10) ?? ""
                out[TranscriptMirror.SessionIdentity(harness: agent, path: row.path), default: []].append(row)
            }
            return out
        }
    }

    /// Rows in `messages` and the FTS rows with the same ids, in the caller's
    /// transaction, so a message is never present in one and absent in the
    /// other.
    /// - Returns: the new message ids, in order.
    @discardableResult
    private func insertMessages(under sessionRowID: Int64, _ messages: [SearchMessageRow]) throws -> [Int64] {
        guard !messages.isEmpty else { return [] }
        let insertMessage = try cachedStatement(
            "INSERT INTO messages(session_row_id, role, ts) VALUES (?, ?, ?)")
        let insertFTS = fts5Available ? try cachedStatement("""
            INSERT INTO messages_fts(rowid, user_text, assistant_text, tool_text, identifiers)
            VALUES (?, ?, ?, ?, ?)
            """) : nil
        var ids: [Int64] = []
        ids.reserveCapacity(messages.count)
        for message in messages {
            try insertMessage.bind([.int(sessionRowID), .text(message.role),
                                    message.ts.map(SQLiteValue.text) ?? .null]).run()
            let rowID = try liveDB().lastInsertRowID
            ids.append(rowID)
            if let insertFTS {
                try insertFTS.bind([.int(rowID), .text(message.userText),
                                    .text(message.assistantText), .text(message.toolText),
                                    .text(message.identifiers)]).run()
            }
        }
        return ids
    }

    // MARK: - Orphan drain

    /// One `meta` row, or nil.
    private func metaValue(_ key: String) throws -> String? {
        let statement = try cachedStatement("SELECT value FROM meta WHERE key = ?")
        statement.bind([.text(key)])
        guard try statement.step() else { return nil }
        let value = statement.text(0)
        statement.bind([])
        return value
    }

    private func setMeta(_ key: String, _ value: String) throws {
        try cachedStatement("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)")
            .bind([.text(key), .text(value)]).run()
    }

    /// Every delete of a `sessions` row marks this, in the same transaction:
    /// it's what lets `drainOrphanedMessages` skip its O(messages) probe on
    /// scans where nothing was orphaned — the common case.
    private func setOrphansPending() throws {
        try setMeta("orphans_pending", "1")
    }

    /// Reclaims up to `limit` rows of one orphaned session's messages — a set
    /// a replace, a removed source or an interrupted earlier drain leaves
    /// behind — in one transaction, then hands pages back in a bounded step.
    /// `auto_vacuum = INCREMENTAL` holds every freed page until asked, and
    /// asking in step with the deletes is many short stalls instead of one
    /// long one at the end.
    ///
    /// The `orphans_pending` flag records the work: every session delete
    /// raises it in the same transaction and a clean probe lowers it, so a
    /// crash mid-drain is found by the next one. An index written before the
    /// flag existed has it unset — nil probes once so old orphans surface.
    ///
    /// - Returns: rows deleted this call; call repeatedly until it returns 0.
    @discardableResult
    public func drainOrphanedMessages(limit: Int = 256) throws -> Int {
        try storeLock.withLock {
            if try metaValue("orphans_pending") == "0" { return 0 }
            let db = try liveDB()
            // The probe and the flag reset share one write transaction, as in
            // swe's `drain_orphans`: swe also writes this index, and a session
            // it deletes between them would raise the flag just before this
            // lowered it, leaving its messages until some later delete.
            try db.execute("BEGIN IMMEDIATE")
            do {
                // Reset once read: the probe is cached, never finalized, and a
                // statement left on a row keeps a read cursor on messages open
                // past the commit, which holds the WAL checkpoint back (and,
                // when the batch limit ends the drain, until the next scan).
                let orphan: Int64? = try {
                    let probe = try cachedStatement("""
                        SELECT session_row_id FROM messages
                        WHERE session_row_id NOT IN (SELECT id FROM sessions) LIMIT 1
                        """)
                    probe.bind([])
                    let id: Int64? = try probe.step() ? probe.int(0) : nil
                    probe.bind([])
                    return id
                }()
                guard let orphan else {
                    try setMeta("orphans_pending", "0")
                    try db.execute("COMMIT")
                    try db.execute("PRAGMA incremental_vacuum(2000)")
                    return 0
                }
                let ids: [Int64] = try {
                    let pick = try cachedStatement(
                        "SELECT id FROM messages WHERE session_row_id = ? LIMIT ?")
                    pick.bind([.int(orphan), .int(limit)])
                    var ids: [Int64] = []
                    while try pick.step() { ids.append(pick.int(0)) }
                    return ids
                }()
                let deleteFTS = fts5Available
                    ? try cachedStatement("DELETE FROM messages_fts WHERE rowid = ?") : nil
                let deleteMessage = try cachedStatement("DELETE FROM messages WHERE id = ?")
                for id in ids {
                    if let deleteFTS {
                        try deleteFTS.bind([.int(id)]).run()
                    }
                    try deleteMessage.bind([.int(id)]).run()
                }
                try db.execute("COMMIT")
                try db.execute("PRAGMA incremental_vacuum(2000)")
                return ids.count
            } catch {
                try? db.execute("ROLLBACK")
                throw error
            }
        }
    }

    // MARK: - Status and teardown

    /// Rows per file state and the session and message totals. The status is
    /// these queries; a caller polls it rather than tracking progress itself.
    ///
    /// Run on the read-only connection inside the lock: polling mid-scan
    /// reads the last committed rows without stalling the writer.
    public func status() throws -> SearchStatus {
        try statusConnection.withLock { connection throws -> SearchStatus in
            if connection == nil {
                let path = url.path
                    .addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? url.path
                connection = try SQLiteDatabase(readOnlyURI: "file:\(path)?mode=ro")
            }
            return try readStatus(from: connection!)
        }
    }

    private func readStatus(from db: SQLiteDatabase) throws -> SearchStatus {
        var status = SearchStatus()
        let states = try db.prepare("SELECT state, count(*) FROM files GROUP BY state")
        while try states.step() {
            if let state = SearchFileState(rawValue: states.text(0) ?? "") {
                status.filesByState[state] = Int(states.int(1))
            }
        }
        let agents = try db.prepare("SELECT agent, count(*) FROM sessions GROUP BY agent")
        while try agents.step() {
            if let agent = agents.text(0) {
                status.sessionsByAgent[agent] = Int(agents.int(1))
            }
        }
        status.sessionCount = Int(try db.scalar("SELECT count(*) FROM sessions"))
        status.messageCount = Int(try db.scalar("SELECT count(*) FROM messages"))
        return status
    }

    /// Closes the connections and deletes the database with every journal
    /// file beside it. The store is unusable afterwards: under `storeLock`
    /// no method can be inside a call, and every later one throws.
    public func clear() {
        storeLock.withLock {
            // A poll holding the read connection would survive the unlink
            // and keep reading the old file; drop it first.
            statusConnection.withLock { $0 = nil }
            // Finalized before the handle closes, same ordering as `deinit`.
            statements.removeAll()
            db = nil
            SearchLocation.removeDatabase(at: url)
        }
    }
}
