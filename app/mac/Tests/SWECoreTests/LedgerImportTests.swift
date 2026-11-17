import Foundation
import Testing
@testable import SWECore

/// Ledger sources beyond the original four: Pi's per-message JSONL,
/// Droid's and Cline's rewritten session-total files, and Grok's
/// per-turn `usage.json`.
@Suite struct LedgerImportTests {
    // MARK: - Fixtures

    private static func piMessage(id: String, ms: Int, input: Int, output: Int,
                                  reasoning: Int, cacheRead: Int, cost: Double) -> String {
        """
        {"type":"message","id":"\(id)","message":{"role":"assistant","model":"gpt-5.3-codex",\
        "provider":"github-copilot","timestamp":\(ms),"usage":{"input":\(input),"output":\(output),\
        "cacheRead":\(cacheRead),"cacheWrite":0,"reasoning":\(reasoning),\
        "totalTokens":\(input + output + cacheRead),\
        "cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":\(cost)}}}}
        """ + "\n"
    }

    private func piHome(_ temp: TempDir, lines: String) throws -> URL {
        let home = temp.path("pi")
        let file = home.appendingPathComponent(
            "agent/sessions/-Users-x-proj/2026-10-03T10-00-00-000Z_aaaa.jsonl")
        try FileManager.default.createDirectory(
            at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(lines.utf8).write(to: file)
        return home
    }

    private func droidHome(_ temp: TempDir, usage: String, lastStamp: String = "2026-10-03T12:00:00Z") throws -> URL {
        let home = temp.path("factory")
        let dir = home.appendingPathComponent("sessions/-Users-x-proj")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data("""
            {"type":"session_start","cwd":"/Users/x/proj"}
            {"type":"message","timestamp":"2026-10-03T11:00:00Z","message":{"role":"assistant"}}
            {"type":"message","timestamp":"\(lastStamp)","message":{"role":"assistant"}}
            """.utf8).write(to: dir.appendingPathComponent("s1.jsonl"))
        try Data("""
            {"model":"glm-5.2","reasoningEffort":"high",
             "inclusiveTokenUsage":{\(usage)}}
            """.utf8).write(to: dir.appendingPathComponent("s1.settings.json"))
        return home
    }

    private func clineHome(_ temp: TempDir, metadata: String,
                           ended: String = #""ended_at":"2026-10-03T11:00:00Z""#) throws -> URL {
        let home = temp.path("cline")
        let dir = home.appendingPathComponent("data/sessions/t1_abc")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data("""
            {"session_id":"t1_abc","provider":"xai","model":"grok-4-1-fast-reasoning",
             "cwd":"/Users/x/proj","started_at":"2026-10-03T10:00:00Z",
             \(ended),"status":"done",
             "metadata":{"title":"t",\(metadata)}}
            """.utf8).write(to: dir.appendingPathComponent("t1_abc.json"))
        try Data(#"{"version":1,"messages":[]}"#.utf8)
            .write(to: dir.appendingPathComponent("t1_abc.messages.json"))
        return home
    }

    // MARK: - Pi

    @Test func piAssistantMessagesGivePricedEvents() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try piHome(temp, lines:
            """
            {"type":"session","cwd":"/Users/x/proj","timestamp":1759480000000}
            {"type":"message","id":"u1","message":{"role":"user","timestamp":1759480001000}}
            """ + "\n"
            + Self.piMessage(id: "a1", ms: 1759480002000, input: 100, output: 50,
                             reasoning: 20, cacheRead: 300, cost: 0.01)
            + Self.piMessage(id: "a2", ms: 1759480003000, input: 40, output: 30,
                             reasoning: 0, cacheRead: 0, cost: 0.005))
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(piHomes: [home]))

        let events = await ledger.events(in: allTime)
        #expect(events.count == 2)
        let first = try #require(events.first)
        // reasoning is inside output: stored output is output - reasoning.
        #expect(first.inputTokens == 100)
        #expect(first.outputTokens == 30)
        #expect(first.reasoningTokens == 20)
        #expect(first.cacheReadTokens == 300)
        #expect(first.costUSD == 0.01)
        #expect(first.model == "gpt-5.3-codex")
        #expect(first.harness == "pi")
        #expect(first.cwd == "/Users/x/proj")
        // swe's Pi session id is the file's full path.
        #expect(first.sessionId.hasSuffix("_aaaa.jsonl"))
    }

