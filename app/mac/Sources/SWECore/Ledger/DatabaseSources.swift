import Foundation

/// Harnesses that keep history in SQLite. Both are opened strictly read-only
/// and only token metadata is selected, never message bodies.
///
/// A database is skipped while it and its WAL are unchanged and settled
/// (`Fingerprint.settled`). Otherwise only rows past a watermark are read,
/// and every row is keyed so re-reading one is harmless.
enum DatabaseSources {
    /// A read from the source database failed. `Ledger.scan` downgrades
    /// only these to a per-source note: a failure on SWE's own cache
    /// (meta, upsert, BEGIN, COMMIT) still throws so the Overview shows a
    /// real error instead of quietly skipping.
    struct SourceRead: Error {
        var underlying: Error
    }

    /// Runs a call against the source database, tagging its failure.
    private static func reading<T>(_ body: () throws -> T) throws -> T {
        do { return try body() } catch { throw SourceRead(underlying: error) }
    }

    struct Fingerprint: Equatable {
        var text: String
        var inode: UInt64
        /// Whether the file and its WAL were last written more than
        /// `settleSeconds` before the stamp was taken. A write in the same
        /// clock tick as the stat can leave inode, size and mtime as they
        /// were (a row that fits the last page), so only a settled stamp
        /// may skip a scan (#1930).
        var settled: Bool

        /// FAT keeps two-second modification times, the coarsest of any
        /// local filesystem (HFS+ keeps one second, ext4 a clock tick of a
        /// few milliseconds). A stamp older than this when taken cannot be
        /// shared by a later write; a younger one can.
        static let settleSeconds: Double = 2

        /// - Parameter now: read before the stat, so a write after it is newer.
        init?(_ url: URL, now: Date = Date()) {
            guard let db = FileInfo(path: url.path) else { return nil }
            let wal = FileInfo(path: url.path + "-wal")
            inode = db.inode
            text = "\(db.inode):\(db.size):\(db.modified):\(wal?.size ?? 0):\(wal?.modified ?? 0)"
            settled = now.timeIntervalSince1970 - max(db.modified, wal?.modified ?? 0) > Self.settleSeconds
        }

        /// What a scan saves under `:fingerprint`: the stamp once settled,
        /// else "", which no stamp matches, so the next scan reads past the
        /// watermark again. Every row is keyed, so that read adds nothing
        /// twice.
        var saved: String { settled ? text : "" }
    }

    /// The saved watermark, or 0 when the database is no longer the file it
    /// was taken from: a recreated or restored file starts its row ids and
    /// timestamps over, and every row is keyed, so reading it all is safe.
    static func watermark(_ metaKey: String, _ fingerprint: Fingerprint, store: LedgerStore) throws -> Int64 {
        guard try store.meta(metaKey + ":inode") == String(fingerprint.inode) else { return 0 }
        return Int64(try store.meta(metaKey + ":watermark") ?? "") ?? 0
    }

    /// The one read-only open every source read shares. Strictly
    /// read-only: nothing is created, migrated or checkpointed in the
    /// other app's database.
    static func openReadOnly(at path: String) throws -> SQLiteDatabase {
        try SQLiteDatabase(readOnly: URL(fileURLWithPath: path))
    }

    /// Whether the source at `url` still matches the fingerprint the last
    /// committed scan stored under `metaKey` — a stat of the file and its
    /// WAL against one meta row, the cheap "nothing moved" verdict a
    /// caller wants before paying for an open or an enumeration. A file
    /// that cannot be stat-ed is never unchanged.
    static func sourceFingerprintUnchanged(at url: URL, metaKey: String, store: LedgerStore) throws -> Bool {
        guard let fingerprint = Fingerprint(url) else { return false }
        return try store.meta(metaKey + ":fingerprint") == fingerprint.text
    }

    // MARK: - OpenCode

