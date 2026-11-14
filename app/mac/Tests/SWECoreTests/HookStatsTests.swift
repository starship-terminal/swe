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

    /// Scripts whose id is their name and whose key is that name's stem, as
    /// a file hook's `stats_keys` reads, for tests that do not care about paths.
    private func S(_ names: String...) -> [HookStatsScript] {
        names.map { HookStatsScript(id: $0, keys: [HookStatsNames.stem($0)]) }
    }

    /// The real `swe hooks list --json` of a hub with two directory hooks and
    /// a file hook (swe-hooks-directory.json), each row carrying `stats_keys`.
    private func inventory() async throws -> [Hook] {
        try await client("swe-hooks-directory").0.hooksList().hooks
    }

    private func stats(_ rows: [HookStatsRow]) -> HookStatsReport {
        HookStatsReport(window: "7d", hooks: rows)
    }

    // MARK: decoding

    @Test func decodesTheStatsRowsAndAsksForTheWindow() async throws {
        let (swe, runner) = try client("swe-hooks-stats")
        let report = try await swe.hooksStats(window: .month)
        #expect(runner.calls.last == ["hooks", "stats", "--since=30d", "--sum-harnesses", "--json"])
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
        let table = HookStatsTable(report: try await report(), scripts: S("prose-guard.py"))
        let counts = table.counts(for: "prose-guard.py")
        #expect(counts.fires == 9)
        #expect(counts.blocks == 3)
        #expect(counts.warns == 1)
        #expect(counts.errors == 0)
        #expect(counts.rateLabel == "33%")
    }

    @Test func mergesReasonsAcrossHarnessesMostFirst() async throws {
        let table = HookStatsTable(report: try await report(), scripts: S("prose-guard.py"))
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
                                   scripts: S("prose-guard.py", "no-sleep-poll.py"))
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
        let table = HookStatsTable(report: try await report(), scripts: S("quiet.py"))
        let counts = table.counts(for: "quiet.py")
        #expect(!counts.hasRuns)
        #expect(counts.rateLabel == "-")
        #expect(counts.blockPercent == nil)
        #expect(table.statsNames(for: "quiet.py").isEmpty)
    }

    // MARK: matching by the keys swe gives

    @Test func theInventoryCarriesTheKeysItsRunsAreLoggedUnder() async throws {
        let hooks = try await inventory()
        // A directory hook is listed by its folder, its runs carry the executable.
        #expect(hooks.map(\.script) == ["bundle", "multi", "prose-guard.py"])
        #expect(hooks.map(\.statsKeys) == [["guard"], ["check", "scan"], ["prose-guard"]])
    }

    @Test func aDirectoryHookCountsTheRunsOfItsExecutable() async throws {
        let hooks = try await inventory()
        let bundle = try #require(hooks.first { $0.script == "bundle" })
        let table = HookStatsTable(report: stats([
            HookStatsRow(hook: "guard", harness: "", fires: 7, blocks: 2),
            // A helper inside the folder that nothing declares is no key, so
            // another hook of that name does not leak in.
            HookStatsRow(hook: "util", harness: "", fires: 50),
            HookStatsRow(hook: "bundle", harness: "", fires: 9)]),
            scripts: HookStatsScript.scripts(for: hooks))
        #expect(table.counts(for: bundle.source).fires == 7)
        #expect(table.counts(for: bundle.source).blocks == 2)
        #expect(table.statsNames(for: bundle.source) == ["guard"])
    }

    @Test func severalKeysAreSummedAndTheirReasonsMergedBeforeTheCap() async throws {
        let hooks = try await inventory()
        let multi = try #require(hooks.first { $0.script == "multi" })
        let table = HookStatsTable(report: stats([
            HookStatsRow(hook: "check", harness: "", fires: 5, blocks: 3,
                         lastBlock: Date(timeIntervalSince1970: 100),
                         topReasons: [HookReason(decision: "block", reason: "shared", count: 3)]),
            HookStatsRow(hook: "scan", harness: "", fires: 4, blocks: 4,
                         lastBlock: Date(timeIntervalSince1970: 900),
                         topReasons: [HookReason(decision: "block", reason: "shared", count: 2)]
                            + (1...4).map { HookReason(decision: "block", reason: "own-\($0)", count: 1) }
                            + [HookReason(decision: "warn", reason: "extra", count: 1)])]),
            scripts: HookStatsScript.scripts(for: hooks))
        let counts = table.counts(for: multi.source)
        #expect((counts.fires, counts.blocks) == (9, 7))
        #expect(counts.lastBlock == Date(timeIntervalSince1970: 900))
        #expect(counts.topReasons.count == 5)
        #expect(counts.topReasons.first == HookReason(decision: "block", reason: "shared", count: 5))
        #expect(table.statsNames(for: multi.source) == ["check", "scan"])
    }

    @Test func aPrefixedKeyFindsItsRunsInTheRealStats() async throws {
        // `${CLAUDE_PLUGIN_ROOT}/hooks/on-stop.sh`: swe gives the row the key
        // the transcript logged it under.
        let script = HookStatsScript(id: "/p/on-stop.sh", keys: ["claude-plugin/on-stop"])
        let table = HookStatsTable(report: try await report(), scripts: [script])
        #expect(table.counts(for: "/p/on-stop.sh").fires == 2)
        #expect(table.counts(for: "/p/on-stop.sh").errors == 1)
    }

    @Test func aRowWithNoKeysShowsDashesNeverAnotherHooksNumbers() async throws {
        // Two files with one basename: swe gives neither the shared name.
        let scripts = [HookStatsScript(id: "/a/guard.sh", keys: []), HookStatsScript(id: "/b/guard.sh", keys: [])]
        let state = HookStatsState.ready(HookStatsTable(report: try await report(), scripts: scripts))
        #expect(state.cells(for: "/a/guard.sh") == .dashes)
        #expect(state.cells(for: "/b/guard.sh") == .dashes)
    }

    @Test func aKeyedRowWithNoRunsReadsZero() async throws {
        let script = HookStatsScript(id: "/q.py", keys: ["quiet"])
        let state = HookStatsState.ready(HookStatsTable(report: try await report(), scripts: [script]))
        guard case .counts(let counts) = state.cells(for: "/q.py") else { Issue.record("expected counts"); return }
        #expect(!counts.hasRuns)
    }

    @Test func anSweWithNoKeysFallsBackToTheFileStem() async throws {
        // The older list fixture has no stats_keys.
        let hooks = try await client("swe-hooks").0.hooksList().hooks
        let guard_ = try #require(hooks.first { $0.script == "prose-guard.py" })
        #expect(guard_.statsKeys == nil)
        let scripts = HookStatsScript.scripts(for: hooks)
        #expect(scripts.first { $0.id == guard_.source }?.keys == ["prose-guard"])
        let table = HookStatsTable(report: try await report(), scripts: scripts)
        #expect(table.counts(for: guard_.source).fires == 9)
    }

    @Test func theSameSharedSourceOnTwoHarnessesIsOneScript() {
        let hooks = [
            Hook(script: "notify.sh", harness: "claude", source: "/h/.swe/hooks/shared/notify.sh",
                 destination: "/h/.claude/hooks/notify.sh", state: .linked, statsKeys: ["notify"]),
            Hook(script: "notify.sh", harness: "codex", source: "/h/.swe/hooks/shared/notify.sh",
                 destination: "/h/.codex/hooks/notify.sh", state: .linked, statsKeys: ["notify"])]
        #expect(HookStatsScript.scripts(for: hooks).count == 1)
    }

    @Test func anExtensionIsDroppedForTheFallbackKey() {
        #expect(HookStatsNames.stem("prose-guard.py") == "prose-guard")
        #expect(HookStatsNames.stem("session-start.sh") == "session-start")
        #expect(HookStatsNames.stem("claude-hook.JS") == "claude-hook")
        #expect(HookStatsNames.stem("README") == "README")
        #expect(HookStatsNames.stem(".py") == ".py")
    }

    // MARK: exact totals and identities

    @Test func theSummedReportHasOneRowPerHookWithNoHarness() async throws {
        let (swe, runner) = try client("swe-hooks-stats-summed")
        let report = try await swe.hooksStats(window: .week)
        #expect(runner.calls.last == ["hooks", "stats", "--since=7d", "--sum-harnesses", "--json"])
        #expect(report.hooks.map(\.harness) == ["", "", "", ""])
        let prose = try #require(report.hooks.first { $0.hook == "prose-guard" })
        // Claude's 6 runs and Codex's 3, folded by swe before it cut the reasons.
        #expect((prose.fires, prose.blocks) == (9, 3))
        let counts = HookStatsTable(report: report, scripts: S("prose-guard.py")).counts(for: "prose-guard.py")
        #expect(counts.topReasons.first?.count == 2)
    }

    @Test func runsAtOneTimestampKeepDistinctIds() async throws {
        // Parallel tool calls one hook blocked share the record's timestamp.
        let json = #"""
        {"schema": 1, "data": {"hook": "x", "window": "7d", "since": 0, "matched": 3, "events": [
          {"ts": 1000, "hook": "x", "harness": "claude", "decision": "block"},
          {"ts": 1000, "hook": "x", "harness": "claude", "decision": "block"},
          {"ts": 1000, "hook": "x", "harness": "claude", "decision": "block", "repo": null}]}}
        """#
        let swe = SweClient(runner: FakeRunner(.init(status: 0, stdout: Data(json.utf8), stderr: "")))
        let log = try await swe.hooksLog(name: "x", window: .week)
        #expect(log.events.count == 3)
        #expect(Set(log.events.map(\.id)).count == 3)
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
        #expect(model.state(for: .week, scripts: S("prose-guard.py")) == .loading)
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        guard case .ready(let table) = model.state(for: .week, scripts: S("prose-guard.py")) else {
            Issue.record("expected numbers"); return
        }
        #expect(table.counts(for: "prose-guard.py").fires == 9)
        // Another window has not been asked for yet.
        #expect(model.state(for: .month, scripts: S("prose-guard.py")) == .loading)
        #expect(model.completed == 1)
    }

    @Test func aFailedStatsRunLeavesDashesNotAnError() async throws {
        let model = HookStatsModel()
        let old = CommandResult(status: 1, stdout: Data(), stderr: "Unknown command: 'stats'")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: old)), for: "/bin/swe")
        #expect(model.state(for: .week, scripts: S("x.py")) == .unavailable)
        #expect(HookStatsState.unavailable.cells(for: "x.py") == .dashes)
        #expect(HookStatsState.loading.cells(for: "x.py") == .loading)
    }

    @Test func aRefreshThatFailsKeepsTheNumbers() async throws {
        let model = HookStatsModel()
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: down)), for: "/bin/swe")
        guard case .ready = model.state(for: .week, scripts: S()) else {
            Issue.record("the numbers should stay"); return
        }
    }

    @Test func anotherSweStartsOver() async throws {
        let model = HookStatsModel()
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: down)), for: "/other/swe")
        #expect(model.state(for: .week, scripts: S()) == .unavailable)
    }

    @Test func recentRunsLoadPerHookAndWindow() async throws {
        let model = HookStatsModel()
        let swe = SweClient(runner: try runner())
        #expect(model.recentIsLoading(names: ["prose-guard"], window: .week))
        await model.loadRecent(names: ["prose-guard"], window: .week, swe: swe, for: "/bin/swe")
        #expect(model.recentRuns(names: ["prose-guard"], window: .week)?.count == 5)
        #expect(model.recentRuns(names: ["prose-guard"], window: .day) == nil)
        #expect(!model.recentIsLoading(names: ["prose-guard"], window: .week))
    }

    @Test func recentRunsMergeEveryKeyNewestFive() async throws {
        func log(_ hook: String, _ times: [Int]) -> CommandResult {
            let events = times.map {
                #"{"ts": \#($0), "hook": "\#(hook)", "harness": "claude", "decision": "block"}"#
            }.joined(separator: ",")
            return .init(status: 0, stdout: Data(
                #"{"schema": 1, "data": {"hook": "\#(hook)", "events": [\#(events)]}}"#.utf8), stderr: "")
        }
        // `guard` ran often yesterday; `check` blocked just now.
        let runner = ScriptedRunner([
            (["hooks", "log", "guard"], log("guard", [500, 400, 300, 200, 100])),
            (["hooks", "log", "check"], log("check", [900, 800]))])
        let model = HookStatsModel()
        await model.loadRecent(names: ["check", "guard"], window: .week,
                               swe: SweClient(runner: runner), for: "/bin/swe")
        let runs = try #require(model.recentRuns(names: ["check", "guard"], window: .week))
        #expect(runs.map { Int($0.ts.timeIntervalSince1970) } == [900, 800, 500, 400, 300])
        #expect(Set(runs.map(\.id)).count == 5)
    }

    @Test func aFailedLogLeavesTheListOut() async throws {
        let model = HookStatsModel()
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.loadRecent(names: ["prose-guard"], window: .week,
                               swe: SweClient(runner: try runner(log: down)), for: "/bin/swe")
        #expect(model.recentRuns(names: ["prose-guard"], window: .week) == nil)
        #expect(!model.recentIsLoading(names: ["prose-guard"], window: .week))
    }
}
