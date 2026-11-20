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

    /// Where the PNGs go; nil (the usual run) renders only the states the
    /// layout assertions need.
    nonisolated private static let snapshots = ProcessInfo.processInfo.environment["SWE_SKILL_DETAIL_SNAPSHOT_DIR"]

    /// Renders `content` in the Skills page at each window size and checks
    /// the panes (`DetailLayoutCheck`, shared with the plugin detail).
    private func check(_ state: String, wait: Set<String>, legacyScrollers: Bool = false,
                       prepare: (PrimitiveDrafts) -> Void = { _ in },
                       _ make: (SkillDetailProbe, PrimitiveDrafts) -> SkillDetailContent) async throws {
        try await DetailLayoutCheck.run(
            state, page: .init(title: "Skills",
                               subtitle: "Every skill on this Mac outside a plugin, and which harnesses load it.",
                               back: "Back to all skills"),
            snapshots: Self.snapshots, wait: wait, legacyScrollers: legacyScrollers,
            prepare: prepare, make)
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
        try await check("default", wait: loads(f.url(".swe/skills/code-review/SKILL.md").path),
                        legacyScrollers: true) { probe, drafts in
            content(f, skill, drafts: drafts, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func detailsOpenWithALongDescription() async throws {
        let f = try Fixture()
        let skill = f.skill(".swe/skills/code-review", description: String(
            repeating: "Review a pull request for correctness, security and tests before it merges. ", count: 4))
        try await check("details", wait: loads(f.url(".swe/skills/code-review/SKILL.md").path)) { probe, drafts in
            content(f, skill, drafts: drafts, detailsOpen: true, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aBlockDescriptionShowsInTheField() async throws {
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

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aNestedFileIsPicked() async throws {
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

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aDirtyDraftShowsItsDots() async throws {
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

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aReadOnlySkillLocksItsFields() async throws {
        let f = try Fixture()
        let skill = f.skill("vendor/skills/vendor-skill", source: .catalog)
        try await check("readonly", wait: loads(f.url("vendor/skills/vendor-skill/SKILL.md").path)) { probe, drafts in
            content(f, skill, drafts: drafts, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aBinaryFileCannotPreview() async throws {
        let f = try Fixture()
        let skill = f.skill(".swe/skills/code-review")
        let logo = f.url(".swe/skills/code-review/assets/logo.png").path
        try await check("binary", wait: loads(logo)) { probe, drafts in
            content(f, skill, drafts: drafts, selected: logo,
                    expanded: [f.url(".swe/skills/code-review/assets").path], probe: probe)
        }
    }
}
