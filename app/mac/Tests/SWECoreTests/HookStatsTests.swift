import Foundation
import Testing
@testable import SWECore

/// `swe hooks stats --json`, `swe hooks list --json` and `swe hooks log
/// --source=<path> --json`, captured from the real commands against a
/// throwaway HOME (see swe-hooks-identity-list.json, swe-hooks-stats*.json
/// and swe-hooks-log.json in Fixtures). The home holds a file hook
/// (prose-guard.py: 6 runs, 2 blocks), a directory hook (bundle: check.py on
/// PreToolUse and scripts/on-stop.sh on Stop, 5 runs, 2 blocks, plus an
/// undeclared lib/util.py), a shared hook deployed to six harnesses
/// (notify.py: 3 Claude runs logged by the shim and again in the transcript,
/// 2 Codex runs, 1 block), two foreign files with one basename
/// (plugins/a/guard.sh with 2 runs, plugins/b/guard.sh with none) and an old
/// Codex log line named `guard` that could be either, and a
/// `${CLAUDE_PLUGIN_ROOT}` run no listed script owns.
@MainActor
@Suite struct HookStatsTests {
    private static let home = "/tmp/swe-fixture-home"
    private static let bundle = "\(home)/.swe/hooks/claude/bundle"
    private static let prose = "\(home)/.swe/hooks/claude/prose-guard.py"
    private static let notify = "\(home)/.swe/hooks/shared/notify.py"
    private static let guardA = "\(home)/plugins/a/guard.sh"
    private static let guardB = "\(home)/plugins/b/guard.sh"

    private func client(_ name: String) throws -> (SweClient, FakeRunner) {
        let runner = FakeRunner(.init(status: 0, stdout: try fixture(name), stderr: ""))
        return (SweClient(runner: runner), runner)
    }

    private func client(json: String) -> SweClient {
        SweClient(runner: FakeRunner(.init(status: 0, stdout: Data(json.utf8), stderr: "")))
    }

    private func report() async throws -> HookStatsReport {
        try await client("swe-hooks-stats-summed").0.hooksStats(window: .week)
    }

    private func table() async throws -> HookStatsTable {
        HookStatsTable(report: try await report())
    }

    /// The real `swe hooks list --json` of the same home.
    private func inventory() async throws -> [Hook] {
        try await client("swe-hooks-identity-list").0.hooksList().hooks
    }

    private func counts(_ cells: HookStatsCells) -> HookCounts? {
        if case .counts(let counts) = cells { return counts }
        return nil
    }

    // MARK: decoding

