import AppKit
import SWECore
import SwiftUI
import Testing
@testable import SWEApp

/// The skill detail rendered offscreen inside the real `Page`, against
/// skill folders in a temp home (never the real `~/.swe`), at a roomy
/// window, at the 900pt minimum, and at the minimum with the sidebar at
/// its widest (the narrowest page the app makes, where the panes stack).
/// Each state checks the rail keeps its width and the editor fills the
/// height, or, stacked, that both take the page's width and the editor
/// keeps its least height. With `SWE_SKILL_DETAIL_SNAPSHOT_DIR` set, each
/// state is also written there as `<state>-<window>.png` for a person to
/// look at.
@Suite(.serialized) @MainActor struct SkillDetailLayoutTests {
    private struct Window {
        var name: String
        var size: CGSize
        /// The app's sidebar: the page gets the rest of the window.
        var sidebar: CGFloat
    }

    private static let windows = [
        Window(name: "1328", size: CGSize(width: 1328, height: 860), sidebar: SidebarWidth.standard),
        Window(name: "900", size: CGSize(width: 900, height: 860), sidebar: SidebarWidth.standard),
        Window(name: "narrowest", size: CGSize(width: 900, height: 600), sidebar: SidebarWidth.range.upperBound),
    ]

    /// A home with two skills under `~/.swe/skills` (the user's own) and
    /// one outside it (read-only).
    private final class Fixture {
        let temp: TempDir
        let home: URL

        init() throws {
            temp = try TempDir()
            home = temp.url.resolvingSymlinksInPath()
            try write(".swe/skills/code-review/SKILL.md", """
                ---
                name: code-review
                description: Review a pull request for correctness, security and tests before it merges.
                license: MIT
                metadata:
                  author: kai
                  tags: [review]
                ---

                # Code review

                Read the diff top to bottom before commenting.

                1. Check the tests run.
                2. Look for missing error handling.
                3. Leave one comment per concern.
                """)
            try write(".swe/skills/code-review/README.md", "# Notes\n\nKept for people, not agents.\n")
            try write(".swe/skills/code-review/scripts/run.sh", "#!/bin/sh\nset -eu\necho review\n")
            try write(".swe/skills/code-review/scripts/lib/util.sh", "util() { :; }\n")
            try write(".swe/skills/code-review/references/checklist.md", "- [ ] tests\n- [ ] docs\n")
            try write(".swe/skills/code-review/assets/logo.png", Data([0x89, 0x50, 0x4E, 0x47, 0, 0, 0, 0x0D]))
            try write(".swe/skills/release-notes/SKILL.md", """
                ---
                name: release-notes
                description: |
                  Use this skill when the user asks for release notes or a changelog entry.
                  Group changes by Added, Changed and Fixed, newest first.
                ---

                # Release notes
                """)
            try write("vendor/skills/vendor-skill/SKILL.md", """
                ---
                name: vendor-skill
                description: A skill a checkout manages, so the app shows it read-only.
                ---

                # Vendor skill
                """)
        }

        func url(_ path: String) -> URL { home.appending(path: path) }

        func write(_ path: String, _ text: String) throws { try write(path, Data(text.utf8)) }

        func write(_ path: String, _ data: Data) throws {
            let file = url(path)
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            try data.write(to: file)
        }

        func skill(_ path: String, description: String = "", source: Skill.Source = .shared) -> Skill {
            Skill(name: URL(fileURLWithPath: path).lastPathComponent, description: description,
                  path: url(path).path, root: "shared", source: source, harnesses: ["claude", "codex"])
        }
    }

    private static let links = AnyView(VStack(alignment: .leading, spacing: 8) {
        Text("In the shared library. Every harness whose folder is linked loads it.")
            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
        FlowLayout(spacing: 6, lineSpacing: 6) {
            Badge("Claude Code · Linked")
            Badge("Codex · Linked")
        }
    })
    private static let usage = AnyView(
        Text("12 calls across 5 sessions · last used 2026-11-18")
            .font(Theme.caption).foregroundStyle(Theme.textSecondary))

    /// Lays `host` out and hands the main actor back until `done` holds,
    /// bounded by `gateTimeout`. A test on the main actor can't spin the
    /// run loop for this: it runs inside a main-queue job, so the view's
    /// tasks and `Page`'s first-frame hop, which need the main queue,
    /// would wait behind it.
    private func settle(_ host: NSView, until done: () -> Bool) async -> Bool {
        let deadline = ContinuousClock.now + gateTimeout
        while ContinuousClock.now < deadline {
            host.layoutSubtreeIfNeeded()
            if done() { return true }
            await Task.yield()
        }
        return done()
    }

