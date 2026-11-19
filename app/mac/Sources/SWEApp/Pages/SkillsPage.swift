import AppKit
import SWECore
import SwiftUI

/// Skills, after TokenTracker's: the skills that stand alone, with the
/// harnesses that load them. Skills shipped inside a plugin are listed with
/// it under Plugins > Explore. Links and unlinks go through `swe skills`,
/// never written from here.
struct SkillsPage: View {
    @Environment(AppModel.self) private var app
    @State private var adding = false

    /// App-wide, so a list outlives a trip to another page and the last
    /// one shows at once.
    private var model: SkillsModel { app.skills }

    /// A skill's detail is on screen (`DetailOpenKey`).
    @State private var detailOpen = false

    var body: some View {
        Page(title: "Skills",
             subtitle: "Every skill on this Mac outside a plugin, and which harnesses load it.",
             back: detailOpen ? ("Back to all skills", { app.navReselects += 1 }) : nil) {
            // The harness links fold into one line in the header, so the
            // first skill sits right under the filter bar.
            if case .loaded(let inventory) = model.inventory {
                HarnessLinksSummary(model: model, sharedDir: inventory.sharedDir)
            }
            Button("New skill", systemImage: "plus") { adding = true }
                .primaryButton()
            Button("Refresh", systemImage: "arrow.clockwise") {
                Task {
                    async let catalog: Void = app.pluginCatalog.load(swe: app.swe, for: app.swePath)
                    await model.load(for: app.swePath)
                    await catalog
                }
            }
            .iconButton()
            .keyboardShortcut("r", modifiers: .command)
            .help("Refresh (⌘R)")
        } content: {
            InstalledSkills(model: model, newSkill: { adding = true }) {
                app.pluginsTab = .explore
                app.selection = .plugins
            }
            .onPreferenceChange(DetailOpenKey.self) { detailOpen = $0 }
        }
        // Both at once: which skills stay here depends on the catalog. The
        // app hands the model each new catalog (AppModel.start).
        .task(id: app.swePath) {
            async let catalog: Void = app.pluginCatalog.loadIfStale(swe: app.swe, for: app.swePath)
            await model.loadIfStale(for: app.swePath)
            await catalog
        }
        .cardSheet(isPresented: $adding) {
            NewSkillSheet()
        }
    }
}

/// Writes ~/.swe/skills/<name>/SKILL.md through `swe skills new`.
private struct NewSkillSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismissSheet) private var dismiss
    @State private var name = ""
    @State private var description = ""
    @State private var error: String?
    @State private var working = false
    @State private var created: String?

    static let nameRule = /^[a-z0-9]+(-[a-z0-9]+)*$/
    var nameProblem: String? {
        if name.isEmpty { return "Lowercase, digits and single hyphens: my-skill" }
        if name.count > 64 { return "At most 64 characters" }
        return name.wholeMatch(of: Self.nameRule) == nil
            ? "Lowercase letters, digits, and single hyphens between them" : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New skill").font(Theme.sectionTitle).tracking(-0.5)
            FormField("Name") {
                TextField("my-skill", text: $name)
                    .sweField(monospaced: true)
                if let nameProblem {
                    Text(nameProblem).font(Theme.caption).foregroundStyle(Theme.textMuted)
                }
            }
            FormField("Description") {
                TextField("When should an agent use this skill?", text: $description)
                    .sweField()
            }
            if let created {
                Text("Created \(displayPath(created))")
                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
            }
            if let error {
                Text(error).font(Theme.caption).foregroundStyle(Theme.dangerText)
            }
            HStack {
                Spacer()
                if let created, let editor = app.editor {
                    Button("Open in \(editor.name)") {
                        NSWorkspace.shared.open([URL(fileURLWithPath: created)],
                                                withApplicationAt: editor.appURL,
                                                configuration: .init())
                    }
                    .secondaryButton()
                }
                Button(created == nil ? "Cancel" : "Done") { dismiss() }
                    .secondaryButton()
                if created == nil {
                    Button("Create") {
                        working = true
                        error = nil
                        Task {
                            do {
                                created = try await app.skills.create(name: name, description: description).path
                            } catch { self.error = error.localizedDescription }
                            working = false
                        }
                    }
                    .primaryButton()
                    .disabled(working || nameProblem != nil)
                }
            }
        }
        .sheetScaffold(width: 420)
    }
}

// MARK: - Installed

private struct InstalledSkills: View {
    @Environment(AppModel.self) private var app
    @Bindable var model: SkillsModel
    var newSkill: () -> Void
    var openPlugins: () -> Void
    /// The skill open in its detail page, nil while the list shows.
    @State private var opened: Skill?

    var body: some View {
        if let opened {
            // A reload swaps the inventory: follow the same folder's row.
            let skill = model.allSkills.first { $0.path == opened.path } ?? opened
            SkillDetailView(skill: skill, home: FileManager.default.homeDirectoryForCurrentUser,
                            onBack: { self.opened = nil },
                            links: AnyView(SkillLinksBlock(skill: skill, model: model)),
                            usage: AnyView(SkillUsageBlock(skill: skill, model: model)))
        } else {
            switch model.inventory {
            case .idle, .loading:
                SkeletonRows(rows: 6, framed: false, shape: .skill, label: "Reading skills…")
            case .failed(let message):
                ErrorCard(message: message)
            case .loaded(let inventory):
                if model.placement == nil {
                    SkeletonRows(rows: 4, framed: false, shape: .skill, label: "Finding the skills that ship inside plugins…")
                } else {
                    list(empty: inventory.skills.isEmpty)
                }
            }
        }
    }

