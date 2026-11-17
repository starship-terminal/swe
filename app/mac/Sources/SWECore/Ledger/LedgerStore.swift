import Foundation

/// Where a file was read up to, and enough of its content to tell an appended
/// file (resume at `offset`) from a replaced or truncated one (read it again).
struct FileCursor: Equatable {
    var path: String
    var inode: UInt64
    var size: UInt64
    var modified: Double
    /// The file's ctime at scan time, catching rewrites that preserve size
    /// and mtime. nil on cursors saved before it was recorded; those keep
    /// the inode+size+mtime test they were written under.
    var changed: Double? = nil
    var offset: UInt64
    /// Hash of the first `headLength` bytes. Those bytes sit before `offset`,
    /// so appending never changes them.
    var headHash: Int64
    var headLength: Int
    /// Hash of the `tailLength` bytes that end at `offset`.
    var tailHash: Int64
    var tailLength: Int
    /// Hash of a block centered in the read range, closing the gap between
    /// head and tail where a same-length edit could hide. nil on cursors
    /// saved before it existed: those rescan once when head and tail do
    /// not already cover the whole file.
    var midHash: Int64? = nil
    /// The parser's state at `offset`, for parsers that carry state between lines.
    var state: Data?
    var skippedLines: Int
}

/// The persisted ledger: one row per deduplicated response, plus per-file
/// cursors. SQLite, so a scan commits a file's events and its cursor in one
/// transaction (a crash cannot count a line twice), memory stays flat however
/// long the history is, and the rows outlive the logs they came from.
///
/// Rows hold token counts, not dollars. Cost is applied when queried, at
/// the rate each event's price entry had at its time (the catalog keeps
/// every price a key held), so the long-context and 1-hour-cache rules can
/// still see each request on its own and a price change never reprices
/// the usage logged before it.
final class LedgerStore {
    /// Bump when parsing changes what a row means. A mismatch drops the rows
    /// and cursors and the next scan rebuilds them, so history whose logs are
    /// gone is lost: bump only when the old rows are wrong.
    static let schemaVersion = 1

    let db: SQLiteDatabase
    private var dimIDs: [DimKey: Int64] = [:]
    private var selectByKey: Statement!
    private var selectByMessage: Statement!
    private var insertEvent: Statement!
    private var deleteEvent: Statement!
    private var selectDim: Statement!
    private var insertDim: Statement!
    private var deleteSameEvent: Statement!
    private var selectSameEventAnyProfile: Statement!
    private var selectDimByID: Statement!
    private var updateEventDim: Statement!
    private var deleteDimIfUnused: Statement!
    /// Dimension rows a relabel moved their last event out of, until
    /// `dropVacatedDims` removes the ones left with no events.
    private var vacatedDims: Set<Int64> = []
    /// Usage events inserted, changed or deleted since the store opened:
    /// every `upsert` that wrote and every `delete(key:)` that removed a
    /// row. A scan reports its own share so the Overview rebuilds only
    /// when the figures can have moved. Writes later rolled back still
    /// count, which costs at most one needless rebuild.
    private(set) var eventWrites = 0

    struct DimKey: Hashable {
        var harness, account, session, cwd, model, tier: String
    }

