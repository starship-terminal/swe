import Foundation
import Testing
@testable import SWECore

@Suite struct PluginFileOutlineTests {
    /// A plugin folder with every group, a README and a dot folder.
    private func plugin(_ t: PrimitiveHome) throws -> URL {
        let root = try t.dir(".swe/plugins/hub/release-tools")
        try t.write(".swe/plugins/hub/release-tools/.claude-plugin/plugin.json", "{}")
        try t.write(".swe/plugins/hub/release-tools/README.md", "x")
        try t.write(".swe/plugins/hub/release-tools/LICENSE", "x")
        try t.write(".swe/plugins/hub/release-tools/docs/guide.md", "x")
        try t.write(".swe/plugins/hub/release-tools/skills/changelog/SKILL.md", "x")
        try t.write(".swe/plugins/hub/release-tools/skills/changelog/README.md", "x")
        try t.write(".swe/plugins/hub/release-tools/skills/changelog/scripts/run.sh", "x")
        try t.write(".swe/plugins/hub/release-tools/skills/release-notes/SKILL.md", "x")
        try t.write(".swe/plugins/hub/release-tools/commands/ship.md", "x")
        try t.write(".swe/plugins/hub/release-tools/commands/git/tag.md", "x")
        try t.write(".swe/plugins/hub/release-tools/agents/reviewer.md", "x")
        try t.write(".swe/plugins/hub/release-tools/hooks/hooks.json", "{}")
        return root
    }

    @Test func groupsByKindWithCountsAndNestedFiles() throws {
        let t = try PrimitiveHome()
        let root = try plugin(t)
        let outline = PluginFileOutline(tree: PrimitiveTree(root: root), root: root)
        #expect(outline.sections.map(\.title)
            == ["Skills · 2", "Commands · 2", "Agents · 1", "Hooks · 1", "Other files · 3"])

