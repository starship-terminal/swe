import Foundation
import SQLite3
import Testing
@testable import SWECore

/// Builds a database from a SQL fixture, as the harness would have written it.
func makeDatabase(_ fixture: String, at url: URL) throws {
    let sql = try String(contentsOf: try ledgerFixtureURL(fixture), encoding: .utf8)
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    let db = try SQLiteDatabase(url: url)
    try db.execute(sql)
}

func execute(_ sql: String, on url: URL) throws {
    try SQLiteDatabase(url: url).execute(sql)
}

/// Ages a database and its WAL a minute, as one nobody has written to
/// lately: only such a stamp is trusted to skip a scan (#1930).
func settle(_ url: URL) throws {
    for path in [url.path, url.path + "-wal"] where FileManager.default.fileExists(atPath: path) {
        let modified = try #require(
            FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as? Date)
        try FileManager.default.setAttributes(
            [.modificationDate: modified.addingTimeInterval(-60)], ofItemAtPath: path)
    }
}

/// Puts back a file's exact modification time, nanoseconds and all, as a
/// filesystem whose clock had not ticked since `stamp` would have left it.
func restoreModified(_ url: URL, to stamp: stat) throws {
    var times = [stamp.st_atimespec, stamp.st_mtimespec]
    #expect(utimensat(AT_FDCWD, url.path, &times, 0) == 0)
}

func fileStat(_ url: URL) throws -> stat {
    var info = stat()
    try #require(stat(url.path, &info) == 0)
    return info
}

@Suite struct LedgerOpenCodeTests {
    @Test func assistantMessagesBecomeEventsWithTheirRecordedCost() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        try await ledger.scan(roots: LedgerRoots(openCodeDatabases: [db]))

        let events = await ledger.events(in: allTime)
        #expect(events.count == 2)
        let first = try #require(events.first)
        #expect(first.harness == "opencode")
        #expect(first.sessionId == "ses_1")
        #expect(first.cwd == "/tmp/oc")
        #expect(first.accountHome == db.deletingLastPathComponent().path)
        #expect(first.model == "anthropic/claude-sonnet-4-5")
        #expect(first.timestamp == Date(timeIntervalSince1970: 1_791_100_001))
        #expect(first.inputTokens == 100)
        #expect(first.outputTokens == 300)
        #expect(first.reasoningTokens == 200)
        #expect(first.cacheReadTokens == 1_000)
        #expect(first.costUSD == 0.01)

