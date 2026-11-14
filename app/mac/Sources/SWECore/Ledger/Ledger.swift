import Foundation
import os

/// Where to look for usage. Each list can name several homes because SWE
/// keeps extra accounts under `~/.swe/accounts/<vendor>/<id>/home`.
public struct LedgerRoots: Sendable, Equatable {
    /// Claude config homes; transcripts are under `<home>/projects`.
    public var claudeHomes: [URL]
    /// Codex homes (`CODEX_HOME`); rollouts are under `sessions/` and `archived_sessions/`.
    public var codexHomes: [URL]
    /// OpenCode databases (`opencode.db`).
    public var openCodeDatabases: [URL]
    /// Devin CLI databases (`sessions.db`).
    public var devinDatabases: [URL]
    /// Pi homes; session logs are under `<home>/agent/sessions`.
    public var piHomes: [URL]
    /// Droid homes (`~/.factory`); session totals are under `<home>/sessions`,
    /// and the search index also reads transcripts under `<home>/projects`.
    public var droidHomes: [URL]
    /// Cline homes; sessions are under `<home>/data/sessions`.
    public var clineHomes: [URL]
    /// Cursor projects roots (`~/.cursor/projects`); chat transcripts sit
    /// under `<root>/<workspace>/agent-transcripts/<id>/<id>.jsonl`.
    public var cursorProjects: [URL]
    /// Cline sessions roots (`~/.cline/data/sessions`); each session dir
    /// pairs `<id>.json` with a `<id>.messages.json` sibling.
    public var clineSessions: [URL]
    /// Roots for the search index's own transcript enumeration, keyed by
    /// harness — every `SearchTranscriptSource` row's roots except the
    /// harnesses the explicit fields above already own (claude/codex/pi/
    /// devin feed ledger records; cursor/droid/cline keep richer mappings).
    /// An empty map means no index-pure enumeration, so a roots value built
    /// for a couple of harnesses never sweeps the real user home.
    public var transcriptRoots: [SearchDecoder.Harness: [URL]]
    /// swe's transcript mirror (`~/.swe/data/transcripts`, #1918): the
    /// copies of Claude Code and Codex transcripts the harness pruned,
    /// which a full scan reads for usage it never recorded and for search.
    /// Nil reads no copy, so a roots value built for a test never sweeps
    /// the real mirror.
    public var mirror: TranscriptMirror?

    public init(
        claudeHomes: [URL] = [], codexHomes: [URL] = [],
        openCodeDatabases: [URL] = [], devinDatabases: [URL] = [],
        piHomes: [URL] = [], droidHomes: [URL] = [], clineHomes: [URL] = [],
        cursorProjects: [URL] = [], clineSessions: [URL] = [],
        transcriptRoots: [SearchDecoder.Harness: [URL]] = [:],
        mirror: TranscriptMirror? = nil
    ) {
        self.mirror = mirror
        self.claudeHomes = claudeHomes
        self.codexHomes = codexHomes
        self.openCodeDatabases = openCodeDatabases
        self.devinDatabases = devinDatabases
        self.piHomes = piHomes
        self.droidHomes = droidHomes
        self.clineHomes = clineHomes
        self.cursorProjects = cursorProjects
        self.clineSessions = clineSessions
        self.transcriptRoots = transcriptRoots
    }

    /// The default locations under one user home, plus any profile homes.
    public static func standard(
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        claudeProfiles: [URL] = [], codexProfiles: [URL] = []
    ) -> LedgerRoots {
        var transcriptRoots: [SearchDecoder.Harness: [URL]] = [:]
        for source in SearchTranscriptSource.table(home: home) {
            guard let harness = source.harness else { continue }
            switch harness {
            // The explicit fields own these — LedgerRoots.standard fills
            // them below and the scan's merges keep them authoritative.
            case .claude, .codex, .pi, .devin, .cursor, .droid, .cline:
                continue
            default:
                transcriptRoots[harness, default: []] += source.roots
            }
        }
        let claudeHomes = [home.appendingPathComponent(".claude")] + claudeProfiles
        let codexHomes = [home.appendingPathComponent(".codex")] + codexProfiles
        return LedgerRoots(
            claudeHomes: claudeHomes,
            codexHomes: codexHomes,
            openCodeDatabases: [home.appendingPathComponent(".local/share/opencode/opencode.db")],
            devinDatabases: [home.appendingPathComponent(".local/share/devin/cli/sessions.db")],
            piHomes: [home.appendingPathComponent(".pi")],
            droidHomes: [home.appendingPathComponent(".factory")],
            clineHomes: [home.appendingPathComponent(".cline")],
            cursorProjects: [home.appendingPathComponent(".cursor/projects")],
            clineSessions: [home.appendingPathComponent(".cline/data/sessions")],
            transcriptRoots: transcriptRoots,
            mirror: .standard(root: TranscriptMirror.defaultRoot(home: home),
                              claudeHomes: claudeHomes, codexHomes: codexHomes,
                              accounts: home.appendingPathComponent(".swe/accounts")))
    }
}

/// Token and dollar totals for a set of events.
public struct UsageTotals: Sendable, Equatable, Codable {
    public var inputTokens = 0
    public var outputTokens = 0
    public var cacheReadTokens = 0
    public var cacheWriteTokens = 0
    public var reasoningTokens = 0
    /// USD for every event that could be priced: the log's own cost when it
    /// has one, otherwise the catalog's rates.
    public var costUSD = 0.0
    /// Tokens of events whose model the catalog does not know. They are in the
    /// token counts above but not in `costUSD`.
    public var unpricedTokens = 0
    public var unpricedModels: Set<String> = []
    public var eventCount = 0

    public init() {}

    public var totalTokens: Int {
        inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens + reasoningTokens
    }

    mutating func add(_ other: UsageTotals) {
        inputTokens += other.inputTokens
        outputTokens += other.outputTokens
        cacheReadTokens += other.cacheReadTokens
        cacheWriteTokens += other.cacheWriteTokens
        reasoningTokens += other.reasoningTokens
        costUSD += other.costUSD
        unpricedTokens += other.unpricedTokens
        unpricedModels.formUnion(other.unpricedModels)
        eventCount += other.eventCount
    }
}

/// One model's usage for one harness, for the Pricing page.
public struct ModelUsage: Sendable, Equatable {
    public var model: String
    public var harness: String
    /// The logged tier for this slice ("fast", "priority"), nil for the
    /// base tier. The price it shows charges the tier's rates.
    public var tier: String?
    public var totals: UsageTotals
    /// Every event in the slice carried its own logged cost, so no price
    /// list was consulted.
    public var harnessBilled: Bool
}

/// One harness and model on one calendar day.
public struct DailyUsage: Sendable, Equatable {
    /// Start of the day in the calendar the query used.
    public var day: Date
    public var harness: String
    public var model: String
    public var totals: UsageTotals

    public init(day: Date, harness: String, model: String, totals: UsageTotals) {
        self.day = day
        self.harness = harness
        self.model = model
        self.totals = totals
    }
}

/// Usage of one session inside a date range.
public struct SessionUsage: Sendable, Equatable, Codable {
    public var sessionId: String
    public var harness: String
    public var accountHome: String
    /// The working directory of the session's latest event.
    public var cwd: String?
    public var models: [String]
    public var totals: UsageTotals
    public var firstTimestamp: Date
    public var lastTimestamp: Date
}

/// How far a full scan has got: every source item it listed up front
/// (transcript files and databases), and how many it has finished.
public struct ScanProgress: Sendable, Equatable {
    public var filesDone: Int
    public var filesTotal: Int
    public init(filesDone: Int, filesTotal: Int) {
        self.filesDone = filesDone
        self.filesTotal = filesTotal
    }
}

/// What a scan did.
public struct ScanReport: Sendable, Equatable {
    public var filesRead = 0
    public var filesUnchanged = 0
    /// Files read from the start again because they were replaced or truncated.
    public var filesRescanned = 0
    /// Files the search index committed this pass, when an indexer is set.
    public var filesIndexed = 0
    /// Lines over the size limit that were skipped.
    public var skippedLines = 0
    public var databasesRead = 0
    /// Usage events the scan inserted, changed or deleted. Reading a file
    /// is not enough to move a figure: most appended transcript lines
    /// (prompts, tool output) carry no usage, and a rewrite can repeat
    /// rows already stored. Only this says the ledger's figures moved.
    public var eventsWritten = 0
    /// Sources or files that failed this round and were skipped, one note
    /// each. A source in a transient state (mid-rebuild, locked for
    /// recovery, an unexpected schema) or an unreadable transcript must
    /// not take the rest of the scan down with it.
    public var notes: [String] = []
    public init() {}
}

