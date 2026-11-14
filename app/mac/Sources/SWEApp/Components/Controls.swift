import SWECore
import AppKit
import SwiftUI

/// TokenTracker's search input: 36pt, radius 8, a search glyph, 14/20
/// text on the field fill, a hairline that strengthens on focus, and the
/// focus ring. Give it the room: it takes the slack in a filter bar.
struct SearchField: View {
    var prompt: String
    @Binding var text: String
    /// A shortcut that puts the cursor in the field (⌘F on Repositories).
    var focusShortcut: KeyboardShortcut? = nil
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(Theme.font(size: 14))
                .foregroundStyle(Theme.textMuted)
            TextField(prompt, text: $text)
                .textFieldStyle(.plain)
                .font(Theme.bodySmall)
                .focused($focused)
            if !text.isEmpty {
                Button("Clear", systemImage: "xmark") { text = "" }
                    .labelStyle(.iconOnly)
                    .font(Theme.font(size: 11))
                    .foregroundStyle(Theme.textMuted)
                    .plainButton(cornerRadius: Theme.smallRadius)
                    .help("Clear")
            }
        }
        .padding(.horizontal, 12)
        .frame(height: Theme.fieldHeight)
        .background(Theme.field, in: .rect(cornerRadius: Theme.fieldRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.fieldRadius)
            .strokeBorder(focused ? Theme.borderHover : Theme.border, lineWidth: 1))
        .focusRing(focused, cornerRadius: Theme.fieldRadius, afterPointer: true)
        .background {
            if let focusShortcut {
                Button("Search") { focused = true }
                    .keyboardShortcut(focusShortcut)
                    .opacity(0)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)
            }
        }
        .layoutPriority(1)
    }
}

/// TokenTracker's select: 36pt, radius 6, 14/20 at 400 on the field fill,
/// a chevron, and a border that strengthens under the pointer. An optional
/// "Label:" prefix and symbol. The list underneath is SWE-drawn, not
/// the system menu: each row keeps its icon before the title and the
/// current pick checks at the trailing edge.
/// An option that stands for a harness or brand can carry its id: the row
/// and, once picked, the closed label then draw its logo. An option whose
/// glyph is an SF Symbol instead goes in `symbols`. An option with neither
/// shows its title only; with no icons at all the icon column drops out.
struct SelectMenu<Value: Hashable>: View {
    var label: String? = nil
    var symbol: String? = nil
    var options: [(value: Value, title: String)]
    @Binding var selection: Value
    /// 168pt as measured; nil sizes to the text.
    var width: CGFloat? = 168
    /// What the closed menu shows. Defaults to the selected option's title;
    /// pass it when the selection can drop out of `options` (a filter whose
    /// value vanished on refresh) so the menu still says what it's set to.
    var title: String? = nil
    /// The harness or brand id whose logo goes beside an option, by option
    /// value. The three-part options initialiser fills it in.
    var icons: [Value: String] = [:]
    /// The SF Symbol beside an option, by option value, for rows with a
    /// glyph but no bundled logo (the benchmark metrics).
    var symbols: [Value: String] = [:]
    @State private var open = false
    @State private var hovering = false

    private var current: String {
        title ?? options.first { $0.value == selection }?.title ?? ""
    }

    var body: some View {
        Button { open.toggle() } label: {
            HStack(spacing: 6) {
                if let symbol { Image(systemName: symbol).foregroundStyle(Theme.textSecondary) }
                if let label {
                    // The prefix keeps its width; the value truncates instead.
                    Text("\(label):").foregroundStyle(Theme.textSecondary).lineLimit(1).fixedSize()
                }
                selectOptionIcon(id: icons[selection], symbol: symbols[selection])
                Text(current).foregroundStyle(Theme.textPrimary).lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 4)
                Image(systemName: "chevron.down").font(Theme.font(size: 12))
                    .foregroundStyle(Theme.textSecondary)
            }
            .font(Theme.bodySmall)
            .padding(.horizontal, 12)
            .frame(width: width, height: Theme.fieldHeight)
            .fixedSize(horizontal: width == nil, vertical: false)
            .background(Theme.field, in: .rect(cornerRadius: Theme.selectRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.selectRadius)
                .strokeBorder(hovering || open ? Theme.borderHover : Theme.border, lineWidth: 1))
            .contentShape(.rect(cornerRadius: Theme.selectRadius))
        }
        .plainButton(cornerRadius: Theme.selectRadius)
        .onHover { hovering = $0 }
        .fixedSize()
        .popover(isPresented: $open, arrowEdge: .bottom) {
            SelectOptionList(options: options, selection: selection,
                             pick: { selection = $0; open = false },
                             icons: icons, symbols: symbols,
                             minWidth: max(200, width ?? 0))
        }
    }
}