        let skills = try #require(outline.sections.first { $0.kind == .skills })
        #expect(skills.nodes.map(\.name) == ["changelog", "release-notes"])
        #expect(skills.nodes.filter(\.isFolder).count == 2)
        #expect(skills.nodes[0].path == root.appending(path: "skills/changelog").path)
        // A skill's SKILL.md leads its folder, then folders, then files.
        #expect(skills.nodes[0].children.map(\.name) == ["SKILL.md", "scripts", "README.md"])
        #expect(skills.nodes[0].children[1].children.map(\.path)
            == [root.appending(path: "skills/changelog/scripts/run.sh").path])

        let commands = try #require(outline.sections.first { $0.kind == .commands })
        #expect(commands.nodes.map(\.name) == ["git", "ship.md"])
        #expect(commands.nodes[0].children.map(\.name) == ["tag.md"])

        // Other files nest too, and the dot folder is never listed.
        let other = try #require(outline.sections.first { $0.kind == .other })
        #expect(other.nodes.map(\.name) == ["docs", "LICENSE", "README.md"])
        #expect(!outline.filePaths.contains { $0.contains(".claude-plugin") })
        #expect(outline.filePaths.count == 11)
    }

    @Test func theDefaultFileIsTheReadmeThenTheFirstSkillThenTheManifest() throws {
        let t = try PrimitiveHome()
        let root = try plugin(t)
        let manifest = root.appending(path: ".claude-plugin/plugin.json").path
        func outline() -> PluginFileOutline { PluginFileOutline(tree: PrimitiveTree(root: root), root: root) }

        #expect(outline().defaultFile(manifest: manifest) == root.appending(path: "README.md").path)
        // A skill's own README is not the plugin's.
        try FileManager.default.removeItem(at: root.appending(path: "README.md"))
        #expect(outline().defaultFile(manifest: manifest)
            == root.appending(path: "skills/changelog/SKILL.md").path)
        try FileManager.default.removeItem(at: root.appending(path: "skills"))
        #expect(outline().defaultFile(manifest: manifest) == manifest)
        // A plugin that isn't the user's has no manifest to offer: the
        // first file listed.
        #expect(outline().defaultFile(manifest: nil) == root.appending(path: "commands/git/tag.md").path)

        let empty = try t.dir(".swe/plugins/hub/empty")
        let none = PluginFileOutline(tree: PrimitiveTree(root: empty), root: empty)
        #expect(none.sections.isEmpty)
        #expect(none.defaultFile(manifest: nil) == nil)
    }

    @Test func aLowercaseReadmeCountsAndMarkdownWins() throws {
        let t = try PrimitiveHome()
        let root = try t.dir(".swe/plugins/hub/tool")
        try t.write(".swe/plugins/hub/tool/readme.txt", "x")
        try t.write(".swe/plugins/hub/tool/Readme.md", "x")
        let outline = PluginFileOutline(tree: PrimitiveTree(root: root), root: root)
        #expect(outline.defaultFile(manifest: nil) == root.appending(path: "Readme.md").path)
    }

    @Test func foldersContainingAFileOpenItsRow() throws {
        let t = try PrimitiveHome()
        let root = try plugin(t)
        let outline = PluginFileOutline(tree: PrimitiveTree(root: root), root: root)
        #expect(outline.folders(containing: root.appending(path: "skills/changelog/scripts/run.sh").path)
            == [root.appending(path: "skills/changelog").path,
                root.appending(path: "skills/changelog/scripts").path])
        #expect(outline.folders(containing: root.appending(path: "README.md").path).isEmpty)
        #expect(outline.folders(containing: root.appending(path: ".claude-plugin/plugin.json").path).isEmpty)
    }

    @Test func manifestsBehindADotLeadTheList() throws {
        let t = try PrimitiveHome()
        let root = try plugin(t)
        try t.write(".swe/plugins/hub/release-tools/.claude-plugin/marketplace.json", "{}")
        try t.write(".swe/plugins/hub/release-tools/.codex-plugin/plugin.json", "{}")
        try t.write(".swe/plugins/hub/release-tools/.codex-plugin/.hidden", "x")
        try t.write(".swe/plugins/hub/release-tools/.mcp.json", "{}")
        // Never manifests: other dotfiles, a dot folder that is no
        // harness's, and a folder named like a top-level JSON file.
        try t.write(".swe/plugins/hub/release-tools/.gitignore", "x")
        try t.write(".swe/plugins/hub/release-tools/.git/config", "x")
        try t.write(".swe/plugins/hub/release-tools/.cache/state.json", "{}")
        try t.dir(".swe/plugins/hub/release-tools/.folder.json")
        // Codex's folder linked from Cursor's: the same file, listed once.
        try FileManager.default.createSymbolicLink(
            at: root.appending(path: ".cursor-plugin"), withDestinationURL: root.appending(path: ".codex-plugin"))

        let manifests = PrimitiveTree.manifestEntries(root: root)
        #expect(manifests.map(\.name) == [".claude-plugin/marketplace.json", ".claude-plugin/plugin.json",
                                          ".codex-plugin/plugin.json", ".mcp.json"])
        #expect(manifests[1].path == root.appending(path: ".claude-plugin/plugin.json").path)
        #expect(manifests.filter(\.isFolder).isEmpty)

        let outline = PluginFileOutline(tree: PrimitiveTree(root: root), root: root, manifests: manifests)
        #expect(outline.sections.map(\.title)
            == ["Manifests · 4", "Skills · 2", "Commands · 2", "Agents · 1", "Hooks · 1", "Other files · 3"])
        #expect(outline.sections[0].nodes.map(\.name) == manifests.map(\.name))
        #expect(outline.filePaths.prefix(4) == manifests.map(\.path)[...])
        #expect(outline.folders(containing: manifests[2].path).isEmpty)
        // The README still opens first; with nothing else in the folder, a
        // manifest does, also for a plugin whose fields can't be edited.
        #expect(outline.defaultFile(manifest: nil) == root.appending(path: "README.md").path)
        let bare = try t.dir(".swe/plugins/hub/bare")
        try t.write(".swe/plugins/hub/bare/.claude-plugin/plugin.json", "{}")
        let only = PluginFileOutline(tree: PrimitiveTree(root: bare), root: bare,
                                     manifests: PrimitiveTree.manifestEntries(root: bare))
        #expect(only.sections.map(\.title) == ["Manifests · 1"])
        #expect(only.defaultFile(manifest: nil) == bare.appending(path: ".claude-plugin/plugin.json").path)
        // The tree itself still never lists a dotfile, so a skill's rail is as it was.
        #expect(PrimitiveTree(root: root).sections.map(\.kind) == [.skills, .commands, .agents, .hooks, .other])
        #expect(PrimitiveTree.manifestEntries(root: try t.dir(".swe/plugins/hub/empty")).isEmpty)
    }

    @Test func aFileAManifestLinksToIsListedOnce() throws {
        let t = try PrimitiveHome()
        let root = try plugin(t)
        let fm = FileManager.default
        // `.mcp.json` is a link to a file Other files lists, and Codex's
        // manifest a link to one inside a skill's folder.
        try t.write(".swe/plugins/hub/release-tools/config/mcp.json", "{}")
        try fm.createSymbolicLink(at: root.appending(path: ".mcp.json"),
                                  withDestinationURL: root.appending(path: "config/mcp.json"))
        try t.write(".swe/plugins/hub/release-tools/skills/changelog/codex.json", "{}")
        try fm.createDirectory(at: root.appending(path: ".codex-plugin"), withIntermediateDirectories: true)
        try fm.createSymbolicLink(at: root.appending(path: ".codex-plugin/plugin.json"),
                                  withDestinationURL: root.appending(path: "skills/changelog/codex.json"))

        let tree = PrimitiveTree(root: root)
        let manifests = PrimitiveTree.manifestEntries(root: root)
        #expect(manifests.map(\.name) == [".claude-plugin/plugin.json", ".codex-plugin/plugin.json", ".mcp.json"])
        // Listed as they are, the two files would each have two rows.
        let twice = PluginFileOutline(tree: tree, root: root, manifests: manifests)
        #expect(twice.filePaths.contains(root.appending(path: "config/mcp.json").path))

        let outline = PluginFileOutline(tree: tree.dropping(filesSharedWith: manifests), root: root,
                                        manifests: manifests)
        // The manifests keep their rows; the files they link to go.
        #expect(outline.sections[0].nodes.map(\.name) == manifests.map(\.name))
        #expect(!outline.filePaths.contains(root.appending(path: "config/mcp.json").path))
        #expect(!outline.filePaths.contains(root.appending(path: "skills/changelog/codex.json").path))
        #expect(Set(outline.filePaths.map { FolderOwners.realPath($0) }).count == outline.filePaths.count)
        // The skill keeps its folder and its other files; `config` held
        // only the linked file, so it leaves Other files.
        let skills = try #require(outline.sections.first { $0.kind == .skills })
        #expect(skills.count == 2)
        #expect(skills.nodes[0].children.map(\.name) == ["SKILL.md", "scripts", "README.md"])
        #expect(outline.sections.first { $0.kind == .other }?.nodes.map(\.name) == ["docs", "LICENSE", "README.md"])
        #expect(outline.sections.first { $0.kind == .other }?.count == 3)

        // No manifests, nothing to resolve or drop.
        #expect(tree.dropping(filesSharedWith: []) == tree)
        // A group whose only file a manifest links to goes with it.
        let bare = try t.dir(".swe/plugins/hub/bare")
        try t.write(".swe/plugins/hub/bare/mcp.json", "{}")
        try fm.createSymbolicLink(at: bare.appending(path: ".mcp.json"),
                                  withDestinationURL: bare.appending(path: "mcp.json"))
        let only = PrimitiveTree(root: bare).dropping(filesSharedWith: PrimitiveTree.manifestEntries(root: bare))
        #expect(only.sections.isEmpty)
    }

    @Test func theSkillOutlineStillLeadsWithItsOwnSkillMd() throws {
        // The two outlines share one builder: a plugin's skills lead with
        // SKILL.md one folder down, a skill's own folder at its top.
        let t = try PrimitiveHome()
        let root = try t.dir(".swe/skills/tdd")
        try t.write(".swe/skills/tdd/SKILL.md", "x")
        try t.write(".swe/skills/tdd/AAA.md", "x")
        try t.write(".swe/skills/tdd/scripts/SKILL.md", "x")
        try t.write(".swe/skills/tdd/scripts/AAA.md", "x")
        let outline = SkillFileOutline(tree: PrimitiveTree(root: root), root: root)
        #expect(outline.nodes.map(\.name) == ["SKILL.md", "scripts", "AAA.md"])
        #expect(outline.nodes[1].children.map(\.name) == ["AAA.md", "SKILL.md"])
    }
}