    init(url: URL) throws {
        // The ledger names projects and sessions, so only the user reads it.
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        db = try SQLiteDatabase(url: url)
        try db.execute("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = OFF;")
        try db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        // Scoped: an unfinalized statement that read a row keeps a cursor on
        // meta open, which locks the INSERT below with SQLITE_LOCKED.
        let stored: String? = try {
            let version = try db.prepare("SELECT value FROM meta WHERE key = 'schema'")
            return try version.step() ? version.text(0) : nil
        }()
        if stored != String(Self.schemaVersion) {
            try archiveOldTables(at: url, stored: stored)
            try db.execute("DROP TABLE IF EXISTS events; DROP TABLE IF EXISTS dims; DROP TABLE IF EXISTS cursors;")
        }
        try db.execute("""
            CREATE TABLE IF NOT EXISTS dims (
                id INTEGER PRIMARY KEY,
                harness TEXT NOT NULL, account TEXT NOT NULL, session TEXT NOT NULL,
                cwd TEXT NOT NULL, model TEXT NOT NULL, tier TEXT NOT NULL,
                UNIQUE (harness, account, session, cwd, model, tier));
            CREATE TABLE IF NOT EXISTS events (
                key INTEGER PRIMARY KEY,
                msg INTEGER,
                sidechain INTEGER NOT NULL,
                dim INTEGER NOT NULL,
                ts REAL NOT NULL,
                input INTEGER NOT NULL, output INTEGER NOT NULL,
                cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, cache_write_1h INTEGER NOT NULL,
                reasoning INTEGER NOT NULL,
                cost REAL);
            CREATE INDEX IF NOT EXISTS events_dim ON events (dim);
            CREATE INDEX IF NOT EXISTS events_msg ON events (msg) WHERE msg IS NOT NULL;
            CREATE TABLE IF NOT EXISTS cursors (
                path TEXT PRIMARY KEY, inode INTEGER NOT NULL, size INTEGER NOT NULL, modified REAL NOT NULL,
                changed REAL, offset INTEGER NOT NULL, head_hash INTEGER NOT NULL, head_length INTEGER NOT NULL,
                tail_hash INTEGER NOT NULL, tail_length INTEGER NOT NULL, mid_hash INTEGER,
                state BLOB, skipped INTEGER NOT NULL);
            INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', '\(Self.schemaVersion)');
            """)
        // Session-total events never count toward long-context surcharges.
        // Old rows are correct with the 0 default, so no schema bump.
        try? db.execute("ALTER TABLE events ADD COLUMN session_total INTEGER NOT NULL DEFAULT 0")
        // Every figure is a time-range read of these columns. The rows sit
        // in key (hash) order, so a plain `ts` index cost a table lookup
        // per row, scattered over the whole file; this one answers the
        // range reads alone, at about half the CPU (#1568). It replaces
        // `events_ts`, whose `ts` prefix it has; built once, about a
        // second on a 400,000-event ledger.
        try db.execute("""
            CREATE INDEX IF NOT EXISTS events_ts_cover ON events (ts, dim, input, output, cache_read,
                cache_write, cache_write_1h, reasoning, session_total, cost);
            DROP INDEX IF EXISTS events_ts;
            """)
        // The cursor fingerprint grew a ctime stamp and a middle sample.
        // Old rows keep NULL: a NULL `changed` is checked the old way and a
        // NULL `mid_hash` fails closed into one rescan.
        try? db.execute("ALTER TABLE cursors ADD COLUMN changed REAL")
        try? db.execute("ALTER TABLE cursors ADD COLUMN mid_hash INTEGER")
        selectByKey = try db.prepare("""
            SELECT sidechain, input, output, cache_read, cache_write, cache_write_1h,
                reasoning, session_total, cost, ts, dim FROM events WHERE key = ?
            """)
        selectByMessage = try db.prepare("""
            SELECT key, sidechain, input + output + cache_read + cache_write + reasoning
            FROM events WHERE msg = ? AND (sidechain = 1 OR ? = 1) LIMIT 1
            """)
        insertEvent = try db.prepare("""
            INSERT OR REPLACE INTO events (key, msg, sidechain, dim, ts, input, output, cache_read, cache_write,
                cache_write_1h, reasoning, session_total, cost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """)
        deleteEvent = try db.prepare("DELETE FROM events WHERE key = ?")
        selectDim = try db.prepare(
            "SELECT id FROM dims WHERE harness = ? AND account = ? AND session = ? AND cwd = ? AND model = ? AND tier = ?")
        insertDim = try db.prepare(
            "INSERT INTO dims (harness, account, session, cwd, model, tier) VALUES (?, ?, ?, ?, ?, ?)")
        deleteSameEvent = try db.prepare(
            "DELETE FROM events WHERE dim = ? AND ts = ? AND key != ? AND msg IS NULL")
        selectDimByID = try db.prepare(
            "SELECT harness, account, session, cwd, model, tier FROM dims WHERE id = ?")
        updateEventDim = try db.prepare("UPDATE events SET dim = ? WHERE key = ?")
        deleteDimIfUnused = try db.prepare(
            "DELETE FROM dims WHERE id = ? AND NOT EXISTS (SELECT 1 FROM events WHERE dim = ?)")
        // For accounts several profiles can read (the Cursor feed when
        // swe names an account_id), the same event's stale rows sit under
        // whichever profile imported them — dims.account names the reader,
        // not the account. The candidates share the event's identity; the
        // key check afterwards confirms they belong to the same account.
        selectSameEventAnyProfile = try db.prepare("""
            SELECT e.key, e.input, e.output FROM events e JOIN dims d ON d.id = e.dim
            WHERE e.ts = ? AND e.key != ? AND e.msg IS NULL
                AND d.harness = ? AND d.session = ? AND d.cwd = ? AND d.model = ? AND d.tier = ?
            """)
    }

