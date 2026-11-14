import Foundation

/// What one file's next read owes the search index — the whole decide step,
/// as a function of the `files` row and the ledger's cursor position.
///
/// Adapted from Orca's `session-search-read-decision.ts`: the same inputs
/// give the same answer on the first pass after a restart as on the
/// hundredth, which is what lets a scan end mid-pass with nothing to record.
public enum SearchReadNeed: Sendable, Equatable {
    /// Never read: an excluded path, or held out after repeated failures.
    case skip
    /// The index's rows already match the file at this stat.
    case current
    /// The index cursor is exactly where the ledger resumes: the index takes
    /// the same appended lines.
    case append
    /// Read from the start: nothing held, behind a span no append reaches,
    /// or marked `due`.
    case whole
}

/// The search index as a consumer of the ledger's transcript scan — the
/// second consumer that lets one read of each transcript feed both the usage
/// ledger and the FTS index. Adapted from Orca's
/// `session-search-index-consumer.ts`.
///
/// A class, not an actor, and not thread-safe: it only ever runs inside the
/// `Ledger` actor's `scan` calls, which is also what makes the single
/// `pending` slot safe — a scan reads one file at a time. `@unchecked
/// Sendable` so the instance can cross into the actor that owns every call.
public final class SearchIndexer: @unchecked Sendable {
    /// Failures at one unchanged mtime before a file is left alone — Orca's
    /// `SESSION_SEARCH_FAILURES_BEFORE_HELD_OUT`. Three rather than one,
    /// because a single failure is often a transcript being rewritten under
    /// the read; the stat itself is the whole release condition, so no timer
    /// is kept.
    static let failuresBeforeHoldout = 3
    /// `finishScan` drains at most this many batches of orphaned messages.
    static let drainBatchLimit = 64
    /// Text bytes buffered before a whole read commits mid-file — Orca's
    /// char budget: one huge transcript holds ~its size in `SearchMessage`
    /// otherwise. Appends never flush: a committed tail chunk whose cursor
    /// never moved would re-read and duplicate on the next pass, and tails
    /// are small anyway. Whole reads are safe: a flush failure leaves the
    /// file `failed`, and the re-read's new generation replaces the
    /// partial one.
    static let bufferBudgetChars = 32 * 1024 * 1024
    /// Harnesses whose transcripts may index from the last offset —
    /// Python's `APPENDABLE`: claude and pi decode statelessly line to
    /// line and codex's only head state is the `session_meta` `beginFile`
    /// re-reads. Every other decoder keeps joins an offset cannot
    /// rebuild, so a grown file re-reads whole.
    static let appendableHarnesses: Set<SearchDecoder.Harness> = [.claude, .codex, .pi]

    let store: SearchStore
    /// The flush trigger — injectable so tests exercise chunking without
    /// writing a 32 MiB transcript.
    let bufferBudget: Int
    /// How many mid-read commits this indexer has run — the observable a
    /// test asserts against. Internal, not public API.
    private(set) var midReadFlushes = 0
    /// Runs after each mid-read commit, staged or not: a test looks at the
    /// index, or a second writer acts, between two pieces here. Nil in the
    /// app.
    var afterPiece: (() -> Void)?

    /// The file currently being decoded — set by `beginFile`, committed by
    /// `finishFile`, dropped by `failFile`. One scan reads one file at a
    /// time, so one slot is the whole in-flight set.
    private var pending: Pending?