    @ViewBuilder private func list(empty: Bool) -> some View {
        if let error = model.actionError { ErrorCard(message: error) }
        filters(total: model.listedSkills.count)
        if model.pluginSkillCount > 0 { PluginSkillsNote(count: model.pluginSkillCount, open: openPlugins) }
        let skills = model.visibleSkills
        if skills.isEmpty {
            EmptyCard(text: empty ? "No skills on this Mac yet."
                          : model.listedSkills.isEmpty ? "Every skill on this Mac ships inside a plugin."
                          : "No skills match.",
                      icon: "sparkles",
                      actionTitle: empty ? "New skill" : nil, action: empty ? newSkill : nil)
        } else {
            // One row per skill, one column per folder a click can change:
            // the same grid the MCP servers page uses.
            Card { SkillTable(skills: skills, roots: model.tableRoots,
                              model: model) { opened = $0 } }
        }
    }

    /// The page's one filter bar: selects, then search taking the slack,
    /// then the count.
    private func filters(total: Int) -> some View {
        let harnesses = Array(Set(model.listedSkills.flatMap(\.harnesses))).sorted()
        // Plugin skills are under Plugins, so Plugin is offered only for one
        // no catalog plugin claims, which stays here rather than vanish.
        let sources = Skill.Source.allCases.filter { source in model.listedSkills.contains { $0.source == source } }
        return HStack(spacing: 8) {
            // Closed, the menus say "All" so "Harness: All harnesses" never
            // truncates in a 168pt select.
            SelectMenu(label: "Harness",
                       options: [(String?.none, "All harnesses", nil)]
                           + harnesses.map { (String?.some($0), HarnessMark($0).name, $0) },
                       selection: $model.harness,
                       title: model.harness.map { HarnessMark($0).name } ?? "All")
            SelectMenu(label: "Source",
                       options: [(Skill.Source?.none, "All sources")] + sources.map { (Skill.Source?.some($0), $0.title) },
                       selection: $model.source,
                       title: model.source?.title ?? "All")
            SearchField(prompt: "Filter skills", text: $model.query)
                .frame(maxWidth: .infinity)
            Text("\(model.visibleSkills.count) of \(total) skills")
                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                .monospacedDigit()
                .lineLimit(1)
                .fixedSize()
        }
    }
}

/// One line under the filter bar: how many skills ship inside plugins, and
/// a way to them on the Plugins page's Explore tab.
private struct PluginSkillsNote: View {
    var count: Int
    var open: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: open) {
            HStack(spacing: 6) {
                Image(systemName: "puzzlepiece.extension").font(Theme.font(size: 12))
                Text(count == 1 ? "1 skill ships inside a plugin;" : "\(count) skills ship inside plugins;")
                    .monospacedDigit()
                Text(count == 1 ? "see it under Plugins" : "see them under Plugins")
                    .foregroundStyle(hovering ? Theme.textPrimary : Theme.textLabel)
                    .underline(hovering)
                Image(systemName: "arrow.right").font(Theme.font(size: 10, weight: .semibold))
            }
            .font(Theme.caption)
            .foregroundStyle(Theme.textSecondary)
            .contentShape(.rect)
        }
        .plainButton(cornerRadius: Theme.smallRadius)
        .onHover { hovering = $0 }
        .hoverTip("Open Plugins > Explore, where each plugin lists the skills it ships", replacesLabel: false)
    }
}

/// "13 of 14 linked" in the page header. A click opens the harness skills
/// folders, and a folder opens what can be done with it, in the same popover.
private struct HarnessLinksSummary: View {
    var model: SkillsModel
    var sharedDir: String
    @State private var open = false
    @State private var hovering = false

