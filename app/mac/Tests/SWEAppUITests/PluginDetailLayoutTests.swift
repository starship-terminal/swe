import AppKit
import SWECore
import SwiftUI
import Testing
@testable import SWEApp

/// The plugin detail rendered offscreen inside the real `Page`, against
/// plugin folders in a temp home (never the real `~/.swe`), at the three
/// window sizes `DetailLayoutCheck` knows. The default state checks the
/// rail's width and the editor's height under both scroller styles. With
/// `SWE_PLUGIN_DETAIL_SNAPSHOT_DIR` set, every state is also written
/// there as `<state>-<window>.png` for a person to look at.
@Suite(.serialized) @MainActor struct PluginDetailLayoutTests {
    /// A home with two plugins in a hub marketplace under `~/.swe/plugins`
    /// (the user's own) and one in a GitHub marketplace's checkout
    /// (read-only).
    private final class Fixture {
        let temp: TempDir
        let home: URL

        init() throws {
            temp = try TempDir()
            // The page lists files under the plugin's resolved folder, so
            // the paths a test picks are built from the resolved home too.
            home = URL(fileURLWithPath: FolderOwners.realPath(temp.url.path))
            try write(".swe/plugins/kai-hub/release-tools/.claude-plugin/plugin.json", """
                {
                  "name": "release-tools",
                  "version": "1.2.0",
                  "description": "Draft release notes and ship a tagged build.",
                  "author": {"name": "Kai", "email": "kai@example.com"},
                  "homepage": "https://example.com/release-tools",
                  "license": "MIT",
                  "keywords": ["release", "notes"]
                }
                """)
            try write(".swe/plugins/kai-hub/release-tools/.codex-plugin/plugin.json",
                      "{\n  \"name\": \"release-tools\",\n  \"interface\": {\"displayName\": \"Release tools\"}\n}\n")
            try write(".swe/plugins/kai-hub/release-tools/.mcp.json",
                      "{\n  \"mcpServers\": {\"github\": {\"command\": \"gh-mcp\"}}\n}\n")
            try write(".swe/plugins/kai-hub/release-tools/README.md", """
                # Release tools

                Skills and commands for cutting a release.

                ## Skills

                - changelog: group merged pull requests by Added, Changed and Fixed
                - release-notes: write the notes for a tag

                ## Commands

                - /ship: tag, build and publish
                """)
            try write(".swe/plugins/kai-hub/release-tools/LICENSE", "MIT License\n")
            try write(".swe/plugins/kai-hub/release-tools/skills/changelog/SKILL.md", """
                ---
                name: changelog
                description: Group merged pull requests by Added, Changed and Fixed.
                ---

                # Changelog
                """)
            try write(".swe/plugins/kai-hub/release-tools/skills/changelog/scripts/collect.sh",
                      "#!/bin/sh\nset -eu\ngit log --merges --oneline\n")
            try write(".swe/plugins/kai-hub/release-tools/skills/release-notes/SKILL.md", """
                ---
                name: release-notes
                description: Write the notes for a tag.
                ---

                # Release notes
                """)
            try write(".swe/plugins/kai-hub/release-tools/commands/ship.md", "# Ship\n\nTag, build and publish.\n")
            try write(".swe/plugins/kai-hub/release-tools/agents/reviewer.md", """
                ---
                name: reviewer
                description: Reads the diff before a release goes out.
                ---

                # Reviewer
                """)
            try write(".swe/plugins/kai-hub/release-tools/hooks/hooks.json", "{\n  \"hooks\": {}\n}\n")
            try write(".swe/plugins/kai-hub/release-tools/assets/logo.png", Data([0x89, 0x50, 0x4E, 0x47, 0, 0, 0, 0x0D]))
            try write(".swe/plugins/kai-hub/notes/.claude-plugin/plugin.json", "{\n  \"name\": \"notes\"\n}\n")
            try write(".swe/plugins/kai-hub/notes/skills/daily/SKILL.md", """
                ---
                name: daily
                description: Start the day's note from yesterday's open items.
                ---

                # Daily note
                """)
            try write("checkouts/official/plugins/vendor-tool/.claude-plugin/plugin.json",
                      "{\n  \"name\": \"vendor-tool\",\n  \"version\": \"0.4.1\"\n}\n")
            try write("checkouts/official/plugins/vendor-tool/README.md",
                      "# Vendor tool\n\nA plugin a GitHub marketplace manages.\n")
            try write("checkouts/official/plugins/vendor-tool/commands/run.md", "# Run\n")
        }