    /// A schema mismatch drops the tables below and the next scan rebuilds
    /// them, which loses rows whose logs are gone. Copy the database first:
    /// it stays beside the live one as `ledger.schema-<version>.sqlite` (a
    /// `.N` suffix when one exists already). Best effort: a failed copy
    /// never blocks the rebuild.
    private func archiveOldTables(at url: URL, stored: String?) throws {
        let hasEvents = try db.prepare(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).step()
        guard hasEvents else { return }
        let tag = String((stored ?? "unknown").map {
            $0.isLetter || $0.isNumber || $0 == "-" || $0 == "." ? $0 : "_"
        })
        let directory = url.deletingLastPathComponent()
        let stem = url.deletingPathExtension().lastPathComponent
        var candidate = directory.appending(path: "\(stem).schema-\(tag).sqlite")
        var copy = 1
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = directory.appending(path: "\(stem).schema-\(tag).\(copy).sqlite")
            copy += 1
        }
        let escaped = candidate.path.replacingOccurrences(of: "'", with: "''")
        try? db.execute("VACUUM INTO '\(escaped)'")
    }

    // MARK: - Events

    /// Adds a record unless the ledger already holds the same response. On a
    /// collision the non-sidechain copy wins, then the larger token total.
    /// - Returns: true when a row was inserted or changed; a re-fetch of
    ///   the same event under the same key is a no-op and returns false.
    @discardableResult
    func upsert(_ record: LedgerRecord) throws -> Bool {
        let wrote = try upsertRow(record)
        if wrote { eventWrites += 1 }
        return wrote
    }

    private func upsertRow(_ record: LedgerRecord) throws -> Bool {
        let key = StableHash.of(record.key)
        let total = Int64(record.event.totalTokens)
        // A cached statement left on a row holds a read transaction open
        // past this call's COMMIT or ROLLBACK: the next BEGIN IMMEDIATE
        // then fails "database is locked" once another connection has
        // written, and the WAL can't be checkpointed. Reset on the way out,
        // as relabelModel does.
        selectByKey.bind([.int(key)])
        defer { selectByKey.bind([]) }
        if try selectByKey.step() {
            let existingSidechain = selectByKey.int(0) != 0
            let existingDim = selectByKey.int(10)
            if record.authoritative {
                // The source's own figure always wins, but only a changed
                // figure is a write.
                guard try !sameEvent(selectByKey, as: record) else { return false }
                try write(record, key: key)
                return true
            }
            let existingTotal = selectByKey.int(1) + selectByKey.int(2)
                + selectByKey.int(3) + selectByKey.int(4) + selectByKey.int(6)
            if Self.shouldReplace(record.isSidechain, total, existingSidechain, existingTotal) {
                let dim = try write(record, key: key)
                if record.relabelsModel, dim != existingDim { vacatedDims.insert(existingDim) }
                return true
            }
            if record.relabelsModel {
                return try relabelModel(key: key, existingDim: existingDim, to: record.event.model)
            }
            return false
        }
        if let messageId = record.messageId {
            selectByMessage.bind([.int(StableHash.of(messageId)), .int(record.isSidechain ? 1 : 0)])
            defer { selectByMessage.bind([]) }
            if try selectByMessage.step() {
                let otherKey = selectByMessage.int(0)
                let otherSidechain = selectByMessage.int(1) != 0
                let otherTotal = selectByMessage.int(2)
                if Self.shouldReplace(record.isSidechain, total, otherSidechain, otherTotal) {
                    try deleteEvent.bind([.int(otherKey)]).run()
                    try write(record, key: key)
                    return true
                }
                return false
            }
        }
        let dim = try write(record, key: key)
        if record.authoritative, !record.skipsLookalikes {
            // An authoritative record is the source's latest word on its
            // event. If an older key scheme left a row for the same event
            // behind — the Cursor feed once keyed on the token counts it
            // reported, so a correction keyed a new row — remove it or the
            // correction double-counts.
            let e = record.event
            if record.accountSpansProfiles {
                // The old counts-bearing key is this record's key plus
                // ":\(input):\(output)". Rehashing it with a candidate's
                // own counts reproduces its key only when the candidate
                // is the same account's same event — another account's
                // lookalike row (same day bucket, model and instant)
                // keeps its own account in the string and is left alone.
                selectSameEventAnyProfile.bind([
                    .double(e.timestamp.timeIntervalSince1970), .int(key),
                    .text(e.harness), .text(e.sessionId), .text(e.cwd ?? ""),
                    .text(e.model), .text(e.serviceTier ?? ""),
                ])
                var staleKeys: [Int64] = []
                while try selectSameEventAnyProfile.step() {
                    let stale = StableHash.of(
                        "\(record.key):\(selectSameEventAnyProfile.int(1)):" +
                            "\(selectSameEventAnyProfile.int(2))")
                    if stale == selectSameEventAnyProfile.int(0) { staleKeys.append(stale) }
                }
                selectSameEventAnyProfile.bind([])
                for stale in staleKeys {
                    try deleteEvent.bind([.int(stale)]).run()
                }
            } else {
                try deleteSameEvent.bind([
                    .int(dim), .double(e.timestamp.timeIntervalSince1970), .int(key),
                ]).run()
            }
        }
        return true
    }

    /// Moves the event under `key` to the dimension row that differs from
    /// its own only in `model`. Totals, cost and time stay as they are.
    /// - Returns: true when the model changed.
    private func relabelModel(key: Int64, existingDim: Int64, to model: String) throws -> Bool {
        selectDimByID.bind([.int(existingDim)])
        defer { selectDimByID.bind([]) }
        guard try selectDimByID.step(), selectDimByID.text(4) != model else { return false }
        let moved = DimKey(
            harness: selectDimByID.text(0) ?? "", account: selectDimByID.text(1) ?? "",
            session: selectDimByID.text(2) ?? "", cwd: selectDimByID.text(3) ?? "",
            model: model, tier: selectDimByID.text(5) ?? "")
        let dim = try dimID(moved)
        try updateEventDim.bind([.int(dim), .int(key)]).run()
        vacatedDims.insert(existingDim)
        return true
    }

    /// Removes the dimension rows `relabelModel` emptied. Without it the
    /// old model names stay in `dims`, which `modelsAndTiers` lists. Call
    /// it inside the transaction that relabelled.
    func dropVacatedDims() throws {
        guard !vacatedDims.isEmpty else { return }
        for id in vacatedDims {
            try deleteDimIfUnused.bind([.int(id), .int(id)]).run()
        }
        vacatedDims.removeAll()
        // A cached id may name a row just removed.
        dimIDs.removeAll()
    }

    /// True when the row `selectByKey` just stepped to stores the same
    /// figures and dimension as `record`. Only meaningful while the
    /// statement's cursor is on that row.
    private func sameEvent(_ row: Statement, as record: LedgerRecord) throws -> Bool {
        let e = record.event
        let costMatches = row.isNull(8)
            ? e.costUSD == nil
            : e.costUSD != nil && row.double(8) == e.costUSD
        // The dim carries model, cwd, account and tier: an authoritative
        // re-read where only those moved is a change, not a duplicate.
        let dim = try dimID(DimKey(
            harness: e.harness, account: e.accountHome, session: e.sessionId,
            cwd: e.cwd ?? "", model: e.model, tier: e.serviceTier ?? ""))
        return row.int(0) == Int64(record.isSidechain ? 1 : 0)
            && row.int(1) == Int64(e.inputTokens)
            && row.int(2) == Int64(e.outputTokens)
            && row.int(3) == Int64(e.cacheReadTokens)
            && row.int(4) == Int64(e.cacheWriteTokens)
            && row.int(5) == Int64(e.cacheWrite1hTokens)
            && row.int(6) == Int64(e.reasoningTokens)
            && row.int(7) == Int64(e.isSessionTotal ? 1 : 0)
            && costMatches
            && row.double(9) == e.timestamp.timeIntervalSince1970
            && row.int(10) == dim
    }

    private static func shouldReplace(
        _ candidateSidechain: Bool, _ candidateTotal: Int64, _ existingSidechain: Bool, _ existingTotal: Int64
    ) -> Bool {
        if candidateSidechain != existingSidechain { return existingSidechain }
        return candidateTotal > existingTotal
    }

    /// - Returns: the event's dimension row id.
    @discardableResult
    private func write(_ record: LedgerRecord, key: Int64) throws -> Int64 {
        let e = record.event
        let dim = try dimID(DimKey(
            harness: e.harness, account: e.accountHome, session: e.sessionId,
            cwd: e.cwd ?? "", model: e.model, tier: e.serviceTier ?? ""))
        try insertEvent.bind([
            .int(key), record.messageId.map { .int(StableHash.of($0)) } ?? .null,
            .int(record.isSidechain ? 1 : 0), .int(dim), .double(e.timestamp.timeIntervalSince1970),
            .int(e.inputTokens), .int(e.outputTokens), .int(e.cacheReadTokens), .int(e.cacheWriteTokens),
            .int(e.cacheWrite1hTokens), .int(e.reasoningTokens),
            .int(e.isSessionTotal ? 1 : 0), .optionalDouble(e.costUSD),
        ]).run()
        return dim
    }

    /// Drops the event under `key` when there is one. Used by session-total
    /// sources whose file now reports nothing, so stale spend clears.
    func delete(key: String) throws {
        try deleteEvent.bind([.int(StableHash.of(key))]).run()
        if db.changes > 0 { eventWrites += 1 }
    }

    /// Abandons the open transaction. Dimension ids inserted in it are gone
    /// from the database, so they must not stay cached either.
    func rollback() {
        try? db.execute("ROLLBACK")
        dimIDs.removeAll()
        vacatedDims.removeAll()
    }

    private func dimID(_ key: DimKey) throws -> Int64 {
        if let id = dimIDs[key] { return id }
        let values: [SQLiteValue?] = [
            .text(key.harness), .text(key.account), .text(key.session),
            .text(key.cwd), .text(key.model), .text(key.tier),
        ]
        selectDim.bind(values)
        let id: Int64
        if try selectDim.step() {
            id = selectDim.int(0)
            selectDim.bind([])
        } else {
            try insertDim.bind(values).run()
            id = db.lastInsertRowID
        }
        dimIDs[key] = id
        return id
    }

    // MARK: - Cursors

    func cursor(for path: String) throws -> FileCursor? {
        let statement = try db.prepare("""
            SELECT inode, size, modified, changed, offset, head_hash, head_length,
                tail_hash, tail_length, mid_hash, state, skipped
            FROM cursors WHERE path = ?
            """)
        statement.bind([.text(path)])
        guard try statement.step() else { return nil }
        return FileCursor(
            path: path, inode: UInt64(bitPattern: statement.int(0)), size: UInt64(statement.int(1)),
            modified: statement.double(2),
            changed: statement.isNull(3) ? nil : statement.double(3),
            offset: UInt64(statement.int(4)),
            headHash: statement.int(5), headLength: Int(statement.int(6)),
            tailHash: statement.int(7), tailLength: Int(statement.int(8)),
            midHash: statement.isNull(9) ? nil : statement.int(9),
            state: statement.blob(10), skippedLines: Int(statement.int(11)))
    }

    func save(_ cursor: FileCursor) throws {
        try db.prepare("""
            INSERT OR REPLACE INTO cursors
            (path, inode, size, modified, changed, offset, head_hash, head_length,
             tail_hash, tail_length, mid_hash, state, skipped)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """).bind([
                .text(cursor.path), .int(Int64(bitPattern: cursor.inode)), .int(Int64(cursor.size)),
                .double(cursor.modified), cursor.changed.map(SQLiteValue.double) ?? .null,
                .int(Int64(cursor.offset)), .int(cursor.headHash),
                .int(cursor.headLength), .int(cursor.tailHash), .int(cursor.tailLength),
                cursor.midHash.map(SQLiteValue.int) ?? .null,
                cursor.state.map(SQLiteValue.blob) ?? .null, .int(cursor.skippedLines),
            ]).run()
    }

    /// Generic key-value state for sources that are not files (SQLite databases).
    func meta(_ key: String) throws -> String? {
        let statement = try db.prepare("SELECT value FROM meta WHERE key = ?")
        statement.bind([.text(key)])
        return try statement.step() ? statement.text(0) : nil
    }

    func setMeta(_ key: String, _ value: String) throws {
        try db.prepare("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)").bind([.text(key), .text(value)]).run()
    }
}

