import SWECore
import SwiftUI

/// The token and spend columns, shared between the header and each row.
let tokensColumnWidth: CGFloat = 80
let spendColumnWidth: CGFloat = 84

/// Every recent session across harnesses, with what it cost. Modelled on
/// TokenTracker's Sessions page: search and filters, a summary strip, then
/// the sessions grouped by the day they were last active.
struct SessionsPage: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        if let model = app.sessions {
            SessionsContent(model: model)
        } else {
            Page(title: "Sessions", subtitle: "Local and cloud sessions, usage and estimated cost.") {
                ErrorCard(message: app.overviewError ?? "The usage ledger is unavailable.")
            }
        }
    }
}

private struct SessionsContent: View {
    @Environment(AppModel.self) private var app
    @Bindable var model: SessionsModel
    @State private var detail: SessionsModel.Row?
    /// The New session sheet is up.
    @State private var showsNewSession = false
    /// The session whose chat is swapped in over the list.
    @State private var chat: SessionsModel.Row?
    /// The newest click, so a slower lookup for an older one can't land.
    @State private var opening: UUID?
    @State private var actionError: String?
    /// What the last Restore transcript did: swe's answer, or its refusal.
    @State private var restoreNote: (text: String, ok: Bool)?
    /// A restore is running; a second click waits for it rather than
    /// racing it into a refusal.
    @State private var restoring = false
    /// The hub refused a pin or unpin; the row went back.
    @State private var pinNote: String?
    /// Rename or Fork's sheet, from a row's menu or the chat's header.
    @State private var hubSheet: SessionHubSheet?
    /// Rows a day's group shows before folding behind a Show more row.
    private static let foldLimit = 10
    /// Days the user has unfolded.
    @State private var openedDays: Set<Date> = []
    /// This Mac's hub machine id from machine.json, read once per visit:
    /// its rows are the list above, never the Other machines group.
    @State private var localMachineId: String?
    /// Dismissed hints stay dismissed.
    @AppStorage("devinKeyHintDismissed") private var devinKeyHintDismissed = false
    @AppStorage("settingsSection") private var settingsSection = ""

    /// The Devin-key hint, until the user has Devin and a key, or closes it.
    private var showDevinHint: Bool {
        DevinKeyHint.shows(
            // Cloud-only users count too: their stored sessions stop
            // updating once the key is gone.
            hasDevin: model.harnesses.contains { DevinKeyHint.isDevin(harness: $0) }
                || app.others.cliInstalled["devin"] == true,
            keySet: app.services.statuses["DEVIN_API_KEY"]?.isSet,
            dismissed: devinKeyHintDismissed)
    }

    var body: some View {
        Group {
            if let chat {
                HStack(alignment: .top, spacing: 24) {
                    SessionChatPage(row: chat, onBack: { self.chat = nil }, onDetails: { detail = chat },
                                    onMissing: {
                                        self.chat = nil
                                        detail = chat
                                    },
                                    act: { run($0, on: chat) }, note: pinNote,
                                    restore: { restore(chat) }, restoreNote: restoreNote)
                        .frame(maxWidth: .infinity)
                        .onChange(of: app.navReselects) { self.chat = nil }
                    SessionRail(model: model, current: chat, select: { self.chat = $0 })
                        .frame(width: 220)
                        .padding(.trailing, 40)
                        .padding(.vertical, 36)
                }
            } else {
                listPage
            }
        }
        .cardSheet(item: $detail) { row in
            SessionDetail(row: row, title: model.hubOverlay.title(for: row),
                          resume: { resume(row) }, restore: { restore(row) }, close: { detail = nil })
        }
        .cardSheet(item: $hubSheet) { sheet in
            switch sheet.kind {
            case .rename:
                SessionRenameSheet(key: sheet.key, current: model.hubOverlay.title(for: sheet.row) ?? "")
            case .fork:
                SessionForkSheet(key: sheet.key, row: sheet.row,
                                 title: model.hubOverlay.title(for: sheet.row) ?? sheet.row.session.id)
            }
        }
        // The hub's pins and names, read with its keys, over the rows swe
        // listed: the Pinned group, hub titles and the rows' hub actions.
        .onChange(of: app.hub.overlay, initial: true) { model.hubOverlay = app.hub.overlay }
        // A launch the sheet lands refreshes the list behind the chat
        // it opens.
        .cardSheet(isPresented: $showsNewSession) {
            NewSessionSheet()
        }
        // Another page started a session (Resolve with agent) and asked
        // for its chat.
        .task(id: app.pendingChat) {
            guard let key = app.pendingChat else { return }
            // Cleared only once the chat is open: clearing changes this
            // task's id, which would cancel the hub lookup mid-way.
            await openChat(key: key)
            if app.pendingChat == key { app.pendingChat = nil }
        }
        // The ⌘K search's session pick opens like a row click: the chat
        // when the hub can show it, the details otherwise. The row id
        // decides which of two same-session rows (one per account) the
        // pick meant; the key alone can't.
        .task(id: app.pendingSession) {
            guard let pending = app.pendingSession else { return }
            if let row = model.rows.first(where: { $0.id == pending.rowId })
                ?? model.rows.first(where: {
                    HubSessionRow.key(harness: $0.session.harness, id: $0.session.id) == pending.key
                }) {
                open(row)
            } else {
                await openChat(key: pending.key)
            }
            if app.pendingSession == pending { app.pendingSession = nil }
        }
    }