    /// `~/.local/share/opencode/opencode.db`: one `message` row per message,
    /// its JSON `data` carrying role, model, cost and token counts, plus the
    /// `session_message` rows OpenCode 2.x writes instead. Messages
    /// are rewritten while they stream, so the watermark is `time_updated`
    /// and the key is the message id (the larger total wins). The model
    /// is recorded as `provider/model` (see `openCodeModel`).
    ///
    /// Current OpenCode stores the five token kinds disjointly
    /// (`total = input + output + reasoning + cache`); older rows folded
    /// reasoning into output (`total = input + output + cache`), which is
    /// detected per row. A recorded cost of 0 is OpenCode not knowing the
    /// price, so it is treated as no recorded cost.
    /// - Parameter source: a read-only connection the caller already holds
    ///   (the index pass enumerates the same database on one open). The
    ///   caller owns it — this never closes what it did not open — and is
    ///   trusted to have checked `sourceFingerprintUnchanged` first, so a
    ///   passed connection is used as-is.
    static func scanOpenCode(_ url: URL, store: LedgerStore, source passed: SQLiteDatabase? = nil) throws -> Bool {
        guard let fingerprint = Fingerprint(url) else { return false }
        let metaKey = openCodeMetaKey(url)
        if try store.meta(metaKey + ":fingerprint") == fingerprint.text { return false }
        let watermark = try watermark(metaKey, fingerprint, store: store)

        let source = try passed ?? reading { try openReadOnly(at: url.path) }
        let tables = try reading { try tableNames(in: source) }
        let statement = try reading { try source.prepare("""
            SELECT m.id, m.session_id, m.time_created, m.time_updated, s.directory,
                json_extract(m.data, '$.modelID'), json_extract(m.data, '$.cost'),
                json_extract(m.data, '$.tokens.input'), json_extract(m.data, '$.tokens.output'),
                json_extract(m.data, '$.tokens.reasoning'), json_extract(m.data, '$.tokens.cache.read'),
                json_extract(m.data, '$.tokens.cache.write'), json_extract(m.data, '$.tokens.total'),
                json_extract(m.data, '$.providerID')
            FROM message m LEFT JOIN session s ON s.id = m.session_id
            WHERE m.time_updated >= ?1 AND json_extract(m.data, '$.role') = 'assistant'
            \(tables.contains("session_message") ? openCodeV2Replies(hasSessionV2: tables.contains("session_v2")) : "")
            ORDER BY 4
            """) }
        statement.bind([.int(watermark)])
        let accountHome = url.deletingLastPathComponent().path
        var highest = watermark
        try store.db.execute("BEGIN IMMEDIATE")
        do {
            while try reading({ try statement.step() }) {
                highest = max(highest, statement.int(3))
                let input = statement.tokens(7), output = statement.tokens(8)
                let reasoning = statement.tokens(9), cacheRead = statement.tokens(10)
                let cacheWrite = statement.tokens(11)
                let total = statement.isNull(12) ? nil : statement.tokens(12)
                let reasoningInsideOutput = reasoning > 0 && total == input + output + cacheRead + cacheWrite
                let event = UsageEvent(
                    harness: "opencode", accountHome: accountHome,
                    sessionId: statement.text(1) ?? "unknown", cwd: statement.text(4),
                    model: openCodeModel(provider: statement.text(13), id: statement.text(5)),
                    timestamp: Date(timeIntervalSince1970: Double(statement.int(2)) / 1_000),
                    inputTokens: input,
                    outputTokens: max(0, reasoningInsideOutput ? output - reasoning : output),
                    cacheReadTokens: cacheRead, cacheWriteTokens: cacheWrite,
                    reasoningTokens: reasoning,
                    costUSD: statement.isNull(6) || statement.double(6) <= 0 ? nil : statement.double(6))
                guard event.totalTokens > 0 else { continue }
                try store.upsert(LedgerRecord(
                    key: "opencode:\(statement.text(0) ?? "")", relabelsModel: true, event: event))
            }
            // Rows a re-read moved to their `provider/model` name leave
            // their old dimension rows behind with no events.
            try store.dropVacatedDims()
            try store.setMeta(metaKey + ":watermark", String(highest))
            try store.setMeta(metaKey + ":inode", String(fingerprint.inode))
            try store.setMeta(metaKey + ":fingerprint", fingerprint.saved)
            try store.db.execute("COMMIT")
        } catch {
            store.rollback()
            throw error
        }
        return true
    }

