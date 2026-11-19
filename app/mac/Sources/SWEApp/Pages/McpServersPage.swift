import AppKit
import SWECore
import SystemConfiguration
import SwiftUI

/// The caption column a server sheet aligns its labelled rows on.
private let detailLabelWidth: CGFloat = 96

/// Every MCP server the harnesses configure, as a server × harness grid:
/// where each is missing, where two definitions disagree, and a sync
/// through `swe mcp sync` to even them out.
struct McpServersPage: View {
    @Environment(AppModel.self) private var app
    @State private var filter: McpFilter = .all
    /// Where a server runs: every one, or only local/remote ones.
    @State private var host: McpHost = .all
    /// What a server is for; nil lists every type.
    @State private var type: String?
    @State private var selected: McpServer?
    @State private var adding = false
    /// Harnesses left out of the comparison, remembered on this Mac.
    @State private var hidden = Set(UserDefaults.standard.stringArray(forKey: Self.hiddenKey) ?? [])

    static let hiddenKey = "mcpHiddenHarnesses"

    /// App-wide, so a read outlives a trip to another page and the last
    /// list shows at once.
    private var model: McpModel { app.mcp }

    /// The folder the folder scan reads, from the Paths setting.
    private var repoRoot: URL { AppModel.mcpServersFolder(path: app.mcpServersPath) }

    /// A toggle's failure, named under the grid.
    @State private var toggleError: String?

    /// `swe mcp enable`/`disable` for one server in one harness, then a
    /// reload so the cell's new state renders.
    private func toggle(_ server: McpServer, in harness: String, on: Bool) async {
        toggleError = nil
        do {
            let result = try await app.swe.mcpSetEnabled(on, server: server.name, in: [harness])
            await model.load(swe: app.swe, for: app.swePath, health: model.hasHealth)
            if !result.failedIn.isEmpty {
                toggleError = "\(HarnessMark(harness).name): \(server.name) stayed off"
            }
        } catch {
            toggleError = "\(HarnessMark(harness).name): \(error.localizedDescription)"
        }
    }

    /// The row's checkbox. Checking it turns the server on in every
    /// compared harness it is off in and can be written to: a kept copy
    /// comes back, and a harness it was never synced to gets the hub's
    /// definition. Clearing it is a bare `swe mcp disable`, off wherever
    /// the server is defined.
    private func toggleAll(_ server: McpServer, on: Bool, compared: [String]) async {
        toggleError = nil
        let targets = on ? server.offTargets(among: compared) : []
        if on, targets.isEmpty { return }
        do {
            let result = try await app.swe.mcpSetEnabled(on, server: server.name, in: targets)
            await model.load(swe: app.swe, for: app.swePath, health: model.hasHealth)
            if !result.failedIn.isEmpty {
                toggleError = "\(server.name) stayed off for \(result.failedIn.joined(separator: ", "))"
            }
        } catch {
            toggleError = error.localizedDescription
        }
    }

    /// One harness's whole column: every server it defines off, or every
    /// server marked off in it back on. A single failure stops neither the
    /// rest of the batch nor the reload that repaints the cells that moved.
    private func toggleColumn(_ harness: String, on: Bool,
                              servers: [McpServer]) async {
        toggleError = nil
        let names = servers.filter {
            on ? $0.disabledIn.contains(harness) : $0.harnesses.contains(harness)
        }.map(\.name)
        guard !names.isEmpty else { return }
        var failed = Set<String>()
        for name in names {
            do {
                let result = try await app.swe.mcpSetEnabled(on, server: name, in: [harness])
                failed.formUnion(result.failedIn)
            } catch {
                failed.insert(name)
            }
        }
        await model.load(swe: app.swe, for: app.swePath, health: model.hasHealth)
        if !failed.isEmpty {
            toggleError = "\(HarnessMark(harness).name): \(failed.sorted().joined(separator: ", ")) stayed put"
        }
    }

    /// The columns the loading shimmer draws: the harnesses the install
    /// check knows, minus any the user hid. Before the check runs it is
    /// empty, which leaves the grid with only its name column.
    private var comparedHarnesses: [String] {
        app.providers.rows.filter(\.isInstalled).map(\.provider.id).filter { !hidden.contains($0) }
    }

    /// The Local folder rows on their own, for states with no server table
    /// to put them in (the inventory is empty or failed to read). With the
    /// hub unknown a row cannot claim "Not in the hub", so the caption hides.
    @ViewBuilder private func folderOnlyRows(hubKnown: Bool) -> some View {
        let local = McpServerRepo.grouped(servers: [], folders: app.mcpRepo.servers).local
        if !local.isEmpty {
            Card {
                ServerTable(harnesses: [], rows: local,
                            hubKnown: hubKnown, editor: app.editor) { _ in }
            }
        }
    }