        let old = events[1]
        #expect(old.outputTokens == 33)
        #expect(old.reasoningTokens == 51)
        #expect(old.totalTokens == 8_695)
        #expect(old.costUSD == nil)
    }

    @Test func updatedAndNewMessagesArePickedUpWithoutDoubleCounting() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        try await ledger.scan(roots: LedgerRoots(openCodeDatabases: [db]))
        #expect(await ledger.totals(in: allTime).totalTokens == 1_600 + 8_695)

        try execute("""
            UPDATE message SET time_updated = 1791100009000, data = json_set(data, '$.tokens.input', 40, '$.tokens.output', 2)
            WHERE id = 'msg_a3';
            INSERT INTO message VALUES ('msg_a4', 'ses_1', 1791100010000, 1791100010000,
              '{"role":"assistant","modelID":"gpt-5.4","time":{"created":1791100010000},"tokens":{"input":7,"output":3,"reasoning":0,"cache":{"read":0,"write":0}}}');
            """, on: db)
        try await ledger.scan(roots: LedgerRoots(openCodeDatabases: [db]))
        #expect(await ledger.totals(in: allTime).totalTokens == 1_600 + 8_695 + 42 + 10)
        try await ledger.scan(roots: LedgerRoots(openCodeDatabases: [db]))
        #expect(await ledger.events(in: allTime).count == 4)
    }

    @Test func aCorruptHugeCountIsCappedInsteadOfTrapping() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        try execute("""
            INSERT INTO message VALUES ('msg_huge', 'ses_1', 1791100010000, 1791100010000,
              '{"role":"assistant","modelID":"gpt-5.4","time":{"created":1791100010000},"tokens":{"input":9223372036854775000,"output":9223372036854775000,"reasoning":1,"cache":{"read":0,"write":0}}}');
            """, on: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        try await ledger.scan(roots: LedgerRoots(openCodeDatabases: [db]))
        let huge = try #require(await ledger.events(in: allTime).first { $0.inputTokens > 1_000_000 })
        #expect(huge.inputTokens == JSONValue.maxTokens)
        #expect(huge.outputTokens == JSONValue.maxTokens)
    }

    /// A build that read only the legacy tables saved a fingerprint for
    /// the database as it stands; the 2.x reader must not take it as
    /// "nothing moved" and skip the rows that build never read.
    @Test func aFingerprintFromTheLegacyReaderDoesNotSkipTheScan() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        let store = try LedgerStore(url: temp.path("cache/ledger.sqlite"))
        let fingerprint = try #require(DatabaseSources.Fingerprint(db))
        try store.setMeta("opencode:\(db.path):fingerprint", fingerprint.text)

        #expect(try DatabaseSources.scanOpenCode(db, store: store))
        #expect(try store.events(in: allTime).count == 2)
    }

    /// OpenCode 2.x writes new sessions only to `session_v2` and their
    /// replies only to `session_message`; its migration copied old replies
    /// there under their `message` ids, which must not count twice.
    @Test func openCode2RepliesCountOnceAlongsideLegacyOnes() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        try execute("""
            CREATE TABLE `session_v2` (`id` text PRIMARY KEY, `directory` text NOT NULL, `time_updated` integer NOT NULL);
            CREATE TABLE `session_message` (`id` text PRIMARY KEY, `session_id` text NOT NULL, `type` text NOT NULL,
              `seq` integer NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL);
            INSERT INTO session_v2 VALUES ('ses_1', '/tmp/oc', 1791100000000), ('ses_v2', '/tmp/v2', 1791100020000);
            INSERT INTO session_message VALUES ('msg_a1', 'ses_1', 'assistant', 1, 1791100001000, 1791100002000,
              '{"model":{"id":"claude-sonnet-4-5"},"cost":0.01,"tokens":{"input":100,"output":300,"reasoning":200,"cache":{"read":1000,"write":0}}}');
            INSERT INTO session_message VALUES ('msg_v1', 'ses_v2', 'user', 4, 1791100020000, 1791100020000, '{"text":"hello"}');
            INSERT INTO session_message VALUES ('msg_v2', 'ses_v2', 'assistant', 7, 1791100021000, 1791100022000,
              '{"model":{"id":"MiniMax-M3","providerID":"minimax"},"cost":0.002,"finish":"stop","tokens":{"input":8323,"output":10,"reasoning":4,"cache":{"read":479,"write":0}}}');
            """, on: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        try await ledger.scan(roots: LedgerRoots(openCodeDatabases: [db]))

        let events = await ledger.events(in: allTime)
        #expect(events.count == 3)
        let v2 = try #require(events.first { $0.sessionId == "ses_v2" })
        #expect(v2.cwd == "/tmp/v2")
        #expect(v2.model == "minimax/MiniMax-M3")
        #expect(v2.outputTokens == 10)
        #expect(v2.reasoningTokens == 4)
        #expect(v2.totalTokens == 8_323 + 10 + 4 + 479)
        #expect(v2.costUSD == 0.002)
        #expect(await ledger.totals(in: allTime).totalTokens == 1_600 + 8_695 + 8_816)
    }
}

@Suite struct LedgerDevinTests {
    @Test func eachRequestCountsOnceHoweverManyNodesCopyIt() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("devin/cli/sessions.db")
        try makeDatabase("devin.sql", at: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: try .bundled())
        try await ledger.scan(roots: LedgerRoots(devinDatabases: [db]))

        let events = await ledger.events(in: allTime)
        #expect(events.map(\.model) == ["swe-2-medium", "claude-fable-5-1-high"])
        let r1 = events[0]
        #expect(r1.harness == "devin")
        #expect(r1.sessionId == "dev-1")
        #expect(r1.cwd == "/tmp/dv")
        #expect(r1.inputTokens == 100)
        #expect(r1.cacheReadTokens == 1_000)
        #expect(r1.cacheWriteTokens == 10)
        #expect(r1.timestamp == utc("2026-10-04T09:00:10Z").addingTimeInterval(0.123456))

