import Foundation
import Testing
@testable import SWECore

/// A home laid out like the real one: `~/.swe/plugins` for the hub.
final class PrimitiveHome {
    private let temp: TempDir
    let home: URL

    init() throws {
        temp = try TempDir()
        home = temp.url.resolvingSymlinksInPath()
        try FileManager.default.createDirectory(
            at: home.appending(path: ".swe/plugins"), withIntermediateDirectories: true)
    }

    @discardableResult
    func dir(_ path: String) throws -> URL {
        let url = home.appending(path: path)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    @discardableResult
    func write(_ path: String, _ text: String) throws -> URL {
        let file = home.appending(path: path)
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try Data(text.utf8).write(to: file)
        return file
    }
}

@Suite struct OwnershipTests {
    @Test func aDirectorySourceUnderSWEPluginsIsEditable() throws {
        let t = try PrimitiveHome()
        let market = try t.dir(".swe/plugins/development")
        let m = PluginMarketplace(name: "development", hub: true, path: market.path,
                                  harnesses: ["claude": .init(sourceType: "directory", location: market.path)],
                                  plugins: [])
        #expect(PrimitiveOwnership.marketplaceDirectory(m, home: t.home)?.path
            == FolderOwners.realPath(market.path))
        #expect(PrimitiveOwnership.isEditable(m, home: t.home))
    }

    @Test func aGitHubSourceIsNotEditable() throws {
        let t = try PrimitiveHome()
        let m = PluginMarketplace(name: "claude-plugins-official",
                                  path: t.home.appending(path: ".claude/plugins/marketplaces/official").path,
                                  harnesses: ["claude": .init(sourceType: "github",
                                                              location: "owner/repo")], plugins: [])
        #expect(!PrimitiveOwnership.isEditable(m, home: t.home))
    }

    @Test func aPluginInsideAnEditableMarketplaceIsEditable() throws {
        let t = try PrimitiveHome()
        try t.dir(".swe/plugins/development/eng")
        let m = PluginMarketplace(name: "development", hub: true,
                                  path: t.home.appending(path: ".swe/plugins/development").path,
                                  harnesses: [:], plugins: [])
        let plugin = CatalogPlugin(name: "eng", marketplace: "development",
                                   sourcePath:
                                    t.home.appending(path: ".swe/plugins/development/eng").path,
                                   harnesses: [:])
        #expect(PrimitiveOwnership.pluginDirectory(plugin, in: m, home: t.home) != nil)
    }

    @Test func aPluginWithoutALocalFolderIsNotEditable() throws {
        let t = try PrimitiveHome()
        try t.dir(".swe/plugins/development")
        let m = PluginMarketplace(name: "development", hub: true,
                                  path: t.home.appending(path: ".swe/plugins/development").path,
                                  harnesses: [:], plugins: [])
        let remote = CatalogPlugin(name: "p", marketplace: "development",
                                   sourcePath: nil, harnesses: [:])
        #expect(PrimitiveOwnership.pluginDirectory(remote, in: m, home: t.home) == nil)
        let outside = CatalogPlugin(name: "o", marketplace: "development",
                                    sourcePath: "/opt/elsewhere", harnesses: [:])
        #expect(PrimitiveOwnership.pluginDirectory(outside, in: m, home: t.home) == nil)
    }

    @Test func aSkillUnderSWEIsEditableASymlinkOutsideIsNot() throws {
        let t = try PrimitiveHome()
        try t.dir(".swe/skills/mine")
        try t.dir("elsewhere")
        try FileManager.default.createSymbolicLink(
            atPath: t.home.appending(path: ".swe/skills/linked").path,
            withDestinationPath: t.home.appending(path: "elsewhere").path)
        #expect(PrimitiveOwnership.isEditable(skillPath: t.home.appending(path: ".swe/skills/mine").path,
                                            home: t.home))
        #expect(!PrimitiveOwnership.isEditable(skillPath: t.home.appending(path: ".swe/skills/linked").path,
                                             home: t.home))
        #expect(!PrimitiveOwnership.isEditable(skillPath: t.home.appending(path: ".swe/cache/x").path,
                                             home: t.home))
        #expect(!PrimitiveOwnership.isEditable(skillPath: t.home.appending(path: ".claude/skills/theirs").path,
                                             home: t.home))
    }
}

