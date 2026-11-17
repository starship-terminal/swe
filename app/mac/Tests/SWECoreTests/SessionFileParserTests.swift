import Foundation
import Testing
@testable import SWECore

/// `PiFileParser`, the line parser for Pi's `<project>/<ts>_<uuid>.jsonl`
/// session logs. `LedgerImportTests` scans whole homes; these pin the
/// per-line rules: the byte gate, the reasoning split, what is dropped,
/// and the cwd a resumed scan remembers.
@Suite struct PiFileParserTests {
    let file = URL(fileURLWithPath: "/h/.pi/agent/sessions/-tmp-proj/2026-10-04T10-00-00Z_aaaa.jsonl")
    /// 2026-10-04T10:00:00Z, in the milliseconds Pi logs.
    let stamp = 1_791_108_000_000

    func parser(savedState: Data? = nil) -> PiFileParser {
        PiFileParser(file: file, accountHome: "/h/.pi", savedState: savedState)
    }

    func line(_ json: String) -> Data { Data(json.utf8) }

    func message(id: String? = "m1", usage: String,
                 model: String = #""model":"pi-1","#, role: String = "assistant",
                 timestamp: String? = nil) -> Data {
        line("""
            {"type":"message",\(id.map { #""id":"\#($0)","# } ?? "")\
            "message":{"role":"\(role)",\(model)"timestamp":\(timestamp ?? "\(stamp)"),\
            "usage":{\(usage)}}}
            """)
    }

    @Test func anAssistantMessageSplitsReasoningOutOfItsOutput() throws {
        var parser = self.parser()
        let records = parser.records(fromLine: message(usage: """
            "input":100,"output":50,"reasoning":20,"cacheRead":800,"cacheWrite":10,\
            "totalTokens":960,"cost":{"total":0.012}
            """))
        let record = try #require(records.count == 1 ? records.first : nil)
        #expect(record.key == "pi:2026-10-04T10-00-00Z_aaaa.jsonl:m1")
        let event = record.event
        #expect(event.harness == "pi")
        #expect(event.accountHome == "/h/.pi")
        // swe keys a Pi session by its log path, not the name alone.
        #expect(event.sessionId == file.path)
        #expect(event.model == "pi-1")
        #expect(event.timestamp == utc("2026-10-04T10:00:00Z"))
        // `output` already holds `reasoning`; splitting keeps them disjoint.
        #expect(event.outputTokens == 30)
        #expect(event.reasoningTokens == 20)
        #expect(event.inputTokens == 100)
        #expect(event.cacheReadTokens == 800)
        #expect(event.cacheWriteTokens == 10)
        #expect(event.totalTokens == 960)
        #expect(event.costUSD == 0.012)
        #expect(event.isSessionTotal == false)
        #expect(!record.authoritative)
    }

