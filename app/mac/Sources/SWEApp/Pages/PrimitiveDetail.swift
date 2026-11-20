import SWECore
import SwiftUI

// MARK: - Shared detail pieces

/// The lock line over anything that isn't the user's own: a plugin out of
/// a GitHub marketplace or a skill in a checkout gets its edits undone by
/// whatever manages that copy, so the page shows it read-only.
struct PrimitiveLockNote: View {
    var text: String

    var body: some View {
        Label(text, systemImage: "lock.fill")
            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.field, in: .rect(cornerRadius: Theme.controlRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius)
                .strokeBorder(Theme.border, lineWidth: 1))
    }
}

/// The "＋ New" menu the plugin detail's file list shows for an editable
/// folder.
struct NewPartMenu: View {
    var kinds: [PrimitiveParts.Kind]
    var pick: (PrimitiveParts.Kind) -> Void

    var body: some View {
        Menu {
            ForEach(kinds, id: \.self) { kind in
                Button("New \(kind.title)") { pick(kind) }
            }
        } label: {
            Label("New", systemImage: "plus")
        }
        .secondaryButton()
    }
}

/// Set by a plugin or skill detail while it is on screen, so its page can
/// show the header's back chevron like every other detail page. The
/// chevron bumps `navReselects`, which the detail answers through its
/// unsaved-changes ask.
struct DetailOpenKey: PreferenceKey {
    static let defaultValue = false
    static func reduce(value: inout Bool, nextValue: () -> Bool) { value = value || nextValue() }
}
