import AppKit
import SWECore
import SwiftUI

/// The shared `~/.swe/AGENTS.md`, where swe has linked it, and the
/// instruction files of one project folder.
struct AgentsMdPage: View {
    // Edit, then Project: the Compile tab joins between them later.
    enum Scope: Hashable { case edit, compile, project }

    @Environment(AppModel.self) private var app
    @State private var scope: Scope = .edit

    var body: some View {
        let model = app.agentsMd
        // The editor fills the page and scrolls itself, so the page doesn't.
        Page(title: "AGENTS.md", subtitle: "The instructions every harness reads, and each project's own.",
             scrolls: false) {
            Button("Refresh", systemImage: "arrow.clockwise") {
                Task { await model.load(swe: app.swe) }
                Task { await model.checkDisk() }
                Task { await app.chapters.load(swe: app.swe) }
                Task { await app.chapters.checkDisk() }
            }
            .iconButton()
            .keyboardShortcut("r", modifiers: .command)
            .help("Check links and reread the file (⌘R)")
        } content: {
            HStack {
                Tabs(options: [(Scope.edit, "Edit"), (Scope.compile, "Compile"),
                               (Scope.project, "Project")],
                     selection: $scope, style: .underline)
                Spacer(minLength: 8)
                if app.chapters.chapterMode {
                    DraftsMenu { scope = .edit }
                }
                if scope == .edit && app.chapters.chapterMode {
                    EditorFocusButton()
                    ViewCompiledButton(onOpenChapter: { _ in scope = .edit })
                }
            }
            switch scope {
            case .edit: GlobalInstructions()
            case .compile: CompileInstructions(onOpenChapter: { _ in scope = .edit })
            case .project: ProjectInstructions()
            }
        }
        .task(id: app.swePath) {
            async let links: () = model.load(swe: app.swe)
            async let chaptered: () = app.chapters.load(swe: app.swe)
            _ = await (links, chaptered)
        }
        // A build or chapter edit made in a terminal, while the page is on
        // screen: the Generated strip and the preview follow it.
        .task {
            let swe = FileManager.default.homeDirectoryForCurrentUser.appending(path: ".swe")
            for await _ in FolderChanges.stream(root: swe, matches: ChaptersModel.affectsCheck) {
                await app.chapters.load(swe: app.swe)
                await app.chapters.checkDisk()
            }
        }
        // Agents rewrite these files while they work; pick that up on
        // return. The chapter report refreshes too: an `instructions init`
        // run elsewhere swaps which editor this page should be showing.
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await model.checkDisk() }
            Task { await app.chapters.checkDisk() }
            Task { await app.chapters.load(swe: app.swe) }
        }
    }
}

private struct GlobalInstructions: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let model = app.agentsMd
        let chapters = app.chapters
        switch model.check {
        case .idle, .loading:
            SkeletonRows(rows: 5, shape: .link, label: "Checking links…")
        case .failed(let message):
            ErrorCard(message: message)
        case .loaded(let report):
            VStack(alignment: .leading, spacing: 12) {
                FocusCollapsible(enabled: chapters.chapterMode) {
                    LinksStrip(report: report)
                }
                switch chapters.status {
                case .loaded(let check) where check.indexExists:
                    ChaptersEditor(check: check)
                case .idle, .loading:
                    SkeletonRows(rows: 8, shape: .file, label: "Reading chapters…")
                default:
                    // A failed check means an swe without instructions: the
                    // flat file is the only thing there is to edit.
                    FlatInstructions(report: report, document: model.global)
                }
            }
        }
    }
}

/// The Compile tab: which chapters and which harnesses compile into what,
/// plus the compiled order. Resolves the same report and check Edit uses.
private struct CompileInstructions: View {
    var onOpenChapter: ((String) -> Void)? = nil
    @Environment(AppModel.self) private var app

    var body: some View {
        let model = app.agentsMd
        let chapters = app.chapters
        switch model.check {
        case .idle, .loading:
            SkeletonRows(rows: 5, shape: .link, label: "Checking links…")
        case .failed(let message):
            ErrorCard(message: message)
        case .loaded(let report):
            switch chapters.status {
            case .loaded(let check) where check.indexExists:
                CompileView(check: check, report: report,
                            onOpenChapter: onOpenChapter)
            case .idle, .loading:
                SkeletonRows(rows: 8, shape: .file, label: "Reading chapters…")
            default:
                Strip {
                    Text("Compilation needs chapters. Convert the flat AGENTS.md on the Edit tab first.")
                        .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                }
            }
        }
    }
}

/// What the filter compiles down to: the Include outline with every
/// file's checkbox, and the Order list of active chapters with per-harness
/// chips in All view.
struct CompileView: View {
    var check: InstructionsCheck
    var report: InitCheck
    var onOpenChapter: ((String) -> Void)? = nil

    @Environment(AppModel.self) private var app
    /// Comma-joined harness names (`CompileFilter`); "" or several names
    /// is the All-harnesses view, one linked name is that harness's view.
    @AppStorage("agentsMd.compile.filter") private var filter = ""
    @State private var error: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Every harness name the link report shows, first-seen order.
    private var harnesses: [String] { report.instructionHarnessNames }

    /// state/rule lookups for a file's AGENTS_MD-relative path, shared
    /// with the Edit tab's targets strip (`ChapterReceipts` in SWECore).
    private func state(for name: String) -> CompileState {
        check.compileState(of: name)
    }

    private func rule(for name: String) -> [String: [String]]? {
        check.rule(for: name)
    }

    private var harness: String? {
        CompileFilter.harness(stored: filter, known: harnesses)
    }

    private func receives(_ name: String) -> Bool {
        CompileToggle.receives(state: state(for: name), rule: rule(for: name),
                               harness: harness)
    }

    /// The output this filter ends in, for the View button and summary.
    private var outputPath: String {
        if let harness, let output = check.outputs?.first(where: { $0.harness == harness }) {
            return output.path
        }
        return check.output
    }

    var body: some View {
        let chapters = app.chapters
        VStack(alignment: .leading, spacing: 12) {
            filterStrip
            // A harness whose link doesn't reach its file still compiles,
            // but the file goes nowhere: say so with the same fix the
            // Edit strip offers.
            if let harness,
               let link = report.instructionRows.first(where: { $0.name == harness })?
                   .instructions,
               link.state != .linked {
                HStack(spacing: 8) {
                    Label("\(HarnessMark(harness).name)'s \(URL(fileURLWithPath: link.path).lastPathComponent) doesn't point at its file yet.",
                          systemImage: "exclamationmark.triangle")
                        .font(Theme.caption).foregroundStyle(Theme.dangerText)
                    Button(app.agentsMd.linking ? "Linking…" : "Link with swe init") {
                        Task { await app.agentsMd.link(swe: app.swe) }
                    }
                    .primaryButton()
                    .disabled(app.agentsMd.linking)
                }
                .padding(.horizontal, 4)
            }
            if let error = error ?? chapters.targetsParseProblem ?? chapters.buildError {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .padding(.horizontal, 4)
            }
            HStack(alignment: .top, spacing: 12) {
                includeRail
                orderList
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(maxHeight: .infinity)
    }

    /// The harness filter as one multi-select dropdown, each harness with
    /// its logo and link-state dot in the menu; the compiled file's
    /// preview button stays on the right.
    private var filterStrip: some View {
        Strip {
            HStack(spacing: 12) {
                Text("Harnesses").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                MultiSelectMenu(
                    allTitle: "All harnesses",
                    options: harnesses.map { (value: $0, title: HarnessMark($0).name, icon: $0) },
                    selection: Binding(
                        get: { Set(CompileFilter.picked(stored: filter, known: harnesses)) },
                        set: { picks in
                            withAnimation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)) {
                                filter = CompileFilter.stored(picking: picks, known: harnesses)
                            }
                        }),
                    countTitle: { "\($0) harnesses" },
                    title: CompileFilter.label(stored: filter, known: harnesses),
                    dots: Dictionary(uniqueKeysWithValues: harnesses.map { name in
                        (name, Self.linkColor(report.instructionRows
                            .first(where: { $0.name == name })?.instructions))
                    }))
                Spacer(minLength: 0)
                ViewCompiledButton(outputPath: outputPath,
                                   onOpenChapter: onOpenChapter)
            }
        }
    }

    /// The status dot's colour for a harness's instruction link.
    private static func linkColor(_ link: InitCheck.Link?) -> Color {
        guard let link else { return Theme.textMuted }
        return link.state == .linked ? Theme.success
            : link.state.isBlocked ? Theme.danger
            : link.state.initFixes ? Theme.warning : Theme.textMuted
    }

    /// A harness's logo only, falling back to the full mark where a
    /// harness has no icon — the name text follows separately.
    static func markIcon(_ name: String, size: CGFloat) -> some View {
        let mark = HarnessMark(name)
        return Group {
            if let icon = mark.icon {
                HarnessIcon(harness: icon, size: size)
            } else {
                HarnessMarkView(harness: name, size: size)
            }
        }
    }