    @Test func decodesTheSummedRowsAndAsksForTheWindow() async throws {
        let (swe, runner) = try client("swe-hooks-stats-summed")
        let report = try await swe.hooksStats(window: .month)
        #expect(runner.calls.last == ["hooks", "stats", "--since=30d", "--sum-harnesses", "--json"])
        #expect(report.window == "7d")
        #expect(report.identified)
        #expect(report.hooks.count == 6)
        #expect(Set(report.hooks.map(\.harness)) == [""])
        let prose = try #require(report.hooks.first { $0.sourcePaths == [Self.prose] })
        #expect((prose.hook, prose.fires, prose.blocks, prose.warns, prose.allows) == ("prose-guard", 6, 2, 1, 3))
        #expect(prose.lastBlock != nil)
        #expect(prose.topReasons.first == HookReason(
            decision: "block", reason: "done-followups: missing Follow-ups", count: 2))
        #expect(report.uncertain == [Self.guardA, Self.guardB])
    }

    @Test func thePerHarnessReportCarriesTheJoinToo() async throws {
        let report = try await client("swe-hooks-stats").0.hooksStats(window: .week)
        let notify = report.hooks.filter { $0.sourcePaths == [Self.notify] }
        #expect(Set(notify.map(\.harness)) == ["claude", "codex"])
        #expect(notify.map(\.fires).reduce(0, +) == 5)
    }

    @Test func runsNoListedScriptOwnsHaveNoSource() async throws {
        let loose = try await report().hooks.filter(\.sourcePaths.isEmpty)
        #expect(Set(loose.map(\.hook)) == ["claude-plugin/on-stop", "guard"])
        #expect(loose.allSatisfy { $0.identified })
    }

    @Test func anEmptyWindowDecodesToNoRows() async throws {
        let empty = try await client("swe-hooks-stats-empty").0.hooksStats(window: .day)
        #expect(empty.hooks.isEmpty)
        #expect(empty.uncertain.isEmpty)
        #expect(empty.identified)
    }

    @Test func oneBadRowDoesNotLoseTheRest() async throws {
        let decoded = try await client(json: #"""
        {"schema": 1, "data": {"window": "7d", "hooks": [
          {"hook": "a", "harness": "", "source_paths": ["/a.py"], "fires": 3, "blocks": 1},
          {"harness": "claude", "fires": "many"},
          {"hook": "b", "harness": "", "source_paths": [], "fires": 2, "future_field": [1, 2]}]}}
        """#).hooksStats(window: .week)
        #expect(decoded.hooks.map(\.hook) == ["a", "b"])
        #expect(decoded.identified)
    }

    @Test func decodesTheRecentRunsOfOneSource() async throws {
        let (swe, runner) = try client("swe-hooks-log")
        let log = try await swe.hooksLog(source: Self.bundle, window: .week)
        #expect(runner.calls.last == ["hooks", "log", "--source=\(Self.bundle)", "--since=7d", "--limit=5", "--json"])
        #expect(log.hook == "bundle")
        #expect(log.events.count == 5)
        // Both of the folder's executables, newest first: the Stop block
        // half an hour ago leads the PreToolUse runs.
        #expect(Set(log.events.map(\.hook)) == ["bundle/on-stop", "check"])
        let first = try #require(log.events.first)
        #expect(first.summary == "Block · alpha · main")
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

    @Test func runsAtOneTimestampKeepDistinctIds() async throws {
        // Parallel tool calls one hook blocked share the record's timestamp.
        let log = try await client(json: #"""
        {"schema": 1, "data": {"hook": "x", "window": "7d", "since": 0, "matched": 3, "events": [
          {"ts": 1000, "hook": "x", "harness": "claude", "decision": "block"},
          {"ts": 1000, "hook": "x", "harness": "claude", "decision": "block"},
          {"ts": 1000, "hook": "x", "harness": "claude", "decision": "block", "repo": null}]}}
        """#).hooksLog(source: "/x.py", window: .week)
        #expect(log.events.count == 3)
        #expect(Set(log.events.map(\.id)).count == 3)
    }

    // MARK: joining list rows to stats rows

    @Test func everyListRowJoinsByItsSourcePath() async throws {
        let hooks = try await inventory()
        let table = try await table()
        func fires(_ source: String) -> Int? { counts(table.cells(for: source))?.fires }
        // A directory hook is listed by its folder and counts both of its
        // declared executables; the helper nothing declares adds nothing.
        let bundle = try #require(hooks.first { $0.script == "bundle" })
        #expect(bundle.source == Self.bundle)
        #expect(fires(bundle.source) == 5)
        #expect(table.counts(for: bundle.source).blocks == 2)
        #expect(fires(Self.prose) == 6)
        // The shared script's six harness rows share one source and so the
        // one row: the shim's Claude runs and the transcript's copies of
        // them count once, Codex's are added by swe.
        let notify = hooks.filter { $0.script == "notify.py" }
        #expect(notify.count == 6)
        #expect(Set(notify.map { fires($0.source) }) == [5])
        #expect(table.counts(for: Self.notify).blocks == 1)
    }

    @Test func aDirectoryHooksReasonsComeRankedFromSwe() async throws {
        let reasons = try await table().counts(for: Self.bundle).topReasons
        #expect(Set(reasons.map(\.reason)) == ["no force push", "stop-rule: tests not run"])
        #expect(reasons.allSatisfy { $0.count == 1 && $0.decision == "block" })
    }

    @Test func twoFilesWithOneBasenameNeverShareAndADoubtShowsDashes() async throws {
        let hooks = try await inventory()
        let guards = hooks.filter { $0.script == "guard.sh" }
        #expect(Set(guards.map(\.source)) == [Self.guardA, Self.guardB])
        let table = try await table()
        // a ran twice, b never did, and an old Codex line named `guard`
        // could be either: swe gives it to neither and marks both.
        #expect(table.cells(for: Self.guardA) == .dashes)
        #expect(table.cells(for: Self.guardB) == .dashes)
        #expect(table.isUnknown(Self.guardA))
        // Without the doubt, a's own runs and b's none.
        let sure = HookStatsReport(window: "7d", hooks: try await report().hooks)
        #expect(HookStatsTable(report: sure).counts(for: Self.guardA).fires == 2)
        #expect(!HookStatsTable(report: sure).counts(for: Self.guardB).hasRuns)
    }

    @Test func aScriptWithNoRunsOfItsOwnReadsZero() async throws {
        let table = try await table()
        let cells = table.cells(for: "\(Self.home)/.swe/hooks/claude/quiet.py")
        let quiet = try #require(counts(cells))
        #expect(!quiet.hasRuns)
        #expect(quiet.rateLabel == "-")
        #expect(quiet.blockPercent == nil)
    }

    @Test func namesAreNeverMatched() {
        // A row whose name is a script's but which names no source (runs no
        // listed script owns), and one that belongs to another source.
        let table = HookStatsTable(report: HookStatsReport(window: "7d", hooks: [
            HookStatsRow(hook: "bundle", fires: 9),
            HookStatsRow(hook: "prose-guard", sourcePaths: ["/elsewhere/prose-guard.py"], fires: 4)]))
        #expect(!table.counts(for: Self.bundle).hasRuns)
        #expect(!table.counts(for: Self.prose).hasRuns)
        #expect(table.counts(for: "/elsewhere/prose-guard.py").fires == 4)
    }

    @Test func aSecondRowForOneSourceIsNotAddedUp() {
        let table = HookStatsTable(report: HookStatsReport(window: "7d", hooks: [
            HookStatsRow(hook: "x", sourcePaths: ["/x.py"], fires: 3),
            HookStatsRow(hook: "x", sourcePaths: ["/x.py"], fires: 4)]))
        #expect(table.counts(for: "/x.py").fires == 3)
    }

    @Test func anSweWithoutSourcePathsShowsDashesForEveryScript() async throws {
        // The shape `swe hooks stats` had before the join: rows by name only.
        let old = try await client(json: #"""
        {"schema": 1, "data": {"window": "7d", "hooks": [
          {"hook": "prose-guard", "harness": "", "fires": 6, "blocks": 2}]}}
        """#).hooksStats(window: .week)
        #expect(!old.identified)
        let table = HookStatsTable(report: old)
        #expect(table.cells(for: Self.prose) == .dashes)
        #expect(table.isUnknown(Self.prose))
    }

    // MARK: rate

    @Test func rateReadsAsAWholePercentOrADash() {
        func rate(fires: Int, blocks: Int) -> String {
            HookCounts(row: HookStatsRow(hook: "x", fires: fires, blocks: blocks)).rateLabel
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

    @Test func atMostFiveReasonsAreKept() {
        let reasons = (1...8).map { HookReason(decision: "block", reason: "r\($0)", count: 9 - $0) }
        let counts = HookCounts(row: HookStatsRow(hook: "x", topReasons: reasons))
        #expect(counts.topReasons.map(\.reason) == ["r1", "r2", "r3", "r4", "r5"])
        #expect(HookCounts.none == HookCounts(row: nil))
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
            (["hooks", "stats"], try stats ?? .init(status: 0, stdout: fixture("swe-hooks-stats-summed"), stderr: "")),
            (["hooks", "log"], try log ?? .init(status: 0, stdout: fixture("swe-hooks-log"), stderr: "")),
        ])
    }

    @Test func theModelShowsBarsThenNumbers() async throws {
        let model = HookStatsModel()
        #expect(model.state(for: .week) == .loading)
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        guard case .ready(let table) = model.state(for: .week) else {
            Issue.record("expected numbers"); return
        }
        #expect(table.counts(for: Self.prose).fires == 6)
        // Another window has not been asked for yet.
        #expect(model.state(for: .month) == .loading)
        #expect(model.completed == 1)
    }

    @Test func aFailedStatsRunLeavesDashesNotAnError() async throws {
        let model = HookStatsModel()
        let old = CommandResult(status: 1, stdout: Data(), stderr: "Unknown command: 'stats'")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: old)), for: "/bin/swe")
        #expect(model.state(for: .week) == .unavailable)
        #expect(HookStatsState.unavailable.cells(for: "x.py") == .dashes)
        #expect(HookStatsState.loading.cells(for: "x.py") == .loading)
    }

    @Test func aRefreshThatFailsKeepsTheNumbers() async throws {
        let model = HookStatsModel()
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: down)), for: "/bin/swe")
        guard case .ready = model.state(for: .week) else {
            Issue.record("the numbers should stay"); return
        }
    }

    @Test func anotherSweStartsOver() async throws {
        let model = HookStatsModel()
        await model.load(window: .week, swe: SweClient(runner: try runner()), for: "/bin/swe")
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.load(window: .week, swe: SweClient(runner: try runner(stats: down)), for: "/other/swe")
        #expect(model.state(for: .week) == .unavailable)
    }

    @Test func recentRunsLoadPerSourceAndWindowInOneAsk() async throws {
        let model = HookStatsModel()
        let runner = try runner()
        let swe = SweClient(runner: runner)
        #expect(model.recentIsLoading(source: Self.bundle, window: .week))
        await model.loadRecent(source: Self.bundle, window: .week, swe: swe, for: "/bin/swe")
        let runs = try #require(model.recentRuns(source: Self.bundle, window: .week))
        #expect(runs.count == 5)
        #expect(Set(runs.map(\.id)).count == 5)
        #expect(model.recentRuns(source: Self.bundle, window: .day) == nil)
        #expect(model.recentRuns(source: Self.prose, window: .week) == nil)
        #expect(!model.recentIsLoading(source: Self.bundle, window: .week))
        #expect(runner.calls.filter { $0.starts(with: ["hooks", "log"]) }
                == [["hooks", "log", "--source=\(Self.bundle)", "--since=7d", "--limit=5", "--json"]])
    }

    @Test func aFailedLogLeavesTheListOut() async throws {
        let model = HookStatsModel()
        let down = CommandResult(status: 1, stdout: Data(), stderr: "boom")
        await model.loadRecent(source: Self.prose, window: .week,
                               swe: SweClient(runner: try runner(log: down)), for: "/bin/swe")
        #expect(model.recentRuns(source: Self.prose, window: .week) == nil)
        #expect(!model.recentIsLoading(source: Self.prose, window: .week))
    }
}