        // Devin's own models have no public price; Claude's has one under an effort suffix.
        let totals = await ledger.totals(in: allTime)
        #expect(totals.unpricedModels == ["swe-2-medium"])
        #expect(totals.unpricedTokens == 1_160)
        #expect(totals.costUSD > 0)
    }

    @Test func aCorruptHugeCountIsCappedInsteadOfTrapping() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("devin/cli/sessions.db")
        try makeDatabase("devin.sql", at: db)
        try execute("""
            UPDATE message_nodes SET chat_message = json_set(chat_message, '$.metadata.metrics',
              json('{"input_tokens":9223372036854775000,"output_tokens":9223372036854775000}')) WHERE node_id = 5;
            """, on: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        try await ledger.scan(roots: LedgerRoots(devinDatabases: [db]))
        let huge = try #require(await ledger.events(in: allTime).first { $0.inputTokens > 1_000_000 })
        #expect(huge.inputTokens == JSONValue.maxTokens)
        #expect(huge.outputTokens == JSONValue.maxTokens)
    }

    @Test func aRequestFinishedInPlaceIsCountedOnTheNextScan() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("devin/cli/sessions.db")
        try makeDatabase("devin.sql", at: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        try await ledger.scan(roots: LedgerRoots(devinDatabases: [db]))
        #expect(await ledger.events(in: allTime).count == 2)

        try execute("""
            UPDATE message_nodes SET chat_message = json_set(chat_message, '$.metadata.metrics',
              json('{"input_tokens":5,"output_tokens":5}')) WHERE node_id = 5;
            """, on: db)
        try await ledger.scan(roots: LedgerRoots(devinDatabases: [db]))
        #expect(await ledger.events(in: allTime).count == 3)
        #expect(await ledger.totals(in: allTime).totalTokens == 1_160 + 450 + 10)
    }

    /// A filesystem that stamps writes from a coarse clock (ext4 ticks every
    /// few milliseconds, HFS+ every second) gives a write landing in the
    /// same tick as a scan the inode, size and mtime that scan saw (#1930).
    @Test func aWriteThatKeepsTheScansStampIsStillRead() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("devin/cli/sessions.db")
        try makeDatabase("devin.sql", at: db)
        let store = try LedgerStore(url: temp.path("cache/ledger.sqlite"))
        let before = try fileStat(db)
        let stamp = try #require(DatabaseSources.Fingerprint(db))
        #expect(try DatabaseSources.scanDevin(db, store: store))
        #expect(try store.events(in: allTime).count == 2)

        try execute("""
            UPDATE message_nodes SET chat_message = json_set(chat_message, '$.metadata.metrics',
              json('{"input_tokens":5,"output_tokens":5}')) WHERE node_id = 5;
            """, on: db)
        try restoreModified(db, to: before)
        #expect(try #require(DatabaseSources.Fingerprint(db)).text == stamp.text)

        #expect(try DatabaseSources.scanDevin(db, store: store))
        #expect(try store.events(in: allTime).count == 3)
        // Read again while unsettled, nothing is counted twice.
        #expect(try DatabaseSources.scanDevin(db, store: store))
        #expect(try store.events(in: allTime).reduce(0) { $0 + $1.totalTokens } == 1_160 + 450 + 10)
    }

    @Test func aSettledDatabaseIsSkippedUntilItMoves() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("devin/cli/sessions.db")
        try makeDatabase("devin.sql", at: db)
        try settle(db)
        let store = try LedgerStore(url: temp.path("cache/ledger.sqlite"))
        #expect(try DatabaseSources.scanDevin(db, store: store))
        #expect(try DatabaseSources.scanDevin(db, store: store) == false)
        try execute("""
            UPDATE message_nodes SET chat_message = json_set(chat_message, '$.metadata.metrics',
              json('{"input_tokens":5,"output_tokens":5}')) WHERE node_id = 5;
            """, on: db)
        #expect(try DatabaseSources.scanDevin(db, store: store))
        #expect(try store.events(in: allTime).count == 3)
    }

    @Test func aReplacedDatabaseIsReadFromTheStart() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("devin/cli/sessions.db")
        try makeDatabase("devin.sql", at: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        try await ledger.scan(roots: LedgerRoots(devinDatabases: [db]))
        #expect(await ledger.events(in: allTime).count == 2)

        // Devin recreates the file: row ids start over below the old watermark.
        for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: db.path + suffix) }
        try execute("""
            CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              node_id INTEGER NOT NULL, parent_node_id INTEGER, chat_message TEXT NOT NULL, created_at INTEGER NOT NULL);
            INSERT INTO message_nodes (session_id, node_id, chat_message, created_at) VALUES ('dev-9', 1,
              '{"role":"assistant","metadata":{"request_id":"r9","generation_model":"swe-2-medium","metrics":{"input_tokens":7,"output_tokens":3}}}', 1791200000);
            """, on: db)
        try await ledger.scan(roots: LedgerRoots(devinDatabases: [db]))
        #expect(await ledger.events(in: allTime).map(\.sessionId).contains("dev-9"))
    }
}