    /// Renders `content` in the Skills page at each window size, waits for
    /// its loads, runs `prepare`, then checks the panes and writes a PNG.
    private func check(_ state: String, wait: Set<String>,
                       prepare: (PrimitiveDrafts) -> Void = { _ in },
                       _ make: (SkillDetailProbe, PrimitiveDrafts) -> SkillDetailContent) async throws {
        _ = NSApplication.shared
        for window in Self.windows {
            let probe = SkillDetailProbe()
            let drafts = PrimitiveDrafts()
            let panel = CGSize(width: window.size.width - window.sidebar, height: window.size.height)
            let page = Page(title: "Skills",
                            subtitle: "Every skill on this Mac outside a plugin, and which harnesses load it.",
                            back: ("Back to all skills", {}), scrolls: false) { make(probe, drafts) }
                .frame(width: panel.width, height: panel.height)
                .background(Theme.panel)
            let host = NSHostingView(rootView: page)
            // Titled and ordered in, so SwiftUI runs the view's tasks; the
            // window still never reaches a screen.
            let shown = OffscreenWindow(size: panel, styleMask: [.titled])
            shown.appearance = NSAppearance(named: .aqua)
            shown.contentView = host
            shown.orderFront(nil)
            defer { shown.orderOut(nil) }
            #expect(await settle(host) {
                wait.isSubset(of: probe.loaded)
                    && drafts.documents.values.allSatisfy { $0.state != .loading }
                    && probe.pane.height > 0
            }, "\(state) at \(window.name): \(probe.loaded)")
            prepare(drafts)
            // A layout pass, a turn of the main actor and another layout
            // for what `prepare` changed.
            host.layoutSubtreeIfNeeded()
            await Task.yield()
            host.layoutSubtreeIfNeeded()

            // Nothing is wider than the page, and the editor never drops
            // under 380pt beside the rail.
            let content = panel.width - 80
            let here = "\(state) at \(window.name)"
            #expect(probe.rail.minX >= 40 - 1 && probe.rail.maxX <= panel.width - 40 + 1, "\(here): rail \(probe.rail)")
            #expect(abs(probe.pane.maxX - (panel.width - 40)) < 1, "\(here): pane \(probe.pane)")
            if SkillDetailLayout.stacks(available: Double(content)) {
                #expect(abs(probe.rail.width - content) < 1, "\(here): rail \(probe.rail)")
                #expect(abs(probe.pane.width - content) < 1, "\(here): pane \(probe.pane)")
                #expect(probe.pane.height >= CGFloat(SkillDetailLayout.stackedEditorHeight) - 1, "\(here): pane \(probe.pane)")
                #expect(probe.pane.minY > probe.rail.maxY, "\(here): \(probe.rail) \(probe.pane)")
            } else {
                // The rail keeps its width and the editor fills the
                // page's height beside it.
                let rail = SkillDetailLayout.railWidth(available: Double(content))
                #expect(abs(probe.rail.width - CGFloat(rail)) < 1, "\(here): rail \(probe.rail)")
                #expect(probe.pane.width >= CGFloat(SkillDetailLayout.stackedBelow) - 1, "\(here): pane \(probe.pane)")
                #expect(abs(probe.rail.height - probe.pane.height) < 1, "\(here): \(probe.rail) \(probe.pane)")
                // Under the page's header, down to its bottom padding.
                // Global frames count from the window's top, title bar
                // included.
                let titleBar = shown.frame.height - panel.height
                #expect(abs(probe.pane.maxY - titleBar - (panel.height - 36)) < 1, "\(here): pane \(probe.pane)")
                #expect(probe.pane.height > panel.height * 0.7, "\(here): pane \(probe.pane)")
            }