/// The usage ledger: scans harness logs into a persisted, deduplicated event
/// store and answers the questions the Overview and Sessions pages ask.
///
/// - `scan(roots:)` reads only what changed since the last scan.
/// - `totals`, `daily`, `bySession` and `byAccount` price events at query time
///   with the current `PricingCatalog`: names resolve through today's list,
///   and each event pays the rate its price entry had when it happened.
public actor Ledger {
    let store: LedgerStore
    /// The catalog in force, behind a lock so the off-actor usage read
    /// prices with it without waiting for the actor.
    private let pricingState: OSAllocatedUnfairLock<PricingCatalog>
    private nonisolated var pricing: PricingCatalog { pricingState.withLock { $0 } }
    private let reader: JSONLReader
    /// The ledger file's path and a lazily opened read-only connection to
    /// it, so the calendar's block query never queues behind a scan on
    /// the write connection or this actor.
    let activityURL: URL
    let activityConnection = OSAllocatedUnfairLock<SQLiteDatabase?>(initialState: nil)
    /// Another read-only connection, for the Pricing and Models pages'
    /// per-model usage: their first open must not wait out a scan either.
    let usageConnection = OSAllocatedUnfairLock<SQLiteDatabase?>(initialState: nil)
    /// Read-only connections for the Overview's queries, one taken per
    /// query and put back after, so several run side by side. Those
    /// queries take seconds on a large ledger, and on this actor every one
    /// of them stalled the launch scan behind it.
    let overviewReaders = OSAllocatedUnfairLock<[SQLiteDatabase]>(initialState: [])
    /// Settings' "Hide accidental sessions": whether the calendar drops
    /// trivial sessions. Outside the actor because the block query runs
    /// on its own read connection.
    let trivialGate = OSAllocatedUnfairLock<Bool>(initialState: true)

    /// Flips the calendar's trivial-session drop (Settings → "Hide
    /// accidental sessions").
    public nonisolated func setHidesTrivialSessions(_ hides: Bool) {
        trivialGate.withLock { $0 = hides }
    }

    /// The search index the scan also feeds. Nil leaves the feature off —
    /// scans then cost nothing for it. Set once at startup; the indexer runs
    /// only inside this actor's calls.
    private var indexer: SearchIndexer?

    public func setIndexer(_ indexer: SearchIndexer?) {
        self.indexer = indexer
    }

    /// The daily copies `backUpIfDue` takes.
    public nonisolated let backups: LedgerBackups
    private static let log = Logger(subsystem: "com.cwenlong.swe", category: "ledger")

    /// - Parameters:
    ///   - directory: where the ledger database lives; production uses
    ///     `~/.swe/data/ledger/` (see `LedgerLocation`).
    ///   - backups: where the daily copies go; defaults to `directory/backups`.
    ///   - pricing: defaults to the bundled LiteLLM snapshot.
    ///   - maxLineBytes: longer lines are skipped rather than buffered.
    public init(
        directory: URL, backups: URL? = nil,
        pricing: PricingCatalog = (try? .bundled()) ?? .empty, maxLineBytes: Int = 64 << 20
    ) throws {
        activityURL = directory.appendingPathComponent(LedgerLocation.fileName)
        store = try LedgerStore(url: activityURL)
        self.backups = LedgerBackups(directory: backups ?? directory.appendingPathComponent("backups"))
        pricingState = OSAllocatedUnfairLock(initialState: pricing)
        reader = JSONLReader(maxLineBytes: maxLineBytes)
    }

    /// How long a cache write waits out another connection's lock: two
    /// seconds, so a reader's snapshot never fails a write at once. A
    /// test that holds the lock on purpose shortens it, so it needn't sit
    /// out the full wait.
    func setBusyTimeout(milliseconds: Int32) {
        store.db.setBusyTimeout(milliseconds: milliseconds)
    }

    /// Nonisolated: the catalog sits behind its own lock, so a saved
    /// price lands at once instead of queueing behind a running scan.
    public nonisolated func setPricing(_ catalog: PricingCatalog) {
        pricingState.withLock { $0 = catalog }
    }

    /// The catalog currently in force, for views that look models up
    /// directly (the Pricing page).
    /// Nonisolated, so a page opening mid-scan doesn't wait for it.
    public nonisolated func catalog() async -> PricingCatalog { pricing }

    // MARK: - Backups

    /// Takes today's copy of the ledger when there is none yet. Returns the
    /// copy, or nil when today's exists already.
    @discardableResult
    public func backUpIfDue(now: Date = Date(), calendar: Calendar = .current) throws -> URL? {
        try backups.backUpIfDue(store.db, now: now, calendar: calendar)
    }

    /// Checks for a due copy every `interval` until cancelled, so an app
    /// left running for days still takes one each day.
    public func keepBackedUp(every interval: Duration = .seconds(3600)) async {
        while !Task.isCancelled {
            do { try backUpIfDue() } catch {
                Self.log.error("ledger backup failed: \(error)")
            }
            do { try await Task.sleep(for: interval) } catch { return }
        }
    }

    // MARK: - Scanning

    /// Publishes the running full scan's progress for `scanProgress()`.
    /// Behind a lock so the Overview reads it without queueing on the actor.
    private let progressState = OSAllocatedUnfairLock<ScanProgress?>(initialState: nil)
    /// Bumped by every full scan; only the newest one writes progress, so
    /// two overlapping sweeps never interleave their counts.
    private var progressOwner = 0

    /// The newest finished full scan's final progress. Tests read it.
    private(set) var lastFullScanProgress: ScanProgress?

    /// How far the running full scan has got, or nil when none is running.
    /// Capped scans (`recentLimit`) report nothing: they are short, and
    /// they run beside a full scan.
    public nonisolated func scanProgress() -> ScanProgress? {
        progressState.withLock { $0 }
    }

    /// Counts one source item done, when `owner` is still the newest full scan.
    private func advanceProgress(_ owner: Int) {
        guard owner != 0, owner == progressOwner else { return }
        progressState.withLock { $0?.filesDone += 1 }
    }

    /// One index-pure source root's files, enumerated before any is read.
    private struct IndexJob {
        var source: SearchTranscriptSource
        var harness: SearchDecoder.Harness
        var cursorMeta: [String: URL]
        var files: [URL]
    }

    /// `recentLimit` caps the sweep to each source's newest files — the
    /// first N of each mtime-ordered list, sessions inside a database
    /// included — so a caller that only needs live sessions hot (the
    /// search index's reconcile tick) skips the backlog. Staleness checks
    /// run only on the kept files; the rest wait for an unlimited pass.
    ///
    /// Every source is enumerated before the first file is read, so a full
    /// scan can publish "files done of total" through `scanProgress()`.
    @discardableResult
    public func scan(roots: LedgerRoots? = nil, recentLimit: Int? = nil) async throws -> ScanReport {
        let roots = roots ?? .standard()
        var report = ScanReport()
        var seen = Set<String>()
        // A Cursor feed ingest interleaving at a yield counts here too:
        // at worst one needless rebuild, and that ingest reloads anyway.
        let writesBefore = store.eventWrites

        let claudeFiles = roots.claudeHomes.map { home in
            (home, Self.capped(Self.jsonlFiles(under: home.appendingPathComponent("projects")),
                               recentLimit: recentLimit))
        }
        // Active sessions first, so a rollout that also sits in the archive
        // is attributed to the live copy.
        let codexFiles = roots.codexHomes.flatMap { home in
            ["sessions", "archived_sessions"].map { directory in
                (home, Self.capped(Self.jsonlFiles(under: home.appendingPathComponent(directory)),
                                   recentLimit: recentLimit))
            }
        }
        let openCodeDatabases = roots.openCodeDatabases.filter {
            FileManager.default.fileExists(atPath: $0.path)
        }
        let devinDatabases = roots.devinDatabases.filter {
            FileManager.default.fileExists(atPath: $0.path)
        }
        // Devin's transcripts are pretty-printed ATIF documents beside
        // `sessions.db`, not JSONL — they feed the index only, never the
        // ledger's records, so they are listed only while an indexer is set.
        let devinTranscripts: [URL] = indexer == nil ? [] : roots.devinDatabases.flatMap { database in
            Self.capped(
                Self.files(under: database.deletingLastPathComponent().appendingPathComponent("transcripts"),
                           suffix: ".json"),
                recentLimit: recentLimit)
        }
        let piFiles = roots.piHomes.map { home in
            (home, Self.capped(Self.jsonlFiles(under: home.appendingPathComponent("agent/sessions")),
                               recentLimit: recentLimit))
        }
        let droidFiles = roots.droidHomes.map { home in
            (home, Self.capped(Self.files(under: home.appendingPathComponent("sessions"),
                                          suffix: ".settings.json"),
                               recentLimit: recentLimit))
        }
        // `<id>/<id>.json`: the stem must equal its session directory.
        // Filter before the cap — sibling `.json` files that fail it must
        // not crowd sessions out of a limited window.
        let clineFiles = roots.clineHomes.map { home in
            (home, Self.capped(
                Self.files(under: home.appendingPathComponent("data/sessions"), suffix: ".json")
                    .filter {
                        $0.deletingPathExtension().lastPathComponent
                            == $0.deletingLastPathComponent().lastPathComponent
                    },
                recentLimit: recentLimit))
        }
        let indexJobs = indexer == nil ? [] : Self.indexJobs(roots: roots, recentLimit: recentLimit)

        var owner = 0
        if recentLimit == nil {
            progressOwner += 1
            owner = progressOwner
            let fileLists: [[URL]] = claudeFiles.map(\.1) + codexFiles.map(\.1) + piFiles.map(\.1)
                + droidFiles.map(\.1) + clineFiles.map(\.1) + indexJobs.map(\.files)
                + [devinTranscripts]
            // OpenCode and Devin databases are visited twice with an
            // indexer: once to index their sessions, once for tokens.
            let total: Int = fileLists.reduce(0) { $0 + $1.count }
                + (openCodeDatabases.count + devinDatabases.count) * (indexer == nil ? 1 : 2)
            progressState.withLock { $0 = ScanProgress(filesDone: 0, filesTotal: total) }
        }
        defer {
            if owner != 0, owner == progressOwner {
                lastFullScanProgress = progressState.withLock { $0 }
                progressState.withLock { $0 = nil }
            }
        }

        // Rows a mirror copy put in, by session (#1918): a live file of one
        // of these sessions retires them in the commit that reads it.
        let copyMarks = (try? indexer?.store.copyMarks()) ?? [:]
        for (home, files) in claudeFiles {
            for file in files {
                if Task.isCancelled { throw CancellationError() }
                try scanFile(file, accountHome: home.path, parser: ClaudeFileParser.self, seen: &seen,
                             report: &report, copyMarks: copyMarks)
                advanceProgress(owner)
                await Task.yield()
            }
        }
        for (home, files) in codexFiles {
            for file in files {
                if Task.isCancelled { throw CancellationError() }
                try scanFile(file, accountHome: home.path, parser: CodexFileParser.self, seen: &seen,
                             report: &report, copyMarks: copyMarks)
                advanceProgress(owner)
                await Task.yield()
            }
        }
        // Copies of sessions the harnesses pruned: only on a full scan,
        // whose enumeration names every live session, so a copy never
        // stands for one a capped list left out. `pruned` adds the
        // sessions of account homes no profile registers, as swe does.
        if recentLimit == nil, let mirror = roots.mirror {
            let live = Self.liveIdentities(claude: claudeFiles.flatMap(\.1), codex: codexFiles.flatMap(\.1))
            // One walk of the mirror for both consumers. A Codex copy the
            // manifest lost has two possible sources; the one the index
            // already holds a row for is preferred, as swe prefers it.
            let store = self.store
            // Claude subagent transcripts come too: their usage counts
            // under the parent session while they are live (every line
            // carries its `sessionId`), so their copies are read under the
            // same rule. Search never indexes a live one, so it never sees
            // their copies either.
            let pruned = mirror.pruned(live: live, known: { [indexer] path in
                if let indexer { return (try? indexer.store.fileRow(for: path)) != nil }
                return (try? store.cursor(for: path)) != nil
            }, subagents: true)
            try await scanCopies(pruned, mirror: mirror, seen: &seen, report: &report)
            if let indexer {
                let counts = SearchMirror.indexCopies(indexer: indexer,
                                                      pruned: pruned.filter { !$0.copy.isSubagent },
                                                      shouldStop: { Task.isCancelled })
                report.filesIndexed += counts.indexed
                // As a live file's failed index read does.
                report.notes += counts.failures
            }
        }
        if let indexer {
            // OpenCode keeps its sessions inside the database, not in files
            // of their own: each indexes under a synthetic `<db>#<id>`
            // path, read whole whenever its stat moves. Runs before the
            // token scan below so its fingerprint check compares against
            // the last scan's committed value — after `scanOpenCode` runs,
            // the stored fingerprint is always the current one.
            for database in openCodeDatabases {
                try indexOpenCodeSessions(database, indexer: indexer,
                                          recentLimit: recentLimit, report: &report)
                advanceProgress(owner)
                await Task.yield()
            }
        }
        for database in openCodeDatabases {
            report.databasesRead += try scanSource(database, kind: HarnessMark("opencode").name,
                                                   scan: { try DatabaseSources.scanOpenCode($0, store: $1) },
                                                   report: &report)
            advanceProgress(owner)
        }
        for database in devinDatabases {
            report.databasesRead += try scanSource(database, kind: HarnessMark("devin").name,
                                                   scan: DatabaseSources.scanDevin, report: &report)
            advanceProgress(owner)
        }
        if let indexer {
            // Every Devin turn lives in `sessions.db`; each session indexes
            // under a synthetic `<db>#<id>` path like OpenCode's.
            for database in devinDatabases {
                try indexDevinSessions(database, indexer: indexer,
                                       recentLimit: recentLimit, report: &report)
                advanceProgress(owner)
                await Task.yield()
            }
            for file in devinTranscripts {
                if Task.isCancelled { throw CancellationError() }
                indexWholeFile(file, harness: .devin, indexer: indexer, report: &report)
                advanceProgress(owner)
                await Task.yield()
            }
        }
        for (home, files) in piFiles {
            for file in files {
                if Task.isCancelled { throw CancellationError() }
                try scanFile(file, accountHome: home.path, parser: PiFileParser.self, seen: &seen, report: &report)
                advanceProgress(owner)
                await Task.yield()
            }
        }
        for (home, files) in droidFiles {
            for file in files {
                if Task.isCancelled { throw CancellationError() }
                try scanSessionFile(file, accountHome: home.path,
                                    watched: SessionTotalsFile.droidTranscript(for: file),
                                    parse: { try SessionTotalsFile.droid(settings: $0, accountHome: home.path) },
                                    report: &report)
                advanceProgress(owner)
                await Task.yield()
            }
        }
        for (home, files) in clineFiles {
            for file in files {
                if Task.isCancelled { throw CancellationError() }
                try scanSessionFile(file, accountHome: home.path,
                                    parse: { try SessionTotalsFile.cline(session: $0, accountHome: home.path) },
                                    report: &report)
                advanceProgress(owner)
                await Task.yield()
            }
        }
        if let indexer {
            // Every Kimi session reads the same session_index.jsonl; parse
            // it once per scan rather than once per session (N² lines).
            var kimiWorkDirs = KimiWorkDirCache()
            for job in indexJobs {
                for file in job.files {
                    if Task.isCancelled { throw CancellationError() }
                    let pathSessionID = job.source.sessionIDFromPath?(file)
                    switch job.harness {
                    case .cline:
                        indexClineFile(file, indexer: indexer, report: &report)
                    case .rovo:
                        indexRovoFile(file, sessionID: pathSessionID,
                                      indexer: indexer, report: &report)
                    case .kiro:
                        indexKiroFile(file, sessionID: pathSessionID,
                                      indexer: indexer, report: &report)
                    case .grok:
                        indexGrokFile(file, sessionID: pathSessionID,
                                      indexer: indexer, report: &report)
                    case .kimi:
                        indexKimiFile(file, sessionID: pathSessionID, workDirs: &kimiWorkDirs,
                                      indexer: indexer, report: &report)
                    case .cursor:
                        indexCursorFile(file, metaIndex: job.cursorMeta,
                                        indexer: indexer, report: &report)
                    case .antigravity:
                        if file.lastPathComponent == "transcript_full.jsonl" {
                            // The compact sibling's predicate flipped to
                            // skip the moment this file existed — its
                            // already-indexed row would otherwise keep
                            // answering searches forever.
                            let compact = file.deletingLastPathComponent()
                                .appendingPathComponent("transcript.jsonl")
                            try? indexer.store.removeFile(
                                compact.resolvingSymlinksInPath().path)
                        }
                        indexJSONLFile(file, harness: job.harness,
                                       sessionID: pathSessionID,
                                       indexer: indexer, report: &report)
                    case .gemini:
                        // Orca's `parseGeminiSessionFile` dispatches on
                        // the extension: `.jsonl` streams, the legacy
                        // `.json` checkpoint reads whole.
                        if file.pathExtension.lowercased() == "jsonl" {
                            indexJSONLFile(file, harness: job.harness,
                                           indexer: indexer, report: &report)
                        } else {
                            indexWholeFile(file, harness: job.harness,
                                           indexer: indexer, report: &report)
                        }
                    default:
                        if job.source.isJSONL {
                            indexJSONLFile(file, harness: job.harness, sessionID: pathSessionID,
                                           indexer: indexer, report: &report)
                        } else {
                            indexWholeFile(file, harness: job.harness, sessionID: pathSessionID,
                                           indexer: indexer, report: &report)
                        }
                    }
                    advanceProgress(owner)
                    await Task.yield()
                }
            }
        }
        if let indexer {
            // A failed drain must not fail the scan: whatever is left
            // orphaned is found by the next pass's drain.
            do { try indexer.finishScan() } catch {
                report.notes.append("Index drain failed: \(error.localizedDescription)")
            }
        }
        report.eventsWritten = store.eventWrites - writesBefore
        return report
    }

    /// Index-pure transcript sources: `SearchTranscriptSource` rows with a
    /// decoder whose files feed no ledger records (cursor's usage is an API
    /// feed; droid and cline ride `scanSessionFile` for totals only).
    /// `transcriptRoots` holds the default root set; the explicit fields win
    /// wherever they overlap — claude/codex/pi/devin's passes already
    /// enumerate them.
    private static func indexJobs(roots: LedgerRoots, recentLimit: Int?) -> [IndexJob] {
        var indexRoots = roots.transcriptRoots
        indexRoots[.cursor] = roots.cursorProjects
        indexRoots[.droid] = roots.droidHomes.flatMap {
            [$0.appendingPathComponent("sessions"),
             $0.appendingPathComponent("projects")]
        }
        indexRoots[.cline] = roots.clineSessions
        var jobs: [IndexJob] = []
        for source in SearchTranscriptSource.table() where source.harness != nil {
            guard let harness = source.harness,
                  let sourceRoots = indexRoots[harness] else { continue }
            for root in sourceRoots {
                // Cursor's chat id → meta.json index, one per scan per
                // chats root: the `chats` tree is the `projects` root's
                // sibling (Orca's `cursorChatsRootFromTranscriptPath`,
                // resolved on the configured root rather than per file).
                let cursorMeta = harness == .cursor
                    ? cursorChatMetaIndex(under: root
                        .deletingLastPathComponent().appendingPathComponent("chats"))
                    : [:]
                jobs.append(IndexJob(
                    source: source, harness: harness, cursorMeta: cursorMeta,
                    files: capped(transcriptFiles(under: root, source: source), recentLimit: recentLimit)))
            }
        }
        return jobs
    }

    /// One index-pure transcript a database does not cover: read whole into
    /// the index, feeding no ledger records. Failures mark the file failed
    /// and become a note, like a bad transcript in `scanFile`.
    private func indexWholeFile(
        _ url: URL, harness: SearchDecoder.Harness, sessionID: String? = nil,
        indexer: SearchIndexer, report: inout ScanReport
    ) {
        let path = url.resolvingSymlinksInPath().path
        guard let info = FileInfo(path: path) else { return }
        let stat = SearchFileStat(
            ino: Int64(info.inode), mtimeMs: info.modified * 1000, sizeBytes: Int64(info.size))
        switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
        case .current, .skip:
            return
        case .append, .whole:
            break
        }
        do {
            // Tens of MB of pretty-printed JSON in one blob is fine — the
            // decoder treats the document as one line.
            let data = try Data(contentsOf: URL(fileURLWithPath: path))
            indexer.beginFile(path: path, harness: harness, sessionID: sessionID)
            indexer.consume(line: data)
            if try indexer.finishFile(
                path: path, stat: stat,
                // The pair fingerprint: `readNeed` expects byteOffset ==
                // sizeBytes for `.current`, and sizeBytes now covers the
                // sidecar too (same convention the OpenCode path uses).
                byteOffset: stat.sizeBytes ?? Int64(info.size)) {
                report.filesIndexed += 1
            }
        } catch {
            indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
            report.notes.append("Index failed \(path): \(error.localizedDescription)")
        }
    }

    /// One index-pure JSONL transcript: `JSONLReader` streams its lines into
    /// the decoder — never one `Data` blob the way `indexWholeFile` feeds a
    /// document, since multi-line JSONL is not one JSON value. No ledger
    /// cursor exists for these files, so `readNeed` never answers `.append`
    /// and every owed read starts at byte zero; the committed offset stops
    /// short of a half-written tail line, which the next pass then re-reads.
    /// - Parameter extraMtimeMs: a watched sibling's stamp blended into the
    ///   stat — Orca's `contentDependencyPath` — so a sidecar-only rewrite
    ///   still re-reads the file it describes.
    /// - Parameter sessionID: the session id the records never carry — the
    ///   source row's `sessionIDFromPath` for antigravity/rovo/muse.
    /// - Parameter fieldsOverlay: evaluated only when a read is owed; the
    ///   returned fields are gap-filled into the file's commit transaction.
    /// - Returns: whether the index committed, so callers can chain a
    ///   sidecar enrichment onto a file that actually landed.
    @discardableResult
    private func indexJSONLFile(
        _ url: URL, harness: SearchDecoder.Harness, sessionID: String? = nil,
        indexer: SearchIndexer,
        report: inout ScanReport, extraMtimeMs: Double? = nil,
        fieldsOverlay: (() -> SearchSessionFields?)? = nil
    ) -> Bool {
        let path = url.resolvingSymlinksInPath().path
        guard let info = FileInfo(path: path) else { return false }
        let stat = SearchFileStat(
            ino: Int64(info.inode),
            mtimeMs: max(info.modified * 1000, extraMtimeMs ?? 0),
            sizeBytes: Int64(info.size))
        switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
        case .current, .skip:
            return false
        case .append, .whole:
            break
        }
        do {
            indexer.beginFile(path: path, harness: harness, sessionID: sessionID)
            if let overlay = fieldsOverlay?() {
                indexer.setFieldOverlay(overlay)
            }
            let result = try reader.read(URL(fileURLWithPath: path), from: 0) { line in
                indexer.consume(line: line)
            }
            report.skippedLines += result.skippedLines
            if try indexer.finishFile(path: path, stat: stat, byteOffset: Int64(result.endOffset)) {
                report.filesIndexed += 1
                return true
            }
            return false
        } catch {
            indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
            report.notes.append("Index failed \(path): \(error.localizedDescription)")
            return false
        }
    }

    /// Cursor keeps transcript and metadata in two trees:
    /// `<cursor>/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` holds the
    /// messages while `<cursor>/chats/<md5 of cwd>/<id>/meta.json` holds the
    /// title, cwd and timestamps the transcript never writes (Orca's
    /// `session-scanner-cursor-chat-meta.ts`). `metaIndex` joins them — the
    /// md5 hashes the very cwd being looked up, so the chat id can only
    /// resolve through the per-scan index the caller built.
    private func indexCursorFile(
        _ url: URL, metaIndex: [String: URL], indexer: SearchIndexer, report: inout ScanReport
    ) {
        let path = url.resolvingSymlinksInPath().path
        let metaURL = Self.cursorChatId(forTranscript: path).flatMap { metaIndex[$0] }
        indexJSONLFile(
            url, harness: .cursor, indexer: indexer, report: &report,
            extraMtimeMs: metaURL.flatMap { FileInfo(path: $0.path)?.modified }.map { $0 * 1000 },
            // The overlay is read only when a read is owed, and merges into
            // the file's own commit: a meta write that fails takes the file
            // down with it instead of stranding behind a `current` row.
            fieldsOverlay: metaURL.map { metaURL in { Self.cursorMetaFields(metaURL) } })
    }

    /// The fields a Cursor transcript never records, from the chat's
    /// `meta.json` — title, cwd and timestamps. Gap-fill happens in
    /// `finishFile`: the transcript's own records keep precedence (Orca's
    /// `mergeCursorChatMeta`).
    static func cursorMetaFields(_ metaURL: URL) -> SearchSessionFields? {
        guard let meta = try? JSONSerialization.jsonObject(with: Data(contentsOf: metaURL))
                as? [String: Any]
        else { return nil }
        return SearchSessionFields(
            agent: "cursor", sessionID: "", filePath: "",
            title: JSONValue.string(meta["title"])
                .flatMap { SearchText.titleCandidate($0, limit: 96) } ?? "",
            cwd: JSONValue.string(meta["cwd"]), cwdKey: nil,
            createdAt: SearchStamp.iso(meta["createdAtMs"]),
            updatedAt: SearchStamp.iso(meta["updatedAtMs"]),
            messageCount: 0)
    }

    /// One Cline session = the `<id>.json` manifest plus its
    /// `<id>.messages.json` sibling, decoded under one `beginFile` in Orca's
    /// order — manifest first so the messages document's first user turn can
    /// still outrank the manifest's `prompt` for the title. The sibling's
    /// mtime blends into the stat (Orca's `contentDependencyPath`), so a
    /// messages-only rewrite re-reads the pair; the byte cursor tracks the
    /// manifest alone, the file this row claims to have read. A missing or
    /// malformed sibling still yields a metadata-only session, as Orca's
    /// does.
    private func indexClineFile(
        _ url: URL, indexer: SearchIndexer, report: inout ScanReport
    ) {
        let path = url.resolvingSymlinksInPath().path
        guard let info = FileInfo(path: path) else { return }
        let messagesURL = URL(fileURLWithPath: path)
            .deletingPathExtension().appendingPathExtension("messages.json")
        let messagesInfo = FileInfo(path: messagesURL.path)
        let stat = SearchFileStat(
            ino: Int64(info.inode),
            mtimeMs: max(info.modified, messagesInfo?.modified ?? 0) * 1000,
            sizeBytes: Int64(info.size + (messagesInfo?.size ?? 0)))
        switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
        case .current, .skip:
            return
        case .append, .whole:
            break
        }
        do {
            indexer.beginFile(path: path, harness: .cline)
            // Whole JSON documents, one `consume` each — the same unit
            // `indexWholeFile` feeds. A document that fails to parse simply
            // contributes nothing; the cursor still advances.
            if let manifest = try? JSONSerialization.jsonObject(
                with: Data(contentsOf: URL(fileURLWithPath: path))) as? [String: Any] {
                indexer.consume(object: manifest)
            }
            if let messages = try? JSONSerialization.jsonObject(
                with: Data(contentsOf: messagesURL)) as? [String: Any] {
                indexer.consume(object: messages)
            }
            if try indexer.finishFile(
                path: path, stat: stat,
                // The pair fingerprint: `readNeed` expects byteOffset ==
                // sizeBytes for `.current`, and sizeBytes now covers the
                // sidecar too (same convention the OpenCode path uses).
                byteOffset: stat.sizeBytes ?? Int64(info.size)) {
                report.filesIndexed += 1
            }
        } catch {
            indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
            report.notes.append("Index failed \(path): \(error.localizedDescription)")
        }
    }

    /// One Rovo session = `metadata.json` plus its `session_context.json`
    /// sibling under one `beginFile` (Orca's `parseRovoSessionFile`). The
    /// sibling's mtime blends into the stat (`contentDependencyPath`), and a
    /// malformed manifest commits `.none` — Orca returns before reading the
    /// context, so the pair must not index a bare session either.
    private func indexRovoFile(
        _ url: URL, sessionID: String?, indexer: SearchIndexer, report: inout ScanReport
    ) {
        let path = url.resolvingSymlinksInPath().path
        guard let info = FileInfo(path: path) else { return }
        let contextURL = URL(fileURLWithPath: path)
            .deletingLastPathComponent().appendingPathComponent("session_context.json")
        // The stat covers both files — size as well as mtime — so a
        // sidecar delete or rewrite can never leave stale turns indexed
        // behind an unchanged fingerprint.
        let contextInfo = FileInfo(path: contextURL.path)
        let stat = SearchFileStat(
            ino: Int64(info.inode),
            mtimeMs: max(info.modified, contextInfo?.modified ?? 0) * 1000,
            sizeBytes: Int64(info.size + (contextInfo?.size ?? 0)))
        switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
        case .current, .skip:
            return
        case .append, .whole:
            break
        }
        do {
            let manifest = try? JSONSerialization.jsonObject(
                with: Data(contentsOf: URL(fileURLWithPath: path))) as? [String: Any]
            indexer.beginFile(path: path, harness: .rovo, sessionID: manifest == nil ? nil : sessionID)
            if let manifest {
                indexer.consume(object: manifest)
                // `updateTimeline`: the manifest's own stamps fold into the
                // session's range (snake then camel, Orca's order; a JSON
                // null is as absent as a missing key).
                indexer.consumeTimeline(Self.firstPresent(
                    manifest["created_at"], manifest["createdAt"]))
                indexer.consumeTimeline(Self.firstPresent(
                    manifest["updated_at"], manifest["updatedAt"]))
                if let context = try? JSONSerialization.jsonObject(
                    with: Data(contentsOf: contextURL)) as? [String: Any] {
                    indexer.consume(object: context)
                }
            }
            if try indexer.finishFile(
                path: path, stat: stat,
                // The pair fingerprint: `readNeed` expects byteOffset ==
                // sizeBytes for `.current`, and sizeBytes now covers the
                // sidecar too (same convention the OpenCode path uses).
                byteOffset: stat.sizeBytes ?? Int64(info.size)) {
                report.filesIndexed += 1
            }
        } catch {
            indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
            report.notes.append("Index failed \(path): \(error.localizedDescription)")
        }
    }

    /// One Kiro session = the `<uuid>.json` manifest plus its streamed
    /// `<uuid>.jsonl` sibling (Orca's `parseKiroSessionFile`). A
    /// `parent_session_id` marks a subagent's child session
    /// (`listedKiroMetadata`) and a malformed manifest commits `.none` —
    /// Orca returns before reading the transcript either way. The
    /// transcript's mtime blends into the stat; a missing transcript still
    /// yields the metadata-only session, as Orca's does.
    private func indexKiroFile(
        _ url: URL, sessionID: String?, indexer: SearchIndexer, report: inout ScanReport
    ) {
        let path = url.resolvingSymlinksInPath().path
        guard let info = FileInfo(path: path) else { return }
        let transcriptURL = URL(fileURLWithPath: path + "l")
        let transcriptInfo = FileInfo(path: transcriptURL.path)
        let stat = SearchFileStat(
            ino: Int64(info.inode),
            mtimeMs: max(info.modified, transcriptInfo?.modified ?? 0) * 1000,
            sizeBytes: Int64(info.size + (transcriptInfo?.size ?? 0)))
        switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
        case .current, .skip:
            return
        case .append, .whole:
            break
        }
        do {
            let manifest = try? JSONSerialization.jsonObject(
                with: Data(contentsOf: URL(fileURLWithPath: path))) as? [String: Any]
            let listed = manifest != nil && JSONValue.string(manifest?["parent_session_id"]) == nil
            // The session id is the validated UUID filename — but only for
            // a listed session: a child manifest (`parent_session_id`) or
            // a malformed one commits `.none` like Orca's early return.
            indexer.beginFile(
                path: path, harness: .kiro, sessionID: listed ? sessionID : nil)
            if listed {
                indexer.consume(object: manifest!)
                indexer.consumeTimeline(manifest?["created_at"])
                indexer.consumeTimeline(manifest?["updated_at"])
                if FileManager.default.fileExists(atPath: transcriptURL.path) {
                    let result = try reader.read(transcriptURL, from: 0) { indexer.consume(line: $0) }
                    report.skippedLines += result.skippedLines
                }
            }
            if try indexer.finishFile(
                path: path, stat: stat,
                // The pair fingerprint: `readNeed` expects byteOffset ==
                // sizeBytes for `.current`, and sizeBytes now covers the
                // sidecar too (same convention the OpenCode path uses).
                byteOffset: stat.sizeBytes ?? Int64(info.size)) {
                report.filesIndexed += 1
            }
        } catch {
            indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
            report.notes.append("Index failed \(path): \(error.localizedDescription)")
        }
    }

    /// One Grok session = `summary.json` plus its streamed
    /// `chat_history.jsonl` sibling (Orca's `parseGrokSessionFile`). A
    /// malformed summary commits `.none` — Orca returns before reading the
    /// history; the history's mtime blends into the stat, and a missing
    /// history still yields the summary-only session.
    private func indexGrokFile(
        _ url: URL, sessionID: String?, indexer: SearchIndexer, report: inout ScanReport
    ) {
        let path = url.resolvingSymlinksInPath().path
        guard let info = FileInfo(path: path) else { return }
        let historyURL = URL(fileURLWithPath: path)
            .deletingLastPathComponent().appendingPathComponent("chat_history.jsonl")
        let historyInfo = FileInfo(path: historyURL.path)
        let stat = SearchFileStat(
            ino: Int64(info.inode),
            mtimeMs: max(info.modified, historyInfo?.modified ?? 0) * 1000,
            sizeBytes: Int64(info.size + (historyInfo?.size ?? 0)))
        switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
        case .current, .skip:
            return
        case .append, .whole:
            break
        }
        do {
            let manifest = try? JSONSerialization.jsonObject(
                with: Data(contentsOf: URL(fileURLWithPath: path))) as? [String: Any]
            indexer.beginFile(path: path, harness: .grok, sessionID: manifest == nil ? nil : sessionID)
            if let manifest {
                indexer.consume(object: manifest)
                indexer.consumeTimeline(manifest["created_at"])
                indexer.consumeTimeline(manifest["updated_at"])
                indexer.consumeTimeline(manifest["last_active_at"])
                if FileManager.default.fileExists(atPath: historyURL.path) {
                    let result = try reader.read(historyURL, from: 0) { indexer.consume(line: $0) }
                    report.skippedLines += result.skippedLines
                }
            }
            if try indexer.finishFile(
                path: path, stat: stat,
                // The pair fingerprint: `readNeed` expects byteOffset ==
                // sizeBytes for `.current`, and sizeBytes now covers the
                // sidecar too (same convention the OpenCode path uses).
                byteOffset: stat.sizeBytes ?? Int64(info.size)) {
                report.filesIndexed += 1
            }
        } catch {
            indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
            report.notes.append("Index failed \(path): \(error.localizedDescription)")
        }
    }

    /// One Kimi session = `state.json` plus the streamed
    /// `agents/<primary>/wire.jsonl` sibling, plus the work dir the shared
    /// `session_index.jsonl` maps it to (Orca's `parseKimiSessionFile` —
    /// the index lives four levels up beside `sessions/`). A malformed
    /// manifest commits `.none`; a missing wire transcript still yields the
    /// metadata-only session.
    private func indexKimiFile(
        _ url: URL, sessionID: String?, workDirs: inout KimiWorkDirCache,
        indexer: SearchIndexer, report: inout ScanReport
    ) {
        let path = url.resolvingSymlinksInPath().path
        guard let info = FileInfo(path: path) else { return }
        let wireURL = SearchTranscriptSource.kimiWireURL(forStateFile: URL(fileURLWithPath: path))
        // `session_index.jsonl` supplies the work dir, so its stamp blends
        // into freshness alongside the wire transcript — a workdir-only
        // change still re-reads.
        let indexURL = Self.kimiSessionIndexURL(forStateFile: path)
        let wireInfo = FileInfo(path: wireURL.path)
        let sessionIndexInfo = FileInfo(path: indexURL.path)
        let stat = SearchFileStat(
            ino: Int64(info.inode),
            mtimeMs: max(info.modified, wireInfo?.modified ?? 0,
                         sessionIndexInfo?.modified ?? 0) * 1000,
            sizeBytes: Int64(info.size + (wireInfo?.size ?? 0)
                             + (sessionIndexInfo?.size ?? 0)))
        switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
        case .current, .skip:
            return
        case .append, .whole:
            break
        }
        do {
            let manifest = try? JSONSerialization.jsonObject(
                with: Data(contentsOf: URL(fileURLWithPath: path))) as? [String: Any]
            indexer.beginFile(path: path, harness: .kimi, sessionID: manifest == nil ? nil : sessionID)
            if let manifest {
                indexer.consume(object: manifest)
                indexer.consumeTimeline(manifest["createdAt"])
                indexer.consumeTimeline(manifest["updatedAt"])
                indexer.setFieldOverlay(Self.manifestFields(
                    // `lastPrompt` is the last-resort title — it fills a
                    // session the wire transcript never retitled.
                    title: manifest["lastPrompt"],
                    cwd: sessionID.flatMap {
                        workDirs.map(forStateFile: path, indexInfo: sessionIndexInfo)[$0]
                    }))
                if FileManager.default.fileExists(atPath: wireURL.path) {
                    let result = try reader.read(wireURL, from: 0) { indexer.consume(line: $0) }
                    report.skippedLines += result.skippedLines
                }
                indexer.consumeEndOfFile()
            }
            if try indexer.finishFile(
                path: path, stat: stat,
                // The pair fingerprint: `readNeed` expects byteOffset ==
                // sizeBytes for `.current`, and sizeBytes now covers the
                // sidecar too (same convention the OpenCode path uses).
                byteOffset: stat.sizeBytes ?? Int64(info.size)) {
                report.filesIndexed += 1
            }
        } catch {
            indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
            report.notes.append("Index failed \(path): \(error.localizedDescription)")
        }
    }

    /// `<KIMI_CODE_HOME>/session_index.jsonl` — the shared session→workdir
    /// map four levels above a `state.json` (Orca's
    /// `kimiSessionIndexPathFromStatePath`).
    static func kimiSessionIndexURL(forStateFile path: String) -> URL {
        URL(fileURLWithPath: path)
            .deletingLastPathComponent()  // session_<uuid>
            .deletingLastPathComponent()  // wd_<name>_<hash>
            .deletingLastPathComponent()  // sessions
            .deletingLastPathComponent()  // <KIMI_CODE_HOME>
            .appendingPathComponent("session_index.jsonl")
    }

    /// `readKimiWorkDirBySessionId`: `session_index.jsonl` folded
    /// later-wins into `sessionId → workDir`.
    static func kimiWorkDirs(forStateFile path: String) -> [String: String] {
        let indexURL = kimiSessionIndexURL(forStateFile: path)
        guard let content = try? String(contentsOf: indexURL, encoding: .utf8) else { return [:] }
        var map: [String: String] = [:]
        for line in content.split(separator: "\n") {
            guard let record = try? JSONSerialization.jsonObject(with: Data(line.utf8))
                as? [String: Any],
                let sessionId = JSONValue.string(record["sessionId"]),
                let workDir = JSONValue.string(record["workDir"])
            else { continue }
            map[sessionId] = workDir
        }
        return map
    }

    /// The first value that exists and is not JSON null — Orca's
    /// `extractString(a) ?? extractString(b)` over snake/camel key pairs.
    private static func firstPresent(_ values: Any?...) -> Any? {
        values.lazy.compactMap { $0 }.first { !($0 is NSNull) }
    }

    /// The gap-fill fields a paired-file session's manifest carries that no
    /// record decodes to — kimi's `lastPrompt` fallback title and the
    /// session-index workDir.
    private static func manifestFields(
        title: Any?, cwd: String? = nil
    ) -> SearchSessionFields {
        SearchSessionFields(
            agent: "kimi", sessionID: "", filePath: "",
            title: JSONValue.string(title)
                .flatMap { SearchText.titleCandidate($0, limit: 96) } ?? "",
            cwd: cwd, cwdKey: nil,
            createdAt: nil, updatedAt: nil, messageCount: 0)
    }

    /// chatId → `meta.json` under one Cursor chats root, indexed once per
    /// scan: `<chats>/<md5-of-cwd>/<chatId>/meta.json`, first hit wins
    /// (Orca's `buildCursorChatMetaIndex`).
    static func cursorChatMetaIndex(under chatsRoot: URL) -> [String: URL] {
        var index: [String: URL] = [:]
        guard let workspaces = try? FileManager.default.contentsOfDirectory(
            at: chatsRoot, includingPropertiesForKeys: [.isDirectoryKey])
        else { return index }
        for workspace in workspaces.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            guard (try? workspace.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true,
                  let chats = try? FileManager.default.contentsOfDirectory(
                      at: workspace, includingPropertiesForKeys: [.isDirectoryKey])
            else { continue }
            for chat in chats where index[chat.lastPathComponent] == nil {
                let meta = chat.appendingPathComponent("meta.json")
                if FileManager.default.fileExists(atPath: meta.path) {
                    index[chat.lastPathComponent] = meta
                }
            }
        }
        return index
    }

    /// The chat id of a Cursor transcript: its parent directory's name when
    /// the grandparent is `agent-transcripts` (Orca's
    /// `cursorChatIdFromTranscriptPath`).
    static func cursorChatId(forTranscript path: String) -> String? {
        let chatDir = URL(fileURLWithPath: path).deletingLastPathComponent()
        return chatDir.deletingLastPathComponent().lastPathComponent == "agent-transcripts"
            ? chatDir.lastPathComponent : nil
    }

    /// OpenCode's sessions live inside `opencode.db`: each is indexed under
    /// a synthetic `<dbPath>#<sessionId>` path whose stat is the session's
    /// last-activity time and message count, so a session re-reads only
    /// when its content actually grew. A synthetic read is always whole —
    /// a row in a container has no byte offset the index could resume from.
    ///
    /// The database is opened read-only once for the whole pass, like
    /// `scanSource` opens it for its one read: a database that will not
    /// enumerate — locked mid-rebuild, a schema this build does not know —
    /// becomes one note instead of aborting the scan, and a session that
    /// fails its own read is held out after three failures like a file.
    ///
    /// The pass is skipped entirely when the database cannot have moved:
    /// the file and its WAL still match the fingerprint the token scan
    /// last committed, so no session's stat can differ from the one its
    /// synthetic row recorded — unless that row's own state says it still
    /// owes a read (`due`, a partial sentinel, or a failure not yet held
    /// out: its recorded mtime is the stat an unchanged database would
    /// still report).
    private func indexOpenCodeSessions(
        _ url: URL, indexer: SearchIndexer, recentLimit: Int?, report: inout ScanReport
    ) throws {
        let dbPath = url.resolvingSymlinksInPath().path
        let metaKey = DatabaseSources.openCodeMetaKey(url)
        let indexed = (try? indexer.store.files(withPrefix: dbPath + "#")) ?? []
        let unchanged = (try? DatabaseSources.sourceFingerprintUnchanged(
            at: url, metaKey: metaKey, store: store)) ?? false
        // The last enumeration may have been capped: sessions past the cap
        // hold no synthetic row, so an unchanged fingerprint alone must not
        // skip them. `partial` records that the census was truncated.
        let partial = (try? store.meta(metaKey + ":partial")) == "1"
        // `indexed` must be non-empty too: a matching fingerprint with no
        // synthetic rows means the index was cleared (or never ran at this
        // fingerprint), not that the database holds nothing.
        if unchanged && !partial && !indexed.isEmpty
           && indexed.allSatisfy({ !$0.owesRead }) { return }
        let source: SQLiteDatabase
        let sessions: [SearchOpenCode.Session]
        do {
            source = try DatabaseSources.openReadOnly(at: url.path)
            sessions = try SearchOpenCode.sessions(in: source, limit: recentLimit)
            let truncated = recentLimit.map { sessions.count >= $0 } ?? false
            if truncated != partial {
                try? store.setMeta(metaKey + ":partial", truncated ? "1" : "0")
            }
        } catch {
            report.notes.append(
                "Skipped \(HarnessMark("opencode").name)'s database (\(url.path)): \(error.localizedDescription)")
            return
        }
        var live: Set<String> = []
        live.reserveCapacity(sessions.count)
        for session in sessions {
            if Task.isCancelled { throw CancellationError() }
            let path = SearchOpenCode.syntheticPath(dbPath: dbPath, sessionID: session.id)
            live.insert(path)
            let stat = session.stat
            switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
            case .current, .skip:
                continue
            case .append, .whole:
                // No ledger offset ever passes in, so `readNeed` never
                // answers `.append`; a whole read covers either case.
                break
            }
            // The fence is the row as it stood before the read.
            let before = try? indexer.store.fileRow(for: path)
            do {
                let messages = try SearchOpenCode.messages(in: source, sessionID: session.id)
                let fields = SearchSessionFields(
                    agent: "opencode", sessionID: session.id, filePath: path,
                    title: SearchOpenCode.title(of: session, messages: messages),
                    cwd: session.directory, cwdKey: session.directory,
                    createdAt: SearchOpenCode.iso(ms: session.timeCreatedMs),
                    updatedAt: SearchOpenCode.iso(ms: session.lastActivityMs),
                    messageCount: messages.count)
                if try indexer.finishSession(path: path, stat: stat, fileRow: before,
                                             fields: fields, messages: messages) {
                    report.filesIndexed += 1
                }
            } catch {
                // Like a bad transcript in `scanFile`: the failure records
                // against the stat — three at one stat holds the session
                // out — and the rest of the database still indexes.
                indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
                report.notes.append("Index failed \(path): \(error.localizedDescription)")
            }
        }
        // A limited enumeration is not a census: a truncated list proves
        // nothing about the sessions past the cap, so nothing retires.
        if recentLimit.map({ sessions.count < $0 }) ?? true {
            retireStaleSyntheticSessions(
                dbPath: dbPath, indexed: indexed, live: live, indexer: indexer, report: &report)
        }
    }

    /// Devin's sessions live inside `sessions.db`: each indexes under a
    /// synthetic `<dbPath>#<sessionId>` path whose stat is the session's
    /// last-activity time and node count (`SearchDevin.Session.stat`), read
    /// whenever that moves: only the nodes past the mark the last commit
    /// left (`SearchDevin.Mark`, either writer's), else the whole chain.
    /// Opened read-only once per pass; a
    /// database that will not enumerate becomes one note, and a session
    /// that fails its own read is held out after three failures like a file.
    private func indexDevinSessions(
        _ url: URL, indexer: SearchIndexer, recentLimit: Int?, report: inout ScanReport
    ) throws {
        let dbPath = url.resolvingSymlinksInPath().path
        // Retirement candidates come from before the listing: a row another
        // writer adds for a session created after it is no candidate.
        let indexed = (try? indexer.store.files(withPrefix: dbPath + "#")) ?? []
        let source: SQLiteDatabase
        let sessions: [SearchDevin.Session]
        do {
            source = try DatabaseSources.openReadOnly(at: url.path)
            sessions = try SearchDevin.sessions(in: source, limit: recentLimit)
        } catch {
            report.notes.append(
                "Skipped \(HarnessMark("devin").name)'s database (\(url.path)): \(error.localizedDescription)")
            return
        }
        let ino = FileInfo(path: dbPath)?.inode ?? 0
        var live: Set<String> = []
        live.reserveCapacity(sessions.count)
        for session in sessions {
            if Task.isCancelled { throw CancellationError() }
            let path = SearchOpenCode.syntheticPath(dbPath: dbPath, sessionID: session.id)
            live.insert(path)
            let stat = session.stat
            switch indexer.readNeed(path: path, stat: stat, ledgerOffset: nil) {
            case .current, .skip:
                continue
            case .append, .whole:
                break
            }
            // The fence is the row as it stood before the read.
            let before = try? indexer.store.fileRow(for: path)
            do {
                let held = Self.heldDevinMark(before, store: indexer.store, path: path, ino: ino)
                let read = try SearchDevin.read(in: source, session: session, mark: held?.mark, ino: ino)
                let fields = SearchSessionFields(
                    agent: "devin", sessionID: session.id, filePath: path,
                    title: SearchDevin.title(of: session, promptTitle: read.promptTitle),
                    cwd: session.directory, cwdKey: session.directory,
                    createdAt: SearchOpenCode.iso(ms: session.createdMs),
                    updatedAt: SearchOpenCode.iso(ms: session.lastActivityMs),
                    messageCount: read.messages.count
                        + (read.appended ? held?.messageCount ?? 0 : 0))
                if try indexer.finishSession(path: path, stat: stat, fileRow: before,
                                             fields: fields, messages: read.messages,
                                             appended: read.appended, mark: read.mark) {
                    report.filesIndexed += 1
                }
            } catch {
                indexer.failFile(path: path, mtimeMs: stat.mtimeMs)
                report.notes.append("Index failed \(path): \(error.localizedDescription)")
            }
        }
        if recentLimit.map({ sessions.count < $0 }) ?? true {
            retireStaleSyntheticSessions(
                dbPath: dbPath, indexed: indexed, live: live, indexer: indexer, report: &report)
        }
    }

    /// The mark committed with `row`, and the message count of the session
    /// it continues, when an append may use them: the row is current at its
    /// own size and the mark was written with this very row (same session,
    /// same node count, same database file). Python's `_held_mark`.
    private static func heldDevinMark(
        _ row: SearchFileRow?, store: SearchStore, path: String, ino: UInt64
    ) -> (mark: SearchDevin.Mark, messageCount: Int)? {
        guard let row, row.state == .current, let sessionRowID = row.sessionRowID,
              row.byteOffset == row.sizeBytes,
              let mark = SearchDevin.Mark(parsing: try? store.meta(SearchDevin.markKey(path))),
              mark.session == sessionRowID, mark.nodes == row.sizeBytes, mark.ino == ino,
              let existing = try? store.sessionRow(sessionRowID)
        else { return nil }
        return (mark, existing.fields.messageCount)
    }

    /// Rows for sessions the enumeration no longer returns — the synthetic
    /// equivalent of a transcript file deleted from its directory. Only a
    /// complete, non-empty enumeration retires anything: a database that
    /// listed zero sessions proves nothing about the ids it holds (Orca's
    /// census rule in `session-search-synthetic-sources.ts`), and a failed
    /// enumeration never reaches here. `indexed` is the caller's
    /// `files(withPrefix:)` fetch, so this never dumps the whole table.
    private func retireStaleSyntheticSessions(
        dbPath: String, indexed: [SearchFileRow], live: Set<String>,
        indexer: SearchIndexer, report: inout ScanReport
    ) {
        guard !live.isEmpty else { return }
        do {
            for row in indexed where !live.contains(row.path) {
                try indexer.store.removeFile(row.path)
            }
        } catch {
            report.notes.append("Index retire failed \(dbPath): \(error.localizedDescription)")
        }
    }

    /// One database source. A failure reading the source becomes a note and
    /// a skip: the file can be locked or rebuilt by the app that owns it at
    /// any moment, and one unreadable source is no reason to drop the
    /// others. A failure on SWE's own cache still throws, so the
    /// Overview shows the error and the scan does not count as complete.
    private func scanSource(
        _ url: URL, kind: String,
        scan: (URL, LedgerStore) throws -> Bool, report: inout ScanReport
    ) throws -> Int {
        do {
            return try scan(url, store) ? 1 : 0
        } catch let error as DatabaseSources.SourceRead {
            report.notes.append(
                "Skipped \(kind)'s database (\(url.path)): \(error.underlying.localizedDescription)")
            return 0
        }
    }

    /// - Parameter indexes: false for a mirror copy, which feeds the
    ///   ledger only: its search rows are filed under its source path, by
    ///   `SearchMirror`, never under the copy's.
    /// - Parameter copyMarks: the index's `mirror_copy:` marks, so this
    ///   file's commit retires the rows a copy put in for its session.
    private func scanFile<P: FileLogParser>(
        _ url: URL, accountHome: String, parser: P.Type, seen: inout Set<String>, report: inout ScanReport,
        indexes: Bool = true,
        copyMarks: [TranscriptMirror.SessionIdentity: [(path: String, session: Int64)]] = [:]
    ) throws {
        let path = url.resolvingSymlinksInPath().path
        guard seen.insert(path).inserted, let info = FileInfo(path: path) else { return }
        let saved = try store.cursor(for: path)
        let indexStat = SearchFileStat(
            ino: Int64(info.inode), mtimeMs: info.modified * 1000, sizeBytes: Int64(info.size))
        let fileIndexer = indexes ? self.indexer : nil
        let indexNeed = fileIndexer?.readNeed(
            path: path, stat: indexStat, ledgerOffset: saved?.offset,
            harness: P.searchHarness) ?? .skip
        let drop = fileIndexer.map {
            SearchMirror.copiedRows(store: $0.store, marks: copyMarks,
                                    harness: P.searchHarness.rawValue, path: path)
        } ?? []
        if indexNeed == .current, !drop.isEmpty {
            fileIndexer?.retireCopies(path: path, drop: drop)
        }

        var start: UInt64 = 0
        var state: Data?
        if let saved {
            // `changed` is ctime: a rewrite that preserved size and mtime
            // (a backup restore, a `touch -r` stamp) still moves it, so the
            // fast path no longer trusts the stamp alone. Cursors written
            // before it was recorded keep the check they were saved under.
            // A `.whole` index need forfeits the fast path: the index is
            // behind on a span of its own and must read the file again.
            if saved.inode == info.inode, saved.size == info.size, saved.modified == info.modified,
               (saved.changed ?? info.changed) == info.changed, saved.offset == info.size,
               indexNeed != .whole {
                report.filesUnchanged += 1
                return
            }
            // Resuming is appends only: a file read to its end already
            // (`offset == size`) whose stamps moved is a rewrite, and
            // resuming at EOF would read nothing and re-stamp the cursor
            // over an edit the head/middle/tail samples cannot see.
            // A `.whole` index need upgrades an append to a rescan: the index
            // is behind on a span the appended lines cannot reach.
            if saved.inode == info.inode, info.size > saved.offset,
               Self.fingerprintsMatch(saved, path: path), indexNeed != .whole {
                start = saved.offset
                state = saved.state
            } else {
                report.filesRescanned += 1
            }
        }

        // The index consumes the same read when it owes one: `.append` takes
        // the appended lines, `.whole` the file. `.current` and `.skip` never
        // enter the closure. A local binding keeps the line closure
        // nonisolated — capturing `self` here would flag every other capture.
        let indexer = (indexNeed == .append || indexNeed == .whole) ? fileIndexer : nil
        if let indexer {
            indexer.beginFile(path: path, harness: P.searchHarness,
                              appending: indexNeed == .append && start > 0, drop: drop)
        }
        var lineParser = P(file: url, accountHome: accountHome, savedState: state)
        let store = self.store
        var endOffset: UInt64 = info.size
        try store.db.execute("BEGIN IMMEDIATE")
        do {
            let result = try reader.read(URL(fileURLWithPath: path), from: start) { line in
                // One JSONSerialization serves both consumers: the byte
                // gates union, then the parsed object feeds the ledger
                // parser and the index decoder together. A line neither
                // wants — or one that parses to no dictionary — is skipped
                // for the price of the marker checks alone.
                guard lineParser.wants(line: line) || indexer?.wants(line: line) == true,
                      let object = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
                else { return }
                for record in lineParser.records(fromObject: object) {
                    try store.upsert(record)
                }
                indexer?.consume(object: object)
            }
            endOffset = result.endOffset
            let head = Self.hashRange(path: path, from: 0, length: min(Self.fingerprintBytes, result.endOffset))
            let tailLength = min(Self.fingerprintBytes, result.endOffset)
            let tail = Self.hashRange(path: path, from: result.endOffset - tailLength, length: tailLength)
            let mid = Self.middleSample(of: result.endOffset)
            let midHash = Self.hashRange(path: path, from: mid.from, length: mid.length)
            try store.save(FileCursor(
                path: path, inode: info.inode, size: info.size, modified: info.modified,
                changed: info.changed, offset: result.endOffset,
                headHash: head, headLength: Int(min(Self.fingerprintBytes, result.endOffset)),
                tailHash: tail, tailLength: Int(tailLength), midHash: midHash,
                state: lineParser.savedState(),
                skippedLines: (start > 0 ? saved?.skippedLines ?? 0 : 0) + result.skippedLines))
            try store.db.execute("COMMIT")
            report.skippedLines += result.skippedLines
        } catch let failure as SQLiteDatabase.Failure {
            // SWE's own store failing aborts the scan, same as a
            // database source's cache failure: the round is not complete.
            // A staged index read takes its pieces back first, as swe's
            // `_read` does in its `finally`: left on the old session they
            // would repeat its text, and no drain reaches them.
            store.rollback()
            indexer?.abandonFile(path: path)
            throw failure
        } catch {
            // The file itself was unreadable — gone or locked mid-scan,
            // permissions, a bad block. One bad transcript must not take
            // every harness's scan down with it, so like a skipped source
            // it becomes a note. Its cursor is left unsaved, so the next
            // scan retries it. The index counts the failure too — that is
            // what holds an unreadable transcript out after enough tries.
            store.rollback()
            indexer?.failFile(path: path, mtimeMs: indexStat.mtimeMs)
            report.notes.append("Skipped \(path): \(error.localizedDescription)")
            return
        }
        // The index commits outside the ledger's transaction: a failure
        // there marks the file `failed` and earns a note, never an aborted
        // scan.
        if let indexer {
            do {
                if try indexer.finishFile(path: path, stat: indexStat, byteOffset: Int64(endOffset)) {
                    report.filesIndexed += 1
                }
            } catch {
                indexer.failFile(path: path, mtimeMs: indexStat.mtimeMs)
                report.notes.append("Index failed \(path): \(error.localizedDescription)")
            }
        }
        report.filesRead += 1
    }

    // MARK: - Mirror copies (#1918)

    /// `(harness, identity)` of every live Claude and Codex transcript a
    /// full scan enumerated. A uuid-named file needs only its name; any
    /// other name is its own identity, its resolved path.
    static func liveIdentities(claude: [URL], codex: [URL]) -> Set<TranscriptMirror.SessionIdentity> {
        var live = Set<TranscriptMirror.SessionIdentity>()
        for (harness, files) in [("claude", claude), ("codex", codex)] {
            for file in files {
                var identity = TranscriptMirror.SessionIdentity(harness: harness, path: file.path)
                if identity.id == file.path {
                    identity = TranscriptMirror.SessionIdentity(
                        harness: harness, path: file.resolvingSymlinksInPath().path)
                }
                live.insert(identity)
            }
        }
        return live
    }

    /// Usage from the copies of sessions the harness pruned, for events
    /// the ledger never recorded (a session that ran and was pruned
    /// between two scans, or before this ledger existed). Never double
    /// counted: a copy is read only when no live file carries its session
    /// and its source is gone, events keep their content keys
    /// (`message.id` + `requestId`, Codex's response id or session and
    /// timestamp), so a line the ledger already holds upserts onto its own
    /// row with nothing to add, and a copy the source's cursor already
    /// covers is not read at all. The copy's own cursor then keeps it from
    /// being read again. Events are filed under the copy's harness home,
    /// as the live scan filed the source's. Nothing is ever deleted here.
    private func scanCopies(_ pruned: [TranscriptMirror.Pruned], mirror: TranscriptMirror,
                            seen: inout Set<String>, report: inout ScanReport) async throws {
        for copy in pruned {
            if Task.isCancelled { throw CancellationError() }
            guard let home = mirror.home(for: copy.copy.rel, harness: copy.copy.harness),
                  let info = FileInfo(path: copy.copy.path) else { continue }
            if let cursor = try store.cursor(for: copy.source),
               Self.cursor(cursor, covers: info, copy: copy.copy.path) { continue }
            let url = URL(fileURLWithPath: copy.copy.path)
            if copy.copy.harness == "claude" {
                try scanFile(url, accountHome: home.home.path, parser: ClaudeFileParser.self, seen: &seen,
                             report: &report, indexes: false)
            } else {
                try scanFile(url, accountHome: home.home.path, parser: CodexFileParser.self, seen: &seen,
                             report: &report, indexes: false)
            }
            await Task.yield()
        }
    }

    /// Whether the source's cursor proves the ledger already read exactly
    /// the copy's bytes: the copy keeps its source's size and mtime, the
    /// cursor read to that size, and its head, middle and tail samples
    /// match the copy. A source rewritten after the ledger read it (same
    /// or smaller size, new usage) fails one of these, so its copy is read
    /// and event keys drop the lines the ledger already holds. Never a
    /// bare offset comparison: a shorter rewrite would pass it.
    static func cursor(_ cursor: FileCursor, covers info: FileInfo, copy path: String) -> Bool {
        cursor.size == info.size && cursor.offset == info.size && cursor.modified == info.modified
            && fingerprintsMatch(cursor, path: path)
    }

    /// Bytes each fingerprint sample covers. Head, middle and tail of the
    /// range already read are hashed, so a same-length edit anywhere near
    /// the middle still shows.
    private static let fingerprintBytes: UInt64 = 4_096

    /// The `fingerprintBytes`-byte block centered in `offset` bytes. A pure
    /// function of the saved offset, so only the hash is stored.
    private static func middleSample(of offset: UInt64) -> (from: UInt64, length: UInt64) {
        let length = min(fingerprintBytes, offset)
        return ((offset - length) / 2, length)
    }

    private static func fingerprintsMatch(_ cursor: FileCursor, path: String) -> Bool {
        // A malformed cursor must fail the check, not trap: validate every
        // conversion and the tail's reach before subtracting.
        guard let headLength = UInt64(exactly: cursor.headLength),
              let tailLength = UInt64(exactly: cursor.tailLength),
              headLength <= fingerprintBytes, tailLength <= fingerprintBytes,
              tailLength <= cursor.offset
        else { return false }
        guard hashRange(path: path, from: 0, length: headLength) == cursor.headHash,
              hashRange(path: path, from: cursor.offset - tailLength, length: tailLength)
                == cursor.tailHash
        else { return false }
        if let midHash = cursor.midHash {
            let mid = middleSample(of: cursor.offset)
            return hashRange(path: path, from: mid.from, length: mid.length) == midHash
        }
        // Saved before the middle sample existed: trust it only where head
        // and tail already cover the file end to end. Bigger files rescan
        // once, and their new cursor records the middle.
        return headLength + tailLength >= cursor.offset
    }

    private static func hashRange(path: String, from offset: UInt64, length: UInt64) -> Int64 {
        guard length > 0, let handle = FileHandle(forReadingAtPath: path) else { return StableHash.of("") }
        defer { try? handle.close() }
        guard (try? handle.seek(toOffset: offset)) != nil,
              let data = try? handle.read(upToCount: Int(length)), data.count == Int(length)
        else { return 0 }
        return Int64(bitPattern: StableHash.of(data))
    }

    /// Every `*.jsonl` under `directory`, newest first: mtime descending
    /// with the path as tiebreaker, so `recentLimit`'s prefix takes the
    /// live sessions before the backlog and dedup's keep-first stays
    /// deterministic.
    static func jsonlFiles(under directory: URL) -> [URL] {
        files(under: directory) { $0.pathExtension == "jsonl" }
    }

    /// Every file under `directory` ending in `suffix`, same ordering.
    static func files(under directory: URL, suffix: String) -> [URL] {
        files(under: directory) { $0.lastPathComponent.hasSuffix(suffix) }
    }

    /// `files` is mtime-descending: `recentLimit` takes its newest prefix,
    /// and the kept set is re-sorted by path so dedup's keep-first resolves
    /// a cross-file tie exactly as an uncapped scan does.
    private static func capped(_ files: [URL], recentLimit: Int?) -> [URL] {
        files.prefix(recentLimit ?? .max).sorted { $0.path < $1.path }
    }

    /// The shared enumeration for `jsonlFiles`/`files`: matches under
    /// `directory`, newest first by mtime with the path as a deterministic
    /// tiebreak. A file `FileInfo` cannot stat sorts last — whatever
    /// consumes the list checks it again anyway.
    private static func files(under directory: URL, where match: (URL) -> Bool) -> [URL] {
        guard let enumerator = FileManager.default.enumerator(
            at: directory, includingPropertiesForKeys: [.isRegularFileKey], options: [.skipsHiddenFiles])
        else { return [] }
        var files: [URL] = []
        for case let url as URL in enumerator where match(url) {
            files.append(url)
        }
        return newestFirst(files)
    }

    /// Files a `SearchTranscriptSource` row discovers under one root —
    /// Orca's `discoverSourceFiles`: the extension list, the file predicate
    /// and per-directory pruning all applied, newest first for
    /// `recentLimit`'s prefix.
    static func transcriptFiles(under root: URL, source: SearchTranscriptSource) -> [URL] {
        // The enumerator yields realpath-resolved URLs (`/var` →
        // `/private/var` on macOS), so depth is measured against the same
        // resolution or a symlinked root counts off by the link's span.
        var buffer = [CChar](repeating: 0, count: Int(PATH_MAX))
        let resolvedRoot = realpath(root.path, &buffer) != nil
            ? URL(fileURLWithPath: String(decoding: buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) },
                                          as: UTF8.self)) : root
        // Antigravity's transcript chain lives under `.system_generated`, so
        // its row keeps hidden entries; every other source keeps skipping.
        var options: FileManager.DirectoryEnumerationOptions = []
        if !source.includesHiddenFiles { options.insert(.skipsHiddenFiles) }
        guard let enumerator = FileManager.default.enumerator(
            at: resolvedRoot, includingPropertiesForKeys: [.isRegularFileKey, .isDirectoryKey],
            options: options)
        else { return [] }
        let rootComponents = resolvedRoot.pathComponents.count
        var files: [URL] = []
        for case let url as URL in enumerator {
            let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isDirectoryKey])
            // Depth 0 is a direct child of the root — Orca's indexing.
            if values?.isDirectory == true,
               let prune = source.directoryPredicate,
               !prune(url.lastPathComponent, url.pathComponents.count - rootComponents - 1) {
                enumerator.skipDescendants()
            }
            guard values?.isRegularFile == true,
                  source.extensions.contains(".\(url.pathExtension.lowercased())"),
                  source.filePredicate?(url) ?? true
            else { continue }
            files.append(url)
        }
        return newestFirst(files, sidecar: source.sidecarPath)
    }

    /// `files`/`transcriptFiles` ordering — newest first for `recentLimit`'s
    /// prefix; on equal mtimes the old path-ascending order survives,
    /// keeping dedup's keep-first attribution stable. A file `FileInfo`
    /// cannot stat sorts last. `sidecar` blends a sibling's mtime into the
    /// stamp — a Cline session whose `.messages.json` moved is newer than
    /// its untouched manifest reports.
    private static func newestFirst(
        _ urls: [URL], sidecar: (@Sendable (URL) -> URL?)? = nil
    ) -> [URL] {
        let stamped: [(url: URL, mtime: TimeInterval)] = urls.map {
            let base = FileInfo(path: $0.path)?.modified ?? 0
            let extra = sidecar?($0).flatMap { FileInfo(path: $0.path)?.modified } ?? 0
            return ($0, max(base, extra))
        }
        return stamped
            .sorted { $0.mtime != $1.mtime ? $0.mtime > $1.mtime : $0.url.path < $1.url.path }
            .map(\.url)
    }

    /// A second file a session-total file is read together with, stored in
    /// the cursor's state so its stamp changes also trigger a re-read
    /// (Droid's `<uuid>.jsonl` carries the timestamp and cwd its
    /// `.settings.json` sidecar does not).
    private struct WatchedStamp: Codable, Equatable {
        var inode: UInt64
        var size: UInt64
        var modified: Double
        var changed: Double

        init?(_ path: String) {
            guard let info = FileInfo(path: path) else { return nil }
            inode = info.inode
            size = info.size
            modified = info.modified
            changed = info.changed
        }
    }

    /// One session-total file (Droid's `.settings.json`, Cline's session
    /// JSON): re-read whole whenever its stamp (or its watched sibling's)
    /// changes; the record upserts under the session's stable key, replacing
    /// rather than duplicating. A file that now reports no usage deletes its
    /// old event.
    private func scanSessionFile(
        _ url: URL, accountHome: String, watched: URL? = nil,
        parse: (URL) throws -> SessionTotals?, report: inout ScanReport
    ) throws {
        let path = url.resolvingSymlinksInPath().path
        guard let info = FileInfo(path: path) else { return }
        // The watched file's absence is part of the stamp, so a transcript
        // appearing or disappearing re-reads too.
        let watchedStamp = watched.flatMap { WatchedStamp($0.path) }
        let state = watched.map { _ in (try? JSONEncoder().encode(watchedStamp)) ?? nil } ?? nil
        if let saved = try store.cursor(for: path),
           saved.inode == info.inode, saved.size == info.size, saved.modified == info.modified,
           (saved.changed ?? info.changed) == info.changed,
           saved.state == state {
            report.filesUnchanged += 1
            return
        }
        // A bad file (mid-rewrite, corrupt) skips this round without a
        // cursor, so it is retried on the next scan instead of aborting
        // every source after it.
        let totals: SessionTotals?
        do {
            totals = try parse(URL(fileURLWithPath: path))
        } catch {
            report.notes.append("\(url.lastPathComponent): \(error.localizedDescription)")
            return
        }
        try store.db.execute("BEGIN IMMEDIATE")
        do {
            switch totals {
            case .record(let record): try store.upsert(record)
            case .empty(let key): try store.delete(key: key)
            case nil: break
            }
            try store.save(FileCursor(
                path: path, inode: info.inode, size: info.size, modified: info.modified,
                changed: info.changed, offset: info.size,
                headHash: 0, headLength: 0, tailHash: 0, tailLength: 0,
                state: state, skippedLines: 0))
            try store.db.execute("COMMIT")
        } catch {
            store.rollback()
            throw error
        }
        report.filesRead += 1
    }

    /// Store records a poll produced (Cursor's usage feed) in one
    /// transaction. Every record carries a stable key, so an overlapping
    /// re-fetch upserts to the same rows instead of duplicating them.
    /// - Returns: how many rows were inserted or changed, so the caller
    ///   can tell a no-op ingest from one the Overview should reload for.
    @discardableResult
    func ingest(_ records: [LedgerRecord]) throws -> Int {
        var changed = 0
        try store.db.execute("BEGIN IMMEDIATE")
        do {
            for record in records {
                if try store.upsert(record) { changed += 1 }
            }
            try store.db.execute("COMMIT")
        } catch {
            store.rollback()
            throw error
        }
        return changed
    }

    // MARK: - Queries

    public func events(in range: DateInterval) -> [UsageEvent] {
        (try? store.events(in: range)) ?? []
    }

    public func totals(in range: DateInterval) -> UsageTotals {
        aggregate(in: range, by: []).values.reduce(into: UsageTotals()) { $0.add($1) }
    }

    /// Usage per calendar day, harness and model. Days with no usage are left out.
    ///
    /// One query in 15-minute buckets, each then placed in its local day:
    /// every timezone offset in use is a whole number of quarter hours, so a
    /// bucket never straddles midnight.
    ///
    /// Off the actor, like the other Overview queries (`readGroups`).
    @concurrent
    public nonisolated func daily(in range: DateInterval, calendar: Calendar) async -> [DailyUsage] {
        let pricing = self.pricing
        guard let groups = try? readGroups(in: range, by: [.harness, .model], bucketSeconds: 900,
                                           pricing: pricing)
        else { return [] }
        struct Key: Hashable { var day: Date, harness: String, model: String }
        var days: [Key: UsageTotals] = [:]
        for group in groups {
            let day = calendar.startOfDay(for: max(group.bucket ?? group.first, range.start))
            days[Key(day: day, harness: group.dimensions[.harness] ?? "", model: group.model), default: UsageTotals()]
                .add(Self.price(group, pricing: pricing))
        }
        return days
            .map { DailyUsage(day: $0.key.day, harness: $0.key.harness, model: $0.key.model, totals: $0.value) }
            .sorted { ($0.day, $0.harness, $0.model) < ($1.day, $1.harness, $1.model) }
    }

    /// One account home's token totals per calendar day for one harness —
    /// every token kind summed, the same `totalTokens` Overview reports.
    /// Days with no usage are left out.
    @concurrent
    public nonisolated func dailyTokens(harness: String, accountHome: String,
                                        in range: DateInterval, calendar: Calendar) async -> [Date: Int] {
        let key = AccountHistory.Key(harness: harness, homes: [accountHome])
        return await dailyTokens(for: [key], in: range, calendar: calendar)[key] ?? [:]
    }

    /// The same for several Accounts cards at once, from one grouped read:
    /// each key sums the rows it `matches`. Off the actor (`readGroups`):
    /// the Accounts history asks at launch, beside the first scan.
    @concurrent
    public nonisolated func dailyTokens(for keys: [AccountHistory.Key], in range: DateInterval,
                                        calendar: Calendar) async -> [AccountHistory.Key: [Date: Int]] {
        var result = Dictionary(keys.map { ($0, [Date: Int]()) }, uniquingKeysWith: { a, _ in a })
        // Keys of one harness (Codex's account usage, rebuilt after every
        // scan that wrote usage) read only that harness's rows: grouping
        // every harness's 200 days cost about 2 CPU-seconds a call (#1568).
        let harnesses = Set(keys.map(\.harness))
        guard !keys.isEmpty, let groups = try? readGroups(
            in: range, by: [.harness, .account], bucketSeconds: 900, pricing: pricing,
            splitsAtPriceChanges: false, harness: harnesses.count == 1 ? harnesses.first : nil)
        else { return result }
        for group in groups {
            let harness = group.dimensions[.harness] ?? ""
            let home = group.dimensions[.account] ?? ""
            let owners = result.keys.filter { $0.matches(harness: harness, home: home) }
            guard !owners.isEmpty else { continue }
            let day = calendar.startOfDay(for: max(group.bucket ?? group.first, range.start))
            for key in owners { result[key]![day, default: 0] += group.tokens.total }
        }
        return result
    }

    /// Usage per session, most recent first. `harness` restricts the read
    /// to one harness's dims when the caller only wants those.
    ///
    /// Off the actor (`readGroups`), like the Overview queries: the
    /// Sessions page asks for it at launch, and on the actor it waited
    /// behind the first scan's whole turn.
    @concurrent
    public nonisolated func bySession(in range: DateInterval, harness: String? = nil) async -> [SessionUsage] {
        sessionUsage(in: range, harness: harness)
    }

    /// `bySession`'s read, synchronous for callers already off the main
    /// thread (the calendar's trivial-session filter).
    nonisolated func sessionUsage(in range: DateInterval, harness: String? = nil) -> [SessionUsage] {
        let pricing = self.pricing
        guard let groups = try? readGroups(
            in: range, by: [.harness, .account, .session, .cwd], pricing: pricing, harness: harness)
        else { return [] }
        struct Key: Hashable { var harness, account, session: String }
        var sessions: [Key: SessionUsage] = [:]
        // Events per folder, in first-seen order: a side session's few
        // notes-folder events must not name it.
        var cwdVotes: [Key: [(cwd: String, events: Int)]] = [:]
        for group in groups {
            let key = Key(harness: group.dimensions[.harness] ?? "", account: group.dimensions[.account] ?? "",
                          session: group.dimensions[.session] ?? "")
            let totals = Self.price(group, pricing: pricing)
            let cwd = group.dimensions[.cwd].flatMap { $0.isEmpty ? nil : $0 }
            var session = sessions[key] ?? SessionUsage(
                sessionId: key.session, harness: key.harness, accountHome: key.account, cwd: nil,
                models: [], totals: UsageTotals(), firstTimestamp: group.first, lastTimestamp: group.last)
            session.totals.add(totals)
            if !session.models.contains(group.model) { session.models.append(group.model) }
            session.firstTimestamp = min(session.firstTimestamp, group.first)
            session.lastTimestamp = max(session.lastTimestamp, group.last)
            if let cwd {
                if let index = cwdVotes[key]?.firstIndex(where: { $0.cwd == cwd }) {
                    cwdVotes[key]![index].events += group.count
                } else {
                    cwdVotes[key, default: []].append((cwd, group.count))
                }
            }
            sessions[key] = session
        }
        for (key, votes) in cwdVotes { sessions[key]?.cwd = SessionFolders.pick(votes) }
        return sessions.values
            .map { var s = $0; s.models.sort(); return s }
            .sorted { ($0.lastTimestamp, $0.sessionId) > ($1.lastTimestamp, $1.sessionId) }
    }

    /// Usage per model and harness inside `range`, priced like the other
    /// queries, for the Pricing and Models pages' per-model rows.
    ///
    /// Off the actor and the main thread, on its own read-only
    /// connection: a WAL reader never waits behind a scan on the write
    /// connection, so these pages fill in while the logs are still read.
    @concurrent
    public nonisolated func byModelHarness(in range: DateInterval) async -> [ModelUsage] {
        let pricing = self.pricing
        let groups = try? usageConnection.withLock { connection throws -> [LedgerStore.PricedGroup] in
            if connection == nil {
                let path = activityURL.path
                    .addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
                    ?? activityURL.path
                connection = try SQLiteDatabase(readOnlyURI: "file:\(path)?mode=ro")
            }
            let db = connection!
            // One read transaction: both queries see the same commit, so a
            // scan landing between them can't add a model whose price
            // changes the first query never split on.
            try db.execute("BEGIN")
            // A transaction left open would fail every later BEGIN.
            defer { if (try? db.execute("COMMIT")) == nil { try? db.execute("ROLLBACK") } }
            let used = pricing.hasHistory ? try db.modelsAndTiers() : []
            return try db.pricedGroups(
                in: range, by: [.harness, .model], thresholds: pricing.thresholds,
                priceChanges: Self.priceChanges(in: range, used: used, pricing: pricing))
        }
        return (groups ?? []).map { group in
            ModelUsage(model: group.model, harness: group.dimensions[.harness] ?? "",
                       tier: group.tier,
                       totals: Self.price(group, pricing: pricing), harnessBilled: group.hasLoggedCost)
        }
    }

    /// Usage per account home.
    @concurrent
    public nonisolated func byAccount(in range: DateInterval) async -> [String: UsageTotals] {
        Dictionary(uniqueKeysWithValues: readAggregate(in: range, by: [.account]).map { ($0.key[0], $0.value) })
    }

    /// The harness each account home's usage came from: the one with the
    /// most tokens, should a home ever hold more than one.
    @concurrent
    public nonisolated func harnessByAccount(in range: DateInterval) async -> [String: String] {
        var best: [String: (harness: String, tokens: Int)] = [:]
        for (key, totals) in readAggregate(in: range, by: [.account, .harness]) {
            let (account, harness) = (key[0], key[1])
            guard !harness.isEmpty else { continue }
            if let current = best[account],
               (current.tokens, harness) >= (totals.totalTokens, current.harness) { continue }
            best[account] = (harness, totals.totalTokens)
        }
        return best.mapValues(\.harness)
    }

    /// Usage per harness.
    @concurrent
    public nonisolated func byHarness(in range: DateInterval) async -> [String: UsageTotals] {
        Dictionary(uniqueKeysWithValues: readAggregate(in: range, by: [.harness]).map { ($0.key[0], $0.value) })
    }

    /// `aggregate` on a read-only connection, off the actor.
    private nonisolated func readAggregate(in range: DateInterval,
                                           by columns: [LedgerStore.Column]) -> [[String]: UsageTotals] {
        let pricing = self.pricing
        guard let groups = try? readGroups(in: range, by: columns, pricing: pricing) else { return [:] }
        var result: [[String]: UsageTotals] = [:]
        for group in groups {
            let key = columns.map { group.dimensions[$0] ?? "" }
            result[key, default: UsageTotals()].add(Self.price(group, pricing: pricing))
        }
        return result
    }

    /// Grouped usage read on one of `overviewReaders`. A WAL reader never
    /// waits on the scan's write connection, and off the actor it never
    /// waits on the scan's turn either.
    /// `splitsAtPriceChanges: false` is for token-only reads, which no
    /// price touches.
    private nonisolated func readGroups(in range: DateInterval, by columns: [LedgerStore.Column],
                                        bucketSeconds: Int? = nil, pricing: PricingCatalog,
                                        splitsAtPriceChanges: Bool = true,
                                        harness: String? = nil) throws -> [LedgerStore.PricedGroup] {
        let reader = try overviewReaders.withLock { $0.popLast() } ?? openReadOnly()
        defer { overviewReaders.withLock { $0.append(reader) } }
        // One read transaction: both queries see the same commit, so a scan
        // landing between them can't add a model whose price changes the
        // grouping never split on.
        try reader.execute("BEGIN")
        // A transaction left open would fail every later BEGIN.
        defer { if (try? reader.execute("COMMIT")) == nil { try? reader.execute("ROLLBACK") } }
        let used = splitsAtPriceChanges && pricing.hasHistory ? try reader.modelsAndTiers() : []
        return try reader.pricedGroups(
            in: range, by: columns, thresholds: pricing.thresholds, bucketSeconds: bucketSeconds,
            harness: harness, priceChanges: Self.priceChanges(in: range, used: used, pricing: pricing))
    }

    private nonisolated func openReadOnly() throws -> SQLiteDatabase {
        let path = activityURL.path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? activityURL.path
        return try SQLiteDatabase(readOnlyURI: "file:\(path)?mode=ro")
    }

    private func aggregate(in range: DateInterval, by columns: [LedgerStore.Column]) -> [[String]: UsageTotals] {
        let pricing = self.pricing
        guard let groups = try? store.groups(in: range, by: columns, thresholds: pricing.thresholds,
                                             priceChanges: priceChanges(in: range, pricing: pricing)) else {
            return [:]
        }
        var result: [[String]: UsageTotals] = [:]
        for group in groups {
            let key = columns.map { group.dimensions[$0] ?? "" }
            result[key, default: UsageTotals()].add(Self.price(group, pricing: pricing))
        }
        return result
    }

    /// The moments inside `range` at which the price entry of any model
    /// the ledger holds changed. Splitting the query there keeps every
    /// group inside one price; other entries' changes split nothing.
    /// Every query reads `pricing` once and passes that copy through
    /// splitting and pricing alike: `setPricing` can swap the catalog
    /// mid-query, and one result must never mix two catalogs.
    private func priceChanges(in range: DateInterval, pricing: PricingCatalog) -> [Double] {
        guard pricing.hasHistory, let used = try? store.modelsAndTiers() else { return [] }
        return Self.priceChanges(in: range, used: used, pricing: pricing)
    }

    /// `priceChanges(in:)` for the models and tiers `used`, priced by
    /// `pricing`: shared with the off-actor read.
    private static func priceChanges(in range: DateInterval, used: [(model: String, tier: String)],
                                     pricing: PricingCatalog) -> [Double] {
        guard pricing.hasHistory else { return [] }
        var entries = Set<String>()
        for (model, tier) in used {
            if case .priced(_, let entry, _, _) = pricing.resolve(model: model, tier: tier) {
                entries.insert(entry)
            }
        }
        let start = range.start.timeIntervalSince1970, end = range.end.timeIntervalSince1970
        return pricing.priceChanges(for: entries).filter { $0 > start && $0 < end }
    }

    private static func price(_ group: LedgerStore.PricedGroup, pricing: PricingCatalog) -> UsageTotals {
        var totals = UsageTotals()
        let t = group.tokens
        totals.inputTokens = t.input
        totals.outputTokens = t.output
        totals.cacheReadTokens = t.cacheRead
        totals.cacheWriteTokens = t.cacheWrite
        totals.reasoningTokens = t.reasoning
        totals.eventCount = group.count
        if group.hasLoggedCost {
            totals.costUSD = group.loggedCost
            return totals
        }
        // One resolution for the ledger and the Pricing page: the literal
        // name wins (`xai/grok-code-fast` is a real entry), then the
        // `-fast` tier suffix, then the free-model check.
        switch pricing.resolve(model: group.model, tier: group.tier ?? "") {
        case .priced(let current, let entry, _, let tier):
            // A group never spans a price change, so its first event's
            // time picks the one rate every event in it paid.
            let rates = pricing.historicalRates(entry: entry, at: group.first.timeIntervalSince1970) ?? current
            let crosses = rates.longContext.map { long in
                (pricing.thresholds.firstIndex(of: long.threshold) ?? Int.max) < group.thresholdsCrossed
            } ?? false
            totals.costUSD = rates.cost(
                input: t.input, output: t.output, cacheRead: t.cacheRead, cacheWrite: t.cacheWrite,
                cacheWrite1h: t.cacheWrite1h, reasoning: t.reasoning, tier: tier, crossesLongContext: crosses)
        case .free:
            // A free model is priced at zero, not reported as unpriced.
            totals.costUSD = 0
        case nil:
            if t.total > 0 {
                totals.unpricedTokens = t.total
                totals.unpricedModels = [group.model]
            }
        }
        return totals
    }
}