/// A select option's icon: the bundled logo a harness or brand id names,
/// else the named SF Symbol in `text.secondary`, else nothing.
@MainActor @ViewBuilder private func selectOptionIcon(id: String?, symbol: String?) -> some View {
    if let id, HarnessIcon.exists(for: id) {
        HarnessIcon(harness: id, size: 16)
    } else if let symbol {
        Image(systemName: symbol)
            .font(Theme.font(size: 13))
            .foregroundStyle(Theme.textSecondary)
    }
}

/// The open half a select draws: a scrollable, key-navigable option list.
/// Each row is icon slot, title and a trailing check on the current pick;
/// the icon column drops out when no option carries one. It opens on the
/// selection so the pick isn't below the fold, and the arrow keys walk the
/// highlight the way the system menu's do. A `searchPrompt` adds a filter
/// field on top, for lists too long to scan: typing narrows the rows while
/// the arrows and Return keep working with the field still focused.
struct SelectOptionList<Value: Hashable>: View {
    var options: [(value: Value, title: String)]
    /// The pick the check and the opening highlight sit on.
    var selection: Value
    /// Called with the clicked or Returned row's value.
    var pick: (Value) -> Void
    /// A harness or brand logo id per option, drawn by `HarnessIcon`.
    var icons: [Value: String] = [:]
    /// An SF Symbol per option without a bundled logo.
    var symbols: [Value: String] = [:]
    /// The popover's floor; the select passes its face's width where that
    /// tops it.
    var minWidth: CGFloat = 200
    /// Action lists use the same rows with destructive text for removal.
    var destructiveValues: Set<Value> = []
    /// A filter field's prompt, or nil for a list short enough to scan.
    var searchPrompt: String? = nil
    /// The row the arrow keys are on; Return picks it.
    @State private var highlighted: Value?
    /// The filter's text. Typing narrows `shown` by title.
    @State private var query = ""
    @FocusState private var listFocused: Bool
    @FocusState private var searchFocused: Bool

    /// Rows keep an icon column once any option carries one, so titles
    /// still line up.
    private var hasIcons: Bool { !icons.isEmpty || !symbols.isEmpty }

