import PhotosUI
import SwiftUI

/// The paperclip left of the prompt box: Photos or Files.
struct AttachButton: View {
    let attachments: ComposerAttachments
    @State private var photosShown = false
    @State private var filesShown = false
    @State private var picks: [PhotosPickerItem] = []
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        Menu {
            Button { photosShown = true } label: { Label("Photos", systemImage: "photo.on.rectangle") }
            Button { filesShown = true } label: { Label("Files", systemImage: "folder") }
        } label: {
            Image(systemName: "paperclip")
                .font(Theme.font(size: 17, weight: .medium))
                .foregroundStyle(Theme.textSecondary)
                .frame(width: 40, height: 40)
                .background(Theme.field, in: .circle)
                .overlay(Circle().strokeBorder(Theme.border, lineWidth: 1))
                .contentShape(.circle)
        }
        .opacity(enabled ? 1 : 0.4)
        .accessibilityLabel("Attach a photo or file")
        .photosPicker(isPresented: $photosShown, selection: $picks,
                      maxSelectionCount: max(1, attachments.room), matching: .images)
        .onChange(of: picks) { _, items in
            guard !items.isEmpty else { return }
            for item in items {
                // As a file: the original never sits in memory whole.
                attachments.addPhoto(file: { try await item.loadTransferable(type: SpooledPhoto.self)?.url })
            }
            picks = []
        }
        .fileImporter(isPresented: $filesShown, allowedContentTypes: AttachmentRules.pickerTypes,
                      allowsMultipleSelection: true) { result in
            guard case .success(let urls) = result else { return }
            for url in urls { attachments.addFile(at: url) }
        }
    }
}

/// The files picked for the next message, above the prompt box: each
/// chip names the file and its size, or how far its upload got, with an
/// x to drop it. A failed chip retries on tap.
struct AttachmentChips: View {
    let attachments: ComposerAttachments
    /// Nothing is typed yet: say the files need a message to go with them.
    var needsText = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let notice = attachments.notice {
                Text(notice).font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 4)
            }
            if !attachments.items.isEmpty {
                // Past two rows the chips scroll, so a big pick never pushes
                // the prompt box or a failed chip's controls off the screen.
                ViewThatFits(in: .vertical) {
                    chips
                    ScrollView(.vertical) { chips }.frame(height: Self.maxHeight)
                }
                .frame(maxHeight: Self.maxHeight)
                if needsText {
                    Text(AttachmentRules.needsText).font(Theme.caption).foregroundStyle(Theme.textMuted)
                        .padding(.horizontal, 4)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Two rows of 44 pt chips and the gap between them.
    static let maxHeight: CGFloat = 94

    private var chips: some View {
        FlowRows(spacing: 6) {
            ForEach(attachments.items) { item in
                AttachmentChip(item: item,
                               onRemove: { attachments.remove(item.id) },
                               onRetry: { attachments.retry(item.id) })
            }
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

struct AttachmentChip: View {
    let item: PendingAttachment
    var onRemove: () -> Void = {}
    var onRetry: () -> Void = {}

    private var failed: Bool { if case .failed = item.state { true } else { false } }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: failed ? "exclamationmark.triangle" : item.isImage ? "photo" : "doc.text")
                .font(Theme.font(size: 14))
                .foregroundStyle(failed ? Theme.dangerText : Theme.textSecondary)
                .iconColumn(16)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.name).font(Theme.control).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.middle)
                Text(Self.detail(item)).font(Theme.font(size: 11))
                    .foregroundStyle(failed ? Theme.dangerText : Theme.textMuted)
                    // A failure's reason and "Tap to retry" both have to show.
                    .lineLimit(failed ? 2 : 1).monospacedDigit()
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: 170, alignment: .leading)
            Button(action: onRemove) {
                Image(systemName: "xmark").font(Theme.font(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.textSecondary)
                    .frame(width: 32, height: 40)
                    .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Remove \(item.name)")
        }
        .padding(.leading, 10)
        .padding(.vertical, failed ? 4 : 0)
        .frame(minHeight: 44)
        .background(alignment: .bottomLeading) {
            if case .uploading(let fraction) = item.state {
                GeometryReader { geo in
                    Rectangle().fill(Theme.accentFill)
                        .frame(width: geo.size.width * fraction, height: 2)
                        .frame(maxHeight: .infinity, alignment: .bottom)
                }
            }
        }
        .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
        .clipShape(.rect(cornerRadius: Theme.blockRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius)
            .strokeBorder(failed ? Theme.danger : Theme.border, lineWidth: 1))
        .contentShape(.rect(cornerRadius: Theme.blockRadius))
        .onTapGesture { if failed { onRetry() } }
        .accessibilityElement(children: .combine)
        .accessibilityAction(named: "Retry") { if failed { onRetry() } }
    }

    /// The chip's second line.
    static func detail(_ item: PendingAttachment) -> String {
        switch item.state {
        case .preparing: "Preparing"
        case .uploading(let f): "Uploading \(Int((f * 100).rounded()))%"
        case .ready: AttachmentRules.size(item.size)
        case .failed(let why): "\(why). Tap to retry"
        }
    }
}