    private struct Pending {
        var path: String
        var harness: SearchDecoder.Harness
        /// This read covers only the tail past the `files` row's offset; a
        /// whole read instead starts a new session generation.
        var appending: Bool
        /// The `files` row as the read began — what every commit fences on.
        var fileRow: SearchFileRow?
        var decoder: SearchDecoder
        /// The session id the records never carry — antigravity's
        /// conversation dir, rovo/muse's session dir — supplied by the
        /// source row's `sessionIDFromPath`. A record-carried id still wins.
        var sessionIDHint: String?
        var messages: [SearchMessage] = []
        var hints = SearchHints()
        /// First and last decoded timestamps — the session's time range.
        var firstTs: String?
        var lastTs: String?
        /// Decoded messages so far, pre-chunk — the session's messageCount.
        var decoded = 0
        /// The session the mid-read flushes committed under — `finishFile`
        /// continues it rather than minting another.
        var flushedSessionRowID: Int64?
        /// The fence the next commit expects: after a flush, the row points
        /// at the flushed session at the same offset.
        var expected: SearchFileFence?
        /// Approximate text bytes in `messages` — the flush trigger.
        var bufferedChars = 0
        /// The first flush failure, thrown from `finishFile` so the scan
        /// marks the file failed; also stops buffering, so the bound holds.
        var flushError: Error?
        /// Sidecar-derived fields that fill gaps the transcript never
        /// recorded — applied inside `finishFile`'s commit, so they land in
        /// the same transaction as the messages they describe.
        var fieldOverlay: SearchSessionFields?
        /// Rows of the same session under other, gone paths (a mirror
        /// copy's, #1918), deleted by the commit that leaves this file a
        /// session.
        var drop: [SearchFileRow] = []
        /// Paths that must still be gone at every commit: a copy's source
        /// path and the dropped rows' paths.
        var gone: [String] = []
        /// The agent, when the bytes come from a mirror copy: every commit
        /// records `mirror_copy:<path>`.
        var copyAgent: String?
        /// The stat a mid-read flush records. Nil reads `path`'s own; a
        /// copy read passes the copy's, since `path` is gone.
        var stat: SearchFileStat?
        /// A staged read parks its pieces on `host`, a session already
        /// visible for the session (the messages `stagedIDs` names), and
        /// switches sessions only in its final commit, so no commit shows
        /// two sessions or a prefix in place of a complete row.
        var host: Int64?
        var stagedIDs: [Int64] = []
        /// A staged piece lost its fence: the read stops, commits nothing,
        /// and the next pass decides again.
        var moved = false
    }

    public init(store: SearchStore) {
        self.store = store
        self.bufferBudget = Self.bufferBudgetChars
    }

    /// A test-size buffer budget — `SearchIndexerTests` passes a few hundred
    /// bytes here so chunk commits run on fixture transcripts.
    init(store: SearchStore, bufferBudget: Int) {
        self.store = store
        self.bufferBudget = bufferBudget
    }

    /// Paths that never enter the index: Claude `subagents/` transcripts are
    /// a session's internal delegation chatter (Orca skips them too). The
    /// ledger still counts their tokens.
    public func shouldIndex(path: String) -> Bool {
        !path.contains("/subagents/")
    }

    /// What the next read of `path` owes the index. `ledgerOffset` is where
    /// the ledger's own cursor sits, or nil when the ledger never read this
    /// file — index-pure sources (devin transcripts) pass nil. `harness`
    /// gates `.append` to `appendableHarnesses`; without it the answer can
    /// never be `.append`.
    public func readNeed(
        path: String, stat: SearchFileStat, ledgerOffset: UInt64?,
        harness: SearchDecoder.Harness? = nil
    ) -> SearchReadNeed {
        guard shouldIndex(path: path) else { return .skip }
        guard let row = try? store.fileRow(for: path) else { return .whole }
        // Held out: failed enough times at exactly this stat. Only a change
        // to the stat itself releases it — a file nobody touches is never
        // read again.
        if row.state == .failed, row.failCount >= Self.failuresBeforeHoldout,
           row.failedMtimeMs == stat.mtimeMs {
            return .skip
        }
        // `.due` is behind on a span no append reaches; the partial sentinel
        // marks a chunked read's prefix that nothing may continue.
        if row.state == .due || row.byteOffset == SearchStore.partialFileOffset {
            return .whole
        }
        if let size = stat.sizeBytes, row.state == .current,
           row.byteOffset == size, row.sizeBytes == size, row.mtimeMs == stat.mtimeMs {
            return .current
        }
        // The index cursor is exactly where the ledger resumes, and the file
        // grew past it: the index takes the same lines the ledger appends —
        // for the harnesses whose decoders can resume mid-file. The rest
        // re-read whole, Python's `APPENDABLE` gate.
        if let ledgerOffset, let size = stat.sizeBytes, let harness,
           Self.appendableHarnesses.contains(harness),
           UInt64(bitPattern: row.byteOffset) == ledgerOffset, row.byteOffset < size {
            return .append
        }
        // Anything else — a stat that moved, or an index behind a span it
        // cannot reach, like a ledger offset ahead of the index's — is read
        // whole.
        return .whole
    }