    var body: some View {
        let roots = model.roots
        // A selection root reads shared skills too, just not all of them.
        let linked = roots.filter { $0.link == .linked || $0.link == .selection }.count
        let blocked = roots.contains { $0.link == .blocked }
        Button { open = true } label: {
            HStack(spacing: 6) {
                Image(systemName: "link").font(Theme.font(size: 12)).foregroundStyle(Theme.textSecondary)
                Text("Harness links").foregroundStyle(Theme.textPrimary)
                Text(roots.isEmpty ? "None found" : "\(linked) of \(roots.count) linked")
                    .foregroundStyle(Theme.textSecondary).monospacedDigit()
                if blocked { LinkDot(link: .blocked) }
                Image(systemName: "chevron.down").font(Theme.font(size: 10, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
            }
            .font(Theme.caption)
            .padding(.horizontal, 8)
            .frame(height: Theme.controlHeight)
            .background(hovering || open ? Theme.panelHover : .clear, in: .rect(cornerRadius: Theme.controlRadius))
            .contentShape(.rect(cornerRadius: Theme.controlRadius))
        }
        .plainButton(cornerRadius: Theme.controlRadius)
        .onHover { hovering = $0 }
        .help("Which harnesses load the shared skills")
        .popover(isPresented: $open, arrowEdge: .bottom) {
            HarnessLinksPanel(model: model, sharedDir: sharedDir)
        }
    }
}

/// One chip per harness skills folder; a chip opens its detail in place.
private struct HarnessLinksPanel: View {
    @Environment(AppModel.self) private var app
    var model: SkillsModel
    var sharedDir: String
    @State private var selected: SkillRoot.ID?

    var body: some View {
        let roots = model.roots
        if let id = selected, let root = roots.first(where: { $0.id == id }) {
            VStack(alignment: .leading, spacing: 0) {
                Button("Harness links", systemImage: "chevron.left") { selected = nil }
                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    .plainButton()
                    .padding([.top, .horizontal], 16)
                RootDetail(root: root, model: model) { selected = nil }
            }
            .frame(width: 320, alignment: .leading)
        } else {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Harness links").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                    Text("Each harness reads one skills folder. A linked folder points at \(displayPath(sharedDir)), so every shared skill reaches it.")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if roots.isEmpty {
                    // No harness is known yet, so there is nothing to link;
                    // registering one is the step that helps.
                    HStack(spacing: 10) {
                        Text("No harness skills folders found. Register a harness first.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                        Button("Open Harnesses") { app.selection = .harnesses }
                            .secondaryButton()
                    }
                } else {
                    FlowLayout(spacing: 6, lineSpacing: 6) {
                        ForEach(roots) { root in
                            Button { selected = root.id } label: {
                                LinkChipLabel(name: root.name, link: root.link, busy: model.busy.contains(root.path))
                            }
                            .plainButton(cornerRadius: 999)
                            .hoverTip("\(HarnessMark(root.name).name): \(root.link.title.lowercased())")
                        }
                    }
                    LinkLegend()
                    let (targets, _) = SkillsModel.linkAllTargets(roots)
                    if !targets.isEmpty {
                        Button("Link all (\(targets.count))") {
                            Task { await model.linkAll() }
                        }
                        .secondaryButton()
                        .disabled(!model.busy.isEmpty)
                    }
                }
                if let summary = model.linkAllSummary {
                    Text(summary)
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // A link or unlink started here reports back here too, since
                // the page's own error card is behind the popover.
                if let error = model.actionError {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(Theme.caption).foregroundStyle(Theme.dangerText)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            .padding(16)
            .frame(width: 440, alignment: .leading)
        }
    }
}

private struct RootDetail: View {
    var root: SkillRoot
    var model: SkillsModel
    var done: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                if HarnessIcon.exists(for: root.name) { HarnessIcon(harness: root.name, size: 16) }
                Text(HarnessMark(root.name).name).font(Theme.rowTitle).foregroundStyle(Theme.textPrimary)
                Badge(root.link.title)
            }
            Text(displayPath(root.path))
                .font(Theme.font(size: 12, design: .monospaced))
                .foregroundStyle(Theme.textSecondary)
                .textSelection(.enabled)
            Text(root.explanation)
                .font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                switch root.link {
                case .linked:
                    Button("Unlink") { act { await model.unlink(root) } }
                        .secondaryButton()
                case .selection:
                    Button("Unlink all") { act { await model.unlink(root) } }
                        .secondaryButton()
                    Button("Link all skills") { act { await model.link(root) } }
                        .primaryButton()
                case .notLinked:
                    Button("Link to shared skills") { act { await model.link(root) } }
                        .primaryButton()
                case .blocked:
                    EmptyView()
                }
                Button("Show in Finder") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: root.path)])
                }
                .secondaryButton()
            }
            .disabled(model.busy.contains(root.path))
        }
        .padding(16)
        .frame(width: 320, alignment: .leading)
    }

    private func act(_ work: @escaping () async -> Void) {
        done()
        Task { await work() }
    }
}

/// A capsule with a state dot: green when the harness loads the skills
/// (an "on" state), a hollow ring when it doesn't, amber when swe won't
/// link it.
private struct LinkChipLabel: View {
    var name: String
    var link: SkillRoot.Link
    var busy = false
    var compact = false

    var body: some View {
        HStack(spacing: 6) {
            if busy {
                ProgressView().controlSize(.mini)
            } else {
                LinkDot(link: link)
            }
            HarnessMarkView(harness: name, size: compact ? 12 : 14)
                .font(Theme.font(size: compact ? 11 : 12, weight: .medium))
                .foregroundStyle(link == .linked ? Theme.textPrimary : Theme.textSecondary)
        }
        .padding(.horizontal, compact ? 8 : 10)
        .frame(height: compact ? 22 : 26)
        .background(link == .linked ? Theme.subtle : .clear, in: .capsule)
        .overlay(Capsule().strokeBorder(Theme.border, lineWidth: 1))
        .contentShape(.capsule)
    }
}

private struct LinkDot: View {
    var link: SkillRoot.Link

    var body: some View {
        switch link {
        case .linked: Circle().fill(Theme.accentFill).frame(width: Theme.statusDot, height: Theme.statusDot)
        case .selection: Circle().strokeBorder(Theme.accentFill, lineWidth: 1.2).frame(width: Theme.statusDot, height: Theme.statusDot)
        case .notLinked: Circle().strokeBorder(Theme.textMuted, lineWidth: 1.2).frame(width: Theme.statusDot, height: Theme.statusDot)
        case .blocked: Circle().fill(Theme.warning).frame(width: Theme.statusDot, height: Theme.statusDot)
        }
    }
}