/// The ledger names an OpenCode model `provider/model`. Both the legacy
/// `message` rows (`providerID` beside `modelID`) and 2.x `session_message`
/// ones (`model.providerID` beside `model.id`) carry the provider.
@Suite struct LedgerOpenCodeModelNameTests {
    private let v2Tables = """
        CREATE TABLE `session_v2` (`id` text PRIMARY KEY, `directory` text NOT NULL, `time_updated` integer NOT NULL);
        CREATE TABLE `session_message` (`id` text PRIMARY KEY, `session_id` text NOT NULL, `type` text NOT NULL,
          `seq` integer NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL);
        INSERT INTO session_v2 VALUES ('ses_v2', '/tmp/v2', 1791100020000);

        """

    private func v2Reply(_ id: String, _ model: String) -> String {
        """
        INSERT INTO session_message VALUES ('\(id)', 'ses_v2', 'assistant', 7, 1791100021000, 1791100022000,
          '{"model":\(model),"cost":0.002,"tokens":{"input":10,"output":1,"reasoning":0,"cache":{"read":0,"write":0}}}');

        """
    }

    @Test func modelsCarryTheirProviderUnlessThereIsNone() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        try execute(v2Tables + v2Reply("v_plain", #"{"id":"MiniMax-M3","providerID":"minimax"}"#)
            + v2Reply("v_bare", #"{"id":"bare-model"}"#)
            + v2Reply("v_empty", #"{"id":"empty-provider","providerID":""}"#)
            + v2Reply("v_slash", #"{"id":"nvidia/nemotron:free","providerID":"openrouter"}"#)
            + v2Reply("v_none", "null")
            + """
            INSERT INTO message VALUES ('l_bare', 'ses_1', 1791100030000, 1791100030000,
              '{"role":"assistant","modelID":"no-provider","tokens":{"input":5,"output":0,"reasoning":0,"cache":{"read":0,"write":0}}}');
            """, on: db)
        let store = try LedgerStore(url: temp.path("cache/ledger.sqlite"))
        #expect(try DatabaseSources.scanOpenCode(db, store: store))

        let models = try Set(store.events(in: allTime).map(\.model))
        #expect(models == [
            "anthropic/claude-sonnet-4-5", "openai/gpt-5.4", "minimax/MiniMax-M3", "bare-model",
            "empty-provider", "openrouter/nvidia/nemotron:free", "unknown", "no-provider",
        ])
        #expect(DatabaseSources.openCodeModel(provider: "openrouter", id: "openrouter/free")
            == "openrouter/openrouter/free")
        #expect(DatabaseSources.openCodeModel(provider: "openai", id: nil) == "unknown")
    }