    /// Opens `key`'s chat directly. The ledger may not list a session
    /// the hub started seconds ago, so its row comes from the hub's
    /// listing instead, and the ledger lists again behind it.
    private func openChat(key: String) async {
        let click = UUID()
        opening = click
        var hubRow: HubSessionRow?
        let listed = model.rows.contains {
            HubSessionRow.key(harness: $0.session.harness, id: $0.session.id) == key
        }
        if !listed {
            hubRow = try? await app.hub.withClient { try await $0.sessions() }.first { $0.key == key }
        }
        guard opening == click,
              let row = SessionsModel.chatRow(for: key, rows: model.rows, hub: hubRow) else { return }
        detail = nil
        chat = row
        // Not tied to the task above, which ends when the key clears.
        let hub = app.hub
        let model = model
        let swePath = app.swePath
        Task {
            async let keys: Void = hub.refreshKeys()
            await model.load(for: swePath)
            await keys
        }
    }

    private var listPage: some View {
        Page(title: "Sessions", subtitle: "Local and cloud sessions, usage and estimated cost.") {
            Button("New session", systemImage: "plus") {
                showsNewSession = true
            }
                .secondaryButton()
                .keyboardShortcut("n", modifiers: .command)
                .hoverTip("Start a session on this Mac's hub (⌘N)")
            Button("Refresh", systemImage: "arrow.clockwise") {
                Task {
                    // Pins and renames from other devices ride this read.
                    async let hubKeys: Void = app.hub.refreshKeys()
                    await model.load(for: app.swePath)
                    // A scan reloads the figures through completedScans below.
                    await app.overview?.load(scan: true)
                    await hubKeys
                }
            }
                .iconButton()
                .disabled(model.isLoading)
                .keyboardShortcut("r", modifiers: .command)
                .help("List sessions again (⌘R)")
        } content: {
            FilterBar(model: model)
            SummaryStrip(summary: model.summary)
            if let error = model.error ?? actionError ?? pinNote {
                Card {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(Theme.bodySmall).foregroundStyle(Theme.warningText)
                }
            }
            if let restoreNote {
                RestoreNoteCard(note: restoreNote) { self.restoreNote = nil }
            }
            if !model.failures.isEmpty {
                Card {
                    Label("Couldn't read: \(model.failures.joined(separator: "; "))", systemImage: "exclamationmark.circle")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                }
            }
            if let cloudError = model.cloudError {
                Card {
                    Label("Devin cloud sessions didn't update: \(cloudError)", systemImage: "exclamationmark.circle")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                }
            }
            if showDevinHint {
                Card {
                    HStack(spacing: 10) {
                        Label("Devin cloud sessions need a Devin API key. Local Devin sessions still show.",
                              systemImage: "info.circle")
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        Spacer()
                        Button("Open Settings › Integrations") {
                            settingsSection = SettingsSection.integrations.rawValue
                            app.selection = .settings
                        }
                        .secondaryButton()
                        if let devin = ServiceCredentials.services.first(where: { $0.id == "devin" }) {
                            Link(destination: devin.keyURL) {
                                Text("Get a key").font(Theme.caption).foregroundStyle(Theme.link)
                            }
                        }
                        Button { devinKeyHintDismissed = true } label: {
                            Image(systemName: "xmark")
                        }
                        .iconButton()
                        .hoverTip("Don't show this again")
                    }
                }
            }
            if model.isCapped {
                Card {
                    Label("Showing the most recent \(model.rows.count) sessions; older ones are left out.",
                          systemImage: "info.circle")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                }
            }
            list
        }
        // Opening the page again within a minute shows the same list.
        .task(id: app.swePath) {
            let home = AppHome.url()
            localMachineId = await offMain { IssueResolve.localMachineId(home: home) }
            async let keyStatus: Void = app.services.reload()
            async let hubKeys: Void = app.hub.refreshKeys()
            await model.loadIfStale(for: app.swePath)
            await keyStatus
            await hubKeys
        }
        // New usage from a scan, a feed import or a price change, without
        // listing the sessions again. A visit with none since the last
        // one reads nothing.
        .task(id: [app.overview?.completedScans ?? 0, app.overview?.revision ?? 0]) {
            await model.refreshUsage(afterScans: app.overview?.completedScans ?? 0,
                                     ledgerRevision: app.overview?.revision ?? 0)
        }
    }

    /// Pin runs at once (a refusal puts the row back with a note); Rename
    /// and Fork open their sheet. Only rows the hub lists offer these.
    private func run(_ action: SessionMenuAction, on row: SessionsModel.Row) {
        guard let key = HubSessionOverlay.key(row) else { return }
        switch action {
        case .pin(let pinned):
            Task { pinNote = await app.hub.setPinned(key, pinned) }
        case .rename:
            hubSheet = SessionHubSheet(kind: .rename, key: key, row: row)
        case .fork:
            hubSheet = SessionHubSheet(kind: .fork, key: key, row: row)
        }
    }

    /// A click opens the session's chat when this Mac's hub can show it,
    /// else its details: cloud sessions, ones the hub doesn't list, or
    /// any while the hub is down.
    private func open(_ row: SessionsModel.Row) {
        let click = UUID()
        opening = click
        guard row.location == .local else { return detail = row }
        let key = HubSessionRow.key(harness: row.session.harness, id: row.session.id)
        if app.hub.opensChat(key) { return chat = row }
        // The list may predate a session started since.
        Task {
            await app.hub.refreshKeys()
            guard opening == click else { return }
            if app.hub.opensChat(key) { chat = row } else { detail = row }
        }
    }