    var body: some View {
        Page(title: "MCP servers",
             subtitle: "Every server your harnesses configure, where each is missing, and where two disagree.") {
            Button(model.checkingHealth ? "Checking…" : "Check health", systemImage: "waveform.path.ecg") {
                Task { await model.load(swe: app.swe, for: app.swePath, health: true) }
            }
            .secondaryButton()
            .disabled(model.checkingHealth)
            .hoverTip("Run each server's command check and reach each URL", replacesLabel: false)
            Button("Add server", systemImage: "plus") { adding = true }
                .primaryButton()
                .hoverTip("Add a server to the SWE hub, and optionally to harnesses")
            Button("Refresh", systemImage: "arrow.clockwise") {
                Task { await model.load(swe: app.swe, for: app.swePath, health: model.hasHealth) }
                Task { await app.mcpRepo.load() }
            }
            .iconButton()
            .keyboardShortcut("r", modifiers: .command)
            .help("Read the configs again (⌘R)")
        } content: {
            switch model.state {
            case .idle, .loading:
                McpGridSkeleton(harnesses: comparedHarnesses, label: "Reading the configs…")
            case .failed(let message):
                ErrorCard(message: message)
                folderOnlyRows(hubKnown: false)
            case .loaded(let inventory) where inventory.servers.isEmpty:
                Card { VStack(alignment: .leading, spacing: 10) {
                    Label("No MCP servers in any harness config.", systemImage: "powerplug").foregroundStyle(Theme.textSecondary)
                    Button("Add server", systemImage: "plus") { adding = true }.secondaryButton()
                } }
                folderOnlyRows(hubKnown: true)
            case .loaded(let inventory):
                // With no server in any harness there is no active column,
                // and a hub-only row would have nowhere to be switched
                // on: compare every known harness then.
                let listed = inventory.activeHarnesses.isEmpty
                    ? inventory.harnesses : inventory.activeHarnesses
                let compared = listed.filter { !hidden.contains($0) }
                let filtered = inventory.servers(filter, across: compared)
                    .filter { host == .all || $0.isRemote == (host == .remote) }
                    .filter { type == nil || $0.typeTag == type }
                let all = McpServerRepo.grouped(servers: filtered, folders: app.mcpRepo.servers,
                                                usageFrom: inventory.servers)
                // A folder no hub server uses is in no harness and has no
                // type, so it shows only on an unfiltered local list; the
                // Conflicts pill's 4 would otherwise sit over twenty rows.
                let rows = (all.local + all.remote).filter {
                    if case .folder = $0 { return filter == .all && host != .remote && type == nil }
                    return true
                }
                HStack(alignment: .center, spacing: 12) {
                    Tabs(options: McpFilter.allCases.map {
                        ($0, "\($0.title) \(inventory.servers($0, across: compared).count)")
                    }, selection: $filter, style: .period, onPanel: true)
                    Spacer(minLength: 8)
                    Text("Host").font(Theme.control).foregroundStyle(Theme.textMuted)
                    Tabs(options: McpHost.allCases.map { ($0, $0.title) },
                         selection: $host, style: .segmented, onPanel: true)
                    Text("Type").font(Theme.control).foregroundStyle(Theme.textMuted)
                    SelectMenu(options: [(value: Optional<String>.none, title: "All types")]
                               + McpServer.knownTypes.map { (.some($0), $0) },
                               selection: $type, width: nil)
                    let shown = rows.count { row in
                        if case .server = row { return true }
                        return false
                    }
                    Text("\(shown) of \(inventory.servers.count)")
                        .font(Theme.caption).foregroundStyle(Theme.textMuted)
                }
                HarnessChips(harnesses: listed, hidden: $hidden)
                    .onChange(of: hidden) { UserDefaults.standard.set(hidden.sorted(), forKey: Self.hiddenKey) }
                if rows.isEmpty {
                    Card {
                        Text(filter != .all && inventory.servers(filter, across: compared).isEmpty
                             ? "Every server is in every compared harness, defined the same way."
                             : "No servers match these filters.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                    }
                } else {
                    Card {
                        ServerTable(harnesses: compared, rows: rows,
                                    editor: app.editor,
                                    toggle: { server, harness, on in await toggle(server, in: harness, on: on) },
                                    batchServers: inventory.servers,
                                    toggleAll: { harness, on, servers in await toggleColumn(harness, on: on, servers: servers) },
                                    toggleRow: { server, on in await toggleAll(server, on: on, compared: compared) }) { selected = $0 }
                    }
                    if let toggleError {
                        Text(toggleError).font(Theme.caption).foregroundStyle(Theme.dangerText)
                    }
                }
                VStack(alignment: .leading, spacing: 4) {
                    if !inventory.idleHarnesses.isEmpty, !inventory.activeHarnesses.isEmpty {
                        Text("No servers in \(harnessNames(inventory.idleHarnesses)), so they have no column. A sync can still copy servers there.")
                    }
                    Text("Letters mark different definitions of one server. Env and header values never leave swe; only their names show here.")
                }
                .font(Theme.caption).foregroundStyle(Theme.textMuted)
            }
            McpRepoSection(model: app.mcpRepo, root: repoRoot, harnesses: comparedHarnesses)
        }
        .task(id: app.swePath) { await model.loadIfStale(swe: app.swe, for: app.swePath) }
        .task(id: repoRoot) {
            app.mcpRepo.root = repoRoot
            await app.mcpRepo.load()
        }
        .cardSheet(item: $selected) { server in
            if case .loaded(let inventory) = model.state {
                ServerSheet(initial: server, harnesses: inventory.harnesses,
                            model: model) {
                    await model.load(swe: app.swe, for: app.swePath, health: model.hasHealth)
                }
            }
        }
        .cardSheet(isPresented: $adding) {
            McpAddServerSheet(hidden: hidden) {
                await model.load(swe: app.swe, for: app.swePath, health: model.hasHealth)
            }
        }
    }
}

/// Which servers the table lists: every one, the ones a harness launches
/// on this Mac, or the ones reached at a URL. It replaces the Local and
/// Remote group titles the list used to split on.
private enum McpHost: String, CaseIterable {
    case all, local, remote

    var title: String {
        switch self {
        case .all: "All"
        case .local: "Local"
        case .remote: "Remote"
        }
    }
}

/// Which harnesses the grid compares: on chips are columns, off ones are left out.
private struct HarnessChips: View {
    var harnesses: [String]
    @Binding var hidden: Set<String>

    var body: some View {
        // Wraps in a narrow window rather than squeezing the names.
        FlowLayout(spacing: 6, lineSpacing: 6) {
            Text("Compare").font(Theme.caption).foregroundStyle(Theme.textSecondary).padding(.trailing, 4)
            ForEach(harnesses, id: \.self) { harness in
                let on = !hidden.contains(harness)
                Button {
                    if on { hidden.insert(harness) } else { hidden.remove(harness) }
                } label: {
                    HStack(spacing: 4) {
                        if on { Image(systemName: "checkmark").font(Theme.font(size: 9, weight: .bold)) }
                        HarnessMarkView(harness: harness, size: 14)
                    }
                    .font(Theme.control)
                    .foregroundStyle(on ? Theme.textPrimary : Theme.textMuted)
                    .padding(.horizontal, 10)
                    .frame(height: Theme.controlHeight)
                    .background(on ? Theme.selected : .clear, in: .capsule)
                    .overlay(Capsule().strokeBorder(on ? Theme.borderStrong : Theme.border, lineWidth: 1))
                    .contentShape(.capsule)
                }
                .plainButton(cornerRadius: Theme.controlHeight / 2)
                .accessibilityAddTraits(on ? .isSelected : [])
                .help(on ? "Leave \(HarnessMark(harness).name) out of the comparison" : "Compare \(HarnessMark(harness).name) too")
            }
        }
    }
}

private struct ServerTable: View {
    var harnesses: [String]
    /// The flat filtered list: server rows and unused folder rows.
    var rows: [McpServerRepo.Row]
    /// False when the inventory never loaded: an unused-looking folder
    /// cannot claim "Not in the hub", so its caption hides.
    var hubKnown = true
    var editor: CodeEditor?
    /// Switch one server in one harness (the new state, then a reload).
    var toggle: (McpServer, String, Bool) async -> Void = { _, _, _ in }
    /// The full inventory, not the filtered rows: a column's batch switch
    /// and its counts must see the servers the filter hides.
    var batchServers: [McpServer] = []
    /// Switch every server in one harness column (name list supplied by
    /// the table, which knows the rows).
    var toggleAll: (String, Bool, [McpServer]) async -> Void = { _, _, _ in }
    /// Switch one server in every place it is defined or marked off.
    var toggleRow: (McpServer, Bool) async -> Void = { _, _ in }
    var open: (McpServer) -> Void

    /// The name column takes whatever the cells leave, but never less than this.
    static let nameMinWidth: CGFloat = 180
    /// The Type column: the widest tag, PRODUCTIVITY, fits inside it.
    static let typeWidth: CGFloat = 96
    /// Every harness column shares this width: a regular grid, wide
    /// enough for a logo plus its qualifier word.
    static let cellWidth: CGFloat = 92
    /// One column's footprint for the reorder maths: its leading
    /// separator plus the cell.
    static let pitch: CGFloat = 1 + cellWidth
    /// The gap between a Local row's folder buttons and the first fixed
    /// column, the same on a server's row and an unused folder's.
    static let actionsGap: CGFloat = 8
    /// Rows the table shows before folding behind a Show more row.
    private static let foldLimit = 20
    /// The UserDefaults key the harness column order persists under.
    static let orderKey = "mcpColumnOrder"

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The harness columns in display order, remembered on this Mac as a
    /// list of ids: a dragged column rewrites it, a harness the save
    /// predates appends, and a hidden or removed one drops out.
    @State private var order = UserDefaults.standard.stringArray(forKey: ServerTable.orderKey) ?? []
    @State private var showAll = false
    /// The card's inner width: the table fills it, and scrolls only past it.
    @State private var available: CGFloat = 0
    /// The header mid-drag and how far the pointer has carried it.
    @State private var dragging: String?
    @State private var translation: CGFloat = 0

    /// The columns in display order: the saved order, then any harness
    /// the save predates, and none the comparison left out.
    private var ordered: [String] {
        order.filter(harnesses.contains) + harnesses.filter { !order.contains($0) }
    }

    /// The widths RowReorder works on: a uniform `pitch` per column.
    private var pitches: [CGFloat] { Array(repeating: Self.pitch, count: ordered.count) }

    /// Everything after the name column: its separator, the Type column,
    /// and every harness column's separator and cell.
    static func fixedTail(columns: Int) -> CGFloat {
        1 + typeWidth + CGFloat(columns) * pitch
    }

    /// The name column on screen: the table's width after the fixed
    /// columns, at least `nameMinWidth`.
    private var nameWidth: CGFloat {
        max(available - Self.fixedTail(columns: ordered.count), Self.nameMinWidth)
    }

    private var totalWidth: CGFloat { nameWidth + Self.fixedTail(columns: ordered.count) }

    var body: some View {
        ScrollView(.horizontal) {
            VStack(alignment: .leading, spacing: 0) {
                header
                Divider().overlay(Theme.border)
                let shown = RowFold.visible(rows, limit: Self.foldLimit, expanded: showAll)
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, row in
                    switch row {
                    case .server(let server, let folders):
                        ServerRow(server: server, folders: folders, harnesses: ordered,
                                  editor: editor,
                                  toggle: toggle, toggleRow: toggleRow) { open(server) }
                    case .folder(let folder):
                        FolderRow(folder: folder, harnesses: ordered,
                                  hubKnown: hubKnown, editor: editor)
                    }
                    if index < shown.count - 1 { Divider().overlay(Theme.border) }
                }
                if RowFold.hiddenCount(total: rows.count, limit: Self.foldLimit) > 0 {
                    Divider().overlay(Theme.border)
                    ExpandRow(isExpanded: showAll,
                              hidden: RowFold.hiddenCount(total: rows.count, limit: Self.foldLimit),
                              help: (expand: "Show every row",
                                     collapse: "Show only the first \(Self.foldLimit)"),
                              edge: .inset) {
                        withAnimation(.easeOut(duration: Theme.fadeDuration)) { showAll.toggle() }
                    }
                }
            }
            // Exactly the card's width, or the columns' minimum if wider, so a
            // long name truncates instead of pushing the harness columns away.
            .frame(width: max(available, Self.nameMinWidth + Self.fixedTail(columns: ordered.count)),
                   alignment: .leading)
            .overlay(alignment: .topLeading) { dropMarker }
        }
        .scrollIndicators(.automatic)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { available = $0 }
        .onChange(of: order) { UserDefaults.standard.set(order, forKey: Self.orderKey) }
    }

    /// "Server" over the flexible name column, "Type" over the tag
    /// column, then a draggable header centred over each harness column.
    /// Each separator reaches through the row's bottom padding to the
    /// divider, so the column edges read as one line top to bottom.
    private var header: some View {
        HStack(spacing: 0) {
            Text("Server")
                .frame(minWidth: Self.nameMinWidth, maxWidth: .infinity, alignment: .leading)
            columnSeparator().padding(.bottom, -8)
            Text("Type").frame(width: Self.typeWidth)
            ForEach(ordered, id: \.self) { harness in
                columnSeparator().padding(.bottom, -8)
                let index = ordered.firstIndex(of: harness) ?? 0
                HarnessHeader(harness: harness, width: Self.cellWidth,
                              servers: batchServers, toggleAll: toggleAll,
                              carriedBy: dragging == harness
                                  ? RowReorder.clamped(translation, heights: pitches, dragged: index)
                                  : nil,
                              drag: { dragChanged(harness, $0) },
                              drop: { finish(move: true) },
                              cancel: { finish(move: false) },
                              move: { moveColumn(harness, $0) })
            }
        }
        .font(Theme.tableHeader)
        .tracking(0.12)
        .foregroundStyle(Theme.tableHeaderText)
        .padding(.bottom, 8)
    }

    /// The accent bar at the slot a dragged column would land in, like
    /// the mock's inset drop mark but running the table's height.
    @ViewBuilder private var dropMarker: some View {
        if let x = dropEdge {
            Rectangle().fill(Theme.interactive)
                .frame(width: 2)
                .offset(x: x)
        }
    }

    /// The insertion edge's x position while a column is mid-drag, nil
    /// otherwise. The slot the drop targets counts among the other
    /// columns, so it maps back to the column now standing there.
    private var dropEdge: CGFloat? {
        guard let id = dragging, let from = ordered.firstIndex(of: id) else { return nil }
        let target = RowReorder.targetIndex(heights: pitches, dragged: from, translation: translation)
        if target >= ordered.count - 1 { return totalWidth - 2 }
        let slot = target < from ? target : target + 1
        return nameWidth + Self.typeWidth + CGFloat(slot) * Self.pitch
    }

    private func dragChanged(_ harness: String, _ translation: CGFloat) {
        dragging = harness
        self.translation = translation
    }

    /// The VoiceOver and keyboard reorder the grip does by pointer: move
    /// the column one slot left or right. `direction` counts among the
    /// other columns, the same space RowReorder's target uses.
    private func moveColumn(_ harness: String, _ direction: Int) {
        guard let from = ordered.firstIndex(of: harness) else { return }
        let target = from + direction
        guard target >= 0, target <= ordered.count - 1 else { return }
        withAnimation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)) {
            var next = ordered
            next.remove(at: from)
            next.insert(harness, at: target)
            order = next
        }
    }

    /// Drop the dragged column at the slot the pointer left it over; the
    /// other columns slide aside through the animation.
    private func finish(move: Bool) {
        guard let id = dragging else { return }
        defer { dragging = nil; translation = 0 }
        guard move, let from = ordered.firstIndex(of: id) else { return }
        let target = RowReorder.targetIndex(heights: pitches, dragged: from, translation: translation)
        guard target != from else { return }
        withAnimation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)) {
            var next = ordered
            next.remove(at: from)
            next.insert(id, at: target)
            order = next
        }
    }
}

