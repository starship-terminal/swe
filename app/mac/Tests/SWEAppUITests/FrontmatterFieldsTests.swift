import Foundation
import SWECore
import SwiftUI
import Testing
@testable import SWEApp

/// The skill rail's frontmatter fields read and write the SKILL.md draft
/// the editor shows: one document, so one Save writes both.
@Suite @MainActor struct FrontmatterFieldsTests {
    @Test func aFieldReadsAndWritesTheSharedDraft() {
        let document = InstructionDocument(url: URL(fileURLWithPath: "/nonexistent/SKILL.md"))
        document.text = "---\nname: demo\ndescription: >\n  Old words\n  folded.\nmetadata:\n  a: b\n---\n\nBody\n"
        var edits = 0
        let description = SkillDetailContent.field("description", of: document, name: "demo") { edits += 1 }
        #expect(description.wrappedValue == "Old words folded.\n")

        description.wrappedValue = "New words.\nTwo lines."
        #expect(document.text == "---\nname: demo\ndescription: |-\n  New words.\n  Two lines.\nmetadata:\n  a: b\n---\n\nBody\n")
        #expect(description.wrappedValue == "New words.\nTwo lines.")
        #expect(edits == 1)

        // Setting what is already there writes nothing and is no edit.
        description.wrappedValue = "New words.\nTwo lines."
        #expect(edits == 1)

        // The name is the skill's id: the field never writes it.
        let name = SkillDetailContent.field("name", of: document, name: "demo") { edits += 1 }
        name.wrappedValue = "other"
        #expect(name.wrappedValue == "demo")
        #expect(edits == 1)
    }
}