// MARK: - Queries

extension LedgerStore {
    enum Column: String {
        case harness, account, session, cwd, model
    }

    /// One group of rows that share their dimensions and every input the price
    /// depends on: model, tier, whether the log carried its own cost, and how
    /// many long-context thresholds the request's prompt crossed.
    struct PricedGroup {
        var dimensions: [Column: String]
        var model: String
        var tier: String?
        var hasLoggedCost: Bool
        var thresholdsCrossed: Int
        var tokens: TokenSums
        var loggedCost: Double
        var count: Int
        var first: Date
        var last: Date
        /// With `bucketSeconds`, the bucket's start.
        var bucket: Date?
    }

    struct TokenSums: Equatable {
        var input = 0, output = 0, cacheRead = 0, cacheWrite = 0, cacheWrite1h = 0, reasoning = 0
        var total: Int { input + output + cacheRead + cacheWrite + reasoning }
    }

    /// - Parameter bucketSeconds: also split each group into time buckets of
    ///   this many seconds since the epoch.
    /// - Parameter harness: restrict to one harness's dims at the SQL side,
    ///   so a caller after one harness does not read the whole table.
    /// - Parameter priceChanges: also split each group at these moments
    ///   (seconds since the epoch, ascending), so no group spans a price
    ///   change and each prices at the rate of its own time.
    func groups(in range: DateInterval, by columns: [Column], thresholds: [Int],
                bucketSeconds: Int? = nil, harness: String? = nil,
                priceChanges: [Double] = []) throws -> [PricedGroup] {
        try db.pricedGroups(in: range, by: columns, thresholds: thresholds, bucketSeconds: bucketSeconds,
                            harness: harness, priceChanges: priceChanges)
    }