private struct LinkLegend: View {
    var body: some View {
        HStack(spacing: 14) {
            ForEach([SkillRoot.Link.linked, .selection, .notLinked, .blocked], id: \.self) { link in
                HStack(spacing: 6) {
                    LinkDot(link: link)
                    Text(link.title).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                }
            }
            Text("Click one to link or unlink it.")
                .font(Theme.caption).foregroundStyle(Theme.textMuted)
        }
    }
}

/// The skills grid: one row per skill, one column per skills folder a
/// click could still change (skipped and ignored folders stay out). The
/// same mechanics as the MCP servers table: column order is a
/// preference, dragging a header moves the column, a master switch
/// flips a shared skill in every folder at once.
private struct SkillTable: View {
    var skills: [Skill]
    var roots: [SkillRoot]
    var model: SkillsModel
    var open: (Skill) -> Void

    static let nameMinWidth: CGFloat = 220
    static let sourceWidth: CGFloat = 96
    static let cellWidth: CGFloat = 84
    /// One column's footprint for the reorder maths: separator plus cell.
    static let pitch: CGFloat = 1 + cellWidth
    /// Rows the table shows before folding behind a Show more row.
    private static let foldLimit = 20
    /// The UserDefaults key the folder column order persists under.
    static let orderKey = "skillsColumnOrder"

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The folder columns in display order, remembered as a list of
    /// paths (the root id): a dragged column rewrites it, a folder the
    /// save predates appends, and one the model dropped falls away.
    @State private var order = UserDefaults.standard.stringArray(forKey: SkillTable.orderKey) ?? []
    @State private var showAll = false
    /// The card's inner width: the table fills it, and scrolls only past it.
    @State private var available: CGFloat = 0
    /// The header mid-drag and how far the pointer has carried it.
    @State private var dragging: String?
    @State private var translation: CGFloat = 0

    /// The columns in display order: the saved order, then any folder
    /// the save predates, and none the model no longer lists.
    private var ordered: [SkillRoot] {
        let byId = Dictionary(roots.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        return order.compactMap { byId[$0] } + roots.filter { !order.contains($0.id) }
    }

    private var orderedIds: [String] { ordered.map(\.id) }

    /// The widths RowReorder works on: a uniform `pitch` per column.
    private var pitches: [CGFloat] { Array(repeating: Self.pitch, count: ordered.count) }

    /// Everything after the name column: its separator, the Source
    /// column, and every folder column's separator and cell.
    static func fixedTail(columns: Int) -> CGFloat {
        1 + sourceWidth + CGFloat(columns) * pitch
    }

    private var nameWidth: CGFloat {
        max(available - Self.fixedTail(columns: ordered.count), Self.nameMinWidth)
    }

    private var totalWidth: CGFloat { nameWidth + Self.fixedTail(columns: ordered.count) }

    var body: some View {
        ScrollView(.horizontal) {
            VStack(alignment: .leading, spacing: 0) {
                header
                Divider().overlay(Theme.border)
                let shown = RowFold.visible(skills, limit: Self.foldLimit, expanded: showAll)
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, skill in
                    SkillGridRow(skill: skill, roots: ordered, model: model) { open(skill) }
                    if index < shown.count - 1 { Divider().overlay(Theme.border) }
                }
                if RowFold.hiddenCount(total: skills.count, limit: Self.foldLimit) > 0 {
                    Divider().overlay(Theme.border)
                    ExpandRow(isExpanded: showAll,
                              hidden: RowFold.hiddenCount(total: skills.count, limit: Self.foldLimit),
                              help: (expand: "Show every row",
                                     collapse: "Show only the first \(Self.foldLimit)"),
                              edge: .inset) {
                        withAnimation(.easeOut(duration: Theme.fadeDuration)) { showAll.toggle() }
                    }
                }
            }
            // Exactly the card's width, or the columns' minimum if wider.
            .frame(width: max(available, Self.nameMinWidth + Self.fixedTail(columns: ordered.count)),
                   alignment: .leading)
            .overlay(alignment: .topLeading) { dropMarker }
        }
        .scrollIndicators(.automatic)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { available = $0 }
        .onChange(of: order) { UserDefaults.standard.set(order, forKey: Self.orderKey) }
    }

    /// "Skill" over the flexible name column, "Source" over the tag
    /// column, then a draggable header centred over each folder column.
    /// Each separator reaches through the row's bottom padding to the
    /// divider, so the column edges read as one line top to bottom.
    private var header: some View {
        HStack(spacing: 0) {
            Text("Skill")
                .frame(minWidth: Self.nameMinWidth, maxWidth: .infinity, alignment: .leading)
            columnSeparator().padding(.bottom, -8)
            Text("Source").frame(width: Self.sourceWidth)
            ForEach(ordered) { root in
                columnSeparator().padding(.bottom, -8)
                let index = orderedIds.firstIndex(of: root.id) ?? 0
                SkillRootHeader(root: root, width: Self.cellWidth, model: model,
                                carriedBy: dragging == root.id
                                    ? RowReorder.clamped(translation, heights: pitches, dragged: index)
                                    : nil,
                                drag: { dragChanged(root.id, $0) },
                                drop: { finish(move: true) },
                                cancel: { finish(move: false) },
                                move: { moveColumn(root.id, $0) })
            }
        }
        .font(Theme.tableHeader)
        .tracking(0.12)
        .foregroundStyle(Theme.tableHeaderText)
        .padding(.bottom, 8)
    }