    @ViewBuilder private var list: some View {
        let groups = model.groupedByDay()
        let pinned = model.pinnedRows()
        if model.rows.isEmpty && model.isLoading {
            SkeletonRows(rows: 8, framed: false, shape: .session, label: "Listing sessions…")
        } else if groups.isEmpty && pinned.isEmpty {
            // Every matching row can be trivial: the footer still shows
            // what was hidden and offers "Show" to reveal them.
            if model.rows.isEmpty {
                EmptyCard(text: "No sessions listed.", icon: "clock",
                          actionTitle: "Open Harnesses") { app.selection = .harnesses }
                otherMachines
            } else {
                EmptyCard(text: "No sessions match these filters.", icon: "clock",
                          actionTitle: "Clear filters") {
                    model.filter = .init()
                    // The default window is 90 days; clearing means all time.
                    model.filter.window = .all
                }
                otherMachines
            }
            hiddenFooter
        } else {
            VStack(spacing: 0) {
                HStack(spacing: 16) {
                    Text("Session / project").frame(maxWidth: .infinity, alignment: .leading)
                    Text("Tokens").frame(width: tokensColumnWidth, alignment: .trailing)
                    Text("Spend").frame(width: spendColumnWidth, alignment: .trailing)
                    Color.clear.frame(width: Theme.iconButton)
                }
                .font(Theme.tableHeader).tracking(0.12).foregroundStyle(Theme.tableHeaderText)
                // The rows' side padding, so each heading sits over its column.
                .padding(.horizontal, 8)
                .padding(.bottom, 10)
                Divider().overlay(Theme.border)

                LazyVStack(alignment: .leading, spacing: 0) {
                    // Pinned on the hub: first, in the order they were
                    // pinned, whatever the window, like the web and iPhone.
                    if !pinned.isEmpty {
                        Text("Pinned")
                            .font(Theme.rowTitle).foregroundStyle(Theme.textPrimary)
                            .padding(.horizontal, 8)
                            .padding(.top, 18).padding(.bottom, 10)
                        Divider().overlay(Theme.border)
                        // Their own ids: a row pinned while on screen
                        // would otherwise keep its day-group view, which
                        // LazyVStack matches by id and never redraws.
                        ForEach(pinned, id: \.pinnedID) { row in
                            sessionRow(row)
                            Divider().overlay(Theme.border)
                        }
                    }
                    ForEach(groups) { group in
                        Text(group.day, format: .dateTime.month(.abbreviated).day().year())
                            .font(Theme.rowTitle).foregroundStyle(Theme.textPrimary)
                            .padding(.horizontal, 8)
                            .padding(.top, 18).padding(.bottom, 10)
                        Divider().overlay(Theme.border)
                        let shown = RowFold.visible(group.rows, limit: Self.foldLimit,
                                                    expanded: openedDays.contains(group.day))
                        ForEach(shown) { row in
                            sessionRow(row)
                            Divider().overlay(Theme.border)
                        }
                        if RowFold.hiddenCount(total: group.rows.count, limit: Self.foldLimit) > 0 {
                            ExpandRow(isExpanded: openedDays.contains(group.day),
                                      hidden: RowFold.hiddenCount(total: group.rows.count, limit: Self.foldLimit),
                                      help: (expand: "Show every session from this day",
                                             collapse: "Show only the first \(Self.foldLimit)"),
                                      edge: .inset) {
                                withAnimation(.easeOut(duration: Theme.fadeDuration)) {
                                    if openedDays.contains(group.day) { openedDays.remove(group.day) }
                                    else { openedDays.insert(group.day) }
                                }
                            }
                        }
                    }
                    // Below this Mac's groups; the filters don't reach it.
                    otherMachines
                }
                hiddenFooter
            }
        }
    }

    /// The hub's sessions on other machines, from the read that fills the
    /// overlay; gone while no satellite lists one or the hub is down.
    @ViewBuilder private var otherMachines: some View {
        let group = OtherMachines.build(rows: app.hub.hubRows, machines: app.hub.hubMachines,
                                        localMachineId: localMachineId)
        if !group.isEmpty {
            OtherMachinesGroup(group: group, meta: { app.hub.meta(for: $0) },
                               unread: { app.hub.isUnread($0) },
                               open: { open($0) }, act: { action, row in run(action, on: chatRow(row)) })
        }
    }

    /// The chat row for a satellite session: its hub key reads as
    /// `harness:id@machine`, and the chat opens it through the hub like
    /// the iPhone does.
    /// Its title is the harness's own, never the hub name the last read
    /// carried: the overlay lays the current name over it, so a name
    /// cleared here doesn't linger in the chat header or Rename sheet.
    private func chatRow(_ other: OtherMachines.Row) -> SessionsModel.Row {
        let active = other.hub.lastActiveAt.map { Date(timeIntervalSince1970: $0) } ?? .now
        var hub = other.hub
        let own = (hub.harnessTitle ?? hub.title)?.trimmingCharacters(in: .whitespacesAndNewlines)
        hub.title = own?.isEmpty == false ? own : nil
        return SessionsModel.chatRow(for: other.key, rows: [], hub: hub, now: active)
            ?? SessionsModel.Row(session: SessionInfo(id: other.sessionId, harness: other.hub.harness ?? "",
                                                      agent: other.hub.agent ?? "", cwd: other.hub.cwd,
                                                      title: hub.title, titleSource: nil, lastActiveAt: active,
                                                      status: other.hub.status, resume: nil),
                                 usage: nil)
    }

    private func open(_ other: OtherMachines.Row) {
        opening = UUID()
        detail = nil
        chat = chatRow(other)
    }

