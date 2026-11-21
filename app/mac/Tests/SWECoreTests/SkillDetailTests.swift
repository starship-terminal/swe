import Foundation
import Testing
@testable import SWECore

@Suite struct SkillFileOutlineTests {
    @Test func nestsTheFolderWithSkillMdFirst() throws {
        let t = try PrimitiveHome()
        let root = try t.dir(".swe/skills/tdd")
        try t.write(".swe/skills/tdd/SKILL.md", "---\nname: tdd\n---\n")
        try t.write(".swe/skills/tdd/README.md", "x")
        try t.write(".swe/skills/tdd/scripts/run.sh", "x")
        try t.write(".swe/skills/tdd/scripts/lib/util.sh", "x")
        try t.write(".swe/skills/tdd/references/api.md", "x")
        try t.write(".swe/skills/tdd/.hidden", "x")
        try t.dir(".swe/skills/tdd/skills/empty")
        let outline = SkillFileOutline(tree: PrimitiveTree(root: root), root: root)
        #expect(outline.nodes.map(\.name) == ["SKILL.md", "references", "scripts", "skills", "README.md"])
        let scripts = try #require(outline.nodes.first { $0.name == "scripts" })
        #expect(scripts.isFolder)
        #expect(scripts.path == root.appending(path: "scripts").path)
        #expect(scripts.children.map(\.name) == ["lib", "run.sh"])
        #expect(scripts.children[0].children.map(\.path) == [root.appending(path: "scripts/lib/util.sh").path])
        // An empty skill folder still shows.
        #expect(outline.nodes.first { $0.name == "skills" }?.children.map(\.name) == ["empty"])
        #expect(outline.filePaths.first == root.appending(path: "SKILL.md").path)
        #expect(outline.filePaths.count == 5)
    }
}

@Suite struct SkillDetailLayoutRuleTests {
    @Test func theRailNarrowsBeforeTheEditorGetsCramped() {
        // A 900pt window: 624pt of page beside the standard sidebar.
        #expect(SkillDetailLayout.railWidth(available: 624) == 232)
        #expect(SkillDetailLayout.railWidth(available: 824) == 300)
        #expect(SkillDetailLayout.railWidth(available: 0) == 300)
        // Zoomed in, the rail grows but never takes the page.
        #expect(SkillDetailLayout.railWidth(available: 624, zoom: 1.5) == 624 * 0.45)
        #expect(SkillDetailLayout.railWidth(available: 2000, zoom: 1.5) == 450)
    }

    @Test func thePanesStackWhenTheEditorWouldDropUnder380() {
        // 900pt window, standard sidebar: side by side, editor at 380.
        #expect(!SkillDetailLayout.stacks(available: 624))
        // 900pt window, widest sidebar (360): 460pt of page.
        let narrowest = 900 - SidebarWidth.range.upperBound - 80
        #expect(narrowest == 460)
        #expect(SkillDetailLayout.stacks(available: narrowest))
        #expect(SkillDetailLayout.stacks(available: 623))
        #expect(!SkillDetailLayout.stacks(available: 0))
        #expect(SkillDetailLayout.stacks(available: 824, zoom: 1.5))
    }
}

@Suite struct SkillFrontmatterFormTests {
    @Test func nameIsLockedAndDescriptionAlwaysOffered() {
        let rows = SkillFrontmatterForm.rows(in: "---\nlicense: MIT\nname: tdd\n---\n", name: "folder")
        #expect(rows.map(\.key) == ["name", "description", "license"])
        #expect(rows[0] == .init(key: "name", value: "tdd", multiline: false, locked: true))
        #expect(rows[1] == .init(key: "description", value: "", multiline: true, locked: false))
        // No frontmatter: the folder's name, and a description to add.
        let bare = SkillFrontmatterForm.rows(in: "# Body\n", name: "folder")
        #expect(bare.map(\.value) == ["folder", ""])
    }

    @Test func aDescriptionTheFormCannotEditIsLeftOut() {
        let text = "---\nname: x\ndescription: \"two\n  lines\"\nmetadata:\n  a: b\n---\n"
        #expect(SkillFrontmatterForm.rows(in: text, name: "x").map(\.key) == ["name"])
        #expect(SkillFrontmatterForm.lockedKeys(in: text) == ["description", "metadata"])
        #expect(SkillFrontmatterForm.setting("description", to: "new", in: text) == text)
    }

    @Test func settingWritesTheDraftTextAndNeverTheName() {
        let text = "---\nname: x\ndescription: |\n  old\n---\nbody\n"
        #expect(SkillFrontmatterForm.setting("name", to: "y", in: text) == text)
        let edited = SkillFrontmatterForm.setting("description", to: "new\nlines\n", in: text)
        #expect(edited == "---\nname: x\ndescription: |\n  new\n  lines\n---\nbody\n")
        #expect(SkillFrontmatterForm.rows(in: edited, name: "x")[1].value == "new\nlines\n")
    }
}