    /// The accent bar at the slot a dragged column would land in.
    @ViewBuilder private var dropMarker: some View {
        if let x = dropEdge {
            Rectangle().fill(Theme.interactive)
                .frame(width: 2)
                .offset(x: x)
        }
    }

    private var dropEdge: CGFloat? {
        guard let id = dragging, let from = orderedIds.firstIndex(of: id) else { return nil }
        let target = RowReorder.targetIndex(heights: pitches, dragged: from, translation: translation)
        if target >= ordered.count - 1 { return totalWidth - 2 }
        let slot = target < from ? target : target + 1
        return nameWidth + Self.sourceWidth + CGFloat(slot) * Self.pitch
    }

    private func dragChanged(_ id: String, _ translation: CGFloat) {
        dragging = id
        self.translation = translation
    }

    /// The VoiceOver and keyboard reorder the grip does by pointer: move
    /// the column one slot left or right.
    private func moveColumn(_ id: String, _ direction: Int) {
        guard let from = orderedIds.firstIndex(of: id) else { return }
        let target = from + direction
        guard target >= 0, target <= ordered.count - 1 else { return }
        withAnimation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)) {
            var next = orderedIds
            next.remove(at: from)
            next.insert(id, at: target)
            order = next
        }
    }

    /// Drop the dragged column at the slot the pointer left it over.
    private func finish(move: Bool) {
        guard let id = dragging else { return }
        defer { dragging = nil; translation = 0 }
        guard move, let from = orderedIds.firstIndex(of: id) else { return }
        let target = RowReorder.targetIndex(heights: pitches, dragged: from, translation: translation)
        guard target != from else { return }
        withAnimation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)) {
            var next = orderedIds
            next.remove(at: from)
            next.insert(id, at: target)
            order = next
        }
    }
}

/// One folder column's header: a drag grip at the leading edge and the
/// mark centred over the column. A click opens the folder's menu, a
/// right click the same one, and only a drag started on the grip moves
/// the column. Two folders can share a harness label, so the tip names
/// the path to keep them apart.
private struct SkillRootHeader: View {
    var root: SkillRoot
    var width: CGFloat
    var model: SkillsModel
    /// The pointer offset while this is the header being carried, or nil.
    var carriedBy: CGFloat?
    var drag: (CGFloat) -> Void
    var drop: () -> Void
    /// The drag ended without reaching `onEnded`: slide everything back.
    var cancel: () -> Void
    /// VoiceOver and keyboard reorder, one slot in a direction (-1, +1).
    var move: (Int) -> Void
    @State private var hovering = false
    /// True only while the grip's gesture is live: resets on cancel.
    @GestureState private var down = false

    private var name: String { HarnessMark(root.name).name }
    private var busy: Bool { model.busy.contains(root.path) }

    /// The folder-level actions the root's link state allows, the same
    /// ones RootDetail offers in the harness links popover.
    @ViewBuilder private var columnMenu: some View {
        switch root.link {
        case .linked:
            Button("Unlink \(name)") { Task { await model.unlink(root) } }
        case .selection:
            Button("Unlink all from \(name)") { Task { await model.unlink(root) } }
            Button("Link all skills into \(name)") { Task { await model.link(root) } }
        case .notLinked:
            Button("Link \(name) to shared skills") { Task { await model.link(root) } }
        case .blocked:
            EmptyView()
        }
        Button("Show in Finder") {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: root.path)])
        }
    }

    var body: some View {
        Menu { columnMenu } label: {
            HStack(spacing: 4) {
                if busy { ProgressView().controlSize(.mini) }
                HarnessMarkView(harness: root.name)
            }
            .frame(width: width)
            .contentShape(.rect)
        }
        .menuStyle(.button)
        .buttonStyle(.plain)
        .menuIndicator(.hidden)
        .fixedSize()
        .hoverTip("\(name), \(displayPath(root.path)). \(root.explanation)", pinsOnClick: false)
        .contextMenu { columnMenu }
        .overlay(alignment: .leading) {
            Image(systemName: "line.3.horizontal")
                .font(Theme.font(size: 9, weight: .bold))
                .foregroundStyle(Theme.textMuted)
                .frame(width: 14, height: 20)
                .opacity(hovering || carriedBy != nil ? 0.9 : 0.3)
                .contentShape(.rect)
                .hoverTip("Drag to move the \(name) column", pinsOnClick: false)
                .gesture(DragGesture(minimumDistance: 4, coordinateSpace: .local)
                    .updating($down) { _, state, _ in state = true }
                    .onChanged { drag($0.translation.width) }
                    .onEnded { _ in drop() })
        }
        .offset(x: carriedBy ?? 0)
        .zIndex(carriedBy == nil ? 0 : 1)
        .opacity(carriedBy == nil ? 1 : 0.85)
        .onHover { hovering = $0 }
        .onChange(of: down) { _, isDown in
            guard !isDown else { return }
            Task { await Task.yield(); cancel() }
        }
        .accessibilityAction(named: "Move left") { move(-1) }
        .accessibilityAction(named: "Move right") { move(1) }
    }
}