/// One harness column's header: a drag grip at the leading edge and the
/// mark centred over the column. A click opens the column menu, a right
/// click the same one, and only a drag started on the grip moves the
/// column, so a press meant for the menu never becomes a move.
private struct HarnessHeader: View {
    var harness: String
    var width: CGFloat
    var servers: [McpServer]
    var toggleAll: (String, Bool, [McpServer]) async -> Void
    /// The pointer offset while this is the header being carried, or nil.
    var carriedBy: CGFloat?
    var drag: (CGFloat) -> Void
    var drop: () -> Void
    /// The drag ended without reaching `onEnded`: slide everything back.
    var cancel: () -> Void
    /// VoiceOver and keyboard reorder, one slot in a direction (-1, +1).
    var move: (Int) -> Void
    @State private var hovering = false
    /// True only while the grip's gesture is live: resets on cancel, which
    /// never reaches `onEnded`, the same tidy-up ReorderableRows does.
    @GestureState private var down = false

    private var name: String { HarnessMark(harness).name }

    @ViewBuilder private var columnMenu: some View {
        let on = servers.filter { $0.harnesses.contains(harness) }.count
        let off = servers.filter { $0.disabledIn.contains(harness) }.count
        Button("Turn off all servers in \(name)") {
            Task { await toggleAll(harness, false, servers) }
        }
        .disabled(on == 0)
        Button("Turn on all servers in \(name)") {
            Task { await toggleAll(harness, true, servers) }
        }
        .disabled(off == 0)
    }

    var body: some View {
        Menu { columnMenu } label: {
            HarnessMarkView(harness: harness)
                .frame(width: width)
                .contentShape(.rect)
        }
        .menuStyle(.button)
        .buttonStyle(.plain)
        .menuIndicator(.hidden)
        .fixedSize()
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

private struct ServerRow: View {
    var server: McpServer
    /// The folders in the servers folder this server runs from; empty for
    /// a remote one.
    var folders: [McpRepoServer] = []
    var harnesses: [String]
    var editor: CodeEditor?
    var toggle: (McpServer, String, Bool) async -> Void = { _, _, _ in }
    var toggleRow: (McpServer, Bool) async -> Void = { _, _ in }
    var open: () -> Void
    @State private var hovering = false
    /// The `swe mcp enable/disable` for every harness, mid-flight.
    @State private var masterBusy = false

    /// The fixed columns' total width: the folder actions overlay the row
    /// just left of the Type separator, so the cells never move.
    private var cellsWidth: CGFloat {
        ServerTable.fixedTail(columns: harnesses.count)
    }

    /// What the name column leaves for the folder actions.
    private var actionsWidth: CGFloat {
        folders.isEmpty ? 0 : CGFloat(editor == nil ? 1 : 2) * Theme.iconButton + 24
    }

    var body: some View {
        Button(action: open) {
            HStack(spacing: 0) {
                HStack(alignment: .top, spacing: 8) {
                    master
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 5) {
                            Text(server.name).font(Theme.rowTitle).foregroundStyle(Theme.textPrimary).lineLimit(1)
                            if !server.secretsMissing.isEmpty {
                                Image(systemName: "exclamationmark.triangle")
                                    .font(Theme.font(size: 10))
                                    .foregroundStyle(Theme.warningText)
                                    .hoverTip("Needs a value for \(server.secretsMissing.joined(separator: ", "))")
                            }
                        }
                        secondary
                    }
                }
                .padding(.trailing, actionsWidth)
                .frame(minWidth: ServerTable.nameMinWidth, maxWidth: .infinity, alignment: .leading)
                columnSeparator()
                TypeTag(tag: server.typeTag)
                    .frame(width: ServerTable.typeWidth)
                let differs = server.conflicts(among: harnesses)
                ForEach(harnesses, id: \.self) { harness in
                    columnSeparator()
                    Cell(server: server, harness: harness, differs: differs, toggle: toggle)
                        .frame(width: ServerTable.cellWidth)
                }
            }
            .padding(.vertical, 8)
            .background(hovering ? Theme.rowHover : .clear)
            .contentShape(.rect)
        }
        .plainButton(cornerRadius: Theme.smallRadius)
        .onHover { hovering = $0 }
        .accessibilityLabel("\(server.name), \(server.typeTag), in \(harnessNames(server.harnesses))")
        .contextMenu {
            Button("Turn off everywhere") { Task { await toggleRow(server, false) } }
                .disabled(server.harnesses.isEmpty)
            Button("Turn on everywhere") { Task { await toggleRow(server, true) } }
                .disabled(server.offTargets(among: harnesses).isEmpty)
        }
        // The Finder/editor buttons sit on the open button, above it in
        // hit order, so they stay clickable without breaking the cells'
        // alignment or losing the row's click to open.
        .overlay(alignment: .trailing) {
            if !folders.isEmpty {
                FolderActions(folders: folders, editor: editor)
                    .padding(.trailing, cellsWidth + ServerTable.actionsGap)
            }
        }
    }