    /// Every file's membership row for this filter, plus the folder the
    /// members roll up into.
    private var includeRail: some View {
        let chapters = app.chapters
        return Strip {
            HStack(spacing: 6) {
                Text("Include").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                Spacer(minLength: 4)
                Text("KiB").font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .frame(width: Self.kibColumn, alignment: .trailing)
                    .padding(.trailing, 8)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(chapters.tree) { node in
                        IncludeNode(node: node, depth: 0, check: check,
                                    harness: harness, sizes: chapters.sizes,
                                    state: { self.state(for: $0) },
                                    rule: { self.rule(for: $0) }) { problem in
                            error = problem
                        }
                    }
                    // Indexed chapters with no row above: missing file or
                    // a name the outline can't show. All view lets each
                    // be turned off from here.
                    if harness == nil {
                        ForEach(chapters.missing, id: \.self) { name in
                            MissingRow(name: name,
                                       absent: chapters.absent.contains(name)) { problem in
                                error = problem
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            // One harness picked: what that harness's file is built from.
            if harness != nil {
                HStack(spacing: 6) {
                    Text("Total").font(Theme.nav).foregroundStyle(Theme.textPrimary)
                    Spacer(minLength: 4)
                    Text(IncludeSizes.cell(bytes: IncludeSizes.bytes(
                        of: ChapterTree.files(under: "", in: chapters.tree) + chapters.missing,
                        sizes: chapters.sizes, included: receives)))
                        .font(Theme.caption).monospacedDigit()
                        .foregroundStyle(Theme.textPrimary)
                        .frame(width: Self.kibColumn, alignment: .trailing)
                }
                .padding(.horizontal, 8).padding(.top, 6)
                .overlay(alignment: .top) { SettingsDivider() }
                .transition(.opacity)
            }
        }
        .frame(width: 300)
        .frame(maxHeight: .infinity)
    }

    /// The right-aligned size column under the Include header's `KiB`.
    static let kibColumn: CGFloat = 40

    /// An Include row for an indexed chapter that has no outline row:
    /// checked (it is compiled) with "missing"/"hidden" saying why.
    private struct MissingRow: View {
        var name: String
        var absent: Bool
        var onError: (String?) -> Void
        @Environment(AppModel.self) private var app
        @State private var hovering = false

        var body: some View {
            HStack(spacing: 5) {
                Color.clear.frame(width: 10)
                TriBox(tri: .all) {
                    Task {
                        onError(await app.chapters.setIncluded(
                            name: name, on: false, swe: app.swe))
                    }
                }
                Text(name).font(Theme.nav)
                    .foregroundStyle(Theme.textNav)
                    .lineLimit(1).truncationMode(.head)
                Text(absent ? "missing" : "hidden")
                    .font(Theme.caption)
                    .foregroundStyle(absent ? Theme.dangerText : Theme.textMuted)
                Spacer(minLength: 4)
            }
            .padding(.leading, 8).padding(.trailing, 8).padding(.vertical, 4)
            .background(hovering ? Theme.hover : .clear,
                        in: .rect(cornerRadius: Theme.controlRadius))
            .onHover { hovering = $0 }
            .hoverTip(absent ? "Listed in index.txt but the file is gone; uncheck to comment it out"
                             : "Listed in index.txt but hidden from the outline; uncheck to comment it out",
                      replacesLabel: false)
        }
    }

    /// The harness columns the Order matrix draws: the dropdown's picks,
    /// or every harness.
    private var columns: [String] {
        CompileFilter.columns(stored: filter, known: harnesses)
    }

    /// The fixed-width harness column shared by the matrix header and
    /// every Order row, so the chips line up vertically.
    private static let matrixColumn: CGFloat = 30

    /// Active chapters in index order: drag to reorder. In All view a
    /// matrix of harness chips — header of marks on top, one chip per
    /// chapter per column — toggles each receipt; in a harness view the
    /// row's own checkbox does it.
    private var orderList: some View {
        let chapters = app.chapters
        return Strip {
            HStack(spacing: 6) {
                Text("Order").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                Text("\(check.uniqueChapters.count)")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                Spacer(minLength: 4)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if harness == nil && !columns.isEmpty {
                        HStack(spacing: 8) {
                            Spacer(minLength: 0)
                            ForEach(columns, id: \.self) { name in
                                Button { filter = name } label: {
                                    Self.markIcon(name, size: 12)
                                        .frame(width: Self.matrixColumn)
                                }
                                .plainButton()
                                .hoverTip(HarnessMark(name).name, replacesLabel: false)
                            }
                        }
                        .padding(.horizontal, 8).padding(.vertical, 3)
                    }
                    ReorderableRows(check.uniqueChapters) { name, before in
                        Task { await chapters.move(chapter: name, before: before, swe: app.swe) }
                    } row: { chapter in
                        OrderRow(chapter: chapter, check: check, harness: harness,
                                 harnesses: columns,
                                 column: Self.matrixColumn,
                                 rule: { self.rule(for: $0) }) { problem in
                            error = problem
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// One outline row for Include: folder with a tri-state checkbox and
    /// "k/n" count, or a file with a checkbox.
    private struct IncludeNode: View {
        var node: ChapterNode
        var depth: Int
        var check: InstructionsCheck
        var harness: String?
        /// Bytes per file path, read off the main actor by the model.
        var sizes: [String: Int]
        var state: (String) -> CompileState
        var rule: (String) -> [String: [String]]?
        var onError: (String?) -> Void

        @Environment(AppModel.self) private var app
        @AppStorage private var expanded: Bool

        init(node: ChapterNode, depth: Int, check: InstructionsCheck,
             harness: String?, sizes: [String: Int],
             state: @escaping (String) -> CompileState,
             rule: @escaping (String) -> [String: [String]]?,
             onError: @escaping (String?) -> Void) {
            self.node = node
            self.depth = depth
            self.check = check
            self.harness = harness
            self.sizes = sizes
            self.state = state
            self.rule = rule
            self.onError = onError
            _expanded = AppStorage(wrappedValue: true,
                                   ChapterFolderState.compilePrefix + node.path)
        }

        /// Every file path under this node (the node itself for a file).
        private var files: [String] {
            node.isFolder ? collect(node) : [node.path]
        }

        private func collect(_ node: ChapterNode) -> [String] {
            node.isFolder ? node.children.flatMap(collect) : [node.path]
        }

        private func receipt(_ name: String) -> Bool {
            CompileToggle.receives(state: state(name), rule: rule(name),
                                   harness: harness)
        }

        private var tri: CompileToggle.TriState {
            CompileToggle.triState(files.map(receipt))
        }

        /// The size column: a file's own size, a folder's checked files.
        private var sizeText: String {
            node.isFolder
                ? IncludeSizes.cell(bytes: IncludeSizes.bytes(
                    of: files, sizes: sizes, included: receipt))
                : IncludeSizes.cell(bytes: sizes[node.path])
        }

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    if node.isFolder {
                        Image(systemName: expanded ? "chevron.down" : "chevron.right")
                            .font(Theme.font(size: 9, weight: .semibold))
                            .foregroundStyle(Theme.textMuted)
                            .frame(width: 10)
                            .onTapGesture { expanded.toggle() }
                    } else {
                        Color.clear.frame(width: 10)
                    }
                    TriBox(tri: node.isFolder ? tri
                           : (receipt(node.path) ? .all : .none)) {
                        toggle()
                    }
                    if node.isFolder {
                        Image(systemName: "folder").font(Theme.font(size: 12))
                            .foregroundStyle(Theme.textSecondary)
                    }
                    Text(node.name).font(Theme.nav)
                        .foregroundStyle(node.isFolder ? Theme.textPrimary
                                         : receipt(node.path) ? Theme.textNav : Theme.textMuted)
                        .lineLimit(1).truncationMode(.tail)
                    if node.isFolder {
                        Text("\(files.filter(receipt).count)/\(files.count)")
                            .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    }
                    Spacer(minLength: 4)
                    Text(sizeText).font(Theme.caption).monospacedDigit()
                        .foregroundStyle(node.isFolder || receipt(node.path)
                                         ? Theme.textSecondary : Theme.textMuted)
                        .frame(width: CompileView.kibColumn, alignment: .trailing)
                }
                .padding(.leading, CGFloat(depth) * 14 + 8)
                .padding(.trailing, 8).padding(.vertical, 4)
                .contentShape(.rect)
                .onTapGesture { toggle() }
                .contextMenu {
                    Button("Move to Trash", role: .destructive) {
                        Task {
                            onError(node.isFolder
                                ? await app.chapters.delete(folder: node.path, swe: app.swe)
                                : await app.chapters.delete(file: node.path, swe: app.swe))
                        }
                    }
                }
                if node.isFolder && expanded {
                    ForEach(node.children) { child in
                        IncludeNode(node: child, depth: depth + 1, check: check,
                                    harness: harness, sizes: sizes,
                                    state: state, rule: rule,
                                    onError: onError)
                    }
                }
            }
        }

        private func toggle() {
            let on = tri != .all
            Task {
                let problem: String?
                if node.isFolder {
                    problem = await app.chapters.setFolder(
                        node.path, on: on, harness: harness, swe: app.swe)
                } else if let harness {
                    problem = await app.chapters.setReceipt(
                        name: node.path, harness: harness,
                        want: on, swe: app.swe)
                } else {
                    problem = await app.chapters.setIncluded(
                        name: node.path, on: on, swe: app.swe)
                }
                onError(problem)
            }
        }
    }

    /// A checkbox that can also show the folder's mixed dash.
    struct TriBox: View {
        var tri: CompileToggle.TriState
        var toggle: () -> Void

        var body: some View {
            Button(action: toggle) {
                RoundedRectangle(cornerRadius: 4)
                    .strokeBorder(tri == .none ? Theme.borderStrong : .clear,
                                  lineWidth: 1)
                    .background(
                        RoundedRectangle(cornerRadius: 4)
                            .fill(tri == .none ? .clear : Theme.interactive))
                    .overlay {
                        if tri == .all {
                            Image(systemName: "checkmark")
                                .font(Theme.font(size: 9, weight: .bold))
                                .foregroundStyle(.white)
                        } else if tri == .mixed {
                            Image(systemName: "minus")
                                .font(Theme.font(size: 9, weight: .bold))
                                .foregroundStyle(.white)
                        }
                    }
                    .frame(width: 14, height: 14)
            }
            .plainButton()
            .accessibilityLabel(tri == .all ? "Included" : tri == .mixed
                                ? "Partially included" : "Not included")
        }
    }

    /// An Order row: position, name, and the per-harness chips (All view)
    /// or the harness's checkbox (harness view).
    private struct OrderRow: View {
        var chapter: InstructionsCheck.Chapter
        var check: InstructionsCheck
        var harness: String?
        var harnesses: [String]
        var column: CGFloat = 30
        var rule: (String) -> [String: [String]]?
        var onError: (String?) -> Void

        @Environment(AppModel.self) private var app
        @State private var hovering = false

        private var ruleText: String? {
            guard let rule = rule(chapter.name), let (kind, names) = rule.first else { return nil }
            return kind == "only" ? "only \(names.joined(separator: " "))"
                : "not \(names.joined(separator: " "))"
        }

        private func receives(_ name: String) -> Bool {
            CompileToggle.receives(state: .on, rule: rule(chapter.name),
                                   harness: name)
        }

        var body: some View {
            let position = check.uniqueChapters.firstIndex(of: chapter).map { $0 + 1 } ?? 0
            let base = (chapter.name as NSString).lastPathComponent
            let folder = (chapter.name as NSString).deletingLastPathComponent
            let greyed = harness.map { !receives($0) } ?? false
            HStack(spacing: 8) {
                Text("\(position)").font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .frame(width: 16, alignment: .trailing)
                Text(base).font(Theme.nav)
                    .foregroundStyle(greyed ? Theme.textMuted : Theme.textNav)
                    .lineLimit(1)
                if !folder.isEmpty {
                    Text(folder).font(Theme.caption).foregroundStyle(Theme.textMuted)
                        .lineLimit(1).truncationMode(.head)
                }
                if let ruleText, harness == nil {
                    Text(ruleText).font(Theme.caption).foregroundStyle(Theme.textMuted)
                }
                Spacer(minLength: 4)
                if let harness {
                    TriBox(tri: receives(harness) ? .all : .none) {
                        toggleHarness(harness)
                    }
                } else {
                    ForEach(harnesses, id: \.self) { name in
                        HarnessChip(name: name, gets: receives(name),
                                    width: column) {
                            toggleHarness(name)
                        }
                    }
                }
            }
            .opacity(greyed ? 0.5 : 1)
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(hovering ? Theme.hover : .clear,
                        in: .rect(cornerRadius: Theme.controlRadius))
            .onHover { hovering = $0 }
            .hoverTip(harness == nil ? "Drag to reorder; click a chip to toggle that harness"
                      : "Drag to reorder; the checkbox toggles this harness",
                      replacesLabel: false)
            .contextMenu {
                Button("Move to Trash", role: .destructive) {
                    Task {
                        onError(await app.chapters.delete(
                            file: chapter.name, swe: app.swe))
                    }
                }
            }
        }

        private func toggleHarness(_ name: String) {
            let want = !receives(name)
            Task {
                onError(await app.chapters.setReceipt(
                    name: chapter.name,
                    harness: name, want: want, swe: app.swe))
            }
        }
    }

    /// One harness chip in an Order matrix cell: the mark at full opacity
    /// on a subtle background when the harness gets the chapter, faded and
    /// unbacked when it skips; clicking toggles.
    private struct HarnessChip: View {
        var name: String
        var gets: Bool
        var width: CGFloat
        var toggle: () -> Void

        var body: some View {
            Button(action: toggle) {
                CompileView.markIcon(name, size: 11)
                    .foregroundStyle(gets ? Theme.textPrimary : Theme.textMuted)
                    .padding(.vertical, 2)
                    .frame(width: width)
                    .background(
                        (gets ? Theme.navActive : .clear),
                        in: .rect(cornerRadius: 4))
                    .opacity(gets ? 1 : 0.3)
            }
            .plainButton()
            .hoverTip("\(HarnessMark(name).name) \(gets ? "gets" : "skips") this chapter",
                      replacesLabel: false)
        }
    }
}

/// The flat-file editing path: the shared AGENTS.md itself, plus the offer
/// to split it into chapters once `swe instructions check` says none exist.
private struct FlatInstructions: View {
    @Environment(AppModel.self) private var app
    var report: InitCheck
    var document: InstructionDocument?

    var body: some View {
        let chapters = app.chapters
        // A loaded, non-chapter check is the only state where converting is
        // meaningful; a failed one is an old swe that cannot build anyway.
        if case .loaded(let check) = chapters.status, !check.indexExists {
            Strip {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Chapters").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                        Text("Split AGENTS.md into ordered sections you edit separately; swe rebuilds the file every harness reads.")
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    if chapters.converting { ProgressView().controlSize(.small) }
                    let unsaved = document?.isDirty == true
                    Button(chapters.converting ? "Converting…" : "Convert to chapters", systemImage: "square.split.2x1") {
                        Task { await chapters.convert(swe: app.swe) }
                    }
                    .secondaryButton()
                    // init folds the file on disk; an unsaved draft in the
                    // editor would never reach the chapter it becomes.
                    .disabled(chapters.converting || unsaved)
                    .hoverTip(unsaved ? AgentsModel.saveFirst
                              : "Runs swe instructions init: your AGENTS.md becomes main.md under ~/.swe/AGENTS_MD.",
                              replacesLabel: false)
                }
                if let error = chapters.convertError {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(Theme.caption).foregroundStyle(Theme.dangerText)
                }
            }
        }
        if let document {
            let linked = report.instructionRows.filter { $0.instructions?.state == .linked }.count
            InstructionEditor(
                document: document,
                title: displayPath(document.url),
                subtitle: linked == 1 ? "Linked into 1 harness" : "Linked into \(linked) harnesses")
        }
    }
}

/// The rail's in-progress chapter drag. Every row measures into `frames`
/// in the "railOutline" coordinate space; the dragged row follows the
/// pointer vertically while the level under it slides open a gap. The
/// drop lands from `target`: a file row is an insertion gap in its level,
/// a folder row is a move-into, the "Top level" strip is the root, and
/// anything else — drifted sideways or over a ghost row — snaps back, so
/// only the pointer's height ever changes where the chapter goes.
///
/// A folder drag nests: a folder row takes it in, a file row means that
/// file's folder, and the strip is the root. Its own parent, itself and
/// its own subfolders are never targets, so a drop there snaps back.
/// A collapsed folder the drag rests on springs open after `springDelay`.
@Observable
final class ChapterRailDrag {
    /// How long a drag rests on a collapsed folder before it opens.
    static let springDelay: Duration = .milliseconds(650)

    enum Target: Equatable {
        case none
        /// The gap before the file at `index` in `level` (display order,
        /// the dragged row already out of it).
        case insert(level: String, index: Int)
        case folder(String)
        case root
    }

    var dragging: String?
    /// The dragged row is a folder: drops nest it rather than reorder.
    var draggingFolder = false
    /// The dragged row's folder ("" for the top level).
    var sourceLevel = ""
    var translation: CGFloat = 0
    var target: Target = .none

    /// Every row's frame in rail space: file, folder and ghost rows alike.
    var frames: [String: CGRect] = [:]
    var folders: Set<String> = []
    /// folder path -> its file rows in the rail's display (index) order.
    var levelFiles: [String: [String]] = [:]
    var fileLevel: [String: String] = [:]
    /// Chapters with an active index entry — the ones a gap can reorder.
    var indexed: Set<String> = []
    /// The "Top level" strip's frame.
    var rootZone: CGRect = .null
    /// The rail content's height: the dragged row's travel clamp.
    var railHeight: CGFloat = 0

    func begin(path: String, tree: [ChapterNode], indexed: Set<String>) {
        guard dragging == nil else { return }
        dragging = path
        self.indexed = indexed
        var files: [String: [String]] = ["": []]
        var level: [String: String] = [:]
        var folders: Set<String> = []
        func walk(_ nodes: [ChapterNode], _ parent: String) {
            for node in nodes {
                if node.isFolder {
                    folders.insert(node.path)
                    files[node.path] = []
                    walk(node.children, node.path)
                } else {
                    files[parent, default: []].append(node.path)
                    level[node.path] = parent
                }
            }
        }
        walk(tree, "")
        levelFiles = files
        fileLevel = level
        self.folders = folders
        draggingFolder = folders.contains(path)
        sourceLevel = draggingFolder
            ? (path.contains("/") ? (path as NSString).deletingLastPathComponent : "")
            : level[path] ?? ""
    }

    /// Reads `target` from the pointer's rail-space position: a file row
    /// opens a gap in its level, a folder row glows, the strip is root,
    /// and rows without a drop meaning (ghosts, missing) refuse.
    func update(point: CGPoint) {
        if draggingFolder {
            target = folderTarget(point: point)
            return
        }
        if let hit = frames.first(where: { $0.value.contains(point) })?.key {
            if folders.contains(hit) {
                target = .folder(hit)
            } else if let level = fileLevel[hit], var others = levelFiles[level] {
                others.removeAll { $0 == dragging }
                var index = others
                    .filter { (frames[$0]?.midY ?? .greatestFiniteMagnitude) < point.y }
                    .count
                if !indexed.contains(dragging ?? "") {
                    // An unindexed file lands at the end of the level's
                    // indexed block, never among the unindexed rows.
                    index = min(index, others.filter { indexed.contains($0) }.count)
                }
                target = level == sourceLevel && !indexed.contains(dragging ?? "")
                    ? .none
                    : .insert(level: level, index: index)
            } else {
                target = .none
            }
        } else {
            target = rootZone.contains(point) ? .root : .none
        }
    }

    /// A folder drag's target: the folder the pointer means — a folder
    /// row itself, a file row's folder, or the strip's top level — unless
    /// that is where the folder already sits or inside the folder itself.
    private func folderTarget(point: CGPoint) -> Target {
        guard let dragging else { return .none }
        let destination: String
        if let hit = frames.first(where: {
            $0.value.contains(point) && $0.key != dragging
                && !ChapterIndex.isUnder($0.key, folder: dragging)
        })?.key {
            if folders.contains(hit) {
                destination = hit
            } else if let level = fileLevel[hit] {
                destination = level
            } else {
                return .none
            }
        } else if rootZone.contains(point) {
            destination = ""
        } else {
            return .none
        }
        if destination == sourceLevel || destination == dragging
            || ChapterIndex.isUnder(destination, folder: dragging) { return .none }
        return destination.isEmpty ? .root : .folder(destination)
    }

    /// The row's slide: the dragged row tracks the pointer (clamped to
    /// the rail), a row in the hovered level springs out of the gap's
    /// way, and anything else stays.
    func yOffset(for path: String) -> CGFloat {
        guard let dragging else { return 0 }
        if path == dragging {
            let frame = frames[path] ?? .zero
            return min(max(translation, -frame.minY), railHeight - frame.maxY)
        }
        guard case .insert(let level, let index) = target,
              fileLevel[path] == level,
              let siblings = levelFiles[level],
              let myIndex = siblings.firstIndex(of: path)
        else { return 0 }
        if let dragIndex = siblings.firstIndex(of: dragging) {
            // Its own level: rows between the slot and the gap close up.
            let heights = siblings.map { (frames[$0]?.height ?? 0) + 2 }
            return RowReorder.offset(of: myIndex, dragged: dragIndex,
                                     target: index, heights: heights)
        }
        // A different level: the gap pushes its rows down by the row's
        // height plus the stack's 2pt spacing.
        return myIndex >= index ? (frames[dragging]?.height ?? 0) + 2 : 0
    }

    /// The drop's answer, if a drag is live; clears the session either way.
    func finish() -> (path: String, target: Target, isFolder: Bool)? {
        guard let dragging else { return nil }
        let done = (dragging, target, draggingFolder)
        self.dragging = nil
        draggingFolder = false
        target = .none
        translation = 0
        return done
    }
}

private struct OpenChapter: Equatable {
    var name: String?
    var status: Loadable<InstructionsCheck>
}

/// Chapter mode: the indented AGENTS_MD outline beside the selected
/// chapter's editor. The rail only navigates and creates — what compiles
/// into AGENTS.md is the Compile page's job.
private struct ChaptersEditor: View {
    @Environment(AppModel.self) private var app
    /// The window's undo manager, handed to the chapter model so ⌘Z
    /// walks back moves (and lands on the same stack as text edits).
    @Environment(\.undoManager) private var undoManager
    var check: InstructionsCheck
    @State private var creating: NewChapterSheet.Initial?
    /// Why the last drag-move was refused, under the rail's header.
    @State private var moveError: String?
    /// The live file drag: targets, gaps and the dragged row's travel.
    @State private var railDrag = ChapterRailDrag()
    /// The outline's search text, keyboard cursor and focus.
    @State private var nav = OutlineNav()
    @FocusState private var outlineFocused: Bool
    /// The selected chapter's document once its path is checked off the
    /// main actor; nil `document` means the path leaves AGENTS_MD.
    @State private var opened: (name: String, document: InstructionDocument?)?

    var body: some View {
        let chapters = app.chapters
        ChapterPaneSplit {
            rail
        } editor: {
            VStack(alignment: .leading, spacing: 12) {
                if let name = chapters.selected {
                    if let document = chapters.openDocument(named: name)
                        ?? (opened?.name == name ? opened?.document : nil) {
                        ChapterTargetsStrip(name: name, check: check)
                        InstructionEditor(
                            document: document,
                            title: name,
                            subtitle: subtitle(for: name)) {
                            Task { await chapters.build(swe: app.swe) }
                        }
                        .id(name)
                    } else if opened?.name == name {
                        Card(title: name, subtitle: "The path leaves AGENTS_MD") {
                            EmptyView()
                        } content: {
                            Text("This file sits outside the chapter folder. Fix or remove it in Finder.")
                                .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                        }
                    }
                } else {
                    Card(title: "No chapters", subtitle: "AGENTS_MD has no .md files yet") {
                        EmptyView()
                    } content: {
                        Text("Create a chapter to start the shared instruction file.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(maxHeight: .infinity)
        // A pick, or a reload that may have dropped the document (a move
        // or rename): an open document comes back without a disk call.
        .task(id: OpenChapter(name: chapters.selected, status: chapters.status)) {
            guard let name = chapters.selected else { opened = nil; return }
            let chapter = InstructionsCheck.Chapter(name: name, path: check.directory + "/" + name,
                                                    exists: true)
            let document = await chapters.opening(chapter)
            guard !Task.isCancelled else { return }
            opened = (name, document)
        }
        .cardSheet(item: $creating) { initial in
            NewChapterSheet(initial: initial)
        }
    }

    /// "Chapter N of M in AGENTS.md", or why this file isn't compiled.
    private func subtitle(for name: String) -> String {
        let active = check.chapters.map(\.name)
        if let index = active.firstIndex(of: name) {
            return "Chapter \(index + 1) of \(active.count) in AGENTS.md"
        }
        return "Not in AGENTS.md"
    }

    /// The AGENTS_MD outline: folders first, files after, each level
    /// alphabetical. Select a file to edit it.
    private var rail: some View {
        let chapters = app.chapters
        return Strip {
            HStack(spacing: 6) {
                // Whole at the pane's narrowest: "+N not compiled" gives
                // way first.
                Text("Chapters").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).fixedSize()
                // The count is the compiled chapters, the same M as the
                // editor's "Chapter N of M"; files index.txt leaves out
                // (the folder's README.md, say) are counted apart.
                let compiledCount = check.chapters.count
                let uncompiled = uncompiledFileCount(chapters.tree)
                Text("\(compiledCount)").font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .lineLimit(1).fixedSize()
                    .hoverTip("\(compiledCount) chapters compiled into AGENTS.md", replacesLabel: false)
                if uncompiled > 0 {
                    Text("+\(uncompiled) not compiled")
                        .font(Theme.caption).foregroundStyle(Theme.textMuted)
                        .lineLimit(1)
                }
                Spacer(minLength: 4)
                // One click opens the sheet; a tip on the button used to
                // eat the first click while it dismissed.
                Button {
                    creating = NewChapterSheet.Initial(folder: "")
                } label: {
                    Image(systemName: "plus").font(Theme.font(size: 12))
                        .foregroundStyle(Theme.textMuted)
                        .frame(width: 22, height: 22).contentShape(.rect)
                }
                .plainButton()
                .accessibilityLabel("New chapter")
            }
            if let moveError = moveError ?? chapters.moveError {
                Label(moveError, systemImage: "exclamationmark.triangle")
                    .font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            OutlineSearchBar(nav: nav) {
                outlineFocused = true
            }
            let shown = ChapterOutline.filter(chapters.tree, query: nav.query)
            ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    if nav.searching && shown.isEmpty {
                        OutlineNoMatches(query: nav.query)
                    }
                    ForEach(shown) { node in
                        OutlineNode(node: node, depth: 0, compiled: check.compiledNames,
                                    rail: railDrag, nav: nav,
                                    onNewChapter: { folder in
                                        creating = NewChapterSheet.Initial(folder: folder)
                                    },
                                    onDrop: finishDrop,
                                    onDelete: delete)
                    }
                    // Dirty drafts whose files vanished: their text is
                    // still in the editor, so the row stays selectable.
                    ForEach(chapters.ghosts.filter(matchesFilter), id: \.self) { name in
                        GhostRow(name: name, rail: railDrag, nav: nav)
                    }
                    ForEach(chapters.missing.filter(matchesFilter), id: \.self) { name in
                        GhostRow(name: name, missing: true,
                                 absent: chapters.absent.contains(name), rail: railDrag, nav: nav)
                    }
                    // Where a drop moves to the top level: only while a
                    // drag is live, so an accidental sideways drift can
                    // never reparent — the strip must be aimed for.
                    if railDrag.dragging != nil {
                        let targeted = railDrag.target == .root
                        Text("Top level")
                            .font(Theme.caption)
                            .foregroundStyle(targeted ? Theme.textPrimary : Theme.textMuted)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 5)
                            .background(targeted ? Theme.dropTarget : .clear,
                                        in: .rect(cornerRadius: Theme.controlRadius))
                            .overlay(
                                RoundedRectangle(cornerRadius: Theme.controlRadius)
                                    .strokeBorder(targeted ? Theme.borderHover : Theme.border,
                                                  style: StrokeStyle(dash: [4])))
                            .onGeometryChange(for: CGRect.self) {
                                $0.frame(in: .named("railOutline"))
                            } action: { railDrag.rootZone = $0 }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .coordinateSpace(name: "railOutline")
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
                    railDrag.railHeight = $0
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .outlineKeyboard(nav: nav, tree: shown, selected: chapters.selected,
                             extra: (chapters.ghosts + chapters.missing).filter(matchesFilter),
                             focus: $outlineFocused) { chapters.selected = $0 }
            // A click anywhere in the outline hands it the keyboard.
            .simultaneousGesture(TapGesture().onEnded { outlineFocused = true })
            .onChange(of: nav.cursor) { _, path in
                guard let path else { return }
                withAnimation(.easeInOut(duration: Theme.fadeDuration)) { proxy.scrollTo(path) }
            }
            }
        }
        .frame(maxHeight: .infinity)
        .onAppear { app.chapters.undoManager = undoManager }
        .onChange(of: undoManager) { _, manager in
            app.chapters.undoManager = manager
        }
    }

    /// Whether a draft row's name passes the outline filter.
    private func matchesFilter(_ name: String) -> Bool {
        !nav.searching || name.localizedCaseInsensitiveContains(
            nav.query.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// A file row dropped on a folder row or the top-level strip: the
    /// model moves the file and its index entry.
    private func move(file: String, toFolder folder: String) {
        Task {
            moveError = await app.chapters.move(file: file, toFolder: folder,
                                                swe: app.swe)
        }
    }

    /// A folder row dropped on another folder or the top-level strip: the
    /// model nests the directory and retargets every chapter under it.
    private func move(folder: String, toFolder parent: String) {
        Task {
            moveError = await app.chapters.move(folder: folder, toFolder: parent,
                                                swe: app.swe)
        }
    }

    /// The drag ends: a folder row or the strip is a plain move, a gap in
    /// the dragged file's own level is an index reorder, and a gap in a
    /// different level moves the file there at that index position.
    private func finishDrop() {
        let dropped = withAnimation(.spring(response: 0.28, dampingFraction: 0.86)) {
            railDrag.finish()
        }
        guard let (path, target, isFolder) = dropped else { return }
        if isFolder {
            switch target {
            case .root: move(folder: path, toFolder: "")
            case .folder(let folder): move(folder: path, toFolder: folder)
            case .none, .insert: break
            }
            return
        }
        switch target {
        case .none:
            break
        case .root:
            move(file: path, toFolder: "")
        case .folder(let folder):
            move(file: path, toFolder: folder)
        case .insert(let level, let index):
            var others = railDrag.levelFiles[level] ?? []
            others.removeAll { $0 == path }
            let before = index < others.count ? others[index] : nil
            if level == railDrag.sourceLevel {
                Task {
                    await app.chapters.move(chapter: path, before: before,
                                            swe: app.swe)
                }
            } else {
                Task {
                    moveError = await app.chapters.move(
                        file: path, toFolder: level, before: before, swe: app.swe)
                }
            }
        }
    }

    /// The row's Move to Trash; the model drops its slot and rule first.
    private func delete(_ node: ChapterNode) {
        Task {
            moveError = node.isFolder
                ? await app.chapters.delete(folder: node.path, swe: app.swe)
                : await app.chapters.delete(file: node.path, swe: app.swe)
        }
    }

    /// A rail row for a dirty draft whose file is gone: the unsaved text
    /// stays reachable so Save/Revert keep working.
    private struct GhostRow: View {
        var name: String
        /// Listed in index.txt but not in the outline, rather than a
        /// deleted draft; `absent` when its file isn't on disk at all.
        var missing = false
        var absent = false
        /// The live rail drag; the row measures in so it isn't a target.
        var rail: ChapterRailDrag
        var nav: OutlineNav
        @Environment(AppModel.self) private var app
        @State private var hovering = false

        var body: some View {
            let chapters = app.chapters
            let selected = chapters.selected == name
            HStack(spacing: 6) {
                Text(missing ? name : (name as NSString).lastPathComponent)
                    .font(Theme.nav)
                    .lineLimit(1).truncationMode(.head)
                    .foregroundStyle(selected ? Theme.textPrimary : Theme.textMuted)
                if !missing {
                    Circle().fill(Theme.warning)
                        .frame(width: Theme.statusDot, height: Theme.statusDot)
                }
                Text(!missing ? "deleted" : absent ? "missing" : "hidden").font(Theme.caption)
                    .foregroundStyle(missing && absent ? Theme.dangerText : Theme.textMuted)
                Spacer(minLength: 4)
            }
            .padding(.leading, 23)
            .padding(.trailing, 8).padding(.vertical, 5)
            .background(selected ? Theme.navActive : hovering ? Theme.hover : .clear,
                        in: .rect(cornerRadius: Theme.controlRadius))
            .contentShape(.rect)
            .onTapGesture { nav.cursor = name; chapters.selected = name }
            .onHover { hovering = $0 }
            .modifier(OutlineDraftCursor(nav: nav, name: name))
            .onGeometryChange(for: CGRect.self) {
                $0.frame(in: .named("railOutline"))
            } action: { rail.frames[name] = $0 }
            .onDisappear { rail.frames[name] = nil }
            .hoverTip(!missing ? "The file is gone but the unsaved text is still here; Save writes it back"
                      : absent ? "index.txt lists this chapter but the file is gone; Save creates it"
                      : "A listed chapter the outline skips (a hidden name or a linked folder)",
                      replacesLabel: false)
        }
    }

    /// Files in the outline index.txt leaves out. The compiled total is
    /// the check's own list (the editor's M), since the outline hides some
    /// files the index can still name.
    private func uncompiledFileCount(_ nodes: [ChapterNode]) -> Int {
        let compiled = check.compiledNames
        return nodes.reduce(0) {
            $0 + ($1.isFolder ? uncompiledFileCount($1.children) : (compiled.contains($1.path) ? 0 : 1))
        }
    }

    /// One outline row: a disclosure folder or a selectable, draggable file.
    private struct OutlineNode: View {
        var node: ChapterNode
        var depth: Int
        var compiled: Set<String>
        /// The rail's live drag session: targets, gaps and row travel.
        var rail: ChapterRailDrag
        var nav: OutlineNav
        var onNewChapter: (String) -> Void
        /// The drop ended; the rail's `target` says what it meant.
        var onDrop: () -> Void
        /// The context menu's Move to Trash; the node says file or folder.
        var onDelete: (ChapterNode) -> Void

        @Environment(AppModel.self) private var app
        @State private var hovering = false
        @AppStorage private var storedExpanded: Bool
        @Environment(\.accessibilityReduceMotion) private var reduceMotion

        /// A filter holds every folder open without touching the stored
        /// state, so clearing it restores the folders as they were.
        private var expanded: Bool {
            get { nav.searching || storedExpanded }
            nonmutating set { if !nav.searching { storedExpanded = newValue } }
        }

        init(node: ChapterNode, depth: Int, compiled: Set<String>,
             rail: ChapterRailDrag, nav: OutlineNav,
             onNewChapter: @escaping (String) -> Void,
             onDrop: @escaping () -> Void,
             onDelete: @escaping (ChapterNode) -> Void) {
            self.node = node
            self.depth = depth
            self.compiled = compiled
            self.rail = rail
            self.nav = nav
            self.onNewChapter = onNewChapter
            self.onDrop = onDrop
            self.onDelete = onDelete
            // Expanded by default; each folder's state is its own key.
            _storedExpanded = AppStorage(wrappedValue: true,
                                   ChapterFolderState.outlinePrefix + node.path)
        }

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                if node.isFolder { folderRow } else { fileRow }
                // A dragged folder travels as its row alone: its children
                // fold away so they can't be aimed at or drawn over.
                if node.isFolder && expanded && rail.dragging != node.path {
                    ForEach(node.children) { child in
                        OutlineNode(node: child, depth: depth + 1,
                                    compiled: compiled, rail: rail, nav: nav,
                                    onNewChapter: onNewChapter,
                                    onDrop: onDrop, onDelete: onDelete)
                    }
                }
            }
            // Above its later siblings, which would otherwise draw over
            // the dragged row as it travels down.
            .zIndex(rail.dragging == node.path ? 1 : 0)
            .id(node.path)
        }

        /// The keyboard cursor's ring, only while the outline has focus.
        private var cursorRing: Bool { nav.focused && nav.cursor == node.path }

        /// The row's drag: a file reorders or moves, a folder nests.
        private var railGesture: some Gesture {
            DragGesture(minimumDistance: 3, coordinateSpace: .named("railOutline"))
                .onChanged { value in
                    rail.begin(path: node.path, tree: app.chapters.tree,
                               indexed: compiled)
                    rail.translation = value.translation.height
                    rail.update(point: value.location)
                }
                .onEnded { value in
                    rail.update(point: value.location)
                    onDrop()
                }
        }

        private var folderRow: some View {
            /// The pointer is over this folder mid-drag: it glows and
            /// grows a little so the row shift marks the drop target.
            let dropTargeted = rail.target == .folder(node.path)
            let isDragged = rail.dragging == node.path
            let landing = rail.landingFolder == node.path
            return HStack(spacing: 5) {
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(Theme.font(size: 9, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 10)
                Image(systemName: "folder")
                    .font(Theme.font(size: 12))
                    .foregroundStyle(Theme.textSecondary)
                Text(node.name).font(Theme.nav).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.tail)
                Text("\(node.fileCount)").font(Theme.caption).foregroundStyle(Theme.textMuted)
                Spacer(minLength: 4)
                if hovering {
                    Button { onNewChapter(node.path) } label: {
                        Image(systemName: "plus")
                            .font(Theme.font(size: 10, weight: .semibold))
                            .foregroundStyle(Theme.textMuted)
                            .frame(width: 18, height: 18).contentShape(.rect)
                    }
                    .plainButton(cornerRadius: Theme.controlRadius)
                    .hoverTip("New chapter here", replacesLabel: false)
                }
                OutlineGrip(visible: hovering && !nav.searching)
            }
            .padding(.leading, CGFloat(depth) * 14 + 8)
            .padding(.trailing, 8)
            .padding(.vertical, dropTargeted ? 6 : 4)
            .background {
                if isDragged {
                    RoundedRectangle(cornerRadius: Theme.controlRadius)
                        .fill(Theme.card)
                        .shadow(color: .black.opacity(0.25), radius: 8, y: 2)
                } else {
                    RoundedRectangle(cornerRadius: Theme.controlRadius)
                        .fill(dropTargeted ? Theme.dropTarget : hovering ? Theme.hover : .clear)
                }
            }
            .overlay {
                // The folder a drop will land in: aimed at, or a gap in
                // one of its levels.
                RoundedRectangle(cornerRadius: Theme.controlRadius)
                    .strokeBorder(dropTargeted || landing ? Theme.accentFill : .clear, lineWidth: 1)
            }
            .animation(reduceMotion ? nil : .easeInOut(duration: Theme.fadeDuration), value: landing)
            .focusRing(cursorRing, cornerRadius: Theme.controlRadius)
            .contentShape(.rect)
            .onTapGesture { nav.cursor = node.path; expanded.toggle() }
            .onHover { hovering = rail.dragging == nil && $0 }
            // Moves resolve against the whole tree, so a filtered outline
            // (rows missing) can't be dragged.
            .gesture(railGesture, including: nav.searching ? .subviews : .all)
            .offset(y: rail.yOffset(for: node.path))
            .animation(.easeInOut(duration: Theme.fadeDuration), value: dropTargeted)
            // Spring loading: a drag resting on this collapsed folder
            // opens it, so a drop can reach the rows inside.
            .task(id: dropTargeted && !expanded) {
                guard dropTargeted && !expanded else { return }
                try? await Task.sleep(for: ChapterRailDrag.springDelay)
                guard !Task.isCancelled, rail.target == .folder(node.path) else { return }
                withAnimation(.easeInOut(duration: Theme.fadeDuration)) { expanded = true }
            }
            .onGeometryChange(for: CGRect.self) {
                $0.frame(in: .named("railOutline"))
            } action: { rail.frames[node.path] = $0 }
            .onDisappear { rail.frames[node.path] = nil }
            .contextMenu {
                Button("New chapter here…") { onNewChapter(node.path) }
                Divider()
                Button("Reveal in Finder") {
                    reveal(node)
                }
                Divider()
                Button("Move to Trash", role: .destructive) {
                    onDelete(node)
                }
            }
        }

        private var fileRow: some View {
            let chapters = app.chapters
            let selected = chapters.selected == node.path
            let isCompiled = compiled.contains(node.path)
            let isDragged = rail.dragging == node.path
            let insertionEdge = rail.insertionEdge(for: node.path)
            return HStack(spacing: 6) {
                Text(node.name)
                    .font(Theme.nav)
                    .lineLimit(1).truncationMode(.tail)
                    .foregroundStyle(isCompiled
                        ? (selected ? Theme.textPrimary : Theme.textNav)
                        : Theme.textMuted)
                if chapters.isDirty(node.path) {
                    Circle().fill(Theme.warning)
                        .frame(width: Theme.statusDot, height: Theme.statusDot)
                }
                Spacer(minLength: 4)
                // Muted text alone didn't say why the row differs.
                if !isCompiled {
                    Text("Not compiled").font(Theme.caption).foregroundStyle(Theme.textMuted)
                        .lineLimit(1).fixedSize()
                }
                OutlineGrip(visible: hovering && !nav.searching)
            }
            .padding(.leading, CGFloat(depth) * 14 + 23)
            .padding(.trailing, 8).padding(.vertical, 5)
            .background {
                if isDragged {
                    RoundedRectangle(cornerRadius: Theme.controlRadius)
                        .fill(Theme.card)
                        .shadow(color: .black.opacity(0.25), radius: 8, y: 2)
                } else {
                    RoundedRectangle(cornerRadius: Theme.controlRadius)
                        .fill(selected ? Theme.navActive : hovering ? Theme.hover : .clear)
                }
            }
            .contentShape(.rect)
            .onTapGesture { nav.cursor = node.path; chapters.selected = node.path }
            .onHover { hovering = rail.dragging == nil && $0 }
            .gesture(railGesture, including: nav.searching ? .subviews : .all)
            .overlay(alignment: insertionEdge == .bottom ? .bottom : .top) {
                if insertionEdge != nil {
                    OutlineInsertionLine().offset(y: insertionEdge == .bottom ? 2 : -2)
                }
            }
            .animation(reduceMotion ? nil : .easeInOut(duration: Theme.fadeDuration),
                       value: insertionEdge == nil)
            .focusRing(cursorRing, cornerRadius: Theme.controlRadius)
            .offset(y: rail.yOffset(for: node.path))
            .zIndex(isDragged ? 1 : 0)
            .animation(isDragged ? nil : .spring(response: 0.28, dampingFraction: 0.86),
                       value: rail.target)
            .onGeometryChange(for: CGRect.self) {
                $0.frame(in: .named("railOutline"))
            } action: { rail.frames[node.path] = $0 }
            .onDisappear { rail.frames[node.path] = nil }
            .hoverTip(isCompiled ? node.path
                      : "Not in AGENTS.md. Include it on the Compile page",
                      replacesLabel: false, pinsOnClick: false,
                      enabled: rail.dragging == nil)
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
            .accessibilityAction(.default) { chapters.selected = node.path }
            .contextMenu {
                Button("Reveal in Finder") { reveal(node) }
                Button("Copy path") {
                    copyToPasteboard(absolutePath(node))
                }
                Divider()
                Button("Move to Trash", role: .destructive) {
                    onDelete(node)
                }
            }
        }

        private func absolutePath(_ node: ChapterNode) -> String {
            guard case .loaded(let check) = app.chapters.status else { return node.path }
            return check.directory + "/" + node.path
        }

        private func reveal(_ node: ChapterNode) {
            NSWorkspace.shared.selectFile(
                absolutePath(node), inFileViewerRootedAtPath: "")
        }
    }
}

/// The New chapter / New folder popup: both fields always, the file field
/// deciding which thing is made.
private struct NewChapterSheet: View {
    /// How the sheet was opened; a folder path prefills the Folder field.
    /// The sheet itself creates folders too: leave File empty and the
    /// submit button reads "Create folder".
    struct Initial: Identifiable {
        var folder: String
        var id = UUID()
    }

    var initial: Initial
    @Environment(AppModel.self) private var app
    @Environment(\.dismissSheet) private var dismiss
    @State private var folder: String
    @State private var file = ""
    @State private var include = true
    @State private var working = false
    @State private var error: String?
    /// Which field's suggestions are showing; nil while neither has focus.
    @FocusState private var focus: Field?
    /// The field whose list is showing. Focus alone can't gate it: a row
    /// click blurs the field before the tap lands, which would drop the
    /// list out from under the cursor. So this latches on focus and
    /// clears only when focus moves to the other field.
    @State private var activeField: Field?
    /// Row the keyboard is on; resets whenever the rows change.
    @State private var highlight = 0
    /// Which field's list is open; nil when closed. Not derived from the
    /// rows: the list opens only on typing (after a short debounce) or
    /// on Down Arrow, and closes on Escape, an outside click, an empty
    /// field, or a pick.
    @State private var openField: Field?
    /// The pending "open 250ms after typing stopped" task; each
    /// keystroke cancels the previous one.
    @State private var openTask: Task<Void, Never>?
    /// A pick that writes the field (a chosen folder) must not trigger
    /// the text-change open for it.
    @State private var suppressOpen = false
    /// The card's own bounds (in the "newChapterSheet" space) and each
    /// field's bottom, so the floating list's height is measured, not
    /// hardcoded. 8pt stays clear of the card edge.
    @State private var cardHeight: CGFloat = 0
    @State private var folderFieldMaxY: CGFloat = 0
    @State private var fileFieldMaxY: CGFloat = 0

    private var listMaxHeight: CGFloat {
        let fieldMaxY = activeField == .file ? fileFieldMaxY : folderFieldMaxY
        return min(6 * 28, max(0, cardHeight - fieldMaxY - 4 - 8))
    }

    private enum Field { case folder, file }

    init(initial: Initial) {
        self.initial = initial
        _folder = State(initialValue: initial.folder)
    }

    private var folderProblem: String? {
        ChapterIndex.folderProblem(folder.trimmingCharacters(in: .whitespaces))
    }

    private var fileName: String {
        var name = file.trimmingCharacters(in: .whitespaces)
        if !name.isEmpty && !name.lowercased().hasSuffix(".md") { name += ".md" }
        return name
    }

    private var fileProblem: String? {
        if fileName.isEmpty { return nil }  // folder-only create
        return ChapterIndex.newChapterProblem(
            folder: folder.trimmingCharacters(in: .whitespaces), file: file)
    }

    private var fullPath: String {
        let rel = [folder.trimmingCharacters(in: .whitespaces), fileName]
            .filter { !$0.isEmpty }.joined(separator: "/")
        return rel.isEmpty ? "" : "~/.swe/AGENTS_MD/\(rel)"
    }

    private var canSubmit: Bool {
        folderProblem == nil && fileProblem == nil
            && !(folder.trimmingCharacters(in: .whitespaces).isEmpty && fileName.isEmpty)
    }

    /// Rows under the active field; the list shows whenever it is
    /// non-empty and the user has not dismissed it with Escape.
    private var rows: [ChapterSuggest.Row] {
        switch activeField {
        case .folder:
            return ChapterSuggest.folders(
                folder, in: app.chapters.folders, files: app.chapters.files)
        case .file:
            return ChapterSuggest.files(
                file, folder: folder.trimmingCharacters(in: .whitespaces),
                in: app.chapters.files, folders: app.chapters.folders)
        case nil: return []
        }
    }

    private var listOpen: Bool {
        openField != nil && openField == activeField && !rows.isEmpty
    }

    /// Down on a closed list opens it at once (an empty field lists
    /// everything); on an open list the arrows move the highlight.
    private func move(_ delta: Int) -> KeyPress.Result {
        guard listOpen else {
            guard delta > 0, let field = activeField else { return .ignored }
            openTask?.cancel()
            highlight = 0
            openField = field
            return .handled
        }
        highlight = min(max(highlight + delta, 0), rows.count - 1)
        return .handled
    }

    /// Return picks the highlighted row when the list is open; closed,
    /// it defers to the Create button's default action.
    private func pickHighlighted() -> KeyPress.Result {
        guard listOpen, rows.indices.contains(highlight) else { return .ignored }
        pick(rows[highlight])
        return .handled
    }

    private func escape() -> KeyPress.Result {
        if listOpen {
            openField = nil
            return .handled
        }
        return .ignored  // falls through to the Cancel shortcut
    }

    private func pick(_ row: ChapterSuggest.Row) {
        switch row {
        case .folder(let path):
            // Writing the field is a pick, not typing: it must not
            // reopen this list through the text-change path. A pick of
            // what is already typed fires no onChange, so leave the
            // flag off or it would swallow the next real keystroke.
            if folder != path { suppressOpen = true }
            folder = path
            openField = nil
            focus = .file
            activeField = .file
        case .file(let path):
            // Same select path the rail's file rows use; the sheet only
            // opens from Edit, which already has the editor up.
            app.chapters.selected = path
            dismiss()
        case .create(let path):
            openField = nil
            if activeField == .folder {
                focus = .file  // keep the typed folder, move on to the name
                activeField = .file
            } else {
                folder = activeField == .folder ? path : folder
                guard canSubmit, !working else { return }
                submit()
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New chapter")
                .font(Theme.sectionTitle).tracking(-0.5)
            Text("Folders group the chapters so you and agents can navigate without reading everything.")
                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
            FormField("Folder") {
                TextField("engineering/review, or empty for top level", text: $folder)
                    .sweField(monospaced: true)
                    .focused($focus, equals: .folder)
                    .onKeyPress(.upArrow, phases: .down) { _ in move(-1) }
                    .onKeyPress(.downArrow, phases: .down) { _ in move(1) }
                    .onKeyPress(.return, phases: .down) { _ in pickHighlighted() }
                    .onKeyPress(.escape, phases: .down) { _ in escape() }
                    // The list floats below the field; it takes no layout
                    // space, so its height never resizes the card.
                    .onGeometryChange(for: CGFloat.self) {
                        $0.frame(in: .named("newChapterSheet")).maxY
                    } action: { folderFieldMaxY = $0 }
                    .dropdownBelow(isPresented: listOpen && activeField == .folder) {
                        SuggestList(rows: rows, highlight: highlight,
                                    folderField: true,
                                    maxHeight: listMaxHeight) { pick($0) }
                    }
            }
            .zIndex(listOpen && activeField == .folder ? 1 : 0)
            FormField("File") {
                    TextField("name (.md added)", text: $file)
                        .sweField(monospaced: true)
                        .focused($focus, equals: .file)
                        .onKeyPress(.upArrow, phases: .down) { _ in move(-1) }
                        .onKeyPress(.downArrow, phases: .down) { _ in move(1) }
                        .onKeyPress(.return, phases: .down) { _ in pickHighlighted() }
                        .onKeyPress(.escape, phases: .down) { _ in escape() }
                        .onGeometryChange(for: CGFloat.self) {
                            $0.frame(in: .named("newChapterSheet")).maxY
                        } action: { fileFieldMaxY = $0 }
                        .dropdownBelow(isPresented: listOpen && activeField == .file) {
                            SuggestList(rows: rows, highlight: highlight,
                                        folderField: false,
                                        maxHeight: listMaxHeight) { pick($0) }
                        }
                }
                .zIndex(listOpen && activeField == .file ? 1 : 0)
            // Only a chapter goes in the index; a folder alone does not.
            // Always visible so the card keeps its height.
            Toggle("Include in AGENTS.md", isOn: $include)
                .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                .toggleStyle(.checkbox)
                .disabled(fileName.isEmpty)
            // One line tall whether it carries a problem, a path preview,
            // or only the placeholder.
            Text(folderProblem ?? fileProblem
                 ?? (fullPath.isEmpty ? "~/.swe/AGENTS_MD/" : fullPath))
                .font(Theme.caption)
                .foregroundStyle(folderProblem ?? fileProblem != nil
                                 ? Theme.dangerText : Theme.textMuted)
                .lineLimit(1).truncationMode(.middle)
            if let error {
                Text(error).font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .secondaryButton().keyboardShortcut(.cancelAction)
                // No default shortcut while the list is open: Return picks
                // the highlighted row, never both at once.
                if listOpen {
                    Button(working ? "Creating…" : submitTitle) { submit() }
                        .primaryButton()
                        .disabled(working || !canSubmit)
                } else {
                    Button(working ? "Creating…" : submitTitle) { submit() }
                        .primaryButton().keyboardShortcut(.defaultAction)
                        .disabled(working || !canSubmit)
                }
            }
        }
        .sheetScaffold(width: 480)
        // A click on empty card space closes the floating list; rows,
        // fields and buttons still get their own clicks since they sit
        // above the background.
        .background {
            Color.clear.contentShape(.rect)
                .onTapGesture { if listOpen { openField = nil } }
        }
        .coordinateSpace(.named("newChapterSheet"))
        .onGeometryChange(for: CGFloat.self) { $0.size.height }
            action: { cardHeight = $0 }
        .onAppear {
            // A prefilled folder (from a rail "+" click) starts the user
            // on File; otherwise on Folder.
            let field: Field = initial.folder.isEmpty ? .folder : .file
            focus = field
            activeField = field
        }
        .onChange(of: folder) { textChanged(folder, field: .folder) }
        .onChange(of: file) { textChanged(file, field: .file) }
        .onChange(of: focus) {
            // Latch: a blur doesn't close the list; only moving to the
            // other field (or nothing) does.
            if let focus {
                // Moving to the other field closes this one's list for
                // good: coming back needs typing or Down again.
                if focus != activeField { openField = nil }
                activeField = focus
            }
            highlight = 0
            openTask?.cancel()
        }
    }

    /// The open rules: an empty field never shows a list; typing opens
    /// the list 250ms after the last keystroke, still focused; an open
    /// list follows the text live and closes at once on empty.
    private func textChanged(_ text: String, field: Field) {
        highlight = 0
        openTask?.cancel()
        // Consume the pick flag first, so a pick that empties the field
        // can't leave it set for the next real edit.
        let picked = suppressOpen
        suppressOpen = false
        if text.trimmingCharacters(in: .whitespaces).isEmpty {
            openField = nil
            return
        }
        if picked { return }
        guard focus == field, openField != field else { return }
        openTask = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled, self.focus == field else { return }
            self.openField = field
        }
    }

    /// The inline suggestion list under the focused field — inside the
    /// sheet's own layout, so nothing clips it.
    private struct SuggestList: View {
        var rows: [ChapterSuggest.Row]
        var highlight: Int
        /// true when the list is under the Folder field: `.create` rows
        /// read "New folder", `.file` never appears.
        var folderField: Bool
        /// Measured room between the field's bottom and the card edge;
        /// the ScrollView scrolls past it.
        var maxHeight: CGFloat
        var pick: (ChapterSuggest.Row) -> Void

        var body: some View {
            if !rows.isEmpty {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(Array(rows.enumerated()), id: \.element) { index, row in
                                rowView(row, highlighted: index == highlight)
                                    .id(index)
                            }
                        }
                    }
                    .onChange(of: highlight) { proxy.scrollTo(highlight) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .frame(maxHeight: maxHeight, alignment: .top)
                .background(Theme.dialog, in: .rect(cornerRadius: Theme.controlRadius))
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.controlRadius)
                        .strokeBorder(Theme.border, lineWidth: 1))
                .shadow(color: Theme.dialogShadow, radius: 8, y: 4)
            }
        }

        private func rowView(_ row: ChapterSuggest.Row, highlighted: Bool) -> some View {
            let icon: String
            let label: String
            var hint: String?
            var accessibility: String
            switch row {
            case .folder(let path):
                icon = "folder"
                label = path.isEmpty ? "Top level" : path
                accessibility = "Folder \(path.isEmpty ? "top level" : path)"
            case .file(let path):
                icon = "doc.text"
                label = path
                hint = "Open"
                accessibility = "Chapter \(path), open"
            case .create(let path):
                icon = "plus"
                label = folderField ? "New folder \"\(path)\""
                    : "New chapter \"\(path)\""
                hint = "↵ to add"
                accessibility = label
            }
            return Button { pick(row) } label: {
                HStack(spacing: 8) {
                    Image(systemName: icon)
                        .font(Theme.font(size: 11))
                        .foregroundStyle(Theme.textMuted)
                        .frame(width: 14)
                    Text(label)
                        .font(Theme.font(size: 12, design: .monospaced))
                        .foregroundStyle(Theme.textNav)
                        .lineLimit(1).truncationMode(.head)
                    Spacer(minLength: 4)
                    if let hint {
                        Text(hint).font(Theme.caption).foregroundStyle(Theme.textMuted)
                    }
                }
                .padding(.horizontal, 8).padding(.vertical, 5)
                .background(highlighted ? Theme.navActive : .clear,
                            in: .rect(cornerRadius: Theme.controlRadius))
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(accessibility)
            .accessibilityAddTraits(highlighted ? .isSelected : [])
        }
    }

    private var submitTitle: String {
        fileName.isEmpty ? "Create folder" : "Create chapter"
    }

    private func submit() {
        working = true
        error = nil
        let folder = self.folder.trimmingCharacters(in: .whitespaces)
        let file = fileName
        Task {
            let problem: String?
            if file.isEmpty {
                problem = await app.chapters.createFolder(folder, swe: app.swe)
            } else {
                problem = await app.chapters.createChapter(
                    folder: folder, file: file, include: include, swe: app.swe)
            }
            working = false
            if let problem { error = problem } else { dismiss() }
        }
    }
}

/// The Edit tab's "View AGENTS.md" button: the compiled output's state
/// as a dot, opening the centre popup.
private struct ViewCompiledButton: View {
    /// The output the button opens; nil is the shared AGENTS.md.
    var outputPath: String? = nil
    var onOpenChapter: ((String) -> Void)? = nil

    @Environment(AppModel.self) private var app
    @State private var showing = false

    var body: some View {
        Button {
            showing = true
        } label: {
            HStack(spacing: 6) {
                Circle().fill(stateColor)
                    .frame(width: Theme.statusDot, height: Theme.statusDot)
                Text("View AGENTS.md").font(Theme.bodySmall)
                CompiledSizeLabel(path: outputPath)
            }
        }
        .secondaryButton()
        .hoverTip(stateText, replacesLabel: false)
        .cardSheet(isPresented: $showing) {
            CompiledOutputSheet(path: outputPath, onOpenChapter: onOpenChapter)
        }
    }

    private var stateColor: Color {
        guard case .loaded(let check) = app.chapters.status else { return Theme.textMuted }
        if check.error != nil { return Theme.danger }
        if check.stale == true || !check.outputExists { return Theme.warning }
        return Theme.success
    }

    private var stateText: String {
        guard case .loaded(let check) = app.chapters.status else { return "Checking…" }
        if let error = check.error { return "Source problem: \(error)" }
        if check.stale == true { return "Stale: the sources changed since the last build" }
        if !check.outputExists { return "Not built yet" }
        return "Current"
    }
}

/// The compiled AGENTS.md as a centre popup: Rich by default, a Raw
/// fallback, one section per source chapter that jumps back to the editor.
private struct CompiledOutputSheet: View {
    /// The generated file to show; nil is the check's shared AGENTS.md.
    var path: String? = nil
    /// A chapter section header was tapped: the page switches to Edit so
    /// the selected chapter's editor is showing when the sheet closes.
    var onOpenChapter: ((String) -> Void)? = nil

    @Environment(AppModel.self) private var app
    @Environment(\.dismissSheet) private var dismiss
    @State private var mode = "rich"

    /// The document for `path`, or the shared output the model keeps.
    private var document: InstructionDocument? {
        guard let path, case .loaded(let check) = app.chapters.status,
              path != check.output else { return app.chapters.output }
        return app.chapters.outputDocument(for: path)
    }

    var body: some View {
        let chapters = app.chapters
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(path.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "AGENTS.md")
                        .font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                    if let shown = path ?? ({
                        if case .loaded(let check) = chapters.status { return check.output }
                        return nil
                    }()) {
                        Text(displayPath(shown))
                            .font(Theme.font(size: 11, design: .monospaced))
                            .foregroundStyle(Theme.textMuted)
                    }
                }
                stateLabel
                CompiledSizeLabel(path: path, detailed: true)
                Spacer(minLength: 8)
                Tabs(options: [("rich", "Rich"), ("raw", "Raw")], selection: $mode,
                     style: .segmented)
                if chapters.building { ProgressView().controlSize(.small) }
                Button(chapters.building ? "Building…" : "Rebuild", systemImage: "arrow.clockwise") {
                    Task { await chapters.build(swe: app.swe) }
                }
                .secondaryButton().disabled(chapters.building)
                .hoverTip("Runs swe instructions build", replacesLabel: false)
                Button("Copy", systemImage: "doc.on.doc") {
                    if let text = document?.text { copyToPasteboard(text) }
                }
                .secondaryButton()
                Button("Close") { dismiss() }
                    .secondaryButton().keyboardShortcut(.cancelAction)
            }
            if case .loaded(let check) = chapters.status, let error = check.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = chapters.buildError {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let note = chapters.buildNote {
                Text(note).font(Theme.caption).foregroundStyle(Theme.textMuted)
            }
            content
        }
        .padding(20)
        .frame(width: 760, height: 620)
        .sheetScaffold(width: 760, padding: 0)
    }

    @ViewBuilder private var content: some View {
        if let document {
            loadedContent(document)
        } else {
            Text("AGENTS.md hasn't been built yet.")
                .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
        }
    }

    @ViewBuilder private func loadedContent(
        _ document: InstructionDocument
    ) -> some View {
        switch document.state {
        case .loading:
            SkeletonLines(rows: 12, framed: false)
        case .failed(let message):
            Label(message, systemImage: "exclamationmark.triangle")
                .foregroundStyle(Theme.dangerText)
        case .ready:
            if mode == "raw" {
                ScrollView {
                    Text(document.text)
                        .font(Theme.font(size: 12, design: .monospaced))
                        .foregroundStyle(Theme.textPrimary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(4)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        // By position: the preamble has no name, and a chapter
                        // index.txt lists twice compiles in twice.
                        ForEach(Array(sections(of: document.text).enumerated()), id: \.offset) { _, section in
                            VStack(alignment: .leading, spacing: 6) {
                                if let name = section.name {
                                    Button {
                                        app.chapters.selected = name
                                        onOpenChapter?(name)
                                        dismiss()
                                    } label: {
                                        Text(name)
                                            .font(Theme.caption)
                                            .foregroundStyle(Theme.link)
                                    }
                                    .plainButton()
                                    .hoverTip("Open this chapter in the editor",
                                              replacesLabel: false)
                                }
                                MarkdownBody(body: section.body)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(4)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private var stateLabel: some View {
        Group {
            if case .loaded(let check) = app.chapters.status {
                if check.error != nil {
                    Label("Source problem", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(Theme.dangerText)
                } else if check.stale == true || !check.outputExists {
                    Label(check.outputExists ? "Stale" : "Not built",
                          systemImage: "exclamationmark.circle")
                        .foregroundStyle(Theme.warningText)
                } else {
                    Label("Current", systemImage: "checkmark.circle")
                        .foregroundStyle(Theme.success)
                }
            }
        }
        .font(Theme.caption)
    }

    /// The generated text split on its Source markers via SWECore.
    private func sections(of text: String) -> [CompiledSection] {
        CompiledSections.split(text)
    }
}

/// Where each harness's instruction file points, from `swe init --check`,
/// as one strip of dots and names so the editor keeps the full width.
private struct LinksStrip: View {
    @Environment(AppModel.self) private var app
    var report: InitCheck
    @State private var confirmingReplace = false

    var body: some View {
        let model = app.agentsMd
        Strip {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text("Harnesses").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                    .hoverTip("From swe init --check", replacesLabel: false)
                FlowLayout(spacing: 14, lineSpacing: 6) {
                    ForEach(report.instructionRows) { row in
                        if let link = row.instructions {
                            LinkRow(name: row.name, link: link)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if report.instructionsNeedInit || model.linking {
                    HStack(spacing: 8) {
                        if model.linking { ProgressView().controlSize(.small) }
                        Button(model.linking ? "Linking…" : "Link with swe init", systemImage: "link") {
                            Task { await model.link(swe: app.swe) }
                        }
                        .primaryButton()
                        .disabled(model.linking)
                        .hoverTip("Runs swe init, which links what is missing and never replaces a real file.", replacesLabel: false)
                    }
                    .alignmentGuide(.firstTextBaseline) { $0[VerticalAlignment.center] + 4 }
                }
            }
            if report.instructionRows.contains(where: { $0.instructions?.state.isBlocked == true }) {
                HStack(spacing: 12) {
                    Text("A real file is in the way for some harnesses. swe init leaves those alone.")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button("Replace and back up…") { confirmingReplace = true }
                        .secondaryButton()
                        .disabled(model.linking || model.projectHasUnsavedChanges)
                        .hoverTip(model.projectHasUnsavedChanges ? AgentsModel.saveFirst
                                  : "Runs swe init --force, which copies each real file in the way to ~/.swe/backups before linking over it.",
                                  replacesLabel: false)
                }
            }
            if let error = model.linkError {
                // swe init can fail with pages of output; bounded, so the
                // editor below keeps its room on this non-scrolling page.
                ScrollView {
                    Text(error).font(Theme.caption).foregroundStyle(Theme.dangerText)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 64)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
        .confirmationDialog("Replace the real files with links?", isPresented: $confirmingReplace) {
            Button("Replace and back up", role: .destructive) {
                Task { await model.link(swe: app.swe, force: true) }
            }
        } message: {
            Text("This runs swe init --force. Each real file or folder in the way is copied to ~/.swe/backups, then replaced with a link to the shared copy.")
        }
    }
}

private struct LinkRow: View {
    var name: String
    var link: InitCheck.Link

    var color: Color {
        if link.state == .linked { return Theme.success }
        if link.state.isBlocked { return Theme.danger }
        if link.state.initFixes { return Theme.warning }
        return Theme.textMuted
    }

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: Theme.statusDot, height: Theme.statusDot)
            HarnessMarkView(harness: name).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
            // Only what needs attention gets words; a dot says linked.
            if link.state != .linked {
                Text(link.state.title.lowercased()).font(Theme.caption).foregroundStyle(Theme.textSecondary)
            }
        }
        .hoverTip("\(HarnessMark(name).name), \(link.state.title.lowercased()): \(displayPath(URL(fileURLWithPath: link.path)))")
        .accessibilityElement(children: .combine)
        // The dot alone says linked, so VoiceOver gets the state in words.
        .accessibilityValue(link.state.title)
    }
}

private struct ProjectInstructions: View {
    static let folderKey = "agentsProjectFolder"

    @Environment(AppModel.self) private var app
    @State private var selectedFile = AgentsModel.projectFileNames[0]
    @State private var pendingFolder: URL?
    /// The selected file's document, opened in a task and never in body:
    /// document(for:) follows symlinks and reads the file on first use.
    @State private var opened: (file: AgentsModel.ProjectFile, document: InstructionDocument)?
    /// Bumped when a link may have moved under an unchanged file entry
    /// (the app comes back to the front, a link run ends), so the task
    /// asks the model again and follows the link to its new target.
    @State private var reopen = 0

    var body: some View {
        let model = app.agentsMd
        VStack(alignment: .leading, spacing: 12) {
            Strip {
                HStack(spacing: 12) {
                    Image(systemName: "folder").foregroundStyle(Theme.textSecondary)
                    if let folder = model.projectFolder {
                        Text(displayPath(folder)).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                            .lineLimit(1).truncationMode(.middle)
                    } else {
                        Text("Choose a project folder to edit its AGENTS.md and CLAUDE.md.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                    }
                    Spacer(minLength: 8)
                    Button("Choose folder…") { choose() }
                        .secondaryButton()
                }
            }
            if model.projectFolder != nil {
                let files = model.projectFiles
                Tabs(options: files.map { ($0.name, tabTitle($0)) }, selection: $selectedFile, style: .table, onPanel: true)
                if let file = files.first(where: { $0.name == selectedFile }),
                   let opened, opened.file == file {
                    InstructionEditor(document: opened.document, title: file.name,
                                      subtitle: subtitle(file, opened.document)) {
                        Task { await model.rescanProject() }
                    }
                    .id(file.name)
                }
            }
        }
        // A new folder, tab or rescan of the file opens its document; the
        // model keeps each one, so a return to a tab keeps its edits.
        .task(id: OpenKey(file: model.projectFolder == nil ? nil : model.projectFiles.first { $0.name == selectedFile },
                          reopen: reopen)) {
            guard model.projectFolder != nil,
                  let file = model.projectFiles.first(where: { $0.name == selectedFile }) else {
                opened = nil
                return
            }
            opened = (file, model.document(for: file))
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            reopen += 1
        }
        .onChange(of: model.linking) { _, linking in if !linking { reopen += 1 } }
        .task { await restore() }
        .confirmationDialog("Discard unsaved changes?", isPresented: .init(
            get: { pendingFolder != nil }, set: { if !$0 { pendingFolder = nil } })) {
            Button("Discard and switch", role: .destructive) {
                if let folder = pendingFolder { open(folder) }
                pendingFolder = nil
            }
        } message: {
            Text("The open project has edits that aren't saved.")
        }
    }

    private struct OpenKey: Equatable {
        var file: AgentsModel.ProjectFile?
        var reopen: Int
    }

    private func tabTitle(_ file: AgentsModel.ProjectFile) -> String {
        if let target = file.linksTo { return "\(file.name) → \(target)" }
        return file.exists ? file.name : "\(file.name) (new)"
    }

    private func subtitle(_ file: AgentsModel.ProjectFile, _ document: InstructionDocument) -> String {
        if let target = file.linksTo { return "Links to \(target); edits land there." }
        if !document.existsOnDisk { return "Doesn't exist yet. Saving creates it." }
        return "In the folder above"
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        panel.directoryURL = app.agentsMd.projectFolder
        guard panel.runModal() == .OK, let url = panel.url else { return }
        if app.agentsMd.projectHasUnsavedChanges {
            pendingFolder = url
        } else {
            open(url)
        }
    }

    private func open(_ folder: URL) {
        Task { await app.agentsMd.openProject(folder) }
        UserDefaults.standard.set(folder.path, forKey: Self.folderKey)
    }

    /// Reopens the last folder. The check runs off the main actor: the
    /// folder may sit on a stale mount or a file provider.
    private func restore() async {
        guard app.agentsMd.projectFolder == nil,
              let path = UserDefaults.standard.string(forKey: Self.folderKey) else { return }
        let isFolder = await offMain {
            var isDirectory: ObjCBool = false
            return FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) && isDirectory.boolValue
        }
        if isFolder, app.agentsMd.projectFolder == nil {
            await app.agentsMd.openProject(URL(fileURLWithPath: path))
        }
    }
}

/// A plain-text editor for one instruction file: save, revert, an unsaved
/// dot, and a stop when the file changed on disk since it was opened.
/// Raw and Rich edit the same text, so nothing is lost switching.
struct InstructionEditor: View {
    private static let modeKey = "agentsMdView"

    @Bindable var document: InstructionDocument
    var title: String
    var subtitle: String
    /// The subtitle is a path: draw it in mono.
    var monoSubtitle = false
    /// View-only: no mode tabs or save controls, just the text.
    var readOnly = false
    /// Code, not prose: always the raw editor, with no Rich tab. A hook
    /// script read as Markdown turns its docstring into paragraphs.
    var rawOnly = false
    /// A Close button in the header, for the editor inside a sheet.
    var close: (() -> Void)? = nil
    var onSaved: () -> Void = {}
    @State private var error: String?
    @FocusState private var editing: Bool
    @State private var mode = UserDefaults.standard.string(forKey: Self.modeKey) ?? "raw"
    @State private var richFocused = false
    @State private var richFocusToken = 0
    /// The card's width, to fold the header's labels away when narrow.
    @State private var width: CGFloat = 0

    /// Icon-only header buttons: the card is too narrow for their labels
    /// beside the title (a 900 pt window with the chapter pane open).
    private var compact: Bool {
        EditorHeader.isCompact(width: Double(width), zoom: TextZoomModel.shared.zoom)
    }

    var body: some View {
        Card(title: title, subtitle: subtitle, monoSubtitle: monoSubtitle) {
            header
        } content: {
            switch document.state {
            case .loading:
                SkeletonLines(rows: 12, framed: false)
                    .padding(Theme.cardPadding)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            case .failed(let message):
                Label(message, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.dangerText)
            case .ready:
                editorContent
            }
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
    }

    /// Mode tabs, the Unsaved dot and the buttons. Labels never wrap: a
    /// narrow card shows the buttons as icons (named by `.help` and
    /// VoiceOver) and the Unsaved dot without its word.
    private var header: some View {
        HStack(spacing: 8) {
            if !readOnly && !rawOnly {
                Tabs(options: [("raw", "Raw"), ("rich", "Rich")], selection: $mode, style: .segmented)
            }
            if document.isDirty {
                HStack(spacing: 5) {
                    Circle().fill(Theme.warning).frame(width: Theme.statusDot, height: Theme.statusDot)
                    if !compact {
                        Text("Unsaved").font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Unsaved")
                .help(compact ? "Unsaved changes" : "")
            }
            if !readOnly {
                headerButton("Revert", icon: "arrow.uturn.backward") {
                    Task { await document.revert(); error = nil }
                }
                .secondaryButton()
                .disabled(!document.isDirty)
                headerButton("Save", icon: "square.and.arrow.down") { save(overwrite: false) }
                    .primaryButton()
                    .keyboardShortcut("s", modifiers: .command)
                    .disabled(!document.isDirty || document.changedOnDisk)
            }
            if let close {
                headerButton("Close", icon: "xmark") { close() }
                    .secondaryButton()
                    .keyboardShortcut(.cancelAction)
            }
        }
        .lineLimit(1)
        .fixedSize()
    }

    /// A header button: its word, or only its icon when `compact`. The
    /// word stays the accessibility label and the tooltip either way.
    private func headerButton(_ title: String, icon: String,
                              action: @escaping () -> Void) -> some View {
        Button(action: action) {
            if compact {
                Label(title, systemImage: icon).labelStyle(.iconOnly)
            } else {
                Text(title)
            }
        }
        .help(compact ? title : "")
        .accessibilityLabel(title)
    }

    /// The ready state: the changed-on-disk banner, a save error, and the
    /// raw or rich editor filling what the card has left.
    @ViewBuilder private var editorContent: some View {
        if document.changedOnDisk {
            ChangedOnDiskBanner(
                reload: { Task { await document.load(); error = nil } },
                overwrite: { save(overwrite: true) })
        }
        if let error {
            Label(error, systemImage: "exclamationmark.triangle")
                .font(Theme.caption).foregroundStyle(Theme.dangerText)
        }
        // Fills the card, and the card fills what the page has left.
        let raw = rawOnly || mode == "raw"
        let focused = raw ? editing : richFocused
        Group {
            if readOnly {
                TextEditor(text: .constant(document.text))
                    .font(Theme.font(size: 13, design: .monospaced))
                    .scrollContentBackground(.hidden)
            } else if raw {
                TextEditor(text: $document.text)
                    .font(Theme.font(size: 13, design: .monospaced))
                    .scrollContentBackground(.hidden)
                    .focused($editing)
            } else {
                MarkdownTextView(text: $document.text, isFocused: $richFocused,
                                 focusToken: richFocusToken, accessibilityLabel: title)
            }
        }
        .disabled(readOnly)
        .focusEffectDisabled()
        .padding(10)
        .background(raw ? Theme.code : Theme.field,
                    in: .rect(cornerRadius: Theme.controlRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius)
            .strokeBorder(focused ? Theme.borderHover : Theme.border, lineWidth: 1))
        .focusRing(focused, cornerRadius: Theme.controlRadius, afterPointer: true)
        .frame(minHeight: 160, maxHeight: .infinity)
        .accessibilityLabel(title)
        .onChange(of: mode) {
            UserDefaults.standard.set(mode, forKey: Self.modeKey)
            if mode == "raw" { editing = true } else { richFocusToken += 1 }
        }
    }

    private func save(overwrite: Bool) {
        Task {
            switch await document.save(overwrite: overwrite) {
            case .saved:
                error = nil
                onSaved()
            case .changedOnDisk:
                error = nil
            case .failed(let message):
                error = message
            }
        }
    }
}

private struct ChangedOnDiskBanner: View {
    var reload: () -> Void
    var overwrite: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle").foregroundStyle(Theme.warningText)
            Text("This file changed on disk since you opened it. Reload to see that version, or overwrite it with yours.")
                .font(Theme.caption).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            Button("Reload", action: reload).secondaryButton()
            Button("Overwrite", action: overwrite).secondaryButton()
        }
        .padding(12)
        .background(Theme.warning.opacity(0.08), in: .rect(cornerRadius: Theme.controlRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.warning.opacity(0.4), lineWidth: 1))
    }
}