/// A skill in the grid: a master switch, the name and description, the
/// source tag, then a cell per folder. The row opens the detail page.
private struct SkillGridRow: View {
    var skill: Skill
    var roots: [SkillRoot]
    var model: SkillsModel
    var open: () -> Void
    @State private var hovering = false
    /// The per-folder enables/disables of a master flip, mid-flight.
    @State private var masterBusy = false

    /// Every column's chip for this skill, keyed by root id (its path)
    /// so the cells can ask for their own.
    private var chips: [String: (link: SkillRoot.Link, on: Bool, toggleable: Bool)] {
        Dictionary(model.chips(for: skill).map { ($0.root.id, ($0.link, $0.on, $0.toggleable)) },
                   uniquingKeysWith: { a, _ in a })
    }

    /// The description first, the folder only when it is all the row has;
    /// then the quiet marks: where an adopted skill came from, the
    /// adoption a harness-private one is waiting on, and a name conflict
    /// swe keeps the copies of apart.
    private var secondary: String {
        var text = skill.description.isEmpty ? displayPath(skill.path) : skill.description
        if let donor = skill.adoptedFrom {
            text += " · added from \(HarnessMark(donor).name)"
        } else if model.pendingAdoption(skill) {
            text += " · adopts into the shared library on the next swe init"
        }
        if skill.conflict {
            text += " · another harness holds a different copy of this name"
        }
        return text
    }

    var body: some View {
        Button(action: open) {
            HStack(spacing: 0) {
                HStack(alignment: .top, spacing: 8) {
                    master
                    VStack(alignment: .leading, spacing: 2) {
                        Text(skill.name).font(Theme.rowTitle).foregroundStyle(Theme.textPrimary).lineLimit(1)
                        Text(secondary).font(Theme.caption).foregroundStyle(Theme.textMuted).lineLimit(1)
                    }
                    .padding(.trailing, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(minWidth: SkillTable.nameMinWidth, maxWidth: .infinity, alignment: .leading)
                columnSeparator()
                Badge(skill.source.title)
                    .frame(width: SkillTable.sourceWidth)
                ForEach(roots) { root in
                    columnSeparator()
                    SkillCell(skill: skill, root: root, chip: chips[root.id], model: model)
                        .frame(width: SkillTable.cellWidth)
                }
            }
            .padding(.vertical, 8)
            .background(hovering ? Theme.rowHover : .clear)
            .contentShape(.rect)
        }
        .plainButton(cornerRadius: Theme.smallRadius)
        .onHover { hovering = $0 }
        .accessibilityLabel(skill.harnesses.isEmpty
            ? "\(skill.name), \(skill.source.title)"
            : "\(skill.name), \(skill.source.title), in \(harnessNames(skill.harnesses))")
        .help("Open \(skill.name)")
        .contextMenu { rowMenu }
    }

    /// The row's right click: the master switch's two directions as menu
    /// items, and the move a private skill can make into shared.
    @ViewBuilder private var rowMenu: some View {
        if skill.source == .shared {
            Button("Turn off everywhere") { Task { await flip(on: false) } }
                .disabled(!model.chips(for: skill).contains { $0.on && $0.toggleable })
            Button("Turn on everywhere") { Task { await flip(on: true) } }
                .disabled(!model.chips(for: skill).contains { !$0.on && $0.toggleable })
        }
        if skill.source == .harness, let root = model.rootHolding(skill) {
            Button("Move to shared library") {
                Task { await model.moveToShared(skill, from: root) }
            }
        }
        Button("Show in Finder") {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: skill.path)])
        }
    }

    /// Flip every folder where a click could change the state: `enable`
    /// where the skill is out, `disable` where it is in. One serialized
    /// call per folder, the same path a single cell takes; a failure
    /// stops the batch so its `actionError` is the one still showing.
    private func flip(on: Bool) async {
        for chip in model.chips(for: skill) where chip.toggleable && chip.on != on {
            await model.setSkill(skill, on: chip.root, enabled: on)
            if model.actionError != nil { break }
        }
    }

    /// The row's checkbox left of the name: checked only when the skill
    /// is on in every folder that can hold it, a dash when it is on in
    /// some. Checking it turns the rest on; clearing a full row turns it
    /// off wherever a folder allows. Only a shared skill answers per
    /// folder; a private or harness-owned copy sits under its own
    /// manager, so its box stays put and explains itself on hover.
    @ViewBuilder private var master: some View {
        if skill.source == .shared {
            let chips = model.chips(for: skill)
            let check = rowCheck(chips)
            // Checking turns on what is off; clearing a full row turns off.
            let canAct = chips.contains { $0.toggleable && $0.on == (check == .all) }
            Group {
                if masterBusy {
                    ProgressView().controlSize(.mini)
                        .frame(width: GridCheckBox.side, height: GridCheckBox.side)
                } else {
                    Button {
                        masterBusy = true
                        Task { await flip(on: check != .all); masterBusy = false }
                    } label: {
                        GridCheckBox(state: GridCheckBox.mark(check), enabled: canAct)
                            .contentShape(.rect)
                    }
                    .buttonStyle(.plain)
                    .disabled(!canAct)
                    .accessibilityLabel("\(skill.name) in every folder")
                    .accessibilityValue(check == .all ? "on" : check == .some ? "on in some" : "off")
                }
            }
            .padding(.top, 2)
            .hoverTip(masterTip(check), pinsOnClick: false)
        } else {
            GridCheckBox(state: .on, enabled: false)
                .padding(.top, 2)
                .hoverTip(privateTip, pinsOnClick: false)
        }
    }

    /// On in every folder a click could have it on in, some, or none. A
    /// folder that is off and can't be switched does not count against a
    /// full row.
    private func rowCheck(_ chips: [(root: SkillRoot, link: SkillRoot.Link, on: Bool, toggleable: Bool)]) -> McpRowCheck {
        McpRowCheck.of(on: chips.filter { $0.on }.count,
                       possible: chips.filter { $0.on || $0.toggleable }.count)
    }

    private var privateTip: String {
        switch skill.source {
        case .harness:
            model.pendingAdoption(skill)
                ? "A private copy \(HarnessMark(skill.root).name) manages; the next swe init adopts it into the shared library. Right-click the row to move it now and switch it per folder."
                : "A private copy \(HarnessMark(skill.root).name) manages, outside the shared library. Right-click the row to move it to shared and switch it per folder."
        case .builtin:
            "Built into the harness itself, so nothing here can switch it."
        default:
            "Lives outside the shared library at \(displayPath(skill.path))."
        }
    }

    private func masterTip(_ check: McpRowCheck) -> String {
        let chips = model.chips(for: skill)
        let onNames = chips.filter { $0.on }.map { HarnessMark($0.root.name).name }
        let offNames = chips.filter { !$0.on && $0.toggleable }.map { HarnessMark($0.root.name).name }
        switch check {
        case .all:
            return "On in every folder that can hold it. Click to turn it off everywhere."
        case .some:
            return "On for \(onNames.joined(separator: ", ")). Click to also turn it on for \(offNames.joined(separator: ", "))."
        case .none:
            let blocked = chips.filter { !$0.toggleable }.count
            if offNames.isEmpty { return "Off, and no folder here can be switched." }
            return blocked > 0
                ? "Off where a folder allows it; \(blocked) can't be switched. Click to turn it on."
                : "Off everywhere. Click to turn it on in every folder."
        }
    }
}