    /// The rows the filter leaves, or all of them.
    private var shown: [(value: Value, title: String)] {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return options }
        return options.filter { $0.title.localizedCaseInsensitiveContains(q) }
    }

    var body: some View {
        ScrollViewReader { proxy in
            VStack(alignment: .leading, spacing: 0) {
                if let searchPrompt {
                    SearchField(prompt: searchPrompt, text: $query)
                        .focused($searchFocused)
                        // The field eats plain arrows as caret moves;
                        // onMoveCommand sees them first, so the highlight
                        // still walks while typing.
                        .onMoveCommand { direction in
                            switch direction {
                            case .up: _ = step(-1, proxy)
                            case .down: _ = step(1, proxy)
                            default: break
                            }
                        }
                        .onSubmit { if let highlighted { pick(highlighted) } }
                        .padding(.horizontal, 8).padding(.top, 8)
                        .padding(.bottom, 2)
                    SettingsDivider()
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if shown.isEmpty {
                            Text("Nothing matches.")
                                .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                                .frame(maxWidth: .infinity).padding(.vertical, 12)
                        } else {
                            ForEach(shown, id: \.value) { option in
                                row(option).id(option.value)
                            }
                        }
                    }
                    .padding(.vertical, 6)
                }
                .focusable()
                .focusEffectDisabled()
                .focused($listFocused)
            }
            .onAppear {
                // A stale filter must not hide the selection on reopen.
                query = ""
                highlighted = options.contains { $0.value == selection } ? selection : shown.first?.value
                if searchPrompt == nil { listFocused = true } else { searchFocused = true }
                proxy.scrollTo(selection, anchor: .center)
            }
            .onChange(of: query) {
                // While filtering, Return takes the top match; a cleared
                // field lands back on the selection.
                highlighted = query.trimmingCharacters(in: .whitespaces).isEmpty
                    ? selection : shown.first?.value
                if let highlighted { proxy.scrollTo(highlighted) }
            }
            .onKeyPress(.upArrow) { step(-1, proxy) }
            .onKeyPress(.downArrow) { step(1, proxy) }
            .onKeyPress(.return) {
                guard let highlighted else { return .ignored }
                pick(highlighted)
                return .handled
            }
        }
        .frame(minWidth: minWidth, maxHeight: 360)
        .fixedSize(horizontal: true, vertical: false)
    }

    private func step(_ by: Int, _ proxy: ScrollViewProxy) -> KeyPress.Result {
        guard !shown.isEmpty else { return .ignored }
        let index = shown.firstIndex { $0.value == highlighted } ?? (by > 0 ? -1 : shown.count)
        let next = shown[min(max(index + by, 0), shown.count - 1)].value
        highlighted = next
        proxy.scrollTo(next)
        return .handled
    }

    private func row(_ option: (value: Value, title: String)) -> some View {
        let on = option.value == selection
        return Button {
            pick(option.value)
        } label: {
            HStack(spacing: 8) {
                if hasIcons {
                    selectOptionIcon(id: icons[option.value], symbol: symbols[option.value])
                        .frame(width: 16, height: 16)
                }
                Text(option.title).font(Theme.bodySmall)
                    .foregroundStyle(destructiveValues.contains(option.value) ? Theme.danger : Theme.textPrimary).lineLimit(1)
                Spacer(minLength: 12)
                Image(systemName: "checkmark").font(Theme.font(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.interactive)
                    .opacity(on ? 1 : 0)
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(option.value == highlighted ? Theme.rowHover : .clear)
            .contentShape(.rect)
        }
        .plainButton(cornerRadius: 0)
        .onHover { if $0 { highlighted = option.value } }
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

extension SelectMenu {
    /// Options as (value, title, harness id or nil).
    init(label: String? = nil, symbol: String? = nil,
         options: [(value: Value, title: String, icon: String?)],
         selection: Binding<Value>, width: CGFloat? = 168, title: String? = nil) {
        var icons: [Value: String] = [:]
        for option in options { if let icon = option.icon { icons[option.value] = icon } }
        self.init(label: label, symbol: symbol, options: options.map { ($0.value, $0.title) },
                  selection: selection, width: width, title: title, icons: icons)
    }
}

/// SelectMenu's face over one pick, for lists whose rows carry logos
/// (providers, harnesses). Its face draws the picked option's logo, or
/// nothing while a `title` stands in for the selection; the open list is
/// the shared `SelectOptionList`.
struct PopoverSelect<Value: Hashable>: View {
    var options: [(value: Value, title: String, icon: String?)]
    @Binding var selection: Value
    /// What the face shows; the selected option's title when nil. A title
    /// also keeps the face from drawing the picked option's logo.
    var title: String? = nil
    /// A filter field's prompt over the open list, for lists too long to
    /// scan; nil keeps the list bare.
    var searchPrompt: String? = nil
    @State private var open = false
    @State private var hovering = false

    private var current: (title: String, icon: String?) {
        let picked = options.first { $0.value == selection }
        return (title ?? picked?.title ?? "", title == nil ? picked?.icon : nil)
    }

    /// The options' logo ids keyed by value, for the list's icon column.
    private var icons: [Value: String] {
        var found: [Value: String] = [:]
        for option in options { if let icon = option.icon { found[option.value] = icon } }
        return found
    }

    var body: some View {
        Button { open.toggle() } label: {
            HStack(spacing: 6) {
                if let icon = current.icon, HarnessIcon.exists(for: icon) { HarnessIcon(harness: icon, size: 16) }
                Text(current.title).foregroundStyle(Theme.textPrimary).lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 4)
                Image(systemName: "chevron.down").font(Theme.font(size: 12))
                    .foregroundStyle(Theme.textSecondary)
            }
            .font(Theme.bodySmall)
            .padding(.horizontal, 12)
            .frame(height: Theme.fieldHeight)
            .fixedSize()
            .background(Theme.field, in: .rect(cornerRadius: Theme.selectRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.selectRadius)
                .strokeBorder(hovering ? Theme.borderHover : Theme.border, lineWidth: 1))
            .contentShape(.rect(cornerRadius: Theme.selectRadius))
        }
        .plainButton(cornerRadius: Theme.selectRadius)
        .onHover { hovering = $0 }
        .fixedSize()
        .popover(isPresented: $open, arrowEdge: .bottom) {
            SelectOptionList(options: options.map { ($0.value, $0.title) },
                             selection: selection,
                             pick: { selection = $0; open = false },
                             icons: icons, minWidth: 220, searchPrompt: searchPrompt)
        }
    }
}

/// SelectMenu's face over a set of picks: each option toggles on its own,
/// and the popover stays open so several can be ticked in one go. An empty
/// set is the "all" row at the top. Options carry an optional harness id,
/// drawn as the logo beside the title and, for one pick, on the face.
struct MultiSelectMenu<Value: Hashable>: View {
    /// The face and top row when nothing is picked: "All harnesses".
    var allTitle: String
    var options: [(value: Value, title: String, icon: String?)]
    @Binding var selection: Set<Value>
    /// The face for two or more picks: "2 harnesses".
    var countTitle: (Int) -> String
    /// What the face says, when the caller knows better than the picks
    /// (a stored selection that includes names no longer offered).
    var title: String? = nil
    /// A status dot before an option's logo, by option value, in the
    /// open list only.
    var dots: [Value: Color] = [:]
    @State private var open = false
    @State private var hovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The picks in option order, for a stable face.
    private var picked: [(value: Value, title: String, icon: String?)] {
        options.filter { selection.contains($0.value) }
    }

    private var current: String {
        if let title { return title }
        switch picked.count {
        case 0: return allTitle
        case 1: return picked[0].title
        default: return countTitle(picked.count)
        }
    }

    var body: some View {
        Button { open.toggle() } label: {
            HStack(spacing: 6) {
                ForEach(picked.prefix(3), id: \.value) { option in
                    if let icon = option.icon, HarnessIcon.exists(for: icon) {
                        HarnessIcon(harness: icon, size: 16)
                    }
                }
                Text(current).foregroundStyle(Theme.textPrimary).lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 4)
                Image(systemName: "chevron.down").font(Theme.font(size: 12))
                    .foregroundStyle(Theme.textSecondary)
            }
            .font(Theme.bodySmall)
            .padding(.horizontal, 12)
            .frame(height: Theme.fieldHeight)
            .fixedSize()
            .background(Theme.field, in: .rect(cornerRadius: Theme.selectRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.selectRadius)
                .strokeBorder(hovering ? Theme.borderHover : Theme.border, lineWidth: 1))
            .contentShape(.rect(cornerRadius: Theme.selectRadius))
        }
        .plainButton(cornerRadius: Theme.selectRadius)
        .onHover { hovering = $0 }
        .fixedSize()
        .popover(isPresented: $open, arrowEdge: .bottom) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    row(title: allTitle, icon: nil, dot: nil, on: selection.isEmpty) { selection = [] }
                    Divider().padding(.vertical, 4)
                    ForEach(options, id: \.value) { option in
                        row(title: option.title, icon: option.icon, dot: dots[option.value],
                            on: selection.contains(option.value)) {
                            if selection.contains(option.value) { selection.remove(option.value) }
                            else { selection.insert(option.value) }
                        }
                    }
                }
                .padding(.vertical, 6)
            }
            // A long list scrolls the way SelectOptionList does; a short
            // one keeps its own height. The cap fits every harness the
            // AGENTS.md Compile tab lists (`PopoverList`).
            .frame(minWidth: 200, maxHeight: PopoverList.maxHeight)
            .fixedSize(horizontal: true, vertical: false)
        }
    }

    private func row(title: String, icon: String?, dot: Color?, on: Bool,
                     action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: on ? "checkmark.square.fill" : "square")
                    .foregroundStyle(on ? Theme.interactive : Theme.textMuted)
                    .animation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration), value: on)
                if !dots.isEmpty {
                    Circle().fill(dot ?? .clear)
                        .frame(width: Theme.statusDot, height: Theme.statusDot)
                }
                if let icon, HarnessIcon.exists(for: icon) { HarnessIcon(harness: icon, size: 16) }
                Text(title).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary).lineLimit(1)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            .contentShape(.rect)
        }
        .plainButton(cornerRadius: 0)
        .modifier(RowHover())
    }
}