    @Test func piRescanAppendsOnlyTheNewLine() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try piHome(temp, lines:
            Self.piMessage(id: "a1", ms: 1759480002000, input: 10, output: 10,
                           reasoning: 0, cacheRead: 0, cost: 0.001))
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(piHomes: [home]))
        #expect(await ledger.events(in: allTime).count == 1)

        let file = home.appendingPathComponent(
            "agent/sessions/-Users-x-proj/2026-10-03T10-00-00-000Z_aaaa.jsonl")
        try FileManager.default.append(Self.piMessage(
            id: "a2", ms: 1759480003000, input: 20, output: 20,
            reasoning: 0, cacheRead: 0, cost: 0.002), to: file)

        try await ledger.scan(roots: LedgerRoots(piHomes: [home]))
        let events = await ledger.events(in: allTime)
        #expect(events.count == 2)
        #expect(await ledger.totals(in: allTime).costUSD == 0.003)
    }

    // MARK: - Droid

    @Test func droidSettingsGiveOneEventAtTheTranscriptsLastStamp() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try droidHome(temp, usage:
            """
            "inputTokens":1000,"outputTokens":200,"cacheReadTokens":5000,
            "cacheCreationTokens":50,"thinkingTokens":0,"factoryCredits":99
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))

        let events = await ledger.events(in: allTime)
        #expect(events.count == 1)
        let event = try #require(events.first)
        #expect(event.sessionId == "s1")
        #expect(event.inputTokens == 1_000)
        #expect(event.outputTokens == 200)
        #expect(event.cacheReadTokens == 5_000)
        #expect(event.cacheWriteTokens == 50)
        #expect(event.costUSD == nil)
        #expect(event.model == "glm-5.2")
        #expect(event.cwd == "/Users/x/proj")
        #expect(event.isSessionTotal)
        #expect(event.timestamp == utc("2026-10-03T12:00:00Z"))
    }

    @Test func droidRewriteReplacesTheTotals() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try droidHome(temp, usage:
            """
            "inputTokens":1000,"outputTokens":200,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))

        let file = home.appendingPathComponent("sessions/-Users-x-proj/s1.settings.json")
        try Data("""
            {"model":"glm-5.2",
             "inclusiveTokenUsage":{"inputTokens":2000,"outputTokens":400,
             "cacheReadTokens":0,"cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":2}}
            """.utf8).write(to: file)
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))

        let events = await ledger.events(in: allTime)
        #expect(events.count == 1)
        #expect(events.first?.inputTokens == 2_000)
        #expect(events.first?.outputTokens == 400)
    }

    /// A store failure while a session-total file is written rolls the
    /// file back whole: the old totals stay, no cursor moves, the ledger
    /// is left with no transaction open, and the next scan reads the
    /// file again and lands the new totals. The rewrite hits an existing
    /// row, so its key lookup must not keep a read open past the
    /// rollback: with the trigger dropped from another connection in
    /// between, that stale read failed the retry "database is locked".
    @Test func aStoreFailureOnASessionTotalFileKeepsTheOldTotals() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try droidHome(temp, usage:
            """
            "inputTokens":1000,"outputTokens":200,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        #expect(await ledger.events(in: allTime).map(\.outputTokens) == [200])

        // The store refuses the rewrite's totals, the way a full disk
        // fails a write: the same trigger the incremental tests use.
        let db = try SQLiteDatabase(url: temp.path("cache").appending(path: LedgerLocation.fileName))
        try db.execute("""
            CREATE TRIGGER refuse BEFORE INSERT ON events WHEN NEW.output = 666
            BEGIN SELECT RAISE(ABORT, 'refused by the test'); END
            """)
        try Data("""
            {"model":"glm-5.2",
             "inclusiveTokenUsage":{"inputTokens":2000,"outputTokens":666,
             "cacheReadTokens":0,"cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":2}}
            """.utf8).write(to: home.appendingPathComponent("sessions/-Users-x-proj/s1.settings.json"))
        do {
            try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
            Issue.record("a refused write must fail the scan")
        } catch let failure as SQLiteDatabase.Failure {
            #expect(failure.message.contains("refused by the test"))
        }
        #expect(await ledger.events(in: allTime).map(\.outputTokens) == [200])

        try db.execute("DROP TRIGGER refuse")
        let report = try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        // Read again, not skipped as unchanged: no cursor was saved.
        #expect(report.filesRead == 1)
        #expect(report.filesUnchanged == 0)
        #expect(await ledger.events(in: allTime).map(\.outputTokens) == [666])
    }

    @Test func droidAllZeroUsageGivesNoEvent() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try droidHome(temp, usage:
            """
            "inputTokens":0,"outputTokens":0,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":0
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        #expect(await ledger.events(in: allTime).isEmpty)
    }

    @Test func aMalformedFileSkipsWithoutACursor() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try droidHome(temp, usage:
            """
            "inputTokens":100,"outputTokens":20,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1
            """)
        // A settings file caught mid-rewrite must not abort the scan.
        let broken = home.appendingPathComponent("sessions/-Users-x-proj/s2.settings.json")
        try Data("{\"inclusiveTokenUsage\":".utf8).write(to: broken)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        #expect(await ledger.events(in: allTime).count == 1)

        // No cursor was saved, so once the file is fixed a rescan reads it.
        try Data("""
            {"model":"glm-5.2",
             "inclusiveTokenUsage":{"inputTokens":50,"outputTokens":10,
             "cacheReadTokens":0,"cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1}}
            """.utf8).write(to: broken)
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        #expect(await ledger.events(in: allTime).count == 2)
    }

    @Test func aModelChangeWithTheSameTotalsReImports() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try droidHome(temp, usage:
            """
            "inputTokens":1000,"outputTokens":200,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        #expect(await ledger.events(in: allTime).first?.model == "glm-5.2")

        // Same session, same totals, new model: the dim changed, so the
        // re-read must land rather than look identical to the stored row.
        let file = home.appendingPathComponent("sessions/-Users-x-proj/s1.settings.json")
        try Data("""
            {"model":"glm-6",
             "inclusiveTokenUsage":{"inputTokens":1000,"outputTokens":200,
             "cacheReadTokens":0,"cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1}}
            """.utf8).write(to: file)
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        let events = await ledger.events(in: allTime)
        #expect(events.count == 1)
        #expect(events.first?.model == "glm-6")
    }

    @Test func aSessionNowReportingZeroClearsItsEvent() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try droidHome(temp, usage:
            """
            "inputTokens":1000,"outputTokens":200,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        #expect(await ledger.events(in: allTime).count == 1)

        let file = home.appendingPathComponent("sessions/-Users-x-proj/s1.settings.json")
        try Data("""
            {"model":"glm-5.2",
             "inclusiveTokenUsage":{"inputTokens":0,"outputTokens":0,
             "cacheReadTokens":0,"cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":0}}
            """.utf8).write(to: file)
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        #expect(await ledger.events(in: allTime).isEmpty)
    }

    @Test func aNewerTranscriptReReadsUnchangedSettings() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try droidHome(temp, usage:
            """
            "inputTokens":1000,"outputTokens":200,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        #expect(await ledger.events(in: allTime).first?.timestamp == utc("2026-10-03T12:00:00Z"))

        // The settings file is untouched, but the transcript gained a later
        // line, so the event's timestamp must move.
        let transcript = home.appendingPathComponent("sessions/-Users-x-proj/s1.jsonl")
        // The fixture's last line has no trailing newline, so prepend one.
        try FileManager.default.append(
            "\n" + #"{"type":"message","timestamp":"2026-10-03T13:00:00Z","message":{"role":"assistant"}}"#,
            to: transcript)
        try await ledger.scan(roots: LedgerRoots(droidHomes: [home]))
        let events = await ledger.events(in: allTime)
        #expect(events.count == 1)
        #expect(events.first?.timestamp == utc("2026-10-03T13:00:00Z"))
    }

    @Test func sessionTotalsNeverCrossTheLongContextThreshold() async throws {
        // `glm-5.2` prices input at 2x above a 200k-token prompt.
        let pricing = try PricingCatalog(liteLLM: Data("""
            {"glm-5.2": {"input_cost_per_token": 2e-06, "output_cost_per_token": 8e-06,
             "input_cost_per_token_above_200k_tokens": 4e-06,
             "output_cost_per_token_above_200k_tokens": 1.6e-05, "litellm_provider": "openai"}}
            """.utf8), supplement: PricingSupplement())
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let droid = try droidHome(temp, usage:
            """
            "inputTokens":300000,"outputTokens":100,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1
            """)
        let sessionLedger = try Ledger(directory: temp.path("cache1"), pricing: pricing)
        try await sessionLedger.scan(roots: LedgerRoots(droidHomes: [droid]))
        // Session totals are not one request's prompt: base rates apply.
        #expect(await sessionLedger.totals(in: allTime).costUSD == 300_000 * 2e-06 + 100 * 8e-06)

        // A real per-request event over the same threshold still surcharges.
        let claude = temp.path("claude")
        let file = claude.appendingPathComponent("projects/-x/t.jsonl")
        try FileManager.default.createDirectory(
            at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(("""
            {"type":"assistant","timestamp":"2026-10-03T10:00:00Z","sessionId":"s","cwd":"/x",\
            "requestId":"r1","message":{"id":"m1","role":"assistant","model":"glm-5.2",\
            "usage":{"input_tokens":300000,"output_tokens":100}}}
            """ + "\n").utf8).write(to: file)
        let requestLedger = try Ledger(directory: temp.path("cache2"), pricing: pricing)
        try await requestLedger.scan(roots: LedgerRoots(claudeHomes: [claude]))
        #expect(await requestLedger.totals(in: allTime).costUSD == 300_000 * 4e-06 + 100 * 1.6e-05)
    }

    // MARK: - Cline

    @Test func clineReadsTokensInNotUsageInputTokens() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try clineHome(temp, metadata:
            """
            "tokensIn":100,"tokensOut":20,"cacheReads":900,"cacheWrites":5,"totalCost":0.5,
            "usage":{"inputTokens":1000,"outputTokens":20,"cacheReadTokens":900,
                     "cacheWriteTokens":5,"totalCost":0.5}
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(clineHomes: [home]))

        let events = await ledger.events(in: allTime)
        #expect(events.count == 1)
        let event = try #require(events.first)
        // tokensIn, not usage.inputTokens (100, not 1000).
        #expect(event.inputTokens == 100)
        #expect(event.outputTokens == 20)
        #expect(event.cacheReadTokens == 900)
        #expect(event.cacheWriteTokens == 5)
        #expect(event.costUSD == 0.5)
        #expect(event.sessionId == "t1_abc")
        #expect(event.model == "grok-4-1-fast-reasoning")
        #expect(event.timestamp == utc("2026-10-03T11:00:00Z"))
    }

    @Test func clineZeroCostFallsBackToTheCatalog() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try clineHome(temp, metadata:
            """
            "tokensIn":100,"tokensOut":20,"cacheReads":0,"cacheWrites":0,"totalCost":0
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(clineHomes: [home]))
        let event = try #require(await ledger.events(in: allTime).first)
        #expect(event.costUSD == nil)
    }

    @Test func clineNullEndedAtFallsBackToStartedAt() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try clineHome(temp, metadata:
            """
            "tokensIn":100,"tokensOut":20,"cacheReads":0,"cacheWrites":0,"totalCost":0
            """, ended: #""ended_at":null"#)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(clineHomes: [home]))
        let event = try #require(await ledger.events(in: allTime).first)
        #expect(event.timestamp == utc("2026-10-03T10:00:00Z"))
    }

    @Test func clineRewriteReplacesTheSession() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try clineHome(temp, metadata:
            """
            "tokensIn":100,"tokensOut":20,"cacheReads":0,"cacheWrites":0,"totalCost":0.5
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(clineHomes: [home]))

        let file = home.appendingPathComponent("data/sessions/t1_abc/t1_abc.json")
        var object = try #require(
            try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        var metadata = object["metadata"] as? [String: Any] ?? [:]
        metadata["tokensIn"] = 300
        metadata["totalCost"] = 1.5
        object["metadata"] = metadata
        try JSONSerialization.data(withJSONObject: object).write(to: file)
        try await ledger.scan(roots: LedgerRoots(clineHomes: [home]))

        let events = await ledger.events(in: allTime)
        #expect(events.count == 1)
        #expect(events.first?.inputTokens == 300)
        #expect(events.first?.costUSD == 1.5)
    }

    // MARK: - Grok

    private func grokHome(_ temp: TempDir, turns: String) throws -> URL {
        let home = temp.path("grok")
        let dir = home.appendingPathComponent("sessions/proj/s1")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data("""
            {"turns":[\(turns)]}
            """.utf8).write(to: dir.appendingPathComponent("usage.json"))
        return home
    }

    @Test func grokReadsEachTurnAndDropsOneARewriteRemoves() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try grokHome(temp, turns: """
            {"turnNumber":1,"endedAt":"2026-10-03T10:00:00Z","primaryModelId":"grok-4.7",
             "inputTokens":1000,"outputTokens":40,"cachedReadTokens":800,"reasoningTokens":12,
             "costUsdTicks":20000000000},
            {"turnNumber":2,"endedAt":"2026-10-03T11:00:00Z","primaryModelId":"grok-4.7",
             "inputTokens":500,"outputTokens":10,"costUsdTicks":5000000000}
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(grokHomes: [home]))

        let first = await ledger.events(in: allTime)
        #expect(first.count == 2)
        #expect(first.map(\.sessionId) == ["s1", "s1"])
        #expect(first.map(\.costUSD) == [2, 0.5])

        let file = home.appendingPathComponent("sessions/proj/s1/usage.json")
        try Data("""
            {"turns":[
              {"turnNumber":1,"endedAt":"2026-10-03T10:00:00Z","primaryModelId":"grok-4.7",
               "inputTokens":7,"outputTokens":3,"costUsdTicks":1000000000}
            ]}
            """.utf8).write(to: file)
        try await ledger.scan(roots: LedgerRoots(grokHomes: [home]))
        let second = await ledger.events(in: allTime)
        #expect(second.count == 1)
        #expect(second.first?.inputTokens == 7)
        #expect(second.first?.costUSD == 0.1)
    }

    @Test func aSecondGrokScanOfAnUnchangedFileReadsNothing() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let home = try grokHome(temp, turns: """
            {"turnNumber":1,"endedAt":"2026-10-03T10:00:00Z","inputTokens":10,
             "outputTokens":1,"costUsdTicks":10000000000}
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(grokHomes: [home]))
        let report = try await ledger.scan(roots: LedgerRoots(grokHomes: [home]))
        #expect(report.filesRead == 0)
        #expect(report.filesUnchanged == 1)
    }

    // MARK: - Calendar

    @Test func ledgerActivitySkipsPiDroidAndCline() async throws {
        let temp = try TempDir()
        defer { withExtendedLifetime(temp) {} }
        let pi = try piHome(temp, lines:
            Self.piMessage(id: "a1", ms: 1759480002000, input: 10, output: 10,
                           reasoning: 0, cacheRead: 0, cost: 0.001))
        let droid = try droidHome(temp, usage:
            """
            "inputTokens":100,"outputTokens":20,"cacheReadTokens":0,
            "cacheCreationTokens":0,"thinkingTokens":0,"factoryCredits":1
            """)
        let cline = try clineHome(temp, metadata:
            """
            "tokensIn":100,"tokensOut":20,"cacheReads":0,"cacheWrites":0,"totalCost":0.5
            """)
        let ledger = try Ledger(directory: temp.path("cache"))
        try await ledger.scan(roots: LedgerRoots(piHomes: [pi], droidHomes: [droid], clineHomes: [cline]))

        #expect(await ledger.events(in: allTime).count == 3)
        // The calendar keeps Pi and Droid's own file sources and no Cline;
        // the ledger answers for the four original harnesses only.
        let blocks = try await ledger.activityBlocks(
            in: allTime, gap: 3600, force: false)
        #expect(blocks.isEmpty)
    }
}