/// Files a sent message carried, above its bubble.
struct SentFiles: View {
    let names: [String]

    var body: some View {
        FlowRows(spacing: 6, trailing: true) {
            ForEach(Array(names.enumerated()), id: \.offset) { _, name in
                Label(name, systemImage: "paperclip")
                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    .lineLimit(1).truncationMode(.middle)
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(Theme.card, in: .rect(cornerRadius: Theme.controlRadius))
                    .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.border, lineWidth: 1))
            }
        }
    }
}

/// Lays its children out left to right, wrapping to a new row when the
/// next one doesn't fit. `trailing` packs each row to the right edge.
struct FlowRows: Layout {
    var spacing: CGFloat = 6
    var trailing = false

    private func rows(_ subviews: Subviews, width: CGFloat) -> [[(Int, CGSize)]] {
        var rows: [[(Int, CGSize)]] = [[]]
        var x: CGFloat = 0
        for (i, view) in subviews.enumerated() {
            var size = view.sizeThatFits(.unspecified)
            size.width = min(size.width, width)
            if x > 0, x + spacing + size.width > width {
                rows.append([])
                x = 0
            }
            x += (x > 0 ? spacing : 0) + size.width
            rows[rows.count - 1].append((i, size))
        }
        return rows
    }

    // The sums below are typed and kept one step per line on purpose. As
    // one expression, the widest-row sum took Xcode 26.6's compiler past
    // its type-check limit ("unable to type-check this expression in
    // reasonable time") while newer compilers still accepted it.

    /// The gaps between `count` children laid side by side or stacked.
    private func gaps(_ count: Int) -> CGFloat {
        spacing * CGFloat(max(0, count - 1))
    }

    /// A row's width: its children plus the gaps between them.
    private func width(of row: [(Int, CGSize)]) -> CGFloat {
        var total: CGFloat = 0
        for (_, size) in row { total += size.width }
        return total + gaps(row.count)
    }

    /// A row's height: its tallest child.
    private func height(of row: [(Int, CGSize)]) -> CGFloat {
        var tallest: CGFloat = 0
        for (_, size) in row { tallest = max(tallest, size.height) }
        return tallest
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = rows(subviews, width: proposal.width ?? .infinity)
        var height: CGFloat = gaps(rows.count)
        var widest: CGFloat = 0
        for row in rows {
            height += self.height(of: row)
            widest = max(widest, width(of: row))
        }
        return CGSize(width: proposal.width ?? widest, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in rows(subviews, width: bounds.width) {
            let used = width(of: row)
            var x = trailing ? bounds.maxX - used : bounds.minX
            let height = height(of: row)
            for (i, size) in row {
                subviews[i].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += height + spacing
        }
    }
}