/// The detail pages' folder facts, worked out off the main actor.
@Suite struct DetailFoldersTests {
    @Test func anEditablePluginListsItsOwnFolderAndManifest() throws {
        let t = try PrimitiveHome()
        try t.write(".swe/plugins/development/eng/.claude-plugin/plugin.json", "{}")
        let m = PluginMarketplace(name: "development", hub: true,
                                  path: t.home.appending(path: ".swe/plugins/development").path,
                                  harnesses: [:], plugins: [])
        let plugin = CatalogPlugin(name: "eng", marketplace: "development",
                                   sourcePath: t.home.appending(path: ".swe/plugins/development/eng").path,
                                   harnesses: [:])
        let folders = PluginFolders(plugin: plugin, market: m, home: t.home)
        #expect(folders.editable)
        #expect(folders.dir == folders.editableDir)
        #expect(folders.manifest?.path == FolderOwners.realPath(
            t.home.appending(path: ".swe/plugins/development/eng/.claude-plugin/plugin.json").path))
    }

    @Test func aReadOnlyPluginFallsBackToTheFirstCopyThatExists() throws {
        let t = try PrimitiveHome()
        let cached = try t.dir(".claude/plugins/cache/p")
        let m = PluginMarketplace(name: "official", path: "/nowhere",
                                  harnesses: ["claude": .init(sourceType: "github", location: "o/r")],
                                  plugins: [])
        let plugin = CatalogPlugin(name: "p", marketplace: "official", sourcePath: "/nowhere/p",
                                   harnesses: ["claude": .init(available: true, installed: true,
                                                               path: t.home.appending(path: "gone").path,
                                                               copies: [cached.path])])
        let folders = PluginFolders(plugin: plugin, market: m, home: t.home)
        #expect(!folders.editable)
        #expect(folders.dir?.path == cached.path)
        #expect(folders.manifest == nil)
        let missing = PluginFolders(plugin: plugin, market: m, home: t.home, exists: { _ in false })
        #expect(missing.dir == nil)
    }

    @Test func aSkillFileThatLinksOutsideItsFolderIsReadOnly() throws {
        let t = try PrimitiveHome()
        try t.write(".swe/skills/mine/SKILL.md", "---\nname: mine\n---\n")
        let outside = try t.write("elsewhere/SKILL.md", "---\nname: x\n---\n")
        try t.dir(".swe/skills/linked")
        try FileManager.default.createSymbolicLink(
            atPath: t.home.appending(path: ".swe/skills/linked/SKILL.md").path,
            withDestinationPath: outside.path)
        let mine = SkillFolders(skillPath: t.home.appending(path: ".swe/skills/mine").path, home: t.home)
        #expect(mine.editable && mine.skillFileEditable)
        #expect(mine.skillFile?.lastPathComponent == "SKILL.md")
        let linked = SkillFolders(skillPath: t.home.appending(path: ".swe/skills/linked").path, home: t.home)
        #expect(linked.editable && !linked.skillFileEditable)
        let empty = try t.dir(".swe/skills/empty")
        let none = SkillFolders(skillPath: empty.path, home: t.home)
        #expect(none.skillFile == nil && !none.skillFileEditable)
    }

    @Test func aFileSwappedForAnOutwardLinkChecksAsOutside() throws {
        let t = try PrimitiveHome()
        let root = try t.dir(".swe/plugins/development/eng")
        let file = try t.write(".swe/plugins/development/eng/commands/go.md", "go")
        let before = PrimitiveFileCheck(path: file.path, root: root)
        #expect(before.canPreview && before.isInside)
        // The tree saw a plain file; it becomes a link out before opening.
        let outside = try t.write("elsewhere/go.md", "away")
        try FileManager.default.removeItem(at: file)
        try FileManager.default.createSymbolicLink(atPath: file.path, withDestinationPath: outside.path)
        let after = PrimitiveFileCheck(path: file.path, root: root)
        #expect(after.canPreview && !after.isInside)
    }

    @Test func pathsInsideLeavesOutALinkThatPointsOutside() throws {
        let t = try PrimitiveHome()
        let root = try t.dir(".swe/plugins/development/eng")
        try t.write(".swe/plugins/development/eng/commands/go.md", "go")
        try t.write(".swe/plugins/development/eng/skills/tdd/SKILL.md", "tdd")
        let outside = try t.write("elsewhere/away.md", "away")
        try FileManager.default.createSymbolicLink(
            atPath: root.appending(path: "commands/away.md").path, withDestinationPath: outside.path)
        let inside = PrimitiveTree(root: root).pathsInside(root)
        #expect(inside.contains(root.appending(path: "commands/go.md").path))
        #expect(inside.contains(root.appending(path: "skills/tdd").path))
        #expect(inside.contains(root.appending(path: "skills/tdd/SKILL.md").path))
        #expect(!inside.contains(root.appending(path: "commands/away.md").path))
    }

    /// A skill can't work without its SKILL.md, so no row offers to trash
    /// one by itself; every other file, and the skill folder, keep theirs.
    @Test func aSkillsOwnSkillFileHasNoTrash() throws {
        let t = try PrimitiveHome()
        let skill = t.home.appending(path: "tdd")
        try t.write("tdd/SKILL.md", "---\nname: tdd\n---\n")
        try t.write("tdd/references/loop.md", "x")
        let lone = PrimitiveTree(root: skill).trashablePaths(skill)
        #expect(!lone.contains(skill.appending(path: "SKILL.md").path))
        #expect(lone.contains(skill.appending(path: "references/loop.md").path))

        // In a plugin: the skill folder and its other files stay
        // trashable, its top-level SKILL.md does not, and a SKILL.md
        // that is not a skill's (nested deeper, or a command) still is.
        let root = try t.dir("plugin")
        try t.write("plugin/skills/review/SKILL.md", "x")
        try t.write("plugin/skills/review/scripts/run.sh", "x")
        try t.write("plugin/skills/review/notes/SKILL.md", "x")
        try t.write("plugin/commands/SKILL.md", "x")
        let plugin = PrimitiveTree(root: root).trashablePaths(root)
        let review = root.appending(path: "skills/review")
        #expect(plugin.contains(review.path))
        #expect(!plugin.contains(review.appending(path: "SKILL.md").path))
        #expect(plugin.contains(review.appending(path: "scripts/run.sh").path))
        #expect(plugin.contains(review.appending(path: "notes/SKILL.md").path))
        #expect(plugin.contains(root.appending(path: "commands/SKILL.md").path))
    }
}

@Suite struct DisplayNamesTests {
    @Test func roundTripAndClear() throws {
        let t = try PrimitiveHome()
        let file = PluginDisplayNames.file(home: t.home)
        var names = PluginDisplayNames()
        names.set("My Marketplace", forMarketplace: "development")
        names.set("Engineering", forPlugin: "eng@development")
        try names.save(to: file)
        let loaded = PluginDisplayNames.load(from: file)
        #expect(loaded == names)
        #expect(loaded.name(forMarketplace: "development") == "My Marketplace")
        #expect(loaded.name(forPlugin: "eng@development") == "Engineering")
        var cleared = loaded
        cleared.set("", forPlugin: "eng@development")
        #expect(cleared.name(forPlugin: "eng@development") == nil)
        try cleared.save(to: file)
        #expect(PluginDisplayNames.load(from: file).plugins.isEmpty)
    }

    @Test func missingFileLoadsEmpty() throws {
        let t = try PrimitiveHome()
        #expect(PluginDisplayNames.load(from: PluginDisplayNames.file(home: t.home)) == PluginDisplayNames())
    }
}

@Suite struct JSONTextEditTests {
    /// A URL keeps its slashes bare, in a string and inside an object.
    @Test func slashesStayBare() throws {
        let text = "{\n  \"name\": \"eng\"\n}\n"
        var edited = try JSONTextEdit.set("https://example.com/a/b", at: ["homepage"], in: text)
        edited = try JSONTextEdit.set(["url": "https://github.com/o/r"], at: ["repository"], in: edited)
        #expect(edited.contains(#""homepage": "https://example.com/a/b""#))
        // A new object goes through JSONSerialization, whose spacing is
        // its own business: read the value back rather than pin the text.
        let json = try #require(try JSONSerialization.jsonObject(with: Data(edited.utf8)) as? [String: Any])
        let repository = try #require(json["repository"] as? [String: Any])
        #expect(repository["url"] as? String == "https://github.com/o/r")
        #expect(!edited.contains(#"\/"#))
    }