    /// The row's checkbox left of the name: checked only when the server
    /// is on in every compared harness it can be on in, a dash when it is
    /// on in some. Checking it turns the rest on; clearing a full row
    /// turns the server off everywhere. The dot beside it is the health
    /// readout: green for a healthy probe, red for a failing one, amber
    /// when the probe never ran or a secret is missing, grey when the
    /// server is off everywhere.
    @ViewBuilder private var master: some View {
        let check = server.rowCheck(among: harnesses)
        let pending = server.offTargets(among: harnesses)
        let on = !server.harnesses.isEmpty
        HStack(spacing: 6) {
            Group {
                if masterBusy {
                    ProgressView().controlSize(.mini)
                        .frame(width: McpCheckBox.side, height: McpCheckBox.side)
                } else {
                    Button {
                        masterBusy = true
                        Task { await toggleRow(server, check != .all); masterBusy = false }
                    } label: {
                        McpCheckBox(state: check == .all ? .on : check == .some ? .mixed : .off,
                                    enabled: check == .all || !pending.isEmpty)
                            .contentShape(.rect)
                    }
                    .buttonStyle(.plain)
                    .disabled(check != .all && pending.isEmpty)
                    .accessibilityLabel("\(server.name) in every harness")
                    .accessibilityValue(check == .all ? "on" : check == .some ? "on in some" : "off")
                }
            }
            .hoverTip(mcpRowCheckTip(server, check: check, pending: pending), pinsOnClick: false)
            let health = mcpMasterTip(server, on: on)
            Circle().fill(on ? mcpMasterTint(server, on: true) : Theme.borderStrong)
                .frame(width: Theme.statusDot, height: Theme.statusDot)
                .hoverTip(health.title ?? "Off everywhere.", pinsOnClick: false)
        }
        .padding(.top, 2)
    }

    /// "stdio · checkout · linear-mcp · main ↑1 ↓45 · Local changes": the
    /// transport, a checkout tag and the folder name(s) in mono when it runs
    /// from the servers folder, then the git state when there is one
    /// folder; several folders leave their git state to the menus.
    @ViewBuilder private var secondary: some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text(server.transport)
            if !folders.isEmpty {
                Text("·")
                Text("checkout")
                    .font(Theme.font(size: 10, weight: .medium))
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Theme.subtle, in: .rect(cornerRadius: 4))
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Theme.border, lineWidth: 1))
                    .hoverTip("Runs from a folder in your MCP servers folder")
                Text("·")
                Text(folders.map(\.name).joined(separator: ", "))
                    .font(Theme.font(size: 12, design: .monospaced))
                if folders.count == 1 {
                    Text("·")
                    GitPieces(folder: folders[0])
                }
            }
        }
        .font(Theme.caption).foregroundStyle(Theme.textMuted)
        .lineLimit(1)
    }
}

/// The small muted type tag in the Type column: a capsule tinted by the
/// kind the hub recorded, "other" when it recorded none the app knows.
private struct TypeTag: View {
    var tag: String

    /// One muted tint per type. The hue is enough to tell the tags apart;
    /// none of them reads as a status colour.
    private var tint: Color {
        switch tag {
        case "dev": .blue
        case "productivity": .purple
        case "research": .teal
        case "social": .pink
        case "search": .indigo
        default: .secondary
        }
    }

    var body: some View {
        Text(tag.uppercased())
            .font(Theme.font(size: 9, weight: .semibold))
            .tracking(0.6)
            .foregroundStyle(tint)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(tint.opacity(0.12), in: .rect(cornerRadius: 5))
    }
}

private struct Cell: View {
    var server: McpServer
    var harness: String
    /// Whether the compared harnesses disagree; letters show only then.
    var differs: Bool
    /// Switch the server in this harness (the new state, then a reload).
    var toggle: (McpServer, String, Bool) async -> Void
    @State private var busy = false

    private var name: String { HarnessMark(harness).name }

    /// A checkbox per harness; a click anywhere in the cell flips it.
    /// Clearing pulls the entry out of the harness (the hub keeps a copy
    /// to restore). Checking restores that copy, or writes the hub's
    /// definition where the server was never synced. The box is dimmed
    /// where swe has nothing it could write.
    var body: some View {
        if busy {
            ProgressView().controlSize(.mini)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let group = server.group(of: harness) {
            Button {
                busy = true
                Task { await toggle(server, harness, false); busy = false }
            } label: {
                HStack(spacing: 5) {
                    McpCheckBox(state: .on)
                    if differs { variant(group) }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .hoverTip("On for \(name). Click to turn it off.", pinsOnClick: false)
            .accessibilityLabel("\(server.name) in \(name)")
            .accessibilityValue("on")
        } else if server.canBeOn(in: harness) {
            Button {
                busy = true
                Task { await toggle(server, harness, true); busy = false }
            } label: {
                McpCheckBox(state: .off)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .hoverTip(server.disabledIn.contains(harness)
                      ? "Off for \(name). Click to turn it back on."
                      : "Not in \(name) yet. Click to add it from the hub.", pinsOnClick: false)
            .accessibilityLabel("\(server.name) in \(name)")
            .accessibilityValue("off")
        } else {
            McpCheckBox(state: .off, enabled: false)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .hoverTip(server.inHub
                          ? "\(name) only takes servers it launches with a command; this one is at a URL."
                          : "Not in the hub yet: the next swe mcp sync adopts it, then it can go to \(name).",
                          pinsOnClick: false)
                .accessibilityLabel("\(server.name) cannot be turned on in \(name)")
        }
    }

    private func variant(_ group: Int) -> some View {
        Text(variantLetter(group))
            .font(Theme.font(size: 11, weight: .semibold))
            .foregroundStyle(group == 0 ? Theme.textPrimary : Theme.warningText)
            .frame(width: 18, height: 18)
            .background(Theme.subtle, in: .rect(cornerRadius: 5))
            .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Theme.border, lineWidth: 1))
    }
}

/// The table's checkbox mark: checked, a dash for "some", or empty.
/// Dimmed when a click would have nothing to do.
struct McpCheckBox: View {
    enum Mark { case on, mixed, off }
    var state: Mark
    var enabled = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let side: CGFloat = 16

    private var symbol: String {
        switch state {
        case .on: "checkmark.square.fill"
        case .mixed: "minus.square.fill"
        case .off: "square"
        }
    }

    var body: some View {
        Image(systemName: symbol)
            .font(Theme.font(size: 15))
            .foregroundStyle(state == .off ? Theme.textMuted : Theme.interactive)
            .opacity(enabled ? 1 : 0.35)
            .frame(width: Self.side, height: Self.side)
            .animation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration), value: state)
    }
}

/// What the row checkbox explains on hover: what a click would do.
func mcpRowCheckTip(_ server: McpServer, check: McpRowCheck, pending: [String]) -> String {
    switch check {
    case .all:
        return "On in every harness shown. Click to turn it off everywhere."
    case .some:
        return "On in \(harnessNames(server.harnesses)). Click to also turn it on in \(harnessNames(pending))."
    case .none:
        return pending.isEmpty
            ? "Not in the hub yet: the next swe mcp sync adopts it, then it can be turned on."
            : "Off everywhere. Click to turn it on in \(harnessNames(pending))."
    }
}

func variantLetter(_ index: Int) -> String {
    index < 26 ? String(UnicodeScalar(UInt8(65 + index))) : "\(index + 1)"
}

/// The master switch's on-track tint: the health readout, red for a
/// failing probe, amber when unprobed or a secret is missing, green for
/// a healthy one; the off track stays plain. Shared by the table row's
/// and the server sheet's master switches.
func mcpMasterTint(_ server: McpServer, on: Bool) -> Color {
    guard on else { return Theme.interactive }
    if server.health == "error" { return Theme.danger }
    if server.health == "ok", server.secretsMissing.isEmpty { return Theme.success }
    return Theme.warning
}

/// What the master switch explains on hover: where the server is on and
/// what the probe said, or how to bring the stashed set back.
func mcpMasterTip(_ server: McpServer, on: Bool) -> (text: String, title: String?) {
    if !on {
        return server.disabledIn.isEmpty
            ? ("Not in any harness.", nil)
            : ("Off everywhere. Turn the harnesses back on.", nil)
    }
    let health: String
    switch server.health {
    case "error": health = server.detail ?? "The probe is failing."
    case "ok": health = server.secretsMissing.isEmpty ? "Healthy." : "A secret value is missing."
    default: health = server.secretsMissing.isEmpty ? "Not probed yet." : "A secret value is missing."
    }
    return ("On for \(harnessNames(server.harnesses)). Turn off everywhere.", health)
}

/// One server, hub-first: its definition, a switch per known harness,
/// gateway stats, tools and files. The hub (mcp.json) is the source of
/// truth — `swe mcp sync` writes it into harnesses and a harness-only
/// server adopts into it on the next sync, so the sheet holds no copy
/// form: switches, facts, Remove, and an edit form for the hub entry's
/// name and its URL or command.
private struct ServerSheet: View {
    var initial: McpServer
    /// Every harness swe reads; each gets a row and its switch.
    var harnesses: [String]
    var model: McpModel
    var reload: () async -> Void