        func path(_ path: String) -> String { home.appending(path: path).path }

        func write(_ path: String, _ text: String) throws { try write(path, Data(text.utf8)) }

        func write(_ path: String, _ data: Data) throws {
            let file = home.appending(path: path)
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            try data.write(to: file)
        }

        /// The user's own marketplace: a folder in the hub.
        var hub: PluginMarketplace {
            PluginMarketplace(name: "kai-hub", hub: true, path: path(".swe/plugins/kai-hub"),
                              harnesses: ["claude": .init(sourceType: "directory",
                                                          location: path(".swe/plugins/kai-hub"))],
                              plugins: [])
        }

        /// A marketplace a harness cloned from GitHub.
        var official: PluginMarketplace {
            PluginMarketplace(name: "official", path: path("checkouts/official"),
                              harnesses: ["claude": .init(sourceType: "github", location: "example/official")],
                              plugins: [])
        }

        func plugin(_ name: String, in market: PluginMarketplace, folder: String,
                    description: String? = nil, version: String? = nil) -> CatalogPlugin {
            CatalogPlugin(name: name, marketplace: market.name, description: description, version: version,
                          sourcePath: path(folder),
                          harnesses: ["claude": PluginHarnessState(available: true, installed: true, enabled: true),
                                      "codex": PluginHarnessState(available: true, installed: true)])
        }

        var releaseTools: CatalogPlugin {
            plugin("release-tools", in: hub, folder: ".swe/plugins/kai-hub/release-tools",
                   description: "Draft release notes and ship a tagged build.", version: "1.2.0")
        }
    }

