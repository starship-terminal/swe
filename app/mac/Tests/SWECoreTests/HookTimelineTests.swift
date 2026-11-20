import Foundation
import Testing
@testable import SWECore

/// The by-event layout of the Hooks page, built from rows shaped like this
/// Mac's real `swe hooks list`: five Claude Code hooks of your own, herdr's
/// and Agent Relay's scripts beside them, and a hub folder nothing declares.
@Suite struct HookTimelineTests {
    static let claudeEvents = ["PreToolUse", "PostToolUse", "PermissionRequest", "UserPromptSubmit",
                               "SessionStart", "SessionEnd", "Stop", "SubagentStart"]

    static func hook(_ script: String, harness: String = "claude", tier: Hook.Tier = .harness,
                     state: LinkState = .linked, _ declared: [(String, String)],
                     summary: String = "") -> Hook {
        Hook(script: script, harness: harness, source: "/h/\(harness)/\(script)",
             destination: "/h/.\(harness)/hooks/\(script)", state: state,
             declared: declared.map {
                 HookDeclaration(event: $0.0, matcher: $0.1, timeout: 10,
                                 command: script, settings: "/h/.\(harness)/settings.json")
             },
             tier: tier, enabled: true, eventsSupported: claudeEvents, summary: summary)
    }

    static let hooks: [Hook] = [
        hook("no-sleep-poll.py", [("PreToolUse", "Bash")]),
        hook("pr-behind-main.py", [("PreToolUse", "Bash")]),
        hook("prose-guard.py", [("Stop", "")]),
        hook("session-size-warn.py", [("PreToolUse", "")]),
        hook("task-model-nudge.py", [("PreToolUse", "Task")]),
        hook("herdr-agent-state.sh", tier: .foreign, state: LinkState(rawValue: "foreign"),
             [("SessionStart", "*")]),
        hook("claude-hook.sh", tier: .foreign, state: LinkState(rawValue: "foreign"),
             [("SessionStart", ""), ("Stop", "")]),
        hook("codex-hook.sh", harness: "codex", tier: .foreign, state: LinkState(rawValue: "foreign"),
             [("Stop", "")]),
        hook("command-guard", harness: "command-guard", state: .skipped, []),
    ]