    @Environment(AppModel.self) private var app
    @Environment(\.dismissSheet) private var dismiss
    @State private var error: String?
    /// Harnesses whose switch is mid-flight on `swe mcp enable/disable`.
    @State private var toggling: Set<String> = []
    /// The header's master switch mid-flight.
    @State private var masterBusy = false
    /// `swe mcp set-type` mid-flight.
    @State private var typeBusy = false
    @State private var removing = false
    /// The pasted value per missing ${NAME}, cleared after it is saved.
    @State private var secretDrafts: [String: String] = [:]
    @State private var savingSecret: String?
    @State private var confirmingRemove = false
    /// The Definition section's edit form: open, its two fields, mid-save.
    @State private var editing = false
    @State private var draftName = ""
    @State private var draftDefinition = ""
    @State private var saving = false
    /// `swe mcp info`: the files behind this server, loaded on open.
    @State private var info: McpServerInfo?
    /// Nil until "Load tools" runs the handshake — spawning a server is
    /// the user's call, not the sheet's.
    @State private var toolsResult: McpToolsResult?
    @State private var toolsLoading = false

    /// What one known harness holds for this server.
    private enum HarnessState {
        /// Defines the server; the switch sits on.
        case on
        /// swe-disabled: the entry left the file, its copy stays stashed.
        case off
        /// In the hub but not written here; on writes it like a sync does.
        case notSynced
        /// Accepts command servers only and the hub definition is a URL.
        case localOnly
        /// Harness-only server: nothing to restore until the hub adopts it.
        case blocked
    }