    private func sessionRow(_ row: SessionsModel.Row) -> some View {
        SessionRow(row: row, project: model.project(for: row.session),
                   hubModel: app.hub.model(for: HubSessionRow.key(
                       harness: row.session.harness, id: row.session.id)),
                   hub: model.hubOverlay.meta(for: row),
                   unread: app.hub.isUnread(row),
                   match: model.searchHit(for: row.session.id),
                   open: { open(row) }, showDetails: { detail = row },
                   resume: { resume(row) }, restore: { restore(row) }, act: { run($0, on: row) })
    }

    /// "N short or empty sessions hidden" plus the Show/Hide toggle,
    /// under the list and under the empty card alike.
    @ViewBuilder private var hiddenFooter: some View {
        if model.hideTrivial, model.hiddenTrivialCount > 0 {
            HStack(spacing: 8) {
                Text(model.showTrivial
                     ? "Showing \(model.hiddenTrivialCount) short or empty sessions"
                     : "\(model.hiddenTrivialCount) short or empty sessions hidden")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                Spacer()
                Button(model.showTrivial ? "Hide" : "Show") {
                    model.showTrivial.toggle()
                }
                .font(Theme.caption)
                .buttonStyle(.plain)
                .foregroundStyle(Theme.textSecondary)
            }
            .padding(.horizontal, 8)
            .padding(.top, 10)
        }
    }

    private func resume(_ row: SessionsModel.Row) {
        Task {
            do {
                try await app.resume(row)
                actionError = nil
            } catch {
                actionError = "Couldn't resume: \(error.localizedDescription)"
            }
        }
    }

    /// Restore transcript: swe puts the mirror copy back, off the main
    /// actor, and the list is read again so the row turns live.
    private func restore(_ row: SessionsModel.Row) {
        guard !restoring else { return }
        restoring = true
        restoreNote = (text: "Restoring the transcript…", ok: true)
        Task {
            let result = await app.restore(row)
            restoring = false
            restoreNote = (text: result.message, ok: result.ok)
            // Back to the list: the chat it was opened from now reads a
            // live file, which this view's copy of the row does not know.
            if result.ok, chat?.id == row.id { chat = nil }
        }
    }
}

/// The result of Restore transcript, above the list or the chat.
struct RestoreNoteCard: View {
    var note: (text: String, ok: Bool)
    var dismiss: (() -> Void)? = nil

    var body: some View {
        Card {
            HStack(spacing: 10) {
                Label(note.text, systemImage: note.ok ? "arrow.uturn.backward.circle" : "exclamationmark.triangle")
                    .font(Theme.bodySmall)
                    .foregroundStyle(note.ok ? Theme.textSecondary : Theme.warningText)
                Spacer()
                if let dismiss {
                    Button(action: dismiss) { Image(systemName: "xmark") }
                        .iconButton()
                        .help("Dismiss")
                        .accessibilityLabel("Dismiss")
                }
            }
        }
    }
}

/// A mirrored session's tag, in the status badge's slot: the harness
/// deleted the transcript and swe lists the read-only copy it kept.
struct MirroredTag: View {
    var body: some View {
        Badge("Mirrored")
            .help(SessionInfo.mirroredHint)
            .accessibilityLabel("Mirrored")
            .accessibilityHint(SessionInfo.mirroredHint)
    }
}

// MARK: - Filters and summary

private struct FilterBar: View {
    @Bindable var model: SessionsModel

    var body: some View {
        // One row when it fits, else the search on its own line.
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                search.frame(minWidth: 180)
                selects
            }
            // Narrow windows: the selects wrap rather than overflow.
            VStack(alignment: .leading, spacing: 10) {
                search
                FlowLayout(spacing: 10, lineSpacing: 10) { selects }
            }
        }
    }

    private var search: some View {
        SearchField(prompt: "Search sessions", text: $model.filter.search)
            .frame(maxWidth: .infinity)
    }

    @ViewBuilder private var selects: some View {
        SelectMenu(options: [(nil, "All tools", nil)] + model.harnesses.map { (Optional($0), UsageFormat.harness($0), $0) },
                   selection: $model.filter.harness,
                   title: model.filter.harness.map(UsageFormat.harness) ?? "All tools")
        SelectMenu(symbol: "calendar", options: SessionsModel.Window.allCases.map { ($0, $0.title) },
                   selection: $model.filter.window)
        CountedMenu(symbol: "folder", allTitle: "All projects",
                    title: selectedProject ?? "All projects",
                    rows: model.projects.map { ($0.key, $0.name, "\($0.count)") },
                    selection: $model.filter.project)
        CountedMenu(allTitle: "All models",
                    title: model.filter.model ?? "All models",
                    rows: model.models.map { ($0.model, $0.model, UsageFormat.tokens($0.tokens)) },
                    selection: $model.filter.model,
                    limit: 30,
                    logo: { BrandLogo(model: $0, size: 14) })
    }

    /// The selected project's display name for its filter key.
    private var selectedProject: String? {
        model.filter.project.flatMap { key in model.projects.first { $0.key == key }?.name }
    }
}