    /// The meta rows one OpenCode database's fingerprint, watermark and
    /// census live under, shared by the token scan and the search pass.
    /// The `v3` names the reader: a build that read only the legacy tables
    /// (`v1`, no suffix) or named models without their provider (`v2`)
    /// saved a fingerprint and watermark that would skip rows this reader
    /// reads differently, so the first scan after the upgrade starts over.
    /// Every row is keyed by its message id, so re-reading replaces events
    /// in place (`LedgerRecord.relabelsModel`) and never adds a second.
    static func openCodeMetaKey(_ url: URL) -> String { "opencode-v3:\(url.path)" }

    /// The ledger's model name for an OpenCode reply: `provider/model`
    /// (OpenCode's own `-m` form, so the first slash always splits them;
    /// an id that has a slash itself, like OpenRouter's
    /// `nvidia/nemotron-...`, keeps it), the bare id when no provider is
    /// recorded. Pricing strips the prefix again (`PricingCatalog.forms`).
    /// Keep in step with `opencode_model` in `src/swe/ledger/parsers.py`.
    static func openCodeModel(provider: String?, id: String?) -> String {
        guard let id else { return "unknown" }
        guard let provider, !provider.isEmpty, !id.isEmpty else { return id }
        return provider + "/" + id
    }

    /// OpenCode 2.x writes new replies only to `session_message` (and new
    /// sessions only to `session_v2`), naming the model `model.id` and
    /// keeping no `tokens.total`: its counts are disjoint, like current
    /// `message` rows. The 2.x migration copied old replies there under
    /// their `message` ids, so those are left to the `message` half.
    private static func openCodeV2Replies(hasSessionV2: Bool) -> String {
        """
        UNION ALL
        SELECT sm.id, sm.session_id, sm.time_created, sm.time_updated,
            \(hasSessionV2 ? "COALESCE(v.directory, s.directory)" : "s.directory"),
            json_extract(sm.data, '$.model.id'), json_extract(sm.data, '$.cost'),
            json_extract(sm.data, '$.tokens.input'), json_extract(sm.data, '$.tokens.output'),
            json_extract(sm.data, '$.tokens.reasoning'), json_extract(sm.data, '$.tokens.cache.read'),
            json_extract(sm.data, '$.tokens.cache.write'), NULL,
            json_extract(sm.data, '$.model.providerID')
        FROM session_message sm LEFT JOIN session s ON s.id = sm.session_id
            \(hasSessionV2 ? "LEFT JOIN session_v2 v ON v.id = sm.session_id" : "")
        WHERE sm.time_updated >= ?1 AND sm.type = 'assistant'
            AND NOT EXISTS (SELECT 1 FROM message m WHERE m.id = sm.id)
        """
    }

    /// The tables `db` declares.
    static func tableNames(in db: SQLiteDatabase) throws -> Set<String> {
        let statement = try db.prepare("SELECT name FROM sqlite_master WHERE type = 'table'")
        var names: Set<String> = []
        while try statement.step() {
            if let name = statement.text(0) { names.insert(name) }
        }
        return names
    }

    // MARK: - Devin