    @Test func lanesFollowTheSessionOrderAndHoldOnlyYoursByDefault() {
        let timeline = HookTimeline(harness: "claude", hooks: Self.hooks, includeForeign: false)
        #expect(timeline.lanes.map(\.event) == ["PreToolUse", "Stop"])
        #expect(timeline.foreignCount == 2)
        let pre = timeline.lanes[0].entries
        // No matcher first, then by matcher.
        #expect(pre.map(\.hook.script) == ["session-size-warn.py", "no-sleep-poll.py",
                                           "pr-behind-main.py", "task-model-nudge.py"])
        #expect(pre.map(\.matcherLabel) == ["any tool", "Bash", "Bash", "Task"])
        #expect(timeline.lanes[1].entries.first?.matcherLabel == nil)
        #expect(timeline.lanes[0].caption == "before a tool runs")
    }

    @Test func quietEventsAreTheSupportedOnesNothingFiresOn() {
        let timeline = HookTimeline(harness: "claude", hooks: Self.hooks, includeForeign: false)
        #expect(timeline.quietEvents == ["SessionStart", "UserPromptSubmit", "PermissionRequest",
                                         "PostToolUse", "SubagentStart", "SessionEnd"])
    }

    @Test func otherInstallersHooksJoinTheirLanesAfterYours() {
        let timeline = HookTimeline(harness: "claude", hooks: Self.hooks, includeForeign: true)
        #expect(timeline.lanes.map(\.event) == ["SessionStart", "PreToolUse", "Stop"])
        let start = timeline.lanes[0].entries
        // "*" on SessionStart gates nothing, so it shows no matcher.
        #expect(start.map(\.matcherLabel) == [nil, nil])
        #expect(start.allSatisfy { $0.isForeign })
        let stop = timeline.lanes[2].entries
        #expect(stop.map(\.hook.script) == ["prose-guard.py", "claude-hook.sh"])
        #expect(!timeline.quietEvents.contains("SessionStart"))
    }

    @Test func aScriptDeclaredTwiceOnOneEventIsOneEntry() {
        let twice = [Self.hook("cursor-hook.sh", harness: "cursor", tier: .foreign,
                               [("stop", ""), ("stop", "")])]
        let timeline = HookTimeline(harness: "cursor", hooks: twice + twice, includeForeign: true)
        #expect(timeline.lanes.map { $0.entries.count } == [1])
    }

    @Test func cursorsLowercaseEventsSortWithTheRest() {
        #expect(HookEvents.sorted(["stop", "beforeShellExecution", "sessionStart", "zzCustom"])
                == ["sessionStart", "beforeShellExecution", "stop", "zzCustom"])
        #expect(HookEvents.caption(for: "preToolUse") == "before a tool runs")
        #expect(HookEvents.caption(for: "zzCustom") == nil)
    }

    @Test func tabsPutHarnessesWithYourHooksFirst() {
        #expect(HookTimeline.harnesses(in: Self.hooks) == ["claude", "codex"])
    }

    @Test func notRunningIsYourUndeclaredScriptsOnly() {
        let foreignIdle = Self.hook("state", harness: "cursor", tier: .foreign, [])
        #expect(HookTimeline.notRunning(in: Self.hooks + [foreignIdle]).map(\.script) == ["command-guard"])
    }

    @Test func shortSummaryDropsTheEventPrefix() {
        #expect(HookTimeline.shortSummary("PreToolUse hook (Bash): block synchronous sleep-polling of PR/CI state.")
                == "Block synchronous sleep-polling of PR/CI state.")
        #expect(HookTimeline.shortSummary("Stop hook: block replies that break the rules.")
                == "Block replies that break the rules.")
        #expect(HookTimeline.shortSummary("Warns when the disk fills.") == "Warns when the disk fills.")
        #expect(HookTimeline.shortSummary("") == "")
    }

    // MARK: The table's rows

    static func scripts(_ table: HookTable) -> [String] {
        table.rows.map { "\($0.hook.harness):\($0.hook.script)@\($0.event ?? "-")" }
    }

    @Test func oneHarnessFlattensItsLanesInSessionOrderThenNotRunning() {
        let table = HookTable(hooks: Self.hooks, harness: "claude", includeForeign: false)
        #expect(Self.scripts(table) == [
            "claude:session-size-warn.py@PreToolUse", "claude:no-sleep-poll.py@PreToolUse",
            "claude:pr-behind-main.py@PreToolUse", "claude:task-model-nudge.py@PreToolUse",
            "claude:prose-guard.py@Stop",
        ])
        #expect(table.rows.map(\.matcherLabel) == ["any tool", "Bash", "Bash", "Task", nil])
        #expect(table.foreignCount == 2)
        #expect(table.quietEvents == ["SessionStart", "UserPromptSubmit", "PermissionRequest",
                                      "PostToolUse", "SubagentStart", "SessionEnd"])
        #expect(table.rows.allSatisfy { $0.isRunning })
    }

    @Test func otherToolsJoinTheirEventsWhenAskedFor() {
        let off = HookTable(hooks: Self.hooks, harness: "claude", includeForeign: false)
        let on = HookTable(hooks: Self.hooks, harness: "claude", includeForeign: true)
        #expect(!off.rows.contains { $0.isForeign })
        #expect(Self.scripts(on) == [
            "claude:claude-hook.sh@SessionStart", "claude:herdr-agent-state.sh@SessionStart",
            "claude:session-size-warn.py@PreToolUse", "claude:no-sleep-poll.py@PreToolUse",
            "claude:pr-behind-main.py@PreToolUse", "claude:task-model-nudge.py@PreToolUse",
            "claude:prose-guard.py@Stop", "claude:claude-hook.sh@Stop",
        ])
        #expect(on.foreignCount == off.foreignCount)
        #expect(!on.quietEvents.contains("SessionStart"))
    }

    @Test func everyHarnessSortsByEventThenHarnessAndListsEveryIdleScript() {
        let cursor = Self.hook("guard.sh", harness: "cursor", [("preToolUse", "")])
        let table = HookTable(hooks: Self.hooks + [cursor], harness: nil, includeForeign: true)
        // Cursor's preToolUse sits with Claude Code's PreToolUse, after it:
        // the harnesses go in dropdown order (most of your own hooks first).
        #expect(Self.scripts(table) == [
            "claude:claude-hook.sh@SessionStart", "claude:herdr-agent-state.sh@SessionStart",
            "claude:session-size-warn.py@PreToolUse", "claude:no-sleep-poll.py@PreToolUse",
            "claude:pr-behind-main.py@PreToolUse", "claude:task-model-nudge.py@PreToolUse",
            "cursor:guard.sh@preToolUse",
            "claude:prose-guard.py@Stop", "claude:claude-hook.sh@Stop", "codex:codex-hook.sh@Stop",
            "command-guard:command-guard@-",
        ])
        #expect(table.harness == nil)
        #expect(table.quietEvents.isEmpty)
        #expect(table.foreignCount == 3)
        let idle = table.rows.last
        #expect(idle?.isRunning == false)
        #expect(idle?.matcherLabel == nil)
    }

    @Test func everyHarnessWithoutOtherToolsKeepsOnlyYours() {
        let table = HookTable(hooks: Self.hooks, harness: nil, includeForeign: false)
        #expect(!table.rows.contains { $0.isForeign })
        #expect(table.rows.count == 6)
        // Codex has only herdr's script, so it counts but shows nothing.
        #expect(table.foreignCount == 3)
    }

    @Test func aScriptInTwoHarnessesIsTwoRowsWithTheirOwnKeys() {
        let shared = [
            Self.hook("notify.py", harness: "claude", [("Stop", "")]),
            Self.hook("notify.py", harness: "codex", [("Stop", "")]),
        ]
        let table = HookTable(hooks: shared, harness: nil, includeForeign: false)
        #expect(table.rows.map(\.hookKey) == ["claude|/h/claude/notify.py", "codex|/h/codex/notify.py"])
        #expect(Set(table.rows.map(\.id)).count == 2)
        let one = HookTable(hooks: shared, harness: "codex", includeForeign: false)
        #expect(one.rows.map(\.hookKey) == ["codex|/h/codex/notify.py"])
    }

    @Test func aScriptOnTwoEventsIsARowOnEach() {
        let both = [Self.hook("audit.py", [("SessionEnd", ""), ("SessionStart", "")])]
        let table = HookTable(hooks: both, harness: "claude", includeForeign: false)
        #expect(table.rows.map(\.event) == ["SessionStart", "SessionEnd"])
        #expect(Set(table.rows.map(\.id)).count == 2)
        #expect(Set(table.rows.map(\.hookKey)).count == 1)
    }

    @Test func notRunningRowsFollowTheHarnessPicked() {
        let idle = Self.hook("draft.py", harness: "claude", [])
        let hooks = Self.hooks + [idle]
        let claude = HookTable(hooks: hooks, harness: "claude", includeForeign: false)
        #expect(claude.rows.filter { !$0.isRunning }.map(\.hook.script) == ["draft.py"])
        let all = HookTable(hooks: hooks, harness: nil, includeForeign: false)
        #expect(all.rows.filter { !$0.isRunning }.map(\.hook.script) == ["command-guard", "draft.py"])
        #expect(all.rows.suffix(2).allSatisfy { !$0.isRunning })
    }

    @Test func aStoredHarnessThatIsGoneOrEmptyMeansEveryHarness() {
        #expect(HookTable.harness(stored: "", in: Self.hooks) == nil)
        #expect(HookTable.harness(stored: "gemini", in: Self.hooks) == nil)
        // A folder with no declared hook gets no dropdown entry.
        #expect(HookTable.harness(stored: "command-guard", in: Self.hooks) == nil)
        #expect(HookTable.harness(stored: "codex", in: Self.hooks) == "codex")
    }

    @Test func aListRowCarriesItsSummary() throws {
        let row = #"{"name": "a.py", "harness": "claude", "source_path": "/a", "dest_path": "/b", "link_status": "linked", "declared": [], "summary": "Stop hook: hi."}"#
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let hook = try decoder.decode(Hook.self, from: Data(row.utf8))
        #expect(hook.summary == "Stop hook: hi.")
    }
}