/// A select whose options carry a count and can number in the hundreds,
/// so it opens a popover with a search field and, when `limit` is set, a
/// "Show all" row that expands past a top slice. Closed it looks like the
/// `SelectMenu` beside it.
private struct CountedMenu: View {
    var symbol: String? = nil
    /// The "no filter" row's text.
    var allTitle: String
    /// The closed control's text: the selection or `allTitle`.
    var title: String
    /// The rows, already in display order: key, label, detail.
    var rows: [(key: String, title: String, detail: String)]
    @Binding var selection: String?
    /// Show only this many rows behind "Show all"; nil shows them all.
    var limit: Int? = nil
    /// A search field appears once there are more rows than this.
    var searchThreshold = 15
    /// A logo before each row's title, by key; the model filter passes
    /// the brand logo, the project filter none.
    var logo: ((String) -> BrandLogo)? = nil
    @State private var open = false
    @State private var query = ""
    @State private var showAll = false
    @State private var hovering = false

    private var needle: String { query.trimmingCharacters(in: .whitespaces).lowercased() }

    /// The rows to list: the matches while searching, else the top slice.
    private var shown: [(key: String, title: String, detail: String)] {
        if !needle.isEmpty {
            return rows.filter { $0.title.lowercased().contains(needle) }
        }
        if let limit, !showAll, rows.count > limit { return Array(rows.prefix(limit)) }
        return rows
    }

    var body: some View {
        Button { open.toggle() } label: {
            HStack(spacing: 6) {
                if let symbol { Image(systemName: symbol).foregroundStyle(Theme.textSecondary) }
                Text(title).foregroundStyle(Theme.textPrimary).lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 4)
                Image(systemName: "chevron.down").font(Theme.font(size: 12))
                    .foregroundStyle(Theme.textSecondary)
            }
            .font(Theme.bodySmall)
            .padding(.horizontal, 12)
            .frame(width: 168, height: Theme.fieldHeight)
            .background(Theme.field, in: .rect(cornerRadius: Theme.selectRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.selectRadius)
                .strokeBorder(hovering || open ? Theme.borderHover : Theme.border, lineWidth: 1))
            .contentShape(.rect(cornerRadius: Theme.selectRadius))
        }
        .plainButton(cornerRadius: Theme.selectRadius)
        .onHover { hovering = $0 }
        .popover(isPresented: $open, arrowEdge: .bottom) { popover }
        .onChange(of: open) {
            if !open { query = ""; showAll = false }
        }
    }

    private var popover: some View {
        VStack(alignment: .leading, spacing: 0) {
            if rows.count > searchThreshold {
                SearchField(prompt: "Search", text: $query)
                    .padding(8)
                Divider().overlay(Theme.border)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    option(key: nil, title: allTitle, detail: nil)
                    Divider().overlay(Theme.border)
                    ForEach(shown, id: \.key) { row in
                        option(key: row.key, title: row.title, detail: row.detail)
                    }
                    if let limit, rows.count > limit, !showAll, needle.isEmpty {
                        Divider().overlay(Theme.border)
                        Button {
                            showAll = true
                        } label: {
                            Text("Show all (\(rows.count))")
                                .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                                .padding(.horizontal, 8).padding(.vertical, 6)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .contentShape(.rect)
                        }
                        .plainButton(cornerRadius: Theme.controlRadius)
                    }
                }
                .padding(6)
            }
            .frame(maxHeight: 360)
        }
        .frame(width: 300)
    }

    private func option(key: String?, title: String, detail: String?) -> some View {
        Button {
            selection = key
            open = false
        } label: {
            HStack(spacing: 8) {
                if let key, let logo { logo(key) }
                Text(title)
                    .font(Theme.bodySmall)
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                Spacer(minLength: 8)
                if let detail {
                    Text(detail).font(Theme.caption).foregroundStyle(Theme.textMuted).monospacedDigit()
                }
                Image(systemName: "checkmark")
                    .font(Theme.font(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                    .opacity(selection == key ? 1 : 0)
            }
            .padding(.horizontal, 8).padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(.rect)
        }
        .plainButton(cornerRadius: Theme.controlRadius)
    }
}

private struct SummaryStrip: View {
    var summary: SessionsModel.Summary

    var body: some View {
        HStack(spacing: 0) {
            cell("Matching sessions", Text("\(summary.sessions)"))
            Divider().overlay(Theme.border).padding(.vertical, 14)
            cell("Tokens", Text(UsageFormat.tokens(summary.totals.totalTokens)))
            Divider().overlay(Theme.border).padding(.vertical, 14)
            cell("Spend", Text(UsageFormat.dollars(summary.totals.costUSD)).foregroundStyle(Theme.accent),
                 note: summary.totals.unpricedTokens > 0
                    ? "Plus \(UsageFormat.tokens(summary.totals.unpricedTokens)) tokens with no list price" : nil)
        }
        .frame(height: 76)
        .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius).strokeBorder(Theme.border, lineWidth: 1))
    }

    private func cell(_ label: String, _ value: Text, note: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Text(label).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                if let note { Image(systemName: "info.circle").font(Theme.caption).foregroundStyle(Theme.textSecondary).hoverTip(note) }
            }
            value.font(Theme.figure).monospacedDigit().foregroundStyle(Theme.textPrimary)
        }
        .padding(.horizontal, 22)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Rows

/// A Sessions row's unread dot, before its title: the hub has activity
/// this Mac hasn't read (`UnreadDots.swift`). The blue the Inbox's dot
/// uses.
struct UnreadDot: View {
    var body: some View {
        Circle()
            .fill(Theme.interactive)
            .frame(width: Theme.statusDot, height: Theme.statusDot)
            .hoverTip("Unread")
    }
}

private extension SessionsModel.Row {
    /// A pinned row's id in the Pinned group, apart from its day-group id.
    var pinnedID: String { "pinned|\(id)" }
}