    /// `swe plugins info`, in the shape swe emits: one hub server with a
    /// missing key, one declared-only server, and the call rollup.
    private static let info: PluginInfo? = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(PluginInfo.self, from: Data("""
            {"ref": "release-tools@kai-hub", "updated": "2026-11-19T09:30:00+00:00",
             "mcp_servers": [
               {"name": "github", "env_keys": ["GITHUB_TOKEN"], "header_keys": ["X-Api-Version"],
                "harnesses": ["claude"], "disabled_in": ["codex"], "in_hub": true,
                "secrets_missing": ["GITHUB_TOKEN"],
                "usage": {"tracked": true, "calls": 14, "sessions": 4}},
               {"name": "release-feed", "env_keys": ["FEED_URL"], "header_keys": [],
                "harnesses": [], "disabled_in": [], "in_hub": false, "secrets_missing": ["FEED_URL"]}],
             "skills": [{"name": "changelog", "harnesses": ["claude"],
                         "usage": {"tracked": true, "calls": 17, "sessions": 6}}],
             "usage": {"tracked": true, "calls": 31, "sessions": 9, "last_used": "2026-11-18T12:00:00Z",
                       "skill_calls": 17, "mcp_calls": 14}}
            """.utf8))
    }()

    /// Where the PNGs go; nil (the usual run) renders only the states the
    /// layout assertions need.
    nonisolated private static let snapshots = ProcessInfo.processInfo.environment["SWE_PLUGIN_DETAIL_SNAPSHOT_DIR"]

    private func check(_ state: String, wait: Set<String>, legacyScrollers: Bool = false,
                       prepare: (PrimitiveDrafts) -> Void = { _ in },
                       _ make: (PluginDetailProbe, PrimitiveDrafts) -> PluginDetailContent) async throws {
        try await DetailLayoutCheck.run(
            state, page: .init(title: "Plugins",
                               subtitle: "Switch installed plugins on or off in each harness. Import into SWE to own and edit their files.",
                               back: "Back to all plugins"),
            snapshots: Self.snapshots, wait: wait, legacyScrollers: legacyScrollers,
            prepare: prepare, make)
    }

    private func content(_ f: Fixture, _ plugin: CatalogPlugin, market: PluginMarketplace? = nil,
                         drafts: PrimitiveDrafts, selected: String? = nil, detailsOpen: Bool = false,
                         manifestShowsAll: Bool = false, expanded: Set<String> = [],
                         probe: PluginDetailProbe) -> PluginDetailContent {
        PluginDetailContent(plugin: plugin, harnesses: ["claude", "codex"], market: market ?? f.hub, home: f.home,
                            navReselects: 0, marketName: (market ?? f.hub).name, onBack: {}, onFilesChanged: {},
                            loadInfo: { _ in Self.info }, drafts: drafts, selected: selected,
                            detailsOpen: detailsOpen, manifestShowsAll: manifestShowsAll,
                            expanded: expanded, probe: probe)
    }

    /// The loads a detail of the user's own plugin waits for, with
    /// `selected` in the editor.
    private func loads(_ selected: String) -> Set<String> {
        ["folders", "files", "manifest", "info", "file:" + selected]
    }

    private static let root = ".swe/plugins/kai-hub/release-tools/"

    @Test func defaultOpensTheReadmeWithDetailsClosed() async throws {
        let f = try Fixture()
        #expect(Self.info != nil)
        // No file is handed in: the page picks the README itself, and the
        // wait below ends only once that file's check has landed.
        try await check("default", wait: loads(f.path(Self.root + "README.md")),
                        legacyScrollers: true) { probe, drafts in
            content(f, f.releaseTools, drafts: drafts, probe: probe)
        }
    }

    @Test func aFieldWritesTheDraftTheEditorShows() async throws {
        let f = try Fixture()
        let drafts = PrimitiveDrafts()
        let document = drafts.document(for: f.path(Self.root + ".claude-plugin/plugin.json"))
        await drafts.settled()
        var edits = 0
        var typed: String?
        let keywords = Binding(get: { typed }, set: { typed = $0 })
        let version = PluginDetailContent.field(.version, of: document, typedKeywords: keywords) { edits += 1 }
        #expect(version.wrappedValue == "1.2.0")
        version.wrappedValue = "1.3.0"
        #expect(document.text.contains(#""version": "1.3.0""#))
        #expect(document.isDirty && edits == 1)
        // The same value again changes nothing, so nothing asks for the
        // editor.
        version.wrappedValue = "1.3.0"
        #expect(edits == 1)

        // A trailing comma on the way to the next keyword stays in the
        // field, though the file's list has not changed yet.
        let field = PluginDetailContent.field(.keywords, of: document, typedKeywords: keywords) { edits += 1 }
        field.wrappedValue = "release, notes, "
        #expect(field.wrappedValue == "release, notes, ")
        #expect(edits == 1)
        field.wrappedValue = "release, notes, docs"
        #expect(PluginManifestForm.value(.keywords, in: document.text) == "release, notes, docs")
        #expect(edits == 2)

        // One document: a revert in the editor is what the fields show.
        await document.revert()
        #expect(version.wrappedValue == "1.2.0")
        #expect(field.wrappedValue == "release, notes")
        #expect(drafts.dirty.isEmpty)
    }

    @Test func theEditorNamesAFileUnderThePluginNotItsFolder() {
        // A cached copy's folder is named for its version.
        #expect(PluginDetailContent.fileSubtitle("/cache/composio/0.2.4/commands/connect.md",
                                                 under: "/cache/composio/0.2.4", plugin: "composio")
            == "composio/commands/connect.md")
        #expect(PluginDetailContent.fileSubtitle("/hub/eng/.claude-plugin/plugin.json", under: "/hub/eng/", plugin: "eng")
            == "eng/.claude-plugin/plugin.json")
        // A file outside the folder gets its full path instead.
        #expect(PluginDetailContent.fileSubtitle("/elsewhere/notes.md", under: "/hub/eng", plugin: "eng") == nil)
        #expect(PluginDetailContent.fileSubtitle("/hub/engine/notes.md", under: "/hub/eng", plugin: "eng") == nil)
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func detailsOpenWithServersAndAMissingKey() async throws {
        let f = try Fixture()
        try await check("details", wait: loads(f.path(Self.root + "README.md"))) { probe, drafts in
            content(f, f.releaseTools, drafts: drafts, detailsOpen: true, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func everyManifestFieldWithDirtyDrafts() async throws {
        let f = try Fixture()
        let readme = f.path(Self.root + "README.md")
        let manifest = f.path(Self.root + ".claude-plugin/plugin.json")
        try await check("manifest", wait: loads(readme), prepare: { drafts in
            drafts.documents[readme]?.text += "\nAn edit not saved yet.\n"
            if let document = drafts.documents[manifest] {
                document.text = PluginManifestForm.setting(.version, to: "next", in: document.text)
            }
            #expect(drafts.dirty.count == 2)
        }) { probe, drafts in
            content(f, f.releaseTools, drafts: drafts, manifestShowsAll: true, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func theManifestIsPicked() async throws {
        let f = try Fixture()
        let manifest = f.path(Self.root + ".claude-plugin/plugin.json")
        try await check("pluginjson", wait: loads(manifest)) { probe, drafts in
            content(f, f.releaseTools, drafts: drafts, selected: manifest, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aManifestBehindADotIsPicked() async throws {
        let f = try Fixture()
        let mcp = f.path(Self.root + ".mcp.json")
        try await check("mcpjson", wait: loads(mcp)) { probe, drafts in
            content(f, f.releaseTools, drafts: drafts, selected: mcp, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aNestedSkillFileIsPicked() async throws {
        let f = try Fixture()
        let script = f.path(Self.root + "skills/changelog/scripts/collect.sh")
        try await check("files", wait: loads(script)) { probe, drafts in
            content(f, f.releaseTools, drafts: drafts, selected: script,
                    expanded: [f.path(Self.root + "skills/changelog"),
                               f.path(Self.root + "skills/changelog/scripts"),
                               f.path(Self.root + "assets")],
                    probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aPluginWithNoReadmeOpensItsFirstSkill() async throws {
        let f = try Fixture()
        let plugin = f.plugin("notes", in: f.hub, folder: ".swe/plugins/kai-hub/notes")
        try await check("noreadme", wait: loads(f.path(".swe/plugins/kai-hub/notes/skills/daily/SKILL.md"))) { probe, drafts in
            content(f, plugin, drafts: drafts, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aReadOnlyPluginLocksItsFiles() async throws {
        let f = try Fixture()
        let plugin = f.plugin("vendor-tool", in: f.official, folder: "checkouts/official/plugins/vendor-tool",
                              description: "A plugin a GitHub marketplace manages.", version: "0.4.1")
        // Not the user's: no manifest fields, so no manifest check to wait for.
        try await check("readonly", wait: ["folders", "files", "info",
                                           "file:" + f.path("checkouts/official/plugins/vendor-tool/README.md")]) { probe, drafts in
            content(f, plugin, market: f.official, drafts: drafts, detailsOpen: true, probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aBinaryFileCannotPreview() async throws {
        let f = try Fixture()
        let logo = f.path(Self.root + "assets/logo.png")
        try await check("binary", wait: loads(logo)) { probe, drafts in
            content(f, f.releaseTools, drafts: drafts, selected: logo,
                    expanded: [f.path(Self.root + "assets")], probe: probe)
        }
    }

    @Test(.enabled(if: snapshots != nil, "Snapshot only: the layout assertions run in the default state"))
    func aBrokenManifestSaysWhereToFixIt() async throws {
        let f = try Fixture()
        let manifest = f.path(Self.root + ".claude-plugin/plugin.json")
        try await check("broken", wait: loads(manifest), prepare: { drafts in
            drafts.documents[manifest]?.text = "{\n  \"name\": \"release-tools\",\n"
        }) { probe, drafts in
            content(f, f.releaseTools, drafts: drafts, selected: manifest, probe: probe)
        }
    }
}