            if let dir = ProcessInfo.processInfo.environment["SWE_SKILL_DETAIL_SNAPSHOT_DIR"],
               let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
                host.cacheDisplay(in: host.bounds, to: rep)
                let png = try #require(rep.representation(using: .png, properties: [:]))
                try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
                try png.write(to: URL(fileURLWithPath: dir).appending(path: "\(state)-\(window.name).png"))
            }
        }
    }

    private func content(_ f: Fixture, _ skill: Skill, drafts: PrimitiveDrafts, selected: String? = nil,
                         detailsOpen: Bool = false, expanded: Set<String> = [],
                         probe: SkillDetailProbe) -> SkillDetailContent {
        SkillDetailContent(skill: skill, home: f.home, navReselects: 0, onBack: {}, onFilesChanged: {},
                           links: Self.links, usage: Self.usage, drafts: drafts, selected: selected,
                           detailsOpen: detailsOpen, expanded: expanded, probe: probe)
    }

    private func loads(_ selected: String) -> Set<String> {
        ["folders", "files", "skill", "file:" + selected]
    }

    @Test func defaultOpensSkillMdWithDetailsClosed() async throws {
        let f = try Fixture()
        let skill = f.skill(".swe/skills/code-review")
        try await check("default", wait: loads(f.url(".swe/skills/code-review/SKILL.md").path)) { probe, drafts in
            content(f, skill, drafts: drafts, probe: probe)
        }
    }

    @Test func detailsOpenWithALongDescription() async throws {
        let f = try Fixture()
        let skill = f.skill(".swe/skills/code-review", description: String(
            repeating: "Review a pull request for correctness, security and tests before it merges. ", count: 4))
        try await check("details", wait: loads(f.url(".swe/skills/code-review/SKILL.md").path)) { probe, drafts in
            content(f, skill, drafts: drafts, detailsOpen: true, probe: probe)
        }
    }

    @Test func aBlockDescriptionShowsInTheField() async throws {
        let f = try Fixture()
        let skill = f.skill(".swe/skills/release-notes")
        let file = f.url(".swe/skills/release-notes/SKILL.md").path
        try await check("block", wait: loads(file), prepare: { drafts in
            let value = drafts.documents[file].map {
                SkillDetailContent.field("description", of: $0, name: "release-notes") {}.wrappedValue
            }
            #expect(value == "Use this skill when the user asks for release notes or a changelog entry.\n"
                    + "Group changes by Added, Changed and Fixed, newest first.\n")
        }) { probe, drafts in
            content(f, skill, drafts: drafts, probe: probe)
        }
    }

    @Test func aNestedFileIsPicked() async throws {
        let f = try Fixture()
        let skill = f.skill(".swe/skills/code-review")
        let run = f.url(".swe/skills/code-review/scripts/run.sh").path
        try await check("files", wait: loads(run)) { probe, drafts in
            content(f, skill, drafts: drafts, selected: run,
                    expanded: [f.url(".swe/skills/code-review/scripts").path,
                               f.url(".swe/skills/code-review/scripts/lib").path,
                               f.url(".swe/skills/code-review/references").path],
                    probe: probe)
        }
    }

    @Test func aDirtyDraftShowsItsDots() async throws {
        let f = try Fixture()
        let skill = f.skill(".swe/skills/code-review")
        let readme = f.url(".swe/skills/code-review/README.md").path
        let main = f.url(".swe/skills/code-review/SKILL.md").path
        try await check("dirty", wait: loads(readme), prepare: { drafts in
            drafts.documents[readme]?.text += "\nAn edit not saved yet.\n"
            if let document = drafts.documents[main] {
                document.text = SkillFrontmatterForm.setting("description", to: "Edited in the rail.",
                                                             in: document.text)
            }
            #expect(drafts.dirty.count == 2)
        }) { probe, drafts in
            content(f, skill, drafts: drafts, selected: readme, probe: probe)
        }
    }

    @Test func aReadOnlySkillLocksItsFields() async throws {
        let f = try Fixture()
        let skill = f.skill("vendor/skills/vendor-skill", source: .catalog)
        try await check("readonly", wait: loads(f.url("vendor/skills/vendor-skill/SKILL.md").path)) { probe, drafts in
            content(f, skill, drafts: drafts, probe: probe)
        }
    }

    @Test func aBinaryFileCannotPreview() async throws {
        let f = try Fixture()
        let skill = f.skill(".swe/skills/code-review")
        let logo = f.url(".swe/skills/code-review/assets/logo.png").path
        try await check("binary", wait: loads(logo)) { probe, drafts in
            content(f, skill, drafts: drafts, selected: logo,
                    expanded: [f.url(".swe/skills/code-review/assets").path], probe: probe)
        }
    }
}