    /// `swe mcp enable`/`disable` for this one harness, then a reload so
    /// the sheet and the grid behind it show the new state.
    private func setEnabled(_ harness: String, on: Bool) async {
        toggling.insert(harness)
        defer { toggling.remove(harness) }
        error = nil
        do {
            let result = try await app.swe.mcpSetEnabled(on, server: server.name, in: [harness])
            await reload()
            await loadInfo()
            if !result.failedIn.isEmpty {
                error = "\(HarnessMark(harness).name): \(server.name) stayed off"
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// The header's batch switch: a bare `swe mcp disable`/`enable` — no
    /// `--for` — so the CLI's `master_off` record drives the restore set
    /// and a harness the user switched off by hand is never resurrected.
    private func setEverywhere(_ on: Bool) async {
        masterBusy = true
        defer { masterBusy = false }
        error = nil
        do {
            let result = try await app.swe.mcpSetEnabled(
                on, server: server.name, in: [])
            await reload()
            await loadInfo()
            if !result.failedIn.isEmpty {
                error = "\(server.name) stayed off for \(result.failedIn.joined(separator: ", "))"
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// `swe mcp set-type` for the picker's new label, then a reload.
    /// Re-picking the current value runs nothing.
    private func setType(_ type: String) async {
        guard type != currentType else { return }
        typeBusy = true
        defer { typeBusy = false }
        error = nil
        do {
            _ = try await app.swe.mcpSetType(type, server: server.name)
            await reload()
            await loadInfo()
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// The three-position read of one harness row: on, stashed off, or
    /// not written — plus the two states where the switch stays locked
    /// (a URL definition in a command-only harness, or nothing anywhere
    /// to restore until the hub adopts the server).
    private func harnessState(_ harness: String) -> HarnessState {
        if server.harnesses.contains(harness) { return .on }
        if server.disabledIn.contains(harness) { return .off }
        if server.inHub {
            if McpServer.localOnly.contains(harness),
               server.isRemote(from: McpServer.hubSource) {
                return .localOnly
            }
            return .notSynced
        }
        return .blocked
    }

    /// Opens the edit form on the server as it stands. A definition with
    /// a hidden part starts empty: left empty, it is not touched.
    private func startEditing() {
        draftName = server.name
        draftDefinition = McpEditPlan.prefill(for: server)
        error = nil
        editing = true
    }

    /// `swe mcp set` for a changed URL or command, then `swe mcp rename`
    /// for a changed name. The definition goes first: if it is refused,
    /// nothing was renamed and the sheet still shows the server.
    private func saveEdit() async {
        error = nil
        var taken: Set<String> = []
        if case .loaded(let inventory) = model.state {
            taken = Set(inventory.servers.map(\.name)).subtracting([server.name])
        }
        let plan: McpEditPlan
        do {
            plan = try McpEditPlan.make(for: server, name: draftName,
                                        definition: draftDefinition, taken: taken)
        } catch {
            self.error = error.localizedDescription
            return
        }
        guard !plan.isEmpty else { editing = false; return }
        saving = true
        defer { saving = false }
        do {
            if plan.url != nil || plan.command != nil {
                let result = try await app.swe.mcpSet(server: server.name, url: plan.url,
                                                      command: plan.command)
                if !result.failed.isEmpty {
                    // The hub already holds the new definition, so a
                    // second Save would find nothing to change and read
                    // as done. Close the form and say where it stopped.
                    await reload()
                    await loadInfo()
                    editing = false
                    error = "Saved to the hub, but \(harnessNames(result.failed)) kept the old definition. "
                        + "Switch \(server.name) off and on there to rewrite it."
                    return
                }
            }
            if let newName = plan.rename {
                let result = try await app.swe.mcpRename(server.name, to: newName)
                await reload()
                if !result.failedIn.isEmpty {
                    error = "\(harnessNames(result.failedIn)) still call it \(server.name)."
                    return
                }
                // The sheet follows a server by name; this one has a new one.
                dismiss()
                return
            }
            await reload()
            await loadInfo()
            editing = false
        } catch {
            self.error = error.localizedDescription
            await reload()
        }
    }

    /// The selected type tag: a known word normalised, an unknown label
    /// verbatim so it stays displayable, "other" when the hub has none.
    private var currentType: String {
        guard let raw = server.type, !raw.isEmpty else { return "other" }
        let lower = raw.lowercased()
        return McpServer.knownTypes.contains(lower) ? lower : raw
    }

    /// The picker's options: every known word, plus the hub's own label
    /// when it isn't one, so the closed menu still says what it is set to.
    private var typeOptions: [(value: String, title: String)] {
        var options = McpServer.knownTypes.map { (value: $0, title: $0) }
        if !options.contains(where: { $0.value == currentType }) {
            options.append((currentType, currentType))
        }
        return options
    }

    /// `swe mcp info` for the Files section — file reads only, cheap
    /// enough to run when the sheet opens.
    private func loadInfo() async {
        do {
            info = try await app.swe.mcpInfo(server: server.name)
        } catch {
            // An swe from before `mcp info` has nothing to show; the row
            // data in the sheet header still covers the basics.
            info = nil
        }
    }

    /// `swe mcp tools` spawns the server for the handshake, so it runs
    /// only on the button.
    private func loadTools() async {
        toolsLoading = true
        defer { toolsLoading = false }
        do {
            toolsResult = try await app.swe.mcpTools(server: server.name)
        } catch {
            toolsResult = McpToolsResult(name: server.name, transport: server.transport,
                                         error: error.localizedDescription)
        }
    }

    /// Every file that defines the server or holds its off-mark, with
    /// its mtime; the hub file reads as "SWE hub".
    @ViewBuilder private func filesSection(_ info: McpServerInfo) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Files").font(Theme.cardTitle)
            ForEach(info.places, id: \.place) { place in
                HStack(spacing: 10) {
                    if McpServer.isHubSource(place.place) {
                        Image(systemName: "shippingbox")
                            .foregroundStyle(Theme.textSecondary)
                            .frame(width: 16)
                    } else {
                        HarnessMarkView(harness: place.place, size: 16)
                    }
                    Text(place.state == "hub" ? "SWE hub" : HarnessMark(place.place).name)
                        .font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                    if place.state == "off" {
                        Badge("off")
                    }
                    Spacer(minLength: 8)
                    Text(displayPath(place.path))
                        .font(Theme.caption).foregroundStyle(Theme.textMuted)
                        .lineLimit(1).truncationMode(.middle)
                    if let stamp = relativeStamp(place.updated) {
                        Text(stamp).font(Theme.caption).foregroundStyle(Theme.textMuted)
                    }
                }
            }
            if let stamp = relativeStamp(info.updated), info.places.count > 1 {
                Facts(label: "Updated", value: stamp)
            }
        }
    }

    /// The server's tool list, from a real handshake on demand.
    @ViewBuilder private var toolsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("Tools").font(Theme.cardTitle)
                Spacer()
                if toolsLoading {
                    ProgressView().controlSize(.mini)
                } else {
                    Button(toolsResult == nil ? "Load tools" : "Reload") {
                        Task { await loadTools() }
                    }
                    .secondaryButton()
                }
            }
            if let toolsResult {
                if let failure = toolsResult.error {
                    Text(failure).font(Theme.caption).foregroundStyle(Theme.danger)
                        .fixedSize(horizontal: false, vertical: true)
                } else if toolsResult.tools.isEmpty {
                    Text("The server answered with no tools.")
                        .font(Theme.caption).foregroundStyle(Theme.textMuted)
                } else {
                    ForEach(toolsResult.tools, id: \.name) { tool in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(tool.name)
                                .font(Theme.font(size: 12, design: .monospaced))
                                .foregroundStyle(Theme.textPrimary)
                            if !tool.description.isEmpty {
                                Text(tool.description)
                                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            } else {
                Text("Starts the server once to ask what it offers.")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
            }
        }
    }

    /// "running · pid 12 · up 300s · starts 2 · error: …", the gateway
    /// row minus its call counters, which get their own Stats lines.
    private func gatewayStateLine(_ gateway: McpServerInfo.GatewayRow) -> String {
        var bits = [gateway.state]
        if let pid = gateway.pid { bits.append("pid \(pid)") }
        if let up = gateway.uptimeS { bits.append("up \(up)s") }
        if let starts = gateway.starts { bits.append("starts \(starts)") }
        if let error = gateway.error, !error.isEmpty { bits.append("error: \(error)") }
        return bits.joined(separator: " · ")
    }

    /// The most-called tools first; at most five get their own line.
    private func topTools(_ gateway: McpServerInfo.GatewayRow) -> [(key: String, value: Int)] {
        Array(gateway.callsByTool.sorted { $0.value > $1.value }.prefix(5))
    }

    /// "3h ago"-style stamp from an ISO mtime; nil when it won't parse.
    /// `swe mcp info` emits local times without a zone ("2026-11-03T11:20:00"),
    /// so the zone-aware formatters are only part of the answer.
    private func relativeStamp(_ iso: String?) -> String? {
        guard let iso else { return nil }
        let parsed = ISO8601DateFormatter().date(from: iso)
            ?? ServerSheet.fractionalISO.date(from: iso)
            ?? ServerSheet.localISO.date(from: iso)
        guard let date = parsed else { return nil }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private static let fractionalISO: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    /// The zone-naive local time `_mtime_iso` writes.
    private static let localISO: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter
    }()

    /// The server as the latest load has it, so a finished switch or a
    /// type change shows its effect.
    var server: McpServer { model.server(named: initial.name) ?? initial }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            Divider().overlay(Theme.border)
            definition
            if !server.secretsMissing.isEmpty {
                Divider().overlay(Theme.border)
                needsValues
            }
            Divider().overlay(Theme.border)
            harnessesSection
            if let gateway = info?.gateway {
                Divider().overlay(Theme.border)
                statsSection(gateway)
            }
            Divider().overlay(Theme.border)
            toolsSection
            if let info, !info.places.isEmpty {
                Divider().overlay(Theme.border)
                filesSection(info)
            }
            Divider().overlay(Theme.border)
            footer
        }
        .sheetScaffold()
        .task { await loadInfo() }
        .confirmationDialog("Remove \(server.name)?", isPresented: $confirmingRemove) {
            Button("Remove", role: .destructive) { Task { await removeServer() } }
        } message: {
            let places = server.harnesses.map { HarnessMark($0).name }
                + (server.inHub ? ["SWE hub"] : [])
            Text(places.isEmpty
                 ? "It only leaves the list."
                 : "It goes from \(places.joined(separator: ", ")).")
        }
    }

    /// Name, editable type, host·transport badge and the master switch,
    /// the same health-tinted control the table row draws.
    @ViewBuilder private var header: some View {
        HStack(spacing: 8) {
            Text(server.name)
                .font(Theme.sectionTitle).tracking(-0.5)
                .foregroundStyle(Theme.textPrimary)
                .lineLimit(1)
                .textSelection(.enabled)
            typeControl
            Badge("\(server.isRemote ? "Remote" : "Local") · \(server.transport)")
            Spacer(minLength: 8)
            master
        }
    }

    /// The hub entry's `type` label as a dropdown once the entry exists;
    /// until the server adopts into the hub the tag is read-only, and an
    /// `sse` entry's tag always is — there `type` is the connection
    /// marker, not a label, so `set-type` leaves it alone.
    @ViewBuilder private var typeControl: some View {
        if server.transport == "sse" {
            TypeTag(tag: server.typeTag)
                .hoverTip("This entry's type field is the sse connection marker, not a label — swe leaves it alone.",
                          pinsOnClick: false)
        } else if server.inHub {
            SelectMenu(options: typeOptions,
                       selection: Binding(
                           get: { currentType },
                           set: { type in Task { await setType(type) } }),
                       width: nil)
                .disabled(typeBusy)
                .hoverTip("What the server is for: the label its hub entry carries.",
                          pinsOnClick: false)
        } else {
            TypeTag(tag: server.typeTag)
                .hoverTip("The type label lives on the hub entry: it can be set once the next swe mcp sync adopts this server.",
                          pinsOnClick: false)
        }
    }

    /// The batch switch: off pulls the entry from every harness, on
    /// restores exactly the harnesses marked off (the hub keeps copies).
    @ViewBuilder private var master: some View {
        let on = !server.harnesses.isEmpty
        let tip = mcpMasterTip(server, on: on)
        Group {
            if masterBusy {
                ProgressView().controlSize(.mini)
                    .frame(width: 36, height: 20)
            } else {
                Toggle(server.name, isOn: Binding(
                    get: { on },
                    set: { value in Task { await setEverywhere(value) } }))
                .labelsHidden()
                .toggleStyle(AccentSwitchStyle(tint: mcpMasterTint(server, on: on)))
                .disabled(!on && server.disabledIn.isEmpty)
            }
        }
        .hoverTip(tip.text, title: tip.title, pinsOnClick: false)
    }

    /// The scrubbed definition: command line or URL, env and header key
    /// names (values never leave swe), the shared-gateway badge and a
    /// chip per secret still missing a value. While the server lives
    /// only in harnesses a hint says the hub adopts it on the next sync.
    @ViewBuilder private var definition: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("Definition").font(Theme.cardTitle)
                if info?.shared == true {
                    Badge("Shared · gateway")
                        .hoverTip("Harnesses point at the mcp gateway, which serves this server once.",
                                  pinsOnClick: false)
                }
                Spacer()
                if server.inHub, !editing {
                    Button("Edit", systemImage: "pencil") { startEditing() }
                        .secondaryButton()
                        .hoverTip("Change the name, or the \(McpEditPlan.hubIsRemote(server) ? "URL" : "command").",
                                  pinsOnClick: false)
                }
            }
            if editing {
                editForm
            } else {
                Text(server.summary)
                    .font(Theme.font(size: 12, design: .monospaced))
                    .textSelection(.enabled)
                    .foregroundStyle(Theme.textSecondary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if server.groups.count > 1 {
                // swe describes a server by the first harness that defines it.
                Text("Command, URL and key names are definition A's.")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
            }
            if !server.envKeys.isEmpty {
                Facts(label: "Env vars", value: server.envKeys.joined(separator: ", "))
            }
            if !server.headerKeys.isEmpty {
                Facts(label: "Headers", value: server.headerKeys.joined(separator: ", "))
            }
            if !server.secretsMissing.isEmpty {
                FlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(server.secretsMissing, id: \.self) { missingChip($0) }
                }
            }
            if !server.inHub {
                Text("Not in the hub yet: the next swe mcp sync adopts it there, and the switches below can then write it.")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Name and URL (or command line) fields over Cancel and Save. Env
    /// and header values are not editable here: swe never sends them.
    @ViewBuilder private var editForm: some View {
        let remote = McpEditPlan.hubIsRemote(server)
        let hidden = McpEditPlan.prefill(for: server).isEmpty
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("Name").font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .frame(width: detailLabelWidth, alignment: .leading)
                TextField(server.name, text: $draftName).sweField()
                    .accessibilityLabel("Name")
            }
            HStack(spacing: 8) {
                Text(remote ? "URL" : "Command").font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .frame(width: detailLabelWidth, alignment: .leading)
                TextField(hidden ? "Leave empty to keep the current one"
                          : McpEditPlan.prefill(for: server),
                          text: $draftDefinition)
                    .sweField(monospaced: true)
                    .accessibilityLabel(remote ? "URL" : "Command")
            }
            Text(hidden
                 ? "The hub's \(remote ? "URL" : "command") is not shown here: part of it is hidden, or the harnesses hold a different copy. Type the whole value to replace it."
                 : server.harnesses.isEmpty
                 ? "Saves to the hub. No harness holds this server yet."
                 : "Saves to the hub, then rewrites \(harnessNames(server.harnesses)).")
                .font(Theme.caption).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button("Cancel", role: .cancel) { editing = false; error = nil }
                    .secondaryButton()
                    .disabled(saving)
                Button("Save") { Task { await saveEdit() } }
                    .primaryButton()
                    .disabled(saving || draftName.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
    }

    /// One ${NAME} still without a value, as a warning chip.
    private func missingChip(_ name: String) -> some View {
        HStack(spacing: 4) {
            Image(systemName: "exclamationmark.triangle")
                .font(Theme.font(size: 9, weight: .bold))
            Text(name)
                .font(Theme.font(size: 10, design: .monospaced))
        }
        .foregroundStyle(Theme.warningText)
        .padding(.horizontal, 7).padding(.vertical, 3)
        .background(Theme.warningText.opacity(0.12), in: .rect(cornerRadius: 5))
        .overlay(RoundedRectangle(cornerRadius: 5)
            .strokeBorder(Theme.warningText.opacity(0.4), lineWidth: 1))
        .hoverTip("Missing a value: set it below and sync fills it into the harness configs.",
                  pinsOnClick: false)
    }

    /// One row per known harness: logo, name, its state and the switch.
    /// On restores a stashed copy or writes the hub's definition where
    /// the server was never synced; off stashes it.
    @ViewBuilder private var harnessesSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Harnesses").font(Theme.cardTitle)
            ForEach(harnesses, id: \.self) { harness in
                let state = harnessState(harness)
                HStack(spacing: 10) {
                    HarnessMarkView(harness: harness, size: 16)
                    Text(HarnessMark(harness).name)
                        .font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                    stateCaption(harness, state: state)
                    Spacer(minLength: 8)
                    if toggling.contains(harness) {
                        ProgressView().controlSize(.mini)
                    }
                    Toggle(HarnessMark(harness).name, isOn: Binding(
                        get: { state == .on },
                        set: { on in Task { await setEnabled(harness, on: on) } }))
                        .labelsHidden()
                        .toggleStyle(AccentSwitchStyle())
                        .disabled(toggling.contains(harness)
                                  || state == .blocked || state == .localOnly)
                }
                .hoverTip(stateTip(harness, state: state), pinsOnClick: false)
            }
            if server.inHub {
                Text("Off leaves the harness's file; the hub keeps the definition.")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
            } else {
                Text("Once the hub adopts this server, the remaining harnesses above can switch it on.")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder private func stateCaption(_ harness: String,
                                           state: HarnessState) -> some View {
        switch state {
        case .on:
            // A definition letter flags the harnesses disagreeing with A.
            if server.groups.count > 1, let group = server.group(of: harness) {
                Text("def \(variantLetter(group))")
                    .font(Theme.caption)
                    .foregroundStyle(group == 0 ? Theme.textMuted : Theme.warningText)
            }
        case .off:
            Text("off, copy kept").font(Theme.caption).foregroundStyle(Theme.textMuted)
        case .notSynced:
            Text("not synced").font(Theme.caption).foregroundStyle(Theme.textMuted)
        case .localOnly:
            Text("commands only").font(Theme.caption).foregroundStyle(Theme.textMuted)
        case .blocked:
            Text("not in hub").font(Theme.caption).foregroundStyle(Theme.textMuted)
        }
    }

    private func stateTip(_ harness: String, state: HarnessState) -> String {
        let name = HarnessMark(harness).name
        switch state {
        case .on:
            return "On for \(name). Turning it off pulls the entry out; the hub keeps the definition."
        case .off:
            return "Off for \(name). Turning it on restores the stashed copy."
        case .notSynced:
            return "Not synced to \(name) yet. Turning it on writes the hub's definition, the same as a swe mcp sync."
        case .localOnly:
            return "\(name) only accepts servers it launches with a command; this one is at a URL."
        case .blocked:
            return "\(name) has nothing to restore: the next swe mcp sync adopts this server into the hub first."
        }
    }

    /// The live gateway's call counters for a shared server.
    @ViewBuilder private func statsSection(_ gateway: McpServerInfo.GatewayRow) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Stats").font(Theme.cardTitle)
            Facts(label: "Gateway", value: gatewayStateLine(gateway))
            Facts(label: "Calls", value: gateway.callErrors > 0
                  ? "\(gateway.calls) · \(gateway.callErrors) errors"
                  : "\(gateway.calls)")
            if let last = relativeStamp(gateway.lastCall) {
                Facts(label: "Last call", value: last)
            }
            ForEach(topTools(gateway), id: \.key) { tool in
                Facts(label: tool.key, value: "\(tool.value) calls")
            }
            if gateway.callsByTool.count > topTools(gateway).count {
                Facts(label: "More tools",
                      value: "\(gateway.callsByTool.count - topTools(gateway).count)")
            }
        }
    }

    /// Remove on the left, Close on the right; action errors land above.
    @ViewBuilder private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let error {
                Text(error).font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("Remove…", role: .destructive) { confirmingRemove = true }
                    .secondaryButton()
                    .disabled(removing)
                if removing { ProgressView().controlSize(.small) }
                Spacer()
                Button("Close", role: .cancel) { dismiss() }
                    .secondaryButton()
            }
        }
    }

    /// Paste fields for the hub's unresolved ${NAME} references: swe
    /// refuses to write a harness config while one is missing.
    private var needsValues: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Needs values").font(Theme.cardTitle)
            ForEach(server.secretsMissing, id: \.self) { name in
                HStack(spacing: 8) {
                    Text(name)
                        .font(Theme.font(size: 12, design: .monospaced))
                        .foregroundStyle(Theme.textPrimary).lineLimit(1)
                    SecureField("Paste the value",
                                text: Binding(get: { secretDrafts[name] ?? "" },
                                              set: { secretDrafts[name] = $0 }))
                        .sweField(monospaced: true)
                    Button(savingSecret == name ? "Saving…" : "Save") {
                        Task { await saveSecret(name) }
                    }
                    .secondaryButton()
                    .disabled((secretDrafts[name] ?? "").isEmpty || savingSecret != nil)
                }
            }
            Text("Saved to ~/.swe/secrets/mcp/.api_keys, never versioned. "
                 + "Sync fills these in when it writes a harness config.")
                .font(Theme.caption).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func saveSecret(_ name: String) async {
        let value = secretDrafts[name] ?? ""
        guard !value.isEmpty else { return }
        savingSecret = name
        error = nil
        defer { savingSecret = nil }
        do {
            try await app.swe.mcpSecretSet(name: name, value: value)
            secretDrafts[name] = ""
            await reload()
            await loadInfo()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func removeServer() async {
        removing = true
        error = nil
        defer { removing = false }
        do {
            let result = try await app.swe.mcpRemove(
                name: server.name,
                from: server.harnesses + (server.inHub ? [McpServer.hubSource] : []))
            await reload()
            if result.failed.isEmpty {
                dismiss()
            } else {
                // A copy left in a file adopts back into the hub on the
                // next sync, so keep the sheet open and say where it is.
                error = "\(server.name) could not be removed from "
                    + result.failed.map { HarnessMark($0).name }.joined(separator: ", ")
            }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct Facts: View {
    var label: String
    var value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(label).font(Theme.caption).foregroundStyle(Theme.textSecondary).frame(width: detailLabelWidth, alignment: .leading)
            Text(value).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Server folders

/// What is left of the old "Server folders" section: only the states the
/// grouped list cannot show. A good scan renders nothing here, since its
/// folders are the Local rows up in the grid.
private struct McpRepoSection: View {
    var model: McpServerRepoModel
    var root: URL
    /// The grid's harness columns, so the scan's shimmer matches them.
    var harnesses: [String] = []

    var body: some View {
        if let error = model.error {
            ErrorCard(message: error)
        } else if !FileManager.default.fileExists(atPath: root.path) {
            Card {
                Label("\(displayPath(root)) doesn't exist. Set the folder in Settings > Files.",
                      systemImage: "folder.badge.questionmark")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
            }
        } else if model.servers.isEmpty && model.isLoading {
            McpGridSkeleton(harnesses: harnesses, rows: 4, label: "Scanning server folders…")
        } else if model.servers.isEmpty {
            Card {
                Text("No server folders in \(displayPath(root)).")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
            }
        }
    }
}

/// A folder in the servers folder that no hub server uses yet: the
/// folder's details in the name column and nothing in the harness cells.
private struct FolderRow: View {
    var folder: McpRepoServer
    var harnesses: [String]
    /// False when the hub never loaded: the caption hides rather than
    /// claim "Not in the hub" on a guess.
    var hubKnown = true
    var editor: CodeEditor?

    var body: some View {
        HStack(spacing: 0) {
            RepoFolderRow(folder: folder, usedBy: hubKnown ? [] : nil, editor: editor)
                // Its buttons end where a server row's do, not 8pt further right.
                .padding(.trailing, ServerTable.actionsGap)
                .frame(minWidth: ServerTable.nameMinWidth, maxWidth: .infinity, alignment: .leading)
            columnSeparator()
            // A folder has no type and no harness: a dash per fixed
            // column, like any server a harness lacks.
            Image(systemName: "minus").foregroundStyle(Theme.borderStrong)
                .hoverTip("Folders have no type")
                .frame(width: ServerTable.typeWidth)
            ForEach(harnesses, id: \.self) { harness in
                columnSeparator()
                Image(systemName: "minus").foregroundStyle(Theme.borderStrong)
                    .hoverTip("Not in \(HarnessMark(harness).name)")
                    .frame(width: ServerTable.cellWidth)
            }
        }
        .padding(.vertical, 8)
    }
}

/// The Local rows' folder buttons: Show in Finder and Open in the
/// editor. One folder gets plain buttons; a server backed by several
/// gives each button a menu to pick the folder.
private struct FolderActions: View {
    var folders: [McpRepoServer]
    var editor: CodeEditor?

    private var single: McpRepoServer? { folders.count == 1 ? folders[0] : nil }

    var body: some View {
        HStack(spacing: 8) {
            if let folder = single {
                Button("Show in Finder", systemImage: "folder") {
                    NSWorkspace.shared.activateFileViewerSelecting([folder.path])
                }
                .iconButton()
                .help("Show \(folder.name) in Finder")
            } else if !folders.isEmpty {
                Menu {
                    ForEach(folders) { folder in
                        Button(Self.menuTitle(folder)) {
                            NSWorkspace.shared.activateFileViewerSelecting([folder.path])
                        }
                    }
                } label: { actionIcon("folder") }
                .menuStyle(.button).menuIndicator(.hidden)
                .plainButton(cornerRadius: Theme.iconButtonRadius).fixedSize()
                .help("Show in Finder")
            }
            if let editor {
                if let folder = single {
                    Button("Open in \(editor.name)", systemImage: "chevron.left.forwardslash.chevron.right") {
                        NSWorkspace.shared.open([folder.path], withApplicationAt: editor.appURL,
                                                configuration: .init())
                    }
                    .iconButton()
                    .help("Open \(folder.name) in \(editor.name)")
                } else if !folders.isEmpty {
                    Menu {
                        ForEach(folders) { folder in
                            Button(Self.menuTitle(folder)) {
                                NSWorkspace.shared.open([folder.path], withApplicationAt: editor.appURL,
                                                        configuration: .init())
                            }
                        }
                    } label: { actionIcon("chevron.left.forwardslash.chevron.right") }
                    .menuStyle(.button).menuIndicator(.hidden)
                    .plainButton(cornerRadius: Theme.iconButtonRadius).fixedSize()
                    .help("Open in \(editor.name)")
                }
            }
        }
    }

    /// "linear-mcp · main ↑1 ↓2 · Local changes" so a folder picker names
    /// its checkout the way the row's caption does, or just the name
    /// while git is still answering.
    private static func menuTitle(_ folder: McpRepoServer) -> String {
        var parts = [folder.name]
        if let git = folder.git {
            if git.isRepo {
                parts.append(git.branch ?? "detached HEAD")
                let counts = [git.ahead > 0 ? "↑\(git.ahead)" : nil,
                              git.behind > 0 ? "↓\(git.behind)" : nil].compactMap { $0 }
                if !counts.isEmpty { parts.append(counts.joined(separator: " ")) }
                if git.hasChanges { parts.append("Local changes") }
            } else {
                parts.append("Not a git repo")
            }
        }
        return parts.joined(separator: " · ")
    }

    /// The icon buttons' chrome, applied by hand since IconButtonStyle is
    /// a ButtonStyle and a Menu cannot take it.
    private func actionIcon(_ name: String) -> some View {
        Image(systemName: name)
            .font(Theme.font(size: 16))
            .foregroundStyle(Theme.textNav)
            .frame(width: Theme.iconButton, height: Theme.iconButton)
            .overlay(RoundedRectangle(cornerRadius: Theme.iconButtonRadius).strokeBorder(Theme.border, lineWidth: 1))
            .contentShape(.rect(cornerRadius: Theme.iconButtonRadius))
    }
}

/// A folder's git state as dot-separated caption pieces: the branch, the
/// ahead/behind counts and a Local changes badge; "Not a git repo" when
/// the folder isn't a repository, or a spinner while git answers.
private struct GitPieces: View {
    var folder: McpRepoServer

    var body: some View {
        if let git = folder.git {
            if git.isRepo {
                Text(git.branch ?? "detached HEAD").foregroundStyle(Theme.textSecondary)
                let counts = [git.ahead > 0 ? "↑\(git.ahead)" : nil,
                              git.behind > 0 ? "↓\(git.behind)" : nil].compactMap { $0 }
                if !counts.isEmpty {
                    Text(counts.joined(separator: " ")).foregroundStyle(Theme.textSecondary).monospacedDigit()
                }
                if git.hasChanges {
                    Text("·")
                    Badge("Local changes")
                }
            } else {
                Text("Not a git repo")
            }
        } else {
            ProgressView().controlSize(.mini)
        }
    }
}

/// A folder in the servers folder that no hub server uses yet: its name
/// as the title, "Not in the hub · main" folded into the secondary line,
/// then Show in Finder and Open in editor.
private struct RepoFolderRow: View {
    var folder: McpRepoServer
    /// nil hides the hub caption (the inventory never loaded); [] shows
    /// "Not in the hub".
    var usedBy: [String]?
    var editor: CodeEditor?

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(folder.name)
                    .font(Theme.font(size: 14, weight: .medium, design: .monospaced))
                    .foregroundStyle(Theme.textPrimary).lineLimit(1)
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    if let usedBy {
                        Text(usedBy.isEmpty ? "Not in the hub" : "Used by \(usedBy.joined(separator: ", "))")
                            .foregroundStyle(usedBy.isEmpty ? Theme.textMuted : Theme.textSecondary)
                            .lineLimit(1)
                        Text("·")
                    }
                    GitPieces(folder: folder)
                }
                .font(Theme.caption).foregroundStyle(Theme.textMuted)
            }
            Spacer(minLength: 8)
            FolderActions(folders: [folder], editor: editor)
        }
    }
}

/// The Add sheet's local-card title: the machine whose harnesses launch a
/// command server. The Mac app manages this Mac's harnesses.
/// Read once: `Host.current()` can stall for seconds on a misconfigured
/// network, and this is read in view bodies on the main thread.
let mcpLocalTitle: String = {
    let name = SCDynamicStoreCopyComputerName(nil, nil) as String?
    return "Local: runs on \(name ?? "this Mac")"
}()

// MARK: - Loading

/// The table's loading shape: the "Server" and "Type" headers and the
/// harness mark columns at their real widths, then rows of name lines
/// and cell marks where the server's cells will land.
private struct McpGridSkeleton: View {
    var harnesses: [String]
    var rows = 6
    var label: String
    /// The card's inner width: the table fills it, and scrolls only past it.
    @State private var available: CGFloat = 0

    var body: some View {
        Card {
            ScrollView(.horizontal) {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 0) {
                        SkeletonBlock(width: 44, height: 10)
                            .frame(minWidth: ServerTable.nameMinWidth, maxWidth: .infinity, alignment: .leading)
                        columnSeparator()
                        SkeletonBlock(width: 24, height: 10)
                            .frame(width: ServerTable.typeWidth)
                        ForEach(harnesses, id: \.self) { _ in
                            columnSeparator()
                            SkeletonBlock(width: 18, height: 18, radius: 4)
                                .frame(width: ServerTable.cellWidth)
                        }
                    }
                    .padding(.bottom, 8)
                    Divider().overlay(Theme.border)
                    ForEach(0..<rows, id: \.self) { row in
                        rowBody(row)
                        if row < rows - 1 { Divider().overlay(Theme.border) }
                    }
                }
                .frame(width: max(available, ServerTable.nameMinWidth
                                  + ServerTable.fixedTail(columns: harnesses.count)),
                       alignment: .leading)
            }
            .scrollIndicators(.automatic)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { available = $0 }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(label))
    }

    private func rowBody(_ row: Int) -> some View {
        HStack(spacing: 0) {
            HStack(alignment: .top, spacing: 8) {
                SkeletonBlock(width: 36, height: 20, radius: 10)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 4) {
                    SkeletonBlock(width: 130 - CGFloat(row % 3) * 22, height: 13)
                    SkeletonBlock(width: 180 - CGFloat(row % 2) * 44, height: 10)
                }
            }
            .frame(minWidth: ServerTable.nameMinWidth, maxWidth: .infinity, alignment: .leading)
            columnSeparator()
            SkeletonBlock(width: 44, height: 12, radius: 5)
                .frame(width: ServerTable.typeWidth)
            ForEach(harnesses, id: \.self) { _ in
                columnSeparator()
                SkeletonBlock(width: 14, height: 14, radius: 7)
                    .frame(width: ServerTable.cellWidth)
            }
        }
        .padding(.vertical, 8)
    }
}