/// `Theme.rowHover` under the pointer: the one row-hover fill for list
/// rows, menus and pickers.
struct RowHover: ViewModifier {
    @State private var hovering = false

    func body(content: Content) -> some View {
        content
            .background(hovering ? Theme.rowHover : .clear)
            .onHover { hovering = $0 }
    }
}

/// Lays children out left to right and wraps, like inline text.
struct FlowLayout: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(width: proposal.width ?? .infinity, subviews: subviews)
        let width = rows.map(\.width).max() ?? 0
        let height = rows.map(\.height).reduce(0, +) + lineSpacing * CGFloat(max(rows.count - 1, 0))
        // An unbounded proposal (a horizontal scroll view, sizeThatFits
        // at .infinity) gets the rows' own width, never an infinite one.
        let proposed = proposal.width.flatMap { $0.isFinite ? $0 : nil }
        return CGSize(width: proposed ?? width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in arrange(width: bounds.width, subviews: subviews) {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(at: CGPoint(x: x, y: y + (row.height - size.height) / 2),
                                      proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += row.height + lineSpacing
        }
    }

    private struct Row {
        var indices: [Int] = []
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> [Row] {
        var rows: [Row] = []
        var row = Row()
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            let next = row.indices.isEmpty ? size.width : row.width + spacing + size.width
            if next > width, !row.indices.isEmpty {
                rows.append(row)
                row = Row()
            }
            row.width = row.indices.isEmpty ? size.width : row.width + spacing + size.width
            row.height = max(row.height, size.height)
            row.indices.append(index)
        }
        if !row.indices.isEmpty { rows.append(row) }
        return rows
    }
}

