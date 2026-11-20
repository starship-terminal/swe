import SwiftUI

/// A compact bordered band: a label, then what it's about. The AGENTS.md
/// page sets one above its editor and builds its chapter rail on one; the
/// skill detail's rail is one too.
struct Strip<Content: View>: View {
    /// False for content that pads itself, such as a scroll view whose
    /// rows' hover fills reach toward the edge.
    var padded = true
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) { content }
            .padding(.horizontal, padded ? 16 : 0)
            .padding(.vertical, padded ? 12 : 0)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius).strokeBorder(Theme.border, lineWidth: 1))
    }
}