    @Test func reasoningNeverMakesTheOutputNegative() {
        var parser = self.parser()
        let records = parser.records(fromLine: message(usage:
            #""input":10,"output":30,"reasoning":50"#))
        #expect(records.first?.event.outputTokens == 0)
        #expect(records.first?.event.reasoningTokens == 30)
        #expect(records.first?.event.totalTokens == 40)
    }

    @Test func onlyAssistantMessagesWithUsageCount() {
        var parser = self.parser()
        #expect(parser.records(fromLine: message(usage: #""input":10,"output":5"#,
                                                 role: "user")).isEmpty)
        #expect(parser.records(fromLine: message(
            id: nil, usage: #""input":10,"output":5"#, model: "", timestamp: #""later""#)).isEmpty)
        // No usage block at all, and a session record itself, both yield nothing.
        #expect(parser.records(fromLine: line("""
            {"type":"message","message":{"role":"assistant","timestamp":1791079200000}}
            """)).isEmpty)
        #expect(parser.records(fromLine: line(#"{"type":"session","cwd":"/x"}"#)).isEmpty)
    }

    @Test func theByteGateCoversSessionInfoButNotOtherRecords() {
        var parser = self.parser()
        // `"session` has no closing quote on purpose: session_info passes
        // the gate, then its type yields nothing.
        let info = line(#"{"type":"session_info","version":5}"#)
        #expect(parser.wants(line: info))
        #expect(parser.records(fromLine: info).isEmpty)
        let thinking = line(#"{"type":"thinking","text":"hmm"}"#)
        #expect(!parser.wants(line: thinking))
        #expect(parser.records(fromLine: thinking).isEmpty)
    }

    @Test func anEmptyRequestIsDroppedUnlessItCarriesACost() {
        var parser = self.parser()
        #expect(parser.records(fromLine: message(usage:
            #""input":0,"output":0,"reasoning":0,"cacheRead":0,"cacheWrite":0"#)).isEmpty)
        // A priced event with no token counts is still usage.
        let priced = parser.records(fromLine: message(usage:
            #""input":0,"output":0,"reasoning":0,"cost":{"total":0.25}"#))
        #expect(priced.count == 1)
        #expect(priced.first?.event.costUSD == 0.25)
        #expect(priced.first?.event.totalTokens == 0)
        // A zero cost changes nothing.
        #expect(parser.records(fromLine: message(usage:
            #""input":0,"output":0,"cost":{"total":0}"#)).isEmpty)
    }

    @Test func theSessionLinesCwdReachesEveryEventAndSurvivesAResume() throws {
        var parser = self.parser()
        _ = parser.records(fromLine: line(#"{"type":"session","cwd":"/work/app"}"#))
        #expect(parser.records(fromLine: message(usage: #""input":1,"output":1"#))
            .first?.event.cwd == "/work/app")
        // A later session line replaces it.
        _ = parser.records(fromLine: line(#"{"type":"session","cwd":"/work/other"}"#))
        #expect(parser.records(fromLine: message(usage: #""input":1,"output":1"#))
            .first?.event.cwd == "/work/other")
        // An incremental scan resumes mid-file: a parser rebuilt from the
        // saved state still knows the cwd.
        var resumed = self.parser(savedState: try #require(parser.savedState()))
        #expect(resumed.records(fromLine: message(usage: #""input":1,"output":1"#))
            .first?.event.cwd == "/work/other")
    }

    @Test func aMessageWithoutAnIdKeysOnItsTimestamp() {
        var parser = self.parser()
        let records = parser.records(fromLine: message(
            id: nil, usage: #""input":1,"output":1"#))
        // A rescan still collides with the stored event.
        #expect(records.first?.key
                == "pi:2026-10-04T10-00-00Z_aaaa.jsonl:1791108000000.000")
    }

    @Test func aMissingModelIsUnknown() {
        var parser = self.parser()
        let records = parser.records(fromLine: message(
            usage: #""input":1,"output":1"#, model: ""))
        #expect(records.first?.event.model == "unknown")
    }
}

/// `SessionTotalsFile.droid`: `<uuid>.settings.json`, rewritten in place, so
/// one event per session keyed `droid:<uuid>` — and `.empty` when the file
/// parses but reports nothing, which deletes what an earlier scan stored.
@Suite struct DroidSessionFileTests {
    private func dir(_ temp: TempDir) throws -> URL {
        let dir = temp.path("sessions/-Users-x-proj")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func settings(_ temp: TempDir, _ json: String, named name: String = "s1") throws -> URL {
        let file = try dir(temp).appendingPathComponent("\(name).settings.json")
        try Data(json.utf8).write(to: file)
        return file
    }

    private func unwrap(_ totals: SessionTotals?) -> LedgerRecord? {
        guard case .record(let record) = totals else {
            Issue.record("expected a record, got \(String(describing: totals))")
            return nil
        }
        return record
    }

    @Test func inclusiveUsageWinsAndThinkingIsReasoning() throws {
        let temp = try TempDir()
        try Data(#"{"type":"message","timestamp":"2026-10-03T11:00:00Z"}"#.utf8)
            .write(to: try dir(temp).appendingPathComponent("s1.jsonl"))
        let file = try settings(temp, """
            {"model":"glm-5.2",
             "inclusiveTokenUsage":{"inputTokens":1000,"outputTokens":200,"cacheReadTokens":50,
             "cacheCreationTokens":30,"thinkingTokens":80,"factoryCredits":99},
             "tokenUsage":{"inputTokens":5}}
            """)
        let record = try #require(unwrap(try SessionTotalsFile.droid(
            settings: file, accountHome: "/h/.factory")))
        #expect(record.key == "droid:s1")
        #expect(record.authoritative)
        let event = record.event
        #expect(event.harness == "droid")
        #expect(event.sessionId == "s1")
        #expect(event.isSessionTotal)
        #expect(event.inputTokens == 1_000)
        #expect(event.outputTokens == 200)
        #expect(event.cacheReadTokens == 50)
        #expect(event.cacheWriteTokens == 30)
        #expect(event.reasoningTokens == 80)
        // factoryCredits is not USD; the catalog prices the event.
        #expect(event.costUSD == nil)
        #expect(event.timestamp == utc("2026-10-03T11:00:00Z"))
    }

    @Test func theTranscriptsCwdAndLastTimestampSpanChunksAndAnOpenLastLine() throws {
        // Many lines across the reader's chunks, then a last line still
        // being written (no newline): swe counts it, so this does too.
        let temp = try TempDir()
        var text = #"{"type":"session_start","cwd":"/repo/d","timestamp":"2026-10-03T10:00:00Z"}"# + "\n"
        let filler = #"{"type":"message","text":"\#(String(repeating: "x", count: 200))"}"# + "\n"
        text += String(repeating: filler, count: 10_000)
        text += #"{"type":"message","timestamp":"2026-10-03T12:30:00Z"}"#
        try Data(text.utf8).write(to: try dir(temp).appendingPathComponent("s1.jsonl"))
        let file = try settings(temp, #"{"tokenUsage":{"inputTokens":7,"outputTokens":3}}"#)
        let record = try #require(unwrap(try SessionTotalsFile.droid(
            settings: file, accountHome: "/h")))
        #expect(record.event.cwd == "/repo/d")
        #expect(record.event.timestamp == utc("2026-10-03T12:30:00Z"))
    }

    @Test func tokenUsageIsTheFallbackWhenInclusiveIsMissing() throws {
        let temp = try TempDir()
        let file = try settings(temp, #"{"tokenUsage":{"inputTokens":7,"outputTokens":3}}"#)
        let record = try #require(unwrap(try SessionTotalsFile.droid(
            settings: file, accountHome: "/h")))
        #expect(record.event.inputTokens == 7)
        #expect(record.event.outputTokens == 3)
    }

    @Test func aSettingsFileWithNothingToCountEmptiesItsKey() throws {
        let temp = try TempDir()
        let none = try settings(temp, #"{"model":"glm-5.2"}"#)
        guard case .empty(let key) = try SessionTotalsFile.droid(settings: none, accountHome: "/h")
        else {
            Issue.record("expected .empty for a file with no usage block")
            return
        }
        #expect(key == "droid:s1")
        let zero = try settings(temp, #"{"inclusiveTokenUsage":{"inputTokens":0,"outputTokens":0}}"#,
                                named: "s2")
        guard case .empty(let zeroKey) = try SessionTotalsFile.droid(settings: zero, accountHome: "/h")
        else {
            Issue.record("expected .empty for all-zero usage")
            return
        }
        #expect(zeroKey == "droid:s2")
    }

    @Test func withoutATranscriptTheSettingsStampStands() throws {
        let temp = try TempDir()
        let file = try settings(temp, #"{"inclusiveTokenUsage":{"inputTokens":5}}"#)
        let stamp = utc("2026-10-02T08:00:00Z")
        try FileManager.default.setAttributes([.modificationDate: stamp], ofItemAtPath: file.path)
        let record = try #require(unwrap(try SessionTotalsFile.droid(
            settings: file, accountHome: "/h")))
        #expect(record.event.cwd == nil)
        #expect(abs(record.event.timestamp.timeIntervalSince(stamp)) < 0.001)
    }

    @Test func theTranscriptGivesCwdAndTheLastTimestamp() throws {
        let temp = try TempDir()
        try Data("""
            {"type":"session_start","cwd":"/work/first"}
            {"type":"session_start","cwd":"/work/later","timestamp":"2026-10-03T10:00:00Z"}
            {"type":"message","timestamp":"2026-10-03T12:00:00Z"}
            """.utf8).write(to: try dir(temp).appendingPathComponent("s1.jsonl"))
        let file = try settings(temp, #"{"inclusiveTokenUsage":{"inputTokens":5}}"#)
        let record = try #require(unwrap(try SessionTotalsFile.droid(
            settings: file, accountHome: "/h")))
        // The first session_start wins; the last timestamp anywhere does.
        #expect(record.event.cwd == "/work/first")
        #expect(record.event.timestamp == utc("2026-10-03T12:00:00Z"))
    }

    @Test func theTranscriptIsTheSettingsNameMinusBothExtensions() {
        #expect(SessionTotalsFile.droidTranscript(
            for: URL(fileURLWithPath: "/s/proj/x.settings.json")).path == "/s/proj/x.jsonl")
        #expect(SessionTotalsFile.droidTranscript(
            for: URL(fileURLWithPath: "/s/proj/a.b.settings.json")).path == "/s/proj/a.b.jsonl")
    }

    @Test func aFileCaughtMidRewriteThrows() throws {
        let temp = try TempDir()
        let file = try settings(temp, #"{"inclusiveTokenUsage":"#)
        // The scan skips the file without a cursor, so it retries next round.
        #expect(throws: Error.self) {
            try SessionTotalsFile.droid(settings: file, accountHome: "/h")
        }
    }
}

/// `SessionTotalsFile.cline`: `<id>.json` beside its `.messages.json`
/// transcript. `nil` means "not a session file, leave it alone"; `.empty`
/// means the session parses but has nothing to count, so its stored event
/// goes away.
@Suite struct ClineSessionFileTests {
    private func read(_ temp: TempDir, _ json: String) throws -> SessionTotals? {
        let dir = temp.path("data/sessions/t1_abc")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("t1_abc.json")
        try Data(json.utf8).write(to: file)
        return try SessionTotalsFile.cline(session: file, accountHome: "/h/.cline")
    }

    private func unwrap(_ totals: SessionTotals?) -> LedgerRecord? {
        guard case .record(let record) = totals else {
            Issue.record("expected a record, got \(String(describing: totals))")
            return nil
        }
        return record
    }

    @Test func theMetadataRowBecomesOneAuthoritativeSessionEvent() throws {
        let temp = try TempDir()
        let record = try #require(unwrap(try read(temp, """
            {"session_id":"t1_abc","model":"grok-4","cwd":"/work",
             "started_at":"2026-10-03T10:00:00Z","ended_at":"2026-10-03T11:00:00Z",
             "metadata":{"tokensIn":100,"tokensOut":20,"cacheReads":900,"cacheWrites":5,
             "totalCost":0.5}}
            """)))
        #expect(record.key == "cline:t1_abc")
        #expect(record.authoritative)
        #expect(record.event.isSessionTotal)
        #expect(record.event.harness == "cline")
        #expect(record.event.accountHome == "/h/.cline")
        #expect(record.event.totalTokens == 1_025)
        #expect(record.event.costUSD == 0.5)
        #expect(record.event.timestamp == utc("2026-10-03T11:00:00Z"))
    }

    @Test func aCostWithNoTokensStillRecords() throws {
        let temp = try TempDir()
        let record = try #require(unwrap(try read(temp, """
            {"session_id":"t1_abc","started_at":"2026-10-03T10:00:00Z",
             "metadata":{"tokensIn":0,"tokensOut":0,"cacheReads":0,"cacheWrites":0,"totalCost":0.5}}
            """)))
        #expect(record.event.totalTokens == 0)
        #expect(record.event.costUSD == 0.5)
    }

    @Test func nothingToCountAndNoCostEmptiesTheKey() throws {
        let temp = try TempDir()
        let totals = try read(temp, """
            {"session_id":"t1_abc","started_at":"2026-10-03T10:00:00Z",
             "metadata":{"tokensIn":0,"tokensOut":0,"cacheReads":0,"cacheWrites":0,"totalCost":0}}
            """)
        guard case .empty(let key) = totals else {
            Issue.record("expected .empty, got \(String(describing: totals))")
            return
        }
        #expect(key == "cline:t1_abc")
    }

    @Test func aFileWithoutSessionIdIsNotASessionFile() throws {
        let temp = try TempDir()
        // nil, not .empty: nothing is stored under a key the file never had.
        #expect(try read(temp, #"{"metadata":{"tokensIn":100}}"#) == nil)
    }

    @Test func theWorkspaceRootIsTheCwdFallback() throws {
        let temp = try TempDir()
        let record = try #require(unwrap(try read(temp, """
            {"session_id":"t1_abc","workspace_root":"/ws",
             "started_at":"2026-10-03T10:00:00Z",
             "metadata":{"tokensIn":1,"tokensOut":1}}
            """)))
        #expect(record.event.cwd == "/ws")
    }

    @Test func noReadableTimestampEmptiesTheKey() throws {
        let temp = try TempDir()
        // `ended_at` may be null while the session runs; a started_at the
        // parser cannot read leaves nothing to stamp.
        let totals = try read(temp, """
            {"session_id":"t1_abc","started_at":"soon",
             "metadata":{"tokensIn":1,"tokensOut":1}}
            """)
        guard case .empty(let key) = totals else {
            Issue.record("expected .empty, got \(String(describing: totals))")
            return
        }
        #expect(key == "cline:t1_abc")
    }
}

/// `SessionTotalsFile.grok`: one `usage.json` per session, one event per
/// turn. The cumulative `session` block is the same numbers summed, so it
/// is never stored.
@Suite struct GrokSessionFileTests {
    private func read(_ temp: TempDir, _ json: String, context: String? = nil) throws -> [SessionTotals] {
        let dir = temp.path("sessions/proj/s1")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let context {
            try Data(context.utf8).write(to: dir.appendingPathComponent("prompt_context.json"))
        }
        let file = dir.appendingPathComponent("usage.json")
        try Data(json.utf8).write(to: file)
        return try SessionTotalsFile.grok(usage: file, accountHome: "/h/.grok")
    }

    private func records(_ rows: [SessionTotals]) -> [LedgerRecord] {
        rows.compactMap { row in
            if case .record(let record) = row { return record }
            return nil
        }
    }

    @Test func oneEventPerTurnIgnoresTheSessionTotal() throws {
        let temp = try TempDir()
        let rows = try read(temp, """
            {"session":{"inputTokens":999999,"costUsdTicks":9000000000000},
             "turns":[
               {"turnNumber":1,"endedAt":"2026-10-03T10:00:00Z","primaryModelId":"grok-4.7",
                "inputTokens":1000,"outputTokens":40,"cachedReadTokens":800,
                "cacheCreationTokens":2,"reasoningTokens":12,"costUsdTicks":20000000000},
               {"turnNumber":2,"endedAt":"2026-10-03T11:00:00Z","primaryModelId":"grok-4.7",
                "inputTokens":500,"outputTokens":10,"cachedReadTokens":400,
                "cacheCreationTokens":0,"reasoningTokens":0,"costUsdTicks":5000000000}
             ]}
            """, context: #"{"working_directory":"/work/app"}"#)
        let records = records(rows)
        #expect(records.map(\.key) == ["grok:s1:1", "grok:s1:2"])
        let event = records[0].event
        #expect(event.harness == "grok")
        #expect(event.accountHome == "/h/.grok")
        #expect(event.sessionId == "s1")
        #expect(event.cwd == "/work/app")
        #expect(event.model == "grok-4.7")
        #expect(!event.isSessionTotal)
        #expect(records[0].authoritative)
        #expect(event.inputTokens == 1_000)
        #expect(event.outputTokens == 40)
        #expect(event.cacheReadTokens == 800)
        #expect(event.cacheWriteTokens == 2)
        #expect(event.reasoningTokens == 12)
        #expect(event.costUSD == 2)
        #expect(event.timestamp == utc("2026-10-03T10:00:00Z"))
        #expect(records[1].event.costUSD == 0.5)
        guard case .kept(let prefix, let turns) = rows.last else {
            Issue.record("expected a kept marker")
            return
        }
        #expect(prefix == "grok:s1:")
        #expect(turns == [1, 2])
    }

    @Test func zeroCostFallsThroughAndABlankTurnEmpties() throws {
        let temp = try TempDir()
        let rows = try read(temp, """
            {"turns":[
               {"turnNumber":1,"endedAt":"2026-10-03T10:00:00Z","inputTokens":10,
                "outputTokens":1,"costUsdTicks":0},
               {"turnNumber":2,"endedAt":"2026-10-03T10:00:00Z","inputTokens":0,
                "outputTokens":0,"costUsdTicks":0}
             ]}
            """)
        #expect(records(rows).first?.event.costUSD == nil)
        guard case .empty(let key) = rows[1] else {
            Issue.record("expected .empty for a blank turn")
            return
        }
        #expect(key == "grok:s1:2")
    }

    @Test func aMissingTimestampAndADuplicateTurnAreSkipped() throws {
        let temp = try TempDir()
        let rows = try read(temp, """
            {"turns":[
               {"turnNumber":1,"endedAt":null,"inputTokens":10,"costUsdTicks":10000000000},
               {"turnNumber":3,"endedAt":"2026-10-03T10:00:00Z","inputTokens":10,
                "costUsdTicks":20000000000},
               {"turnNumber":3,"endedAt":"2026-10-03T12:00:00Z","inputTokens":99,
                "costUsdTicks":9000000000}
             ]}
            """)
        guard case .empty = rows[0] else {
            Issue.record("expected .empty for a turn with no timestamp")
            return
        }
        let records = records(rows)
        #expect(records.map(\.key) == ["grok:s1:3"])
        #expect(records[0].event.costUSD == 2)
    }

    @Test func aFileCaughtMidRewriteThrows() throws {
        let temp = try TempDir()
        let dir = temp.path("sessions/proj/s1")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("usage.json")
        try Data(#"{"turns":["#.utf8).write(to: file)
        #expect(throws: Error.self) {
            try SessionTotalsFile.grok(usage: file, accountHome: "/h")
        }
    }
}