    /// Every model and tier the ledger has a row for.
    func modelsAndTiers() throws -> [(model: String, tier: String)] {
        try db.modelsAndTiers()
    }

    func events(in range: DateInterval) throws -> [UsageEvent] {
        let statement = try db.prepare("""
            SELECT d.harness, d.account, d.session, d.cwd, d.model, d.tier, e.ts,
                e.input, e.output, e.cache_read, e.cache_write, e.cache_write_1h, e.reasoning,
                e.session_total, e.cost
            FROM events e JOIN dims d ON d.id = e.dim
            WHERE e.ts >= ? AND e.ts < ? ORDER BY e.ts, e.key
            """)
        statement.bind([.double(range.start.timeIntervalSince1970), .double(range.end.timeIntervalSince1970)])
        var events: [UsageEvent] = []
        while try statement.step() {
            let cwd = statement.text(3) ?? ""
            let tier = statement.text(5) ?? ""
            events.append(UsageEvent(
                harness: statement.text(0) ?? "", accountHome: statement.text(1) ?? "",
                sessionId: statement.text(2) ?? "", cwd: cwd.isEmpty ? nil : cwd,
                model: statement.text(4) ?? "", timestamp: Date(timeIntervalSince1970: statement.double(6)),
                inputTokens: Int(statement.int(7)), outputTokens: Int(statement.int(8)),
                cacheReadTokens: Int(statement.int(9)), cacheWriteTokens: Int(statement.int(10)),
                cacheWrite1hTokens: Int(statement.int(11)), reasoningTokens: Int(statement.int(12)),
                costUSD: statement.isNull(14) ? nil : statement.double(14),
                serviceTier: tier.isEmpty ? nil : tier,
                isSessionTotal: statement.int(13) != 0))
        }
        return events
    }
}