private struct SessionRow: View {
    var row: SessionsModel.Row
    /// The session's project resolved to its main repository.
    var project: String?
    /// The model of the session's newest reply, from the hub's row; nil
    /// when the hub doesn't list it or doesn't know the model.
    var hubModel: String?
    /// The hub's pin, name and fork flag; nil when the hub doesn't list
    /// the session (or is down), and the menu then has no hub actions.
    var hub: HubSessionMeta?
    /// The hub has activity on it this Mac hasn't read; never for a row
    /// the hub doesn't list.
    var unread = false
    /// The transcript hit this row surfaced on, for the snippet line; nil
    /// when it matched on metadata or content search is off.
    var match: SearchHit?
    /// The chat, or the details when the hub can't show it.
    var open: () -> Void
    var showDetails: () -> Void
    var resume: () -> Void
    /// Restore transcript, for a mirrored row.
    var restore: () -> Void
    var act: (SessionMenuAction) -> Void
    @State private var hovering = false
    @FocusState private var resumeFocused: Bool

    var body: some View {
        let s = row.session
        HStack(alignment: .top, spacing: 16) {
            // The logo centres on the row (title and details together), not
            // on the title line.
            HStack(alignment: .center, spacing: 12) {
                HarnessIcon(harness: HarnessMark(s.harness).icon ?? s.harness, size: 16)
                    .hoverTip(HarnessMark(s.harness).name)
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        if unread { UnreadDot() }
                        Text(hub?.title(over: s.title) ?? s.title ?? s.id)
                            .font(Theme.rowTitle).foregroundStyle(Theme.textPrimary).lineLimit(1)
                        if s.mirrored { MirroredTag() } else { StatusBadge(status: s.status) }
                    }
                    Text(details)
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        .lineLimit(2)
                    // The transcript excerpt that matched the search, on a
                    // terse third line; matched terms stand out in primary.
                    if let match, !match.snippetParts.isEmpty {
                        snippet(match.snippetParts)
                            .font(Theme.caption)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(row.usage.map { UsageFormat.tokens($0.totals.totalTokens) } ?? "–")
                .font(Theme.bodySmall).monospacedDigit().foregroundStyle(Theme.textPrimary)
                .frame(width: tokensColumnWidth, alignment: .trailing)
            Text(row.usage.map { UsageFormat.dollars($0.totals.costUSD) } ?? "–")
                .font(Theme.bodySmall).monospacedDigit()
                .foregroundStyle(row.usage == nil ? Theme.textSecondary : Theme.accent)
                .frame(width: spendColumnWidth, alignment: .trailing)
            Button(s.opensInBrowser ? "Open in Devin" : "Resume",
                   systemImage: s.opensInBrowser ? "arrow.up.right.square" : "play", action: resume)
                .buttonStyle(IconButtonStyle())
                .disabled(s.resume == nil && !s.opensInBrowser)
                // Shown on row hover, or while it has keyboard focus so its
                // ring is visible; otherwise hidden but still reachable.
                .opacity(hovering || resumeFocused ? 1 : 0)
                .focused($resumeFocused)
                .focusRing(resumeFocused, cornerRadius: Theme.iconButtonRadius)
                .hoverTip(s.resume != nil ? "Resume in your terminal"
                          : s.mirrored ? SessionInfo.mirroredHint
                          : s.opensInBrowser ? "Open in Devin"
                          : row.location == .cloud ? "A cloud session can't be resumed here"
                          : "This harness has no resume command",
                          replacesLabel: false)
                .help(s.mirrored ? SessionInfo.mirroredHint : "")
        }
        .padding(.vertical, 12)
        .padding(.horizontal, 8)
        .background(hovering ? Theme.panelHover : .clear, in: .rect(cornerRadius: Theme.controlRadius))
        .contentShape(.rect)
        .onHover { hovering = $0 }
        // A click opens the session; a selection would take it.
        .textSelection(.disabled)
        .onTapGesture(perform: open)
        .contextMenu {
            Button("Open chat", action: open)
            Button("Details…", action: showDetails)
            if s.opensInBrowser {
                Button("Open in Devin", action: resume)
            } else if s.mirrored {
                Button("Resume in terminal", action: resume).disabled(true).help(SessionInfo.mirroredHint)
                Button("Restore transcript", action: restore)
                    .help("Put swe's copy back where \(HarnessMark(s.harness).name) kept it, so it resumes again")
            } else {
                Button("Resume in terminal", action: resume).disabled(s.resume == nil)
            }
            if let hub {
                Divider()
                SessionHubMenuItems(meta: hub, act: act)
            }
            Divider()
            Button("Copy session ID") { copyToPasteboard(s.id) }
            if let cwd = s.cwd {
                Button("Show folder in Finder") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: cwd)])
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAction(named: "Open chat", open)
        .accessibilityAction(named: "Details", showDetails)
        .accessibilityActions {
            if s.mirrored {
                Button("Restore transcript", action: restore)
            } else {
                Button("Resume", action: resume)
            }
            if let hub {
                Button(hub.pinned ? "Unpin" : "Pin") { act(.pin(!hub.pinned)) }
                Button("Rename") { act(.rename) }
                if hub.canFork { Button("Fork") { act(.fork) } }
            }
        }
    }

    /// "match: <snippet>" with each matched term in primary text on the
    /// muted line — subtle, and green stays reserved for money.
    private func snippet(_ parts: [SearchSnippetPart]) -> Text {
        var text = AttributedString("match: ")
        text.foregroundColor = Theme.textMuted
        for part in parts {
            var run = AttributedString(part.text)
            run.foregroundColor = part.matched ? Theme.textPrimary : Theme.textMuted
            text += run
        }
        return Text(text)
    }