    /// A ledger that holds bare model names (what the `v2` reader wrote)
    /// reads the database again under the new key. Each event is keyed by
    /// its message id, so it moves to its `provider/model` name in place:
    /// no event is added, and no token or cent changes.
    @Test func aLedgerFromTheBareNameReaderIsRenamedInPlaceWithoutDoubleCounting() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        try execute(v2Tables + v2Reply("v_one", #"{"id":"MiniMax-M3","providerID":"minimax"}"#), on: db)
        let ledgerURL = temp.path("cache/ledger.sqlite")
        func figures(_ store: LedgerStore) throws -> (events: Int, tokens: Int, cost: Double) {
            let events = try store.events(in: allTime)
            return (events.count, events.reduce(0) { $0 + $1.totalTokens },
                    events.reduce(0) { $0 + ($1.costUSD ?? 0) })
        }
        try settle(db)
        let before: (events: Int, tokens: Int, cost: Double)
        do {
            let store = try LedgerStore(url: ledgerURL)
            #expect(try DatabaseSources.scanOpenCode(db, store: store))
            before = try figures(store)
        }
        #expect(before.events == 3)
        // What the previous build left: bare names in the dimension rows
        // and its scan state under the old key.
        let raw = try SQLiteDatabase(url: ledgerURL)
        try raw.execute("""
            UPDATE dims SET model = substr(model, instr(model, '/') + 1) WHERE harness = 'opencode';
            UPDATE meta SET key = replace(key, 'opencode-v3:', 'opencode-v2:');
            """)
        let store = try LedgerStore(url: ledgerURL)
        #expect(Set(try store.modelsAndTiers().map(\.model)) == ["claude-sonnet-4-5", "gpt-5.4", "MiniMax-M3"])
        let current = try #require(DatabaseSources.Fingerprint(db))
        #expect(try store.meta("opencode-v2:\(db.path):fingerprint") == current.text)

        #expect(try DatabaseSources.scanOpenCode(db, store: store))

        let after = try figures(store)
        #expect(after.events == before.events)
        #expect(after.tokens == before.tokens)
        #expect(after.cost == before.cost)
        let renamed: Set = ["anthropic/claude-sonnet-4-5", "openai/gpt-5.4", "minimax/MiniMax-M3"]
        #expect(Set(try store.events(in: allTime).map(\.model)) == renamed)
        // The bare names' dimension rows went with their last event.
        #expect(Set(try store.modelsAndTiers().map(\.model)) == renamed)
        #expect(try store.meta("opencode-v3:\(db.path):fingerprint") == current.text)
        // Settled: the next scan finds nothing moved.
        #expect(try DatabaseSources.scanOpenCode(db, store: store) == false)
    }

    /// The ledger outlives its logs: a row whose source message is gone has
    /// no provider to read, so it keeps the name it was logged under.
    @Test func aRowWhoseSourceMessageIsGoneKeepsItsBareName() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        let ledgerURL = temp.path("cache/ledger.sqlite")
        do {
            let store = try LedgerStore(url: ledgerURL)
            #expect(try DatabaseSources.scanOpenCode(db, store: store))
        }
        let raw = try SQLiteDatabase(url: ledgerURL)
        try raw.execute("""
            UPDATE dims SET model = substr(model, instr(model, '/') + 1) WHERE harness = 'opencode';
            UPDATE meta SET key = replace(key, 'opencode-v3:', 'opencode-v2:');
            """)
        try execute("DELETE FROM message WHERE id = 'msg_a2'", on: db)
        let store = try LedgerStore(url: ledgerURL)

        #expect(try DatabaseSources.scanOpenCode(db, store: store))
        #expect(try store.events(in: allTime).count == 2)
        #expect(Set(try store.events(in: allTime).map(\.model)) == ["anthropic/claude-sonnet-4-5", "gpt-5.4"])
        #expect(Set(try store.modelsAndTiers().map(\.model)) == ["anthropic/claude-sonnet-4-5", "gpt-5.4"])
    }

    /// A re-read that ties on totals still renames the row, one that
    /// records fewer tokens never lowers them, one that records more
    /// replaces them, and a read that does not ask to relabel changes
    /// nothing.
    @Test func aRelabellingReadMovesTheModelWhateverTheTotals() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let store = try LedgerStore(url: temp.path("cache/ledger.sqlite"))
        func event(_ model: String, tokens: Int) -> UsageEvent {
            UsageEvent(harness: "opencode", accountHome: "/h", sessionId: "s", model: model,
                       timestamp: utc("2026-10-01T00:00:00Z"), inputTokens: tokens)
        }
        try store.db.execute("BEGIN IMMEDIATE")
        for (key, again) in [("tie", 5), ("smaller", 3), ("larger", 9)] {
            try store.upsert(LedgerRecord(key: key, event: event("m", tokens: 5)))
            try store.upsert(LedgerRecord(key: key, relabelsModel: true, event: event("p/m", tokens: again)))
        }
        try store.upsert(LedgerRecord(key: "plain", event: event("q", tokens: 5)))
        try store.upsert(LedgerRecord(key: "plain", event: event("p/q", tokens: 5)))
        try store.dropVacatedDims()
        try store.db.execute("COMMIT")