    /// Starts accumulating one file's decode. The decoder is always fresh —
    /// no decoder state is persisted in the `files` row — so an append
    /// re-reads the file's head for what the tail cannot supply, the way
    /// Python's `search_writer` primes the read: the span's first prompt
    /// must not re-title the session (only an explicit title record may),
    /// and codex's first `session_meta` decides which record type carries
    /// the appended lines.
    /// - Parameter appending: whether this read covers only the tail past the
    ///   `files` row's offset. `finishFile` commits a tail against the row's
    ///   session and a whole read as a new session generation.
    /// - Parameter sessionID: the source-derived session id for transcripts
    ///   whose records never carry one; a record's own `sessionId` hint
    ///   still outranks it.
    /// - Parameter drop: rows a mirror copy put in for this file's session
    ///   under other, gone paths (`SearchMirror.copiedRows`): they go in the commit
    ///   that reads this file. A whole read that drops them is staged.
    public func beginFile(
        path: String, harness: SearchDecoder.Harness,
        appending: Bool = false, sessionID: String? = nil, drop: [SearchFileRow] = []
    ) {
        // A read that never finished must not leave its staged pieces
        // behind when this one takes the slot.
        if let stale = pending { abandonFile(path: stale.path) }
        let fileRow = try? store.fileRow(for: path)
        var decoder = SearchDecoder(harness)
        if appending {
            decoder.primeForAppend()
            if harness == .codex,
               let meta = Self.headSessionMeta(path: path, before: fileRow?.byteOffset ?? 0) {
                decoder.prime(object: meta)
            }
        }
        pending = Pending(
            path: path, harness: harness, appending: appending,
            fileRow: fileRow, decoder: decoder,
            sessionIDHint: sessionID)
        if !drop.isEmpty {
            pending!.drop = drop
            pending!.gone = drop.map(\.path)
            if !appending {
                pending!.host = ([fileRow?.sessionRowID] + drop.map(\.sessionRowID)).compactMap { $0 }.first
            }
        }
    }