    /// project · agent · location · models · time · duration
    var details: String {
        let s = row.session
        var parts: [String] = []
        if let project { parts.append(project) }
        parts.append(row.account ?? HarnessMark(s.harness).name)
        parts.append(row.location.title)
        let models = ModelLabel.rowModels(current: hubModel, used: row.usage?.models ?? [])
        if !models.isEmpty { parts.append(models.joined(separator: ", ")) }
        parts.append(s.lastActiveAt.formatted(date: .omitted, time: .shortened))
        if let duration = row.duration, duration >= 60 { parts.append(Self.duration(duration)) }
        return parts.joined(separator: " · ")
    }

    static func duration(_ seconds: TimeInterval) -> String {
        CalendarFormat.duration(seconds)
    }
}

/// A session's state as the shared badge; nothing for a finished one.
private struct StatusBadge: View {
    var status: String?

    var body: some View {
        if let title = Self.title(status) { Badge(title) }
    }

    static func title(_ status: String?) -> String? {
        switch status {
        case "active": "Active"
        case "error": "Error"
        case "interrupted": "Interrupted"
        case "followup": "Waiting"
        default: nil
        }
    }
}

/// The chat's right-hand rail, a cut-down RepoRail: every session the
/// hub can open a chat for, grouped by day like the list, so a chat can
/// switch sessions without going back.
private struct SessionRail: View {
    @Environment(AppModel.self) private var app
    var model: SessionsModel
    /// The open chat: marked, and kept in the list even when the page's
    /// filters leave it out.
    var current: SessionsModel.Row
    var select: (SessionsModel.Row) -> Void

    /// The main list's rule for a click that opens a chat.
    private func opens(_ row: SessionsModel.Row) -> Bool {
        row.location == .local
            && app.hub.opensChat(HubSessionRow.key(harness: row.session.harness,
                                                 id: row.session.id))
    }

    private var rows: [SessionsModel.Row] {
        var list = model.rows.filter(opens)
        // A refresh leaves the open chat's stale copy in `current`; the
        // row it became is already here, matched by id not value.
        if !list.contains(where: { $0.id == current.id }) {
            list.append(current)
            list.sort { $0.session.lastActiveAt > $1.session.lastActiveAt }
        }
        return list
    }

    /// The list's day captions over the chat-openable rows.
    private var groups: [(day: Date, rows: [SessionsModel.Row])] {
        var out: [(day: Date, rows: [SessionsModel.Row])] = []
        for row in rows {
            let day = Calendar.current.startOfDay(for: row.session.lastActiveAt)
            if out.last?.day == day { out[out.count - 1].rows.append(row) }
            else { out.append((day: day, rows: [row])) }
        }
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 4) {
                Text("Sessions")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .padding(.leading, 8)
                Spacer(minLength: 0)
                RailIconButton(symbol: "arrow.clockwise",
                               help: "List sessions again (⌘R)") {
                    Task {
                        // The rows and the hub's "has a chat" keys: a
                        // session started since is hidden until both land.
                        async let hubKeys: Void = app.hub.refreshKeys()
                        await model.load(for: app.swePath)
                        await hubKeys
                    }
                }
                .keyboardShortcut("r", modifiers: .command)
            }
            ScrollView {
                // Lazy, like the list beside it: the rail can hold every
                // session the listing returned.
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(groups.enumerated()), id: \.offset) { _, group in
                        Text(group.day, format: .dateTime.month(.abbreviated).day().year())
                            .font(Theme.caption).foregroundStyle(Theme.textMuted)
                            .lineLimit(1).truncationMode(.tail)
                            .padding(.horizontal, 8)
                            .padding(.top, 14).padding(.bottom, 4)
                        ForEach(group.rows) { row in
                            RailRow(selected: row.id == current.id, select: { select(row) }) {
                                HarnessIcon(harness: HarnessMark(row.session.harness).icon ?? row.session.harness,
                                            size: 13)
                                // A title reads from its start; only a bare
                                // id keeps both ends.
                                let title = model.hubOverlay.title(for: row)
                                Text(title ?? row.session.id)
                                    .font(Theme.nav)
                                    .lineLimit(1).truncationMode(title == nil ? .middle : .tail)
                                Spacer(minLength: 6)
                                // Named for VoiceOver only: a .help inside the
                                // RailRow's button label never shows, and a
                                // hover tip's tap would take the row's click.
                                if let kind = Self.arrowKind(row.session.status),
                                   let title = StatusBadge.title(row.session.status) {
                                    StateArrow(kind: kind, size: 9, label: title)
                                }
                            }
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
        }
        .padding(8)
        .background(Theme.panel, in: .rect(cornerRadius: Theme.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius)
            .strokeBorder(Theme.border, lineWidth: Theme.hairline))
    }

    /// The list's badge states as a quiver arrow; nothing for a finished
    /// session.
    private static func arrowKind(_ status: String?) -> StateArrows.Kind? {
        switch status {
        case "active": .green
        case "followup": .yellow
        case "error": .red
        case "interrupted": .grey
        default: nil
        }
    }
}

// MARK: - Detail