/// `stat` of one file.
/// Kimi's shared `session_index.jsonl`, parsed once per scan per index
/// and stamp: every session under one index reads the same map, and the
/// stamp each session records for freshness is the stamp the map was
/// parsed at, so an index rewritten mid-scan is parsed again rather than
/// served stale under its new stamp.
struct KimiWorkDirCache {
    private var entries: [String: (stamp: [Double], map: [String: String])] = [:]
    /// How many times an index was actually parsed, for tests.
    private(set) var parses = 0

    mutating func map(forStateFile path: String, indexInfo: FileInfo?) -> [String: String] {
        let key = Ledger.kimiSessionIndexURL(forStateFile: path).path
        let stamp = indexInfo.map { [Double($0.size), $0.modified, $0.changed] } ?? []
        if let entry = entries[key], entry.stamp == stamp { return entry.map }
        let map = Ledger.kimiWorkDirs(forStateFile: path)
        parses += 1
        entries[key] = (stamp, map)
        return map
    }
}

struct FileInfo {
    var inode: UInt64
    var size: UInt64
    var modified: Double
    /// The inode's change time. Any write — including one that then
    /// restores `modified` — moves it, and it cannot be stamped back,
    /// which makes it the tell for metadata-preserved rewrites.
    var changed: Double

    init?(path: String) {
        var info = stat()
        guard stat(path, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG else { return nil }
        inode = UInt64(info.st_ino)
        size = UInt64(info.st_size)
        modified = Double(info.st_mtimespec.tv_sec) + Double(info.st_mtimespec.tv_nsec) / 1e9
        changed = Double(info.st_ctimespec.tv_sec) + Double(info.st_ctimespec.tv_nsec) / 1e9
    }
}

/// Parses the lines of one log file. A parser that needs context from earlier
/// lines (Codex's running model and totals) saves it with the file's cursor,
/// so an appended file resumes with the right state.
protocol FileLogParser {
    /// The decoder the search index feeds the same lines to.
    static var searchHarness: SearchDecoder.Harness { get }
    init(file: URL, accountHome: String, savedState: Data?)
    /// The cheap byte gate: can this line carry ledger records? The scan
    /// unions it with the index decoder's gate and parses the line once.
    func wants(line: Data) -> Bool
    /// Records from the already-parsed line object — the shared-parse path.
    mutating func records(fromObject object: [String: Any]) -> [LedgerRecord]
    func savedState() -> Data?
}

extension FileLogParser {
    /// The one-consumer entry, kept for callers that parse their own lines.
    mutating func records(fromLine line: Data) -> [LedgerRecord] {
        guard wants(line: line),
              let object = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
        else { return [] }
        return records(fromObject: object)
    }
}

struct ClaudeFileParser: FileLogParser {
    static let searchHarness: SearchDecoder.Harness = .claude
    let accountHome: String
    init(file: URL, accountHome: String, savedState: Data?) { self.accountHome = accountHome }
    func wants(line: Data) -> Bool { line.range(of: ClaudeLogParser.usageMarker) != nil }
    func records(fromObject object: [String: Any]) -> [LedgerRecord] {
        ClaudeLogParser.records(fromObject: object, accountHome: accountHome)
    }
    func savedState() -> Data? { nil }
}