        let events = try store.events(in: allTime)
        #expect(events.count == 4)
        #expect(events.map(\.model).sorted() == ["p/m", "p/m", "p/m", "q"])
        #expect(events.map(\.inputTokens).sorted() == [5, 5, 5, 9])
        // The three relabelled rows left their dimension row ("m") behind
        // empty, including the one whose figures were replaced too.
        #expect(Set(try store.modelsAndTiers().map(\.model)) == ["p/m", "q"])
    }
}

@Suite struct LedgerOpenCodeReplacementTests {
    @Test func aReplacedDatabaseIsReadFromTheStart() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("opencode/opencode.db")
        try makeDatabase("opencode.sql", at: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        try await ledger.scan(roots: LedgerRoots(openCodeDatabases: [db]))
        let before = await ledger.events(in: allTime).count

        // A restored copy whose rows are older than the old watermark.
        for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: db.path + suffix) }
        try makeDatabase("opencode.sql", at: db)
        try execute("""
            INSERT INTO message VALUES ('msg_old', 'ses_1', 1791000000000, 1791000000000,
              '{"role":"assistant","modelID":"gpt-5.4","time":{"created":1791000000000},"tokens":{"input":7,"output":3,"reasoning":0,"cache":{"read":0,"write":0}}}');
            """, on: db)
        try await ledger.scan(roots: LedgerRoots(openCodeDatabases: [db]))
        #expect(await ledger.events(in: allTime).count == before + 1)
    }
}

@Suite struct LedgerRollbackTests {
    @Test func aRolledBackScanLeavesNoStaleDimensionIds() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let store = try LedgerStore(url: temp.path("cache/ledger.sqlite"))
        let event = UsageEvent(harness: "claude", accountHome: "/h", sessionId: "s", model: "m",
                               timestamp: utc("2026-10-01T00:00:00Z"), inputTokens: 5)
        try store.db.execute("BEGIN IMMEDIATE")
        try store.upsert(LedgerRecord(key: "a", event: event))
        store.rollback()

        try store.db.execute("BEGIN IMMEDIATE")
        try store.upsert(LedgerRecord(key: "a", event: event))
        try store.db.execute("COMMIT")
        #expect(try store.events(in: allTime).map(\.sessionId) == ["s"])
    }
}

@Suite struct LedgerSourceSkipTests {
    /// A source database in a transient state (here a mid-rebuild file whose
    /// schema does not match yet) is skipped with a note instead of failing
    /// the whole scan: the healthy source still reads.
    @Test func aBrokenSourceIsSkippedWithANoteAndTheRestStillScans() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let good = temp.path("devin/cli/sessions.db")
        try makeDatabase("devin.sql", at: good)
        // Exists, opens, but has none of the expected tables: like OpenCode's
        // database caught between delete and recreate.
        let broken = temp.path("opencode/opencode.db")
        try FileManager.default.createDirectory(
            at: broken.deletingLastPathComponent(), withIntermediateDirectories: true)
        try execute("CREATE TABLE unrelated (id INTEGER)", on: broken)

        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        let report = try await ledger.scan(roots: LedgerRoots(
            openCodeDatabases: [broken], devinDatabases: [good]))

