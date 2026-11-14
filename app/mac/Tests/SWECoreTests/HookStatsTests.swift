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

    /// Scripts whose id is their name, for tests that do not care about paths.
    private func S(_ names: String...) -> [HookStatsScript] {
        names.map { HookStatsScript(id: $0, names: [$0]) }
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
        let matched = HookStatsNames.match(scripts: S("prose-guard.py", "night-watch.sh"),
                                           statsNames: ["prose-guard", "other"])
        #expect(matched.names == ["prose-guard.py": ["prose-guard"]])
        #expect(matched.ambiguous.isEmpty)
    }

    @Test func aScriptInAFolderMatchesOnItsLastComponentWhenUnambiguous() async throws {
        let matched = HookStatsNames.match(scripts: S("command-guard/command_guard.py"),
                                           statsNames: ["command_guard", "prose-guard"])
        #expect(matched.names == ["command-guard/command_guard.py": ["command_guard"]])
        // The same through the real report.
        let table = HookStatsTable(report: try await report(), scripts: S("command-guard/command_guard.py"))
        #expect(table.counts(for: "command-guard/command_guard.py").fires == 3)
        #expect(table.statsName(for: "command-guard/command_guard.py") == "command_guard")
    }

    @Test func aFolderPrefixedStatsNameMatchesTheBareScript() async throws {
        let table = HookStatsTable(report: try await report(), scripts: S("on-stop.sh"))
        let counts = table.counts(for: "on-stop.sh")
        #expect(counts.fires == 2)
        #expect(counts.errors == 1)
        #expect(table.statsName(for: "on-stop.sh") == "claude-plugin/on-stop")
    }

    @Test func twoScriptsWithOneLastComponentMatchNeither() {
        let matched = HookStatsNames.match(scripts: S("a/on-stop.sh", "b/on-stop.sh"),
                                           statsNames: ["claude-plugin/on-stop"])
        #expect(matched.names.isEmpty)
        #expect(matched.ambiguous == ["a/on-stop.sh", "b/on-stop.sh"])
    }

    @Test func twoStatsNamesWithOneLastComponentMatchNothing() {
        let matched = HookStatsNames.match(scripts: S("on-stop.sh"),
                                           statsNames: ["a/on-stop", "b/on-stop"])
        #expect(matched.names.isEmpty)
        #expect(matched.ambiguous == ["on-stop.sh"])
    }

    @Test func anExactMatchBeatsTheLooserRule() {
        // `a/on-stop` is exact for its script; the other shares a last
        // component with it, so it is left ambiguous rather than guessed.
        let matched = HookStatsNames.match(
            scripts: [HookStatsScript(id: "/p/a/on-stop.sh", names: ["a/on-stop.sh"]),
                      HookStatsScript(id: "/p/b/on-stop.sh", names: ["b/on-stop.sh"])],
            statsNames: ["a/on-stop"])
        #expect(matched.names == ["/p/a/on-stop.sh": ["a/on-stop"]])
        #expect(matched.ambiguous == ["/p/b/on-stop.sh"])
    }

    @Test func theSameSharedSourceOnTwoHarnessesIsOneScript() {
        // A shared script is a row per harness it deploys to, all with one
        // source: it is one script, so the last-component rule holds.
        let hooks = [
            Hook(script: "notify.sh", harness: "claude", source: "/h/.swe/hooks/shared/notify.sh",
                 destination: "/h/.claude/hooks/notify.sh", state: .linked),
            Hook(script: "notify.sh", harness: "codex", source: "/h/.swe/hooks/shared/notify.sh",
                 destination: "/h/.codex/hooks/notify.sh", state: .linked)]
        let scripts = HookStatsScript.scripts(for: hooks)
        #expect(scripts.count == 1)
        let matched = HookStatsNames.match(scripts: scripts, statsNames: ["herdr/notify"])
        #expect(matched.names == ["/h/.swe/hooks/shared/notify.sh": ["herdr/notify"]])
    }

    @Test func twoFilesWithOneBasenameDoNotShareCounts() async throws {
        // Hook.script is only the basename, so two plugins' on-stop.sh look
        // alike by name; their sources tell them apart, and neither takes
        // the one hook's history.
        let hooks = [
            Hook(script: "on-stop.sh", harness: "claude", source: "/plugins/a/on-stop.sh",
                 destination: "/plugins/a/on-stop.sh", state: .keep, tier: .foreign),
            Hook(script: "on-stop.sh", harness: "claude", source: "/plugins/b/on-stop.sh",
                 destination: "/plugins/b/on-stop.sh", state: .keep, tier: .foreign)]
        let table = HookStatsTable(report: try await report(), scripts: HookStatsScript.scripts(for: hooks))
        for hook in hooks {
            #expect(table.isAmbiguous(hook.source))
            #expect(table.statsName(for: hook.source) == nil)
        }
        let state = HookStatsState.ready(table)
        #expect(state.cells(for: "/plugins/a/on-stop.sh") == .dashes)
        #expect(state.cells(for: "/plugins/b/on-stop.sh") == .dashes)
    }

    // MARK: directory hooks

    /// `swe hooks list --json` for a hub with `claude/bundle/guard.py`
    /// (a directory hook declared through `bundle/guard.py`) and a file
    /// hook; swe-hooks-directory.json. The list names the row after its
    /// folder, `bundle`, and carries only the folder as its source.
    private func directoryInventory(home: String) async throws -> [Hook] {
        var text = String(decoding: try fixture("swe-hooks-directory"), as: UTF8.self)
        text = text.replacingOccurrences(of: "/tmp/swe-fixture-home", with: home)
        let swe = SweClient(runner: FakeRunner(.init(status: 0, stdout: Data(text.utf8), stderr: "")))
        return try await swe.hooksList().hooks
    }

    @Test func aDirectoryHookCountsTheRunsOfItsExecutable() async throws {
        let home = try TempDir()
        let folder = home.path(".swe/hooks/claude/bundle")
        try FileManager.default.createDirectory(at: folder.appendingPathComponent("lib"),
                                                withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: folder.appendingPathComponent("__pycache__"),
                                                withIntermediateDirectories: true)
        for file in ["guard.py", "lib/util.py", "README.md", "__pycache__/guard.py"] {
            try Data("x".utf8).write(to: folder.appendingPathComponent(file))
        }
        let hooks = try await directoryInventory(home: home.url.path)
        // What the inventory really says: the folder's name, the folder as its source.
        let bundle = try #require(hooks.first { $0.script == "bundle" })
        #expect(bundle.source == folder.path)

        let children = HookStatsScript.childNames(of: bundle.source)
        #expect(children.contains("guard") && children.contains("bundle/guard"))
        #expect(children.contains("util") && children.contains("lib/util"))
        #expect(!children.contains("README"))
        #expect(HookStatsScript.childNames(of: hooks[1].source).isEmpty)

        // Its runs are logged as `guard`, the executable's stem.
        let report = HookStatsReport(window: "7d", hooks: [
            HookStatsRow(hook: "guard", harness: "", fires: 7, blocks: 2),
            HookStatsRow(hook: "prose-guard", harness: "", fires: 3)])
        let scripts = HookStatsScript.scripts(for: hooks, children: [bundle.source: children])
        let table = HookStatsTable(report: report, scripts: scripts)
        #expect(table.counts(for: bundle.source).fires == 7)
        #expect(table.counts(for: bundle.source).blocks == 2)
        #expect(table.statsName(for: bundle.source) == "guard")
        // Without the folder's contents the display name finds nothing: the bug.
        let blind = HookStatsTable(report: report, scripts: HookStatsScript.scripts(for: hooks))
        #expect(blind.counts(for: bundle.source).fires == 0)
        // The file hook beside it is unaffected.
        let prose = try #require(hooks.first { $0.script == "prose-guard.py" })
        #expect(table.counts(for: prose.source).fires == 3)
    }

    @Test func aFolderThatIsGoneHasNoChildren() {
        #expect(HookStatsScript.childNames(of: "/nonexistent/swe-test/bundle").isEmpty)
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