    /// `~/.local/share/devin/cli/sessions.db`: usage sits on assistant
    /// `message_nodes` under `chat_message.metadata` (`request_id`,
    /// `generation_model`, `created_at`, `metrics`). Replay, compaction and
    /// forks copy nodes, so several rows share one `request_id`; that id is
    /// the key across the whole install (TokenTracker's finding). Metrics
    /// are already disjoint: `input_tokens` excludes cache reads and writes.
    ///
    /// Rows are read past a `row_id` watermark. An assistant row still
    /// waiting for its metrics holds the watermark back so it is read again
    /// once Devin fills them in, unless a later row of the same session has
    /// metrics, which means it was abandoned.
    static func scanDevin(_ url: URL, store: LedgerStore) throws -> Bool {
        guard let fingerprint = Fingerprint(url) else { return false }
        let metaKey = "devin:\(url.path)"
        if try store.meta(metaKey + ":fingerprint") == fingerprint.text { return false }
        var watermark = try watermark(metaKey, fingerprint, store: store)

        let source = try reading { try SQLiteDatabase(readOnly: url) }
        // Row ids only grow, so a highest id below the watermark means the
        // file was rebuilt in place.
        let top = try reading { try source.prepare("SELECT COALESCE(MAX(row_id), 0) FROM message_nodes") }
        if try reading({ try top.step() }), top.int(0) < watermark { watermark = 0 }
        let hasSessions = try reading { try source.prepare(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'").step() }
        let statement = try reading { try source.prepare("""
            SELECT n.row_id, n.session_id, n.created_at, \(hasSessions ? "s.working_directory" : "NULL"),
                json_extract(n.chat_message, '$.metadata.request_id'),
                json_extract(n.chat_message, '$.metadata.generation_model'),
                json_extract(n.chat_message, '$.metadata.created_at'),
                json_extract(n.chat_message, '$.metadata.metrics.input_tokens'),
                json_extract(n.chat_message, '$.metadata.metrics.output_tokens'),
                json_extract(n.chat_message, '$.metadata.metrics.cache_read_tokens'),
                json_extract(n.chat_message, '$.metadata.metrics.cache_creation_tokens')
            FROM message_nodes n \(hasSessions ? "LEFT JOIN sessions s ON s.id = n.session_id" : "")
            WHERE n.row_id > ? AND json_valid(n.chat_message)
                AND json_extract(n.chat_message, '$.role') = 'assistant'
                AND json_extract(n.chat_message, '$.metadata.request_id') IS NOT NULL
            ORDER BY n.row_id
            """) }
        statement.bind([.int(watermark)])
        let accountHome = url.deletingLastPathComponent().path
        var highest = watermark
        // Session to its earliest row still waiting for metrics.
        var pending: [String: Int64] = [:]
        try store.db.execute("BEGIN IMMEDIATE")
        do {
            while try reading({ try statement.step() }) {
                let rowID = statement.int(0)
                let session = statement.text(1) ?? "unknown"
                highest = max(highest, rowID)
                guard !statement.isNull(7), !statement.isNull(8) else {
                    if pending[session] == nil { pending[session] = rowID }
                    continue
                }
                pending[session] = nil
                let timestamp = statement.text(6).flatMap(Timestamp.parse)
                    ?? Date(timeIntervalSince1970: Double(statement.int(2)))
                let event = UsageEvent(
                    harness: "devin", accountHome: accountHome,
                    sessionId: session, cwd: statement.text(3),
                    model: statement.text(5) ?? "unknown", timestamp: timestamp,
                    inputTokens: statement.tokens(7), outputTokens: statement.tokens(8),
                    cacheReadTokens: statement.tokens(9), cacheWriteTokens: statement.tokens(10))
                guard event.totalTokens > 0, let requestID = statement.text(4) else { continue }
                try store.upsert(LedgerRecord(key: "devin:\(requestID)", event: event))
            }
            let pendingFloor = pending.values.min().map { $0 - 1 }
            try store.setMeta(metaKey + ":watermark", String(pendingFloor ?? highest))
            try store.setMeta(metaKey + ":inode", String(fingerprint.inode))
            try store.setMeta(metaKey + ":fingerprint", fingerprint.saved)
            try store.db.execute("COMMIT")
        } catch {
            store.rollback()
            throw error
        }
        return true
    }
}

private extension Statement {
    /// A token count column, between 0 and `JSONValue.maxTokens`: the
    /// cap the JSON log readers apply. A corrupt count near `Int64.max`
    /// would otherwise trap in `UsageEvent.totalTokens`, and since the
    /// watermark is saved only after the loop, on every launch scan.
    func tokens(_ column: Int32) -> Int {
        min(max(0, Int(int(column))), JSONValue.maxTokens)
    }
}