        #expect(report.databasesRead == 1)
        let note = try #require(report.notes.first)
        #expect(note.contains("OpenCode"))
        #expect(note.contains(broken.path))
        #expect(note.contains("sqlite"))
        #expect(await ledger.events(in: allTime).count == 2)
    }

    /// Only reads of the source are forgiven. A write to SWE's own cache
    /// fails the scan, so the Overview can show the error.
    @Test func aCacheWriteFailureThrowsInsteadOfBecomingANote() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = temp.path("devin/cli/sessions.db")
        try makeDatabase("devin.sql", at: db)
        let ledger = try Ledger(directory: temp.path("cache"), pricing: .empty)
        // The lock below is held for good, so waiting it out only costs
        // the test two seconds before the same failure.
        await ledger.setBusyTimeout(milliseconds: 0)

        // A second connection holding an exclusive lock makes every cache
        // operation fail, starting at BEGIN IMMEDIATE.
        let lock = try SQLiteDatabase(url: temp.path("cache/ledger.sqlite"))
        try lock.execute("BEGIN EXCLUSIVE")
        do {
            _ = try await ledger.scan(roots: LedgerRoots(devinDatabases: [db]))
            Issue.record("expected the scan to throw")
        } catch let error as SQLiteDatabase.Failure {
            #expect(error.message.contains("locked"))
        }
    }

    /// Every connection waits out a held lock for two seconds instead of
    /// failing at once: without `sqlite3_busy_timeout` the read-write one
    /// returned SQLITE_BUSY immediately. SQLite reports the setting itself,
    /// so the test needn't sit out a real two-second wait.
    @Test func everyConnectionWaitsOutAHeldLock() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let url = temp.path("held.sqlite")
        try execute("CREATE TABLE seed (id INTEGER)", on: url)
        let connections = [
            try SQLiteDatabase(),
            try SQLiteDatabase(url: url),
            try SQLiteDatabase(readOnly: url),
            try SQLiteDatabase(readOnlyURI: "file:\(url.path)?mode=ro"),
        ]
        for db in connections {
            #expect(try db.scalar("PRAGMA busy_timeout") == 2_000)
        }
    }

    /// The failure carries what SQLite actually said, so the Overview banner
    /// can show more than "error 1".
    @Test func aFailureDescribesTheSQLiteCodeAndMessage() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let db = try SQLiteDatabase(url: temp.path("x.sqlite"))
        do {
            _ = try db.prepare("SELECT nope FROM missing")
            Issue.record("expected a Failure")
        } catch let error as SQLiteDatabase.Failure {
            #expect(error.code == 1)
            #expect(error.errorDescription == error.description)
            #expect(error.localizedDescription.contains("sqlite 1"))
            #expect(error.localizedDescription.contains("missing"))
        }
    }
}

@Suite struct LedgerSchemaTests {
    /// A schema mismatch rebuilds the tables, but the old file is archived
    /// beside the database first so history whose logs are gone is not lost.
    @Test func aSchemaMismatchArchivesTheOldDatabaseBeforeRebuilding() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let url = temp.path("cache/ledger.sqlite")
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try execute("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema', '0');
            CREATE TABLE events (key INTEGER PRIMARY KEY, input INTEGER NOT NULL);
            INSERT INTO events VALUES (1, 42);
            """, on: url)

        do {
            // Scoped: the second open below drops the tables, which this
            // connection's prepared statements would lock.
            let store = try LedgerStore(url: url)
            #expect(try store.events(in: allTime).isEmpty)
        }

        let archive = try SQLiteDatabase(readOnly: temp.path("cache/ledger.schema-0.sqlite"))
        let schema = try archive.prepare("SELECT value FROM meta WHERE key = 'schema'")
        #expect(try schema.step() && schema.text(0) == "0")
        let row = try archive.prepare("SELECT input FROM events WHERE key = 1")
        #expect(try row.step() && row.int(0) == 42)

        // A second mismatch takes a numbered name rather than overwriting.
        do {
            let fixture = try SQLiteDatabase(url: url)
            try fixture.execute("UPDATE meta SET value = '0' WHERE key = 'schema'")
        }
        _ = try LedgerStore(url: url)
        #expect(FileManager.default.fileExists(
            atPath: temp.path("cache/ledger.schema-0.1.sqlite").path))
    }

    /// A brand new database has nothing worth keeping, so no archive file
    /// appears beside it.
    @Test func aFreshDatabaseLeavesNoArchive() throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        _ = try LedgerStore(url: temp.path("cache/ledger.sqlite"))
        let siblings = try FileManager.default.contentsOfDirectory(
            atPath: temp.path("cache").path)
        #expect(siblings.filter { $0.contains("schema-") }.isEmpty)
    }
}
