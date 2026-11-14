import Foundation
import Testing
@testable import SWECore

/// `swe hooks stats --json` and `swe hooks log <name> --json`, captured from
/// the real command against a throwaway HOME holding 18 made-up runs (see
/// swe-hooks-stats*.json and swe-hooks-log.json in Fixtures): prose-guard on
/// Claude (6 runs, 2 blocks) and Codex (3 runs, 1 block), no-sleep-poll
/// (4 runs, 4 blocks), command_guard (3 runs, no blocks) and
/// claude-plugin/on-stop (2 runs from a Claude transcript, 1 error).
@MainActor
@Suite struct HookStatsTests {
    private func client(_ name: String) throws -> (SweClient, FakeRunner) {
        let runner = FakeRunner(.init(status: 0, stdout: try fixture(name), stderr: ""))
        return (SweClient(runner: runner), runner)
    }

    private func report() async throws -> HookStatsReport {
        try await client("swe-hooks-stats").0.hooksStats(window: .week)
    }

    // MARK: decoding

    @Test func decodesTheStatsRowsAndAsksForTheWindow() async throws {
        let (swe, runner) = try client("swe-hooks-stats")
        let report = try await swe.hooksStats(window: .month)
        #expect(runner.calls.last == ["hooks", "stats", "--since=30d", "--json"])
        #expect(report.window == "7d")
        #expect(report.hooks.count == 5)
        let claude = try #require(report.hooks.first { $0.hook == "prose-guard" && $0.harness == "claude" })
        #expect(claude.fires == 6)
        #expect(claude.blocks == 2)
        #expect(claude.warns == 1)
        #expect(claude.allows == 3)
        #expect(claude.lastBlock != nil)
        #expect(claude.topReasons.first == HookReason(
            decision: "block", reason: "done-followups: missing Follow-ups", count: 2))
    }

    @Test func aHookThatNeverBlockedHasNoLastBlock() async throws {
        let guardRow = try #require(try await report().hooks.first { $0.hook == "command_guard" })
        #expect(guardRow.blocks == 0)
        #expect(guardRow.lastBlock == nil)
        #expect(guardRow.topReasons.isEmpty)
    }

    @Test func anEmptyWindowDecodesToNoRows() async throws {
        let empty = try await client("swe-hooks-stats-empty").0.hooksStats(window: .day)
        #expect(empty.hooks.isEmpty)
    }

    @Test func oneBadRowDoesNotLoseTheRest() async throws {
        let json = #"""
        {"schema": 1, "data": {"window": "7d", "hooks": [
          {"hook": "a", "harness": "claude", "fires": 3, "blocks": 1},
          {"harness": "claude", "fires": "many"},
          {"hook": "b", "harness": "claude", "fires": 2, "future_field": [1, 2]}]}}
        """#
        let swe = SweClient(runner: FakeRunner(.init(status: 0, stdout: Data(json.utf8), stderr: "")))
        let decoded = try await swe.hooksStats(window: .week)
        #expect(decoded.hooks.map(\.hook) == ["a", "b"])
    }

    @Test func decodesTheRecentRuns() async throws {
        let (swe, runner) = try client("swe-hooks-log")
        let log = try await swe.hooksLog(name: "prose-guard", window: .week)
        #expect(runner.calls.last == ["hooks", "log", "prose-guard", "--since=7d", "--limit=5", "--json"])
        #expect(log.hook == "prose-guard")
        #expect(log.events.count == 5)
        let first = try #require(log.events.first)
        #expect(first.decision == "allow")
        #expect(first.project == "alpha")
        #expect(first.gitBranch == "main")
        #expect(first.summary == "Allow · alpha · main")
        // Newest first.
        #expect(log.events.map(\.ts) == log.events.map(\.ts).sorted(by: >))
    }

    @Test func aRunNamesItsProjectBeforeItsFolder() {
        let at = Date(timeIntervalSince1970: 0)
        #expect(HookRunEvent(ts: at, hook: "h", decision: "block", repo: "/w/alpha", cwd: "/w/alpha/sub",
                             gitBranch: "feat/x").summary == "Block · alpha · feat/x")
        #expect(HookRunEvent(ts: at, hook: "h", decision: "warn", repo: nil, cwd: "/w/beta/").summary
                == "Warn · beta")
        #expect(HookRunEvent(ts: at, hook: "h", decision: "error").summary == "Error")
        #expect(HookRunEvent(ts: at, hook: "h", decision: "allow", repo: "", cwd: nil,
                             gitBranch: "").summary == "Allow")
    }

    // MARK: summing across harnesses

    @Test func sumsARowAcrossHarnesses() async throws {
        let table = HookStatsTable(report: try await report(), scripts: ["prose-guard.py"])
        let counts = table.counts(for: "prose-guard.py")
        #expect(counts.fires == 9)
        #expect(counts.blocks == 3)
        #expect(counts.warns == 1)
        #expect(counts.errors == 0)
        #expect(counts.rateLabel == "33%")
    }

    @Test func mergesReasonsAcrossHarnessesMostFirst() async throws {
        let table = HookStatsTable(report: try await report(), scripts: ["prose-guard.py"])
        // Ties keep the order swe gave them in (Claude's row first).
        let reasons = table.counts(for: "prose-guard.py").topReasons
        #expect(reasons.map(\.reason) == ["done-followups: missing Follow-ups", "long paragraph", "em dash found"])
        #expect(reasons.map(\.count) == [2, 1, 1])
        #expect(reasons.map(\.countLabel) == ["2 blocks", "1 warn", "1 block"])
    }

    @Test func theLastBlockIsTheNewestAcrossHarnesses() {
        let early = Date(timeIntervalSince1970: 100), late = Date(timeIntervalSince1970: 900)
        let counts = HookCounts(rows: [
            HookStatsRow(hook: "x", harness: "claude", fires: 1, blocks: 1, lastBlock: early),
            HookStatsRow(hook: "x", harness: "codex", fires: 1, blocks: 1, lastBlock: late),
            HookStatsRow(hook: "x", harness: "gemini", fires: 1)])
        #expect(counts.lastBlock == late)
    }

    @Test func atMostFiveReasonsKeepTheBiggest() {
        let reasons = (1...8).map { HookReason(decision: "block", reason: "r\($0)", count: $0) }
        let counts = HookCounts(rows: [HookStatsRow(hook: "x", harness: "claude", topReasons: reasons)])
        #expect(counts.topReasons.map(\.reason) == ["r8", "r7", "r6", "r5", "r4"])
    }

    @Test func rowsOfOneNameButDifferentHarnessesDoNotLeakIntoOthers() async throws {
        let table = HookStatsTable(report: try await report(),
                                   scripts: ["prose-guard.py", "no-sleep-poll.py"])
        #expect(table.counts(for: "no-sleep-poll.py").fires == 4)
        #expect(table.counts(for: "no-sleep-poll.py").blocks == 4)
        #expect(table.counts(for: "no-sleep-poll.py").rateLabel == "100%")
    }

    // MARK: rate

    @Test func rateReadsAsAWholePercentOrADash() {
        func rate(fires: Int, blocks: Int) -> String {
            HookCounts(rows: [HookStatsRow(hook: "x", harness: "c", fires: fires, blocks: blocks)]).rateLabel
        }
        #expect(rate(fires: 5, blocks: 0) == "0%")
        #expect(rate(fires: 3, blocks: 1) == "33%")
        #expect(rate(fires: 2, blocks: 1) == "50%")
        #expect(rate(fires: 4, blocks: 4) == "100%")
        #expect(rate(fires: 0, blocks: 0) == "-")
        // Never "0%" beside a block.
        #expect(rate(fires: 1000, blocks: 1) == "<1%")
        #expect(rate(fires: 3, blocks: 2) == "67%")
    }

    @Test func aHookWithNoRowsHasNoRuns() async throws {
        let table = HookStatsTable(report: try await report(), scripts: ["quiet.py"])
        let counts = table.counts(for: "quiet.py")
        #expect(!counts.hasRuns)
        #expect(counts.rateLabel == "-")
        #expect(counts.blockPercent == nil)
        #expect(table.statsName(for: "quiet.py") == nil)
    }

    // MARK: name matching

    @Test func anExtensionIsDroppedFromTheFinalComponent() {
        #expect(HookStatsNames.stem("prose-guard.py") == "prose-guard")
        #expect(HookStatsNames.stem("session-start.sh") == "session-start")
        #expect(HookStatsNames.stem("claude-hook.JS") == "claude-hook")
        #expect(HookStatsNames.stem("command-guard/command_guard.py") == "command-guard/command_guard")
        #expect(HookStatsNames.stem("README") == "README")
        #expect(HookStatsNames.stem(".py") == ".py")
        #expect(HookStatsNames.lastComponent("claude-plugin/on-stop") == "on-stop")
        #expect(HookStatsNames.lastComponent("solo") == "solo")
    }

    @Test func equalStemsMatch() {
        let matched = HookStatsNames.match(scripts: ["prose-guard.py", "night-watch.sh"],
                                           statsNames: ["prose-guard", "other"])
        #expect(matched == ["prose-guard.py": "prose-guard"])
    }

    @Test func aScriptInAFolderMatchesOnItsLastComponentWhenUnambiguous() async throws {
        let matched = HookStatsNames.match(scripts: ["command-guard/command_guard.py"],
                                           statsNames: ["command_guard", "prose-guard"])
        #expect(matched == ["command-guard/command_guard.py": "command_guard"])
        // The same through the real report.
        let table = HookStatsTable(report: try await report(), scripts: ["command-guard/command_guard.py"])
        #expect(table.counts(for: "command-guard/command_guard.py").fires == 3)
        #expect(table.statsName(for: "command-guard/command_guard.py") == "command_guard")
    }

    @Test func aFolderPrefixedStatsNameMatchesTheBareScript() async throws {
        let table = HookStatsTable(report: try await report(), scripts: ["on-stop.sh"])
        let counts = table.counts(for: "on-stop.sh")
        #expect(counts.fires == 2)
        #expect(counts.errors == 1)
        #expect(table.statsName(for: "on-stop.sh") == "claude-plugin/on-stop")
    }

    @Test func twoScriptsWithOneLastComponentMatchNeither() {
        let matched = HookStatsNames.match(scripts: ["a/on-stop.sh", "b/on-stop.sh"],
                                           statsNames: ["on-stop"])
        #expect(matched.isEmpty)
    }

    @Test func twoStatsNamesWithOneLastComponentMatchNothing() {
        let matched = HookStatsNames.match(scripts: ["on-stop.sh"],
                                           statsNames: ["a/on-stop", "b/on-stop"])
        #expect(matched.isEmpty)
    }

    @Test func anExactMatchBeatsTheLooserRule() {
        // `a/on-stop` is exact for its script; `b/on-stop.sh` shares a last
        // component with it, so it stays unmatched rather than guessing.
        let matched = HookStatsNames.match(scripts: ["a/on-stop.sh", "b/on-stop.sh"],
                                           statsNames: ["a/on-stop"])
        #expect(matched == ["a/on-stop.sh": "a/on-stop"])
    }

    @Test func theSameScriptOnTwoHarnessesIsOneScript() {
        // herdr's script sits in the Claude and Codex folders: one name, so
        // the last-component rule is not ambiguous.
        let matched = HookStatsNames.match(scripts: ["herdr/notify.sh", "herdr/notify.sh"],
                                           statsNames: ["notify"])
        #expect(matched == ["herdr/notify.sh": "notify"])
    }

    // MARK: window mapping

    @Test func theWindowPickerMapsToSince() {
        #expect(HookStatsWindow.allCases == [.day, .week, .month])
        #expect(HookStatsWindow.day.since == "24h")
        #expect(HookStatsWindow.week.since == "7d")
        #expect(HookStatsWindow.month.since == "30d")
        #expect(HookStatsWindow.allCases.map(\.title) == ["Last 24 hours", "Last 7 days", "Last 30 days"])
        #expect(HookStatsWindow.week.emptyLine == "No runs in the last 7 days.")
        #expect(HookStatsWindow.day.emptyLine == "No runs in the last 24 hours.")
        // The stored choice round-trips through its raw value.
        #expect(HookStatsWindow(rawValue: "30d") == .month)
        #expect(HookStatsWindow(rawValue: "bogus") == nil)
    }

    @Test func relativeTimeReadsInEnglish() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        #expect(HookStatsFormat.relative(now.addingTimeInterval(-3 * 3600), now: now) == "3h ago")
        #expect(HookStatsFormat.relative(now.addingTimeInterval(-2 * 86400), now: now) == "2d ago")
    }

    // MARK: the model

    private func runner(stats: CommandResult? = nil, log: CommandResult? = nil) throws -> ScriptedRunner {
        ScriptedRunner([
            (["hooks", "stats"], try stats ?? .init(status: 0, stdout: fixture("swe-hooks-stats"), stderr: "")),
            (["hooks", "log"], try log ?? .init(status: 0, stdout: fixture("swe-hooks-log"), stderr: "")),
        ])
    }

    @Test func theModelShowsBarsThenNumbers() async throws {
        let model = HookStatsModel()
        #expect(model.state(for: .week, scripts: ["prose-guard.py"]) == .loading)
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        guard case .ready(let table) = model.state(for: .week, scripts: ["prose-guard.py"]) else {
            Issue.record("expected numbers"); return
        }
        #expect(table.counts(for: "prose-guard.py").fires == 9)
        // Another window has not been asked for yet.
        #expect(model.state(for: .month, scripts: ["prose-guard.py"]) == .loading)
        #expect(model.completed == 1)
    }

    @Test func aFailedStatsRunLeavesDashesNotAnError() async throws {
        let model = HookStatsModel()
        let old = CommandResult(status: 1, stdout: Data(), stderr: "Unknown command: 'stats'")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: old)), for: "/bin/swe")
        #expect(model.state(for: .week, scripts: ["x.py"]) == .unavailable)
        #expect(HookStatsState.unavailable.cells(for: "x.py") == .dashes)
        #expect(HookStatsState.loading.cells(for: "x.py") == .loading)
    }

    @Test func aRefreshThatFailsKeepsTheNumbers() async throws {
        let model = HookStatsModel()
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: down)), for: "/bin/swe")
        guard case .ready = model.state(for: .week, scripts: []) else {
            Issue.record("the numbers should stay"); return
        }
    }

    @Test func anotherSweStartsOver() async throws {
        let model = HookStatsModel()
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: down)), for: "/other/swe")
        #expect(model.state(for: .week, scripts: []) == .unavailable)
    }

    @Test func recentRunsLoadPerHookAndWindow() async throws {
        let model = HookStatsModel()
        let swe = SweClient(runner: try runner())
        #expect(model.recentIsLoading(name: "prose-guard", window: .week))
        await model.loadRecent(name: "prose-guard", window: .week, swe: swe, for: "/bin/swe")
        #expect(model.recentRuns(name: "prose-guard", window: .week)?.count == 5)
        #expect(model.recentRuns(name: "prose-guard", window: .day) == nil)
        #expect(!model.recentIsLoading(name: "prose-guard", window: .week))
    }

    @Test func aFailedLogLeavesTheListOut() async throws {
        let model = HookStatsModel()
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.loadRecent(name: "prose-guard", window: .week,
                               swe: SweClient(runner: try runner(log: down)), for: "/bin/swe")
        #expect(model.recentRuns(name: "prose-guard", window: .week) == nil)
        #expect(!model.recentIsLoading(name: "prose-guard", window: .week))
    }
}