/// One skill in one folder: a checkbox a click flips, a "P" where the
/// folder keeps its own private copy, a dimmed checked box where the
/// folder's state can't be switched per skill, an amber mark where the
/// folder needs fixing first, and a dimmed empty box where the skill
/// isn't seen at all.
private struct SkillCell: View {
    var skill: Skill
    var root: SkillRoot
    var chip: (link: SkillRoot.Link, on: Bool, toggleable: Bool)?
    var model: SkillsModel
    @State private var busy = false

    private var name: String { HarnessMark(root.name).name }

    var body: some View {
        // model.busy carries the folder's change whatever cell began it,
        // so a second tap in the same folder is never silently dropped.
        if busy || model.busy.contains(root.path) {
            ProgressView().controlSize(.mini)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let chip {
            if chip.on && skill.source == .shared && chip.toggleable {
                toggleButton(to: false,
                             label: AnyView(GridCheckBox(state: .on)),
                             tip: "On for \(name). Click to turn it off.")
            } else if !chip.on && chip.toggleable {
                toggleButton(to: true,
                             label: AnyView(GridCheckBox(state: .off)),
                             tip: "Off for \(name). Click to turn it on.")
            } else if chip.on {
                staticMark
            } else {
                blocked
            }
        } else {
            GridCheckBox(state: .off, enabled: false)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .hoverTip("Not seen by \(name)", pinsOnClick: false)
        }
    }

    /// The P mark's tip: what the copy is, and whether a move or the next
    /// init's adoption carries it into the shared library.
    private var privateMarkTip: String {
        var tip = "Private copy in \(name) at \(displayPath(skill.path))."
        if model.pendingAdoption(skill) {
            tip += " The next swe init adopts it into the shared library."
        } else {
            tip += " Move it to the shared library to switch it per folder."
        }
        return tip
    }

    /// A private copy or a harness-owned skill a folder sees: no per
    /// skill switch exists for those, so the mark only reports.
    @ViewBuilder private var staticMark: some View {
        if skill.source == .harness, model.rootHolding(skill) == root {
            Text("P")
                .font(Theme.font(size: 10, weight: .bold))
                .foregroundStyle(Theme.accent)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(Theme.accentTint, in: .rect(cornerRadius: 5))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .hoverTip(privateMarkTip, pinsOnClick: false)
                .help(privateMarkTip)
        } else {
            GridCheckBox(state: .on, enabled: false)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .hoverTip("On for \(name); this folder's state can't be switched per skill.",
                          pinsOnClick: false)
        }
    }

    /// A folder the skill can't enter or leave from a click: relink and
    /// conflicted folders land here. The tip names the fix.
    private var blocked: some View {
        Image(systemName: "exclamationmark.triangle.fill")
            .font(Theme.font(size: 10))
            .foregroundStyle(Theme.warningText)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .hoverTip("\(root.explanation) Folder: \(displayPath(root.path))", pinsOnClick: false)
    }

    private func toggleButton(to value: Bool, label: AnyView, tip: String) -> some View {
        Button {
            busy = true
            Task { await model.setSkill(skill, on: root, enabled: value); busy = false }
        } label: {
            label
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .hoverTip(tip, pinsOnClick: false)
        .accessibilityLabel("\(skill.name) in \(name)")
        .accessibilityValue(value ? "off" : "on")
    }
}

/// What the skill's detail page shows under its header: which harnesses
/// load it, the link chips a shared skill carries, and its note.
private struct SkillLinksBlock: View {
    var skill: Skill
    var model: SkillsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(note).font(Theme.caption).foregroundStyle(Theme.textSecondary)
            if skill.conflict {
                Label("Another harness's folder holds a different copy of this name; swe keeps the copies separate and never merges them.",
                      systemImage: "exclamationmark.triangle")
                    .font(Theme.caption).foregroundStyle(Theme.warningText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            let chips = model.chips(for: skill)
            if !chips.isEmpty {
                FlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(chips, id: \.root.id) { chip in
                        chipView(chip)
                    }
                }
            }
            // A refused toggle lands in actionError; without this the
            // chip just stays put with no reason shown.
            if let error = model.actionError {
                Text(error).font(Theme.caption).foregroundStyle(Theme.warning)
            }
        }
    }

    @ViewBuilder private func chipView(_ chip: (root: SkillRoot, link: SkillRoot.Link, on: Bool, toggleable: Bool)) -> some View {
        let tip = "\(HarnessMark(chip.root.name).name): \(chip.on ? "on" : "off")"
            + (chip.toggleable ? ": click to \(chip.on ? "disable" : "enable")" : "")
        if chip.toggleable {
            Button {
                Task { await model.setSkill(skill, on: chip.root, enabled: !chip.on) }
            } label: {
                LinkChipLabel(name: chip.root.name, link: chip.link,
                              busy: model.busy.contains(chip.root.path), compact: true)
            }
            .plainButton(cornerRadius: 999)
            .hoverTip(tip)
        } else {
            LinkChipLabel(name: chip.root.name, link: chip.link, compact: true)
                .hoverTip(tip)
        }
    }

    private var note: String {
        switch skill.source {
        case .shared:
            if let donor = skill.adoptedFrom {
                "In the shared library, added from \(HarnessMark(donor).name). Every harness whose folder is linked loads it."
            } else {
                "In the shared library. Every harness whose folder is linked loads it."
            }
        case .plugin:
            "Shipped by a plugin, so only \(skill.harnesses.first.map { HarnessMark($0).name } ?? "its harness") loads it."
        case .builtin:
            "Built into \(skill.harnesses.first.map { HarnessMark($0).name } ?? "its harness")."
        case .harness:
            (skill.harnesses.isEmpty
                ? "In \(skill.root)'s own folder, which no linked harness reads."
                : "In \(skill.root)'s own folder, outside the shared library.")
                + (model.pendingAdoption(skill)
                    ? " The next swe init adopts it into the shared library." : "")
        case .catalog:
            "In the \(skill.root) catalog. Harnesses load it only once it is moved or linked into a skills folder."
        case .project:
            "In a project's own skills folder."
        }
    }
}

/// `swe skills usage` for the detail card: call counts from the session
/// index, per harness, or a note when the index is off.
private struct SkillUsageBlock: View {
    var skill: Skill
    var model: SkillsModel
    /// nil while the swe call is in flight, double-nil meaning "never got one".
    @State private var usage: SkillUsage?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let usage, usage.tracked {
                if usage.calls == 0 {
                    Text("No calls indexed for this skill yet: the index scans existing sessions too, but calls written before the skill input key count as unattributed.")
                } else {
                    Text("\(usage.calls) \(usage.calls == 1 ? "call" : "calls") across \(usage.sessions) \(usage.sessions == 1 ? "session" : "sessions")"
                        + (usage.lastUsed.map { " · last used \($0.prefix(10))" } ?? ""))
                    if !usage.byHarness.isEmpty {
                        HStack(spacing: 10) {
                            ForEach(usage.byHarness.sorted(by: { $0.key < $1.key }), id: \.key) { harness, n in
                                HStack(spacing: 4) {
                                    HarnessMarkView(harness: harness, size: 12)
                                    Text("\(n)")
                                }
                            }
                        }
                    }
                    if let unattributed = usage.unattributed, unattributed > 0 {
                        Text("+\(unattributed) `Skill` call\(unattributed == 1 ? "" : "s") indexed without a skill name")
                            .foregroundStyle(Theme.textMuted)
                    }
                }
            } else if usage != nil {
                Text("Usage isn't tracked. Turn on the session index (`swe session index --on`) to count calls.")
            } else {
                // The call failed (a swe from before `usage` existed);
                // an empty block reads better than a wrong hint.
                EmptyView()
            }
        }
        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
        .task(id: [skill.name, String(model.loadCount)]) {
            // A swe from before `usage` fails the call; the block just
            // stays empty rather than fail the card. `loadCount` is in
            // the key because Refresh can leave the skill row as it was
            // while the index counted new calls.
            usage = try? await model.swe.skillUsage(skill.name)
        }
    }
}

extension SkillRoot.Link {
    var title: String {
        switch self {
        case .linked: "Linked"
        case .selection: "Selection"
        case .notLinked: "Not linked"
        case .blocked: "Blocked"
        }
    }
}