    @Test func replacesAValueKeepingOrderAndUnknownKeys() throws {
        let text = """
        {
          "name": "eng",
          "weird": {"kept": true},
          "version": "1.0.0"
        }
        """
        let edited = try JSONTextEdit.set("2.0.0", at: ["version"], in: text)
        #expect(edited.contains("\"name\": \"eng\""))
        #expect(edited.contains("\"weird\": {\"kept\": true}"))
        #expect(edited.contains("\"version\": \"2.0.0\""))
        // Unknown key still sits between name and version.
        let order = edited.range(of: "\"weird\"")!.lowerBound
        #expect(edited.range(of: "\"version\"")!.lowerBound > order)
    }

    @Test func insertsANewKeyAndCreatesParents() throws {
        let text = "{\n  \"name\": \"eng\"\n}\n"
        let edited = try JSONTextEdit.set("Shown", at: ["interface", "displayName"], in: text)
        #expect(edited.contains("\"name\": \"eng\""))
        #expect(edited.contains("\"interface\": {\"displayName\": \"Shown\"}"))
        let object = try JSONSerialization.jsonObject(with: Data(edited.utf8)) as? [String: Any]
        #expect((object?["interface"] as? [String: Any])?["displayName"] as? String == "Shown")
    }

    /// A new object or array lines up under its key like the rest of
    /// the file: its entries one level in, its closing bracket under
    /// the key, at the top level and one object down. (The space before
    /// a new object's colons is JSONSerialization's own.)
    @Test func aNewObjectIsIndentedUnderItsKey() throws {
        let text = "{\n  \"name\": \"eng\"\n}\n"
        let top = try JSONTextEdit.set(["url": "https://github.com/o/r"], at: ["repository"], in: text)
        #expect(top == """
            {
              "name": "eng",
              "repository": {
                "url" : "https://github.com/o/r"
              }
            }

            """)
        let nested = "{\n  \"author\": {\n    \"name\": \"dev\"\n  }\n}\n"
        let deep = try JSONTextEdit.set(["a", "b"], at: ["author", "tags"], in: nested)
        #expect(deep == """
            {
              "author": {
                "name": "dev",
                "tags": [
                  "a",
                  "b"
                ]
              }
            }

            """)
        // Into an empty object, written compactly: the value takes the
        // indent of the line the brace sits on.
        let empty = "{\n  \"author\": {}\n}\n"
        let compact = try JSONTextEdit.set(["a"], at: ["author", "tags"], in: empty)
        #expect(compact == """
            {
              "author": {"tags": [
                "a"
              ]}
            }

            """)
    }

    @Test func insertsIntoAnExistingObject() throws {
        let text = "{\n  \"author\": {\n    \"name\": \"dev\"\n  }\n}\n"
        let edited = try JSONTextEdit.set("k@x.dev", at: ["author", "email"], in: text)
        let object = try JSONSerialization.jsonObject(with: Data(edited.utf8)) as? [String: Any]
        let author = object?["author"] as? [String: Any]
        #expect(author?["name"] as? String == "dev")
        #expect(author?["email"] as? String == "k@x.dev")
    }
}

@Suite struct ManifestFieldsTests {
    @Test func applyPreservesUnknownKeysAndOrder() throws {
        let text = """
        {
          "name": "eng",
          "extra": [1, 2],
          "author": "old",
          "version": "1.0.0"
        }
        """
        var fields = PluginManifestFields(parsing: text)
        fields.version = "2.0.0"
        fields.authorName = "Dev"
        fields.authorEmail = "k@x.dev"
        let edited = try fields.apply(to: text)
        let parsed = PluginManifestFields(parsing: edited)
        #expect(parsed.version == "2.0.0")
        #expect(parsed.authorName == "Dev")
        #expect(parsed.authorEmail == "k@x.dev")
        // `extra` untouched and still ahead of `author` in the file.
        #expect(edited.contains("\"extra\": [1, 2]"))
        #expect(edited.range(of: "\"extra\"")!.lowerBound < edited.range(of: "\"author\"")!.lowerBound)
    }

    /// Only fields the form changed are written: no empty keys appear,
    /// and values the form can't show stay as they are.
    @Test func applyWritesOnlyTheFieldsThatChanged() throws {
        let text = """
        {
          "name": "eng",
          "repository": {"type": "git", "url": "https://github.com/o/r"},
          "author": "Sam",
          "keywords": ["a", 3]
        }
        """
        var fields = PluginManifestFields(parsing: text)
        #expect(try fields.apply(to: text) == text)
        fields.version = "1.0.0"
        let edited = try fields.apply(to: text)
        #expect(edited.contains(#""version": "1.0.0""#))
        #expect(edited.contains(#""repository": {"type": "git", "url": "https://github.com/o/r"}"#))
        #expect(edited.contains(#""author": "Sam""#))
        #expect(edited.contains(#""keywords": ["a", 3]"#))
        for key in ["description", "homepage", "license", "email", "url"] {
            #expect(!edited.contains("\"\(key)\": \"\""), "\(key)")
        }
        // A string author stays a string when only its name changes.
        fields = PluginManifestFields(parsing: text)
        fields.authorName = "Sam Lee"
        #expect(try fields.apply(to: text).contains(#""author": "Sam Lee""#))
        // A string author turning into an object keeps its unchanged name.
        fields = PluginManifestFields(parsing: text)
        fields.authorEmail = "k@x.dev"
        let object = try JSONSerialization.jsonObject(with: Data(try fields.apply(to: text).utf8)) as? [String: Any]
        let author = object?["author"] as? [String: Any]
        #expect(author?["name"] as? String == "Sam")
        #expect(author?["email"] as? String == "k@x.dev")
        #expect(author?["url"] == nil)
    }

    @Test func versionAndURLValidation() {
        #expect(PluginManifestFields.isVersion("1.2.3"))
        #expect(PluginManifestFields.isVersion("1.2.3-beta.1"))
        #expect(!PluginManifestFields.isVersion("v2"))
        #expect(PluginManifestFields.isURL("https://example.com/x"))
        #expect(!PluginManifestFields.isURL("not a url"))
        var fields = PluginManifestFields()
        fields.version = "abc"
        fields.homepage = "nope"
        #expect(fields.problems.count == 2)
    }

    @Test func codexDisplayNameKeepsOtherKeys() throws {
        let text = "{\n  \"name\": \"eng\",\n  \"interface\": {\n    \"category\": \"dev\"\n  }\n}\n"
        let edited = try PluginManifestFields.setCodexDisplayName("Engineering", in: text)
        let object = try JSONSerialization.jsonObject(with: Data(edited.utf8)) as? [String: Any]
        let interface = object?["interface"] as? [String: Any]
        #expect(interface?["displayName"] as? String == "Engineering")
        #expect(interface?["category"] as? String == "dev")
    }
}

@Suite struct FrontmatterTests {
    let skill = """
    ---
    name: tdd
    description: Red, green, refactor.
    license: MIT
    ---

    # Body

    Keep me.
    """

    @Test func editsAFieldKeepingBodyAndUnknownKeys() {
        let edited = MarkdownFrontmatter.update(skill, set: [(key: "description", value: "New words.")])
        #expect(edited.contains("description: New words."))
        #expect(edited.contains("name: tdd"))
        #expect(edited.contains("license: MIT"))
        #expect(edited.contains("# Body\n\nKeep me."))
    }

    @Test func addsAMissingKeyBeforeTheClose() {
        let edited = MarkdownFrontmatter.update(skill, set: [(key: "tags", value: "testing")])
        #expect(edited.contains("tags: testing\n---"))
    }

    @Test func aComplexFieldIsLeftAlone() {
        let text = "---\nname: x\nrefs:\n  - a\n  - b\n---\nbody\n"
        let front = MarkdownFrontmatter(text: text)
        #expect(front?.fields.map(\.key) == ["name"])
        #expect(front?.other == ["refs:", "  - a", "  - b"])
        // A complex field is not rewritten and never duplicated.
        let edited = MarkdownFrontmatter.update(text, set: [(key: "refs", value: "oops")])
        #expect(edited == text)
        #expect(edited.contains("refs:\n  - a\n  - b"))
    }

    @Test func createsFrontmatterWhenMissing() {
        let edited = MarkdownFrontmatter.update("# Hi\n", set: [(key: "name", value: "s")])
        #expect(edited.hasPrefix("---\nname: s\n---\n\n# Hi"))
    }

    /// A value YAML would read as something else is written quoted, and
    /// reads back as the text typed.
    @Test func aValueYAMLWouldMisreadIsQuoted() {
        let cases: [(typed: String, written: String)] = [
            ("Fix: the bug", #""Fix: the bug""#),
            ("#tag", ##""#tag""##),
            ("yes", #""yes""#),
            ("1.0", #""1.0""#),
            ("- item", #""- item""#),
            ("Plain words, fine.", "Plain words, fine."),
            ("bell\u{07}here", #""bell\u0007here""#),
        ]
        for (typed, written) in cases {
            let edited = MarkdownFrontmatter.update(skill, set: [(key: "description", value: typed)])
            #expect(edited.contains("description: \(written)\n"), "\(typed)")
            #expect(MarkdownFrontmatter(text: edited)?.fields.first { $0.key == "description" }?.value == typed)
        }
        // A value over several lines is a literal block instead, where
        // nothing needs quoting.
        let block = MarkdownFrontmatter.update(skill, set: [(key: "description", value: "say \"hi\"\nthen go")])
        #expect(block.contains("description: |-\n  say \"hi\"\n  then go\nlicense: MIT"))
    }

    /// A quoted value reads without its quotes, and saving the form
    /// unchanged leaves the line exactly as written.
    @Test func aQuotedValueReadsBareAndAnUnchangedOneStays() {
        let text = "---\nname: 'it''s'\ndescription: \"a: b\" # note\nkind: \"x\"\n---\nbody\n"
        let fields = MarkdownFrontmatter(text: text)?.fields.map { ($0.key, $0.value) } ?? []
        #expect(fields.map(\.0) == ["name", "description", "kind"])
        #expect(fields[0].1 == "it's")
        #expect(fields[1].1 == "a: b")
        #expect(fields[2].1 == "x")
        #expect(MarkdownFrontmatter.update(text, set: fields) == text)
        // A changed value keeps its comment, outside the quotes.
        let edited = MarkdownFrontmatter.update(text, set: [(key: "description", value: "a: c")])
        #expect(edited.contains("description: \"a: c\" # note\n"))
    }

    /// Every YAML escape reads as the text it stands for, a plain value
    /// drops its comment, and a value YAML can't read shows as written.
    @Test func yamlEscapesAndCommentsRead() {
        let cases: [(raw: String, value: String)] = [
            (#""\u00E9 \x41 \U0001F600 \uD83D\uDE00""#, "\u{E9} A \u{1F600} \u{1F600}"),
            (#""\e\a\b\v\f\N\_\L\P\ \/""#, "\u{1B}\u{07}\u{08}\u{0B}\u{0C}\u{85}\u{A0}\u{2028}\u{2029} /"),
            ("plain words # a note", "plain words"),
            ("#only a note", ""),
            ("a#b", "a#b"),
            (#""bad \q escape""#, #""bad \q escape""#),
            (#""unclosed"#, #""unclosed"#),
            (#""a" b"#, #""a" b"#),
            ("'it''s' # c", "it's"),
        ]
        for (raw, value) in cases {
            let text = "---\ndescription: \(raw)\n---\n"
            #expect(MarkdownFrontmatter(text: text)?.fields.first?.value == value, "\(raw)")
        }
        let plain = MarkdownFrontmatter.update("---\nkind: x # c\n---\n", set: [(key: "kind", value: "z")])
        #expect(plain == "---\nkind: z # c\n---\n")
    }
}

/// Block scalars and plain values over several lines: read as YAML reads
/// them (the expected values come from Ruby's Psych, which is libyaml),
/// left byte for byte when unchanged, and written back as a `|` block.
@Suite struct FrontmatterBlockTests {
    private func description(_ text: String) -> String? {
        MarkdownFrontmatter(text: text)?.fields.first { $0.key == "description" }?.value
    }

    private func frontmatter(_ inside: String) -> String { "---\n" + inside + "---\n\n# Body\n" }

    @Test func blockValuesReadAsYAMLReadsThem() {
        let cases: [(inside: String, value: String)] = [
            ("description: |\n  Line one.\n  Line two.\nname: x\n", "Line one.\nLine two.\n"),
            ("description: >\n  Fold these\n  into one.\n\n  New para.\nname: x\n", "Fold these into one.\nNew para.\n"),
            ("description: |-\n  a\n  b\n\nname: x\n", "a\nb"),
            ("description: |+\n  a\n  b\n\n\nname: x\n", "a\nb\n\n\n"),
            ("description: >+\n  a\n\n", "a\n\n"),
            ("description: >\n  a\n  b\n    code\n  c\n", "a b\n  code\nc\n"),
            ("description: |2\n    lead\n  b\n", "  lead\nb\n"),
            ("description: >-4\n      x\n    y\n", "  x\ny"),
            ("description: | # note\n  text\n", "text\n"),
            ("description: |\nname: x\n", ""),
            ("description: |\n  a\n    \n  b\n", "a\n  \nb\n"),
            ("description: |\n\n  a\n", "\na\n"),
            ("description: |\n  a\n  \tb\n", "a\n\tb\n"),
            ("description: >\n  a\n\n\n  b\n", "a\n\nb\n"),
            ("description: >\n  a\n   \n  b\n", "a\n \nb\n"),
        ]
        for (inside, value) in cases {
            #expect(description(frontmatter(inside)) == value, "\(inside)")
        }
    }

    @Test func plainValuesOverSeveralLinesFold() {
        #expect(description(frontmatter("description: Use this when\n  the user asks for\n\n  a review.\nname: x\n"))
                == "Use this when the user asks for\na review.")
        #expect(description(frontmatter("description:\n  starts below\n  and wraps\n")) == "starts below and wraps")
    }

    /// Lists, maps and anything else under a key stay where they are, and
    /// the key is reported locked rather than offered empty.
    @Test func nestedValuesStayInOther() {
        let inside = "name: x\ndescription: |\n  Words.\nmetadata:\n  author: me\n  tags: [a]\ntriggers:\n  - one\n  - two\nlist: a\n  - b\nquoted: \"a\n  b\"\n"
        let front = MarkdownFrontmatter(text: frontmatter(inside))
        #expect(front?.fields.map(\.key) == ["name", "description"])
        #expect(front?.fields[1].value == "Words.\n")
        #expect(front?.lockedKeys == ["metadata", "triggers", "list", "quoted"])
        #expect(front?.other == ["metadata:", "  author: me", "  tags: [a]", "triggers:", "  - one", "  - two",
                                 "list: a", "  - b", "quoted: \"a", "  b\""])
        // A block followed by an indented line it did not take is locked too.
        let bad = MarkdownFrontmatter(text: frontmatter("description: |\n    deep\n  shallow\n"))
        #expect(bad?.lockedKeys == ["description"])
    }

    /// Saving with nothing changed writes the file as it was, for every
    /// shape the form reads.
    @Test func anUnchangedValueKeepsTheFile() {
        let text = frontmatter("name: x # id\ndescription: >-\n  Fold\n  me.\n\n# a comment\nother: |+\n  kept\n\n\nplain: one\n  two\nmetadata:\n  a: b\n")
        let fields = MarkdownFrontmatter(text: text)?.fields.map { ($0.key, $0.value) } ?? []
        #expect(fields.map(\.0) == ["name", "description", "other", "plain"])
        #expect(MarkdownFrontmatter.update(text, set: fields) == text)
    }

    @Test func aChangedValueWithBreaksIsALiteralBlockAtItsIndent() {
        let text = frontmatter("name: x\ndescription: >\n    Old words\n    folded.\nmetadata:\n  a: b\n")
        let edited = MarkdownFrontmatter.update(text, set: [(key: "description", value: "New words.\nSecond line.\n")])
        #expect(edited == frontmatter("name: x\ndescription: |\n    New words.\n    Second line.\nmetadata:\n  a: b\n"))
        // A single-line field taking a break gets a new block, 2 spaces
        // in, keeping its comment.
        let single = MarkdownFrontmatter.update(frontmatter("description: one # c\n"),
                                                set: [(key: "description", value: "one\ntwo")])
        #expect(single == frontmatter("description: |- # c\n  one\n  two\n"))
    }

    @Test func aChangedValueWithoutBreaksGoesBackToOneLine() {
        let text = frontmatter("description: |\n  Was\n  long.\n\nname: x\n")
        let edited = MarkdownFrontmatter.update(text, set: [(key: "description", value: "Short: now")])
        #expect(edited == frontmatter("description: \"Short: now\"\n\nname: x\n"))
        let plain = MarkdownFrontmatter.update(frontmatter("description: a\n  b\nname: x\n"),
                                               set: [(key: "description", value: "c")])
        #expect(plain == frontmatter("description: c\nname: x\n"))
    }

    /// What the field shows after a write is exactly what was typed, so a
    /// text field bound through `update` never jumps under the cursor.
    @Test func everyTypedValueReadsBack() {
        let typed = ["a\nb", "a\nb\n", "a\nb\n\n\n", " lead\nb", "\nstarts empty", "\n lead", "  \nspaces first",
                     "tab\n\tin", "trailing \nspaces ", "a\n\n\nb", "\n\n", "\n", "a\r\nb", "a\n---\nb",
                     "bell\u{07}\nx", "sep\u{2028}\nx", "# not a comment\nreally", "key: value\n- item",
                     "plain", "", "Fix: it", "x",
                     "a\n ", "a\n   ", "a\n \n", "a\n\n ", " \n", "a\n\t", "a\n\n", "a\n\n\n\n", "  \n  \n"]
        let starts = [frontmatter("name: x\ndescription: old\n"),
                      frontmatter("name: x\ndescription: >\n    old\n"),
                      frontmatter("name: x\n"),
                      frontmatter("description: old\n\nlicense: MIT\n"),
                      frontmatter("description: old\n      \n\t\nlicense: MIT\n"),
                      frontmatter("description: |+\n  old\n\n\nlicense: MIT\n"),
                      frontmatter("description: old\n\n"),
                      "# no frontmatter\n"]
        for start in starts {
            for value in typed {
                let edited = MarkdownFrontmatter.update(start, set: [(key: "description", value: value)])
                let front = MarkdownFrontmatter(text: edited)
                #expect(front?.lockedKeys == [], "\(start.debugDescription) <- \(value.debugDescription)")
                #expect(description(edited) == value, "\(start.debugDescription) <- \(value.debugDescription)")
                if start.contains("license") {
                    #expect(front?.fields.last?.key == "license", "\(start.debugDescription) <- \(value.debugDescription)")
                }
                #expect(edited.hasSuffix(start.hasPrefix("---") ? "# Body\n" : "# no frontmatter\n"))
            }
        }
    }

    /// A `+` block keeps the empty lines after it, so the blank lines that
    /// already followed the field must not join the value (review on
    /// #1985: `old\n\nlicense` set to `a\n\n` read back `a\n\n\n`).
    @Test func blankLinesAfterTheFieldDoNotJoinAKeptBlock() {
        let text = frontmatter("description: old\n\nlicense: MIT\n")
        let edited = MarkdownFrontmatter.update(text, set: [(key: "description", value: "a\n\n")])
        #expect(description(edited) == "a\n\n")
        #expect(MarkdownFrontmatter(text: edited)?.fields.first { $0.key == "license" }?.value == "MIT")
        // Blank lines after a clip or strip block are not its value, so
        // they stay as the separator they were.
        let clipped = MarkdownFrontmatter.update(text, set: [(key: "description", value: "a\nb")])
        #expect(clipped == frontmatter("description: |-\n  a\n  b\n\nlicense: MIT\n"))
    }

    /// A last line of only spaces is content to the block but blank to a
    /// scan that ignores the block's indentation; the two disagreed, and
    /// the field turned locked while it was being typed (review on #1985).
    @Test func aLastLineOfSpacesKeepsTheFieldEditable() {
        let text = frontmatter("name: x\ndescription: old\nlicense: MIT\n")
        for value in ["a\n ", "a\n   ", "a\n \n", "a\n\n ", " \n", "a\n\t"] {
            let edited = MarkdownFrontmatter.update(text, set: [(key: "description", value: value)])
            let front = MarkdownFrontmatter(text: edited)
            #expect(front?.lockedKeys == [], "\(value.debugDescription)")
            #expect(description(edited) == value, "\(value.debugDescription)")
            #expect(SkillFrontmatterForm.rows(in: edited, name: "x").map(\.key) == ["name", "description", "license"])
        }
        // Read from a file, the same block reads with its last line.
        #expect(description(frontmatter("description: |-\n  a\n   \nname: x\n")) == "a\n ")
    }

    /// CRLF files read like LF ones, an unchanged save keeps every byte,
    /// and lines the form writes take the file's ending.
    @Test func crlfLinesKeepTheirEndings() {
        let text = "---\r\nname: x\r\ndescription: |\r\n  one\r\n  two\r\n---\r\nbody\r\n"
        let front = MarkdownFrontmatter(text: text)
        #expect(front?.fields.map(\.key) == ["name", "description"])
        #expect(front?.fields[1].value == "one\ntwo\n")
        let fields = front?.fields.map { ($0.key, $0.value) } ?? []
        #expect(MarkdownFrontmatter.update(text, set: fields) == text)
        let edited = MarkdownFrontmatter.update(text, set: [(key: "description", value: "three\nfour"),
                                                          (key: "license", value: "MIT")])
        #expect(edited == "---\r\nname: x\r\ndescription: |-\r\n  three\r\n  four\r\nlicense: MIT\r\n---\r\nbody\r\n")
    }
}

@Suite struct PrimitivePartsTests {
    @Test func nameValidation() {
        #expect(PrimitiveParts.isValidName("tdd"))
        #expect(PrimitiveParts.isValidName("code-review-2"))
        #expect(!PrimitiveParts.isValidName("Code Review"))
        #expect(!PrimitiveParts.isValidName("-lead"))
        #expect(!PrimitiveParts.isValidName("trail-"))
        #expect(!PrimitiveParts.isValidName("has space"))
    }

    @Test func createsASkillWithFrontmatter() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        let file = try PrimitiveParts.create(.skill, named: "tdd", in: root)
        #expect(file.lastPathComponent == "SKILL.md")
        let text = try String(contentsOf: file, encoding: .utf8)
        #expect(text.hasPrefix("---\nname: tdd\ndescription:"))
        #expect(throws: Error.self) { try PrimitiveParts.create(.skill, named: "tdd", in: root) }
    }

    @Test func createsCommandAndAgentFiles() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        #expect(try PrimitiveParts.create(.command, named: "go", in: root)
            .path.hasSuffix("commands/go.md"))
        let agent = try PrimitiveParts.create(.agent, named: "review", in: root)
        #expect(agent.path.hasSuffix("agents/review.md"))
        #expect(try String(contentsOf: agent, encoding: .utf8).contains("name: review"))
    }

    @Test func deleteGoesThroughTheTrashFunction() throws {
        let t = try PrimitiveHome()
        let file = try t.write("plugin/commands/x.md", "x")
        var trashed: [String] = []
        try PrimitiveParts.trash(file, inside: t.home.appending(path: "plugin")) {
            trashed.append($0.path)
        }
        #expect(trashed == [file.path])
        #expect(FileManager.default.fileExists(atPath: file.path))
    }
}

@Suite struct PrimitiveTreeTests {
    @Test func groupsHidesAndCounts() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        try t.write("plugin/skills/tdd/SKILL.md", "---\nname: tdd\n---\n")
        try t.write("plugin/skills/review/SKILL.md", "---\nname: review\n---\n")
        try t.write("plugin/commands/ship.md", "# Ship\n")
        try t.write("plugin/.claude-plugin/plugin.json", "{}")
        try t.write("plugin/.git/config", "x")
        try t.write("plugin/notes.md", "n")
        try t.write("plugin/.hidden", "x")
        let tree = PrimitiveTree(root: root)
        let kinds = tree.sections.map(\.kind)
        #expect(kinds == [.skills, .commands, .other])
        let skills = tree.sections.first { $0.kind == .skills }
        #expect(skills?.entries.map(\.name) == ["review", "tdd"])
        #expect(skills?.entries.first?.isFolder == true)
        #expect(skills?.entries.first?.previewPath.hasSuffix("SKILL.md") == true)
        let others = tree.sections.first { $0.kind == .other }?.entries.map(\.name)
        #expect(others == ["notes.md"])
        #expect(tree.sections.flatMap(\.entries).allSatisfy { !$0.name.hasPrefix(".") })
    }

    /// A skill folder's own SKILL.md is its main file, listed first, never
    /// one of its "Other files" (the web's `pages/skills.py` tree too).
    @Test func aSkillsOwnSkillFileIsItsMainFile() throws {
        let t = try PrimitiveHome()
        let root = t.home.appending(path: "tdd")
        try t.write("tdd/SKILL.md", "---\nname: tdd\n---\n")
        try t.write("tdd/references/loop.md", "x")
        let tree = PrimitiveTree(root: root)
        #expect(tree.sections.map(\.kind) == [.main, .other])
        #expect(tree.sections.first?.entries.map(\.name) == ["SKILL.md"])
        #expect(tree.sections.last?.entries.map(\.name) == ["references/loop.md"])

        // Alone in the folder, it is still the main file, with no Other.
        let lone = t.home.appending(path: "lone")
        try t.write("lone/SKILL.md", "x")
        #expect(PrimitiveTree(root: lone).sections.map(\.kind) == [.main])
    }

    @Test func binaryAndLargeFilesAreNotText() throws {
        let t = try PrimitiveHome()
        let png = try t.write("bin/img.png", "not a png")
        try Data([0x89, 0x50, 0x00, 0x01]).write(to: png)
        #expect(!PrimitiveTree.isTextFile(png))
        let big = t.home.appending(path: "bin/big.txt")
        try Data(count: 1_100_000).write(to: big)
        #expect(!PrimitiveTree.isTextFile(big))
        let md = try t.write("bin/readme.md", "# ok\n")
        #expect(PrimitiveTree.isTextFile(md))
    }

    @Test func theCapAppliesToALinksTargetAndAFifoIsNotText() throws {
        let t = try PrimitiveHome()
        let big = t.home.appending(path: "bin/big.txt")
        try FileManager.default.createDirectory(at: big.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try Data(repeating: 0x61, count: 1_100_000).write(to: big)
        let bigLink = t.home.appending(path: "bin/big-link.txt")
        try FileManager.default.createSymbolicLink(at: bigLink, withDestinationURL: big)
        #expect(!PrimitiveTree.isTextFile(bigLink))
        let md = try t.write("bin/readme.md", "# ok\n")
        let mdLink = t.home.appending(path: "bin/readme-link.md")
        try FileManager.default.createSymbolicLink(at: mdLink, withDestinationURL: md)
        #expect(PrimitiveTree.isTextFile(mdLink))
        let fifo = t.home.appending(path: "bin/pipe")
        try #require(mkfifo(fifo.path, 0o600) == 0)
        #expect(!PrimitiveTree.isTextFile(fifo))
    }

    /// The 8 KB sniff can cut a multi-byte character in two; the text is
    /// still text. A real bad byte before the cut still isn't.
    @Test func aCharacterSplitAtTheSniffLimitIsStillText() throws {
        let t = try PrimitiveHome()
        for (pad, character) in [(8191, "é"), (8190, "€"), (8191, "€"), (8189, "😀"), (8190, "😀"), (8191, "😀")] {
            let text = String(repeating: "a", count: pad) + character + " more text after the cut\n"
            let file = try t.write("notes/split-\(pad)-\(character.utf8.count).md", text)
            #expect(PrimitiveTree.isTextFile(file), "\(pad) + \(character)")
        }
        let bad = t.home.appending(path: "notes/bad.md")
        try (Data(repeating: 0x61, count: 8000) + Data([0xFF]) + Data(repeating: 0x61, count: 500)).write(to: bad)
        #expect(!PrimitiveTree.isTextFile(bad))
        // Bytes no character can start with, right at the cut, aren't
        // mistaken for a split character.
        for tail in [[0xFF], [0xC0], [0xF5], [0xE0, 0x80], [0xF4, 0x90], [0xED, 0xA0]] as [[UInt8]] {
            let file = t.home.appending(path: "notes/bad-\(tail.map { String($0, radix: 16) }.joined()).md")
            try (Data(repeating: 0x61, count: 8192 - tail.count) + Data(tail)
                 + Data(repeating: 0x61, count: 100)).write(to: file)
            #expect(!PrimitiveTree.isTextFile(file), "\(tail)")
        }
    }
}


@Suite struct JSONTextEditSpacingTests {
    @Test func spacesAroundTheColonDoNotCrash() throws {
        for gap in [" ", "\t", "\n", "  \n  "] {
            let text = "{\"name\"" + gap + ": \"eng\"}"
            let edited = try JSONTextEdit.set("x", at: ["other"], in: text)
            let object = try JSONSerialization.jsonObject(with: Data(edited.utf8)) as? [String: Any]
            #expect(object?["name"] as? String == "eng")
            #expect(object?["other"] as? String == "x")
        }
    }

    @Test func aThreeLevelPathSearchesTheCurrentObject() throws {
        let edited = try JSONTextEdit.set(1, at: ["a", "b", "c"], in: "{\"a\":{}}")
        let object = try JSONSerialization.jsonObject(with: Data(edited.utf8)) as? [String: Any]
        let a = object?["a"] as? [String: Any]
        let b = a?["b"] as? [String: Any]
        #expect(b?["c"] as? Int == 1)
    }

    @Test func aScalarBecomesAnObjectForDeeperPaths() throws {
        let edited = try JSONTextEdit.set("x", at: ["a", "b"], in: "{\"a\":1}")
        let object = try JSONSerialization.jsonObject(with: Data(edited.utf8)) as? [String: Any]
        #expect((object?["a"] as? [String: Any])?["b"] as? String == "x")
    }
}

@Suite struct FormDirtyTests {
    @Test func manifestDirtyTracksEditedFieldsNotTheFile() throws {
        let text = "{\n  \"name\": \"eng\",\n  \"version\": \"1.0.0\"\n}\n"
        let loaded = PluginManifestFields(parsing: text)
        #expect(loaded == PluginManifestFields(parsing: text))
        var fields = loaded
        // A field edit is dirty without document.text ever changing.
        fields.description = "A new line."
        #expect(fields != loaded)
        fields = loaded
        #expect(fields == loaded)
        // A saved form parses back to itself: save clears dirty too.
        fields.version = "2.0.0"
        #expect(PluginManifestFields(parsing: try fields.apply(to: text)) == fields)
    }

    @Test func frontmatterDirtyTracksEditedValues() {
        let text = "---\nname: x\ndescription: old\n---\nbody\n"
        let loaded: [(key: String, value: String)] =
            MarkdownFrontmatter(text: text)?.fields.map { ($0.key, $0.value) } ?? []
        var fields = loaded
        let same = { (a: [(key: String, value: String)], b: [(key: String, value: String)]) in
            a.elementsEqual(b) { $0.key == $1.key && $0.value == $1.value }
        }
        #expect(same(fields, loaded))
        fields[1].value = "new"
        #expect(!same(fields, loaded))
    }
}

@Suite struct SymlinkEscapeTests {
    @Test func aSymlinkedFileOutsideTheRootIsNotInside() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        let outside = try t.write("elsewhere/secret.md", "x")
        try FileManager.default.createSymbolicLink(
            atPath: root.appending(path: "linked.md").path,
            withDestinationPath: outside.path)
        try t.write("plugin/inside.md", "y")
        #expect(PrimitiveOwnership.isInside(root.appending(path: "inside.md").path, root: root.path))
        #expect(!PrimitiveOwnership.isInside(root.appending(path: "linked.md").path, root: root.path))
    }

    @Test func creatingThroughASymlinkedFolderIsRefused() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        let outside = try t.dir("elsewhere")
        try FileManager.default.createSymbolicLink(
            atPath: root.appending(path: "commands").path,
            withDestinationPath: outside.path)
        #expect(throws: Error.self) {
            try PrimitiveParts.create(.command, named: "go", in: root)
        }
        #expect(!FileManager.default.fileExists(atPath: outside.appending(path: "go.md").path))
    }

    /// A missing path ending in ".." used to loop forever; the missing
    /// rest of a path now lands where its ".." takes it.
    @Test func aMissingPathsDotsApplyOnTopOfItsExistingAncestor() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        let real = FolderOwners.trimmed(FolderOwners.realPath(root.path))
        let parent = (real as NSString).deletingLastPathComponent
        #expect(PrimitiveOwnership.resolvedAncestor(of: root.path + "/new/..") == real)
        #expect(PrimitiveOwnership.resolvedAncestor(of: root.path + "/a/b/../c") == real + "/a/c")
        #expect(PrimitiveOwnership.resolvedAncestor(of: root.path + "/new/../../out") == parent + "/out")
    }

    @Test func aNameThatIsntPlainIsRefused() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        for name in ["../../escape", "a/b", "..", ""] {
            #expect(throws: Error.self) { try PrimitiveParts.create(.command, named: name, in: root) }
        }
        #expect(!FileManager.default.fileExists(atPath: t.home.appending(path: "escape.md").path))
    }

    @Test func trashingASymlinkEscapeIsRefused() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        let outside = try t.write("elsewhere/keep.md", "x")
        let link = root.appending(path: "linked.md")
        try FileManager.default.createSymbolicLink(atPath: link.path, withDestinationPath: outside.path)
        var trashed: [String] = []
        #expect(throws: Error.self) {
            try PrimitiveParts.trash(link, inside: root) { trashed.append($0.path) }
        }
        #expect(trashed.isEmpty)
        #expect(FileManager.default.fileExists(atPath: outside.path))
    }
}

@Suite struct SkillFolderTreeTests {
    @Test func aSkillFolderListsAllItsFiles() throws {
        let t = try PrimitiveHome()
        let root = try t.dir("plugin")
        try t.write("plugin/skills/tdd/SKILL.md", "---\nname: tdd\n---\n")
        try t.write("plugin/skills/tdd/scripts/run.sh", "echo hi")
        try t.write("plugin/skills/tdd/references/api.md", "# api")
        try t.write("plugin/skills/tdd/references/deep/more.md", "x")
        let tree = PrimitiveTree(root: root)
        let skill = tree.sections.first { $0.kind == .skills }?.entries.first
        #expect(skill?.isFolder == true)
        #expect(skill?.children.map(\.name)
            == ["SKILL.md", "references/api.md", "references/deep/more.md", "scripts/run.sh"])
    }
}


@Suite struct DraftStoreTests {
    @MainActor @Test func trashDropsDraftsAtAndUnderThePath() {
        let drafts = PrimitiveDrafts()
        drafts.document(for: "/tmp/nope/file.md")
        drafts.document(for: "/tmp/nope/sub/deep.md")
        drafts.document(for: "/tmp/other.md")
        drafts.remove(under: "/tmp/nope")
        #expect(drafts.documents.keys.sorted() == ["/tmp/other.md"])
        drafts.document(for: "/tmp/a/b.md")
        drafts.remove(under: "/tmp/a/b.md")
        #expect(drafts.documents["/tmp/a/b.md"] == nil)
        #expect(drafts.documents.keys.sorted() == ["/tmp/other.md"])
    }

    @MainActor @Test func saveAllWritesWhatItCanAndNamesTheRest() async throws {
        let t = try PrimitiveHome()
        let kept = try t.write("plugin/a.md", "a\n")
        let stale = try t.write("plugin/b.md", "b\n")
        let clean = try t.write("plugin/c.md", "c\n")
        let drafts = PrimitiveDrafts()
        for file in [kept, stale, clean] { drafts.document(for: file.path) }
        await drafts.settled()
        // Nothing unsaved: nothing written, nothing to report.
        var result = await drafts.saveAll()
        #expect(!result.wrote && result.failed.isEmpty)

        drafts.documents[kept.path]?.text = "a, edited\n"
        drafts.documents[stale.path]?.text = "b, edited\n"
        // Something else rewrote b.md since it was opened: its save is refused.
        try "b, from elsewhere\n".write(to: stale, atomically: true, encoding: .utf8)
        result = await drafts.saveAll()
        // One file landed, so the page's lists must read the disk again
        // even though the other is still unsaved.
        #expect(result.wrote)
        #expect(result.failed == ["b.md"])
        #expect(try String(contentsOf: kept, encoding: .utf8) == "a, edited\n")
        #expect(try String(contentsOf: stale, encoding: .utf8) == "b, from elsewhere\n")
        #expect(drafts.dirty.keys.sorted() == [stale.path])

        // Only the refused file is left: nothing is written this time.
        result = await drafts.saveAll()
        #expect(!result.wrote && result.failed == ["b.md"])
    }
}

@Suite struct OverlaySaveTests {
    @Test func aFailedWriteRollsTheRenameBack() throws {
        let t = try PrimitiveHome()
        var names = PluginDisplayNames()
        names.set("Old", forPlugin: "p@m")
        struct Boom: Error {}
        #expect(throws: Boom.self) {
            try names.saved({ $0.set("New", forPlugin: "p@m") },
                            to: PluginDisplayNames.file(home: t.home),
                            write: { _, _ in throw Boom() })
        }
        #expect(names.name(forPlugin: "p@m") == "Old")
    }

    @Test func aGoodWriteKeepsTheRename() throws {
        let t = try PrimitiveHome()
        var names = PluginDisplayNames()
        var written: PluginDisplayNames?
        try names.saved({ $0.set("New", forPlugin: "p@m") },
                        to: PluginDisplayNames.file(home: t.home),
                        write: { n, _ in written = n })
        #expect(written?.name(forPlugin: "p@m") == "New")
        #expect(names.name(forPlugin: "p@m") == "New")
    }
}