/// `~/…` for a path under the home folder.
func displayPath(_ path: String) -> String {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    if path == home || path == home + "/" { return "~" }
    return path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
}

func displayPath(_ url: URL) -> String {
    displayPath(url.path)
}

func copyToPasteboard(_ text: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
}

extension Caret {
    /// A line break in place of `selection` in `text`, and the caret
    /// offset just after it. A selection the field reports from an older
    /// copy of the text (cleared after a send, edited since) can hold
    /// indices that fall inside one of its characters, and replacing
    /// there corrupts the text; then, as with no selection at all, the
    /// break goes at the end.
    static func newline(in text: String, replacing selection: TextSelection?) -> (text: String, cursor: Int) {
        var text = text
        guard case .selection(let range) = selection?.indices,
              range.upperBound <= text.endIndex,
              // Character boundaries of this text, or nothing: an index
              // inside a character would split it into garbage.
              let lower = boundary(range.lowerBound, in: text),
              let upper = boundary(range.upperBound, in: text),
              lower <= upper else {
            text += "\n"
            return (text, text.count)
        }
        let at = text.distance(from: text.startIndex, to: lower)
        text.replaceSubrange(lower..<upper, with: "\n")
        return (text, at + 1)
    }

    /// `index` as one of `text`'s own character positions, matched by its
    /// UTF-8 offset; nil when that offset falls inside a character.
    /// (`samePosition(in:)` and `String.Index(_:within:)` trust an index
    /// from an ASCII string and pass it through unchecked.)
    private static func boundary(_ index: String.Index, in text: String) -> String.Index? {
        let target = text.utf8.distance(from: text.utf8.startIndex, to: index)
        var offset = 0
        var position = text.startIndex
        while offset < target, position < text.endIndex {
            offset += text[position].utf8.count
            position = text.index(after: position)
        }
        return offset == target ? position : nil
    }
}
