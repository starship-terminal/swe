import AppKit
import SwiftUI
import SWECore
import Testing
@testable import SWEApp

/// The Hooks page laid out offscreen from fixture hooks, the way the window
/// shows it: the table's columns must hold their x positions on every row,
/// a row with no matcher included, and every header must fit its column.
/// With `SWE_HOOKS_SNAPSHOT_DIR` set, each state is also written there as a
/// PNG for a person to look at.
@Suite @MainActor struct HooksPageLayoutTests {
    /// The window sizes checked: a large one and the 900pt minimum. The
    /// page gets the window less the standard sidebar, as in `MainWindow`.
    static let wide = CGSize(width: 1328, height: 860)
    static let narrow = CGSize(width: 900, height: 860)
    static let sizes = [("1328", wide), ("900", narrow)]

    nonisolated static let claudeEvents = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                               "PostToolUse", "SubagentStart", "Stop", "SessionEnd"]

    /// Real files under a scratch home, so the detail page's ownership
    /// check and the editor read them like the app does.
    struct Fixture {
        let dir: TempDir
        let home: URL
        let hooks: [Hook]
        let editable: Hook
        let readOnly: Hook
        let folder: Hook
        let matcherRow: Hook
        let noMatcherRow: Hook
        let findings: [HookFinding]
        let report: HookStatsReport

        init() throws {
            let dir = try TempDir()
            let home = dir.url
            self.dir = dir
            self.home = home
            let fm = FileManager.default
            let hub = home.appending(path: ".swe/hooks")
            func write(_ path: String, _ text: String) throws -> String {
                let url = home.appending(path: path)
                try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
                try text.write(to: url, atomically: true, encoding: .utf8)
                return url.path
            }
            func hook(_ script: String, source: String, harness: String = "claude",
                      tier: Hook.Tier = .harness, state: LinkState = .linked,
                      _ declared: [(String, String)], detail: String = "", summary: String = "") -> Hook {
                Hook(script: script, harness: harness, source: source,
                     destination: home.appending(path: ".\(harness)/hooks/\(script)").path,
                     state: state, detail: detail,
                     declared: declared.map {
                         HookDeclaration(event: $0.0, matcher: $0.1, timeout: 10,
                                         command: "python3 ~/.\(harness)/hooks/\(script)",
                                         settings: home.appending(path: ".\(harness)/settings.json").path)
                     },
                     tier: tier, enabled: true, eventsSupported: HooksPageLayoutTests.claudeEvents,
                     summary: summary)
            }
            let script = """
                #!/usr/bin/env python3
                \"\"\"Stop hook: block replies that break the house style rules.\"\"\"
                import json, sys

                def main() -> int:
                    event = json.load(sys.stdin)
                    text = event.get("last_assistant_message", "")
                    if "\\u2014" in text:
                        print("No em dashes, please.", file=sys.stderr)
                        return 2
                    return 0

                if __name__ == "__main__":
                    sys.exit(main())

                """
            let prose = try write(".swe/hooks/claude/prose-guard.py", script)
            let reaper = try write(".swe/hooks/claude/reaper-daily.py", "#!/usr/bin/env python3\n")
            let sleep = try write(".swe/hooks/claude/no-sleep-poll.py", "#!/usr/bin/env python3\n")
            let draft = try write(".swe/hooks/claude/draft-check.py", "#!/usr/bin/env python3\n")
            let herdr = try write(".herdr/hooks/herdr-agent-state.sh", "#!/bin/sh\n")
            let notify = try write(".swe/hooks/codex/notify.py", "#!/usr/bin/env python3\n")
            let guardScript = try write("dotfiles/hooks/force-push-guard.sh",
                                        "#!/bin/sh\n# Block git push --force.\nexit 0\n")
            try fm.createDirectory(at: hub.appending(path: "claude/session-bundle"), withIntermediateDirectories: true)
            _ = try write(".swe/hooks/claude/session-bundle/run.sh", "#!/bin/sh\n")
            let bundle = hub.appending(path: "claude/session-bundle").path

            noMatcherRow = hook("prose-guard.py", source: prose, [("Stop", "")],
                                summary: "Stop hook: block replies that break the house style rules.")
            matcherRow = hook("no-sleep-poll.py", source: sleep, [("PreToolUse", "Bash")],
                              summary: "PreToolUse hook (Bash): block synchronous sleep-polling of PR and CI state.")
            editable = noMatcherRow
            readOnly = hook("force-push-guard.sh", source: guardScript, [("PreToolUse", "Bash")],
                            summary: "Block git push --force.")
            folder = hook("session-bundle", source: bundle, [("SessionStart", "")])
            hooks = [
                hook("reaper-daily.py", source: reaper, [("SessionStart", "")],
                     summary: "SessionStart hook: prune worktrees whose PR merged."),
                matcherRow,
                noMatcherRow,
                readOnly,
                folder,
                hook("herdr-agent-state.sh", source: herdr, tier: .foreign,
                     state: LinkState(rawValue: "foreign"), [("SessionStart", "*"), ("Stop", "")]),
                hook("notify.py", source: notify, harness: "codex", [("Stop", "")],
                     summary: "Stop hook: post a desktop notification."),
                hook("draft-check.py", source: draft, state: .create, [],
                     summary: "Warn when a draft PR has no description."),
            ]
            findings = [
                HookFinding(kind: "undeclared", harness: "claude",
                            message: "~/.claude/hooks/draft-check.py is linked but no settings file declares it"),
                HookFinding(kind: "managed", harness: "claude",
                            message: "~/.claude/settings.json \u{2014} declaration managed elsewhere"),
            ]
            report = HookStatsReport(window: "7d", hooks: [
                HookStatsRow(hook: "no-sleep-poll", sourcePaths: [sleep], fires: 12_480, blocks: 37, allows: 12_443,
                             lastBlock: Date().addingTimeInterval(-3 * 3600),
                             topReasons: [HookReason(decision: "block", reason: "sleep inside a PR wait loop", count: 30),
                                          HookReason(decision: "block", reason: "gh pr checks --watch in the foreground", count: 7)]),
                HookStatsRow(hook: "prose-guard", sourcePaths: [prose], fires: 420, blocks: 18, allows: 402,
                             lastBlock: Date().addingTimeInterval(-600),
                             topReasons: [HookReason(decision: "block", reason: "em dash in the reply", count: 11),
                                          HookReason(decision: "block", reason: "three or more arrows outside a code block", count: 4),
                                          HookReason(decision: "block", reason: "a paragraph over five sentences", count: 2),
                                          HookReason(decision: "block", reason: "a work report without Done and Follow-ups sections", count: 1)]),
                HookStatsRow(hook: "reaper-daily", sourcePaths: [reaper], fires: 7, allows: 7),
                HookStatsRow(hook: "notify", sourcePaths: [notify], fires: 3, allows: 3),
            ])
        }

        var inventory: HookInventory { HookInventory(hooks: hooks, findings: findings) }
        var counts: HookStatsState { .ready(HookStatsTable(report: report)) }
    }

    // MARK: Rendering

    /// Lays `content` out in an offscreen window the size of the page panel
    /// in a window of `window`, inside the real `Page` scaffold, and writes
    /// a PNG when `SWE_HOOKS_SNAPSHOT_DIR` is set.
    private func render(_ name: String, window: CGSize, detail: Bool = false,
                        recorder: HookColumnRecorder? = nil,
                        @ViewBuilder content: () -> some View) async throws {
        _ = NSApplication.shared
        let size = CGSize(width: window.width - SidebarWidth.standard, height: window.height)
        let page = Page(title: HooksPage.title, subtitle: HooksPage.subtitle,
                        back: detail ? (HooksPage.backHelp, {}) : nil, scrolls: !detail) {
            Button("New hook", systemImage: "plus") {}.primaryButton()
            Button("Refresh", systemImage: "arrow.clockwise") {}.iconButton()
        } content: {
            content()
        }
        let root = page
            .environment(\.hookColumnRecorder, recorder)
            .frame(width: size.width, height: size.height)
            .background(Theme.panel)
        let host = NSHostingView(rootView: root)
        host.appearance = NSAppearance(named: .aqua)
        let offscreen = OffscreenWindow(size: size, styleMask: [.borderless])
        offscreen.appearance = NSAppearance(named: .aqua)
        offscreen.contentView = host
        offscreen.orderBack(nil)
        defer { offscreen.orderOut(nil) }
        host.layoutSubtreeIfNeeded()
        // `Page` adds its content on the next main-queue turn, and the
        // detail's tasks land after that; let both run, then lay out again.
        await Task.yield()
        await Task.yield()
        host.layoutSubtreeIfNeeded()
        await Task.yield()
        host.layoutSubtreeIfNeeded()
        host.display()
        if let dir = ProcessInfo.processInfo.environment["SWE_HOOKS_SNAPSHOT_DIR"] {
            let rep = try #require(host.bitmapImageRepForCachingDisplay(in: host.bounds))
            host.cacheDisplay(in: host.bounds, to: rep)
            let png = try #require(rep.representation(using: .png, properties: [:]))
            let folder = URL(fileURLWithPath: dir)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try png.write(to: folder.appending(path: "\(name).png"))
        }
    }

    private func overview(_ fixture: Fixture, harness: String, foreign: Bool = true,
                          findingsOpen: Bool = false, quietOpen: Bool = false,
                          inventory: HookInventory? = nil) -> HooksOverview {
        HooksOverview(inventory: inventory ?? fixture.inventory, counts: fixture.counts,
                      harness: .constant(harness), showForeign: .constant(foreign),
                      statsWindow: .constant(.week), showFindings: .constant(findingsOpen),
                      quietEventsOpen: quietOpen, open: { _ in })
    }

    private func detail(_ fixture: Fixture, _ hook: Hook, drafts: PrimitiveDrafts) async -> HookDetailView {
        let checked = HookScript(source: hook.source, home: fixture.home)
        var directory: ObjCBool = false
        let folder = FileManager.default.fileExists(atPath: hook.source, isDirectory: &directory)
            && directory.boolValue
        if checked.exists && !folder { drafts.document(for: hook.source) }
        await drafts.settled()
        return HookDetailView(hook: hook, counts: fixture.counts, window: .week, home: fixture.home,
                              drafts: drafts, findings: fixture.findings, stats: HookStatsModel(),
                              navReselects: 0, info: { _ in nil }, reloadList: {},
                              loadRecent: { _, _ in }, onBack: {},
                              checked: .init(check: checked, isFolder: folder))
    }

    // MARK: Layout rules

    /// The regression this suite exists for: a row with no matcher once
    /// dropped its empty Matcher cell, so its numbers slid a column left,
    /// its separators missed the header's and its hover fill stopped short.
    @Test(arguments: [("claude", "one-harness"), ("", "all-harnesses")])
    func everyRowKeepsTheHeadersColumns(harness: String, label: String) async throws {
        let fixture = try Fixture()
        for (width, window) in Self.sizes {
            let recorder = HookColumnRecorder()
            try await render("\(label)-\(width)", window: window, recorder: recorder) {
                overview(fixture, harness: harness)
            }
            let table = HookTable(hooks: fixture.hooks, harness: harness.isEmpty ? nil : harness,
                                  includeForeign: true)
            let header = try #require(recorder.frames["header"], "no header frames at \(width)")
            let rows = table.rows.filter { $0.hook.source == fixture.matcherRow.source
                || $0.hook.source == fixture.noMatcherRow.source }
            #expect(rows.count == 2)
            let columns: [HookColumn] = harness.isEmpty
                ? [.script, .event, .matcher, .harness, .fires, .blocks, .rate]
                : [.script, .event, .matcher, .fires, .blocks, .rate]
            for row in rows {
                let cells = try #require(recorder.frames[row.id], "no frames for \(row.hook.script)")
                for column in columns {
                    let want = try #require(header[column], "no header \(column)")
                    let got = try #require(cells[column], "\(row.hook.script) has no \(column) cell")
                    #expect(abs(got.minX - want.minX) < 0.5 && abs(got.width - want.width) < 0.5,
                            "\(row.hook.script) \(column) at \(got.minX)+\(got.width), header \(want.minX)+\(want.width), \(width)")
                }
                let fill = try #require(cells[.row])
                let right = try #require(header[.rate]).maxX
                #expect(abs(fill.minX) < 0.5 && abs(fill.maxX - right) < 0.5,
                        "\(row.hook.script)'s row spans \(fill.minX)...\(fill.maxX), the table \(right), \(width)")
            }
            // Not-running rows span the table too.
            for row in table.rows where !row.isRunning {
                let fill = try #require(recorder.frames[row.id]?[.row])
                #expect(abs(fill.maxX - (header[.rate]?.maxX ?? 0)) < 0.5)
            }
            // The 900pt window fits the table without scrolling sideways:
            // the page pads 40 a side, the card 20.
            let inner = window.width - SidebarWidth.standard - 80 - 40
            #expect((header[.rate]?.maxX ?? .infinity) <= inner + 0.5,
                    "the table is \(header[.rate]?.maxX ?? 0) wide in \(inner) at \(width)")
        }
    }

    /// Every column name fits between its column's insets at every text
    /// zoom, so "Blocks" never reads "Bloc…".
    @Test func everyHeaderFitsItsColumnAtEveryZoom() {
        for zoom in TextZoom.steps {
            let scale: (CGFloat) -> CGFloat = { TextZoom.pointSize($0, at: zoom) }
            for all in [false, true] {
                let columns = HookColumns(available: 600, showsHarness: all, scale: scale)
                let font = AppFonts.nsFont(size: scale(12), weight: .medium)
                let inset = HookColumns.inset
                var cases: [(String, CGFloat)] = [
                    ("Event", columns.event - 2 * inset), ("Matcher", columns.matcher - 2 * inset),
                    ("Fires", columns.fires - 2 * inset), ("Blocks", columns.blocks - 2 * inset),
                    ("Rate", columns.rate - inset),
                ]
                if all {
                    let harnessInset = columns.compactHarness ? HookColumns.compactInset : inset
                    cases.append(("Harness", columns.harness - 2 * harnessInset))
                }
                for (title, room) in cases {
                    // The header's 0.12 tracking adds after every letter.
                    let width = (title as NSString).size(withAttributes: [.font: font]).width
                        + 0.12 * CGFloat(title.count)
                    #expect(width <= room, "\(title) needs \(width) in \(room) at zoom \(zoom)")
                }
            }
        }
    }

    // MARK: Snapshots of the other states

    @Test func overviewStatesRender() async throws {
        let fixture = try Fixture()
        let onlyForeign = HookInventory(hooks: fixture.hooks.filter { $0.tier == .foreign })
        for (width, window) in Self.sizes {
            try await render("check-open-\(width)", window: window) {
                overview(fixture, harness: "claude", findingsOpen: true)
            }
            try await render("quiet-events-open-\(width)", window: window) {
                overview(fixture, harness: "claude", quietOpen: true)
            }
            try await render("empty-table-\(width)", window: window) {
                overview(fixture, harness: "claude", foreign: false, inventory: onlyForeign)
            }
        }
        #expect(HooksOverview.emptyLine(HookTable(hooks: onlyForeign.hooks, harness: "claude", includeForeign: false))
                == "Only other tools' hooks are declared for Claude Code. Turn on Other tools to see them.")
    }

    @Test func detailStatesRender() async throws {
        let fixture = try Fixture()
        #expect(HookScript(source: fixture.editable.source, home: fixture.home).editable)
        #expect(!HookScript(source: fixture.readOnly.source, home: fixture.home).editable)
        let drafts = PrimitiveDrafts()
        for (width, window) in Self.sizes {
            let editable = await detail(fixture, fixture.editable, drafts: drafts)
            try await render("detail-\(width)", window: window, detail: true) { editable }
            try await render("detail-short-\(width)", window: CGSize(width: window.width, height: 520),
                             detail: true) { editable }
            let readOnly = await detail(fixture, fixture.readOnly, drafts: drafts)
            try await render("detail-read-only-\(width)", window: window, detail: true) { readOnly }
            let folder = await detail(fixture, fixture.folder, drafts: drafts)
            try await render("detail-folder-\(width)", window: window, detail: true) { folder }
        }
        #expect(drafts.documents[fixture.editable.source]?.state == .ready)
    }
}