struct SessionDetail: View {
    /// The resume argv as a line a shell runs as is: a word with a space,
    /// quote or other shell character is single-quoted, plain words stay
    /// bare so the line still reads like a command.
    static func shellLine(_ argv: [String]) -> String {
        argv.map { word in
            !word.isEmpty && word.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || "@%_+=:,./-".contains($0)) })
                ? word : LaunchPlanner.shellQuote(word)
        }.joined(separator: " ")
    }

    /// The widest a value can be: the 640 pt sheet less its padding and
    /// the label column.
    static let valueWidth: CGFloat = 460
    var row: SessionsModel.Row
    /// The title to show: a name set on the hub, else swe's; nil falls
    /// back to swe's title, then the id.
    var title: String? = nil
    var resume: () -> Void
    /// Restore transcript, for a mirrored session; nil leaves it out.
    var restore: (() -> Void)? = nil
    var close: () -> Void

    var body: some View {
        let s = row.session
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    HarnessIcon(harness: HarnessMark(s.harness).icon ?? s.harness, size: 20)
                    Text(title ?? s.title ?? s.id).font(Theme.sectionTitle).tracking(-0.5).lineLimit(2)
                    if s.mirrored { MirroredTag() } else { StatusBadge(status: s.status) }
                }
                if s.mirrored {
                    Text(SessionInfo.mirroredHint)
                        .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                }
            }
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                field("Harness", HarnessMark(s.harness).name)
                field("Location", row.location.title)
                if let parent = row.parent {
                    field(parent.kind == .fork ? "Forked from" : "Started from", parent.title)
                }
                if !s.opensInBrowser { field("Folder", s.cwd ?? "Unknown") }
                field("Last active", s.lastActiveAt.formatted(date: .abbreviated, time: .shortened))
                field("Session id", s.id)
                if let account = row.account ?? row.session.accountHome ?? row.usage?.accountHome {
                    field("Account", account)
                }
                if let usage = row.usage {
                    GridRow(alignment: .top) {
                        Text("Models").font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                        FlowLayout(spacing: 10, lineSpacing: 4) {
                            ForEach(usage.models, id: \.self) { name in
                                HStack(spacing: 5) {
                                    BrandLogo(model: name, size: 14)
                                    Text(name).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                                        .lineLimit(1).truncationMode(.middle)
                                }
                                // A long provider-prefixed or custom name
                                // truncates inside the sheet's value column.
                                .frame(maxWidth: SessionDetail.valueWidth, alignment: .leading)
                            }
                        }
                    }
                }
                if let duration = row.duration, duration >= 60 {
                    field("Ran for", SessionRow.duration(duration))
                }
            }
            if let usage = row.usage {
                HStack(spacing: 0) {
                    figure("Input", UsageFormat.tokens(usage.totals.inputTokens))
                    figure("Cache read", UsageFormat.tokens(usage.totals.cacheReadTokens))
                    figure("Cache write", UsageFormat.tokens(usage.totals.cacheWriteTokens))
                    figure("Output", UsageFormat.tokens(usage.totals.outputTokens))
                    figure("Spend", UsageFormat.dollars(usage.totals.costUSD), money: true)
                }
                .padding(.vertical, 12)
                .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
                .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius).strokeBorder(Theme.border, lineWidth: 1))
                if usage.totals.unpricedTokens > 0 {
                    Text("\(UsageFormat.tokens(usage.totals.unpricedTokens)) tokens from models with no list price.")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                }
            } else {
                Text(s.opensInBrowser ? "Devin's API reports no token or ACU usage for this session."
                     : s.mirrored ? "No token usage recorded for this session in the ledger. A full scan reads it from swe's copy."
                     : "No token usage recorded for this session in the ledger.")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
            }
            if let command = s.resume {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Resume command").font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    Text(Self.shellLine(command.argv))
                        .font(Theme.font(size: 12, design: .monospaced))
                        .textSelection(.enabled)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.subtle, in: .rect(cornerRadius: Theme.controlRadius))
                }
            }
            HStack {
                Button("Copy session ID") { copyToPasteboard(s.id) }.secondaryButton()
                Spacer()
                Button("Close", action: close).keyboardShortcut(.cancelAction).secondaryButton()
                if s.mirrored, let restore {
                    // Nothing can resume a copy; putting it back can.
                    Button("Restore transcript") { restore(); close() }
                        .keyboardShortcut(.defaultAction)
                        .primaryButton()
                        .help("Put swe's copy back where \(HarnessMark(s.harness).name) kept it, so it resumes again")
                } else {
                    Button(s.opensInBrowser ? "Open in Devin" : "Resume in terminal") { resume(); close() }
                        .keyboardShortcut(.defaultAction)
                        .primaryButton()
                        .disabled(s.resume == nil && !s.opensInBrowser)
                        .hoverTip(s.resume != nil ? "Resume in terminal"
                                  : s.mirrored ? SessionInfo.mirroredHint
                                  : s.opensInBrowser ? "Open the session in Devin's web app"
                                  : row.location == .cloud ? "A cloud session can't be resumed here"
                                  : "This session is no longer in \(HarnessMark(s.harness).name)'s history",
                                  replacesLabel: false)
                        .help(s.mirrored ? SessionInfo.mirroredHint : "")
                }
            }
        }
        .padding(24)
        .frame(width: 640)
        .background(Theme.panel)
    }

    @ViewBuilder private func field(_ name: String, _ value: String) -> some View {
        GridRow {
            Text(name).font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
            Text(value).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                .textSelection(.enabled).lineLimit(2).truncationMode(.middle)
        }
    }

    private func figure(_ label: String, _ value: String, money: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(Theme.caption).foregroundStyle(Theme.textSecondary)
            Text(value).font(Theme.rowTitle).monospacedDigit()
                .foregroundStyle(money ? Theme.accent : Theme.textPrimary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14)
    }
}