    /// The parsed object of the first `"session_meta"`-marked line in the
    /// already-indexed prefix — the codex `history_mode` an appended read
    /// cannot recover from the tail. As in Python's `prime` loop, the scan
    /// stops at the first marker line that parses, or at the offset the
    /// tail resumes at; an unreadable head primes nothing.
    private static func headSessionMeta(path: String, before byteOffset: Int64) -> [String: Any]? {
        guard byteOffset > 0 else { return nil }
        var end: UInt64 = 0
        var found: [String: Any]?
        struct Stop: Error {}
        _ = try? JSONLReader().read(URL(fileURLWithPath: path), from: 0) { line in
            end += UInt64(line.count + 1)  // the newline the reader strips
            if line.range(of: SearchCodexDecoder.sessionMetaMarker) != nil,
               let object = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                found = object
                throw Stop()
            }
            if end >= UInt64(bitPattern: byteOffset) { throw Stop() }
        }
        return found
    }

    /// Whether the line *could* carry anything this file's decoder wants —
    /// the cheap marker gate that lets the caller run one `JSONSerialization`
    /// for the union of both consumers instead of parsing per consumer.
    public func wants(line: Data) -> Bool {
        pending?.decoder.wants(line: line) ?? false
    }

    /// One already-parsed transcript line — the shared-parse path; the
    /// caller ran `JSONSerialization` once for the ledger and the index.
    public func consume(object: [String: Any]) {
        guard pending != nil else { return }
        record(pending!.decoder.decode(object: object))
    }

    /// One transcript line — for devin, a whole `transcripts/*.json`
    /// document, since those files are pretty-printed.
    public func consume(line: Data) {
        guard pending != nil else { return }
        record(pending!.decoder.decode(line: line))
    }

    /// The decoder's end-of-file flush — Kimi joins streamed `content.part`
    /// chunks into one assistant message per `step.end`, and a trailing
    /// unterminated step releases here (`consumeKimiWireTranscript`'s
    /// `flushAssistant` after the stream ends).
    public func consumeEndOfFile() {
        guard pending != nil else { return }
        record(pending!.decoder.endOfFile())
    }

    /// Fields a sidecar supplies for the file in flight — Cursor's
    /// `meta.json` carries title/cwd/dates the transcript never writes.
    /// `finishFile` gap-fills them into the same commit, so a session's
    /// metadata cannot strand behind a `files` row already marked current.
    public func setFieldOverlay(_ fields: SearchSessionFields) {
        pending?.fieldOverlay = fields
    }

    /// A stamp carried outside any message — the paired manifest's
    /// `created_at`/`updated_at` (rovo/kiro/grok/kimi) — folded into the
    /// session timeline exactly like a record's own stamp, Orca's
    /// `updateTimeline`.
    public func consumeTimeline(_ value: Any?) {
        guard pending != nil, let ts = SearchStamp.iso(value) else { return }
        foldTimeline(ts)
    }

    /// Orca's `updateTimeline` on one ISO stamp: `createdAt` is the earliest
    /// stamp seen, `updatedAt` the latest. Comparison runs on parsed epoch
    /// ms where both stamps parse; an unparseable stamp (only fixtures write
    /// those) keeps the old first/last-seen behavior.
    private func foldTimeline(_ ts: String) {
        guard let incoming = SearchStamp.milliseconds(ts) else {
            if pending!.firstTs == nil { pending!.firstTs = ts }
            pending!.lastTs = ts
            return
        }
        if let first = pending!.firstTs, let firstMs = SearchStamp.milliseconds(first) {
            if incoming < firstMs { pending!.firstTs = ts }
        } else {
            pending!.firstTs = ts
        }
        if let last = pending!.lastTs, let lastMs = SearchStamp.milliseconds(last) {
            if incoming >= lastMs { pending!.lastTs = ts }
        } else {
            pending!.lastTs = ts
        }
    }

    private func record(_ output: (messages: [SearchMessage], hints: SearchHints)) {
        // A flushed-failure file buffers nothing more — the bound holds and
        // `finishFile` still gets to throw the original error.
        guard pending!.flushError == nil, !pending!.moved else { return }
        // Hints arrive repeatedly across a file: keep the latest non-nil.
        // That lands the explicit title wherever its record sits.
        if let value = output.hints.sessionId { pending!.hints.sessionId = value }
        if let value = output.hints.cwd { pending!.hints.cwd = value }
        if let value = output.hints.branch { pending!.hints.branch = value }
        if let value = output.hints.title { pending!.hints.title = value }
        pending!.hints.nonUserOrigin = pending!.hints.nonUserOrigin || output.hints.nonUserOrigin
        for message in output.messages {
            if let ts = message.ts {
                foldTimeline(ts)
            }
            pending!.messages.append(message)
            pending!.bufferedChars += message.text.utf8.count
        }
        pending!.decoded += output.messages.count
        if !pending!.appending, pending!.bufferedChars >= bufferBudget {
            flushPending()
        }
    }

    /// Commits the buffered whole-read rows under the in-flight session —
    /// offset parked at the partial sentinel, so a scan that dies mid-read
    /// re-reads the file whole rather than trusting the fresh stat.
    private func flushPending() {
        do { try flushNow() } catch { pending!.flushError = error }
        pending!.messages.removeAll(keepingCapacity: true)
        pending!.bufferedChars = 0
    }

    private func flushNow() throws {
        let file = pending!
        guard !file.messages.isEmpty else { return }
        let expected = file.expected
            ?? file.fileRow.map { SearchFileFence(sessionRowID: $0.sessionRowID, byteOffset: $0.byteOffset) }
        if let host = file.host {
            // Staged: the piece goes onto the visible session, no `files`
            // row moves, and only the final commit switches sessions.
            guard let ids = try store.stageMessages(
                path: file.path, expected: expected, drop: file.drop, gone: file.gone,
                host: host, rows: Self.messageRows(from: file.messages))
            else {
                pending!.moved = true
                return
            }
            pending!.stagedIDs += ids
            midReadFlushes += 1
            afterPiece?()
            return
        }
        let stat: SearchFileStat
        if let given = file.stat {
            stat = given
        } else if let info = FileInfo(path: file.path) {
            stat = SearchFileStat(
                ino: Int64(info.inode), mtimeMs: info.modified * 1000, sizeBytes: Int64(info.size))
        } else {
            return
        }
        let fields = SearchSessionFields(
            agent: file.harness.rawValue,
            sessionID: file.hints.sessionId ?? file.sessionIDHint ?? Self.stem(of: file.path),
            filePath: file.path, title: file.hints.title ?? "",
            cwd: file.hints.cwd, cwdKey: file.hints.cwd,
            createdAt: file.firstTs, updatedAt: file.lastTs,
            messageCount: file.decoded)
        let session: IndexSessionCommit
        if let rowID = file.flushedSessionRowID {
            session = .existing(rowID)
        } else if file.fileRow?.sessionRowID != nil {
            session = .replace(fields)
        } else {
            session = .fresh(fields)
        }
        // The row's offset parks at the partial sentinel; the final commit
        // moves it to the real offset. A fence miss means the record moved
        // under the read — nothing was written and the next pass decides.
        guard try store.commitFile(
            path: file.path, expected: expected, stat: stat,
            byteOffset: SearchStore.partialFileOffset,
            session: session, messages: Self.messageRows(from: file.messages),
            mark: nil, gone: file.gone, copyMark: file.copyAgent)
        else { return }
        let fence = try? store.fileFence(for: file.path)
        pending!.flushedSessionRowID = fence?.sessionRowID
        pending!.expected = fence
        midReadFlushes += 1
        afterPiece?()
    }

    /// Commits the read in the store's per-file transaction, fenced on the
    /// `files` row as `beginFile` saw it. Returns false when the record moved
    /// under the read — nothing was written and the next pass decides again.
    ///
    /// A whole read starts a new session generation: `replaceSession` swaps
    /// the rows in one transaction, so the old conversation stops answering
    /// at once and `finishScan`'s drain reclaims its messages later. An
    /// append keeps the row's session and merges fields onto it.
    @discardableResult
    public func finishFile(path: String, stat: SearchFileStat, byteOffset: Int64) throws -> Bool {
        guard let file = pending, file.path == path else {
            pending = nil
            // A file the index never began still records as read, so a
            // transcript with nothing to decode is not retried forever.
            return try store.commitFile(
                path: path, expected: try? store.fileFence(for: path), stat: stat,
                byteOffset: byteOffset, session: .none, messages: [])
        }
        defer { pending = nil }
        var committed = false
        // A staged read that does not land takes its pieces back: they would
        // only add to the old session's text. A kill skips this, and the
        // next whole read deletes the session they sit on.
        defer {
            if !committed, let host = file.host, !file.stagedIDs.isEmpty {
                try? store.discardStaged(host: host, ids: file.stagedIDs)
            }
        }
        if let flushError = file.flushError { throw flushError }
        if file.moved { return false }
        committed = try commitRead(file, path: path, stat: stat, byteOffset: byteOffset)
        return committed
    }

    /// `finishFile`'s commit, by the read's kind (swe's `_finish`). Rows of
    /// the session under other paths (`drop`) go only with a commit that
    /// leaves this file a session: a read that found none keeps them.
    private func commitRead(_ file: Pending, path: String, stat: SearchFileStat,
                            byteOffset: Int64) throws -> Bool {
        let rows = Self.messageRows(from: file.messages)
        var fields = SearchSessionFields(
            agent: file.harness.rawValue,
            sessionID: file.hints.sessionId ?? file.sessionIDHint ?? Self.stem(of: path),
            filePath: path, title: file.hints.title ?? "",
            cwd: file.hints.cwd, cwdKey: file.hints.cwd,
            createdAt: file.firstTs, updatedAt: file.lastTs,
            messageCount: file.decoded)
        // The sidecar overlay only fills gaps — the transcript's own records
        // keep precedence (Orca's `mergeCursorChatMeta` order).
        if let overlay = file.fieldOverlay {
            if fields.title.isEmpty, !overlay.title.isEmpty {
                fields.title = overlay.title
            }
            if fields.cwd == nil {
                fields.cwd = overlay.cwd
                fields.cwdKey = overlay.cwdKey ?? overlay.cwd
            }
            if fields.createdAt == nil { fields.createdAt = overlay.createdAt }
            if fields.updatedAt == nil { fields.updatedAt = overlay.updatedAt }
        }
        // A session worth writing is an explicit session id — record- or
        // path-derived — or any message (`decoded` counts flushed chunks too).
        let hasSession = file.hints.sessionId != nil
            || file.sessionIDHint != nil || file.decoded > 0
        let old = file.fileRow
        let fence = old.map { SearchFileFence(sessionRowID: $0.sessionRowID, byteOffset: $0.byteOffset) }

        if file.appending, let old {
            if let sessionRowID = old.sessionRowID {
                // The read covered only the tail; the rows continue the row's
                // session. Fields merge onto it: counts add, decoded values
                // win, and the stored ones fill what this span never saw.
                if let existing = try? store.sessionRow(sessionRowID)?.fields {
                    fields.sessionID = file.hints.sessionId ?? existing.sessionID
                    fields.title = file.hints.title ?? existing.title
                    fields.cwd = file.hints.cwd ?? existing.cwd
                    fields.cwdKey = fields.cwd
                    fields.createdAt = existing.createdAt ?? fields.createdAt
                    fields.updatedAt = fields.updatedAt ?? existing.updatedAt
                    fields.messageCount += existing.messageCount
                }
                return try store.commitFile(path: path, expected: fence, stat: stat,
                    byteOffset: byteOffset, session: .existing(sessionRowID),
                    messages: rows, updating: fields, mark: nil,
                    drop: file.drop, gone: file.gone, copyMark: file.copyAgent)
            }
            guard hasSession else {
                return try store.commitFile(path: path, expected: fence, stat: stat,
                    byteOffset: byteOffset, session: .none, messages: [], mark: nil,
                    gone: file.gone, copyMark: file.copyAgent)
            }
            return try store.commitFile(path: path, expected: fence, stat: stat,
                byteOffset: byteOffset, session: .fresh(fields), messages: rows, mark: nil,
                drop: file.drop, gone: file.gone, copyMark: file.copyAgent)
        }
        if let host = file.host, !file.stagedIDs.isEmpty {
            // The switch: a new session takes the parked pieces and the
            // rest, and every old row of the session goes, in one commit.
            let expected = file.expected ?? fence
            return try store.commitFile(path: path, expected: expected, stat: stat,
                byteOffset: byteOffset, session: old?.sessionRowID != nil ? .replace(fields) : .fresh(fields),
                messages: rows, mark: nil, drop: file.drop, adopt: (host, file.stagedIDs),
                gone: file.gone, copyMark: file.copyAgent)
        }

        return try commitWholeRead(
            path: path, stat: stat, byteOffset: byteOffset,
            fields: fields, rows: rows, hasSession: hasSession, old: old,
            flushedID: file.flushedSessionRowID, expected: file.expected,
            drop: file.drop, gone: file.gone, copyMark: file.copyAgent)
    }

    /// Commits one session read whole out of a container — the synthetic
    /// counterpart of `finishFile`, for sources like OpenCode's
    /// `opencode.db` whose sessions are rows rather than files. `fields`
    /// comes from the container's own session row, `messages` from its
    /// transcript rows; `byteOffset` carries the synthetic size, so the
    /// `.current` check (offset == size) can hold on a later pass.
    /// - Parameter fileRow: the `files` row as it stood before the session
    ///   was read — what the commit fences on. Read after, it could be a
    ///   newer commit from another writer (swe's), which this older read
    ///   would then replace.
    @discardableResult
    public func finishSession(
        path: String, stat: SearchFileStat, fileRow: SearchFileRow?,
        fields: SearchSessionFields, messages: [SearchMessage]
    ) throws -> Bool {
        try finishSession(path: path, stat: stat, fileRow: fileRow, fields: fields,
                          messages: messages, appended: false, mark: nil, writesMark: false)
    }

    /// `finishSession` for a Devin read: one that `appended` adds its
    /// messages to `fileRow`'s session instead of replacing it, and `mark`
    /// lands in the same commit (Python's `index_session`).
    @discardableResult
    func finishSession(
        path: String, stat: SearchFileStat, fileRow: SearchFileRow?,
        fields: SearchSessionFields, messages: [SearchMessage],
        appended: Bool, mark: SearchDevin.Mark?, writesMark: Bool = true
    ) throws -> Bool {
        let rows = Self.messageRows(from: messages)
        let fence = fileRow.map { SearchFileFence(sessionRowID: $0.sessionRowID, byteOffset: $0.byteOffset) }
        if appended, let sessionRowID = fileRow?.sessionRowID {
            return try store.commitFile(
                path: path, expected: fence, stat: stat, byteOffset: stat.sizeBytes ?? 0,
                session: .existing(sessionRowID), messages: rows, updating: fields, mark: mark,
                writesMark: writesMark, expectedMtimeMs: fileRow?.mtimeMs)
        }
        let session: IndexSessionCommit = fileRow?.sessionRowID != nil ? .replace(fields) : .fresh(fields)
        return try store.commitFile(
            path: path, expected: fence, stat: stat, byteOffset: stat.sizeBytes ?? 0,
            session: session, messages: rows, mark: mark, writesMark: writesMark)
    }

    // MARK: - Mirror copies (#1918)

    /// What one copy read did, as swe's `index_copy` reports it.
    public enum CopyOutcome: Sendable, Equatable {
        case skip, current, indexed, moved
        /// The read or its commit threw; the reason is the error's text.
        case failed(String)
    }

    /// `readNeed` from a `files` row alone, with no ledger cursor: swe's
    /// `read_need`. A copy is always read whole, so `.append` reads whole.
    static func readNeed(row: SearchFileRow?, stat: SearchFileStat) -> SearchReadNeed {
        guard let row else { return .whole }
        if row.state == .failed, row.failCount >= failuresBeforeHoldout,
           row.failedMtimeMs == stat.mtimeMs {
            return .skip
        }
        if row.state == .due || row.byteOffset == SearchStore.partialFileOffset { return .whole }
        if let size = stat.sizeBytes, row.state == .current, row.byteOffset == size,
           row.sizeBytes == size, row.mtimeMs == stat.mtimeMs {
            return .current
        }
        return .whole
    }

    /// Reads the mirror copy `copy` into the index under `path`, the
    /// transcript it was copied from, whose file is gone (swe's
    /// `index_copy`). `stat` is the copy's and is what the `files` row
    /// records, so an unchanged copy reads as current next pass. Always a
    /// whole read, staged when it commits in pieces and a session is
    /// already visible for it. `drop` are the session's rows under other
    /// paths, removed in the final commit; `path` and their paths must
    /// still be gone at every commit.
    @discardableResult
    func indexCopy(path: String, copy: String, harness: SearchDecoder.Harness, stat: SearchFileStat,
                   row: SearchFileRow?, drop: [SearchFileRow]) -> CopyOutcome {
        switch Self.readNeed(row: row, stat: stat) {
        case .skip: return .skip
        case .current: return .current
        case .append, .whole: break
        }
        if let stale = pending { abandonFile(path: stale.path) }
        pending = Pending(path: path, harness: harness, appending: false, fileRow: row,
                          decoder: SearchDecoder(harness))
        pending!.drop = drop
        pending!.gone = [path] + drop.map(\.path)
        pending!.copyAgent = harness.rawValue
        pending!.stat = stat
        pending!.host = ([row?.sessionRowID] + drop.map(\.sessionRowID)).compactMap { $0 }.first
        do {
            let result = try JSONLReader().read(URL(fileURLWithPath: copy), from: 0) { line in
                consume(line: line)
            }
            return try finishFile(path: path, stat: stat, byteOffset: Int64(result.endOffset))
                ? .indexed : .moved
        } catch {
            failFile(path: path, mtimeMs: stat.mtimeMs)
            return .failed(error.localizedDescription)
        }
    }

    /// For a live file whose row is current: retires the rows a mirror copy
    /// put in for its session under other, gone paths (swe's `index_file`
    /// with `drop` on a current row). Fenced on the row and every dropped
    /// row; a failure leaves them for the next pass.
    func retireCopies(path: String, drop: [SearchFileRow]) {
        guard !drop.isEmpty, let row = try? store.fileRow(for: path), row.sessionRowID != nil else { return }
        _ = try? store.retire(keep: row, rows: drop)
    }

    /// Drops the read in flight for `path` without counting a failure (the
    /// scan stopped for a reason of its own, such as the ledger's store
    /// failing), taking back exactly the messages a staged read parked:
    /// swe's `_read` discards them in its `finally` on every path that
    /// does not commit.
    func abandonFile(path: String) {
        guard let file = pending, file.path == path else { return }
        if let host = file.host, !file.stagedIDs.isEmpty {
            try? store.discardStaged(host: host, ids: file.stagedIDs)
        }
        pending = nil
    }

    /// A read that never committed. Counted against the mtime it failed at:
    /// three failures at the same stat hold the file out until the stat
    /// itself moves.
    public func failFile(path: String, mtimeMs: Double) {
        if let file = pending, file.path == path {
            if let host = file.host, !file.stagedIDs.isEmpty {
                try? store.discardStaged(host: host, ids: file.stagedIDs)
            }
            pending = nil
        }
        guard shouldIndex(path: path) else { return }
        try? store.setFileState(path, .failed, atMtimeMs: mtimeMs)
    }

    /// Reclaims messages orphaned by session replacements, in bounded batches
    /// so a scan's tail never stalls on one giant delete.
    public func finishScan() throws {
        for _ in 0..<Self.drainBatchLimit {
            if try store.drainOrphanedMessages(limit: 256) == 0 { break }
        }
    }

    /// `SearchChunker.rows` mapped to store rows — the one text column the
    /// role searches under plus identifier shadow text. Shared by
    /// `finishFile` and `finishSession`.
    private static func messageRows(from messages: [SearchMessage]) -> [SearchMessageRow] {
        SearchChunker.rows(from: messages).map { chunk -> SearchMessageRow in
            var row = SearchMessageRow(
                role: chunk.role.rawValue, ts: chunk.ts,
                identifiers: identifierShadowText(chunk.text))
            switch chunk.role {
            case .user: row.userText = chunk.text
            case .assistant: row.assistantText = chunk.text
            case .tool: row.toolText = chunk.text
            }
            return row
        }
    }

    /// The whole-read commit `finishFile` and `finishSession` share: a row
    /// that already holds a session gets a new generation (`.replace` swaps
    /// it in the same transaction), a first session is created, and a read
    /// that found none commits only its cursor.
    private func commitWholeRead(
        path: String, stat: SearchFileStat, byteOffset: Int64,
        fields: SearchSessionFields, rows: [SearchMessageRow],
        hasSession: Bool, old: SearchFileRow?,
        flushedID: Int64? = nil, expected: SearchFileFence? = nil,
        drop: [SearchFileRow] = [], gone: [String] = [], copyMark: String? = nil
    ) throws -> Bool {
        let fence = expected
            ?? old.map { SearchFileFence(sessionRowID: $0.sessionRowID, byteOffset: $0.byteOffset) }
        // A flushed read already holds its session — the final commit adds
        // the last rows, publishes the finished fields, and moves the cursor.
        if let flushedID {
            return try store.commitFile(path: path, expected: fence, stat: stat,
                byteOffset: byteOffset, session: .existing(flushedID),
                messages: rows, updating: fields, mark: nil,
                drop: drop, gone: gone, copyMark: copyMark)
        }
        if let old, old.sessionRowID != nil {
            return try store.commitFile(path: path, expected: fence, stat: stat,
                byteOffset: byteOffset, session: .replace(fields), messages: rows, mark: nil,
                drop: drop, gone: gone, copyMark: copyMark)
        }
        guard hasSession else {
            return try store.commitFile(path: path, expected: fence, stat: stat,
                byteOffset: byteOffset, session: .none, messages: [], mark: nil,
                gone: gone, copyMark: copyMark)
        }
        return try store.commitFile(path: path, expected: fence, stat: stat,
            byteOffset: byteOffset, session: .fresh(fields), messages: rows, mark: nil,
            drop: drop, gone: gone, copyMark: copyMark)
    }

    /// The session id a file reports when no line carried one.
    private static func stem(of path: String) -> String {
        URL(fileURLWithPath: path).deletingPathExtension().lastPathComponent
    }
}