@Suite struct PluginManifestFormTests {
    private static let manifest = """
        {
          "name": "release-tools",
          "version": "1.2.0",
          "description": "Draft release notes and ship a tagged build.",
          "author": "Kai",
          "keywords": ["release", "notes"],
          "mcpServers": {"github": {"command": "gh-mcp"}}
        }
        """

    @Test func theFirstThreeFieldsShowBeforeTheFold() {
        #expect(PluginManifestForm.Field.allCases.map(\.label)
            == ["Description", "Version", "Author", "Email", "Author URL", "Homepage",
                "Repository", "License", "Keywords"])
        #expect(PluginManifestForm.foldLimit == 3)
    }

    @Test func readsEachFieldOutOfTheDraftText() {
        let text = Self.manifest
        #expect(PluginManifestForm.value(.description, in: text) == "Draft release notes and ship a tagged build.")
        #expect(PluginManifestForm.value(.version, in: text) == "1.2.0")
        #expect(PluginManifestForm.value(.authorName, in: text) == "Kai")
        #expect(PluginManifestForm.value(.keywords, in: text) == "release, notes")
        #expect(PluginManifestForm.value(.homepage, in: text) == "")
    }

    @Test func settingChangesOnlyThatFieldAndKeepsTheLayout() {
        let text = PluginManifestForm.setting(.version, to: "1.3.0", in: Self.manifest)
        #expect(text == Self.manifest.replacingOccurrences(of: "1.2.0", with: "1.3.0"))
        // A key the file lacks is added, everything else left as it was.
        let added = PluginManifestForm.setting(.license, to: "MIT", in: Self.manifest)
        #expect(PluginManifestForm.value(.license, in: added) == "MIT")
        #expect(added.contains(#""mcpServers": {"github": {"command": "gh-mcp"}}"#))
        #expect(PluginManifestForm.value(.version, in: added) == "1.2.0")
        // An email turns a string author into an object that keeps the name.
        let author = PluginManifestForm.setting(.authorEmail, to: "kai@example.com", in: Self.manifest)
        #expect(PluginManifestForm.value(.authorName, in: author) == "Kai")
        #expect(PluginManifestForm.value(.authorEmail, in: author) == "kai@example.com")
    }

    /// The fields are bound straight to the draft's text, so whatever is
    /// typed must read back as typed or the field jumps under the cursor.
    @Test func everyTypedValueReadsBack() {
        let typed = ["", " ", "a", "two words ", "quote \" and \\ slash", "tab\tand\nnewline",
                     "https://example.com/a?b=c", "émoji 🚀", "{\"json\": true}", "1.2.3-beta+4"]
        for field in PluginManifestForm.Field.allCases where field != .keywords {
            var text = Self.manifest
            for value in typed {
                text = PluginManifestForm.setting(field, to: value, in: text)
                #expect(PluginManifestForm.value(field, in: text) == value, "\(field) \(value.debugDescription)")
                #expect(PluginManifestForm.isEditable(text))
            }
        }
    }

    @Test func keywordsKeepWhatIsTypedWhileItNamesTheSameList() {
        var text = Self.manifest
        // Each keystroke on the way to "release, notes, docs".
        for typed in ["release, notes,", "release, notes, ", "release, notes, d", "release, notes, docs"] {
            text = PluginManifestForm.setting(.keywords, to: typed, in: text)
            #expect(PluginManifestForm.keywords(typed: typed, in: text) == typed)
        }
        #expect(PluginManifestForm.value(.keywords, in: text) == "release, notes, docs")
        // The draft moved on without the field (a revert, an edit in the
        // editor): the file's keywords show, not the stale typing.
        #expect(PluginManifestForm.keywords(typed: "release, notes, docs", in: Self.manifest) == "release, notes")
        #expect(PluginManifestForm.keywords(typed: nil, in: Self.manifest) == "release, notes")
    }

    @Test func aDraftThatIsNotAnObjectIsLeftAlone() {
        for broken in ["", "{", "[1, 2]", "\"text\"", "{\"version\": }"] {
            #expect(!PluginManifestForm.isEditable(broken))
            #expect(PluginManifestForm.setting(.version, to: "1.0.0", in: broken) == broken)
            #expect(PluginManifestForm.value(.version, in: broken) == "")
        }
        #expect(PluginManifestForm.isEditable("{}"))
        #expect(PluginManifestForm.value(.version, in: PluginManifestForm.setting(.version, to: "1.0.0", in: "{}")) == "1.0.0")
    }

    @Test func eachFieldSaysWhatAHarnessMayReject() {
        for field in PluginManifestForm.Field.allCases {
            #expect(PluginManifestForm.problem(field, in: Self.manifest) == nil)
        }
        var text = Self.manifest
        for field in [PluginManifestForm.Field.version, .homepage, .repository, .authorURL] {
            text = PluginManifestForm.setting(field, to: "next up", in: text)
        }
        #expect(PluginManifestForm.problem(.version, in: text) == "Version should look like 1.2.3.")
        #expect(PluginManifestForm.problem(.homepage, in: text) == "Homepage is not a URL.")
        #expect(PluginManifestForm.problem(.authorURL, in: text) == "Author URL is not a URL.")
        // The same checks the manifest's own list makes.
        let all = PluginManifestForm.Field.allCases.compactMap { PluginManifestForm.problem($0, in: text) }
        #expect(Set(all) == Set(PluginManifestFields(parsing: text).problems))
        #expect(all.count == 4)
        // Any other text is the user's to write.
        #expect(PluginManifestForm.problem(.license, in: PluginManifestForm.setting(.license, to: "next up", in: text)) == nil)
    }
}