/// The grouped reads on any connection to the ledger file: the store's
/// write connection, or `Ledger`'s read-only one that never queues
/// behind a scan.
extension SQLiteDatabase {
    func pricedGroups(in range: DateInterval, by columns: [LedgerStore.Column], thresholds: [Int],
                bucketSeconds: Int? = nil, harness: String? = nil,
                priceChanges: [Double] = []) throws -> [LedgerStore.PricedGroup] {
        let bucket = bucketSeconds.map { "CAST(e.ts / \($0) AS INTEGER)" }
        let era = priceChanges.isEmpty
            ? "0"
            : priceChanges.map { "(e.ts >= \($0))" }.joined(separator: " + ")
        let dims = columns.map { "d.\($0.rawValue)" }
        let crossed = thresholds.isEmpty
            ? "0"
            : thresholds.map { "(e.session_total = 0 AND e.input + e.cache_read + e.cache_write > \($0))" }
                .joined(separator: " + ")
        // Two passes (#1568): the events group first by their integer
        // `dim` and price inputs, straight off the covering index with no
        // join per row, and only those groups join `dims` to merge by the
        // strings asked for. Sums, counts and min/max compose, so the
        // groups are the one-pass query's.
        let inner = ["e.dim AS dim", "(e.cost IS NOT NULL) AS logged", "\(crossed) AS crossed", "\(era) AS era"]
            + (bucket.map { ["\($0) AS bucket"] } ?? [])
        let innerGroup = ["e.dim", "logged", "crossed", "era"] + (bucket.map { _ in ["bucket"] } ?? [])
        let outer = dims + ["d.model", "d.tier", "g.logged", "g.crossed", "g.era"]
            + (bucket.map { _ in ["g.bucket"] } ?? [])
        let statement = try prepare("""
            SELECT \(outer.joined(separator: ", ")),
                SUM(g.input), SUM(g.output), SUM(g.cache_read), SUM(g.cache_write), SUM(g.cache_write_1h),
                SUM(g.reasoning), TOTAL(g.cost), SUM(g.n), MIN(g.first), MAX(g.last)
            FROM (
                SELECT \(inner.joined(separator: ", ")),
                    SUM(e.input) AS input, SUM(e.output) AS output, SUM(e.cache_read) AS cache_read,
                    SUM(e.cache_write) AS cache_write, SUM(e.cache_write_1h) AS cache_write_1h,
                    SUM(e.reasoning) AS reasoning, TOTAL(e.cost) AS cost, COUNT(*) AS n,
                    MIN(e.ts) AS first, MAX(e.ts) AS last
                FROM events e
                WHERE e.ts >= ? AND e.ts < ?
                \(harness.map { _ in "AND e.dim IN (SELECT id FROM dims WHERE harness = ?)" } ?? "")
                GROUP BY \(innerGroup.joined(separator: ", "))
            ) g JOIN dims d ON d.id = g.dim
            GROUP BY \(outer.joined(separator: ", "))
            """)
        statement.bind([.double(range.start.timeIntervalSince1970), .double(range.end.timeIntervalSince1970)]
                       + (harness.map { [SQLiteValue.text($0)] } ?? []))
        var result: [LedgerStore.PricedGroup] = []
        while try statement.step() {
            var dimensions: [LedgerStore.Column: String] = [:]
            for (index, column) in columns.enumerated() {
                dimensions[column] = statement.text(Int32(index)) ?? ""
            }
            var i = Int32(columns.count)
            func next() -> Int32 { defer { i += 1 }; return i }
            let model = statement.text(next()) ?? ""
            let tier = statement.text(next()) ?? ""
            let logged = statement.int(next()) != 0
            let crossedCount = Int(statement.int(next()))
            _ = next() // era: only splits the groups; each prices at its own `first`.
            let bucketStart = bucketSeconds.map { size in
                Date(timeIntervalSince1970: Double(statement.int(next())) * Double(size))
            }
            let sums = LedgerStore.TokenSums(
                input: Int(statement.int(next())), output: Int(statement.int(next())),
                cacheRead: Int(statement.int(next())), cacheWrite: Int(statement.int(next())),
                cacheWrite1h: Int(statement.int(next())), reasoning: Int(statement.int(next())))
            let cost = statement.double(next())
            let count = Int(statement.int(next()))
            let first = Date(timeIntervalSince1970: statement.double(next()))
            let last = Date(timeIntervalSince1970: statement.double(next()))
            result.append(LedgerStore.PricedGroup(
                dimensions: dimensions, model: model, tier: tier.isEmpty ? nil : tier,
                hasLoggedCost: logged, thresholdsCrossed: crossedCount, tokens: sums,
                loggedCost: cost, count: count, first: first, last: last, bucket: bucketStart))
        }
        return result
    }

    /// Every model and tier the ledger has a row for.
    func modelsAndTiers() throws -> [(model: String, tier: String)] {
        let statement = try prepare("SELECT DISTINCT model, tier FROM dims")
        var result: [(model: String, tier: String)] = []
        while try statement.step() { result.append((statement.text(0) ?? "", statement.text(1) ?? "")) }
        return result
    }
}
